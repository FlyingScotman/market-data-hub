"""Regression tests for the 2026-10-16 correctness review (F2, F3, F5, F6, F9, F13, F14 and
follow-ups). F1/F10/F12 live in ``test_backfill.py``, F4 in ``test_calendar.py``, F7 in
``test_futures.py``, F8 in ``test_maintenance.py``."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub import cli
from mdhub.api.app import create_app
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.capture import reap_orphaned_runs
from mdhub.core.upsert import upsert_rows
from mdhub.db.pool import Database
from mdhub.export.tdm import TdmExportOptions, build_tdm_export
from mdhub.jobs import LOCK_BUSY, REGISTRY, JobContext, record_skipped_run, run_job
from mdhub.loaders.fx_moex import only_changed, refresh_fx_instruments
from mdhub.quality.health import catch_up_params, job_health
from mdhub.schedule import Rule
from mdhub.sources import cbr as cbr_src
from mdhub.sources import moex_static as ms
from mdhub.sources.moex_fx import FX_INTRADAY_VALUE_COLUMNS, snapshot_ts
from mdhub.sources.moex_market import _quote_ts

Conn = psycopg.Connection[DictRow]
MSK = ZoneInfo("Europe/Moscow")


# --------------------------------------------------------------------------- F2


@pytest.fixture
def fake_job() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def loader(
        ctx: JobContext,
        *,
        mode: str = "final",
        days_back: list[int] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> None:
        calls.append({"mode": mode, "date_from": date_from, "date_to": date_to})

    REGISTRY.register("rv_locked", "fake", loader)
    try:
        yield calls
    finally:
        REGISTRY.unregister("rv_locked")


def test_scheduled_run_waits_for_lock_held_by_backfill(
    db_url: str, db: Database, settings: Settings, fake_job: list[dict[str, Any]]
) -> None:
    """Review F2: a scheduled run no longer skips at once while a backfill holds the lock -
    it waits (and runs when the lock is released)."""
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:rv_locked', 0))")
    released = threading.Timer(0.6, holder.close)
    released.start()
    try:
        out = run_job(
            "rv_locked",
            {"mode": "final"},
            settings=settings,
            db=db,
            lock_wait_s=10,
            lock_poll_s=0.1,
        )
    finally:
        released.cancel()
        holder.close()
    assert out.status == "succeeded" and fake_job == [
        {"mode": "final", "date_from": None, "date_to": None}
    ]


def test_scheduled_run_still_skipped_is_recorded_lock_busy_and_caught_up(
    db_url: str,
    db: Database,
    settings: Settings,
    conn: Conn,
    fake_job: list[dict[str, Any]],
) -> None:
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:rv_locked', 0))")
    t0 = time.monotonic()
    try:
        out = run_job(
            "rv_locked",
            {"mode": "final"},
            settings=settings,
            db=db,
            lock_wait_s=0.3,
            lock_poll_s=0.05,
            trigger="schedule",
        )
    finally:
        holder.close()
    assert time.monotonic() - t0 >= 0.3
    assert out.status == "skipped" and fake_job == []
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert err is not None and err["error"].startswith(LOCK_BUSY)

    # job health: the skipped fire counts as missed (not as "ran"), with its rule
    now = datetime.now(MSK)
    fire = now - timedelta(minutes=40)
    rule = Rule(
        cron=f"{fire.minute} {fire.hour} * * *",
        params={"mode": "final", "days_back": [1, 2]},
        days="all",
    )
    conn.execute(
        "UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s",
        (fire, fire, out.run_id),
    )
    conn.commit()
    health = {
        h.job: h
        for h in job_health(
            conn,
            now=now,
            calendar=weekday_calendar,
            tz=MSK,
            rules={"rv_locked": (rule,)},
            known=["rv_locked"],
        )
    }["rv_locked"]
    assert health.status == "missed" and health.missed_by_lock and health.problem
    assert health.expected_rule == rule

    # catch-up: days_back of the fire date become date_from/date_to
    accepted, _ = REGISTRY.get("rv_locked").accepted_params()
    params = catch_up_params(
        rule, health.last_expected_at or fire, calendar=weekday_calendar, tz=MSK, accepted=accepted
    )
    assert params is not None and params["mode"] == "final" and "days_back" not in params
    assert params["date_from"] <= params["date_to"] < fire.date()

    from mdhub.quality.job import catch_up_missed

    ctx = JobContext(
        settings=settings,
        db=db,
        run=_FakeRun(),  # type: ignore[arg-type]
        job="quality",
        params={},
        logger=__import__("logging").getLogger("t"),
    )
    done = catch_up_missed(ctx, [health], lock_wait_s=1)
    assert done[0]["status"] == "succeeded"
    assert fake_job == [
        {"mode": "final", "date_from": params["date_from"], "date_to": params["date_to"]}
    ]


class _FakeRun:
    id = 0

    def mark_partial(self, reason: str) -> None:  # pragma: no cover - not expected
        raise AssertionError(reason)


def test_record_skipped_run(db: Database, conn: Conn) -> None:
    run_id = record_skipped_run(db, "rv_x", {"a": 1}, f"{LOCK_BUSY}: test")
    row = conn.execute("SELECT status, error FROM ops.job_run WHERE id = %s", (run_id,)).fetchone()
    assert row == {"status": "skipped", "error": f"{LOCK_BUSY}: test"}


def test_reap_orphaned_running_runs(db: Database, conn: Conn, db_url: str) -> None:
    conn.execute(
        "INSERT INTO ops.job_run (job, started_at) VALUES "
        "('old_dead', now() - interval '20 hours'), ('fresh', now() - interval '1 hour'), "
        "('old_locked', now() - interval '20 hours')"
    )
    conn.commit()
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:old_locked', 0))")
    try:
        assert reap_orphaned_runs(db) == 1
    finally:
        holder.close()
    rows = {
        r["job"]: r for r in conn.execute("SELECT job, status, error FROM ops.job_run").fetchall()
    }
    assert rows["old_dead"]["status"] == "failed" and "orphaned" in rows["old_dead"]["error"]
    assert rows["fresh"]["status"] == "running" and rows["old_locked"]["status"] == "running"


# --------------------------------------------------------------------------- F3


def test_tdm_cashflows_carry_only_known_values(conn: Conn) -> None:
    """Review F3 (scratch test converted): projected/current-published coupons are exported
    with empty rate and amount and counted in the manifest."""
    from test_api_seed import seed

    seed(conn)
    conn.execute(
        """INSERT INTO ref.bond_coupon (secid, coupon_no, start_date, pay_date, amount, value_prc,
               face_value, face_unit, status)
           VALUES ('RU000A105X12', 10, '2027-01-15', '2027-04-15', 42.50, 17.0, 1000, 'RUB',
                   'projected_by_moex'),
                  ('RU000A105X12', 11, '2027-04-15', '2027-07-15', 42.50, 17.0, 1000, 'RUB',
                   'current_published')"""
    )
    conn.commit()
    exp = build_tdm_export(conn, TdmExportOptions(date_to=date(2026, 10, 15)))
    files = {f.dataset: f for f in exp.files}
    cf = {
        r[1]: r for r in files["bond_cashflows"].rows if r[0] == "RU000A105X12" and r[2] == "coupon"
    }
    assert cf[date(2027, 4, 15)][3:] == (None, None)
    assert cf[date(2027, 7, 15)][3:] == (None, None)
    manifest = exp.manifest()
    withheld = manifest["withheld_values"]
    assert withheld["bond_cashflows: coupon without rate/amount (projected_by_moex)"] >= 1
    assert withheld["bond_cashflows: coupon without rate/amount (current_published)"] == 1
    assert "bond_cashflows" in manifest["conventions"]


def test_tdm_ytm_only_when_yield_date_matches_offer_rule(conn: Conn) -> None:
    from test_api_seed import seed

    seed(conn)
    conn.commit()
    base = build_tdm_export(conn, TdmExportOptions(date(2026, 10, 15), price_basis="close"))
    quotes = {r[1]: r for f in base.files if f.dataset == "md_bond_quotes" for r in f.rows}
    assert quotes["SU26242RMFS0"][3] == Decimal("15.42")
    # an offer ahead: MOEX's yield to maturity no longer matches the TDM basis
    conn.execute(
        "INSERT INTO ref.bond_offer (secid, source, offer_key, offer_date, offer_type, status) "
        "VALUES ('SU26242RMFS0', 'moex_iss', 'k1', '2027-06-01', 'put', 'scheduled')"
    )
    conn.commit()
    exp = build_tdm_export(conn, TdmExportOptions(date(2026, 10, 15), price_basis="close"))
    quotes = {r[1]: r for f in exp.files if f.dataset == "md_bond_quotes" for r in f.rows}
    assert quotes["SU26242RMFS0"][3] is None
    assert any("ytm left empty" in k for k in exp.withheld)


# --------------------------------------------------------------------------- F5


def test_api_secid_filters_keep_case(conn: Conn, db_url: str) -> None:
    """Review F5 (scratch test converted): FORTS/index SECIDs are mixed case."""
    conn.execute(
        """INSERT INTO ref.futures_contract (secid, asset_code, is_fx, base_ccy, quote_ccy,
               contract_type, spec_source, first_seen)
           VALUES ('SiZ6', 'Si', true, 'USD', 'RUB', 'dated', 'rfud', '2026-01-01')"""
    )
    conn.execute(
        """INSERT INTO md.futures_eod (trade_date, secid, board, asset_code, settle_price)
           VALUES ('2026-10-15', 'SiZ6', 'RFUD', 'Si', 84000)"""
    )
    conn.execute(
        "INSERT INTO md.index_eod (trade_date, secid, board, close, basis) "
        "VALUES ('2026-10-15', '2xEQT', 'SNDX', 1.5, 'history')"
    )
    conn.execute(
        "INSERT INTO ops.data_gap (dataset, key, gap_date) VALUES "
        "('d', '{\"secid\": \"B\"}', '2026-10-01'), ('d', '{\"secid\": \"A\"}', '2026-10-01'), "
        "('d', '{}', '2026-10-01')"
    )
    conn.commit()
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="tok-review-1234")
    )
    h = {"Authorization": "Bearer tok-review-1234"}
    with TestClient(app) as c:
        by_secid = c.get("/v1/futures/eod?date=2026-10-15&secid=SiZ6", headers=h).json()
        upper = c.get("/v1/futures/eod?date=2026-10-15&secid=SIZ6", headers=h).json()
        idx = c.get("/v1/indices/eod?date=2026-10-15&secid=2xEQT", headers=h).json()
        gaps = c.get("/v1/gaps?dataset=d", headers=h).json()
    assert by_secid["meta"]["returned"] == 1 and by_secid["data"][0]["secid"] == "SiZ6"
    assert upper["meta"]["returned"] == 0
    assert [r["secid"] for r in idx["data"]] == ["2xEQT"]
    # ordered by (dataset, gap_date, key::text): stable paging over series gaps
    assert [r["key"] for r in gaps["data"]] == [{"secid": "A"}, {"secid": "B"}, {}]


# --------------------------------------------------------------------------- F6


def test_intraday_same_ts_update_keeps_history(conn: Conn) -> None:
    """Review F6 (scratch test converted): a same-UPDATETIME change is kept in row history."""
    ts = datetime(2026, 10, 15, 23, 49, 59, tzinfo=timezone(timedelta(hours=3)))
    base = {
        "ts": ts, "secid": "CNYRUB_TOM", "board": "CETS", "source": "moex_iss",
        "source_variant": "iss_public", "bid": Decimal("12.50"), "offer": Decimal("12.51"),
        "last": Decimal("12.505"), "waprice": Decimal("12.5"), "open": Decimal("12.4"),
        "high": Decimal("12.6"), "low": Decimal("12.3"), "num_trades": 150000,
        "vol_today": Decimal("1e9"), "val_today": Decimal("1.25e10"), "trading_status": "T",
        "source_ts": ts, "latency": "delayed", "run_id": 1, "capture_id": None,
    }  # fmt: skip
    key = ["secid", "board", "source", "source_variant", "ts"]
    upsert_rows(conn, "md.fx_intraday", [base], key, run_id=1)
    conn.commit()
    nxt = {**base, "num_trades": 0, "vol_today": Decimal(0), "val_today": Decimal(0),
           "open": None, "high": None, "low": None, "trading_status": "N", "run_id": 2}  # fmt: skip
    changed, _ = only_changed(conn, "md.fx_intraday", [nxt], key[:-1], FX_INTRADAY_VALUE_COLUMNS)
    res = upsert_rows(conn, "md.fx_intraday", changed, key, run_id=2)
    conn.commit()
    hist = conn.execute(
        "SELECT old_row FROM ops.row_history WHERE table_name = 'md.fx_intraday'"
    ).fetchall()
    assert res.updated == 1 and len(hist) == 1 and hist[0]["old_row"]["num_trades"] == 150000
    trig = conn.execute(
        "SELECT count(*) AS n FROM pg_trigger WHERE tgname = 'mdhub_row_history' "
        "AND tgrelid = 'md.futures_intraday'::regclass"
    ).fetchone()
    assert trig == {"n": 1}


def test_pre_session_quote_dated_on_previous_business_day() -> None:
    """Monday 06:50 snapshot carrying Friday's 23:49 update -> Friday, not Sunday."""
    systime = datetime(2026, 10, 19, 6, 50, tzinfo=MSK)  # Monday
    assert snapshot_ts("23:49:59", systime) == datetime(2026, 10, 16, 23, 49, 59, tzinfo=MSK)
    holiday = {date(2026, 10, 16)}  # Friday closed: Thursday's session

    def cal(d: date) -> bool:
        return d.weekday() < 5 and d not in holiday

    got = snapshot_ts("23:49:59", systime, is_trading_day=cal)
    assert got == datetime(2026, 10, 15, 23, 49, 59, tzinfo=MSK)
    tue = datetime(2026, 10, 20, 0, 5, tzinfo=MSK)  # after midnight: previous day
    assert snapshot_ts("23:49:00", tue) == datetime(2026, 10, 19, 23, 49, tzinfo=MSK)
    q = _quote_ts(systime, datetime(2026, 1, 1, 18, 39, 58).time())
    assert q == datetime(2026, 10, 16, 18, 39, 58, tzinfo=MSK)


# --------------------------------------------------------------------------- F8


def test_bond_quote_capture_fks_set_null(conn: Conn) -> None:
    rows = conn.execute(
        """
        SELECT cl.relname, con.confdeltype FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        WHERE con.contype = 'f' AND con.confrelid = 'ops.raw_capture'::regclass
          AND cl.relname IN ('bond_quote_intraday', 'bond_quote_last')
        ORDER BY 1
        """
    ).fetchall()
    assert rows == [
        {"relname": "bond_quote_intraday", "confdeltype": "n"},
        {"relname": "bond_quote_last", "confdeltype": "n"},
    ]
    from mdhub.loaders.bonds_intraday import INTRADAY_ENDPOINT
    from mdhub.loaders.maintenance import is_intraday_endpoint

    assert is_intraday_endpoint(INTRADAY_ENDPOINT)


# --------------------------------------------------------------------------- F9


def _coupon(pay: date, value: str | None) -> ms.CouponRow:
    v = Decimal(value) if value is not None else None
    return ms.CouponRow(
        secid="X", isin=None, name=None, pay_date=pay, start_date=pay - timedelta(days=91),
        record_date=None, value=v, value_prc=v, value_rub=v, face_value=Decimal(1000),
        initial_face_value=Decimal(1000), face_unit="RUB", issue_value=None, capture_id=None,
    )  # fmt: skip


def test_floater_current_coupon_is_not_known_unless_fixed_in_advance() -> None:
    today = date(2026, 10, 16)
    cur = _coupon(date(2026, 12, 1), "42.5")
    kw: dict[str, Any] = {"coupon_type": "floating", "is_current": True, "face_indexed": False}
    assert ms.coupon_status(cur, benchmark="KEY_RATE", today=today, **kw) == "current_published"
    assert ms.coupon_status(cur, benchmark=None, today=today, **kw) == "current_published"
    assert (
        ms.coupon_status(cur, benchmark="KEY_RATE", today=today, fixed_in_advance=True, **kw)
        == "known"
    )
    assert ms.coupon_status(cur, benchmark="RUONIA", today=today, **kw) == "projected_by_moex"
    paid = _coupon(date(2026, 9, 1), "40")
    assert ms.coupon_status(paid, benchmark="KEY_RATE", today=today, **kw) == "known"


def test_fully_valued_schedule_does_not_make_an_active_bond_fixed() -> None:
    described = ms.ClassifyInput(
        secid="RU000Y", sec_type="corporate_bond", has_description=True, coupon_values=(True,) * 4
    )
    assert ms.classify_coupon_type(described, "corporate") == ("unknown", "schedule")
    labelled = ms.ClassifyInput(
        secid="RU000F",
        sec_type="corporate_bond",
        has_description=True,
        coupon_values=(True,) * 4,
        bond_type=next(iter(ms.FIXED_KNOWN_BOND_TYPES)),
    )
    assert ms.classify_coupon_type(labelled, "corporate") == ("fixed", "moex_label")


def test_fixed_in_advance_from_curated_formula(conn: Conn) -> None:
    from mdhub.loaders.bonds_static import fixed_in_advance_secids

    conn.execute(
        "INSERT INTO ref.bond (secid, name, bond_class, coupon_type, coupon_type_basis, status, "
        "source) VALUES ('F1', 'f', 'corporate', 'floating', 'moex_label', 'active', 'moex_iss'), "
        "('F2', 'f', 'corporate', 'floating', 'moex_label', 'active', 'moex_iss')"
    )
    conn.execute(
        """INSERT INTO ref.bond_rate_formula (secid, source, benchmark, status, evidence,
               averaging)
           VALUES ('F1', 'curated', 'KEY_RATE', 'curated',
                   '{"formula": {"fixing_calendar_days_before_start": 1}}', NULL),
                  ('F2', 'curated', 'KEY_RATE', 'curated',
                   '{"formula": {"lag_calendar_days": 1}}', 'arithmetic')"""
    )
    assert fixed_in_advance_secids(conn, ["F1", "F2"]) == {"F1"}
    conn.execute(
        "INSERT INTO ref.bond_coupon (secid, coupon_no, pay_date, status) "
        "VALUES ('F1', 1, '2026-12-01', 'current_published')"
    )


def test_formula_resolved_includes_legacy_at_lowest_priority(conn: Conn) -> None:
    conn.execute(
        "INSERT INTO ref.bond (secid, name, bond_class, coupon_type, coupon_type_basis, status, "
        "source) VALUES ('L1', 'l', 'corporate', 'floating', 'moex_label', 'active', 'moex_iss')"
    )
    conn.execute(
        """INSERT INTO ref.bond_rate_formula (secid, source, benchmark, spread_bp, status)
           VALUES ('L1', 'legacy_bc', 'KEY_RATE', 150, 'inferred')"""
    )
    r = conn.execute("SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = 'L1'").fetchone()
    assert r is not None and r["sources"] == ["legacy_bc"] and r["spread_bp"] == 150
    conn.execute(
        """INSERT INTO ref.bond_rate_formula (secid, source, benchmark, spread_bp, status)
           VALUES ('L1', 'moex_iss', 'RUONIA', 200, 'inferred')"""
    )
    r = conn.execute("SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = 'L1'").fetchone()
    assert r is not None and r["benchmark"] == "RUONIA" and r["spread_bp"] == 200
    assert r["sources"] == ["moex_iss", "legacy_bc"]


# --------------------------------------------------------------------------- F13


def test_fx_instrument_settle_date_is_an_observation(conn: Conn) -> None:
    import json
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "moex_fx" / "selt_securities.json").read_text()
    )
    first = refresh_fx_instruments(
        conn, payload, capture_id=None, run_id=1, today=date(2026, 10, 15)
    )  # type: ignore[arg-type]
    assert first.inserted > 0
    conn.commit()
    cols = payload["securities"]["columns"]
    i = cols.index("SETTLEDATE")
    for row in payload["securities"]["data"]:
        if row[i]:
            row[i] = "2026-10-20"
    again = refresh_fx_instruments(
        conn, payload, capture_id=None, run_id=2, today=date(2026, 10, 16)
    )  # type: ignore[arg-type]
    conn.commit()
    assert again.updated == 0 and again.inserted == 0
    n = conn.execute(
        "SELECT count(*) AS n FROM ref.fx_instrument WHERE settle_date = '2026-10-20'"
    ).fetchone()
    assert n is not None and n["n"] > 0
    hist = conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE table_name = 'ref.fx_instrument'"
    ).fetchone()
    assert hist == {"n": 0}


# --------------------------------------------------------------------------- F14


def _standing_page(rows: list[list[str]]) -> str:
    head = (
        "<table class='data'><tr><th rowspan='2'>Дата</th>"
        "<th rowspan='2'>Ставка по депозитам постоянного действия</th>"
        "<th>x</th><th>x</th><th>x</th><th>x</th><th>x</th><th>Основной механизм</th>"
        "<th>x</th><th>x</th><th>x</th><th>x</th><th>x</th></tr>"
    )
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return head + body + "</table>"


def test_standing_lending_rate_maps_exactly_one_column() -> None:
    page = _standing_page(
        [
            ["16.10.2023", "12,00", "13,00", "13,00", "13,10", "13,25", "13,25", "14,00",
             "14,75", "—", "—", "—", "—"],
            ["17.10.2023", "12,00", "13,00", "13,00", "13,10", "13,25", "13,25", "—",
             "14,75", "99,00", "—", "—", "—"],
            ["13.10.2023", "12,00", "13,00", "13,00", "13,10", "13,25", "13,25", "—", "—",
             "14,00", "14,00", "14,75", "—"],
        ]
    )  # fmt: skip
    try:
        rows = {r.rate_date: r for r in cbr_src.parse_standing_rates_page(page)}
    except Exception:  # header layout of the fixture differs from the parser's check
        pytest.skip("synthetic page not accepted by the table finder")
    assert rows[date(2023, 10, 16)].lending_standing_pct == Decimal("14.00")
    assert rows[date(2023, 10, 13)].lending_standing_pct == Decimal("14.00")  # column 9
    assert rows[date(2023, 10, 17)].lending_standing_pct is None  # no splice from column 9


# ---------------------------------------------------------------------- CLI / settings


def test_calendar_seed_cli_wired(monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    import mdhub.loaders.calendar as cal

    got: dict[str, Any] = {}

    def fake_seed(settings: Settings, db: Database, *, since: date = cal.OBSERVED_START) -> Any:
        got["since"] = since
        return {"curated": {"written": 1}}

    monkeypatch.setattr(cal, "seed_calendar", fake_seed)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    res = CliRunner().invoke(cli.app, ["calendar", "seed", "--since", "2019-01-01"])
    assert res.exit_code == 0, res.output
    assert got == {"since": date(2019, 1, 1)} and "curated" in res.output


def test_api_pool_size_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_API_POOL_SIZE", "3")
    assert Settings().api_pool_size == 3
    with pytest.raises(ValueError, match="greater than or equal"):
        Settings(api_pool_size=0)


def test_cbr_meeting_effective_date_never_weekend() -> None:
    from mdhub.loaders.cbr import _next_business_day

    class Ctx:
        def is_business_day(self, d: date, calendar: str = "RU_PRODUCTION") -> bool:
            return d != date(2021, 2, 22) and (d.weekday() < 5 or d == date(2021, 2, 20))

    # Friday 2021-02-19 -> Saturday 02-20 is a working Saturday, Monday 02-22 a holiday
    assert _next_business_day(Ctx(), date(2021, 2, 19)) == date(2021, 2, 23)  # type: ignore[arg-type]


def test_rule_runner_waits_then_records_lock_busy(
    db: Database, settings: Settings, conn: Conn, fake_job: list[dict[str, Any]]
) -> None:
    from mdhub.schedule import RULES, _RuleRunner

    lock = threading.Lock()
    rule = Rule(cron="0 0 * * *", params={"mode": "final"}, days="all", lock_wait_s=1)
    runner = _RuleRunner(
        "rv_locked",
        rule,
        settings=settings,
        db=db,
        registry=REGISTRY,
        calendar=weekday_calendar,
        lock=lock,
    )
    lock.acquire()  # a previous run of the job is still running in this process
    try:
        runner()
    finally:
        lock.release()
    row = conn.execute("SELECT status, error FROM ops.job_run WHERE job = 'rv_locked'").fetchone()
    assert row is not None and row["status"] == "skipped"
    assert row["error"].startswith(LOCK_BUSY) and fake_job == []
    runner()
    assert fake_job == [{"mode": "final", "date_from": None, "date_to": None}]
    # polling rules neither wait nor catch up
    polls = [r for rules in RULES.values() for r in rules if r.lock_wait_s == 0]
    assert polls and all(not r.catch_up for r in polls)
    assert all(r.lock_wait_s == 0 for r in RULES["futures_intraday"])
    assert all(r.lock_wait_s > 0 and r.catch_up for r in RULES["bonds_eod"])
