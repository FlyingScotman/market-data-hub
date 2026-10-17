"""``mdhub verify static``: bond static verification on synthetic bonds."""

from __future__ import annotations

import csv
import io
import math
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub import cli
from mdhub.verify.bondmath import (
    BondSchedule,
    Coupon,
    Redemption,
    accrued,
    check_amortization_sum,
    check_coupon_amounts,
    check_coupon_dates,
    check_fixed_coupons_known,
    check_floater_formula,
    check_perpetual_label,
    check_redemption_at_maturity,
    days_30e_360,
    effective_yield,
    yield_cash_flows,
)
from mdhub.verify.render import BEGIN, END, merge_generated, render_csv, render_markdown
from mdhub.verify.static import (
    AiRow,
    BondResult,
    _ai_row,
    classify_ai_mismatch,
    run_verification,
    verify_bond_ai,
)

Conn = psycopg.Connection[DictRow]
runner = CliRunner()

D = Decimal
Q2 = D("0.01")


def r2(v: Decimal) -> Decimal:
    return v.quantize(Q2, rounding=ROUND_HALF_UP)


def fixed_bond(**kw: Any) -> BondSchedule:
    """1000 RUB, 7.1 %, three 182-day coupons of 35.40 from 2025-01-08, bullet 2026-07-08."""
    starts = [date(2025, 1, 8), date(2025, 7, 9), date(2026, 1, 7), date(2026, 7, 8)]
    b = BondSchedule(
        secid=kw.pop("secid", "FIX1"),
        initial_face=D(1000),
        face_value=D(1000),
        face_unit=kw.pop("face_unit", "RUB"),
        maturity=date(2026, 7, 8),
        coupon_frequency=2,
        coupons=[Coupon(i + 1, starts[i], starts[i + 1], D("35.40"), D("7.1")) for i in range(3)],
        redemptions=[Redemption(date(2026, 7, 8), D(1000), D(100), "maturity")],
        **kw,
    )
    b.refresh()
    return b


# ------------------------------------------------------------------------------ pure


def test_periods_face_and_conventions() -> None:
    b = fixed_bond()
    p = b.period_at(date(2025, 3, 3))
    assert p is not None and p.coupon.no == 1 and p.days == 182
    assert b.period_at(date(2025, 7, 9)) is not None
    assert b.period_at(date(2025, 7, 9)).coupon.no == 2  # type: ignore[union-attr]
    assert b.period_at(date(2025, 1, 7)) is None
    assert b.period_at(date(2026, 7, 8)) is None
    d = date(2025, 3, 3)  # 54 days
    assert accrued(p, d, "coupon_pro_rata", frequency=2) == D("35.40") * 54 / 182
    assert accrued(p, d, "rate_act365", frequency=2) == D(1000) * D("7.1") / 100 * 54 / 365
    assert accrued(p, d, "rate_act360", frequency=2) == D(71) * 54 / 360
    assert accrued(p, d, "coupon_freq_365", frequency=2) == D("35.40") * 54 * 2 / 365
    assert days_30e_360(date(2025, 1, 31), date(2025, 3, 31)) == 60
    assert b.face_at(date(2026, 7, 8)) == 0


def test_amortizing_face() -> None:
    b = BondSchedule(
        secid="AM",
        initial_face=D(1000),
        coupons=[
            Coupon(1, date(2025, 1, 8), date(2025, 7, 9), D("35.40"), D("7.1")),
            Coupon(2, date(2025, 7, 9), date(2026, 1, 7), D("17.70"), D("7.1")),
        ],
        redemptions=[
            Redemption(date(2025, 7, 9), D(500), None),
            Redemption(date(2026, 1, 7), None, D(50), "maturity"),
        ],
    )
    assert b.face_at(date(2025, 7, 8)) == 1000
    assert b.face_at(date(2025, 7, 9)) == 500
    assert b.period_at(date(2025, 8, 1)).face == 500  # type: ignore[union-attr]
    res = check_amortization_sum(b)
    assert res.ok is True
    inv, _ = check_coupon_amounts(b)
    assert inv.ok is True  # 17.70 = 7.1 % x 500 x 182/365


def test_effective_yield_closed_form() -> None:
    # one flow of 1100 in 365 days for 1000 -> 10 %
    y = effective_yield([(365, D(1100))], D(1000))
    assert y is not None and abs(y - 10.0) < 1e-8
    b = fixed_bond()
    flows, why = yield_cash_flows(b, date(2026, 1, 8), date(2026, 7, 8))
    assert why is None
    assert flows == [(181, D("35.40")), (181, D(1000))]
    assert effective_yield([], D(1)) is None


def test_invariants() -> None:
    assert check_amortization_sum(fixed_bond()).ok is True
    short = fixed_bond(secid="SHORT")
    short.redemptions = [Redemption(date(2026, 7, 8), D(900), D(90), "maturity")]
    short.refresh()
    res = check_amortization_sum(short)
    assert (res.ok, res.reason) == (False, "sum_below_face")
    early = fixed_bond()
    early.maturity = date(2027, 1, 1)
    assert check_redemption_at_maturity(early).reason == "last_redemption_before_maturity"

    gap = fixed_bond()
    c = gap.coupons[1]
    gap.coupons[1] = Coupon(c.no, c.start + timedelta(days=3), c.pay, c.amount, c.rate_pct)
    gap.refresh()
    assert (check_coupon_dates(gap).ok, check_coupon_dates(gap).reason) == (False, "gap")
    overlap = fixed_bond()
    c = overlap.coupons[2]
    overlap.coupons[2] = Coupon(c.no, c.start - timedelta(days=1), c.pay, c.amount, c.rate_pct)
    assert check_coupon_dates(overlap).reason == "overlap"
    assert check_coupon_dates(fixed_bond()).ok is True

    thirty = fixed_bond()
    # equal semi-annual coupons: 1000 x 7.1 % / 2 = 35.50 -> not ACT/365 (35.40)
    thirty.coupons = [Coupon(x.no, x.start, x.pay, D("35.50"), x.rate_pct) for x in thirty.coupons]
    thirty.refresh()
    res, votes = check_coupon_amounts(thirty)
    assert res.ok is False and res.reason == "matches_rate_freq"
    assert votes.get("rate_freq") == 3 and "rate_act365" not in votes

    flt = fixed_bond(coupon_type="floating")
    assert check_floater_formula(flt).reason == "no_formula_row"
    flt.formula_status = "missing"
    assert check_floater_formula(flt).reason == "formula_without_benchmark"
    flt.formula_status = "inferred"
    assert check_floater_formula(flt).ok is True

    perp = fixed_bond(bond_subtype="Бессрочные")
    assert check_perpetual_label(perp).reason == "perpetual_subtype_not_labelled"
    perp.is_perpetual = True
    assert check_perpetual_label(perp).reason == "perpetual_with_maturity"
    perp.maturity = None
    perp.eod_mat_date = date(2030, 1, 1)
    assert check_perpetual_label(perp).reason == "perpetual_but_eod_matdate"
    nomat = fixed_bond()
    nomat.maturity = None
    assert check_perpetual_label(nomat).reason == "no_maturity_not_perpetual"
    diff = fixed_bond()
    diff.eod_mat_date = date(2026, 7, 9)
    assert check_perpetual_label(diff).reason == "maturity_differs_eod"

    unknown = fixed_bond()
    c = unknown.coupons[2]
    unknown.coupons[2] = Coupon(c.no, c.start, c.pay, None, None, "unknown")
    unknown.refresh()
    assert check_fixed_coupons_known(unknown, date(2025, 8, 1)).ok is False
    assert check_fixed_coupons_known(fixed_bond(), date(2025, 8, 1)).ok is True
    assert check_fixed_coupons_known(fixed_bond(), None).ok is None


def _row(d: date, moex: Decimal | None, **kw: Any) -> AiRow:
    return AiRow(d, "TQCB", d + timedelta(days=1), moex, kw.pop("face", D(1000)), **kw)


def test_accrued_inference_and_reasons() -> None:
    b = fixed_bond()
    days = [date(2025, 3, 3) + timedelta(days=i) for i in range(4)]
    rows = [_row(d, r2(D("35.40") * (d - date(2025, 1, 8)).days / 182)) for d in days]
    # one day late at MOEX
    rows.append(_row(date(2025, 3, 10), r2(D("35.40") * 62 / 182)))
    rows.append(_row(date(2025, 3, 11), None))
    res = BondResult("FIX1", "corporate", "fixed", "active", "RUB")
    verify_bond_ai(b, rows, res)
    assert res.ai_key == "coupon_pro_rata@trade"
    assert (res.ai_checked, res.ai_matched) == (5, 4)
    assert res.ai_reasons == {"day_shift_+1": 1}
    assert res.ai_skipped == {"moex_null": 1}
    assert res.ai_boards["TQCB"]["trade"] == 4

    # EOD face differs from the schedule face
    row = _row(date(2025, 3, 3), D("5.00"), face=D(500))
    assert classify_ai_mismatch(b, row, "coupon_pro_rata@trade", D("10.5")) == "face_differs"
    # MOEX accrues at a steady other rate (coupon set after the period start)
    row = _row(date(2025, 3, 3), r2(D(1000) * D("0.09") * 54 / 365))
    calc = D("10.5")
    assert classify_ai_mismatch(b, row, "coupon_pro_rata@trade", calc) == "accrual_rate_differs"
    # MOEX accrues outside every schedule period
    row = _row(date(2026, 8, 1), D(3))
    assert classify_ai_mismatch(b, row, "coupon_pro_rata@trade", D(0)) == "no_coupon_period"


def test_fx_accrued_conversion() -> None:
    b = fixed_bond(secid="FX1", face_unit="EUR")
    fx = {("EUR", date(2025, 3, 3)): D("100"), ("USD", date(2025, 3, 3)): D("80")}
    rub = _ai_row(
        b,
        ("FX1", "TQCB", date(2025, 3, 3), D("1050.00"), D(1000), D(35), "SUR"),
        date(2025, 3, 4),
        fx,
    )
    assert rub.moex == D("10.50") and rub.fx == D(100)
    usd = _ai_row(
        b,
        ("FX1", "TQOD", date(2025, 3, 3), D("13.125"), D(1000), D(35), "USD"),
        date(2025, 3, 4),
        fx,
    )
    assert usd.moex == D("10.5") and usd.fx == D("1.25")
    same = _ai_row(
        b,
        ("FX1", "TQOE", date(2025, 3, 3), D("10.50"), D(1000), D(35), "EUR"),
        date(2025, 3, 4),
        fx,
    )
    assert same.fx is None and same.moex == D("10.50")
    missing = _ai_row(
        b, ("FX1", "TQCB", date(2025, 3, 4), D(1), D(1000), D(35), "SUR"), date(2025, 3, 5), fx
    )
    assert missing.skip == "fx_rate_missing"


def test_merge_generated_keeps_hand_written_text() -> None:
    gen = f"{BEGIN}\nnew\n{END}\n"
    assert merge_generated(None, gen, "T") == f"# T\n\n{gen}"
    old = f"# T\n\nintro\n\n{BEGIN}\nold\n{END}\n\n## Findings\nkeep\n"
    assert (
        merge_generated(old, gen, "T")
        == f"# T\n\nintro\n\n{BEGIN}\nnew\n{END}\n\n## Findings\nkeep\n"
    )


# ----------------------------------------------------------------------- database


BONDS = [
    # secid, class, coupon_type, face_unit, initial face, maturity, subtype, perpetual
    ("FIX1", "corporate", "fixed", "RUB", 1000, date(2026, 7, 8), None, False),
    ("FX1", "sovereign_fx", "fixed", "USD", 1000, date(2026, 7, 8), None, False),
    ("AM1", "corporate", "fixed", "RUB", 1000, date(2026, 1, 7), None, False),
    ("FLT1", "corporate", "floating", "RUB", 1000, date(2026, 7, 8), None, False),
    ("PERP1", "corporate", "fixed", "RUB", 1000, None, "Бессрочные", False),
    ("ZERO1", "corporate", "zero", "RUB", 1000, date(2026, 7, 1), None, False),
]
MATURITY = {b[0]: b[5] for b in BONDS}
STARTS = [date(2025, 1, 8), date(2025, 7, 9), date(2026, 1, 7), date(2026, 7, 8)]
EOD_DAYS = [date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 3), date(2025, 8, 1)]
FX = D("90.1234")
ZERO_PRICE = D(90)


def _ai(amount: Decimal, start: date, end: date, d: date) -> Decimal:
    return r2(amount * (d - start).days / (end - start).days)


def _seed(conn: Conn) -> None:
    for secid, cls, ct, unit, face, mat, sub, perp in BONDS:
        conn.execute(
            "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status,"
            " face_value, initial_face_value, face_unit, maturity_date, bond_subtype,"
            " is_perpetual, coupon_frequency) VALUES"
            " (%s, %s, %s, 'moex_label', 'active', %s, %s, %s, %s, %s, %s, 2)",
            (secid, cls, ct, face, face, unit, mat, sub, perp),
        )
    coupons: list[tuple[Any, ...]] = []
    for secid in ("FIX1", "FLT1", "PERP1"):
        for i in range(3):
            coupons.append((secid, i + 1, STARTS[i], STARTS[i + 1], D("35.40"), D("7.1")))
    for i in range(3):  # 3.385 % USD: 16.88 per 182 days
        coupons.append(("FX1", i + 1, STARTS[i], STARTS[i + 1], D("16.88"), D("3.385")))
    coupons += [
        ("AM1", 1, STARTS[0], STARTS[1], D("35.40"), D("7.1")),
        ("AM1", 2, STARTS[1], STARTS[2], D("17.70"), D("7.1")),
    ]
    for c in coupons:
        conn.execute(
            "INSERT INTO ref.bond_coupon (secid, coupon_no, start_date, pay_date, amount,"
            " value_prc, status) VALUES (%s, %s, %s, %s, %s, %s, 'known')",
            c,
        )
    amort = [
        ("FIX1", STARTS[3], 1000, 100, "maturity"),
        ("FX1", STARTS[3], 1000, 100, "maturity"),
        ("FLT1", STARTS[3], 1000, 100, "maturity"),
        ("ZERO1", date(2026, 7, 1), 1000, 100, "maturity"),
        ("AM1", STARTS[1], 500, 50, "amortization"),
        ("AM1", STARTS[2], 400, 40, "maturity"),  # sums to 900: schedule error
    ]
    for a in amort:
        conn.execute(
            "INSERT INTO ref.bond_amortization (secid, amort_date, value, value_prc, data_source)"
            " VALUES (%s, %s, %s, %s, %s)",
            a,
        )
    for d in EOD_DAYS:
        p = 0 if d < STARTS[1] else 1
        fix = _ai(D("35.40"), STARTS[p], STARTS[p + 1], d)
        if d == date(2025, 7, 3):
            fix = _ai(D("35.40"), STARTS[p], STARTS[p + 1], d + timedelta(days=1))  # MOEX +1 day
        usd = r2(D(1000) * D("0.03385") * (d - STARTS[p]).days / 365)
        am_face = 1000 if d < STARTS[1] else 500
        am = _ai(D("35.40") if p == 0 else D("17.70"), STARTS[p], STARTS[p + 1], d)
        rows = [
            ("FIX1", fix, 1000, "SUR"),
            ("FX1", usd * FX, 1000, "SUR"),
            ("AM1", am, am_face, "SUR"),
            ("ZERO1", D(0), 1000, "SUR"),
        ]
        for secid, ai, face, ccy in rows:
            conn.execute(
                "INSERT INTO md.bond_eod (trade_date, secid, board, accrued_int, face_value,"
                " currency_id, mat_date) VALUES (%s, %s, 'TQCB', %s, %s, %s, %s)",
                (d, secid, ai, face, ccy, MATURITY[secid]),
            )
        conn.execute(
            "INSERT INTO md.fx_official (effective_date, ccy, nominal, value, rate_per_unit,"
            " record_date, set_date, cbr_id) VALUES (%s, 'USD', 1, %s, %s, %s, %s, 'R01235')",
            (d, FX, FX, d, d - timedelta(days=1)),
        )
    # ZERO1 yield on 2025-07-01 (Tue): settle 07-02, 364 days to 2026-07-01, price 90
    y = (1000 / 900) ** (365 / 364) - 1
    conn.execute(
        "INSERT INTO md.bond_eod_yield (trade_date, secid, board, yield_date, yield_date_type,"
        " price, accrued_int, effective_yield_pct) VALUES"
        " ('2025-07-01', 'ZERO1', 'TQCB', '2026-07-01', 'MATDATE', %s, 0, %s)",
        (ZERO_PRICE, round(y * 100, 4)),
    )
    # FIX1 yield: settle 2025-07-02 -> flows 35.40 on 07-09 (7 d), 01-07 (189 d), 07-08 (371 d)
    flows = [(7, 35.40), (189, 35.40), (371, 1035.40)]
    ai_settle = 34.04  # round(35.40 x 175 / 182, 2), MOEX's settlement accrued interest
    dirty = 98.0 * 10 + ai_settle
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        v = sum(cf * math.exp(-t / 365 * math.log1p(mid)) for t, cf in flows) - dirty
        lo, hi = (mid, hi) if v > 0 else (lo, mid)
    conn.execute(
        "INSERT INTO md.bond_eod_yield (trade_date, secid, board, yield_date, yield_date_type,"
        " price, accrued_int, effective_yield_pct) VALUES"
        " ('2025-07-01', 'FIX1', 'TQCB', '2026-07-08', 'MATDATE', %s, %s, %s)",
        (D("98"), D("34.04"), round(lo * 100, 4)),
    )
    conn.commit()


def test_run_verification_on_synthetic_bonds(conn: Conn) -> None:
    _seed(conn)
    run = run_verification(conn, yield_sample="all")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    by = {b.secid: b for b in run.bonds}
    assert run.eod_rows == 16 and run.bonds_in_ref == 6

    fix = by["FIX1"]
    assert fix.ai_key == "coupon_pro_rata@trade"
    assert (fix.ai_checked, fix.ai_matched) == (4, 3)
    assert fix.ai_reasons == {"day_shift_+1": 1}
    assert (fix.y_checked, fix.y_matched) == (1, 1)

    fx = by["FX1"]
    assert fx.ai_fx_rows == 4 and fx.ai_matched == 4
    am = by["AM1"]
    assert am.ai_matched == 4
    zero = by["ZERO1"]
    assert (zero.ai_checked, zero.ai_matched) == (4, 4)
    assert (zero.y_checked, zero.y_matched) == (1, 1)

    failed = {(b.secid, i.check, i.reason) for b in run.bonds for i in b.failed_invariants()}
    assert ("AM1", "amortization_sum", "sum_below_face") in failed
    assert ("FLT1", "floater_formula", "no_formula_row") in failed
    assert ("PERP1", "perpetual_label", "perpetual_subtype_not_labelled") in failed
    assert not [f for f in failed if f[0] in ("FIX1", "FX1", "ZERO1")]

    md = render_markdown(run)
    assert md.startswith(BEGIN) and md.rstrip().endswith(END)
    assert "`day_shift_+1`" in md and "FIX1" in md
    rows = list(csv.DictReader(io.StringIO(render_csv(run))))
    assert len(rows) == 6
    fix_row = next(r for r in rows if r["secid"] == "FIX1")
    assert fix_row["ai_convention"] == "coupon_pro_rata"
    assert fix_row["ai_reasons"] == "day_shift_+1=1"


def test_run_verification_needs_idle_connection(conn: Conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(ValueError, match="open transaction"):
        run_verification(conn)


def test_cli_verify_static(
    conn: Conn, db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(conn)
    out = tmp_path / "STATIC.md"
    out.write_text(f"# Mine\n\n{BEGIN}\nstale\n{END}\n\n## Findings\nhand-written\n")
    bonds = tmp_path / "bonds.csv"
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    result = runner.invoke(
        cli.app,
        ["verify", "static", "--out", str(out), "--bonds-csv", str(bonds), "--to", "2025-07-31"],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert text.startswith("# Mine") and "stale" not in text
    assert text.rstrip().endswith("hand-written")
    assert "EOD rows (`md.bond_eod`): 12 " in text  # --to excludes 2025-08-01
    assert bonds.read_text().startswith("secid,")
    bad = runner.invoke(cli.app, ["verify", "static", "--from", "01.07.2025"])
    assert bad.exit_code == 2
    bad = runner.invoke(cli.app, ["verify", "static", "--yield-sample", "weekly"])
    assert bad.exit_code == 2
