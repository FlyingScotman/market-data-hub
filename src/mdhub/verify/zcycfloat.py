"""Coupons of floaters on the MOEX KBD (benchmark ``ZCYC``): pure arithmetic, no DB access.

Formula (contract market_snapshot/1.4, ``FloaterFormula``)::

    rate % = round_half_up(Y(T; d_fix), rate_rounding_decimals) + spread_bp / 100

* ``Y(T; d)`` - MOEX effective annual zero yield (%) at the **fixed** tenor ``T`` years from the
  KBD parameters of the end-of-day publication of day ``d``
  (:func:`mdhub.sources.moex_market.kbd_yield_pct`, ``(exp(G/1e4) - 1) * 100``);
* ``d_fix`` - :func:`fixing_date`: the ``N``-th day of the unit before the start of the coupon
  period (``fixing_anchor = accrual_start``); unit ``zcyc_publication_days`` = days with an
  end-of-day KBD publication. ``N = 0`` is the anchor itself when it is such a day, else the
  last one before it;
* ``spread_bp`` - the margin of the period: the last step of the margin schedule whose
  ``effective_from`` is on or before the period start.

:func:`fit` recovers ``T`` and ``N`` from the published coupon rates (``ref.bond_coupon.value_prc``
of coupons whose rate is a fact): for every candidate ``(T, N)`` the implied margin of each
coupon is ``rate - round(Y)``. The margin may change only at a put offer (a reset), so the
coupons are split into segments at the put-offer dates; the segment of the latest coupon must
carry the current margin (MOEX ``spread_bp``), every earlier segment the margin most of its
coupons imply (at least :data:`MIN_SEGMENT_SUPPORT` of them). A coupon matches when the
formula gives its rate within :data:`RATE_TOL_PCT`. The best candidate is kept only when it is
unique, matches the latest coupon, and reproduces at least :data:`MIN_MATCH_SHARE` of at least
:data:`MIN_PERIODS` coupons after the first (the first coupon is often set at placement; it is
still reported); otherwise nothing is published and the statistics say why. The resets are the
dated offers of the bond (put, call or unknown: a margin is often revised at an issuer call or
a put offer).
:func:`reproduce` checks given terms (e.g. curated from an issue document) the same way.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from mdhub.sources.moex_market import kbd_yield_pct

__all__ = [
    "MIN_MATCH_SHARE",
    "MIN_PERIODS",
    "MIN_SEGMENT_SUPPORT",
    "OFFSETS",
    "RATE_TOL_PCT",
    "TENORS",
    "CouponObs",
    "FitResult",
    "KbdHistory",
    "Reproduction",
    "SpreadStep",
    "ZcycTerms",
    "check_terms",
    "expected_rate",
    "fit",
    "fixing_date",
    "implied_schedule",
    "reproduce",
    "spread_at",
]

RATE_TOL_PCT: Final = Decimal("0.005")
"""A published rate matches the formula within half a hundredth of a percent."""
TENORS: Final = tuple(Decimal(t) for t in ("0.25", "0.5", "0.75", *map(str, range(1, 16))))
OFFSETS: Final = tuple(range(16))
MIN_PERIODS: Final = 6
MIN_MATCH_SHARE: Final = Decimal("0.8")
MIN_SEGMENT_SUPPORT: Final = 2
UNITS: Final = frozenset({"zcyc_publication_days", "business_days", "calendar_days"})
CENT: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class CouponObs:
    """A coupon whose rate is a fact (paid, or fixed and published)."""

    coupon_no: int
    start_date: date
    rate_pct: Decimal


@dataclass(frozen=True, slots=True)
class SpreadStep:
    effective_from: date
    spread_bp: Decimal
    basis: str  # document | coupon_history_fit
    evidence_document_url: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "effective_from": self.effective_from.isoformat(),
            "spread_bp": str(self.spread_bp),
            "basis": self.basis,
            "evidence_document_url": self.evidence_document_url,
        }

    @classmethod
    def from_json(cls, v: Mapping[str, Any]) -> SpreadStep:
        return cls(
            effective_from=date.fromisoformat(str(v["effective_from"])),
            spread_bp=Decimal(str(v["spread_bp"])),
            basis=str(v.get("basis") or "document"),
            evidence_document_url=v.get("evidence_document_url"),
        )


@dataclass(frozen=True, slots=True)
class ZcycTerms:
    tenor_years: Decimal
    fixing_offset: int
    fixing_offset_unit: str = "zcyc_publication_days"
    fixing_anchor: str = "accrual_start"
    rate_rounding_decimals: int = 2


class KbdHistory:
    """End-of-day KBD publications: parameters per day, sorted publication days."""

    def __init__(
        self,
        params: Mapping[date, Mapping[str, float]],
        business_day: Callable[[date], bool] | None = None,
    ) -> None:
        self.params = dict(params)
        self.days: tuple[date, ...] = tuple(sorted(self.params))
        self.business_day = business_day
        self._cache: dict[tuple[date, Decimal, int], Decimal] = {}

    def yield_pct(self, day: date, tenor: Decimal, decimals: int) -> Decimal:
        key = (day, tenor, decimals)
        y = self._cache.get(key)
        if y is None:
            raw = Decimal(repr(kbd_yield_pct(self.params[day], float(tenor))))
            y = raw.quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP)
            self._cache[key] = y
        return y


def _last_on_or_before(days: Sequence[date], d: date) -> date | None:
    i = bisect_right(days, d)
    return days[i - 1] if i else None


def fixing_date(h: KbdHistory, start: date, offset: int, unit: str) -> date | None:
    """Day whose KBD fixes the coupon of a period starting on ``start``; ``None`` when the
    history does not reach it (a day before the first publication, or no publication on the
    rule's day)."""
    days = h.days
    if unit == "zcyc_publication_days":
        i = bisect_left(days, start)  # first publication day >= start
        if offset == 0:
            return start if i < len(days) and days[i] == start else _last_on_or_before(days, start)
        j = i - offset
        return days[j] if j >= 0 else None
    if unit == "business_days":
        is_bd = h.business_day or (lambda d: d in h.params)
        d, n = start, offset
        if n == 0:
            while not is_bd(d):
                d -= timedelta(days=1)
        while n > 0:
            d -= timedelta(days=1)
            if is_bd(d):
                n -= 1
        return d if d in h.params else None
    if unit == "calendar_days":
        d = start - timedelta(days=offset)
        return _last_on_or_before(days, d) if days and d >= days[0] else None
    raise ValueError(f"unknown fixing_offset_unit {unit!r}")


def spread_at(schedule: Sequence[SpreadStep], start: date) -> Decimal | None:
    steps = [s for s in schedule if s.effective_from <= start]
    return max(steps, key=lambda s: s.effective_from).spread_bp if steps else None


def expected_rate(
    h: KbdHistory, terms: ZcycTerms, start: date, spread_bp: Decimal
) -> Decimal | None:
    d = fixing_date(h, start, terms.fixing_offset, terms.fixing_offset_unit)
    if d is None:
        return None
    return h.yield_pct(d, terms.tenor_years, terms.rate_rounding_decimals) + spread_bp / 100


@dataclass(slots=True)
class Reproduction:
    method: str  # fit | check
    terms: ZcycTerms | None
    periods_checked: int = 0
    periods_matched: int = 0
    max_abs_error_pct: Decimal | None = None
    checked_through: date | None = None
    unmatched: list[tuple[int, date, Decimal, Decimal | None]] = field(default_factory=list)
    accepted: bool = False
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        t = self.terms
        return {
            "method": self.method,
            "tenor_years": str(t.tenor_years) if t else None,
            "fixing_offset": t.fixing_offset if t else None,
            "fixing_offset_unit": t.fixing_offset_unit if t else None,
            "rate_rounding_decimals": t.rate_rounding_decimals if t else None,
            "periods_checked": self.periods_checked,
            "periods_matched": self.periods_matched,
            "tolerance_pct": str(RATE_TOL_PCT),
            "max_abs_error_pct": None if self.max_abs_error_pct is None
            else str(self.max_abs_error_pct),
            "checked_through": self.checked_through.isoformat() if self.checked_through else None,
            "unmatched_coupon_nos": [u[0] for u in self.unmatched],
            "unmatched": [
                {
                    "coupon_no": n,
                    "start_date": s.isoformat(),
                    "published_pct": str(p),
                    "formula_pct": None if e is None else str(e),
                }
                for n, s, p, e in self.unmatched
            ],
            "accepted": self.accepted,
            "reason": self.reason,
        }  # fmt: skip


def _usable(h: KbdHistory, coupons: Sequence[CouponObs], terms: ZcycTerms) -> list[CouponObs]:
    return [
        c
        for c in sorted(coupons, key=lambda c: c.start_date)
        if fixing_date(h, c.start_date, terms.fixing_offset, terms.fixing_offset_unit) is not None
    ]


def reproduce(
    h: KbdHistory,
    coupons: Sequence[CouponObs],
    terms: ZcycTerms,
    schedule: Sequence[SpreadStep],
    method: str = "check",
) -> Reproduction:
    """Compare the published rates with the formula under ``terms`` and ``schedule``."""
    rep = Reproduction(method=method, terms=terms)
    for c in _usable(h, coupons, terms):
        spread = spread_at(schedule, c.start_date)
        e = None if spread is None else expected_rate(h, terms, c.start_date, spread)
        rep.periods_checked += 1
        rep.checked_through = c.start_date
        if e is not None:
            err = abs(e - c.rate_pct)
            rep.max_abs_error_pct = (
                err if rep.max_abs_error_pct is None else max(rep.max_abs_error_pct, err)
            )
            if err <= RATE_TOL_PCT:
                rep.periods_matched += 1
                continue
        rep.unmatched.append((c.coupon_no, c.start_date, c.rate_pct, e))
    return rep


@dataclass(slots=True)
class FitResult:
    reproduction: Reproduction
    schedule: list[SpreadStep]
    candidates: list[dict[str, Any]]

    @property
    def accepted(self) -> bool:
        return self.reproduction.accepted


def _segments(coupons: Sequence[CouponObs], resets: Sequence[date]) -> list[list[CouponObs]]:
    resets = sorted(set(resets))
    out: dict[int, list[CouponObs]] = {}
    for c in coupons:
        out.setdefault(bisect_right(resets, c.start_date), []).append(c)
    return [out[k] for k in sorted(out)]


def _fit_schedule(
    h: KbdHistory,
    coupons: Sequence[CouponObs],
    terms: ZcycTerms,
    resets: Sequence[date],
    current_spread_bp: Decimal | None,
) -> list[SpreadStep]:
    """Margin schedule implied under ``terms``: one step per segment that has a margin."""
    steps: list[SpreadStep] = []
    segs = _segments(coupons, resets)
    for k, seg in enumerate(segs):
        implied = []
        for c in seg:
            e = expected_rate(h, terms, c.start_date, Decimal(0))
            if e is not None:
                implied.append((c.rate_pct - e).quantize(CENT, ROUND_HALF_UP))
        margin: Decimal | None = None
        if k == len(segs) - 1 and current_spread_bp is not None:
            margin = current_spread_bp / 100
        elif implied:
            top, n = Counter(implied).most_common(1)[0]
            if n >= MIN_SEGMENT_SUPPORT:
                margin = top
        if margin is not None and (not steps or steps[-1].spread_bp != margin * 100):
            steps.append(
                SpreadStep(
                    effective_from=seg[0].start_date,
                    spread_bp=Decimal(f"{(margin * 100).normalize():f}"),
                    basis="coupon_history_fit",
                )
            )
    return steps


def fit(
    h: KbdHistory,
    coupons: Sequence[CouponObs],
    resets: Sequence[date],
    current_spread_bp: Decimal | None,
    tenors: Sequence[Decimal] = TENORS,
    offsets: Sequence[int] = OFFSETS,
) -> FitResult:
    """Recover ``(T, N)`` and the margin schedule from the published rates (module doc)."""
    scored: list[tuple[tuple[int, int, int], ZcycTerms, Reproduction, list[SpreadStep]]] = []
    for t in tenors:
        for n in offsets:
            terms = ZcycTerms(tenor_years=t, fixing_offset=n)
            usable = _usable(h, coupons, terms)
            if not usable:
                continue
            schedule = _fit_schedule(h, usable, terms, resets, current_spread_bp)
            rep = reproduce(h, usable, terms, schedule, method="fit")
            last_ok = not rep.unmatched or rep.unmatched[-1][1] != rep.checked_through
            # more matches first; on a tie the latest coupon matched, then the smaller offset
            scored.append(((rep.periods_matched, int(last_ok), -n), terms, rep, schedule))
    if not scored:
        rep = Reproduction(method="fit", terms=None, reason="no coupon with a fixing in history")
        return FitResult(rep, [], [])
    scored.sort(key=lambda s: s[0], reverse=True)
    candidates = [
        {
            "tenor_years": str(s[1].tenor_years),
            "fixing_offset": s[1].fixing_offset,
            "periods_matched": s[2].periods_matched,
            "periods_checked": s[2].periods_checked,
        }
        for s in scored[:5]
    ]
    _, terms, rep, schedule = scored[0]
    ties = [s for s in scored[1:] if s[0][:2] == scored[0][0][:2]]
    _judge(rep, _usable(h, coupons, terms), "best candidate")
    if rep.accepted and ties:
        rep.accepted = False
        rep.reason = (
            "ambiguous: "
            + ", ".join(f"T={s[1].tenor_years} N={s[1].fixing_offset}" for s in ties[:3])
            + " reproduce as many coupons"
        )
    return FitResult(rep, schedule if rep.accepted else [], candidates)


def _judge(rep: Reproduction, usable: Sequence[CouponObs], subject: str) -> None:
    """Accept ``rep`` over the coupons it checked (``usable``; module doc). The first coupon
    is often set at placement (book-building), not by the formula: it is reported but does not
    count towards the acceptance share."""
    later = [c for c in usable if c.coupon_no > 1]
    later_unmatched = sum(1 for u in rep.unmatched if u[0] > 1)
    later_matched = len(later) - later_unmatched
    last_unmatched = bool(rep.unmatched) and rep.unmatched[-1][1] == rep.checked_through
    rep.accepted = False
    if len(later) < MIN_PERIODS:
        rep.reason = f"fewer than {MIN_PERIODS} coupons after the first with a fixing in history"
    elif Decimal(later_matched) < MIN_MATCH_SHARE * len(later):
        rep.reason = (
            f"{subject} reproduces {rep.periods_matched} of {rep.periods_checked} coupons "
            f"({later_matched} of {len(later)} after the first)"
        )
    elif last_unmatched:
        rep.reason = f"{subject} does not reproduce the latest coupon"
    else:
        rep.accepted = True
        rep.reason = None


def check_terms(
    h: KbdHistory,
    coupons: Sequence[CouponObs],
    terms: ZcycTerms,
    schedule: Sequence[SpreadStep],
) -> Reproduction:
    """Reproduce given (e.g. documented) terms and judge them by the fit's acceptance rule."""
    usable = _usable(h, coupons, terms)
    rep = reproduce(h, usable, terms, schedule, method="check")
    _judge(rep, usable, "documented formula")
    return rep


def implied_schedule(
    h: KbdHistory,
    coupons: Sequence[CouponObs],
    terms: ZcycTerms,
    resets: Sequence[date],
    current_spread_bp: Decimal | None,
) -> list[SpreadStep]:
    """Margin schedule the published rates imply under given terms (see :func:`fit`)."""
    return _fit_schedule(h, _usable(h, coupons, terms), terms, resets, current_spread_bp)
