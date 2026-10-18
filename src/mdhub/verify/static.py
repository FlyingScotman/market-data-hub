"""Bond static verification against MOEX daily data (read-only).

For every ``(secid, board, trade_date)`` of ``md.bond_eod`` with an ``ACCRUEDINT`` the accrued
interest is recomputed from ``ref.bond_coupon`` / ``ref.bond_amortization`` under every
candidate convention of :data:`mdhub.verify.bondmath.CONVENTIONS` and two date bases:

* ``trade`` - accrued to the trade date,
* ``settle`` - accrued to the settlement date: trade date + the board's settlement lag
  (``ref.bond_board.settle_lag_days`` of the SECID/board, else the board's most common lag,
  else T+1) in business days of ``MOEX_STOCK`` (``ref.calendar_effective``, weekday fallback).

Per bond the candidate (convention @ basis) that matches most rows is the bond's **inferred
convention**; rows it does not match get a mismatch reason (:func:`classify_ai_mismatch`). The
fixed reference candidate ``coupon_pro_rata@trade`` is counted as well, so match rates do not
depend on the inference.

Yields (fixed and zero-coupon bonds only, ``md.bond_eod_yield``): cash flows after the
settlement date up to MOEX's ``yield_date`` (maturity or offer; ``MBS`` rows are skipped) are
discounted at the MOEX effective yield convention; dirty price = ``price`` % of the schedule
face on the settlement date + MOEX's settlement accrued interest of the row (MOEX's yield is
computed with it), or the schedule's when MOEX's value is evidently in RUB for a
foreign-currency face (``md.bond_eod_yield.accrued_int`` mixes both units). The yield check
thus isolates the cash-flow schedule from the accrued-interest check.

Invariants run on every bond (:func:`mdhub.verify.bondmath.invariants`).

Everything runs in one ``REPEATABLE READ READ ONLY`` transaction; nothing is written to the
database.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import groupby
from typing import Any, Final

import psycopg
from psycopg.rows import tuple_row

from mdhub.verify.bondmath import (
    AI_TOL,
    AMOUNT_CONVENTIONS,
    CONVENTIONS,
    YIELD_TOL_PCT,
    BondSchedule,
    Coupon,
    InvariantResult,
    Offer,
    Redemption,
    accrued,
    add_business_days,
    effective_yield,
    invariants,
    per_day,
    yield_cash_flows,
)

__all__ = [
    "BASES",
    "CANDIDATES",
    "REFERENCE_CANDIDATE",
    "AiRow",
    "BondResult",
    "BusinessCalendar",
    "VerificationRun",
    "YieldRow",
    "classify_ai_mismatch",
    "load_bonds",
    "run_verification",
    "verify_bond_ai",
    "verify_bond_yield",
]

Conn = psycopg.Connection[Any]

BASES: Final = ("trade", "settle")
CANDIDATES: Final = tuple(f"{c}@{b}" for b in BASES for c in CONVENTIONS)
"""Candidate keys ``convention@basis`` in tie-break order (trade basis first)."""
REFERENCE_CANDIDATE: Final = "coupon_pro_rata@trade"

DEFAULT_SETTLE_LAG: Final = 1
MAX_DAY_SHIFT: Final = 7
"""Implied-day differences up to this many days are reported as ``day_shift_±n``."""
YIELD_FIXED_TYPES: Final = ("fixed", "zero")
FETCH_SIZE: Final = 20_000


# ------------------------------------------------------------------------- calendar


class BusinessCalendar:
    """``MOEX_STOCK`` business days from ``ref.calendar_effective``; Monday-Friday for days the
    table does not cover."""

    def __init__(self, days: dict[date, bool] | None = None) -> None:
        self._days = days or {}
        self._cache: dict[tuple[date, int], date] = {}

    def __call__(self, d: date) -> bool:
        known = self._days.get(d)
        return known if known is not None else d.weekday() < 5

    def settle(self, trade_date: date, lag: int) -> date:
        key = (trade_date, lag)
        out = self._cache.get(key)
        if out is None:
            out = add_business_days(trade_date, lag, self)
            self._cache[key] = out
        return out

    @classmethod
    def load(cls, conn: Conn, calendar: str = "MOEX_STOCK") -> BusinessCalendar:
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(
                "SELECT day, is_business_day FROM ref.calendar_effective WHERE calendar = %s",
                (calendar,),
            )
            return cls({d: bool(b) for d, b in cur.fetchall()})


# ---------------------------------------------------------------------------- rows


@dataclass(frozen=True, slots=True)
class AiRow:
    """One ``md.bond_eod`` row as the accrued-interest check needs it."""

    trade_date: date
    board: str
    settle: date
    moex: Decimal | None
    """MOEX ACCRUEDINT in the face currency (converted with ``fx`` when MOEX reports RUB)."""
    eod_face: Decimal | None = None
    eod_coupon: Decimal | None = None
    fx: Decimal | None = None
    """CBR rate (RUB per face-currency unit) MOEX's RUB value was divided by, if any."""
    skip: str | None = None
    """Reason the row cannot be evaluated (e.g. ``fx_rate_missing``)."""


@dataclass(frozen=True, slots=True)
class YieldRow:
    """One ``md.bond_eod_yield`` row."""

    trade_date: date
    board: str
    settle: date
    yield_date: date | None
    yield_date_type: str | None
    price: Decimal | None
    accrued: Decimal | None
    moex_yield: Decimal | None


# -------------------------------------------------------------------------- results


@dataclass(slots=True)
class BondResult:
    """Verification outcome of one bond (one row of the per-bond CSV)."""

    secid: str
    bond_class: str
    coupon_type: str
    status: str
    face_unit: str | None
    benchmark: str | None = None
    ai_rows: int = 0
    ai_fx_rows: int = 0
    """Rows whose MOEX value is in RUB for a foreign-currency face (converted at CBR)."""
    ai_checked: int = 0
    ai_matched: int = 0
    ai_skipped: Counter[str] = field(default_factory=Counter)
    ai_key: str | None = None
    ai_ref_checked: int = 0
    ai_ref_matched: int = 0
    ai_reasons: Counter[str] = field(default_factory=Counter)
    ai_examples: dict[str, str] = field(default_factory=dict)
    ai_boards: dict[str, Counter[str]] = field(default_factory=dict)
    """board -> counts of ``rows`` / ``trade`` / ``settle`` / ``both`` matches (best convention)."""
    ai_candidate_matches: Counter[str] = field(default_factory=Counter)
    y_rows: int = 0
    y_checked: int = 0
    y_matched: int = 0
    y_skipped: Counter[str] = field(default_factory=Counter)
    y_reasons: Counter[str] = field(default_factory=Counter)
    y_examples: dict[str, str] = field(default_factory=dict)
    y_max_abs_diff: float | None = None
    invariants: list[InvariantResult] = field(default_factory=list)
    amount_votes: dict[str, int] = field(default_factory=dict)

    @property
    def ai_convention(self) -> str | None:
        return self.ai_key.split("@")[0] if self.ai_key else None

    @property
    def ai_basis(self) -> str | None:
        return self.ai_key.split("@")[1] if self.ai_key else None

    @property
    def amount_convention(self) -> str | None:
        if not self.amount_votes:
            return None
        order = [k for k in AMOUNT_CONVENTIONS if k in self.amount_votes]
        return max(order, key=lambda k: self.amount_votes[k])

    def failed_invariants(self) -> list[InvariantResult]:
        return [r for r in self.invariants if r.ok is False]


@dataclass(slots=True)
class VerificationRun:
    """Everything a report needs."""

    generated_at: datetime
    date_from: date | None
    date_to: date | None
    eod_first: date | None
    eod_last: date | None
    yield_dates: list[date]
    yield_sample: str
    bonds: list[BondResult]
    board_lags: dict[str, int]
    eod_rows: int = 0
    bonds_in_ref: int = 0
    duration_s: float = 0.0


# --------------------------------------------------------------------- accrued interest


def _matches(calc: Decimal | None, moex: Decimal) -> bool:
    return calc is not None and abs(calc - moex) <= AI_TOL


def _candidate_values(bond: BondSchedule, row: AiRow) -> dict[str, Decimal | None]:
    out: dict[str, Decimal | None] = {}
    for basis, d in (("trade", row.trade_date), ("settle", row.settle)):
        p = bond.period_at(d)
        if p is not None and bond.is_face_indexed and row.eod_face is not None:
            # indexed face (linkers): the face is market data (MOEX FACEVALUE of the day),
            # the schedule only knows the nominal face
            p = replace(p, face=row.eod_face)
        for conv in CONVENTIONS:
            key = f"{conv}@{basis}"
            if p is None:
                # outside every coupon period: nothing accrues (before the first period,
                # after the last pay date, or in a schedule gap - see classify_ai_mismatch)
                out[key] = Decimal(0)
            else:
                out[key] = accrued(p, d, conv, frequency=bond.coupon_frequency)
    return out


def classify_ai_mismatch(bond: BondSchedule, row: AiRow, key: str, calc: Decimal) -> str:
    """Why ``calc`` (candidate ``key``) differs from MOEX ``row.moex`` - the first matching
    pattern of: ``no_coupon_period`` (MOEX accrues outside every schedule period: missing
    coupon or wrong dates), ``moex_zero`` (MOEX shows 0: default / suspended accrual),
    ``face_differs`` (EOD face value differs from the schedule's outstanding face),
    ``eod_coupon_differs`` (EOD COUPONVALUE differs from the schedule amount),
    ``floater_accrual_vs_final_coupon`` (floater: MOEX accrued during the period at a rate
    other than the final coupon's - in-arrears benchmarks such as RUONIA accrue on the fixings
    known so far), ``day_shift_±n`` (MOEX value = the accrual of n days more/less),
    ``full_coupon`` (MOEX shows the whole coupon), ``coupon_not_fixed`` (period coupon is a
    MOEX projection), ``accrual_rate_differs`` (MOEX accrues at an annual rate other than the
    scheduled coupon's: the coupon was set or changed after the period started),
    ``unexplained``."""
    assert row.moex is not None
    conv, basis = key.split("@")
    d = row.trade_date if basis == "trade" else row.settle
    p = bond.period_at(d)
    if p is None:
        return "no_coupon_period"
    if row.moex == 0:
        return "moex_zero"
    sched_face = bond.face_at(d)
    if (
        not bond.is_face_indexed
        and row.eod_face is not None
        and sched_face is not None
        and abs(row.eod_face - sched_face) > Decimal("0.005")
    ):
        return "face_differs"
    p_trade = bond.period_at(row.trade_date)
    if (
        row.eod_coupon is not None
        and row.eod_coupon != 0
        and p_trade is not None
        and p_trade.coupon.amount is not None
        and abs(row.eod_coupon - p_trade.coupon.amount) > Decimal("0.005")
    ):
        return "eod_coupon_differs"
    if bond.coupon_type == "floating":
        return "floater_accrual_vs_final_coupon"
    daily = per_day(p, conv, frequency=bond.coupon_frequency)
    if daily is not None and daily > 0:
        implied = row.moex / daily
        k = int(implied.to_integral_value())
        if abs(implied - k) * daily <= AI_TOL:
            shift = k - (d - p.start).days
            if k == p.days:
                return "full_coupon"
            if shift != 0 and abs(shift) <= MAX_DAY_SHIFT:
                return f"day_shift_{shift:+d}"
    if p.coupon.status in ("projected_by_moex", "current_published"):
        return "coupon_not_fixed"
    t = (d - p.start).days
    face = row.eod_face or p.face
    if t > 0 and face and p.coupon.amount is not None and p.days > 0:
        moex_rate = row.moex * 365 * 100 / (face * t)
        sched_rate = p.coupon.amount * 365 * 100 / (face * p.days)
        if abs(moex_rate - sched_rate) > Decimal("0.05"):
            return "accrual_rate_differs"
    return "unexplained"


def _example(bond: BondSchedule, row: AiRow, calc: Decimal | None) -> str:
    c = f"{calc:.4f}" if calc is not None else "-"
    m = (
        f"{row.moex:.4f} (RUB/{row.fx})"
        if row.fx is not None and row.moex is not None
        else row.moex
    )
    return f"{bond.secid} {row.board} {row.trade_date}: moex={m} calc={c}"


def verify_bond_ai(bond: BondSchedule, rows: Sequence[AiRow], result: BondResult) -> None:
    """Accrued-interest check of one bond (fills the ``ai_*`` fields of ``result``)."""
    result.ai_rows += len(rows)
    evaluable: list[tuple[AiRow, dict[str, Decimal | None]]] = []
    zero_without_schedule = bond.coupon_type == "zero" and not bond.coupons
    for row in rows:
        if row.skip is not None:
            result.ai_skipped[row.skip] += 1
            continue
        if row.moex is None:
            result.ai_skipped["moex_null"] += 1
            continue
        if row.fx is not None:
            result.ai_fx_rows += 1
        if not bond.coupons and not zero_without_schedule:
            result.ai_skipped["no_coupon_schedule"] += 1
            continue
        values = _candidate_values(bond, row)
        if all(v is None for v in values.values()):
            result.ai_skipped["coupon_unknown"] += 1
            continue
        evaluable.append((row, values))
        for k, v in values.items():
            if _matches(v, row.moex):
                result.ai_candidate_matches[k] += 1
    if not evaluable:
        return
    best = max(CANDIDATES, key=lambda k: (result.ai_candidate_matches[k], -CANDIDATES.index(k)))
    result.ai_key = best
    conv = best.split("@")[0]
    for row, values in evaluable:
        assert row.moex is not None
        ref = values[REFERENCE_CANDIDATE]
        if ref is not None:
            result.ai_ref_checked += 1
            result.ai_ref_matched += _matches(ref, row.moex)
        calc = values[best]
        board = result.ai_boards.setdefault(row.board, Counter())
        board["rows"] += 1
        t_ok = _matches(values[f"{conv}@trade"], row.moex)
        s_ok = _matches(values[f"{conv}@settle"], row.moex)
        board["trade"] += t_ok
        board["settle"] += s_ok
        board["both"] += t_ok and s_ok
        if calc is None:
            result.ai_skipped["coupon_unknown"] += 1
            continue
        result.ai_checked += 1
        if _matches(calc, row.moex):
            result.ai_matched += 1
            continue
        reason = classify_ai_mismatch(bond, row, best, calc)
        result.ai_reasons[reason] += 1
        result.ai_examples.setdefault(reason, _example(bond, row, calc))


# --------------------------------------------------------------------------- yields


def verify_bond_yield(bond: BondSchedule, rows: Sequence[YieldRow], result: BondResult) -> None:
    """Yield check of one fixed / zero-coupon bond (fills the ``y_*`` fields of ``result``)."""
    offers = {o.day: o for o in bond.offers}
    for row in rows:
        result.y_rows += 1
        if row.price is None or row.moex_yield is None:
            result.y_skipped["price_or_yield_null"] += 1
            continue
        if row.yield_date_type == "MBS":
            result.y_skipped["mbs_yield"] += 1
            continue
        if row.yield_date is None or row.yield_date <= row.settle:
            result.y_skipped["no_horizon"] += 1
            continue
        horizon = row.yield_date
        offer = offers.get(horizon)
        price_pct = offer.price_pct if offer is not None and offer.price_pct else Decimal(100)
        flows, why = yield_cash_flows(bond, row.settle, horizon, redemption_price_pct=price_pct)
        if why is not None:
            result.y_skipped[why] += 1
            continue
        face = bond.face_at(row.settle)
        if face is None or face <= 0:
            result.y_skipped["face_unknown"] += 1
            continue
        ai = _yield_accrued(bond, row, result.ai_convention)
        if ai is None:
            result.y_skipped["accrued_unknown"] += 1
            continue
        dirty = row.price / 100 * face + ai
        y = effective_yield(flows, dirty)
        result.y_checked += 1
        if y is None:
            result.y_reasons["solver_failed"] += 1
            continue
        diff = y - float(row.moex_yield)
        if result.y_max_abs_diff is None or abs(diff) > result.y_max_abs_diff:
            result.y_max_abs_diff = abs(diff)
        if abs(diff) <= _yield_tol(row.moex_yield):
            result.y_matched += 1
            continue
        reason = _classify_yield(bond, row, offer is not None, ai, result.ai_convention)
        result.y_reasons[reason] += 1
        result.y_examples.setdefault(
            reason,
            f"{bond.secid} {row.board} {row.trade_date}: moex={row.moex_yield} calc={y:.4f}"
            f" to {row.yield_date_type} {horizon}",
        )


def _yield_tol(moex: Decimal) -> float:
    """:data:`YIELD_TOL_PCT` plus half a unit of the last decimal MOEX published when that is
    coarser than 0.01 pp (some yields come with two decimals)."""
    exp = moex.as_tuple().exponent
    extra = 0.5 * 10.0**exp if isinstance(exp, int) and exp >= -2 else 0.0
    return YIELD_TOL_PCT + extra


def _settle_accrued(bond: BondSchedule, settle: date, convention: str | None) -> Decimal | None:
    """Accrued interest at ``settle`` from the schedule under the bond's inferred convention."""
    p = bond.period_at(settle)
    if p is None:
        return Decimal(0)
    return accrued(p, settle, convention or "coupon_pro_rata", frequency=bond.coupon_frequency)


def _yield_accrued(bond: BondSchedule, row: YieldRow, convention: str | None) -> Decimal | None:
    """Accrued interest for the dirty price: MOEX's own settlement value of the row (MOEX's
    yield is computed with it, even where it is 0), unless it is evidently in RUB for a
    foreign-currency face (more than the largest coupon) - then the schedule's value."""
    ours = _settle_accrued(bond, row.settle, convention)
    if row.accrued is None:
        return ours
    if bond.face_unit in (None, "RUB", "SUR"):
        return row.accrued
    biggest = max((c.amount for c in bond.coupons if c.amount is not None), default=None)
    if biggest is not None and row.accrued > biggest:
        return ours
    return row.accrued


def _classify_yield(
    bond: BondSchedule, row: YieldRow, offer_known: bool, ai: Decimal, convention: str | None
) -> str:
    """``maturity_differs`` / ``offer_not_in_ref`` (static), ``accrued_differs`` (MOEX's
    settlement accrued interest - face-currency rows only - differs from ours),
    ``coupon_not_fixed``, ``fx_face_bond`` (foreign-currency face: MOEX yields of such bonds
    are often a few bp off any standard convention), ``short_horizon``, ``unexplained``."""
    assert row.yield_date is not None
    if row.yield_date_type == "MATDATE" and bond.maturity != row.yield_date:
        return "maturity_differs"
    if row.yield_date_type == "OFFER" and not offer_known:
        return "offer_not_in_ref"
    ours = _settle_accrued(bond, row.settle, convention)
    if ours is not None and abs(ai - ours) > AI_TOL:
        return "accrued_differs"
    if any(c.status != "known" for c in bond.coupons if row.settle < c.pay <= row.yield_date):
        return "coupon_not_fixed"
    if bond.face_unit not in (None, "RUB", "SUR"):
        return "fx_face_bond"
    if (row.yield_date - row.settle).days <= 365:
        return "short_horizon"
    return "unexplained"


# -------------------------------------------------------------------------- loading


def _rows(conn: Conn, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def load_bonds(conn: Conn, secids: Sequence[str] | None = None) -> dict[str, BondSchedule]:
    """``BondSchedule`` of every bond in ``ref.bond`` (or of ``secids``)."""
    flt = " WHERE secid = ANY(%s)" if secids else ""
    params: tuple[Any, ...] = (list(secids),) if secids else ()
    bonds: dict[str, BondSchedule] = {}
    for r in _rows(
        conn,
        "SELECT secid, bond_class, coupon_type, status, initial_face_value, face_value,"
        " face_unit, maturity_date, is_perpetual, coalesce(is_face_indexed, false),"
        " coupon_frequency, bond_subtype FROM ref.bond" + flt,
        params,
    ):
        bonds[r[0]] = BondSchedule(
            secid=r[0],
            bond_class=r[1],
            coupon_type=r[2],
            status=r[3],
            initial_face=r[4],
            face_value=r[5],
            face_unit=r[6],
            maturity=r[7],
            is_perpetual=r[8],
            is_face_indexed=r[9],
            coupon_frequency=r[10],
            bond_subtype=r[11],
        )
    for r in _rows(
        conn,
        "SELECT secid, coupon_no, start_date, pay_date, record_date, amount, value_prc, status"
        " FROM ref.bond_coupon WHERE NOT is_deleted" + flt.replace("WHERE", "AND"),
        params,
    ):
        b = bonds.get(r[0])
        if b is not None:
            b.coupons.append(Coupon(r[1], r[2], r[3], r[5], r[6], r[7], r[4]))
    for r in _rows(
        conn,
        "SELECT secid, amort_date, value, value_prc, data_source, initial_face_value"
        " FROM ref.bond_amortization WHERE NOT is_deleted" + flt.replace("WHERE", "AND"),
        params,
    ):
        b = bonds.get(r[0])
        if b is not None:
            b.redemptions.append(Redemption(r[1], r[2], r[3], r[4]))
            if b.initial_face is None and r[5] is not None:
                b.initial_face = r[5]
    for r in _rows(
        conn,
        "SELECT secid, offer_date, offer_type, status, price FROM ref.bond_offer"
        " WHERE NOT is_deleted AND offer_date IS NOT NULL" + flt.replace("WHERE", "AND"),
        params,
    ):
        b = bonds.get(r[0])
        if b is not None:
            b.offers.append(Offer(r[1], r[2], r[3], r[4]))
    for r in _rows(
        conn, "SELECT secid, status, benchmark FROM ref.bond_rate_formula_resolved" + flt, params
    ):
        b = bonds.get(r[0])
        if b is not None:
            b.formula_status = r[1] if r[2] is not None else "missing"
            b.benchmark = r[2]
    for r in _rows(
        conn,
        "SELECT DISTINCT ON (secid) secid, mat_date FROM md.bond_eod WHERE mat_date IS NOT NULL"
        + flt.replace("WHERE", "AND")
        + " ORDER BY secid, trade_date DESC",
        params,
    ):
        b = bonds.get(r[0])
        if b is not None:
            b.eod_mat_date = r[1]
    for b in bonds.values():
        if b.initial_face is None and not b.redemptions and b.face_value is not None:
            b.initial_face = b.face_value
        b.refresh()
    return bonds


def load_cbr_rates(conn: Conn) -> dict[tuple[str, date], Decimal]:
    """CBR official rates (RUB per unit) by ``(currency, effective date)``."""
    return {
        (ccy, d): v
        for ccy, d, v in _rows(
            conn, "SELECT ccy, effective_date, rate_per_unit FROM md.fx_official"
        )
    }


def _ai_row(
    bond: BondSchedule,
    r: tuple[Any, ...],
    settle: date,
    fx_rates: dict[tuple[str, date], Decimal],
) -> AiRow:
    """``AiRow`` from ``(secid, board, trade_date, accrued_int, face_value, coupon_value,
    currency_id)``. MOEX reports ACCRUEDINT in the board's settlement currency: for a face in
    another currency it is round(accrued in face currency, 2) x CBR rate of the trade date
    (cross rate via RUB for e.g. an EUR face settled in USD) - converted back here."""
    _, board, trade_date, moex, face, coupon, ccy = r
    ccy = _norm_ccy(ccy)
    unit = _norm_ccy(bond.face_unit)
    if moex is None or ccy is None or unit is None or ccy == unit:
        return AiRow(trade_date, board, settle, moex, face, coupon)
    num = Decimal(1) if unit == "RUB" else fx_rates.get((unit, trade_date))
    den = Decimal(1) if ccy == "RUB" else fx_rates.get((ccy, trade_date))
    if num is None or den is None:
        return AiRow(trade_date, board, settle, moex, face, coupon, skip="fx_rate_missing")
    fx = num / den
    return AiRow(trade_date, board, settle, moex / fx, face, coupon, fx=fx.quantize(_FX_Q))


def _norm_ccy(ccy: str | None) -> str | None:
    return "RUB" if ccy == "SUR" else ccy


_FX_Q: Final = Decimal("0.000001")


def load_board_lags(conn: Conn) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """Settlement lags per (secid, board) and the most common lag per board."""
    per: dict[tuple[str, str], int] = {}
    counts: dict[str, Counter[int]] = {}
    for secid, board, lag in _rows(
        conn,
        "SELECT secid, board, settle_lag_days FROM ref.bond_board"
        " WHERE settle_lag_days IS NOT NULL",
    ):
        per[(secid, board)] = lag
        counts.setdefault(board, Counter())[lag] += 1
    return per, {b: c.most_common(1)[0][0] for b, c in counts.items()}


def _yield_dates(conn: Conn, sample: str, d_from: date | None, d_to: date | None) -> list[date]:
    days = [
        r[0]
        for r in _rows(
            conn,
            "SELECT DISTINCT trade_date FROM md.bond_eod_yield"
            " WHERE (%s::date IS NULL OR trade_date >= %s)"
            " AND (%s::date IS NULL OR trade_date <= %s) ORDER BY 1",
            (d_from, d_from, d_to, d_to),
        )
    ]
    if sample == "all" or not days:
        return days
    if sample == "none":
        return []
    # month_end: the last trade date of every month plus the last five trade dates
    last_of_month: dict[tuple[int, int], date] = {}
    for d in days:
        last_of_month[(d.year, d.month)] = d
    return sorted(set(last_of_month.values()) | set(days[-5:]))


def _stream(conn: Conn, query: str, params: Sequence[Any], name: str) -> Iterator[tuple[Any, ...]]:
    with conn.cursor(name=name, row_factory=tuple_row) as cur:
        cur.itersize = FETCH_SIZE
        cur.execute(query, params)
        yield from cur


# ------------------------------------------------------------------------------ run


def run_verification(
    conn: Conn,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    secids: Sequence[str] | None = None,
    yield_sample: str = "month_end",
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> VerificationRun:
    """Run all checks in one ``REPEATABLE READ READ ONLY`` transaction of ``conn``.

    ``conn`` must be idle (no open transaction); it is rolled back afterwards and its
    ``read_only`` / ``isolation_level`` / ``autocommit`` settings are restored.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("connection has an open transaction")
    saved = (conn.autocommit, conn.read_only, conn.isolation_level)
    conn.autocommit = False
    conn.read_only = True
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    try:
        return _run(conn, date_from, date_to, secids, yield_sample, progress, now)
    finally:
        conn.rollback()
        conn.autocommit, conn.read_only, conn.isolation_level = saved


def _run(
    conn: Conn,
    date_from: date | None,
    date_to: date | None,
    secids: Sequence[str] | None,
    yield_sample: str,
    progress: Callable[[str], None] | None,
    now: datetime | None,
) -> VerificationRun:
    started = time.monotonic()
    say = progress or (lambda _msg: None)
    cal = BusinessCalendar.load(conn)
    lag_per, lag_board = load_board_lags(conn)
    fx_rates = load_cbr_rates(conn)
    bonds = load_bonds(conn, secids)
    say(f"loaded {len(bonds)} bonds")
    results: dict[str, BondResult] = {}

    def result_for(b: BondSchedule) -> BondResult:
        res = results.get(b.secid)
        if res is None:
            res = BondResult(
                b.secid, b.bond_class, b.coupon_type, b.status, b.face_unit, b.benchmark
            )
            results[b.secid] = res
        return res

    def lag(secid: str, board: str) -> int:
        return lag_per.get((secid, board), lag_board.get(board, DEFAULT_SETTLE_LAG))

    flt = ""
    params: list[Any] = [date_from, date_from, date_to, date_to]
    if secids:
        flt = " AND secid = ANY(%s)"
        params.append(list(secids))
    where = (
        " WHERE (%s::date IS NULL OR trade_date >= %s) AND (%s::date IS NULL OR trade_date <= %s)"
        + flt
    )
    eod_rows = 0
    eod_first: date | None = None
    eod_last: date | None = None
    stream = _stream(
        conn,
        "SELECT secid, board, trade_date, accrued_int, face_value, coupon_value, currency_id"
        " FROM md.bond_eod" + where + " ORDER BY secid",
        params,
        "mdhub_verify_eod",
    )
    seen = 0
    for secid, grp in groupby(stream, key=lambda r: r[0]):
        rows = list(grp)
        seen += 1
        eod_rows += len(rows)
        for r in rows:
            eod_first = r[2] if eod_first is None or r[2] < eod_first else eod_first
            eod_last = r[2] if eod_last is None or r[2] > eod_last else eod_last
        bond = bonds.get(secid)
        if bond is None:
            continue
        ai_rows = [_ai_row(bond, r, cal.settle(r[2], lag(secid, r[1])), fx_rates) for r in rows]
        verify_bond_ai(bond, ai_rows, result_for(bond))
        if seen % 500 == 0:
            say(f"accrued interest: {seen} bonds, {eod_rows} rows")
    say(f"accrued interest done: {len(results)} bonds, {eod_rows} rows")

    ydates = _yield_dates(conn, yield_sample, date_from, date_to)
    if ydates:
        fixed = [s for s, b in bonds.items() if b.coupon_type in YIELD_FIXED_TYPES]
        stream = _stream(
            conn,
            "SELECT secid, board, trade_date, yield_date, yield_date_type, price, accrued_int,"
            " effective_yield_pct FROM md.bond_eod_yield"
            " WHERE trade_date = ANY(%s) AND secid = ANY(%s) ORDER BY secid",
            (ydates, fixed),
            "mdhub_verify_yield",
        )
        for secid, grp in groupby(stream, key=lambda r: r[0]):
            bond = bonds[secid]
            yrows = [
                YieldRow(
                    r[2], r[1], cal.settle(r[2], lag(secid, r[1])), r[3], r[4], r[5], r[6], r[7]
                )
                for r in grp
            ]
            verify_bond_yield(bond, yrows, result_for(bond))
        say(f"yields done on {len(ydates)} dates")

    as_of = date_to or eod_last
    for b in bonds.values():
        res = result_for(b)
        res.invariants, res.amount_votes = invariants(b, as_of)
    return VerificationRun(
        generated_at=now or datetime.now(UTC),
        date_from=date_from,
        date_to=date_to,
        eod_first=eod_first,
        eod_last=eod_last,
        yield_dates=ydates,
        yield_sample=yield_sample,
        bonds=sorted(results.values(), key=lambda r: r.secid),
        board_lags=dict(sorted(lag_board.items())),
        eod_rows=eod_rows,
        bonds_in_ref=len(bonds),
        duration_s=time.monotonic() - started,
    )


def iter_failed(run: VerificationRun) -> Iterable[tuple[BondResult, InvariantResult]]:
    for b in run.bonds:
        for inv in b.failed_invariants():
            yield b, inv
