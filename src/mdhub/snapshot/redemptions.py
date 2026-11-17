"""Redemption schedule of a bond: MOEX rows, manual overrides, consistency labels (#165).

MOEX ISS publishes the principal schedule (``ref.bond_amortization``) rounded: ``valueprc`` in
whole percent and ``value`` in steps of 0.01. For a bond with a low face (RUS-30: face 0.035,
USD) every future redemption then reads 0.01 although 0.005 is paid, and MOEX computes its yields
and durations from that schedule. This module holds everything the static snapshot and the TDM
export share about it:

* the **consistency labels** between face and schedule (HS13): ``face_value_not_in_schedule``,
  ``future_redemptions_differ_from_face``, and the label ``redemptions_rounded_by_source``
  (:func:`redemptions_rounded_by_source`: the gap is within what rounding every future row to
  0.01 can produce);
* the **manual override** (:func:`plan_schedule`): a reviewed file of amounts of future
  redemptions (``mdhub/data/bond_amortization_overrides.json``, table
  ``ref.bond_amortization_override``) replaces the MOEX rows of that bond from the first
  override date on, but only while it agrees with what MOEX publishes (same dates, amounts
  within rounding, same face currency, sum equal to the face MOEX states). A divergent override
  is **not applied** and the bond is labelled ``redemptions_override_stale``;
* the **reasons the MOEX analytics are unreliable** (``source_analytics_unreliable_reasons``):
  MOEX computed its yields, durations and spreads from its own schedule, so they stay
  unreliable whenever that schedule is inconsistent, also when an override gives the hub a
  correct one.

Pure functions, no database access.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, Literal

from mdhub.snapshot.build import sorted_labels

__all__ = [
    "FACE_TOLERANCE",
    "LABEL_DIFFER",
    "LABEL_NOT_IN_SCHEDULE",
    "LABEL_OVERRIDE_APPLIED",
    "LABEL_OVERRIDE_STALE",
    "LABEL_ROUNDED",
    "ROUNDING_HALF_STEP",
    "ROUNDING_STEP",
    "SOURCE_MANUAL_OVERRIDE",
    "SOURCE_MOEX",
    "UNRELIABLE_FIELDS",
    "SchedulePlan",
    "face_schedule_labels",
    "face_steps",
    "face_value_date",
    "plan_schedule",
    "redemption_pairs",
    "redemptions_rounded_by_source",
    "unreliable_fields",
]

FACE_TOLERANCE: Final = Decimal("0.005")
"""Per-bond amounts are rounded to kopecks / cents by MOEX: steps closer than this match."""

ROUNDING_STEP: Final = Decimal("0.01")
"""Step of the amounts MOEX publishes (``value``)."""

ROUNDING_HALF_STEP: Final = ROUNDING_STEP / 2
"""Largest error of one amount rounded to :data:`ROUNDING_STEP`."""

LABEL_NOT_IN_SCHEDULE: Final = "face_value_not_in_schedule"
LABEL_DIFFER: Final = "future_redemptions_differ_from_face"
LABEL_ROUNDED: Final = "redemptions_rounded_by_source"
LABEL_OVERRIDE_APPLIED: Final = "redemptions_manual_override"
LABEL_OVERRIDE_STALE: Final = "redemptions_override_stale"

SOURCE_MOEX: Final = "moex_iss"
SOURCE_MANUAL_OVERRIDE: Final = "manual_override"
"""``Redemption.source`` values."""

UNRELIABLE_FIELDS: Final = frozenset(
    {
        "yield_close_pct",
        "yield_pct",
        "yield_at_waprice_pct",
        "yield_to_offer_pct",
        "yield_last_coupon_pct",
        "effective_yield_pct",
        "effective_yield_waprice_pct",
        "duration_days",
        "duration_waprice_days",
        "zspread_bp",
        "gspread_bp",
    }
)
"""``source_analytics`` fields MOEX derives from the redemption schedule (cash flows): yields,
durations and spreads. ``yield_date`` / ``yield_date_type`` (a date and a type) and the linker
fields ``icpi`` / ``ir`` / ``bei`` / ``cbr`` are not among them."""


def unreliable_fields(analytics: Mapping[str, Any]) -> list[str]:
    """The :data:`UNRELIABLE_FIELDS` present in a quote's ``source_analytics``, sorted."""
    return sorted(UNRELIABLE_FIELDS & set(analytics))


# ------------------------------------------------------------------ face and schedule


def face_steps(
    b: Mapping[str, Any], redemptions: Sequence[Mapping[str, Any]]
) -> list[tuple[date | None, Decimal]] | None:
    """Outstanding face after each redemption date of the schedule, starting with
    ``(issue_date, initial_face_value)``; ``None`` if the initial face or a redemption value
    is missing (the steps cannot be established)."""
    initial = b["initial_face_value"]
    if initial is None:
        return None
    by_date: dict[date, Decimal] = {}
    for r in redemptions:
        if r["value_per_bond"] is None:
            return None
        by_date[r["date"]] = by_date.get(r["date"], Decimal(0)) + r["value_per_bond"]
    steps: list[tuple[date | None, Decimal]] = [(b["issue_date"], initial)]
    left = initial
    for d in sorted(by_date):
        left -= by_date[d]
        steps.append((d, left))
    return steps


def face_value_date(
    b: Mapping[str, Any], redemptions: Sequence[Mapping[str, Any]], business_date: date
) -> date | None:
    """HS11: date from which the published ``face_value`` is outstanding (see the contract
    README): the latest schedule step on or before ``business_date`` whose outstanding face
    equals ``face_value``, else the earliest later one; ``None`` without a match."""
    face = b["face_value"]
    steps = face_steps(b, redemptions)
    if face is None or steps is None:
        return None
    hits = [d for d, left in steps if abs(left - face) <= FACE_TOLERANCE]
    if not hits:
        return None
    past = [d for d in hits if d is None or d <= business_date]
    return past[-1] if past else hits[0]


def _owed_candidates(
    b: Mapping[str, Any], redemptions: Sequence[Mapping[str, Any]], business_date: date
) -> list[list[Decimal]]:
    """The amounts still to be paid, as one candidate set (the face date says whether today's
    redemption is already deducted from ``face_value``) or, when it does not say, two (with and
    without today's redemption)."""
    fd = face_value_date(b, redemptions, business_date)

    def owed(include_today: bool) -> list[Decimal]:
        return [
            r["value_per_bond"]
            for r in redemptions
            if r["date"] > business_date or (include_today and r["date"] == business_date)
        ]

    return [owed(fd < business_date)] if fd is not None else [owed(True), owed(False)]


def face_schedule_labels(
    b: Mapping[str, Any], redemptions: Sequence[Mapping[str, Any]], business_date: date
) -> tuple[str | None, str | None]:
    """HS13: MOEX inconsistencies between the face and the redemption schedule, as labels
    (the published values are not corrected):

    * ``face_value_not_in_schedule`` - ``initial_face_value`` minus the scheduled redemptions
      never equals ``face_value`` (e.g. face 400, redemptions 300 + 400). Not for indexed-face
      bonds (indexation explains the difference).
    * ``future_redemptions_differ_from_face`` - the redemptions still to be paid do not add
      up to ``face_value`` (on a payment day, today's redemption counts as still to be paid
      only while ``face_value`` has not been reduced by it).

    Bonds without redemption rows, or with a missing value, get neither label."""
    face = b["face_value"]
    if face is None or not redemptions or any(r["value_per_bond"] is None for r in redemptions):
        return None, None
    fd = face_value_date(b, redemptions, business_date)
    not_in = (
        LABEL_NOT_IN_SCHEDULE
        if fd is None and b["initial_face_value"] is not None and not b.get("is_face_indexed")
        else None
    )
    differ = (
        LABEL_DIFFER
        if all(
            abs(sum(c, Decimal(0)) - face) > FACE_TOLERANCE
            for c in _owed_candidates(b, redemptions, business_date)
        )
        else None
    )
    return not_in, differ


def _on_grid(value: Decimal) -> bool:
    steps = value / ROUNDING_STEP
    return steps == steps.to_integral_value()


def redemptions_rounded_by_source(
    b: Mapping[str, Any], redemptions: Sequence[Mapping[str, Any]], business_date: date
) -> bool:
    """Label ``redemptions_rounded_by_source``: the schedule is inconsistent with the face and
    the gap is what rounding the amounts to :data:`ROUNDING_STEP` can produce. All of:

    1. ``future_redemptions_differ_from_face`` holds (the sum of the amounts still to be paid
       differs from ``face_value`` by more than :data:`FACE_TOLERANCE`);
    2. every one of those amounts lies on the 0.01 grid MOEX publishes (nothing finer is shown);
    3. ``|sum - face_value| <= n * 0.005`` for the ``n`` amounts: an amount rounded to 0.01 is
       off by at most half a step, so a gap above that cannot come from rounding alone.

    Where a day's redemption may or may not be deducted from the face yet, 1 must hold for every
    reading (as for the label it rests on) and 2-3 for at least one: the label does not flicker
    with a face date that cannot be resolved. Not for indexed-face bonds. It is a necessary
    condition, not proof: the true amounts are not known, and the label says only that rounding
    can explain the gap."""
    face = b["face_value"]
    if (
        face is None
        or b.get("is_face_indexed")
        or not redemptions
        or any(r["value_per_bond"] is None for r in redemptions)
    ):
        return False
    candidates = _owed_candidates(b, redemptions, business_date)
    if not all(abs(sum(c, Decimal(0)) - face) > FACE_TOLERANCE for c in candidates):
        return False  # condition 1: no label of the schedule being inconsistent
    return any(
        owed
        and all(_on_grid(v) for v in owed)
        and abs(sum(owed, Decimal(0)) - face) <= len(owed) * ROUNDING_HALF_STEP
        for owed in candidates
    )


# ------------------------------------------------------------------ manual override


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """The redemption schedule of one bond as the snapshot and the export publish it."""

    rows: list[dict[str, Any]]
    """Effective rows: ``ref.bond_amortization`` columns plus ``source`` and ``override``
    (``None`` or ``{note, confirmed_by, confirmed_on, file_version}``), ordered by date."""
    override: Literal["none", "applied", "stale"]
    stale_reason: str | None
    """Why an override was not applied (``override == "stale"``): facts only, no values of
    other bonds."""
    unreliable_reasons: tuple[str, ...]
    """Why MOEX's analytics of this bond are unreliable (:data:`LABEL_DIFFER` and / or
    :data:`LABEL_ROUNDED`), judged on the MOEX rows as published - an applied override does not
    change what MOEX computed its yields from."""

    def labels(self, b: Mapping[str, Any], business_date: date) -> list[str]:
        """Schedule labels of the bond: the HS13 face labels of the effective rows, plus
        ``redemptions_rounded_by_source`` (the published rows are MOEX's; not when an override
        replaces them), ``redemptions_manual_override`` and ``redemptions_override_stale``."""
        return sorted_labels(
            *face_schedule_labels(b, redemption_pairs(self.rows), business_date),
            LABEL_ROUNDED
            if LABEL_ROUNDED in self.unreliable_reasons and self.override != "applied"
            else None,
            LABEL_OVERRIDE_APPLIED if self.override == "applied" else None,
            LABEL_OVERRIDE_STALE if self.override == "stale" else None,
        )


def redemption_pairs(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``(date, value_per_bond)`` pairs of schedule rows, the input of the face functions."""
    return [{"date": r["amort_date"], "value_per_bond": r["value"]} for r in rows]


def _moex_row(a: Mapping[str, Any]) -> dict[str, Any]:
    return {**a, "source": SOURCE_MOEX, "override": None}


def _unit(v: Any) -> str | None:
    return None if v is None else str(v).strip().upper()


def _stale_reason(
    b: Mapping[str, Any],
    moex: Sequence[Mapping[str, Any]],
    override: Sequence[Mapping[str, Any]],
    since: date,
) -> str | None:
    """Why the override does not agree with the MOEX rows from ``since`` on (``None``: it
    does). Dates and amounts are compared on the whole window, past rows included: MOEX keeps
    the rows of paid redemptions. A date for which MOEX has no amount yet (``value`` NULL) must
    still be a date of the override; its amount is not compared (MOEX may publish a date before
    its amount)."""
    if b.get("is_face_indexed"):
        return "indexed face: the outstanding face changes with the index, amounts are projected"
    units = {_unit(o["face_unit"]) for o in override}
    if len(units) != 1 or _unit(b["face_unit"]) not in units:
        return "face currency of the override differs from the bond's"
    mine = {o["amort_date"]: o["value"] for o in override}
    theirs: dict[date, Decimal | None] = {}
    for a in moex:
        d = a["amort_date"]
        if d < since:
            continue
        before = theirs.get(d, Decimal(0))
        theirs[d] = None if before is None or a["value"] is None else before + a["value"]
    if set(mine) != set(theirs):
        added = sorted(set(theirs) - set(mine))
        gone = sorted(set(mine) - set(theirs))
        return (
            "redemption dates differ from MOEX"
            + (f"; MOEX only: {', '.join(d.isoformat() for d in added)}" if added else "")
            + (f"; override only: {', '.join(d.isoformat() for d in gone)}" if gone else "")
        )
    off = sorted(
        d
        for d, v in mine.items()
        if (t := theirs[d]) is not None and abs(t - v) > ROUNDING_HALF_STEP
    )
    if off:
        return "amounts differ from MOEX by more than rounding on " + ", ".join(
            d.isoformat() for d in off
        )
    return None


SUM_TOLERANCE: Final = Decimal("1e-9")
"""Override amounts and ``face_value`` are exact decimals: the sum check allows no rounding
slack (:data:`FACE_TOLERANCE` is a whole payment for a bond with a low face)."""


def _adds_up_to_face(
    face: Decimal | None, rows: Sequence[Mapping[str, Any]], reference_date: date
) -> bool:
    """Do the amounts still to be paid add up exactly to ``face``? On a payment day, today's
    amount may or may not be deducted from the face yet: either reading may match."""
    if face is None:
        return False
    ahead = sum((r["value"] for r in rows if r["amort_date"] > reference_date), Decimal(0))
    today = sum((r["value"] for r in rows if r["amort_date"] == reference_date), Decimal(0))
    return any(abs(total - face) <= SUM_TOLERANCE for total in (ahead, ahead + today))


def plan_schedule(
    b: Mapping[str, Any],
    moex: Sequence[Mapping[str, Any]],
    override: Sequence[Mapping[str, Any]],
    reference_date: date,
) -> SchedulePlan:
    """The schedule of bond ``b`` from its MOEX rows ``moex`` (``ref.bond_amortization``) and
    the override rows ``override`` (``ref.bond_amortization_override``, non-deleted), judged on
    ``reference_date``.

    An override replaces the MOEX rows from its first date on and is applied only if
    :func:`_stale_reason` finds nothing and the resulting future rows add up **exactly** to the
    face MOEX states (:func:`_adds_up_to_face`, not the HS13 tolerance). Otherwise the MOEX rows
    are published unchanged and the plan says why (``override == "stale"``)."""
    moex_rows = sorted(
        (_moex_row(a) for a in moex), key=lambda r: (r["amort_date"], r["data_source"])
    )
    raw = redemption_pairs(moex_rows)
    differ = face_schedule_labels(b, raw, reference_date)[1]
    rounded = redemptions_rounded_by_source(b, raw, reference_date)
    reasons = tuple(sorted(x for x in (differ, LABEL_ROUNDED if rounded else None) if x))
    if not override:
        return SchedulePlan(moex_rows, "none", None, reasons)

    since = min(o["amort_date"] for o in override)
    reason = _stale_reason(b, moex, override, since)
    rows: list[dict[str, Any]] = []
    if reason is None:
        # two MOEX rows on one date (amortization + maturity): the maturity row, deterministically
        by_date = {a["amort_date"]: a for a in moex_rows if a["amort_date"] >= since}
        kept = [r for r in moex_rows if r["amort_date"] < since]
        replaced: list[dict[str, Any]] = []
        for o in sorted(override, key=lambda o: o["amort_date"]):
            same = by_date.get(o["amort_date"])
            replaced.append(
                {
                    "secid": o["secid"],
                    "amort_date": o["amort_date"],
                    "data_source": same["data_source"]
                    if same is not None
                    else ("maturity" if o["amort_date"] == b["maturity_date"] else "amortization"),
                    "value_prc": None,
                    "value": o["value"],
                    "value_rub": None,
                    "face_value": same["face_value"] if same is not None else None,
                    "initial_face_value": same["initial_face_value"]
                    if same is not None
                    else b["initial_face_value"],
                    "face_unit": o["face_unit"],
                    "source": SOURCE_MANUAL_OVERRIDE,
                    "override": {
                        "note": o["source_note"],
                        "confirmed_by": o["confirmed_by"],
                        "confirmed_on": o["confirmed_on"],
                        "file_version": o["file_version"],
                    },
                    "run_id": o.get("run_id"),
                    "ingested_at": o.get("ingested_at"),
                    "updated_at": o.get("updated_at"),
                }
            )
        rows = [*kept, *replaced]
        if not _adds_up_to_face(b["face_value"], rows, reference_date):
            reason = "redemptions still to be paid do not add up to the face value MOEX states"
    if reason is not None:
        return SchedulePlan(moex_rows, "stale", reason, reasons)
    return SchedulePlan(rows, "applied", None, reasons)
