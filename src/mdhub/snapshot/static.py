"""Static snapshot builder: instruments, schedules, formulas, issuers, calendars, IdMap.

Everything is read **as known at** ``known_at`` through the readers of migration 0500
(``ref.<table>_known_at``, ``ref.bond_rate_formula_resolved_known_at``,
``ref.calendar_effective_known_at``), so rebuilding a past static snapshot with the same
``known_at`` reproduces it byte for byte (except for writes whose transaction was still open
at ``known_at``: their rows carry an earlier ``ingested_at``).

Universe: bonds whose status (as known) is ``active``, plus ``matured`` bonds with a maturity
date within :data:`MATURED_LOOKBACK_DAYS` before the business date (late EOD rows and final
redemptions stay quotable), plus **alive bonds that are not traded** (``delisted`` with a
maturity date on or after the business date, or perpetual; label ``not_traded``): desk
positions resolve by ISIN against the instruments, also when MOEX lists no board for the
bond. A ``delisted`` bond without a maturity date that is not perpetual is not included (its
life cannot be established). For a linked EOD market cut, ``quoted_secids`` also retains any
known instrument with a price or yield on that date, including older matured and delisted
bonds (label ``retained_for_market_quote``). Missing reference instruments are not invented.
Inside the universe nothing is excluded: unknown coupon types, perpetuals, missing day
counts and partial floater formulas are published with **labels** (contract rule
"coverage labelling instead of exclusion").

Calendars come from ``ref.calendar_effective`` only (never ``ref.weekend_session``): per
calendar the covered range ``[valid_from, valid_to]`` of its rows, holidays (weekdays that are
not business days), working weekends, ``provisional_from`` (first day decided by an unverified
curated row), a version (latest row change time) and a fingerprint; the snapshot carries one
calendar version and fingerprint for the whole set.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any, Final, LiteralString

from psycopg import sql

from mdhub.core.bizday import CALENDARS
from mdhub.db.pool import DictConnection
from mdhub.snapshot.build import Build, Cut, Dataset, Excluded, Provenance, sorted_labels
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex, utc_str

__all__ = [
    "BOND_CALENDAR",
    "MATURED_LOOKBACK_DAYS",
    "build_static",
    "calendar_objects",
    "coupon_frequency_from_schedule",
    "face_schedule_labels",
    "face_steps",
    "face_value_date",
    "issue_size_moex",
    "replacement_by_name",
    "undated_offer_reason",
]

MATURED_LOOKBACK_DAYS: Final = 31
BOND_CALENDAR: Final = "MOEX_STOCK"
"""Settlement calendar of MOEX bonds (the hub's bond datasets use it)."""

_PROVISIONAL_SOURCES: Final = frozenset({"curated_unverified"})


def _alive_not_traded(b: dict[str, Any], day: date) -> bool:
    """``delisted`` but alive on ``day``: perpetual or maturing on/after it (HS2)."""
    return b["status"] == "delisted" and bool(
        b["is_perpetual"] or (b["maturity_date"] is not None and b["maturity_date"] >= day)
    )


def _universe(
    conn: DictConnection,
    known_at: datetime,
    since: date,
    day: date,
    quoted_secids: frozenset[str],
) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT * FROM ref.bond_known_at(%(t)s) b
        WHERE b.status = 'active' OR (b.status = 'matured' AND b.maturity_date >= %(since)s)
           OR (b.status = 'delisted' AND (b.is_perpetual OR b.maturity_date >= %(day)s))
           OR b.secid = ANY(%(quoted)s)
        ORDER BY b.secid
        """,
        {"t": known_at, "since": since, "day": day, "quoted": sorted(quoted_secids)},
    ).fetchall()


def _by_secid(
    conn: DictConnection, func: str, known_at: datetime, secids: list[str], order: LiteralString
) -> list[dict[str, Any]]:
    """Non-deleted rows of the as-known-at reader ``ref.<func>`` for ``secids``."""
    query = sql.SQL(
        "SELECT * FROM {f}(%(t)s) x WHERE x.secid = ANY(%(s)s) "
        "AND NOT COALESCE(x.is_deleted, false) ORDER BY "
    ).format(f=sql.Identifier("ref", func)) + sql.SQL(order)
    return conn.execute(query, {"t": known_at, "s": secids}).fetchall()


def _boards(conn: DictConnection, known_at: datetime, secids: list[str]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM ref.bond_board_known_at(%(t)s) x WHERE x.secid = ANY(%(s)s) "
        "ORDER BY x.secid, x.board",
        {"t": known_at, "s": secids},
    ).fetchall()


def coupon_frequency_from_schedule(coupons: list[dict[str, Any]]) -> int | None:
    """Coupons per year from a schedule (HS7): ``round(365.25 / median period)`` over the
    periods with both ``start_date`` and ``pay_date`` (positive length); ``None`` with fewer
    than two such periods. Irregular first/last periods do not move the median."""
    days = sorted(
        (c["pay_date"] - c["start_date"]).days
        for c in coupons
        if c["start_date"] is not None and c["pay_date"] is not None
        and c["pay_date"] > c["start_date"]
    )  # fmt: skip
    if len(days) < 2:
        return None
    return max(1, round(Decimal("365.25") / Decimal(median(days))))


LARGE_ISSUER_RUB: Final = 5_000_000_000
"""Issuers whose active RUB bonds outstanding (face x issue size) reach this must have a
curated group (TDM brief HS4); the others missing one are listed in the issuers coverage."""

FACE_TOLERANCE: Final = Decimal("0.005")
"""Per-bond amounts are rounded to kopecks / cents by MOEX: steps closer than this match."""


def face_steps(
    b: Mapping[str, Any], redemptions: list[dict[str, Any]]
) -> list[tuple[date | None, Decimal]] | None:
    """Outstanding face after each redemption date of the schedule, starting with
    ``(issue_date, initial_face_value)``; ``None`` if the initial face or a redemption value
    is missing (the steps cannot be established)."""
    initial = b["initial_face_value"]
    if initial is None:
        return None
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    for r in redemptions:
        if r["value_per_bond"] is None:
            return None
        by_date[r["date"]] += r["value_per_bond"]
    steps: list[tuple[date | None, Decimal]] = [(b["issue_date"], initial)]
    left = initial
    for d in sorted(by_date):
        left -= by_date[d]
        steps.append((d, left))
    return steps


def face_value_date(
    b: Mapping[str, Any], redemptions: list[dict[str, Any]], business_date: date
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


def face_schedule_labels(
    b: Mapping[str, Any], redemptions: list[dict[str, Any]], business_date: date
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
        "face_value_not_in_schedule"
        if fd is None and b["initial_face_value"] is not None and not b.get("is_face_indexed")
        else None
    )

    def owed(include_today: bool) -> Decimal:
        return sum(
            (r["value_per_bond"] for r in redemptions
             if r["date"] > business_date or (include_today and r["date"] == business_date)),
            Decimal(0),
        )  # fmt: skip

    candidates = [owed(fd < business_date)] if fd is not None else [owed(True), owed(False)]
    differ = (
        "future_redemptions_differ_from_face"
        if all(abs(c - face) > FACE_TOLERANCE for c in candidates)
        else None
    )
    return not_in, differ


PUT_SUBTYPE: Final = "До оферты (put)"
"""MOEX BOND_SUBTYPE of bonds that trade to a put offer (HS10)."""

UNDATED_DONE: Final = frozenset({"completed", "cancelled", "default", "technical_default"})


def undated_offer_reason(o: Mapping[str, Any], business_date: date) -> str:
    """HS10: why an offer row has no date, as a label (the row is published as MOEX has it):

    * ``undated_past_event`` - MOEX marks it completed / cancelled / defaulted;
    * ``undated_window_past`` / ``undated_window_future`` - an exercise window is known and
      ends before / on or after the business date;
    * ``undated_unexplained`` - nothing in the row dates it (needs the issue terms)."""
    if o["status"] in UNDATED_DONE:
        return "undated_past_event"
    ends = [d for d in (o["offer_date_end"], o["offer_date_start"]) if d is not None]
    if ends:
        return "undated_window_past" if max(ends) < business_date else "undated_window_future"
    return "undated_unexplained"


REPLACEMENT_NAME_RE: Final = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё])(?:Б|С|СУБ-)?ЗО(?=[\s-]?(?:\d|Т\d)|$)|^ОВОЗ\b"
)
"""P1-6 name rule on the MOEX full name (NAME): a token ``ЗО`` (замещающие облигации), also
``БЗО`` (perpetual), ``СЗО`` / ``СУБ-ЗО`` (subordinated), followed by a series number, ``Т1`` or
the end (``Газпром капитал ООО ЗО27-1-Д``, ``МКБ ЗО-2027``, ``Банк ВТБ ЗО-Т1``, ``ТКБ
СУБ-ЗО``), and the sovereign ``ОВОЗ РФ …`` (MinFin replacement of eurobonds). The short name
is not used: ``ЗО-001P-01`` there is ``Защита онлайн``."""


def replacement_by_name(name: str | None) -> bool:
    """P1-6: does the MOEX full name mark a replacement bond (:data:`REPLACEMENT_NAME_RE`)?"""
    return bool(name and REPLACEMENT_NAME_RE.search(name))


def issue_size_moex(b: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
    """P1-10: number of bonds as MOEX publishes it and the field it came from: ``ISSUESIZE``
    (``ref.bond.issue_size``), else ``ISSUESIZEPLACED`` of the market board. Not the amount
    outstanding: buybacks, offer redemptions and partial placements are not deducted."""
    if b.get("issue_size") is not None:
        return b["issue_size"], "ISSUESIZE"
    if b.get("issue_size_placed") is not None:
        return b["issue_size_placed"], "ISSUESIZEPLACED"
    return None, None


def _instrument(
    b: dict[str, Any],
    boards: list[dict[str, Any]],
    labels: list[str],
    *,
    schedule_frequency: int | None = None,
    face_date: date | None = None,
    replacement: tuple[bool | None, str | None] = (None, None),
) -> dict[str, Any]:
    size, size_field = issue_size_moex(b)
    return {
        "secid": b["secid"],
        "isin": b["isin"],
        "regnumber": b["regnumber"],
        "issuer_id": b["issuer_id"],
        "name": b["name"],
        "short_name": b["short_name"],
        "lat_name": b["lat_name"],
        "sec_type": b["sec_type"],
        "bond_type": b["bond_type"],
        "bond_subtype": b["bond_subtype"],
        "coupon_details": b["coupon_details"],
        "bond_class": b["bond_class"],
        "coupon_type": b["coupon_type"],
        "coupon_type_basis": b["coupon_type_basis"],
        "coupon_benchmark": b["coupon_benchmark"],
        "coupon_benchmark_spread_pct": b["coupon_benchmark_spread_pct"],
        "face_unit": b["face_unit"],
        "currency_id": b["currency_id"],
        "face_value": b["face_value"],
        "initial_face_value": b["initial_face_value"],
        "face_value_date": face_date,
        "issue_date": b["issue_date"],
        "maturity_date": b["maturity_date"],
        "is_perpetual": bool(b["is_perpetual"]),
        "is_amortizing": b["is_amortizing"],
        "is_subordinated": b["is_subordinated"],
        "is_structured": b["is_structured"],
        "is_face_indexed": b["is_face_indexed"],
        "is_fx_face": b["is_fx_face"],
        "qualified_only": b["qualified_only"],
        "high_risk": b["high_risk"],
        "coupon_frequency": b["coupon_frequency"],
        "coupon_frequency_schedule": schedule_frequency,
        "coupon_period_days": b["coupon_period_days"],
        "day_count": b["day_count"],
        "day_count_source": b["day_count_source"],
        "listing_level": b["listing_level"],
        "issue_size": b["issue_size"],
        "issue_size_moex": size,
        "issue_size_moex_field": size_field,
        "replacement_bond": replacement[0],
        "replacement_basis": replacement[1],
        "primary_board": b["primary_board"],
        "status": b["status"],
        "calendar": BOND_CALENDAR,
        "boards": [
            {
                "board": r["board"],
                "is_primary": bool(r["is_primary"]),
                "listed": bool(r["listed"]),
                "listing_source": r["listing_source"],
                "lot_size": r["lot_size"],
                "min_step": r["min_step"],
                "decimals": r["decimals"],
                "currency_id": r["currency_id"],
                "face_unit": r["face_unit"],
                "settle_code": r["settle_code"],
                "settle_lag_days": r["settle_lag_days"],
                "listed_from": r["listed_from"],
                "listed_till": r["listed_till"],
            }
            for r in boards
        ],
        "labels": labels,
    }


def calendar_objects(
    conn: DictConnection, known_at: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """``(calendars, snapshot calendar {version, fingerprint}, effective rows)`` as known at
    ``known_at`` from ``ref.calendar_effective_known_at`` (never ``ref.weekend_session``)."""
    eff = conn.execute(
        "SELECT * FROM ref.calendar_effective_known_at(%(t)s) ORDER BY calendar, day",
        {"t": known_at},
    ).fetchall()
    versions = {
        r["calendar"]: r["v"]
        for r in conn.execute(
            "SELECT calendar, max(COALESCE(ingested_at, known_at)) AS v "
            "FROM ref.calendar_day_known_at(%(t)s) GROUP BY calendar",
            {"t": known_at},
        ).fetchall()
    }
    by_cal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in eff:
        by_cal[r["calendar"]].append(r)
    out: list[dict[str, Any]] = []
    for code in sorted(by_cal):
        rows = by_cal[code]
        flags = {r["day"]: bool(r["is_business_day"]) for r in rows}
        valid_from, valid_to = rows[0]["day"], rows[-1]["day"]
        holidays: list[date] = []
        working_weekends: list[date] = []
        d = valid_from
        while d <= valid_to:
            weekday = d.weekday() < 5
            flag = flags.get(d, weekday)
            if weekday and not flag:
                holidays.append(d)
            elif not weekday and flag:
                working_weekends.append(d)
            d += timedelta(days=1)
        provisional = [r["day"] for r in rows if r["source"] in _PROVISIONAL_SOURCES]
        body = {
            "calendar": code,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "provisional_from": min(provisional) if provisional else None,
            "holidays": holidays,
            "working_weekends": working_weekends,
        }
        out.append(
            {
                **body,
                "sources": sorted({r["source"] for r in rows}),
                "version": utc_str(versions.get(code)),
                "fingerprint": sha256_hex(canonical_bytes(body)),
            }
        )
    all_versions = [v for v in versions.values() if v is not None]
    summary = {
        "version": utc_str(max(all_versions)) if all_versions else None,
        "fingerprint": sha256_hex(
            canonical_bytes(
                [{"calendar": c["calendar"], "fingerprint": c["fingerprint"]} for c in out]
            )
        ),
    }
    return out, summary, eff


def _evidence_field(row: Mapping[str, Any] | None, field: str) -> str | None:
    ev = row.get("evidence") if row else None
    value = ev.get(field) if isinstance(ev, Mapping) else None
    return str(value) if value is not None else None


def _document_url(row: Mapping[str, Any] | None) -> str | None:
    """1.3: URL of the issue document a curated row was read from (``evidence``)."""
    return _evidence_field(row, "document_url")


def _replacement(
    b: Mapping[str, Any], documented: Mapping[str, Any] | None
) -> tuple[bool | None, str | None]:
    """1.3 (P1-6): ``(replacement_bond, replacement_basis)``: a curated document entry decides
    (``document``, true or false); else the name rule marks a replacement bond (``name_rule``,
    true only: no match is ``None``, unknown, never ``false``)."""
    if documented is not None:
        value = documented["value"].get("replacement_bond")
        if isinstance(value, bool):
            return value, "document"
    if replacement_by_name(b.get("name")):
        return True, "name_rule"
    return None, None


def _rating(
    r: Mapping[str, Any], scope: str, secid: str | None, issuer_id: int | None
) -> dict[str, Any]:
    return {
        "scope": scope,
        "secid": secid,
        "issuer_id": issuer_id,
        "agency": r["agency"],
        "scale": r["scale"],
        "rating": r["rating"],
        "grade": r["grade"],
        "outlook": r["outlook"],
        "watch": r["watch"],
        "rating_date": r["rating_date"],
        "action": r["action"],
        "rating_type": r["rating_type"],
        "press_release": _evidence_field(r, "press_release"),
        "mapping_basis": _evidence_field(r, "mapping_basis"),
        "source": r["source"],
    }


def _rating_key(r: Mapping[str, Any]) -> tuple[str, str, int, str, str, str, str]:
    return (
        r["scope"], r["secid"] or "", r["issuer_id"] or 0, r["agency"], r["scale"],
        r["source"], r["rating_type"] or "",
    )  # fmt: skip


def _rating_coverage(ratings: list[dict[str, Any]], bonds: list[dict[str, Any]]) -> dict[str, Any]:
    """HS5 / P1-1 coverage. ``active_rub_corporate_issuers_rated``: issuers of active
    non-structured RUB corporate bonds with a current rating (issue or issuer scope; a withdrawn
    rating does not count; the desk's acceptance base). 1.3: the same over active
    non-structured bonds - rated themselves (issue), through their issuer, or either. No
    source rows is shown as zero coverage, never filled from elsewhere."""
    current = [r for r in ratings if r["rating"] is not None]
    base = {
        b["issuer_id"]
        for b in bonds
        if b["status"] == "active"
        and b["bond_class"] == "corporate"
        and not b["is_structured"]
        and b["face_unit"] == "RUB"
        and b["issuer_id"] is not None
    }
    rated = {r["issuer_id"] for r in current if r["issuer_id"] in base}
    active = [b for b in bonds if b["status"] == "active" and not b["is_structured"]]
    issue_rated = {r["secid"] for r in current if r["scope"] == "issue"}
    issuer_rated = {r["issuer_id"] for r in current if r["scope"] == "issuer"}
    by_issue = {b["secid"] for b in active if b["secid"] in issue_rated}
    by_issuer = {b["secid"] for b in active if b["issuer_id"] in issuer_rated}
    return {
        "rows": len(ratings),
        "by_agency": dict(sorted(Counter(r["agency"] for r in ratings).items())),
        "by_source": dict(sorted(Counter(r["source"] for r in ratings).items())),
        "by_scope": dict(sorted(Counter(r["scope"] for r in ratings).items())),
        "withdrawn": sum(1 for r in ratings if r["rating"] is None),
        "sources": sorted({r["source"] for r in ratings}),
        "active_rub_corporate_issuers": len(base),
        "active_rub_corporate_issuers_rated": len(rated),
        "active_non_structured_bonds": len(active),
        "active_non_structured_bonds_issue_rated": len(by_issue),
        "active_non_structured_bonds_issuer_rated": len(by_issuer),
        "active_non_structured_bonds_rated": len(by_issue | by_issuer),
    }


def _documented_coverage(
    bonds: list[dict[str, Any]], formulas: list[dict[str, Any]], scheduled_calls: set[str]
) -> dict[str, Any]:
    """1.3 (HS8 / HS9): how many of the desk-relevant terms are documented: active floaters on
    the key rate or RUONIA with a curated formula, active perpetuals with a scheduled call."""
    active = {b["secid"]: b for b in bonds if b["status"] == "active"}
    on_key_ruonia = [
        f
        for f in formulas
        if f["secid"] in active
        and (f["benchmark"] == "KEY_RATE" or str(f["benchmark"] or "").startswith("RUONIA"))
    ]
    perpetuals = [s for s, b in active.items() if b["is_perpetual"]]
    return {
        "key_rate_ruonia_floaters": len(on_key_ruonia),
        "key_rate_ruonia_floaters_curated": sum(
            1 for f in on_key_ruonia if f["status"] == "curated"
        ),
        "active_perpetuals": len(perpetuals),
        "active_perpetuals_with_scheduled_call": sum(1 for s in perpetuals if s in scheduled_calls),
    }


def _issuer_coverage(
    issuers: list[dict[str, Any]], bonds: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fill rates of the issuer reference (HS1/HS3): over all published issuers and over the
    issuers of ``active`` non-structured bonds (the desk's acceptance base)."""
    active = {
        b["issuer_id"]
        for b in bonds
        if b["status"] == "active" and not b["is_structured"] and b["issuer_id"] is not None
    }

    def rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "issuers": len(rows),
            "with_short_name": sum(1 for i in rows if i["short_name"]),
            "with_ogrn": sum(1 for i in rows if i["ogrn"]),
            "with_country": sum(1 for i in rows if i["country"]),
            "with_sector": sum(1 for i in rows if i["sector"]),
        }

    outstanding: Counter[int] = Counter()
    for b in bonds:
        if (
            b["status"] == "active"
            and b["issuer_id"] is not None
            and b["face_unit"] == "RUB"
            and b["face_value"] is not None
            and b["issue_size"] is not None
        ):
            outstanding[b["issuer_id"]] += b["face_value"] * b["issue_size"]
    large = {iid for iid, v in outstanding.items() if v >= LARGE_ISSUER_RUB}
    return {
        **rates(issuers),
        "of_active_non_structured": rates([i for i in issuers if i["issuer_id"] in active]),
        "with_group": sum(1 for i in issuers if i["group_id"]),
        "large_issuers_rub": len(large),
        "large_issuer_threshold_rub": LARGE_ISSUER_RUB,
        "large_issuers_without_group": sorted(
            i["issuer_id"] for i in issuers if i["issuer_id"] in large and not i["group_id"]
        ),
        "by_sector": dict(sorted(Counter(i["sector"] or "none" for i in issuers).items())),
        "by_sector_source": dict(
            sorted(Counter(r.get("sector_source") or "none" for r in rows).items())
        ),
        "without_sector": sorted(i["issuer_id"] for i in issuers if not i["sector"]),
    }


def build_static(
    conn: DictConnection,
    *,
    cut: Cut,
    matured_lookback_days: int = MATURED_LOOKBACK_DAYS,
    quoted_secids: frozenset[str] = frozenset(),
) -> Build:
    """Build the static snapshot of ``cut`` (kind ``static``) as known at ``cut.known_at``."""
    if cut.kind != "static":
        raise ValueError("build_static needs a static cut")
    t = cut.known_at
    since = cut.business_date - timedelta(days=matured_lookback_days)
    prov = Provenance()
    bonds = _universe(conn, t, since, cut.business_date, quoted_secids)
    secids = [b["secid"] for b in bonds]
    universe = frozenset(secids)

    boards = _boards(conn, t, secids)
    coupons = _by_secid(conn, "bond_coupon_known_at", t, secids, "x.secid, x.coupon_no")
    amort = _by_secid(
        conn, "bond_amortization_known_at", t, secids, "x.secid, x.amort_date, x.data_source"
    )
    offers = _by_secid(conn, "bond_offer_known_at", t, secids, "x.secid, x.source, x.offer_key")
    formula_rows = _by_secid(conn, "bond_rate_formula_known_at", t, secids, "x.secid, x.source")
    formulas = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved_known_at(%(t)s) x "
        "WHERE x.secid = ANY(%(s)s) ORDER BY x.secid",
        {"t": t, "s": secids},
    ).fetchall()
    issuer_ids = sorted({b["issuer_id"] for b in bonds if b["issuer_id"] is not None})
    issuers = conn.execute(
        "SELECT * FROM ref.issuer_known_at(%(t)s) x WHERE x.issuer_id = ANY(%(i)s) "
        "ORDER BY x.issuer_id",
        {"t": t, "i": issuer_ids},
    ).fetchall()
    rating_rows = conn.execute(
        "SELECT * FROM ref.bond_rating_known_at(%(t)s) x WHERE x.secid = ANY(%(s)s) "
        "ORDER BY x.secid, x.agency, x.scale, x.source",
        {"t": t, "s": secids},
    ).fetchall()
    issuer_rating_rows = conn.execute(
        "SELECT * FROM ref.issuer_rating_known_at(%(t)s) x WHERE x.issuer_id = ANY(%(i)s) "
        "ORDER BY x.issuer_id, x.agency, x.scale, x.source, x.rating_type",
        {"t": t, "i": issuer_ids},
    ).fetchall()
    curated = _by_secid(
        conn, "bond_curated_term_known_at", t, secids, "x.secid, x.term, x.term_key"
    )
    isin_rows = conn.execute(
        "SELECT secid, isin FROM ref.bond_known_at(%(t)s) WHERE isin IS NOT NULL",
        {"t": t},
    ).fetchall()
    calendars, cal_summary, cal_rows = calendar_objects(conn, t)

    # ---------------------------------------------------------------- instruments
    boards_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in boards:
        boards_of[r["secid"]].append(r)
    coupon_count = Counter(c["secid"] for c in coupons)
    coupons_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in coupons:
        coupons_of[c["secid"]].append(c)
    formula_of = {f["secid"]: f for f in formulas}
    issuer_set = {i["issuer_id"] for i in issuers}
    scheduled_calls = {
        o["secid"] for o in offers if o["offer_type"] == "call" and o["status"] == "scheduled"
    }
    scheduled_offers = {o["secid"] for o in offers if o["status"] == "scheduled"}
    documented: dict[tuple[str, str, str], dict[str, Any]] = {
        (c["secid"], c["term"], c["term_key"]): c for c in curated
    }
    redemptions_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in amort:
        redemptions_of[a["secid"]].append({"date": a["amort_date"], "value_per_bond": a["value"]})
    instruments = []
    label_counts: Counter[str] = Counter()
    for b in bonds:
        f = formula_of.get(b["secid"])
        labels = sorted_labels(
            "retained_for_market_quote"
            if b["secid"] in quoted_secids
            and not (
                b["status"] == "active"
                or (
                    b["status"] == "matured"
                    and b["maturity_date"] is not None
                    and b["maturity_date"] >= since
                )
                or _alive_not_traded(b, cut.business_date)
            )
            else None,
            "not_traded" if _alive_not_traded(b, cut.business_date) else None,
            "coupon_type_unknown" if b["coupon_type"] == "unknown" else None,
            "perpetual" if b["is_perpetual"] else None,
            "perpetual_no_call_date"
            if b["is_perpetual"] and b["secid"] not in scheduled_calls
            else None,
            "day_count_not_published" if b["day_count"] is None else None,
            "floater_formula_missing" if b["coupon_type"] == "floating" and f is None else None,
            "floater_formula_partial" if f is not None and f["status"] == "partial" else None,
            "coupon_schedule_empty"
            if coupon_count[b["secid"]] == 0 and b["coupon_type"] != "zero"
            else None,
            "no_issuer" if b["issuer_id"] is None or b["issuer_id"] not in issuer_set else None,
            "structured" if b["is_structured"] else None,
            "matured" if b["status"] == "matured" else None,
            *face_schedule_labels(b, redemptions_of[b["secid"]], cut.business_date),
            "put_subtype_without_scheduled_offer"
            if b["bond_subtype"] == PUT_SUBTYPE
            and b["status"] == "active"
            and b["secid"] not in scheduled_offers
            else None,
        )
        label_counts.update(labels)
        instruments.append(
            _instrument(
                b,
                boards_of[b["secid"]],
                labels,
                schedule_frequency=coupon_frequency_from_schedule(coupons_of[b["secid"]]),
                face_date=face_value_date(b, redemptions_of[b["secid"]], cut.business_date),
                replacement=_replacement(b, documented.get((b["secid"], "replacement_bond", ""))),
            )
        )
    prov.add_rows("instruments", bonds)

    coupon_rows = [
        {
            "secid": c["secid"],
            "coupon_no": c["coupon_no"],
            "start_date": c["start_date"],
            "pay_date": c["pay_date"],
            "record_date": c["record_date"],
            "amount_per_bond": c["amount"],
            "amount_rub_per_bond": c["value_rub"],
            "rate_pct": c["value_prc"],
            "face_value": c["face_value"],
            "face_unit": c["face_unit"],
            "status": c["status"],
        }
        for c in coupons
    ]
    prov.add_rows("coupon_periods", coupons)
    redemptions = [
        {
            "secid": a["secid"],
            "date": a["amort_date"],
            "kind": "maturity" if a["data_source"] == "maturity" else "amortization",
            "value_pct_of_initial": a["value_prc"],
            "value_per_bond": a["value"],
            "value_rub_per_bond": a["value_rub"],
            "face_value": a["face_value"],
            "initial_face_value": a["initial_face_value"],
            "face_unit": a["face_unit"],
        }
        for a in amort
    ]
    prov.add_rows("redemptions", amort)
    offer_rows = [
        {
            "secid": o["secid"],
            "source": o["source"],
            "offer_key": o["offer_key"],
            "offer_date": o["offer_date"],
            "type": o["offer_type"],
            "type_basis": o["offer_type_basis"],
            "type_raw": o["offer_type_raw"],
            "status": o["status"],
            "is_offer_or_redemption": bool(o["is_offer_or_redemption"]),
            "window_start": o["offer_date_start"],
            "window_end": o["offer_date_end"],
            "price_pct": o["price"],
            "value_per_bond": o["value"],
            "face_value": o["face_value"],
            "face_unit": o["face_unit"],
            "agent": o["agent"],
            "evidence_document_url": _document_url(
                documented.get((o["secid"], "call", o["offer_key"]))
                or documented.get((o["secid"], "offer", o["offer_key"]))
            )
            if o["source"] == "curated"
            else None,
            "labels": sorted_labels(
                "date_missing" if o["date_missing"] else None,
                undated_offer_reason(o, cut.business_date) if o["date_missing"] else None,
            ),
        }
        for o in offers
    ]
    prov.add_rows("offers", offers)
    curated_formula = {
        r["secid"]: r for r in formula_rows if r["source"] == "curated" and r["evidence"]
    }
    formula_out = [
        {
            "secid": f["secid"],
            "benchmark": f["benchmark"],
            "benchmark_raw": f["benchmark_raw"],
            "benchmark_source": f["benchmark_source"],
            "spread_bp": f["spread_bp"],
            "multiplier": f["multiplier"],
            "lag_days": f["lag_days"],
            "averaging": f["averaging"],
            "floor_pct": f["floor_pct"],
            "cap_pct": f["cap_pct"],
            "variable_rate_type": f["variable_rate_type"],
            "fixing_day": f["fixing_day"],
            "status": f["status"],
            "sources": list(f["sources"] or []),
            "evidence_document_url": _document_url(curated_formula.get(f["secid"])),
            "evidence_page": _evidence_field(curated_formula.get(f["secid"]), "page"),
        }
        for f in formulas
    ]
    prov.add_rows("floater_formulas", formula_rows)
    issuer_out = [
        {
            "issuer_id": i["issuer_id"],
            "inn": i["inn"],
            "ogrn": i["ogrn"],
            "name": i["name"],
            "short_name": i["short_name"],
            "sector": i["sector"],
            "sector_source": i["sector_source"] if i["sector"] is not None else None,
            "okved": i["okved"],
            "okved_version": i["okved_version"],
            "country": i["country"],
            "is_sovereign": bool(i["is_sovereign"]),
            "group_id": i["group_id"],
            "group_name": i["group_name"],
            "group_relation": i["group_relation"],
            "is_state_owned": i["is_state_owned"],
        }
        for i in issuers
    ]
    prov.add_rows("issuers", issuers)
    issuer_of = {b["secid"]: b["issuer_id"] for b in bonds}
    ratings_out = sorted(
        [
            _rating(r, "issue", r["secid"],
                    r["issuer_id"] if r["issuer_id"] is not None else issuer_of[r["secid"]])
            for r in rating_rows
        ]
        + [_rating(r, "issuer", None, r["issuer_id"]) for r in issuer_rating_rows],
        key=_rating_key,
    )  # fmt: skip
    prov.add_rows("ratings", [*rating_rows, *issuer_rating_rows])
    for r in cal_rows:
        prov.add("calendars", r["source"], r["run_id"], r["ingested_at"])

    secids_of_isin: dict[str, set[str]] = defaultdict(set)
    for r in isin_rows:
        secids_of_isin[r["isin"]].add(r["secid"])
    id_map = [
        {
            "secid": b["secid"],
            "isin": b["isin"],
            "regnumber": b["regnumber"],
            "isin_shared_with": sorted(secids_of_isin.get(b["isin"], set()) - {b["secid"]})
            if b["isin"]
            else [],
        }
        for b in bonds
    ]
    prov.add_rows("id_map", bonds)

    missing_cals = sorted(set(CALENDARS) - {c["calendar"] for c in calendars})
    datasets = [
        Dataset(
            "instruments",
            instruments,
            {
                "universe": "status active, or matured with maturity_date >= since, or "
                "delisted and alive (perpetual or maturity_date >= business_date)"
                + (", or quoted in the linked market cut" if quoted_secids else ""),
                "since": since,
                "by_coupon_type": dict(sorted(Counter(b["coupon_type"] for b in bonds).items())),
                "by_status": dict(sorted(Counter(b["status"] for b in bonds).items())),
                "labels": dict(sorted(label_counts.items())),
            },
        ),
        Dataset(
            "coupon_periods",
            coupon_rows,
            {"by_status": dict(sorted(Counter(c["status"] for c in coupons).items()))},
        ),
        Dataset("redemptions", redemptions),
        Dataset("offers", offer_rows),
        Dataset(
            "floater_formulas",
            formula_out,
            {
                "by_status": dict(sorted(Counter(f["status"] for f in formulas).items())),
                "floaters_without_formula": label_counts.get("floater_formula_missing", 0),
                **_documented_coverage(bonds, formulas, scheduled_calls),
            },
        ),
        Dataset("issuers", issuer_out, _issuer_coverage(issuer_out, bonds, issuers)),
        Dataset("ratings", ratings_out, _rating_coverage(ratings_out, bonds)),
        Dataset(
            "calendars",
            calendars,
            {"missing": missing_cals},
            [f"calendar {c} has no rows" for c in missing_cals],
        ),
        Dataset(
            "id_map",
            id_map,
            {"ambiguous_isin": sum(1 for r in id_map if r["isin_shared_with"])},
        ),
    ]
    return Build(
        cut=cut,
        datasets=datasets,
        calendar=cal_summary,
        parameters={
            "universe": "active_or_recently_matured_or_alive_or_quoted"
            if quoted_secids
            else "active_or_recently_matured_or_alive",
            "matured_lookback_days": matured_lookback_days,
            "bond_calendar": BOND_CALENDAR,
        },
        provenance=prov,
        excluded=Excluded(),
        universe=universe,
    )
