"""Floaters on the MOEX KBD (contract market_snapshot/1.4): fixing rule, fit and check of the
terms against the ZCYC history (``mdhub.verify.zcycfloat``), job ``zcyc_floater_fit``, curated
ZCYC terms with margin steps, and the static snapshot fields / coverage.

The KBD history and the coupons are synthetic: the coupons are generated with a known formula
(7-year KBD, 5th publication day before the period start, margin 75 bp reset to 125 bp at a
put offer, first coupon set at placement), so the fit must recover exactly that.
"""

from __future__ import annotations

import copy
import gzip
import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import bond_terms, zcyc_floater_fit
from mdhub.snapshot.runner import publish_static_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION
from mdhub.sources.moex_market import kbd_yield_pct
from mdhub.verify.render import render_markdown
from mdhub.verify.static import run_verification
from mdhub.verify.zcycfloat import (
    CouponObs,
    KbdHistory,
    SpreadStep,
    ZcycTerms,
    check_terms,
    fit,
    fixing_date,
)
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]

SECID = "KBD-1"
RESET = date(2023, 1, 16)  # put offer: margin 75 -> 125 bp from the coupon starting that day
TRUE = ZcycTerms(tenor_years=Decimal(7), fixing_offset=5)


def _params(i: int) -> dict[str, float]:
    """A KBD whose level and slope move from day to day (deterministic)."""
    return {
        "B1": 800.0 + 300.0 * math.sin(i / 40.0) + 50.0 * math.sin(i / 7.0),
        "B2": -200.0 + 150.0 * math.cos(i / 55.0),
        "B3": 100.0 * math.sin(i / 23.0),
        "T1": 1.5,
        **{f"G{k}": 0.0 for k in range(1, 10)},
    }


def _history() -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    d, i = date(2019, 6, 3), 0
    while d <= date(2025, 12, 31):
        if d.weekday() < 5:
            out[d] = _params(i)
            i += 1
        d += timedelta(days=1)
    return out


HIST = _history()


def _starts() -> list[date]:
    out, d = [], date(2020, 1, 15)
    while d < date(2025, 12, 1):
        out.append(d if d.weekday() < 5 else d + timedelta(days=7 - d.weekday()))
        d = date(d.year + (d.month + 6 > 12), (d.month + 6 - 1) % 12 + 1, 15)
    return out


def _coupons(h: KbdHistory) -> list[CouponObs]:
    out = []
    for n, start in enumerate(_starts(), start=1):
        fix = fixing_date(h, start, 5, "zcyc_publication_days")
        assert fix is not None
        y = Decimal(repr(kbd_yield_pct(HIST[fix], 7.0))).quantize(Decimal("0.01"), ROUND_HALF_UP)
        margin = Decimal("0.75") if start < RESET else Decimal("1.25")
        rate = y + margin + (Decimal("0.30") if n == 1 else 0)  # coupon 1 set at placement
        out.append(CouponObs(n, start, rate))
    return out


def test_fixing_date_units() -> None:
    h = KbdHistory({date(2026, 1, d): {} for d in (12, 13, 14, 15, 16, 19)})
    assert fixing_date(h, date(2026, 1, 19), 1, "zcyc_publication_days") == date(2026, 1, 16)
    assert fixing_date(h, date(2026, 1, 19), 4, "zcyc_publication_days") == date(2026, 1, 13)
    assert fixing_date(h, date(2026, 1, 19), 0, "zcyc_publication_days") == date(2026, 1, 19)
    assert fixing_date(h, date(2026, 1, 18), 0, "zcyc_publication_days") == date(2026, 1, 16)
    assert fixing_date(h, date(2026, 1, 19), 6, "zcyc_publication_days") is None  # before history
    assert fixing_date(h, date(2026, 1, 19), 3, "calendar_days") == date(2026, 1, 16)
    assert fixing_date(h, date(2026, 1, 19), 2, "business_days") == date(2026, 1, 15)


def test_fit_recovers_terms_and_margin_reset() -> None:
    h = KbdHistory(HIST)
    coupons = _coupons(h)
    res = fit(h, coupons, [RESET], Decimal(125))
    rep = res.reproduction
    assert res.accepted, rep.reason
    assert rep.terms == TRUE
    assert (rep.periods_checked, rep.periods_matched) == (len(coupons), len(coupons) - 1)
    assert [u[0] for u in rep.unmatched] == [1]
    assert [(s.effective_from, s.spread_bp) for s in res.schedule] == [
        (coupons[0].start_date, Decimal(75)), (RESET, Decimal(125)),
    ]  # fmt: skip
    j = rep.as_json()
    assert (j["tenor_years"], j["fixing_offset"], j["tolerance_pct"], j["accepted"]) == (
        "7", 5, "0.005", True,
    )  # fmt: skip
    assert res.candidates[0]["tenor_years"] == "7"


def test_fit_rejects_without_evidence() -> None:
    h = KbdHistory(HIST)
    coupons = _coupons(h)
    # the current margin MOEX shows does not fit the latest coupons: nothing is published
    res = fit(h, coupons, [RESET], Decimal(200))
    assert not res.accepted and res.schedule == []
    assert res.reproduction.reason is not None
    # too few coupons
    res = fit(h, coupons[:4], [], Decimal(75))
    assert not res.accepted and "fewer than" in str(res.reproduction.reason)


def test_check_documented_terms() -> None:
    h = KbdHistory(HIST)
    coupons = _coupons(h)
    steps = [
        SpreadStep(date(2020, 1, 15), Decimal(75), "document"),
        SpreadStep(RESET, Decimal(125), "document"),
    ]
    rep = check_terms(h, coupons, TRUE, steps)
    assert rep.accepted and rep.method == "check" and rep.periods_matched == len(coupons) - 1
    wrong = check_terms(h, coupons, ZcycTerms(Decimal(5), 5), steps)
    assert not wrong.accepted and "documented formula" in str(wrong.reason)


# ------------------------------------------------------------------------------------ job


def _seed_bond(conn: Conn) -> None:
    for q in (
        """
        INSERT INTO ref.bond (secid, isin, bond_class, coupon_type, coupon_type_basis, status,
                              maturity_date, is_perpetual, face_value, face_unit)
        VALUES (%(s)s, 'RU000XKBD001', 'corporate', 'floating', 'benchmark', 'active',
                '2030-01-15', false, 1000, 'RUB')
        """,
        """
        INSERT INTO ref.bond_rate_formula (secid, source, benchmark, benchmark_raw, spread_bp,
                                           status)
        VALUES (%(s)s, 'moex_iss', 'ZCYC', 'ZR_YLD_CRV', 125, 'inferred')
        """,
        """
        INSERT INTO ref.bond_offer (secid, source, offer_key, offer_date, offer_type, status)
        VALUES (%(s)s, 'cbonds', %(r)s::text, %(r)s::date, 'call', 'past')
        """,
    ):
        conn.execute(q, {"s": SECID, "r": RESET.isoformat()})
    h = KbdHistory(HIST)
    coupons = _coupons(h)
    for i, c in enumerate(coupons):
        pay = coupons[i + 1].start_date if i + 1 < len(coupons) else date(2026, 6, 15)
        conn.execute(
            "INSERT INTO ref.bond_coupon (secid, coupon_no, start_date, pay_date, value_prc, "
            "amount, face_value, face_unit, status) VALUES (%s, %s, %s, %s, %s, %s, 1000, 'RUB', "
            "%s)",
            (SECID, c.coupon_no, c.start_date, pay, c.rate_pct,
             (c.rate_pct * 10 * (pay - c.start_date).days / 365).quantize(Decimal("0.01")),
             "known" if i + 1 < len(coupons) else "current_published"),
        )  # fmt: skip
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO md.zcyc_snapshot (trade_date, trade_time, source_ts, params, "
            "standard_yields, check_max_error_bp, check_tolerance_bp, is_eod) "
            "VALUES (%s, '18:50:00', %s, %s, '[]', 0.001, 0.01, true)",
            [
                (d, datetime(d.year, d.month, d.day, 15, 50, tzinfo=UTC), json.dumps(p))
                for d, p in HIST.items()
            ],
        )
    conn.commit()


EVIDENCE = {
    "document_url": "https://example.invalid/decision.pdf",  # test data, not a real document
    "document_title": "TEST decision on issue",
    "page": "12",
    "extracted_text": "TEST clause",
    "retrieved_on": "2026-10-22",
}
DOC: dict[str, Any] = {
    "version": "test.1",
    "entries": [
        {
            "secid": SECID,
            "term": "floater_formula",
            "value": {"benchmark": "ZCYC", "benchmark_raw": "КБД 7 лет", "spread_bp": "125",
                      "benchmark_tenor_years": "7", "fixing_anchor": "accrual_start",
                      "fixing_offset": 5, "fixing_offset_unit": "zcyc_publication_days",
                      "rate_rounding_decimals": 2},
            "evidence": EVIDENCE,
        },
        {
            "secid": SECID,
            "term": "floater_spread",
            "value": {"effective_from": "2020-01-15", "spread_bp": "75"},
            "evidence": EVIDENCE,
        },
        {
            "secid": SECID,
            "term": "floater_spread",
            "value": {"effective_from": RESET.isoformat(), "spread_bp": "125"},
            "evidence": {**EVIDENCE, "document_url": "https://example.invalid/reset.pdf"},
        },
    ],
}  # fmt: skip


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"fixing_offset": None}, "go together"),
        ({"fixing_offset_unit": "weeks"}, "not in"),
        ({"fixing_offset": -1}, "non-negative"),
        ({"benchmark_tenor_years": "0"}, "positive"),
    ],
)
def test_curated_zcyc_validation(patch: dict[str, Any], match: str) -> None:
    doc = copy.deepcopy(DOC)
    doc["entries"][0]["value"].update(patch)
    with pytest.raises(ValueError, match=match):
        bond_terms.load_terms(doc)
    doc = copy.deepcopy(DOC)
    doc["entries"] = doc["entries"][1:]
    with pytest.raises(ValueError, match="floater_spread without floater_formula"):
        bond_terms.load_terms(doc)


def test_job_fit_then_curated_check(
    conn: Conn, settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_bond(conn)
    out = run_job("zcyc_floater_fit", {}, settings=settings, db=db)
    assert out.status == "succeeded", out
    assert out.result["zcyc_floaters"] == 1 and out.result["published_terms"] == 1
    f = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = %s", (SECID,)
    ).fetchone()
    assert f is not None
    assert (f["benchmark_tenor_years"], f["fixing_offset"], f["fixing_offset_unit"]) == (
        7, 5, "zcyc_publication_days",
    )  # fmt: skip
    assert (f["fixing_anchor"], f["rate_rounding_decimals"], f["fixing_basis"]) == (
        "accrual_start", 2, "coupon_history_fit",
    )  # fmt: skip
    assert f["status"] == "inferred" and f["sources"] == ["moex_iss", zcyc_floater_fit.SOURCE]
    assert [(s["effective_from"], s["spread_bp"]) for s in f["spread_schedule"]] == [
        ("2020-01-15", "75"), (RESET.isoformat(), "125"),
    ]  # fmt: skip
    assert f["reproduction"]["accepted"] and f["reproduction"]["unmatched_coupon_nos"] == [1]

    # identical rerun: no new row versions
    hist = "SELECT count(*) AS n FROM ops.row_history WHERE table_name = 'ref.bond_rate_formula'"
    before = conn.execute(hist).fetchone()
    assert run_job("zcyc_floater_fit", {}, settings=settings, db=db).status == "succeeded"
    assert conn.execute(hist).fetchone() == before

    # documented terms win and are checked; the fit row then carries no terms
    monkeypatch.setattr(bond_terms, "packaged_terms", lambda: bond_terms.load_terms(DOC))
    assert run_job("bond_terms_curated", {}, settings=settings, db=db).status == "succeeded"
    assert run_job("zcyc_floater_fit", {}, settings=settings, db=db).status == "succeeded"
    f = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = %s", (SECID,)
    ).fetchone()
    assert f is not None
    assert (f["status"], f["fixing_basis"], f["benchmark_tenor_years"]) == (
        "curated",
        "document",
        7,
    )
    assert f["sources"] == ["curated", "moex_iss"]
    assert f["spread_schedule"][1] == {
        "effective_from": RESET.isoformat(), "spread_bp": "125", "basis": "document",
        "evidence_document_url": "https://example.invalid/reset.pdf",
    }  # fmt: skip
    assert f["reproduction"]["method"] == "check" and f["reproduction"]["accepted"]

    # a bond that leaves ZCYC loses its fit row
    conn.execute(
        "UPDATE ref.bond_rate_formula SET benchmark = 'KEY_RATE' WHERE secid = %s", (SECID,)
    )
    conn.commit()
    out = run_job("zcyc_floater_fit", {}, settings=settings, db=db)
    assert out.result["soft_deleted"] == 1


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def test_static_snapshot_1_4_fields(conn: Conn, settings: Settings, db: Database) -> None:
    seed(conn)
    _seed_bond(conn)
    assert run_job("zcyc_floater_fit", {}, settings=settings, db=db).status == "succeeded"
    out = publish_static_snapshot(
        db,
        cut_type="daily",
        business_date=date(2026, 10, 15),
        cut_ts=datetime(2026, 10, 15, 7, 45, tzinfo=MSK).astimezone(UTC),
        known_at=utc_now(),
    )
    doc = _bundle(conn, out.id)  # validated against the current schema when published
    assert doc["schema_version"] == SCHEMA_VERSION
    assert int(SCHEMA_VERSION.rsplit(".", 1)[1]) >= 4  # the 1.4 KBD floater terms in later minors
    f = {r["secid"]: r for r in doc["data"]["floater_formulas"]}
    k = f[SECID]
    assert (k["benchmark_tenor_years"], k["fixing_offset"], k["fixing_basis"]) == (
        "7", 5, "coupon_history_fit",
    )  # fmt: skip
    assert k["spread_schedule"][0] == {
        "effective_from": "2020-01-15", "spread_bp": "75", "basis": "coupon_history_fit",
        "evidence_document_url": None,
    }  # fmt: skip
    assert k["reproduction"]["accepted"] and k["reproduction"]["method"] == "fit"
    other = next(r for s, r in f.items() if s != SECID)
    assert other["benchmark_tenor_years"] is None and other["reproduction"] is None
    cov = next(d for d in doc["datasets"] if d["name"] == "floater_formulas")["coverage"]
    assert (cov["zcyc_floaters"], cov["zcyc_floaters_with_tenor"], cov["zcyc_floaters_curated"],
            cov["zcyc_floaters_reproduced"]) == (1, 1, 0, 1)  # fmt: skip
    assert cov["zcyc_coupon_periods_matched"] == cov["zcyc_coupon_periods_checked"] - 1


def test_verify_static_reports_zcyc_floaters(conn: Conn) -> None:
    _seed_bond(conn)
    run = run_verification(conn)
    (z,) = run.zcyc_floaters
    assert z.secid == SECID and z.reproduction["accepted"]
    assert run.zcyc_calendar is not None
    assert run.zcyc_calendar.publication_days == len(HIST)
    md = render_markdown(run)
    assert "## 4. KBD (ZCYC) floaters" in md and "1 of 1 KBD floaters reproduced" in md
