"""Static of a past business date (market-data-hub#102): a cut of a past date built later binds a
static with the contract terms as known now and the time-varying values as of the business date,
never information that appeared after it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import mdhub.loaders.snapshots  # noqa: F401  (registers the snapshot jobs)
from mdhub.config import Settings
from mdhub.core.timeutil import utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.static import (
    STATIC_BASIS_AS_OF_DATE,
    bond_as_of,
    coupon_as_of,
    offer_as_of,
    placed_by,
)
from test_api_seed import seed
from test_snapshots import DAY, Conn, _all_snapshots_valid, _bundle, _cut_ts

D = date(2026, 3, 16)


def _bond(**kw: Any) -> dict[str, Any]:
    return {
        "secid": "B",
        "status": "active",
        "issue_date": date(2025, 1, 10),
        "start_date_moex": None,
        "maturity_date": date(2027, 1, 10),
        "face_value": Decimal(500),
        "initial_face_value": Decimal(1000),
        "is_face_indexed": False,
        **kw,
    }


def test_placed_by() -> None:
    assert placed_by(_bond(), D)
    assert placed_by(_bond(issue_date=D), D)
    assert not placed_by(_bond(issue_date=date(2026, 3, 17)), D)
    assert not placed_by(_bond(issue_date=None, start_date_moex=date(2026, 6, 1)), D)
    assert placed_by(_bond(issue_date=None), D)


def test_bond_as_of_status_and_face() -> None:
    redemptions = [
        {"date": date(2026, 6, 1), "value_per_bond": Decimal(500)},
        {"date": date(2027, 1, 10), "value_per_bond": Decimal(500)},
    ]
    b = bond_as_of(_bond(status="matured", maturity_date=date(2026, 4, 1)), redemptions, D)
    assert b["status"] == "active" and b["face_value"] == Decimal(1000)
    assert bond_as_of(_bond(), redemptions, date(2026, 6, 1))["face_value"] == Decimal(500)
    old = _bond(status="matured", maturity_date=date(2026, 3, 1))
    assert bond_as_of(old, [], D)["status"] == "matured"
    # an indexed face or a schedule that cannot be established keeps the stored face
    assert bond_as_of(_bond(is_face_indexed=True), redemptions, D)["face_value"] == Decimal(500)
    gap = [{"date": date(2026, 6, 1), "value_per_bond": None}]
    assert bond_as_of(_bond(), gap, D)["face_value"] == Decimal(500)


def test_coupon_and_offer_as_of() -> None:
    later = {"start_date": date(2026, 4, 15), "amount": Decimal(40), "value_rub": Decimal(40),
             "value_prc": Decimal(16), "status": "known"}  # fmt: skip
    assert coupon_as_of(later, "floating", D) == {
        **later, "amount": None, "value_rub": None, "value_prc": None, "status": "unknown"
    }  # fmt: skip
    assert coupon_as_of(later, "variable_known", D)["status"] == "unknown"
    assert coupon_as_of(later, "fixed", D) is later  # a fixed rate is a contract term
    current = {**later, "start_date": D}
    assert coupon_as_of(current, "floating", D) is current
    done = {"offer_date": date(2026, 5, 15), "status": "completed"}
    assert offer_as_of(done, D)["status"] == "scheduled"
    assert offer_as_of({**done, "offer_date": date(2026, 3, 1)}, D)["status"] == "completed"
    assert offer_as_of({"offer_date": None, "status": "completed"}, D)["status"] == "completed"


def _history(conn: Conn) -> None:
    """Quotes moved to D; after D: a new bond, a matured one, an amortization, a floater
    coupon, an offer that was executed later and a rating."""
    for table in ("md.bond_eod", "md.bond_eod_yield"):
        conn.execute(f"UPDATE {table} SET trade_date = %s WHERE trade_date = %s", (D, DAY))
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, issuer_id, name, short_name, bond_class, coupon_type,
            coupon_type_basis, face_value, initial_face_value, face_unit, currency_id,
            issue_date, maturity_date, coupon_frequency, primary_board, status)
        VALUES ('NEW1', 'RU000NEW0001', 2, 'Новый', 'Новый', 'corporate', 'fixed', 'moex_label',
            1000, 1000, 'RUB', 'RUB', '2026-06-01', '2029-06-01', 2, 'TQCB', 'active')
        """
    )
    conn.execute(
        "UPDATE ref.bond SET status = 'matured', maturity_date = '2026-04-01' "
        "WHERE secid = 'SU26242RMFS0'"
    )
    conn.execute("UPDATE ref.bond SET face_value = 500, is_amortizing = true WHERE secid = 'DUPA'")
    conn.execute(
        """
        INSERT INTO ref.bond_amortization (secid, amort_date, data_source, value_prc, value,
            face_value, initial_face_value, face_unit) VALUES
          ('DUPA', '2026-06-01', 'amortization', 50, 500, 1000, 1000, 'USD'),
          ('DUPA', '2027-06-01', 'maturity', 50, 500, 500, 1000, 'USD')
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_coupon (secid, coupon_no, start_date, pay_date, amount, value_prc,
            face_value, face_unit, status)
        VALUES ('RU000A105X12', 5, '2026-01-15', '2026-04-15', 44.88, 18.0, 1000, 'RUB', 'known')
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_offer (secid, source, offer_key, offer_date, offer_type, status,
            price) VALUES
          ('RU000A105X12', 'moex_iss', '2026-05-15', '2026-05-15', 'put', 'completed', 100),
          ('RU000A105X12', 'moex_iss', '2026-02-02', '2026-02-02', 'put', 'completed', 100)
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_rating (secid, agency, scale, source, rating, rating_date) VALUES
          ('RU000A105X12', 'АКРА', 'нац', 'cbonds', 'A(RU)', '2026-05-01'),
          ('RU000A105X12', 'Эксперт РА', 'нац', 'cbonds', 'ruA', '2026-01-10')
        """
    )
    conn.commit()


def _static(conn: Conn, db: Database, *, historical: bool) -> dict[str, Any]:
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=D,
        cut_ts=_cut_ts(D),
        known_at=utc_now(),
        historical=historical,
    )
    market = _bundle(conn, out.market.id)
    static = _bundle(conn, out.static.id)
    assert market["static_snapshot_id"] == out.static.id
    return static


@pytest.fixture
def history(conn: Conn) -> Conn:
    seed(conn)
    _history(conn)
    return conn


def test_past_cut_binds_static_as_of_the_business_date(history: Conn, db: Database) -> None:
    now = _static(history, db, historical=False)
    assert "NEW1" in {i["secid"] for i in now["data"]["instruments"]}  # the look-ahead (#102)

    static = _static(history, db, historical=True)
    assert static["business_date"] == D.isoformat()
    assert static["parameters"]["static_basis"] == STATIC_BASIS_AS_OF_DATE
    data = static["data"]
    instruments = {i["secid"]: i for i in data["instruments"]}
    assert "NEW1" not in instruments
    assert instruments["SU26242RMFS0"]["status"] == "active"
    assert "matured" not in instruments["SU26242RMFS0"]["labels"]
    assert instruments["DUPA"]["face_value"] == "1000"
    coupons = {(c["secid"], c["coupon_no"]): c for c in data["coupon_periods"]}
    assert coupons[("RU000A105X12", 5)]["rate_pct"] == "18.0"  # period running on D
    assert coupons[("RU000A105X12", 8)]["rate_pct"] is None  # starts after D
    assert coupons[("RU000A105X12", 8)]["status"] == "unknown"
    assert coupons[("SU26242RMFS0", 15)]["rate_pct"] == "9.0"  # fixed: a contract term
    offers = {o["offer_key"]: o["status"] for o in data["offers"]}
    assert offers["2026-05-15"] == "scheduled" and offers["2026-02-02"] == "completed"
    assert [r["agency"] for r in data["ratings"]] == ["Эксперт РА"]
    # the floater keeps its formula (contract terms as known now)
    assert [f["secid"] for f in data["floater_formulas"]] == ["RU000A105X12"]
    assert _all_snapshots_valid(history) == 4  # the historical cut corrects the first one


def test_jobs_build_old_days_on_their_static(
    history: Conn, settings: Settings, db: Database, today_is: Any
) -> None:
    today_is(date(2026, 10, 20))
    first = run_job(
        "snapshot_market", {"mode": "eod_final", "day": D.isoformat()}, settings=settings, db=db
    )
    assert first.status == "succeeded", first
    static = _bundle(history, first.result["static"]["id"])
    assert static["business_date"] == D.isoformat()
    assert static["parameters"]["static_basis"] == STATIC_BASIS_AS_OF_DATE
    rebuilt = run_job("snapshot_rebuild", {"day": D.isoformat()}, settings=settings, db=db)
    assert rebuilt.status == "succeeded", rebuilt
    (res,) = rebuilt.result.values()
    assert res["market"]["status"] == "unchanged"
    assert res["static"]["id"] == first.result["static"]["id"]
