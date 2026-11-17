"""Rounded redemption schedules and manual overrides (market-data-hub#165, contract 1.10).

MOEX ISS rounds the principal schedule (``value`` to 0.01), so the future redemptions of a bond
with a low face do not add up to its face and MOEX's yields derived from them are wrong. The
bond used here is synthetic (``TESTLOW00009``) and mimics that shape: face 0.035, seven future
amounts of 0.01 that are really 0.005. Nothing below is read from a real feed.
"""

from __future__ import annotations

import copy
import gzip
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.export.tdm import TdmExportOptions, build_tdm_export
from mdhub.jobs import run_job
from mdhub.loaders import _amortization_override as ovr
from mdhub.loaders import bond_terms
from mdhub.snapshot.build import Cut
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex
from mdhub.snapshot.market import build_market
from mdhub.snapshot.redemptions import (
    LABEL_DIFFER,
    LABEL_NOT_IN_SCHEDULE,
    LABEL_OVERRIDE_APPLIED,
    LABEL_OVERRIDE_STALE,
    LABEL_ROUNDED,
    face_schedule_labels,
    plan_schedule,
    redemptions_rounded_by_source,
)
from mdhub.snapshot.runner import publish_static_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION, validate_bundle
from mdhub.snapshot.static import build_static
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
D = Decimal


@pytest.fixture(autouse=True)
def _pinned_today(today_is: Callable[[date], None]) -> None:
    """The job judges an override on today's date (``JobContext.today``): the packaged-style
    test data (future dates from 2027-03-31) must not go stale when the real date passes."""
    today_is(DAY)


PAST = [  # paid, rounded: they add up to 1.01 although the initial face is 1
    (date(2020, 3, 31), "0.55"), (date(2022, 3, 31), "0.25"), (date(2024, 3, 31), "0.18"),
    (date(2025, 9, 30), "0.01"), (date(2026, 3, 31), "0.01"), (date(2026, 9, 30), "0.01"),
]  # fmt: skip
FUTURE_DATES = [
    date(2027, 3, 31), date(2027, 9, 30), date(2028, 3, 31), date(2028, 9, 30),
    date(2029, 3, 31), date(2029, 9, 30), date(2030, 3, 31),
]  # fmt: skip


def _bond(face: str = "0.035", initial: str = "1", **kw: Any) -> dict[str, Any]:
    return {
        "secid": "TESTLOW00009", "isin": "TESTLOW00009", "face_value": D(face),
        "initial_face_value": D(initial), "issue_date": date(2000, 3, 31),
        "maturity_date": date(2030, 3, 31), "face_unit": "USD", "is_face_indexed": False,
        **kw,
    }  # fmt: skip


def _moex(
    future: str = "0.01", past: list[tuple[date, str]] = PAST, dates: list[date] = FUTURE_DATES
) -> list[dict[str, Any]]:
    """``ref.bond_amortization`` rows as MOEX publishes them (rounded)."""
    out = []
    for d, v in [*past, *((d, future) for d in dates)]:
        out.append(
            {
                "secid": "TESTLOW00009", "amort_date": d, "value": D(v), "value_prc": None,
                "value_rub": None, "face_value": D("0.035"), "initial_face_value": D("1"),
                "face_unit": "USD",
                "data_source": "maturity" if d == date(2030, 3, 31) else "amortization",
            }
        )  # fmt: skip
    return out


def _override(
    value: str = "0.005",
    dates: list[date] = FUTURE_DATES,
    unit: str = "USD",
    values: list[str] | None = None,
) -> list[dict[str, Any]]:
    """``ref.bond_amortization_override`` rows (``values``: one amount per date)."""
    amounts = values or [value] * len(dates)
    return [
        {
            "secid": "TESTLOW00009", "amort_date": d, "value": D(v), "face_unit": unit,
            "source_note": "TEST note", "confirmed_by": "owner",
            "confirmed_on": date(2026, 11, 17), "file_version": "test.1",
            "run_id": 7, "ingested_at": None, "updated_at": None,
        }
        for d, v in zip(dates, amounts, strict=True)
    ]  # fmt: skip


def _values(rows: list[dict[str, Any]]) -> list[Decimal | None]:
    return [r["value"] for r in rows if r["amort_date"] >= FUTURE_DATES[0]]


# ------------------------------------------------------------------ label rule (pure)


def test_rounded_label_on_the_low_face_bond() -> None:
    pairs = [{"date": r["amort_date"], "value_per_bond": r["value"]} for r in _moex()]
    assert face_schedule_labels(_bond(), pairs, DAY) == (LABEL_NOT_IN_SCHEDULE, LABEL_DIFFER)
    assert redemptions_rounded_by_source(_bond(), pairs, DAY)
    plan = plan_schedule(_bond(), _moex(), [], DAY)
    assert plan.override == "none" and plan.unreliable_reasons == (LABEL_DIFFER, LABEL_ROUNDED)
    assert plan.labels(_bond(), DAY) == sorted([LABEL_NOT_IN_SCHEDULE, LABEL_DIFFER, LABEL_ROUNDED])


def test_rounded_label_is_bounded_by_half_a_step_per_amount() -> None:
    def pairs(values: list[str]) -> list[dict[str, Any]]:
        return [{"date": FUTURE_DATES[i], "value_per_bond": D(v)} for i, v in enumerate(values)]

    # 7 x 0.01 = 0.07 against 0.035: the gap 0.035 is exactly 7 half steps - rounding can do it
    assert redemptions_rounded_by_source(_bond(), pairs(["0.01"] * 7), DAY)
    # 7 x 0.01 against face 0.03: gap 0.04 > 7 x 0.005, rounding alone cannot explain it
    assert face_schedule_labels(_bond("0.03"), pairs(["0.01"] * 7), DAY)[1] == LABEL_DIFFER
    assert not redemptions_rounded_by_source(_bond("0.03"), pairs(["0.01"] * 7), DAY)
    # one amount: the gap of one half step is within FACE_TOLERANCE, no label at all
    assert face_schedule_labels(_bond("0.005"), pairs(["0.01"]), DAY)[1] is None
    assert not redemptions_rounded_by_source(_bond("0.005"), pairs(["0.01"]), DAY)
    # an amount off the 0.01 grid is not a rounded publication
    assert not redemptions_rounded_by_source(_bond("0.035"), pairs(["0.0105"] + ["0.01"] * 6), DAY)
    # consistent schedule, indexed face, unknown amount: never
    assert not redemptions_rounded_by_source(_bond("0.035"), pairs(["0.005"] * 7), DAY)
    assert not redemptions_rounded_by_source(_bond(is_face_indexed=True), pairs(["0.01"] * 7), DAY)
    assert not redemptions_rounded_by_source(
        _bond(), [*pairs(["0.01"] * 6), {"date": FUTURE_DATES[6], "value_per_bond": None}], DAY
    )
    assert not redemptions_rounded_by_source(_bond(), [], DAY)


def test_rounded_label_not_for_an_inconsistent_schedule_that_is_not_rounding() -> None:
    # securitization-like: face follows the paid redemptions, the projected rest does not add up
    tb4 = [
        {"date": date(2026, 9, 1), "value_per_bond": D("116.12")},
        {"date": date(2027, 9, 1), "value_per_bond": D("866.94")},
    ]
    b = _bond("883.88", "1000")
    assert face_schedule_labels(b, tb4, DAY)[1] == LABEL_DIFFER
    assert not redemptions_rounded_by_source(b, tb4, DAY)
    # ИЛСБО-like: face 400, redemptions 300 + 400 still to come
    ilsbo = [
        {"date": date(2027, 1, 1), "value_per_bond": D("300")},
        {"date": date(2028, 1, 1), "value_per_bond": D("400")},
    ]
    assert not redemptions_rounded_by_source(_bond("400", "1000"), ilsbo, DAY)


def test_ordinary_amortizer_is_untouched() -> None:
    b = _bond("750", "1000", face_unit="RUB", maturity_date=date(2028, 1, 1))
    rows = [
        {**_moex()[0], "amort_date": date(2025, 1, 1), "value": D("250"), "data_source": "x"},
        {**_moex()[0], "amort_date": date(2027, 1, 1), "value": D("250"), "data_source": "x"},
        {**_moex()[0], "amort_date": date(2028, 1, 1), "value": D("500"),
         "data_source": "maturity"},
    ]  # fmt: skip
    plan = plan_schedule(b, rows, [], DAY)
    assert plan.override == "none" and plan.unreliable_reasons == ()
    assert plan.labels(b, DAY) == []
    assert [r["source"] for r in plan.rows] == ["moex_iss"] * 3


# ------------------------------------------------------------------- override (pure)


def test_override_replaces_future_rows_and_clears_the_face_label() -> None:
    plan = plan_schedule(_bond(), _moex(), _override(), DAY)
    assert plan.override == "applied" and plan.stale_reason is None
    # past rows stay MOEX's, the future ones are the confirmed amounts
    assert [r["source"] for r in plan.rows] == ["moex_iss"] * 6 + ["manual_override"] * 7
    assert _values(plan.rows) == [D("0.005")] * 7
    last = plan.rows[-1]
    assert (last["amort_date"], last["data_source"], last["face_unit"]) == (
        date(2030, 3, 31), "maturity", "USD",
    )  # fmt: skip
    assert last["value_prc"] is None and last["value_rub"] is None
    assert last["override"] == {
        "note": "TEST note", "confirmed_by": "owner", "confirmed_on": date(2026, 11, 17),
        "file_version": "test.1",
    }  # fmt: skip
    # the past rounding is still visible, the future sum now equals the face
    labels = plan.labels(_bond(), DAY)
    assert LABEL_DIFFER not in labels and LABEL_ROUNDED not in labels
    assert labels == sorted([LABEL_NOT_IN_SCHEDULE, LABEL_OVERRIDE_APPLIED])
    # MOEX's yields stay unreliable: it computed them from its own rounded rows
    assert plan.unreliable_reasons == (LABEL_DIFFER, LABEL_ROUNDED)


def test_override_keeps_applying_after_dates_pass() -> None:
    # 2027-04-02: the first amount is paid, MOEX's face is 0.030, the rows are unchanged
    b = _bond("0.030")
    plan = plan_schedule(b, _moex(), _override(), date(2027, 4, 2))
    assert plan.override == "applied"
    assert LABEL_DIFFER not in plan.labels(b, date(2027, 4, 2))


@pytest.mark.parametrize(
    ("why", "bond", "moex", "override"),
    [
        ("face changed", _bond("0.02"), _moex(), _override()),
        # a whole payment of 0.005 is a typo or a changed face, not rounding: sum 0.040 / 0.035
        (
            "typo: one amount 0.01",
            _bond(),
            _moex(),
            _override(values=["0.005"] * 6 + ["0.01"]),
        ),
        ("face 0.03 before the first payment", _bond("0.03"), _moex(), _override()),
        ("face 0.04 before the first payment", _bond("0.04"), _moex(), _override()),
        ("face unknown", _bond(face_value=None), _moex(), _override()),
        ("indexed face", _bond(is_face_indexed=True), _moex(), _override()),
        ("a date was added", _bond(), _moex(dates=[*FUTURE_DATES, date(2030, 9, 30)]), _override()),
        ("a date was dropped", _bond(), _moex(dates=FUTURE_DATES[:-1]), _override()),
        ("a date moved", _bond(), _moex(dates=[date(2027, 4, 1), *FUTURE_DATES[1:]]), _override()),
        ("amount beyond rounding", _bond(), _moex(future="0.02"), _override()),
        ("currency", _bond(), _moex(), _override(unit="EUR")),
        ("bond currency", _bond(face_unit="RUB"), _moex(), _override()),
        ("sum differs from the face", _bond(), _moex(), _override(value="0.006")),
    ],
)
def test_diverged_override_is_noticed_and_not_applied(
    why: str, bond: dict[str, Any], moex: list[dict[str, Any]], override: list[dict[str, Any]]
) -> None:
    plan = plan_schedule(bond, moex, override, DAY)
    assert plan.override == "stale", why
    assert plan.stale_reason
    assert [r["source"] for r in plan.rows] == ["moex_iss"] * len(moex)
    assert LABEL_OVERRIDE_STALE in plan.labels(bond, DAY)
    assert LABEL_OVERRIDE_APPLIED not in plan.labels(bond, DAY)


def test_override_with_moex_amounts_not_yet_published_still_needs_the_dates() -> None:
    moex = _moex()
    for r in moex:
        if r["amort_date"] >= FUTURE_DATES[0]:
            r["value"] = None
    plan = plan_schedule(_bond(), moex, _override(), DAY)
    assert plan.override == "applied"  # dates agree, nothing to compare the amounts with


def test_packaged_entry_applies_to_a_moex_shaped_schedule() -> None:
    """The real XS0114288789 entry against the schedule MOEX publishes (7 x 0.01, face 0.035)."""
    (entry,) = [e for e in ovr.packaged_overrides()[1] if e.secid == "XS0114288789"]
    rows = [
        {
            "secid": entry.secid, "amort_date": d, "value": v, "face_unit": entry.face_unit,
            "source_note": entry.source_note, "confirmed_by": entry.confirmed_by,
            "confirmed_on": entry.confirmed_on, "file_version": "packaged",
        }
        for d, v in entry.redemptions
    ]  # fmt: skip
    moex = [
        {**r, "secid": entry.secid, "value": D("0.01")}
        for r in _moex(past=[(date(2026, 9, 30), "0.005")])
    ]
    assert [r["amort_date"] for r in moex[1:]] == [d for d, _ in entry.redemptions]
    bond = _bond(secid=entry.secid, isin=entry.isin)
    plan = plan_schedule(bond, moex, rows, DAY)
    assert plan.override == "applied", plan.stale_reason
    assert _values(plan.rows) == [D("0.005")] * 7


def test_override_amount_missing_at_moex_is_documented_not_stale() -> None:
    moex = _moex()
    moex[-1]["value"] = None  # MOEX publishes the date before the amount
    assert plan_schedule(_bond(), moex, _override(), DAY).override == "applied"
    # ... but the date itself must be there
    assert plan_schedule(_bond(), moex[:-1], _override(), DAY).override == "stale"


def test_two_moex_rows_on_one_date_do_not_depend_on_input_order() -> None:
    moex = _moex()
    moex[-1]["value"] = D("0.005")
    extra = {**moex[-1], "data_source": "amortization"}  # together 0.01, as MOEX rounds
    plans = [
        plan_schedule(_bond(), order, _override(), DAY)
        for order in ([*moex, extra], [extra, *moex])
    ]
    assert [p.override for p in plans] == ["applied", "applied"]
    assert [r["data_source"] for r in plans[0].rows] == [r["data_source"] for r in plans[1].rows]
    assert plans[0].rows[-1]["data_source"] == "maturity"


def test_rounded_label_does_not_flicker_on_a_payment_day() -> None:
    # today's amount is off the grid; the reading without it is rounding (face date unresolved)
    rows = [{"date": DAY, "value_per_bond": D("0.0105")}] + [
        {"date": d, "value_per_bond": D("0.01")} for d in FUTURE_DATES[:6]
    ]
    b = _bond("0.035")
    assert face_schedule_labels(b, rows, DAY)[1] == LABEL_DIFFER
    assert redemptions_rounded_by_source(b, rows, DAY)
    # when no reading is a rounding (every reading off the grid), it is not
    off = [{**r, "value_per_bond": r["value_per_bond"] + D("0.0001")} for r in rows]
    assert not redemptions_rounded_by_source(b, off, DAY)


# ------------------------------------------------------------ packaged file and loader


def test_packaged_overrides_validate() -> None:
    version, entries = ovr.packaged_overrides()
    assert version
    (e,) = [e for e in entries if e.secid == "XS0114288789"]
    assert e.face_unit == "USD" and e.confirmed_on == date(2026, 11, 17)
    assert [d.isoformat() for d, _ in e.redemptions] == [
        "2027-03-31", "2027-09-30", "2028-03-31", "2028-09-30",
        "2029-03-31", "2029-09-30", "2030-03-31",
    ]  # fmt: skip
    assert {v for _, v in e.redemptions} == {D("0.005")}
    assert sum(v for _, v in e.redemptions) == D("0.035")


DOC: dict[str, Any] = {
    "version": "test.1",
    "entries": [
        {
            "secid": "TESTLOW00009", "isin": "TESTLOW00009", "face_unit": "usd",
            "redemptions": [{"date": d.isoformat(), "value": "0.005"} for d in FUTURE_DATES],
            "source_note": "TEST note", "confirmed_by": "owner", "confirmed_on": "2026-11-17",
        }
    ],
}  # fmt: skip


def test_load_overrides_rejects_bad_entries() -> None:
    assert ovr.load_overrides(DOC)[1][0].face_unit == "USD"

    def bad(**change: Any) -> dict[str, Any]:
        doc = copy.deepcopy(DOC)
        doc["entries"][0].update(change)
        return doc

    for change in (
        {"secid": ""},
        {"face_unit": ""},
        {"redemptions": []},
        {"redemptions": [{"date": "2027-03-31", "value": 0.005}]},  # a float, not a decimal string
        {"redemptions": [{"date": "2027-03-31", "value": "0"}]},
        {"redemptions": [{"date": "2027-03-32", "value": "0.005"}]},
        {"redemptions": [{"date": "2027-03-31", "value": "0.005"}] * 2},
        {"redemptions": [{"date": "2027-03-31"}]},
        {"source_note": " "},
        {"confirmed_by": None},
        {"confirmed_on": "17.11.2026"},
        {"isin": "XS0114288788"},  # check digit
        {"isin": "XS01142887"},  # length
    ):
        with pytest.raises(ValueError, match="bond_amortization_overrides entry 0"):
            ovr.load_overrides(bad(**change))
    with pytest.raises(ValueError, match="duplicate"):
        ovr.load_overrides({**DOC, "entries": [DOC["entries"][0], DOC["entries"][0]]})


# --------------------------------------------------------------------------- database


def _seed_low_face(conn: Conn, face: str = "0.035") -> None:
    seed(conn)
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, issuer_id, name, short_name, bond_class, coupon_type,
            coupon_type_basis, face_value, initial_face_value, face_unit, currency_id,
            issue_date, maturity_date, is_perpetual, coupon_frequency, is_amortizing,
            is_subordinated, is_fx_face, primary_board, marketprice_board, status) VALUES
          ('TESTLOW00009', 'TESTLOW00009', 2, 'Test low face', 'Test low', 'corporate', 'fixed',
           'moex_label', %(face)s, 1, 'USD', 'USD', '2000-03-31', '2030-03-31', false, 2, true,
           false, true, 'TQCB', 'TQCB', 'active'),
          ('TESTAMORT001', 'TESTAMORT001', 2, 'Test amortizer', 'Test amort', 'corporate',
           'fixed', 'moex_label', 750, 1000, 'RUB', 'RUB', '2020-01-01', '2028-01-01', false, 2,
           true, false, false, 'TQCB', 'TQCB', 'active')
        """,
        {"face": face},
    )
    for r in _moex():
        conn.execute(
            "INSERT INTO ref.bond_amortization (secid, amort_date, data_source, value, face_value, "
            "initial_face_value, face_unit) VALUES (%s, %s, %s, %s, %s, 1, 'USD')",
            (r["secid"], r["amort_date"], r["data_source"], r["value"], face),
        )
    conn.execute(
        "INSERT INTO ref.bond_amortization (secid, amort_date, data_source, value, face_value, "
        "initial_face_value, face_unit) VALUES "
        "('TESTAMORT001', '2025-01-01', 'amortization', 250, 750, 1000, 'RUB'), "
        "('TESTAMORT001', '2027-01-01', 'amortization', 250, 750, 1000, 'RUB'), "
        "('TESTAMORT001', '2028-01-01', 'maturity', 500, 750, 1000, 'RUB')"
    )
    conn.execute(
        """
        INSERT INTO md.bond_eod (trade_date, secid, board, close, waprice, accrued_int,
            face_value, face_unit, currency_id, value, yield_close_pct, ingested_at) VALUES
          ('2026-10-15', 'TESTLOW00009', 'TQCB', 60.0, 60.5, 0, 0.035, 'USD', 'USD', 1000, 53.0,
           '2026-10-15 16:00Z'),
          ('2026-10-15', 'TESTAMORT001', 'TQCB', 99.0, 99.1, 0, 750, 'SUR', 'SUR', 1000, 15.0,
           '2026-10-15 16:00Z');
        INSERT INTO md.bond_eod_yield (trade_date, secid, board, yield_date, yield_date_type,
            price, effective_yield_pct, waprice, effective_yield_waprice_pct, gspread_bp,
            duration_days, ingested_at) VALUES
          ('2026-10-15', 'TESTLOW00009', 'TQCB', '2030-03-31', 'MATDATE', 60.0, 53.567, 60.5,
           53.9, 12, 400, '2026-10-15 16:00Z'),
          ('2026-10-15', 'TESTAMORT001', 'TQCB', '2028-01-01', 'MATDATE', 99.0, 15.1, 99.1,
           15.2, 7, 300, '2026-10-15 16:00Z');
        """
    )
    conn.commit()


def _run_terms(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, db: Database, doc: dict[str, Any]
) -> Any:
    monkeypatch.setattr(bond_terms, "packaged_overrides", lambda: ovr.load_overrides(doc))
    return run_job("bond_terms_curated", {}, settings=settings, db=db)


def _build(db: Database) -> tuple[dict[str, Any], dict[str, Any]]:
    known_at = utc_now()
    static_cut = Cut("static", "change", DAY, datetime(2026, 10, 15, 7, 45, tzinfo=MSK), known_at)
    market_cut = Cut("market", "eod_final", DAY, datetime(2026, 10, 16, 8, 0, tzinfo=MSK), known_at)
    with db.connection() as c:
        static = build_static(c, cut=static_cut)
        static_doc = static.document(None)
        market = build_market(
            c,
            cut=market_cut,
            static=static,
            static_snapshot_id=sha256_hex(canonical_bytes(static_doc)),
        )
        market_doc = market.document(None)
    validate_bundle(static_doc)
    validate_bundle(market_doc)
    return static_doc, market_doc


def _row(rows: list[dict[str, Any]], secid: str, key: str = "secid") -> dict[str, Any]:
    (r,) = [r for r in rows if r[key] == secid]
    return r


def test_job_applies_the_override_and_the_snapshot_publishes_it(
    conn: Conn, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_low_face(conn)
    run = _run_terms(monkeypatch, settings, db, DOC)
    assert run.status == "succeeded"
    row = conn.execute(
        "SELECT count(*) AS n, min(confirmed_by) AS who, min(file_version) AS v, "
        "min(face_unit) AS unit FROM ref.bond_amortization_override WHERE NOT is_deleted"
    ).fetchone()
    assert row == {"n": 7, "who": "owner", "v": "test.1", "unit": "USD"}

    static, market = _build(db)
    assert static["schema_version"] == SCHEMA_VERSION == market["schema_version"]
    inst = _row(static["data"]["instruments"], "TESTLOW00009")
    assert {LABEL_NOT_IN_SCHEDULE, LABEL_OVERRIDE_APPLIED} <= set(inst["labels"])
    assert {LABEL_DIFFER, LABEL_ROUNDED}.isdisjoint(inst["labels"])
    assert inst["source_analytics_unreliable_reasons"] == [LABEL_DIFFER, LABEL_ROUNDED]
    mine = [r for r in static["data"]["redemptions"] if r["secid"] == "TESTLOW00009"]
    assert [r["source"] for r in mine] == ["moex_iss"] * 6 + ["manual_override"] * 7
    new = mine[6:]
    assert [r["date"] for r in new] == [d.isoformat() for d in FUTURE_DATES]
    assert {r["value_per_bond"] for r in new} == {"0.005"} and new[-1]["kind"] == "maturity"
    assert new[0]["override"] == {
        "note": "TEST note", "confirmed_by": "owner", "confirmed_on": "2026-11-17",
        "file_version": "test.1",
    }  # fmt: skip
    assert all(r["override"] is None for r in mine[:6])
    cov = {d["name"]: d for d in static["datasets"]}["redemptions"]["coverage"]
    assert cov["override_applied"] == ["TESTLOW00009"] and cov["override_stale"] == []
    assert cov["by_source"]["manual_override"] == 7
    assert {"dataset": "redemptions", "source": "manual_override"}.items() <= next(
        s for s in static["sources"] if s["source"] == "manual_override"
    ).items()

    # an ordinary amortizer: no label, no reason, rows as MOEX publishes them
    amort = _row(static["data"]["instruments"], "TESTAMORT001")
    assert {LABEL_DIFFER, LABEL_NOT_IN_SCHEDULE, LABEL_ROUNDED}.isdisjoint(amort["labels"])
    assert amort["source_analytics_unreliable_reasons"] == []
    assert {r["source"] for r in static["data"]["redemptions"] if r["secid"] == "TESTAMORT001"} == {
        "moex_iss"
    }

    # market quotes: the MOEX values stay, the derived fields are listed as unreliable
    low = _row(market["data"]["bond_quotes"], "TESTLOW00009")
    assert low["source_analytics"]["effective_yield_pct"] == "53.567"
    assert low["source_analytics_unreliable"]["reasons"] == [LABEL_DIFFER, LABEL_ROUNDED]
    fields = low["source_analytics_unreliable"]["fields"]
    assert {"effective_yield_pct", "effective_yield_waprice_pct", "duration_days",
            "gspread_bp", "yield_close_pct"} <= set(fields)  # fmt: skip
    assert "yield_date" not in fields and "icpi" not in fields
    assert (
        _row(market["data"]["bond_quotes"], "TESTAMORT001")["source_analytics_unreliable"] is None
    )

    # published and read back: the stored bundle validates against the contract
    out = publish_static_snapshot(
        db,
        cut_type="daily",
        business_date=DAY,
        cut_ts=datetime(2026, 10, 15, 7, 45, tzinfo=MSK).astimezone(UTC),
        known_at=utc_now(),
    )
    stored = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (out.id,)
    ).fetchone()
    assert stored is not None
    assert json.loads(gzip.decompress(bytes(stored["payload_gz"])))["schema_version"] == (
        SCHEMA_VERSION
    )


def test_diverged_override_is_reported_by_the_job_and_not_published(
    conn: Conn, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_low_face(conn)
    assert _run_terms(monkeypatch, settings, db, DOC).status == "succeeded"
    # MOEX changes the number of future redemptions: one more date appears
    conn.execute(
        "INSERT INTO ref.bond_amortization (secid, amort_date, data_source, value, face_value, "
        "initial_face_value, face_unit) VALUES "
        "('TESTLOW00009', '2030-09-30', 'amortization', 0.01, 0.035, 1, 'USD')"
    )
    conn.commit()

    run = _run_terms(monkeypatch, settings, db, DOC)
    assert run.status == "partial"
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (run.run_id,)).fetchone()
    assert err is not None
    assert "TESTLOW00009" in err["error"] and "not applied" in err["error"]

    static, _ = _build(db)
    inst = _row(static["data"]["instruments"], "TESTLOW00009")
    assert LABEL_OVERRIDE_STALE in inst["labels"] and LABEL_OVERRIDE_APPLIED not in inst["labels"]
    # eight rows of 0.01 against 0.035: the gap is more than eight half steps - not rounding
    assert LABEL_DIFFER in inst["labels"] and LABEL_ROUNDED not in inst["labels"]
    assert inst["source_analytics_unreliable_reasons"] == [LABEL_DIFFER]
    mine = [r for r in static["data"]["redemptions"] if r["secid"] == "TESTLOW00009"]
    assert {r["source"] for r in mine} == {"moex_iss"}  # MOEX rows, nothing substituted blindly
    assert {r["value_per_bond"] for r in mine if r["date"] >= "2027-01-01"} == {"0.01"}
    cov = {d["name"]: d for d in static["datasets"]}["redemptions"]["coverage"]
    assert cov["override_applied"] == []
    assert [s["secid"] for s in cov["override_stale"]] == ["TESTLOW00009"]


def test_override_removed_from_the_file_is_soft_deleted(
    conn: Conn, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_low_face(conn)
    assert _run_terms(monkeypatch, settings, db, DOC).status == "succeeded"
    assert _run_terms(monkeypatch, settings, db, {"version": "test.2", "entries": []}).status == (
        "succeeded"
    )
    live = conn.execute(
        "SELECT count(*) AS n FROM ref.bond_amortization_override WHERE NOT is_deleted"
    ).fetchone()
    assert live == {"n": 0}
    static, _ = _build(db)
    inst = _row(static["data"]["instruments"], "TESTLOW00009")
    assert (
        LABEL_OVERRIDE_APPLIED not in inst["labels"] and LABEL_OVERRIDE_STALE not in inst["labels"]
    )
    assert LABEL_ROUNDED in inst["labels"]  # the published rows are MOEX's rounded ones again


def test_override_for_an_unknown_bond_is_a_warning_a_typo_is_partial(
    conn: Conn, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_low_face(conn)
    absent = copy.deepcopy(DOC)  # the database does not hold the bond: nothing to correct
    absent["entries"][0].update(secid="NOSUCHBOND01", isin="NOSUCHBOND01")
    run = _run_terms(monkeypatch, settings, db, absent)
    assert run.status == "succeeded"
    assert run.result["amortization_overrides"]["skipped_unknown_secid"] == ["NOSUCHBOND01"]
    typo = copy.deepcopy(DOC)  # the ISIN is the bond's, the SECID is not
    typo["entries"][0]["secid"] = "TESTLOW0001"
    assert _run_terms(monkeypatch, settings, db, typo).status == "partial"
    wrong = copy.deepcopy(DOC)
    wrong["entries"][0]["isin"] = "XS0000000009"
    with pytest.raises(ValueError, match="ISIN does not match"):
        _run_terms(monkeypatch, settings, db, wrong)


def test_tdm_export_uses_the_effective_schedule_and_lists_unreliable_values(
    conn: Conn, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_low_face(conn)
    monkeypatch.setattr("mdhub.export.tdm.msk_today", lambda: DAY)  # the face is today's
    assert _run_terms(monkeypatch, settings, db, DOC).status == "succeeded"
    export = build_tdm_export(conn, TdmExportOptions(date_to=DAY))
    files = {f.dataset: f for f in export.files}
    flows = [r for r in files["bond_cashflows"].rows if r[0] == "TESTLOW00009"]
    amort = [r for r in flows if r[2] in ("amort", "redemption") and r[1] > DAY]
    assert [(r[1], r[2], r[4]) for r in amort] == [
        (d, "redemption" if d == date(2030, 3, 31) else "amort", D("0.005")) for d in FUTURE_DATES
    ]
    manifest = export.manifest()
    assert manifest["redemption_overrides"] == {"applied": ["TESTLOW00009"], "stale": []}
    assert manifest["unreliable_values"] == [
        {
            "dataset": "md_bond_quotes", "column": "ytm", "instrument_id": "TESTLOW00009",
            "reasons": [LABEL_DIFFER, LABEL_ROUNDED], "rows": 1,
        }
    ]  # fmt: skip
    # the value itself is exported unchanged; the ordinary bond is not listed
    quotes = {r[1]: r for r in files["md_bond_quotes"].rows}
    assert quotes["TESTLOW00009"][3] == D("53.9")
    assert quotes["TESTAMORT001"][3] == D("15.2")

    # a diverged override: MOEX amounts are exported, the manifest says why
    conn.execute("UPDATE ref.bond SET face_value = 0.02 WHERE secid = 'TESTLOW00009'")
    conn.commit()
    export = build_tdm_export(conn, TdmExportOptions(date_to=DAY))
    flows = [r for r in {f.dataset: f for f in export.files}["bond_cashflows"].rows
             if r[0] == "TESTLOW00009" and r[2] == "amort" and r[1] > DAY]  # fmt: skip
    assert {r[4] for r in flows} == {D("0.01")}
    red = export.manifest()["redemption_overrides"]
    assert red["applied"] == [] and [s["secid"] for s in red["stale"]] == ["TESTLOW00009"]
