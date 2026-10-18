"""Pure bond arithmetic for static verification (no DB access).

Model (one :class:`BondSchedule` per SECID, built from ``ref.bond``, ``ref.bond_coupon``,
``ref.bond_amortization``, ``ref.bond_offer``):

* **Coupon period** of a coupon = ``[start, pay)`` with ``start`` = its ``start_date`` or, when
  MOEX gives none, the previous coupon's pay date. The period containing a date ``d`` is the one
  with ``start <= d < pay`` (:meth:`BondSchedule.period_at`); on a pay date the next period
  starts (accrued interest 0).
* **Outstanding face** on ``d`` = initial face minus every redemption (amortization or final
  redemption) dated on or before ``d`` (:meth:`BondSchedule.face_at`); the face of a coupon
  period is the outstanding face on its start date. A redemption row without ``value`` uses
  ``value_prc`` of the initial face.
* **Accrued interest conventions** (:data:`CONVENTIONS`, :func:`accrued`), ``t`` = days from
  the period start to ``d``, ``T`` = period days, ``C`` = coupon amount, ``r`` = coupon rate,
  ``F`` = period face, ``f`` = coupons per year:

  ============================  ==========================================================
  ``coupon_pro_rata``           ``C * t / T`` (MOEX formula for OFZ and most RUB bonds)
  ``coupon_freq_365``           ``C * t * f / 365``
  ``rate_act365``               ``F * r * t / 365``
  ``rate_act360``               ``F * r * t / 360``
  ``rate_30_360``               ``F * r * D30E(start, d) / 360``
  ``rate_actact``               ``F * r * sum(days in year y / days of year y)`` (ACT/ACT ISDA)
  ============================  ==========================================================

  Values are compared unrounded with tolerance :data:`AI_TOL` (MOEX publishes two decimals).
* **Effective yield** (:func:`effective_yield`): MOEX "доходность" - annual compounding on
  ACT/365 from the settlement date: ``sum(CF_i / (1 + y) ** ((t_i - settle) / 365)) =
  price / 100 * F(settle) + AI``.

Floats are used only inside the yield solver; schedule amounts stay :class:`Decimal`.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

__all__ = [
    "AI_TOL",
    "CONVENTIONS",
    "YIELD_TOL_PCT",
    "BondSchedule",
    "Coupon",
    "InvariantResult",
    "Offer",
    "Period",
    "Redemption",
    "accrued",
    "add_business_days",
    "check_amortization_sum",
    "check_coupon_amounts",
    "check_coupon_dates",
    "check_fixed_coupons_known",
    "check_floater_formula",
    "check_perpetual_label",
    "check_redemption_at_maturity",
    "coupon_amount_conventions",
    "days_30e_360",
    "effective_yield",
    "invariants",
    "per_day",
    "yield_cash_flows",
]

AI_TOL: Final = Decimal("0.01")
"""Accrued interest match tolerance in face currency (MOEX ACCRUEDINT has two decimals)."""

YIELD_TOL_PCT: Final = 0.01
"""Yield match tolerance in percentage points (1 bp; MOEX publishes 2-4 decimals)."""

CONVENTIONS: Final = (
    "coupon_pro_rata",
    "coupon_freq_365",
    "rate_act365",
    "rate_act360",
    "rate_30_360",
    "rate_actact",
)
"""Candidate accrued-interest conventions, in tie-break order (module docstring)."""

AMOUNT_CONVENTIONS: Final = (
    "rate_act365",
    "rate_actact",
    "rate_30_360",
    "rate_act360",
    "rate_freq",
)
"""Candidate conventions for ``coupon amount = f(rate, face, period)``: the ``rate_*`` accrual
conventions over the whole period plus ``rate_freq`` = ``F * r / f`` (equal periods)."""

PERPETUAL_SUBTYPE: Final = "Бессрочные"
"""MOEX BOND_SUBTYPE of perpetual bonds (mirrors ``mdhub.sources.moex_static``)."""

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)
_RATE_ROUNDING: Final = Decimal("0.00005")
"""Half a unit of the second decimal of a percent rate (MOEX ``valueprc`` precision)."""


# ------------------------------------------------------------------------------- model


@dataclass(frozen=True, slots=True)
class Coupon:
    """One ``ref.bond_coupon`` row (``rate_pct`` = MOEX ``valueprc``)."""

    no: int
    start: date | None
    pay: date
    amount: Decimal | None
    rate_pct: Decimal | None
    status: str = "known"
    record: date | None = None


@dataclass(frozen=True, slots=True)
class Redemption:
    """One ``ref.bond_amortization`` row (``kind``: MOEX data_source)."""

    day: date
    value: Decimal | None
    value_prc: Decimal | None
    kind: str = "amortization"


@dataclass(frozen=True, slots=True)
class Offer:
    """One ``ref.bond_offer`` row with a date."""

    day: date
    offer_type: str
    status: str
    price_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Period:
    """A coupon period ``[start, end)`` with its coupon and outstanding face."""

    coupon: Coupon
    start: date
    end: date
    face: Decimal | None

    @property
    def days(self) -> int:
        return (self.end - self.start).days


@dataclass(slots=True)
class BondSchedule:
    """Static data of one bond as the verification sees it."""

    secid: str
    bond_class: str = "unknown"
    coupon_type: str = "fixed"
    status: str = "active"
    initial_face: Decimal | None = None
    face_value: Decimal | None = None
    face_unit: str | None = None
    maturity: date | None = None
    is_perpetual: bool = False
    is_face_indexed: bool = False
    coupon_frequency: int | None = None
    bond_subtype: str | None = None
    formula_status: str | None = None
    benchmark: str | None = None
    """Benchmark of the resolved rate formula (KEY_RATE, RUONIA, ...), floaters only."""
    """``ref.bond_rate_formula_resolved.status`` when the resolved row has a benchmark, else
    ``missing`` (row without benchmark) or ``None`` (no row)."""
    eod_mat_date: date | None = None
    """Latest non-NULL MATDATE MOEX published in ``md.bond_eod`` (``None`` if never)."""
    coupons: list[Coupon] = field(default_factory=list)
    redemptions: list[Redemption] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    _periods: list[Period] = field(default_factory=list, init=False, repr=False)
    _ends: list[date] = field(default_factory=list, init=False, repr=False)
    _red_days: list[date] = field(default_factory=list, init=False, repr=False)
    _red_cum: list[Decimal] = field(default_factory=list, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)

    # -- derived structures (built lazily; call refresh() after mutating the lists)
    def refresh(self) -> None:
        self.coupons.sort(key=lambda c: (c.pay, c.no))
        self.redemptions.sort(key=lambda r: r.day)
        self._red_days = []
        self._red_cum = []
        total = _ZERO
        for r in self.redemptions:
            v = self.redemption_value(r)
            total += v if v is not None else _ZERO
            self._red_days.append(r.day)
            self._red_cum.append(total)
        self._ready = True  # face_at() is needed below
        self._periods = []
        prev_pay: date | None = None
        for c in self.coupons:
            start = c.start or prev_pay
            prev_pay = c.pay
            if start is None or start >= c.pay:
                continue
            self._periods.append(Period(c, start, c.pay, self.face_at(start)))
        self._ends = [p.end for p in self._periods]

    def _ensure(self) -> None:
        if not self._ready:
            self.refresh()

    @property
    def periods(self) -> list[Period]:
        self._ensure()
        return self._periods

    def redemption_value(self, r: Redemption) -> Decimal | None:
        if r.value is not None:
            return r.value
        if r.value_prc is not None and self.initial_face is not None:
            return self.initial_face * r.value_prc / _HUNDRED
        return None

    def redeemed_through(self, d: date) -> Decimal:
        """Sum of redemptions dated on or before ``d``."""
        self._ensure()
        i = bisect_right(self._red_days, d)
        return self._red_cum[i - 1] if i else _ZERO

    def face_at(self, d: date) -> Decimal | None:
        """Outstanding face after the redemptions dated on or before ``d``."""
        if self.initial_face is None:
            return None
        return self.initial_face - self.redeemed_through(d)

    def period_at(self, d: date) -> Period | None:
        """The coupon period with ``start <= d < end`` (``None`` outside the schedule)."""
        self._ensure()
        i = bisect_right(self._ends, d)  # first period ending after d
        if i < len(self._periods) and self._periods[i].start <= d:
            return self._periods[i]
        return None

    def outstanding_after_schedule(self) -> Decimal | None:
        """Initial face minus all scheduled redemptions (0 for a complete schedule)."""
        if self.initial_face is None:
            return None
        self._ensure()
        return self.initial_face - (self._red_cum[-1] if self._red_cum else _ZERO)


# -------------------------------------------------------------------- day counting


def days_30e_360(start: date, end: date) -> int:
    """30E/360 day count (Eurobond basis)."""
    d1 = min(start.day, 30)
    d2 = min(end.day, 30)
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def _actact_fraction(start: date, end: date) -> Decimal:
    """ACT/ACT ISDA year fraction."""
    frac = _ZERO
    cur = start
    while cur < end:
        year_end = date(cur.year + 1, 1, 1)
        seg_end = min(end, year_end)
        year_days = 366 if _is_leap(cur.year) else 365
        frac += Decimal((seg_end - cur).days) / year_days
        cur = seg_end
    return frac


def _is_leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def accrued(period: Period, d: date, convention: str, *, frequency: int | None) -> Decimal | None:
    """Accrued interest on ``d`` (inside ``period``) under ``convention``; ``None`` when the
    inputs it needs are missing."""
    t = (d - period.start).days
    c = period.coupon
    if convention == "coupon_pro_rata":
        if c.amount is None or period.days <= 0:
            return None
        return c.amount * t / period.days
    if convention == "coupon_freq_365":
        if c.amount is None or not frequency:
            return None
        return c.amount * t * frequency / 365
    if c.rate_pct is None or period.face is None:
        return None
    base = period.face * c.rate_pct / _HUNDRED
    if convention == "rate_act365":
        return base * t / 365
    if convention == "rate_act360":
        return base * t / 360
    if convention == "rate_30_360":
        return base * days_30e_360(period.start, d) / 360
    if convention == "rate_actact":
        return base * _actact_fraction(period.start, d)
    raise ValueError(f"unknown convention {convention!r}")


def per_day(period: Period, convention: str, *, frequency: int | None) -> Decimal | None:
    """Accrual of the first day of ``period`` under ``convention`` (for implied-day checks)."""
    if period.days <= 0:
        return None
    nxt = date.fromordinal(period.start.toordinal() + 1)
    return accrued(period, nxt, convention, frequency=frequency)


def add_business_days(d: date, n: int, is_business_day: Callable[[date], bool]) -> date:
    """``d`` plus ``n`` business days (``n >= 0``)."""
    cur = d
    left = n
    while left > 0:
        cur = date.fromordinal(cur.toordinal() + 1)
        if is_business_day(cur):
            left -= 1
    return cur


# --------------------------------------------------------------------------- yield


def yield_cash_flows(
    bond: BondSchedule,
    settle: date,
    horizon: date,
    *,
    redemption_price_pct: Decimal = _HUNDRED,
) -> tuple[list[tuple[int, Decimal]], str | None]:
    """Cash flows after ``settle`` up to ``horizon`` as ``(days from settle, amount)``: coupons,
    redemptions and the outstanding face at ``horizon`` (offer or maturity) at
    ``redemption_price_pct``. Returns ``([], reason)`` when a coupon amount is unknown or the
    face is unknown."""
    flows: list[tuple[int, Decimal]] = []
    for c in bond.coupons:
        if settle < c.pay <= horizon:
            if c.amount is None:
                return [], "future_coupon_unknown"
            flows.append(((c.pay - settle).days, c.amount))
    for r in bond.redemptions:
        if settle < r.day <= horizon:
            v = bond.redemption_value(r)
            if v is None:
                return [], "redemption_value_unknown"
            flows.append(((r.day - settle).days, v))
    rest = bond.face_at(horizon)
    if rest is None:
        return [], "face_unknown"
    if rest > 0:
        flows.append(((horizon - settle).days, rest * redemption_price_pct / _HUNDRED))
    return flows, None


def effective_yield(flows: Sequence[tuple[int, Decimal]], dirty: Decimal) -> float | None:
    """Annual effective yield in percent solving ``sum(cf / (1 + y) ** (t / 365)) = dirty``;
    ``None`` without positive flows or when the solver does not converge."""
    cfs = [(t / 365.0, float(v)) for t, v in flows if t > 0]
    target = float(dirty)
    if not cfs or target <= 0:
        return None

    def f(y: float) -> float:
        lb = math.log1p(y)
        return sum(v * math.exp(-t * lb) for t, v in cfs) - target

    lo, hi = -0.99, 1.0
    f_lo, f_hi = f(lo), f(hi)
    while f_hi > 0 and hi < 1e4:
        hi *= 4.0
        f_hi = f(hi)
    if f_lo < 0 or f_hi > 0:
        return None
    # Newton from 10 % with a bisection fallback (f is decreasing in y)
    y = 0.1
    for _ in range(60):
        b = 1.0 + y
        lb = math.log(b)
        val = 0.0
        der = 0.0
        for t, v in cfs:
            disc = v * math.exp(-t * lb)
            val += disc
            der -= t * disc / b
        val -= target
        if abs(val) < 1e-10 * max(1.0, target):
            return y * 100.0
        if val > 0:
            lo = max(lo, y)
        else:
            hi = min(hi, y)
        step = y - val / der if der != 0 else math.nan
        y = step if lo < step < hi else (lo + hi) / 2.0
        if hi - lo < 1e-12:
            return y * 100.0
    return y * 100.0


# ---------------------------------------------------------------------- invariants


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """Outcome of one invariant for one bond: ``ok`` ``True`` / ``False``, ``None`` = not
    applicable (``reason`` says why)."""

    check: str
    ok: bool | None
    reason: str = ""
    detail: str = ""


def _has_schedule(bond: BondSchedule) -> bool:
    return bool(bond.coupons or bond.redemptions)


def check_amortization_sum(bond: BondSchedule) -> InvariantResult:
    """Redemptions (amortizations + final redemption) sum to the initial face."""
    name = "amortization_sum"
    if bond.is_perpetual:
        return InvariantResult(name, None, "perpetual")
    if bond.is_face_indexed:
        return InvariantResult(name, None, "indexed_face")
    if not _has_schedule(bond):
        return InvariantResult(name, None, "no_schedule")
    if bond.initial_face is None:
        return InvariantResult(name, None, "initial_face_unknown")
    if not bond.redemptions:
        return InvariantResult(name, False, "no_redemption_rows")
    if any(bond.redemption_value(r) is None for r in bond.redemptions):
        return InvariantResult(name, False, "redemption_value_unknown")
    rest = bond.outstanding_after_schedule()
    assert rest is not None
    total = bond.initial_face - rest
    tol = Decimal("0.01") + Decimal("0.005") * len(bond.redemptions)
    if abs(rest) <= tol:
        return InvariantResult(name, True)
    pct = total / bond.initial_face * _HUNDRED if bond.initial_face else _ZERO
    detail = f"sum={_fmt(total)} of {_fmt(bond.initial_face)} ({pct:.2f}%)"
    return InvariantResult(name, False, "sum_below_face" if rest > 0 else "sum_above_face", detail)


def check_redemption_at_maturity(bond: BondSchedule) -> InvariantResult:
    """The last redemption is dated on the maturity date."""
    name = "redemption_at_maturity"
    if bond.is_perpetual or bond.maturity is None:
        return InvariantResult(name, None, "no_maturity")
    if not bond.redemptions:
        return InvariantResult(name, None, "no_redemption_rows")
    last = bond.redemptions[-1].day
    if last == bond.maturity:
        return InvariantResult(name, True)
    reason = (
        "last_redemption_before_maturity" if last < bond.maturity else "redemption_after_maturity"
    )
    return InvariantResult(name, False, reason, f"last={last} maturity={bond.maturity}")


def check_coupon_dates(bond: BondSchedule) -> InvariantResult:
    """Coupon pay dates strictly increase, every start precedes its pay date and periods
    neither overlap nor leave gaps; record dates do not follow pay dates."""
    name = "coupon_dates"
    if not bond.coupons:
        return InvariantResult(name, None, "no_coupons")
    problems: list[tuple[str, int]] = []
    prev: Coupon | None = None
    for c in sorted(bond.coupons, key=lambda c: c.no):
        if c.start is not None and c.start >= c.pay:
            problems.append(("start_not_before_pay", c.no))
        if c.record is not None and c.record > c.pay:
            problems.append(("record_after_pay", c.no))
        if prev is not None:
            if c.pay <= prev.pay:
                problems.append(("pay_not_increasing", c.no))
            elif c.start is not None and c.start < prev.pay:
                problems.append(("overlap", c.no))
            elif c.start is not None and c.start > prev.pay:
                problems.append(("gap", c.no))
        prev = c
    if not problems:
        return InvariantResult(name, True)
    kinds = sorted({k for k, _ in problems}, key=_PROBLEM_ORDER.index)
    detail = ", ".join(f"{k}@{no}" for k, no in problems[:5])
    if len(problems) > 5:
        detail += f" (+{len(problems) - 5})"
    return InvariantResult(name, False, kinds[0], detail)


_PROBLEM_ORDER: Final = (
    "pay_not_increasing",
    "start_not_before_pay",
    "overlap",
    "gap",
    "record_after_pay",
)


def coupon_amount_conventions(
    period: Period, *, frequency: int | None
) -> tuple[dict[str, Decimal], Decimal] | None:
    """Coupon amount implied by each of :data:`AMOUNT_CONVENTIONS` for ``period`` and the
    tolerance (amount rounding + rate rounding); ``None`` without rate, amount or face."""
    c = period.coupon
    if c.amount is None or c.rate_pct is None or period.face is None or period.days <= 0:
        return None
    base = period.face * c.rate_pct / _HUNDRED
    out = {
        "rate_act365": base * period.days / 365,
        "rate_actact": base * _actact_fraction(period.start, period.end),
        "rate_30_360": base * days_30e_360(period.start, period.end) / 360,
        "rate_act360": base * period.days / 360,
    }
    if frequency:
        out["rate_freq"] = base / frequency
    tol = AI_TOL + period.face * _RATE_ROUNDING * max(period.days, 1) / 365
    return out, tol


def check_coupon_amounts(bond: BondSchedule) -> tuple[InvariantResult, dict[str, int]]:
    """``amount ~= rate * face * days / 365`` for every coupon with a published rate and amount
    (face = schedule face on the period start). Also returns how many coupons each
    :data:`AMOUNT_CONVENTIONS` member explains (convention inference)."""
    name = "coupon_amount_vs_rate"
    votes: dict[str, int] = {}
    if bond.is_face_indexed:
        return InvariantResult(name, None, "indexed_face"), votes
    checked = 0
    bad: list[int] = []
    alt_only: dict[str, int] = {}
    for p in bond.periods:
        c = p.coupon
        if c.amount is None or c.rate_pct is None or c.amount <= 0 or c.rate_pct <= 0:
            continue
        res = coupon_amount_conventions(p, frequency=bond.coupon_frequency)
        if res is None:
            continue
        implied, tol = res
        checked += 1
        matched = [
            k for k in AMOUNT_CONVENTIONS if k in implied and abs(implied[k] - c.amount) <= tol
        ]
        for k in matched:
            votes[k] = votes.get(k, 0) + 1
        if "rate_act365" not in matched:
            bad.append(c.no)
            if matched:
                alt_only[matched[0]] = alt_only.get(matched[0], 0) + 1
    if checked == 0:
        reason = "face_unknown" if bond.initial_face is None else "no_rate_and_amount"
        return InvariantResult(name, None, reason), votes
    if not bad:
        return InvariantResult(name, True), votes
    if alt_only and sum(alt_only.values()) == len(bad):
        best = max(alt_only, key=lambda k: alt_only[k])
        reason = f"matches_{best}"
    else:
        reason = "amount_not_rate_based"
    detail = f"{len(bad)}/{checked} coupons off ACT/365, e.g. #{bad[0]}"
    return InvariantResult(name, False, reason, detail), votes


def check_fixed_coupons_known(bond: BondSchedule, as_of: date | None) -> InvariantResult:
    """An active fixed-coupon bond has an amount or a rate for every coupon paid after
    ``as_of`` (a fixed coupon is known in advance)."""
    name = "fixed_coupons_known"
    if bond.coupon_type != "fixed":
        return InvariantResult(name, None, "not_fixed")
    if as_of is None or bond.status != "active":
        return InvariantResult(name, None, "not_active")
    future = [c for c in bond.coupons if c.pay > as_of]
    if not future:
        return InvariantResult(name, None, "no_future_coupons")
    unknown = [c.no for c in future if c.amount is None and c.rate_pct is None]
    if not unknown:
        return InvariantResult(name, True)
    return InvariantResult(
        name,
        False,
        "future_coupon_unknown",
        f"{len(unknown)}/{len(future)} future coupons without amount or rate, e.g. #{unknown[0]}",
    )


def check_floater_formula(bond: BondSchedule) -> InvariantResult:
    """Every floater has a resolved rate formula with a benchmark."""
    name = "floater_formula"
    if bond.coupon_type != "floating":
        return InvariantResult(name, None, "not_floating")
    if bond.formula_status is None:
        return InvariantResult(name, False, "no_formula_row")
    if bond.formula_status == "missing":
        return InvariantResult(name, False, "formula_without_benchmark")
    return InvariantResult(name, True, "", bond.formula_status)


def check_perpetual_label(bond: BondSchedule) -> InvariantResult:
    """Perpetuals are labelled: MOEX subtype 'Бессрочные' => ``is_perpetual``; perpetuals have no
    maturity (in ``ref.bond`` nor in MOEX EOD); a bond with a schedule but no maturity is
    labelled perpetual; ``ref.bond.maturity_date`` equals the latest EOD MATDATE."""
    name = "perpetual_label"
    if bond.bond_subtype == PERPETUAL_SUBTYPE and not bond.is_perpetual:
        return InvariantResult(name, False, "perpetual_subtype_not_labelled")
    if bond.is_perpetual:
        if bond.maturity is not None:
            return InvariantResult(name, False, "perpetual_with_maturity", str(bond.maturity))
        if bond.eod_mat_date is not None:
            return InvariantResult(
                name, False, "perpetual_but_eod_matdate", f"eod MATDATE={bond.eod_mat_date}"
            )
        return InvariantResult(name, True)
    if bond.maturity is None:
        if bond.eod_mat_date is not None:
            return InvariantResult(
                name, False, "maturity_missing_eod_has_it", f"eod MATDATE={bond.eod_mat_date}"
            )
        if _has_schedule(bond):
            return InvariantResult(name, False, "no_maturity_not_perpetual")
        return InvariantResult(name, None, "no_schedule")
    if bond.eod_mat_date is not None and bond.eod_mat_date != bond.maturity:
        return InvariantResult(
            name, False, "maturity_differs_eod", f"ref={bond.maturity} eod={bond.eod_mat_date}"
        )
    return InvariantResult(name, True)


def invariants(
    bond: BondSchedule, as_of: date | None = None
) -> tuple[list[InvariantResult], dict[str, int]]:
    """All static invariants of ``bond`` plus the coupon-amount convention votes (``as_of``:
    the date that separates paid from future coupons)."""
    amounts, votes = check_coupon_amounts(bond)
    return [
        check_amortization_sum(bond),
        check_redemption_at_maturity(bond),
        check_coupon_dates(bond),
        amounts,
        check_fixed_coupons_known(bond, as_of),
        check_floater_formula(bond),
        check_perpetual_label(bond),
    ], votes


def _fmt(v: Decimal) -> str:
    return f"{v.normalize():f}"
