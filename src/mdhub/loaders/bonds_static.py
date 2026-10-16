"""Bond static / reference data for every MOEX bond -> ``ref.*`` (DESIGN §4, §6).

Jobs
----
``bonds_static(mode="full")`` (daily 06:30): the whole universe from bulk ISS endpoints

1. directory ``/securities.json`` (stock_bonds, ~160 pages: SECID, ISIN, issuer, is_traded,
   primary board) -> ``ref.issuer``;
2. collection ``stock_bonds_all`` (~12 pages: descriptions in bulk, current MOEX labels);
3. bonds market list (1 call: every board row with lot size, min step, settlement);
4. per-SECID ``/securities/{secid}.json`` only for gaps: traded SECIDs missing from the
   collection (EQRP structured notes ...) that were never described (all) or whose last
   description is older than ``refresh_days`` (``refresh_limit`` per run, oldest first), plus a
   ``backlog_limit`` of never-described archived SECIDs per run. Between refreshes the latest
   stored description is re-parsed from ``ops.raw_capture`` (DESIGN "raw first");
5. bulk bondization (~2,200 requests in date windows, strict cursor/TOTAL validation)
   -> coupons, amortizations, offers of the whole market; per-SECID bondization for
   active bonds without any schedule rows (``gap_limit``);
6. one transaction: upsert issuers, bonds, boards, schedules, MOEX floater formulas and the
   ``ref.static_snapshot`` observation log; schedule rows that disappeared from the complete
   snapshot of a SECID get ``is_deleted``; boards no longer listed get ``listed = false``.

Raw captures are committed at the end of every fetch phase (1-5; the bulk bondization crawl
as a whole, so a failed crawl still leaves no partial captures), so a failure in a later phase
or in the write phase keeps what was fetched; only the write phase (6) is one transaction.

At ``MDHUB_ISS_RPS=3`` a run takes ~13-14 minutes (bondization ~11.5); the first run also
describes the ~850 traded SECIDs outside the collection (~5 more minutes). If the bulk
bondization crawl is inconsistent (TOTAL changed, short pages), schedules are left untouched
and the run ends ``partial``.

``bonds_static_probe(mode="probe")`` (hourly 09:05-19:05): collection + market list; SECIDs
not yet in ``ref.bond`` and SECIDs with ``STARTDATEMOEX >= today`` are loaded individually
(directory search, description, bondization) and written with the same rules.

Status: ``matured`` if ``maturity_date < today`` (MSK); ``active`` if MOEX lists it on a board
(directory ``is_traded`` or present in the market list); otherwise ``delisted``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import psycopg
from psycopg.rows import tuple_row

from mdhub.core.capture import load_capture_payload, record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, HttpError
from mdhub.core.iss import ISS_BASE_URL, IssPagingError
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import UpsertResult, atomic, touch_last_seen, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.sources import moex_static as ms
from mdhub.sources.moex_static import (
    AmortRow,
    BondizationData,
    ClassifyInput,
    Description,
    OptionDates,
)

__all__ = ["PARSER_VERSION", "Universe", "bonds_static", "bonds_static_probe", "write_universe"]

log = logging.getLogger(__name__)

PARSER_VERSION: Final = ms.PARSER_VERSION
SOURCE: Final = "moex_iss"

EP_DIRECTORY: Final = "bonds_directory"
EP_COLLECTION: Final = "bonds_collection"
EP_MARKET: Final = "bonds_market"
EP_DESCRIPTION: Final = "bond_description"
EP_BONDIZATION: Final = "bonds_bondization"
EP_BONDIZATION_ONE: Final = "bond_bondization"

register_dataset(
    DatasetSpec(
        name="ref_bond_static",
        table="ref.static_snapshot",
        date_column="last_seen",
        calendar="MOEX_STOCK",
        frequency="event",
        max_lag_days=2,
        where="source = 'moex_iss' AND endpoint = 'directory'",
        description="Bond static: last observation of the MOEX bond directory (bonds_static full)",
    )
)
register_dataset(
    DatasetSpec(
        name="ref_bond_schedules",
        table="ref.static_snapshot",
        date_column="last_seen",
        calendar="MOEX_STOCK",
        frequency="event",
        max_lag_days=2,
        where="source = 'moex_iss' AND endpoint = 'bondization'",
        description="Bond schedules: last complete MOEX bondization observation",
    )
)


# ------------------------------------------------------------------------------ model


@dataclass(slots=True)
class Universe:
    """Everything fetched in one run, before writing (parsed rows + capture ids)."""

    directory: dict[str, dict[str, Any]] = field(default_factory=dict)
    collection: dict[str, dict[str, Any]] = field(default_factory=dict)
    market: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    market_trade_date: date | None = None
    market_boards: set[str] = field(default_factory=set)
    descriptions: dict[str, Description] = field(default_factory=dict)
    description_fresh: dict[str, tuple[int, str]] = field(default_factory=dict)
    """secid -> (capture_id, stable content hash) of descriptions fetched in this run."""
    bondization: BondizationData = field(default_factory=BondizationData)
    schedule_secids: set[str] = field(default_factory=set)
    """SECIDs whose complete schedule snapshot was fetched in this run."""
    bondization_complete: bool = False
    captures: dict[tuple[str, str], int] = field(default_factory=dict)
    """(endpoint, secid) -> capture_id of the page holding that SECID's row."""


@dataclass(frozen=True, slots=True)
class ScheduleSummary:
    coupon_values: tuple[bool, ...] = ()
    has_amortization: bool = False
    maturity_row_date: date | None = None
    face_value: Decimal | None = None
    initial_face_value: Decimal | None = None
    face_unit: str | None = None
    isin: str | None = None
    name: str | None = None


# ------------------------------------------------------------------------------- jobs


@job("bonds_static", "MOEX bond static: directory, descriptions, boards, bondization -> ref.*")
def bonds_static(
    ctx: JobContext,
    *,
    mode: str = "full",
    refresh_days: int = 7,
    refresh_limit: int = 150,
    backlog_limit: int = 100,
    gap_limit: int = 100,
    bondization: bool = True,
    workers: int = 3,
) -> None:
    """Full refresh of the MOEX bond universe (see module docstring)."""
    if mode != "full":
        raise ValueError(f"bonds_static: unknown mode {mode!r} (expected 'full')")
    today = ctx.today()
    u = Universe()
    with ctx.http(base_url=ISS_BASE_URL) as http, ctx.connection() as conn:
        # Raw first: the captures of each fetch phase are committed when the phase ends, so a
        # failure later (or in the write phase) does not discard them; the normalized rows are
        # written afterwards in ONE transaction (schedules stay atomic per run).
        _fetch_directory(ctx, conn, http, u)
        conn.commit()
        _fetch_collection(ctx, conn, http, u)
        _fetch_market(ctx, conn, http, u)
        conn.commit()
        _fetch_descriptions(
            ctx,
            conn,
            http,
            u,
            refresh_days=refresh_days,
            refresh_limit=refresh_limit,
            backlog_limit=backlog_limit,
            workers=workers,
        )
        conn.commit()
        if bondization:
            try:
                _fetch_bondization_bulk(ctx, conn, http, u, workers=workers)
            except (IssPagingError, HttpError) as exc:
                ctx.logger.warning("bulk bondization failed: %s", exc)
                ctx.run.mark_partial(f"bulk bondization incomplete, schedules not updated: {exc}")
                u.bondization = BondizationData()
            else:
                conn.commit()
                _fetch_bondization_gaps(
                    ctx, conn, http, u, today, gap_limit=gap_limit, workers=workers
                )
            conn.commit()
        scope = (
            set(u.directory)
            | set(u.collection)
            | set(u.market)
            | set(u.descriptions)
            | u.schedule_secids
        )
        write_universe(ctx, conn, u, today, scope=scope, full=True)


@job("bonds_static_probe", "MOEX bond static: hourly probe for new listings")
def bonds_static_probe(ctx: JobContext, *, mode: str = "probe", max_new: int = 50) -> None:
    """Load SECIDs that appeared since the last full run (or start trading today or later)."""
    if mode != "probe":
        raise ValueError(f"bonds_static_probe: unknown mode {mode!r} (expected 'probe')")
    today = ctx.today()
    u = Universe()
    with ctx.http(base_url=ISS_BASE_URL) as http, ctx.connection() as conn:
        _fetch_collection(ctx, conn, http, u)
        _fetch_market(ctx, conn, http, u)
        with conn.cursor(row_factory=tuple_row) as cur:
            known = {r[0] for r in cur.execute("SELECT secid FROM ref.bond").fetchall()}
        new = sorted((set(u.collection) | set(u.market)) - known)
        upcoming = sorted(
            s
            for s, row in u.collection.items()
            if s in known and row.get("STARTDATEMOEX") is not None and row["STARTDATEMOEX"] >= today
        )
        candidates = (new + upcoming)[: max(0, max_new)]
        skipped = len(new) + len(upcoming) - len(candidates)
        if skipped > 0:
            ctx.run.mark_partial(f"probe: {skipped} candidate SECIDs left for the next run")
        ctx.logger.info(
            "probe: %d new, %d upcoming, loading %d", len(new), len(upcoming), len(candidates)
        )
        if not candidates:
            return
        loaded: set[str] = set()
        for secid in candidates:
            try:
                _fetch_one(ctx, conn, http, u, secid)
            except (HttpError, FormatDriftError) as exc:
                ctx.logger.warning("probe %s failed: %s", secid, exc)
                ctx.run.mark_partial(f"{secid}: {exc}")
                continue
            loaded.add(secid)
        if loaded:
            write_universe(ctx, conn, u, today, scope=loaded, full=False)


# ---------------------------------------------------------------------------- fetching


def _capture(ctx: JobContext, conn: psycopg.Connection[Any], endpoint: str, response: Any) -> int:
    return record_capture(conn, ctx.run.id, SOURCE, endpoint, None, response, PARSER_VERSION)


def _fetch_directory(
    ctx: JobContext, conn: psycopg.Connection[Any], http: HttpClient, u: Universe
) -> None:
    for page in ms.iter_directory_pages(http):
        cap = _capture(ctx, conn, EP_DIRECTORY, page.response)
        for row in ms.parse_directory_rows(page.rows):
            u.directory[row["secid"]] = row
            u.captures[(EP_DIRECTORY, row["secid"])] = cap
    if not u.directory:
        raise FormatDriftError("bond directory is empty")
    ctx.logger.info("directory: %d SECIDs", len(u.directory))


def _fetch_collection(
    ctx: JobContext, conn: psycopg.Connection[Any], http: HttpClient, u: Universe
) -> None:
    for page in ms.iter_collection_pages(http):
        cap = _capture(ctx, conn, EP_COLLECTION, page.response)
        for row in ms.parse_collection_rows(page.rows):
            u.collection[row["SECID"]] = row
            u.captures[(EP_COLLECTION, row["SECID"])] = cap
    if not u.collection:
        raise FormatDriftError("stock_bonds_all collection is empty")
    ctx.logger.info("collection: %d SECIDs", len(u.collection))


def _fetch_market(
    ctx: JobContext, conn: psycopg.Connection[Any], http: HttpClient, u: Universe
) -> None:
    response = ms.fetch_market(http)
    cap = _capture(ctx, conn, EP_MARKET, response)
    market = ms.parse_market(response.json())
    if not market.rows:
        raise FormatDriftError("bonds market list is empty")
    u.market_trade_date = market.trade_date
    for row in market.rows:
        u.market.setdefault(row["SECID"], []).append(row)
        u.market_boards.add(row["BOARDID"])
        u.captures[(EP_MARKET, row["SECID"])] = cap
    for rows in u.market.values():
        rows.sort(key=lambda r: str(r["BOARDID"]))
    ctx.logger.info("market: %d rows, %d SECIDs", len(market.rows), len(u.market))


def _description_hash(payload: Mapping[str, Any]) -> str:
    view: dict[str, Any] = {}
    desc = payload.get("description")
    if isinstance(desc, Mapping):
        cols = desc.get("columns") or []
        rows = [dict(zip(cols, r, strict=False)) for r in desc.get("data") or []]
        view["description"] = sorted(
            (
                (str(r.get("name")), r.get("value"))
                for r in rows
                if str(r.get("name") or "").upper() not in ms.VOLATILE_FIELDS
            ),
            key=lambda t: t[0],
        )
    boards = payload.get("boards")
    if isinstance(boards, Mapping):
        cols = boards.get("columns") or []
        view["boards"] = sorted(
            (ms.stable_view(dict(zip(cols, r, strict=False))) for r in boards.get("data") or []),
            key=lambda r: str(r.get("boardid")),
        )
    return ms.canonical_sha256(view)


def _try_fetch(
    fn: Callable[[HttpClient, str], httpx.Response], http: HttpClient, secid: str
) -> httpx.Response | HttpError:
    """Run in worker threads: HTTP errors are returned, not raised (handled per SECID)."""
    try:
        return fn(http, secid)
    except HttpError as exc:
        return exc


def _store_description(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    u: Universe,
    secid: str,
    fetched: httpx.Response | HttpError | None = None,
) -> bool:
    response = fetched if fetched is not None else _try_fetch(ms.fetch_description, http, secid)
    if isinstance(response, HttpError):
        ctx.run.mark_partial(f"description {secid}: {response}")
        return False
    cap = _capture(ctx, conn, EP_DESCRIPTION, response)
    payload = response.json()
    try:
        desc = ms.parse_description(payload, secid)
    except FormatDriftError as exc:
        ctx.run.mark_partial(f"description {secid}: {exc}")
        return False
    if not desc.fields:
        ctx.run.mark_partial(f"description {secid}: empty")
        return False
    u.descriptions[secid] = desc
    u.description_fresh[secid] = (cap, _description_hash(payload))
    return True


def _fetch_descriptions(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    u: Universe,
    *,
    refresh_days: int,
    refresh_limit: int,
    backlog_limit: int,
    workers: int = 1,
) -> None:
    latest = _latest_descriptions(conn)
    stale_before = utc_now() - timedelta(days=refresh_days)
    outside = [s for s in sorted(u.directory) if s not in u.collection]
    traded = [s for s in outside if u.directory[s].get("is_traded")]
    must = [s for s in traded if s not in latest]
    refresh = sorted(
        (s for s in traded if s in latest and latest[s][1] < stale_before),
        key=lambda s: latest[s][1],
    )[: max(0, refresh_limit)]
    backlog = [s for s in outside if s not in latest and not u.directory[s].get("is_traded")]
    backlog = backlog[: max(0, backlog_limit)]
    fetch = must + refresh + backlog
    ctx.logger.info(
        "descriptions: %d new traded, %d refresh, %d backlog", len(must), len(refresh), len(backlog)
    )
    results = ms.ordered_map(
        lambda sec: (sec, _try_fetch(ms.fetch_description, http, sec)), fetch, workers=workers
    )
    for secid, response in results:
        _store_description(ctx, conn, http, u, secid, response)
    # Between refreshes: re-parse the latest stored description of every described SECID.
    for secid, (capture_id, _seen) in latest.items():
        if secid in u.descriptions:
            continue
        try:
            payload = _json_payload(conn, capture_id)
            u.descriptions[secid] = ms.parse_description(payload, secid)
        except (FormatDriftError, KeyError, ValueError) as exc:
            ctx.logger.warning("stored description %s (capture %d): %s", secid, capture_id, exc)


def _json_payload(conn: psycopg.Connection[Any], capture_id: int) -> Mapping[str, Any]:
    import json

    data = json.loads(load_capture_payload(conn, capture_id))
    if not isinstance(data, Mapping):
        raise FormatDriftError(f"capture {capture_id}: not a JSON object")
    return data


def _latest_descriptions(conn: psycopg.Connection[Any]) -> dict[str, tuple[int, datetime]]:
    """secid -> (capture_id, last_seen) of the most recent stored description."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT DISTINCT ON (secid) secid, capture_id, last_seen
            FROM ref.static_snapshot
            WHERE source = %s AND endpoint = 'description' AND capture_id IS NOT NULL
            ORDER BY secid, last_seen DESC NULLS LAST, first_seen_at DESC
            """,
            (SOURCE,),
        ).fetchall()
    epoch = datetime.fromtimestamp(0, tz=utc_now().tzinfo)
    return {str(s): (int(c), lst or epoch) for s, c, lst in rows}


def _fetch_bondization_bulk(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    u: Universe,
    *,
    workers: int = 1,
) -> None:
    data = BondizationData()
    pages = 0
    with atomic(conn):  # a failed crawl leaves no partial captures behind
        for page in ms.iter_bondization_pages(http, workers=workers):
            cap = _capture(ctx, conn, EP_BONDIZATION, page.response)
            data.extend(ms.parse_bondization_page(page, capture_id=cap))
            pages += 1
            if pages % 250 == 0:
                ctx.logger.info("bondization: %d pages (totals %s)", pages, page.totals)
    u.bondization = data
    u.schedule_secids = data.secids()
    u.bondization_complete = True
    ctx.logger.info("bondization: %d pages, %d SECIDs", pages, len(u.schedule_secids))


def _fetch_bondization_one(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    u: Universe,
    secid: str,
    fetched: httpx.Response | HttpError | None = None,
) -> None:
    response = fetched if fetched is not None else ms.fetch_bondization_one(http, secid)
    if isinstance(response, HttpError):
        raise response
    cap = _capture(ctx, conn, EP_BONDIZATION_ONE, response)
    data = ms.parse_bondization_one(response.json(), secid, capture_id=cap)
    for name in ("coupons", "amortizations", "offers"):
        getattr(u.bondization, name).pop(secid, None)
    u.bondization.raw.pop(secid, None)
    u.bondization.extend(data)
    u.schedule_secids.add(secid)


def _fetch_bondization_gaps(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    u: Universe,
    today: date,
    *,
    gap_limit: int,
    workers: int = 1,
) -> None:
    active = {s for s, d in u.directory.items() if d.get("is_traded")} | set(u.market)
    gaps = sorted(active - u.schedule_secids)[: max(0, gap_limit)]
    ctx.logger.info("bondization gaps: %d active SECIDs without schedule rows", len(gaps))
    results = ms.ordered_map(
        lambda sec: (sec, _try_fetch(ms.fetch_bondization_one, http, sec)), gaps, workers=workers
    )
    for secid, response in results:
        try:
            _fetch_bondization_one(ctx, conn, http, u, secid, response)
        except (HttpError, FormatDriftError) as exc:
            ctx.run.mark_partial(f"bondization {secid}: {exc}")


def _fetch_one(
    ctx: JobContext, conn: psycopg.Connection[Any], http: HttpClient, u: Universe, secid: str
) -> None:
    """Probe: directory search + description + bondization of one SECID."""
    response = ms.fetch_directory_one(http, secid)
    cap = _capture(ctx, conn, EP_DIRECTORY, response)
    for row in ms.parse_directory_payload(response.json()):
        if row["secid"] == secid:
            u.directory[secid] = row
            u.captures[(EP_DIRECTORY, secid)] = cap
    _store_description(ctx, conn, http, u, secid)
    _fetch_bondization_one(ctx, conn, http, u, secid)


# --------------------------------------------------------------------------- building


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _schedule_summary(u: Universe, secid: str) -> ScheduleSummary | None:
    coupons = u.bondization.coupons.get(secid, [])
    amorts = u.bondization.amortizations.get(secid, [])
    if secid not in u.schedule_secids:
        return None
    numbered = ms.number_coupons(coupons)
    maturity_rows = [a.amort_date for a in amorts if a.data_source == "maturity"]
    last_coupon = numbered[-1][1] if numbered else None
    last_amort = max(amorts, key=lambda a: a.amort_date) if amorts else None
    return ScheduleSummary(
        coupon_values=tuple(
            any(v is not None and v != 0 for v in (r.value, r.value_prc)) for _, r in numbered
        ),
        has_amortization=any(a.data_source == "amortization" for a in amorts),
        maturity_row_date=max(maturity_rows) if maturity_rows else None,
        face_value=_first(
            last_coupon.face_value if last_coupon else None,
            last_amort.face_value if last_amort else None,
        ),
        initial_face_value=_first(
            last_coupon.initial_face_value if last_coupon else None,
            last_amort.initial_face_value if last_amort else None,
        ),
        face_unit=_first(
            last_coupon.face_unit if last_coupon else None,
            last_amort.face_unit if last_amort else None,
        ),
        isin=last_coupon.isin if last_coupon else None,
        name=last_coupon.name if last_coupon else None,
    )


def _db_schedule_summaries(
    conn: psycopg.Connection[Any], secids: Sequence[str]
) -> dict[str, ScheduleSummary]:
    """Summaries from stored schedules (used when this run has no schedule for a SECID)."""
    out: dict[str, ScheduleSummary] = {}
    if not secids:
        return out
    with conn.cursor(row_factory=tuple_row) as cur:
        coupons = cur.execute(
            """
            SELECT secid, array_agg((COALESCE(amount, 0) <> 0 OR COALESCE(value_prc, 0) <> 0)
                                    ORDER BY coupon_no)
            FROM ref.bond_coupon WHERE NOT is_deleted AND secid = ANY(%s) GROUP BY secid
            """,
            (list(secids),),
        ).fetchall()
        amorts = cur.execute(
            """
            SELECT secid, bool_or(data_source = 'amortization'),
                   max(amort_date) FILTER (WHERE data_source = 'maturity')
            FROM ref.bond_amortization WHERE NOT is_deleted AND secid = ANY(%s) GROUP BY secid
            """,
            (list(secids),),
        ).fetchall()
    cv = {str(s): tuple(bool(x) for x in vals) for s, vals in coupons}
    am = {str(s): (bool(h), m) for s, h, m in amorts}
    for s in set(cv) | set(am):
        has_am, mat = am.get(s, (False, None))
        out[s] = ScheduleSummary(
            coupon_values=cv.get(s, ()), has_amortization=has_am, maturity_row_date=mat
        )
    return out


def _flag(name: str, coll: Mapping[str, Any] | None, desc: Mapping[str, Any] | None) -> bool | None:
    """MOEX flag: collection/description omit unset flags -> False when a source exists."""
    if coll is not None:
        return bool(coll.get(name))
    if desc is not None:
        return bool(desc.get(name))
    return None


def _primary_market_row(
    rows: Sequence[Mapping[str, Any]], primary: str | None
) -> Mapping[str, Any] | None:
    if not rows:
        return None
    for r in rows:
        if r["BOARDID"] == primary:
            return r
    return rows[0]


@dataclass(slots=True)
class BondFacts:
    """Per-bond derived values needed to build schedule rows."""

    coupon_type: str
    benchmark: str | None
    face_indexed: bool
    face_unit: str | None
    options: OptionDates


def build_bond_row(
    secid: str,
    u: Universe,
    today: date,
    summary: ScheduleSummary | None,
    run_id: int,
) -> tuple[dict[str, Any], BondFacts]:
    """Merge directory / collection / description / market / schedule into a ``ref.bond`` row.

    Precedence (per field): collection > per-SECID description > directory > market list
    (primary board) > bondization (stubs and archived bonds only).
    """
    d = u.directory.get(secid)
    c = u.collection.get(secid)
    desc_obj = u.descriptions.get(secid)
    e = desc_obj.fields if desc_obj else None
    mrows = u.market.get(secid, [])

    desc_primary = None
    if desc_obj:
        desc_primary = next((b["board"] for b in desc_obj.boards if b["is_primary"]), None)
    primary_board = _first(
        d.get("primary_boardid") if d else None,
        desc_primary,
        mrows[0]["BOARDID"] if len(mrows) == 1 else None,
    )
    pm = _primary_market_row(mrows, primary_board)
    desc_primary_ccy = None
    if desc_obj:
        desc_primary_ccy = next(
            (b["currency_id"] for b in desc_obj.boards if b["board"] == primary_board), None
        )

    def lab(name: str, market_name: str | None = None) -> Any:
        mval = pm.get(market_name) if pm is not None and market_name else None
        return _first(c.get(name) if c else None, e.get(name) if e else None, mval)

    bond_type = lab("BOND_TYPE", "BONDTYPE")
    bond_subtype = lab("BOND_SUBTYPE", "BONDSUBTYPE")
    coupon_details = lab("COUPON_DETAILS", "COUPON_DETAILS")
    facevalue_type = lab("FACEVALUE_TYPE", "FACEVALUE_TYPE")
    benchmark_code = lab("COUPON_BENCHMARK")
    spread = lab("COUPON_BENCHMARK_SPREAD")
    face_unit = _first(
        ms.norm_ccy(lab("FACEUNIT")),
        pm.get("FACEUNIT") if pm else None,
        summary.face_unit if summary else None,
    )
    # Perpetual: MOEX subtype 'Бессрочные', or an AT1-style 'До оферты (call)' bond without
    # MATDATE and without a redemption row in its complete schedule.
    is_perpetual = bond_subtype == ms.PERPETUAL_SUBTYPE or (
        bond_subtype == "До оферты (call)"
        and lab("MATDATE", "MATDATE") is None
        and summary is not None
        and summary.maturity_row_date is None
    )
    maturity = None
    if not is_perpetual:
        maturity = _first(lab("MATDATE", "MATDATE"), summary.maturity_row_date if summary else None)
    is_matured = maturity is not None and maturity < today
    has_description = c is not None or e is not None
    sec_type = _first(d.get("type") if d else None, lab("TYPE"))
    name = _first(
        lab("NAME"),
        d.get("name") if d else None,
        pm.get("SECNAME") if pm else None,
        summary.name if summary else None,
    )
    short_name = _first(
        lab("SHORTNAME"), d.get("shortname") if d else None, pm.get("SHORTNAME") if pm else None
    )
    struct_flag = _flag("STRUCTBOND", c, e)
    inp = ClassifyInput(
        secid=secid,
        name=name,
        short_name=short_name,
        sec_type=sec_type,
        issuer_inn=d.get("emitent_inn") if d else None,
        issuer_name=d.get("emitent_title") if d else None,
        face_unit=face_unit,
        bond_type=bond_type,
        bond_subtype=bond_subtype,
        coupon_details=coupon_details,
        facevalue_type=facevalue_type,
        coupon_benchmark=benchmark_code,
        struct_flag=struct_flag,
        has_description=has_description,
        is_matured=is_matured,
        coupon_values=summary.coupon_values if summary else (),
    )
    bond_class = ms.classify_bond(inp)
    coupon_type, basis = ms.classify_coupon_type(inp, bond_class)
    labels_known = bond_type is not None or facevalue_type is not None
    face_indexed = facevalue_type == ms.FACE_INDEXED or "линкер" in (bond_type or "").lower()
    amort_flag = _flag("AMORTBOND", c, e)
    if amort_flag or (summary is not None and summary.has_amortization):
        is_amortizing: bool | None = True
    elif amort_flag is not None or summary is not None:
        is_amortizing = False
    else:
        is_amortizing = None
    in_market = bool(mrows)
    is_traded = _first(d.get("is_traded") if d else None, c.get("IS_TRADED") if c else None)
    if is_matured:
        status = "matured"
    elif is_traded or in_market:
        status = "active"
    else:
        status = "delisted"
    issuer_id = _first(d.get("emitent_id") if d else None, e.get("EMITTER_ID") if e else None)
    capture_id = _first(
        u.captures.get((EP_COLLECTION, secid)),
        u.description_fresh.get(secid, (None, None))[0],
        u.captures.get((EP_DIRECTORY, secid)),
        u.captures.get((EP_MARKET, secid)),
    )
    row: dict[str, Any] = {
        "secid": secid,
        "isin": _first(
            d.get("isin") if d else None,
            lab("ISIN"),
            pm.get("ISIN") if pm else None,
            summary.isin if summary else None,
        ),
        "regnumber": _first(
            d.get("regnumber") if d else None, lab("REGNUMBER"), pm.get("REGNUMBER") if pm else None
        ),
        "issuer_id": issuer_id,
        "name": name,
        "short_name": short_name,
        "lat_name": _first(lab("LATNAME"), pm.get("LATNAME") if pm else None),
        "sec_type": sec_type,
        "type_name": lab("TYPENAME"),
        "sec_subtype": lab("SECSUBTYPE"),
        "bond_type": bond_type,
        "bond_subtype": bond_subtype,
        "coupon_details": coupon_details,
        "facevalue_type": facevalue_type,
        "bond_class": bond_class,
        "coupon_type": coupon_type,
        "coupon_type_basis": basis,
        "coupon_benchmark": benchmark_code,
        "coupon_benchmark_spread_pct": spread,
        "face_value": _first(
            lab("FACEVALUE", "FACEVALUE"), summary.face_value if summary else None
        ),
        "initial_face_value": _first(
            lab("INITIALFACEVALUE"), summary.initial_face_value if summary else None
        ),
        "face_unit": face_unit,
        "currency_id": _first(pm.get("CURRENCYID") if pm else None, desc_primary_ccy),
        "issue_date": lab("ISSUEDATE"),
        "maturity_date": maturity,
        "is_perpetual": is_perpetual,
        "start_date_moex": lab("STARTDATEMOEX"),
        "registry_date": lab("REGISTRY_DATE"),
        "coupon_frequency": lab("COUPONFREQUENCY"),
        "coupon_period_days": pm.get("COUPONPERIOD") if pm else None,
        "listing_level": lab("LISTLEVEL", "LISTLEVEL"),
        "issue_size": lab("ISSUESIZE", "ISSUESIZE"),
        "issue_size_placed": pm.get("ISSUESIZEPLACED") if pm else None,
        "issue_size_planned": lab("ISSUESIZEPLANNED"),
        "program_regnumber": lab("PROGRAMREGISTRYNUMBER"),
        "is_amortizing": is_amortizing,
        "is_subordinated": _flag("SUBORDBOND", c, e),
        "is_structured": (
            True
            if struct_flag or bond_type == ms.STRUCTURED_BOND_TYPE
            else (False if has_description else None)
        ),
        "is_convertible": (bond_type == ms.CONVERTIBLE_BOND_TYPE) if bond_type else None,
        "is_face_indexed": face_indexed if labels_known else None,
        "is_fx_face": (face_unit != "RUB") if face_unit else None,
        "qualified_only": _flag("ISQUALIFIEDINVESTORS", c, e),
        "qual_investor_group": lab("QUALINVESTORGROUP"),
        "high_risk": _flag("HIGHRISK", c, e),
        "primary_board": primary_board,
        "marketprice_board": d.get("marketprice_boardid") if d else None,
        "is_traded": is_traded,
        "in_directory": d is not None,
        "description_source": "collection" if c else ("security" if e else None),
        "status": status,
        "source": SOURCE,
        "run_id": run_id,
        "capture_id": capture_id,
    }
    facts = BondFacts(
        coupon_type=coupon_type,
        benchmark=ms.map_benchmark(benchmark_code, coupon_details),
        face_indexed=face_indexed,
        face_unit=face_unit,
        options=OptionDates(
            put=pm.get("PUTOPTIONDATE") if pm else None,
            call=pm.get("CALLOPTIONDATE") if pm else None,
            buyback=pm.get("BUYBACKDATE") if pm else None,
            bond_subtype=bond_subtype,
        ),
    )
    return row, facts


def _issuer_rows(u: Universe, scope: set[str], run_id: int) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    ordered = sorted(
        (d for s, d in u.directory.items() if s in scope and d.get("emitent_id") is not None),
        key=lambda d: (not d.get("is_traded"), d["secid"]),
    )
    for d in ordered:
        iid = int(d["emitent_id"])
        rec = by_id.setdefault(
            iid,
            {
                "issuer_id": iid,
                "name": None,
                "inn": None,
                "okpo": None,
                "capture_id": u.captures.get((EP_DIRECTORY, d["secid"])),
            },
        )
        for col, src in (
            ("name", "emitent_title"),
            ("inn", "emitent_inn"),
            ("okpo", "emitent_okpo"),
        ):
            if rec[col] is None and d.get(src) is not None:
                rec[col] = d[src]
    rows = []
    for rec in by_id.values():
        rows.append(
            {
                **rec,
                "is_sovereign": rec["inn"] == ms.MINFIN_INN,
                "source": SOURCE,
                "run_id": run_id,
            }
        )
    return rows


def _money_rub(
    value: Decimal | None, face_unit: str | None, when: date, today: date
) -> Decimal | None:
    """MOEX value_rub: kept for RUB bonds and for past dates; future FX rows are converted at
    today's FX rate by MOEX and would change every day -> NULL."""
    if face_unit in (None, "RUB") or when <= today:
        return value
    return None


def fixed_in_advance_secids(conn: psycopg.Connection[Any], secids: Sequence[str]) -> set[str]:
    """Floaters whose curated formula states a fixing before the period start (and no
    averaging): their current coupon published by MOEX is ``known``, not
    ``current_published``."""
    if not secids:
        return set()
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT secid FROM ref.bond_rate_formula
            WHERE source = 'curated' AND NOT is_deleted AND secid = ANY(%s)
              AND averaging IS NULL
              AND evidence -> 'formula' ? 'fixing_calendar_days_before_start'
              AND coalesce(benchmark, '') <> ALL(%s)
            """,
            (list(secids), sorted(ms.IN_ARREARS_BENCHMARKS)),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _schedule_rows(
    secid: str,
    u: Universe,
    facts: BondFacts,
    today: date,
    run_id: int,
    *,
    fixed_in_advance: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    numbered = ms.number_coupons(u.bondization.coupons.get(secid, []))
    current = ms.current_coupon_index(numbered, today)
    coupons: list[dict[str, Any]] = []
    for no, r in numbered:
        status = ms.coupon_status(
            r,
            coupon_type=facts.coupon_type,
            benchmark=facts.benchmark,
            is_current=no == current,
            face_indexed=facts.face_indexed,
            today=today,
            fixed_in_advance=fixed_in_advance,
        )
        has = status != "unknown"
        coupons.append(
            {
                "secid": secid,
                "coupon_no": no,
                "start_date": r.start_date,
                "pay_date": r.pay_date,
                "record_date": r.record_date,
                "amount": r.value if has else None,
                "value_rub": _money_rub(r.value_rub, r.face_unit, r.pay_date, today)
                if has
                else None,
                "value_prc": r.value_prc if has else None,
                "face_value": r.face_value,
                "face_unit": r.face_unit,
                "status": status,
                "is_deleted": False,
                "source": SOURCE,
                "run_id": run_id,
                "capture_id": r.capture_id,
            }
        )
    amorts: list[dict[str, Any]] = []
    seen: set[tuple[date, str]] = set()
    duplicates = 0
    for a in sorted(
        u.bondization.amortizations.get(secid, []),
        key=lambda a: (
            a.amort_date,
            a.data_source,
            a.value if a.value is not None else Decimal(-1),
        ),
    ):
        key = (a.amort_date, a.data_source)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        amorts.append(_amort_row(secid, a, today, run_id))
    offers = [
        {"secid": secid, "source": SOURCE, **o, "is_deleted": False, "run_id": run_id}
        for o in ms.normalize_offers(u.bondization.offers.get(secid, []), facts.options, today)
    ]
    return coupons, amorts, offers, duplicates


def _amort_row(secid: str, a: AmortRow, today: date, run_id: int) -> dict[str, Any]:
    return {
        "secid": secid,
        "amort_date": a.amort_date,
        "data_source": a.data_source,
        "value_prc": a.value_prc,
        "value": a.value,
        "value_rub": _money_rub(a.value_rub, a.face_unit, a.amort_date, today),
        "face_value": a.face_value,
        "initial_face_value": a.initial_face_value,
        "face_unit": a.face_unit,
        "is_deleted": False,
        "source": SOURCE,
        "run_id": run_id,
        "capture_id": a.capture_id,
    }


def _formula_row(bond: Mapping[str, Any], facts: BondFacts, run_id: int) -> dict[str, Any] | None:
    raw = bond["coupon_benchmark"] or (
        bond["coupon_details"] if bond["coupon_details"] in ms.COUPON_DETAILS_BENCHMARK else None
    )
    if facts.benchmark is None and facts.coupon_type != "floating":
        return None
    spread = bond["coupon_benchmark_spread_pct"]
    spread_bp = spread * 100 if spread is not None else None
    evidence = {
        "COUPON_BENCHMARK": bond["coupon_benchmark"],
        "COUPON_BENCHMARK_SPREAD": str(spread) if spread is not None else None,
        "COUPON_DETAILS": bond["coupon_details"],
        "BOND_TYPE": bond["bond_type"],
        "description_source": bond["description_source"],
    }
    return {
        "secid": bond["secid"],
        "source": SOURCE,
        "benchmark": facts.benchmark,
        "benchmark_raw": raw,
        "spread_bp": spread_bp,
        "multiplier": None,
        "lag_days": None,
        "averaging": None,
        "floor_pct": None,
        "cap_pct": None,
        "variable_rate_type": None,
        "fixing_day": None,
        "status": "inferred"
        if facts.benchmark is not None and spread_bp is not None
        else "partial",
        "evidence": evidence,
        "is_deleted": False,
        "run_id": run_id,
        "capture_id": bond["capture_id"],
    }


def _board_rows(
    secid: str,
    u: Universe,
    primary_board: str | None,
    ctx: JobContext,
    run_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[Any, ...]]]:
    """(market rows, description-only rows, settle observations).

    Market rows of a SECID whose description was fetched in this run also carry the
    description-only columns (``board_is_traded``, ``listed_from``); otherwise those columns
    are left out so the stored values stay untouched."""
    market_rows: list[dict[str, Any]] = []
    observations: list[tuple[Any, ...]] = []
    market_boards = set()
    cap_m = u.captures.get((EP_MARKET, secid))
    for r in u.market.get(secid, []):
        board = r["BOARDID"]
        market_boards.add(board)
        lag = ms.settle_lag(u.market_trade_date, r["SETTLEDATE"], ctx.is_business_day)
        market_rows.append(
            {
                "secid": secid,
                "board": board,
                "engine": "stock",
                "market": "bonds",
                "title": r["BOARDNAME"],
                "is_primary": board == primary_board,
                "market_status": r["STATUS"],
                "lot_size": r["LOTSIZE"],
                "min_step": r["MINSTEP"],
                "decimals": r["DECIMALS"],
                "currency_id": r["CURRENCYID"],
                "face_unit": r["FACEUNIT"],
                "settle_lag_days": lag,
                "settle_code": f"T{lag}" if lag is not None else None,
                "listed": True,
                "listing_source": "market",
                "source": SOURCE,
                "run_id": run_id,
                "capture_id": cap_m,
            }
        )
        observations.append(
            (secid, board, r["SETTLEDATE"], u.market_trade_date, r["FACEVALUEONSETTLEDATE"])
        )
    desc_only: list[dict[str, Any]] = []
    desc = u.descriptions.get(secid)
    if desc is not None and secid in u.description_fresh:
        cap_d = u.description_fresh[secid][0]
        by_board = {r["board"]: r for r in market_rows}
        for b in desc.boards:
            if b["board"] in market_boards:
                # market row wins; the fresh description only adds its own columns
                by_board[b["board"]]["board_is_traded"] = b["board_is_traded"]
                by_board[b["board"]]["listed_from"] = b["listed_from"]
                continue
            desc_only.append(
                {
                    "secid": secid,
                    "board": b["board"],
                    "engine": b["engine"],
                    "market": b["market"],
                    "title": b["title"],
                    "is_primary": b["board"] == primary_board,
                    "board_is_traded": b["board_is_traded"],
                    "decimals": b["decimals"],
                    "currency_id": b["currency_id"],
                    "listed_from": b["listed_from"],
                    "listed": True,
                    "listing_source": "description",
                    "source": SOURCE,
                    "run_id": run_id,
                    "capture_id": cap_d,
                }
            )
    return market_rows, desc_only, observations


# ---------------------------------------------------------------------------- writing


def _upsert(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> UpsertResult:
    result = UpsertResult()
    for chunk_start in range(0, len(rows), 50_000):
        chunk = rows[chunk_start : chunk_start + 50_000]
        result = result + upsert_rows(conn, table, chunk, key, update_cols, run_id=ctx.run.id)
    ctx.run.add_counts(result)
    return result


def _set_run(conn: psycopg.Connection[Any], run_id: int) -> None:
    conn.execute("SELECT set_config('mdhub.run_id', %s, true)", (str(run_id),))


def _retire(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    table: str,
    key_cols: Sequence[str],
    secids: Iterable[str],
    keep: set[tuple[Any, ...]],
    flag_sql: str,
    extra_where: str = "",
) -> int:
    """Set ``flag_sql`` on live rows of ``secids`` whose key is not in ``keep``."""
    secid_list = sorted(set(secids))
    if not secid_list:
        return 0
    cols = ", ".join(key_cols)
    with conn.cursor(row_factory=tuple_row) as cur:
        existing = cur.execute(
            f"SELECT {cols} FROM {table} WHERE secid = ANY(%s) {extra_where}",
            (secid_list,),
        ).fetchall()
    stale = [tuple(r) for r in existing if tuple(r) not in keep]
    if not stale:
        return 0
    _set_run(conn, ctx.run.id)
    where = " AND ".join(f"{c} = %s" for c in key_cols)
    with conn.cursor() as cur:
        cur.executemany(
            f"UPDATE {table} SET {flag_sql}, run_id = %s WHERE {where}",
            [(ctx.run.id, *k) for k in stale],
        )
    ctx.run.add_counts(UpsertResult(updated=len(stale)))
    return len(stale)


def write_universe(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    u: Universe,
    today: date,
    *,
    scope: set[str],
    full: bool,
) -> None:
    """Write everything in ``u`` for the SECIDs in ``scope`` (one transaction: the caller's)."""
    run_id = ctx.run.id
    missing = [s for s in scope if s not in u.schedule_secids]
    db_summaries = _db_schedule_summaries(conn, missing) if missing else {}

    # issuers
    _upsert(ctx, conn, "ref.issuer", _issuer_rows(u, scope, run_id), ["issuer_id"])

    # bonds
    bonds: list[dict[str, Any]] = []
    facts: dict[str, BondFacts] = {}
    for secid in sorted(scope):
        summary = _schedule_summary(u, secid) or db_summaries.get(secid)
        row, f = build_bond_row(secid, u, today, summary, run_id)
        bonds.append(row)
        facts[secid] = f
    _upsert(ctx, conn, "ref.bond", bonds, ["secid"])
    touch_last_seen(conn, "ref.bond", ["secid"], [(b["secid"],) for b in bonds], run_id)
    issuer_ids = {(b["issuer_id"],) for b in bonds if b["issuer_id"] is not None}
    touch_last_seen(conn, "ref.issuer", ["issuer_id"], issuer_ids, run_id)
    by_secid = {b["secid"]: b for b in bonds}

    # boards
    market_rows: list[dict[str, Any]] = []
    desc_only: list[dict[str, Any]] = []
    observations: list[tuple[Any, ...]] = []
    for secid in sorted(scope):
        m, d_only, obs = _board_rows(secid, u, by_secid[secid]["primary_board"], ctx, run_id)
        market_rows += m
        desc_only += d_only
        observations += obs
    by_shape: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in market_rows:
        by_shape.setdefault(tuple(r), []).append(r)
    for shape_rows in by_shape.values():
        _upsert(ctx, conn, "ref.bond_board", shape_rows, ["secid", "board"])
    _upsert(ctx, conn, "ref.bond_board", desc_only, ["secid", "board"])
    seen_boards = {(r["secid"], r["board"]) for r in market_rows + desc_only}
    touch_last_seen(conn, "ref.bond_board", ["secid", "board"], seen_boards, run_id)
    _observe_settlement(conn, observations)
    market_keys = {(r["secid"], r["board"]) for r in market_rows}
    if full:
        _retire(
            ctx,
            conn,
            "ref.bond_board",
            ["secid", "board"],
            _all_listed_secids(conn, "market"),
            market_keys,
            "listed = false",
            "AND listed AND listing_source = 'market'",
        )
    else:
        _retire(
            ctx,
            conn,
            "ref.bond_board",
            ["secid", "board"],
            scope,
            market_keys,
            "listed = false",
            "AND listed AND listing_source = 'market'",
        )
    desc_keys = {(r["secid"], r["board"]) for r in desc_only}
    _retire(
        ctx,
        conn,
        "ref.bond_board",
        ["secid", "board"],
        set(u.description_fresh) & scope,
        desc_keys,
        "listed = false",
        "AND listed AND listing_source = 'description'",
    )

    # schedules (complete snapshots only)
    sched_scope = sorted(u.schedule_secids & scope)
    coupons: list[dict[str, Any]] = []
    amorts: list[dict[str, Any]] = []
    offers: list[dict[str, Any]] = []
    duplicates = 0
    advance = fixed_in_advance_secids(conn, sched_scope)
    for secid in sched_scope:
        c, a, o, dup = _schedule_rows(
            secid, u, facts[secid], today, run_id, fixed_in_advance=secid in advance
        )
        coupons += c
        amorts += a
        offers += o
        duplicates += dup
    if duplicates:
        ctx.logger.warning("bondization: %d duplicate amortization rows ignored", duplicates)
    _upsert(ctx, conn, "ref.bond_coupon", coupons, ["secid", "coupon_no"])
    _upsert(ctx, conn, "ref.bond_amortization", amorts, ["secid", "amort_date", "data_source"])
    _upsert(ctx, conn, "ref.bond_offer", offers, ["secid", "source", "offer_key"])
    _retire(
        ctx,
        conn,
        "ref.bond_coupon",
        ["secid", "coupon_no"],
        sched_scope,
        {(r["secid"], r["coupon_no"]) for r in coupons},
        "is_deleted = true",
        "AND NOT is_deleted",
    )
    _retire(
        ctx,
        conn,
        "ref.bond_amortization",
        ["secid", "amort_date", "data_source"],
        sched_scope,
        {(r["secid"], r["amort_date"], r["data_source"]) for r in amorts},
        "is_deleted = true",
        "AND NOT is_deleted",
    )
    _retire(
        ctx,
        conn,
        "ref.bond_offer",
        ["secid", "source", "offer_key"],
        sched_scope,
        {(r["secid"], r["source"], r["offer_key"]) for r in offers},
        "is_deleted = true",
        f"AND NOT is_deleted AND source = '{SOURCE}'",
    )

    # MOEX floater formulas (only for bonds with labels from this run)
    formulas = [
        fr
        for b in bonds
        if b["description_source"] is not None
        and (fr := _formula_row(b, facts[b["secid"]], run_id)) is not None
    ]
    _upsert(ctx, conn, "ref.bond_rate_formula", formulas, ["secid", "source"])
    labelled = [b["secid"] for b in bonds if b["description_source"] is not None]
    _retire(
        ctx,
        conn,
        "ref.bond_rate_formula",
        ["secid", "source"],
        labelled,
        {(f["secid"], SOURCE) for f in formulas},
        "is_deleted = true",
        f"AND NOT is_deleted AND source = '{SOURCE}'",
    )

    _write_snapshots(ctx, conn, u, scope)
    ctx.logger.info(
        "written: %d bonds, %d boards, %d coupons, %d amortizations, %d offers, %d formulas",
        len(bonds),
        len(seen_boards),
        len(coupons),
        len(amorts),
        len(offers),
        len(formulas),
    )


def _all_listed_secids(conn: psycopg.Connection[Any], listing_source: str) -> list[str]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            "SELECT DISTINCT secid FROM ref.bond_board WHERE listed AND listing_source = %s",
            (listing_source,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _observe_settlement(conn: psycopg.Connection[Any], obs: Sequence[tuple[Any, ...]]) -> None:
    """Update the observation columns (history meta columns) without counting a data change."""
    if not obs:
        return
    secids, boards, sds, tds, fvs = (list(x) for x in zip(*obs, strict=True))
    conn.execute(
        """
        UPDATE ref.bond_board b
        SET settle_date = v.sd, settle_trade_date = v.td, face_value_on_settle_date = v.fv
        FROM unnest(%s::text[], %s::text[], %s::date[], %s::date[], %s::numeric[])
             AS v(secid, board, sd, td, fv)
        WHERE b.secid = v.secid AND b.board = v.board
          AND (b.settle_date, b.settle_trade_date, b.face_value_on_settle_date)
              IS DISTINCT FROM (v.sd, v.td, v.fv)
        """,
        (secids, boards, sds, tds, fvs),
    )


def _write_snapshots(
    ctx: JobContext, conn: psycopg.Connection[Any], u: Universe, scope: set[str]
) -> None:
    rows: list[dict[str, Any]] = []
    run_id = ctx.run.id
    for secid in sorted(scope):
        d = u.directory.get(secid)
        if d is not None:
            rows.append(
                _snap(
                    secid,
                    "directory",
                    ms.stable_view(d["_raw"]),
                    u.captures.get((EP_DIRECTORY, secid)),
                    run_id,
                )
            )
        c = u.collection.get(secid)
        if c is not None:
            rows.append(
                _snap(
                    secid,
                    "collection",
                    ms.stable_view(c["_raw"]),
                    u.captures.get((EP_COLLECTION, secid)),
                    run_id,
                )
            )
        m = u.market.get(secid)
        if m:
            rows.append(
                _snap(
                    secid,
                    "market",
                    [ms.stable_view(r["_raw"]) for r in m],
                    u.captures.get((EP_MARKET, secid)),
                    run_id,
                )
            )
        if secid in u.description_fresh:
            cap, digest = u.description_fresh[secid]
            rows.append(
                {
                    "secid": secid,
                    "source": SOURCE,
                    "endpoint": "description",
                    "payload_sha256": digest,
                    "capture_id": cap,
                    "run_id": run_id,
                }
            )
        if secid in u.schedule_secids:
            raw = u.bondization.raw.get(secid, {})
            caps = (
                [r.capture_id for r in u.bondization.coupons.get(secid, [])]
                + [r.capture_id for r in u.bondization.amortizations.get(secid, [])]
                + [r.capture_id for r in u.bondization.offers.get(secid, [])]
            )
            cap0 = next((c for c in caps if c is not None), None)
            view = {
                b: sorted(ms.canonical_sha256(r) for r in raw.get(b, []))
                for b in ms.BONDIZATION_BLOCKS
            }
            rows.append(_snap(secid, "bondization", view, cap0, run_id))
    key = ["secid", "source", "endpoint", "payload_sha256"]
    _upsert(ctx, conn, "ref.static_snapshot", rows, key, update_cols=[])
    touch_last_seen(
        conn,
        "ref.static_snapshot",
        key,
        [(r["secid"], r["source"], r["endpoint"], r["payload_sha256"]) for r in rows],
        run_id,
    )


def _snap(
    secid: str, endpoint: str, view: Any, capture_id: int | None, run_id: int
) -> dict[str, Any]:
    return {
        "secid": secid,
        "source": SOURCE,
        "endpoint": endpoint,
        "payload_sha256": ms.canonical_sha256(view),
        "capture_id": capture_id,
        "run_id": run_id,
    }
