"""EOD fixes of the desk-monitor review (2026-10-21): eod_preliminary bonds from the intraday
close while the ISS history is not published (§6.4) and the curve EOD rule that prefers the
latest quote set with bid/ask inside the trading window (§2.5 item 3), the same in the EOD
market cuts and in ``/v1/history/curves`` ``rule=eod``."""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

import mdhub.loaders.snapshots  # noqa: F401  (registers the snapshot jobs)
from mdhub.api.app import create_app
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, run_job
from mdhub.loaders.snapshots import market_lock
from mdhub.schedule import (
    INTRADAY_CUT_GAP_DATASET,
    Rule,
    _RuleRunner,
    intraday_lock_wait_s,
    snapshot_rules,
)
from mdhub.snapshot.market import MarketParams
from mdhub.snapshot.runner import publish_market_snapshot
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
NEXT = date(2026, 10, 16)  # no ISS history rows in the seed: the evening of D before 07:15
TOKENS = "tok-eod-1"
AUTH = {"Authorization": "Bearer tok-eod-1"}


@pytest.fixture
def seeded(conn: Conn) -> Conn:
    seed(conn)
    return conn


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def _dataset(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(d for d in doc["datasets"] if d["name"] == name)


def _msk(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK)


# ------------------------------------------------------- §6.4 eod_preliminary bonds


def _intraday_close(conn: Conn, d: date) -> list[str]:
    """Two states of the day per bond of the seed universe: 15:00 and the close (18:50,
    LCLOSEPRICE set); returns the SECIDs."""
    secids = [
        r["secid"]
        for r in conn.execute(
            "SELECT DISTINCT secid FROM md.bond_eod WHERE trade_date = %s ORDER BY 1", (DAY,)
        ).fetchall()
    ]
    for i, secid in enumerate(secids):
        for hh, mm, legal in ((15, 0, None), (18, 50, Decimal("99.10") + i)):
            conn.execute(
                """
                INSERT INTO md.bond_quote_intraday (secid, board, ts, source_ts, latency, last,
                    open, low, high, waprice, market_price, market_price_today, close_price,
                    legal_close, yield_pct, yield_at_waprice_pct, duration_days, num_trades,
                    vol_today, val_today, trading_session, ingested_at)
                VALUES (%s, 'TQOB', %s, %s, 'delayed', 99.2, 98.5, 98.4, 99.6, 99.05, 97.0,
                    99.07, 99.15, %s, 15.1, 15.2, 700, 42, 1000, 990000, '1', %s)
                """,
                (secid, _msk(d, hh, mm), _msk(d, hh, mm + 1), legal, _msk(d, hh, mm + 2)),
            )
    conn.commit()
    return secids


def test_eod_preliminary_without_history_publishes_the_intraday_close(
    seeded: Conn, db: Database
) -> None:
    """Production 19 and 20.10: the 19:45 cut had ``bond_quotes: 0`` because ISS publishes the
    history of D overnight. The cut now carries the bonds from the latest intraday state, with
    ``basis = intraday_close`` in the coverage and a stale reason; MARKETPRICE (previous day)
    is never used as the day's price."""
    secids = _intraday_close(seeded, NEXT)
    out = publish_market_snapshot(
        db,
        cut_type="eod_preliminary",
        business_date=NEXT,
        cut_ts=_msk(NEXT, 19, 45),
        known_at=utc_now(),
        params=MarketParams(prelim_bond_min_rows=1000),
    )
    doc = _bundle(seeded, out.market.id)
    quotes = doc["data"]["bond_quotes"]
    assert sorted({q["secid"] for q in quotes}) == secids
    first = next(q for q in quotes if q["secid"] == secids[0])
    assert first["trade_date"] == NEXT.isoformat()
    assert Decimal(first["legal_close_pct"]) == Decimal("99.10")  # the 18:50 state, LCLOSEPRICE
    assert Decimal(first["close_pct"]) == Decimal("99.2")  # LAST
    assert Decimal(first["market_price3_pct"]) == Decimal("99.07")  # MARKETPRICETODAY
    assert first["market_price2_pct"] is None and first["accrued_per_bond"] is None
    assert Decimal(first["source_analytics"]["yield_close_pct"]) == Decimal("15.1")
    ds = _dataset(doc, "bond_quotes")
    assert ds["coverage"]["basis"] == "intraday_close"
    assert ds["coverage"]["history_rows"] == 0
    assert ds["stale"] is True
    assert any("basis=intraday_close" in r for r in ds["stale_reasons"])


def test_eod_preliminary_with_history_and_eod_final_keep_the_history_basis(
    seeded: Conn, db: Database
) -> None:
    """The fallback applies to eod_preliminary only and only while the intraday state is the
    fuller source; the seed's history of D wins (no intraday rows), eod_final never falls back."""
    pre = publish_market_snapshot(
        db,
        cut_type="eod_preliminary",
        business_date=DAY,
        cut_ts=_msk(DAY, 19, 45),
        known_at=utc_now(),
        params=MarketParams(prelim_bond_min_rows=1000),
    )
    ds = _dataset(_bundle(seeded, pre.market.id), "bond_quotes")
    assert ds["coverage"]["basis"] == "moex_history" and ds["rows"] > 0
    _intraday_close(seeded, NEXT)
    final = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=NEXT,
        cut_ts=_msk(date(2026, 10, 19), 8, 0),
        known_at=utc_now(),
        params=MarketParams(prelim_bond_min_rows=1000),
    )
    fds = _dataset(_bundle(seeded, final.market.id), "bond_quotes")
    assert fds["rows"] == 0 and fds["coverage"]["basis"] == "moex_history"


# ------------------------------------------------------------ §2.5.3 curve EOD rule


def _curve(
    conn: Conn,
    source: str,
    clock: datetime,
    basis: str,
    mid: str,
    *,
    fetched: datetime | None = None,
    snapshot_date: date = DAY,
) -> int:
    fetched = fetched or clock
    row = conn.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,"
        " source_ts, content_sha256, parser_version, ingested_at)"
        " VALUES (%s, 'ruonia_ois', %s, %s, %s, %s, md5(%s) || md5(%s), '1', %s) RETURNING id",
        (source, basis, fetched, snapshot_date, clock, str(clock), source, fetched),
    ).fetchone()
    assert row is not None
    two = basis == "bid_ask_mid"
    conn.execute(
        "INSERT INTO md.curve_quote (snapshot_id, tenor, position, tenor_months, bid_pct,"
        " ask_pct, mid_pct) VALUES (%s, '1Y', 0, 12, %s, %s, %s)",
        (
            row["id"],
            Decimal(mid) - Decimal("0.1") if two else None,
            Decimal(mid) + Decimal("0.1") if two else None,
            mid,
        ),
    )
    return int(row["id"])


def _otc_day(conn: Conn) -> dict[str, int]:
    """The OTC Monitor pattern of 20.10 (bid/ask 10:38 and 14:48, mid-only 18:50) plus a late
    two-sided set after the cutoff; andromeda: mid-only only (end-of-day export)."""
    ids = {
        "1038": _curve(conn, "otc_monitor", _msk(DAY, 10, 38), "bid_ask_mid", "15.00"),
        "1448": _curve(conn, "otc_monitor", _msk(DAY, 14, 48), "bid_ask_mid", "15.10"),
        "1850": _curve(conn, "otc_monitor", _msk(DAY, 18, 50), "mid_only", "15.20"),
        "1930": _curve(conn, "otc_monitor", _msk(DAY, 19, 30), "bid_ask_mid", "15.30"),
        "andr": _curve(conn, "andromeda", _msk(DAY, 23, 59), "mid_only", "15.40"),
    }
    conn.commit()
    return ids


def _chosen(doc: dict[str, Any], source: str) -> dict[str, Any]:
    return next(
        s
        for s in doc["data"]["curve_quote_sets"]
        if s["source"] == source and s["dataset"] == "ruonia_ois"
    )


def test_eod_cut_prefers_the_latest_bid_ask_set_within_the_window(
    seeded: Conn, db: Database
) -> None:
    """Production 20.10: eod_final took the 18:50 mid-only OTC set (flag ``off-hours mark``).
    The EOD rule now takes the latest two-sided set at or before the cutoff (14:48); a source
    with mid-only sets only keeps its latest (labelled ``mid_only``); intraday cuts still take
    the latest set."""
    ids = _otc_day(seeded)
    known = _msk(date(2026, 10, 16), 8, 0)
    final = publish_market_snapshot(
        db, cut_type="eod_final", business_date=DAY, cut_ts=known, known_at=known
    )
    doc = _bundle(seeded, final.market.id)
    otc = _chosen(doc, "otc_monitor")
    assert otc["quote_set_id"] == ids["1448"] and otc["quote_basis"] == "bid_ask_mid"
    andr = _chosen(doc, "andromeda")
    assert andr["quote_set_id"] == ids["andr"] and "mid_only" in andr["labels"]
    cov = _dataset(doc, "curve_quote_sets")["coverage"]
    assert cov["eod_cutoff_msk"] == "19:00"
    # a later cutoff admits the 19:30 two-sided set
    late = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=DAY,
        cut_ts=known,
        known_at=known,
        params=MarketParams(curve_eod_cutoff="20:00"),
    )
    assert _chosen(_bundle(seeded, late.market.id), "otc_monitor")["quote_set_id"] == ids["1930"]
    intraday = publish_market_snapshot(
        db,
        cut_type="intraday",
        business_date=DAY,
        cut_ts=_msk(DAY, 19, 0),
        known_at=_msk(DAY, 19, 0),
    )
    assert (
        _chosen(_bundle(seeded, intraday.market.id), "otc_monitor")["quote_set_id"] == ids["1850"]
    )


def test_eod_rule_ignores_a_stale_source_clock_from_an_earlier_day(
    seeded: Conn, db: Database
) -> None:
    """A set stored again on a new day with the source clock of an earlier day (OTC
    RUSFARCNY, 21.10 with clock 20.10 18:50) is not "inside the window" of the day."""
    stale = _curve(
        seeded,
        "otc_monitor",
        _msk(date(2026, 10, 14), 12, 0),
        "bid_ask_mid",
        "14.90",
        fetched=_msk(DAY, 9, 0),
    )
    fresh = _curve(seeded, "otc_monitor", _msk(DAY, 18, 0), "mid_only", "15.00")
    seeded.commit()
    known = _msk(date(2026, 10, 16), 8, 0)
    out = publish_market_snapshot(
        db, cut_type="eod_final", business_date=DAY, cut_ts=known, known_at=known
    )
    chosen = _chosen(_bundle(seeded, out.market.id), "otc_monitor")
    assert chosen["quote_set_id"] == fresh != stale


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens=TOKENS)
    )
    with TestClient(app) as c:
        yield c


def test_history_rule_eod_is_the_snapshot_rule(
    seeded: Conn, db: Database, client: TestClient
) -> None:
    """``/v1/history/curves`` ``rule=eod`` selects the same quote set per (day, source) as the
    EOD cut (one function, :func:`mdhub.snapshot.market.curve_eod_order`)."""
    ids = _otc_day(seeded)
    r = client.get(
        "/v1/history/curves/ruonia_ois",
        params={"from": "2026-10-15", "to": "2026-10-15", "fields": "quote_set_id"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    cols = body["columns"]
    chosen = {row[cols.index("source")]: row[cols.index("quote_set_id")] for row in body["rows"]}
    assert chosen["otc_monitor"] == ids["1448"]
    assert chosen["andromeda"] == ids["andr"]
    assert body["meta"]["eod_rule"]["cutoff_msk"] == "19:00"
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=DAY,
        cut_ts=_msk(date(2026, 10, 16), 8, 0),
        known_at=utc_now(),  # as the API reads now (the seed's ratescalcs sets included)
    )
    doc = _bundle(seeded, out.market.id)
    assert set(chosen) == {"otc_monitor", "andromeda", "ratescalcs"}
    for source, qid in chosen.items():
        assert _chosen(doc, source)["quote_set_id"] == qid


# ------------------------------------------------------------ §6.5 snapshot locks


def _hold(db_url: str, lock: str) -> psycopg.Connection[Any]:
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"mdhub.job:{lock}",))
    return holder


def test_manual_eod_rebuild_does_not_block_scheduled_intraday_cuts(
    seeded: Conn,
    db_url: str,
    db: Database,
    settings: Settings,
    today_is: Callable[[date], None],
) -> None:
    """Production 20.10 16:30-19:00 MSK: ~170 ``mdhub run snapshot_market -p mode=eod_final -p
    day=...`` runs held ``mdhub.job:snapshot_market`` and every intraday fire was recorded
    ``skipped: lock_busy``. A rebuild now holds its own lock class; the scheduled intraday cut
    and the scheduled EOD cut still run."""
    today_is(date(2026, 10, 23))  # a Friday: scheduled cuts fire on business days only
    rebuild = market_lock({"mode": "eod_final", "day": date(2026, 1, 15)})
    assert rebuild == "snapshot_market:rebuild"
    holder = _hold(db_url, rebuild)
    try:
        intraday = run_job(
            "snapshot_market", {"mode": "intraday"}, settings=settings, db=db, trigger="schedule"
        )
        assert intraday.status == "succeeded", intraday
        pre = run_job(
            "snapshot_market",
            {"mode": "eod_preliminary", "days_back": [0]},
            settings=settings,
            db=db,
            trigger="schedule",
        )
        assert pre.status in ("succeeded", "partial")
        # a second rebuild waits for the first: runs of one class stay exclusive
        again = run_job(
            "snapshot_market", {"mode": "eod_final", "day": "2026-01-15"}, settings=settings, db=db
        )
        assert again.status == "skipped"
    finally:
        holder.close()
    assert market_lock({"mode": "intraday"}) == "snapshot_market:intraday"
    assert market_lock({"mode": "eod_final", "days_back": [1]}) == "snapshot_market:eod"
    assert market_lock({"mode": "intraday", "known_at": utc_now()}) == "snapshot_market:rebuild"


@pytest.mark.parametrize("mode", ["intraday", "eod_preliminary"])
def test_snapshot_market_on_a_non_business_day_is_skipped(
    seeded: Conn, db: Database, settings: Settings, today_is: Callable[[date], None], mode: str
) -> None:
    """Issue #88: a ``days_back=[0]`` cut (the intraday / ``eod_preliminary`` default) run on a
    weekend - e.g. a manual ``mdhub run snapshot_market`` - resolves no business date. It ends
    ``skipped`` with the reason and publishes nothing, instead of failing with ``IndexError``."""
    today_is(date(2026, 10, 25))  # a Sunday
    before = seeded.execute("SELECT count(*) AS n FROM ops.published_snapshot").fetchone()
    out = run_job("snapshot_market", {"mode": mode, "days_back": [0]}, settings=settings, db=db)
    assert out.status == "skipped", out
    assert out.result == {}
    run = seeded.execute(
        "SELECT status, error FROM ops.job_run WHERE id = %s", (out.run_id,)
    ).fetchone()
    assert run is not None
    assert run["status"] == "skipped"
    assert "2026-10-25 is not a MOEX_STOCK business day" in run["error"]
    after = seeded.execute("SELECT count(*) AS n FROM ops.published_snapshot").fetchone()
    assert before is not None and after is not None
    assert after["n"] == before["n"]


def test_intraday_rules_wait_inside_their_slot_and_open_a_gap_when_missed() -> None:
    rules = snapshot_rules(Settings.model_construct())["snapshot_market"]
    intraday = [r for r in rules if r.params.get("mode") == "intraday"]
    assert intraday and all(r.lock_wait_s == 600 for r in intraday)  # half of a 30-min slot
    assert all(r.missed_gap == INTRADAY_CUT_GAP_DATASET and not r.catch_up for r in intraday)
    assert intraday_lock_wait_s(5) == 150


def test_missed_intraday_cut_is_a_gap_closed_by_its_replay(
    seeded: Conn, db_url: str, db: Database, settings: Settings
) -> None:
    """An intraday fire whose lock stays busy for its whole wait is recorded skipped and opens
    an ``ops.data_gap`` (``/v1/gaps``) keyed by the cut minute; replaying that cut closes it."""
    rule = Rule(
        cron="* * * * *",
        params={"mode": "intraday"},
        days="all",
        lock_wait_s=1,
        catch_up=False,
        missed_gap=INTRADAY_CUT_GAP_DATASET,
    )
    runner = _RuleRunner(
        "snapshot_market",
        rule,
        settings=settings,
        db=db,
        registry=REGISTRY,
        calendar=weekday_calendar,
        lock=threading.Lock(),
    )
    holder = _hold(db_url, "snapshot_market:intraday")
    try:
        runner()
    finally:
        holder.close()
    run = seeded.execute(
        "SELECT status, error FROM ops.job_run WHERE job = 'snapshot_market'"
    ).fetchone()
    assert run is not None and run["status"] == "skipped" and run["error"].startswith("lock_busy")
    gap = seeded.execute(
        "SELECT key, gap_date, note FROM ops.v_open_gaps WHERE dataset = %s",
        (INTRADAY_CUT_GAP_DATASET,),
    ).fetchone()
    assert gap is not None and "lock_busy" in gap["note"]
    cut = gap["key"]["cut"]
    replay = run_job(
        "snapshot_market",
        {"mode": "intraday", "day": gap["gap_date"].isoformat(), "cut": cut},
        settings=settings,
        db=db,
    )
    assert replay.status == "succeeded", replay
    seeded.commit()
    status = seeded.execute(
        "SELECT status FROM ops.data_gap WHERE dataset = %s", (INTRADAY_CUT_GAP_DATASET,)
    ).fetchone()
    assert status == {"status": "backfilled"}
