"""Credit ratings from the agencies' public disclosure (WEB-18, review P1-1, TDM brief HS5).

Job ``ratings_agencies`` (daily 06:50 MSK, after ``bonds_static``; before ``issuers_static``,
which reads the agencies' industries for sectors, and the 07:45 static snapshot):

* **НКР** (:mod:`mdhub.sources.nkr`): the two list exports (issues, issuers) - 2 requests.
* **НРА** (:mod:`mdhub.sources.nra`): the ratings page (export link) and the action export
  since 2020 - 2 requests.
* **Эксперт РА** (:mod:`mdhub.sources.raexpert`): incremental crawl with a request budget
  (``max_requests`` per run, 0.5 requests/s): the 12 credit-rating list pages (latest 20
  objects each) and the sitemap (lags by days) -> new press releases since ``since_days``
  (newest first) -> the company / region / bond pages they link, re-read when a newer
  release links them or after ``refresh_days``. Bookkeeping in ``ref.agency_page``;
  an initial load needs ~1,800 releases and as many object pages (run with a larger
  ``max_requests`` once, see ROADMAP WEB-18), later runs read a few dozen pages.

  ``max_requests`` is a hard cap on **logical page fetches** (one per URL requested: list
  pages, the sitemap index, child sitemaps, releases, object pages), checked before every
  fetch; ``HttpClient`` retries of the same page are not counted separately, a page that
  answers with an HTTP error is. 0 means no RA Expert request at all, a negative budget is
  rejected before any network access. When the budget runs out the crawl stops cleanly:
  captures and the discovered backlog are kept (the next run continues from
  ``ref.agency_page``), the result reports ``budget_exhausted`` (the phase) and what was
  skipped, and a run whose *discovery* (lists, sitemaps) was cut short is ``partial`` - its
  view of new releases is incomplete. A backlog of releases / object pages left for later
  runs is reported in ``backlog`` and does not make the run partial.

Every response is a raw capture (endpoints ``issues_xlsx``, ``issuers_xlsx``,
``ratings_page``, ``export_xlsx``, ``list``, ``sitemap``, ``release``, ``company``,
``region``, ``bond``). Agency rows go to ``ref.agency_rating`` (latest state, verbatim) and
``ref.rating_action`` (history); then all agency rows are **mapped** to MOEX:

* issue ratings by ISIN -> ``ref.bond_rating`` (every SECID with that ISIN; source = agency
  code, agency = display name, evidence ``mapping_basis = isin``);
* issuer ratings by INN -> ``ref.issuer_rating`` (``mapping_basis = inn``); RA Expert regions
  (no INN) by the ISINs of their rated bonds when those belong to exactly one MOEX issuer
  (``region_bond_isin``).

A failing agency does not stop the others (run ``partial``); all failing -> ``failed``.
The result reports the coverage of active non-structured bonds (issue / issuer rated).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, HttpError, HttpStatusError
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import touch_last_seen, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.sources import nkr, nra, raexpert
from mdhub.sources.ratings_common import AGENCY_NAMES, ActionRecord, RatingRecord

__all__ = ["AGENCIES", "map_ratings", "rating_coverage", "ratings_agencies"]

AGENCIES: Final = ("nkr", "nra", "raexpert")

for _agency in AGENCIES:
    # freshness only: each agency publishes rating actions every few business days, so a
    # latest action older than 10 days means the loader (or the source) stopped
    register_dataset(
        DatasetSpec(
            name=f"ratings_{_agency}",
            table="ref.rating_action",
            date_column="action_date",
            calendar="MOEX_STOCK",
            frequency="event",
            max_lag_days=10,
            where=f"agency = '{_agency}'",
            description=f"Credit rating actions of {AGENCY_NAMES[_agency]} (job ratings_agencies)",
        )
    )
STATE_KEY: Final = ["agency", "object_kind", "object_key", "rating_type", "scale"]
ACTION_KEY: Final = [*STATE_KEY, "action_date", "action_ref"]


def _state_row(
    r: RatingRecord, capture_id: int | None, source_url: str | None, run_id: int
) -> dict[str, Any]:
    return {
        "agency": r.agency,
        "object_kind": r.object_kind,
        "object_key": r.object_key,
        "rating_type": r.rating_type,
        "scale": r.scale,
        "agency_object_id": r.agency_object_id,
        "object_name": r.object_name,
        "isin": r.isin,
        "inn": r.inn,
        "ogrn": r.ogrn,
        "rating": r.rating,
        "grade": r.grade,
        "outlook": r.outlook,
        "watch": r.watch,
        "action": r.action,
        "action_raw": r.action_raw,
        "rating_date": r.rating_date,
        "press_release": r.press_release,
        "industry": r.industry,
        "is_withdrawn": r.is_withdrawn,
        "source_url": source_url,
        "details": Jsonb(dict(sorted(r.extra.items()))),
        "capture_id": capture_id,
        "run_id": run_id,
    }


def _action_row(a: ActionRecord, capture_id: int | None, run_id: int) -> dict[str, Any]:
    return {
        "agency": a.agency,
        "object_kind": a.object_kind,
        "object_key": a.object_key,
        "rating_type": a.rating_type,
        "scale": a.scale,
        "action_date": a.action_date,
        "action_ref": a.action_ref,
        "rating": a.rating,
        "grade": a.grade,
        "outlook": a.outlook,
        "watch": a.watch,
        "action": a.action,
        "action_raw": a.action_raw,
        "title": a.title,
        "capture_id": capture_id,
        "run_id": run_id,
    }


def _observed_action(r: RatingRecord) -> ActionRecord | None:
    """A list state (NKR) as an action row: the latest action of the object as observed."""
    if r.rating_date is None:
        return None
    return ActionRecord(
        agency=r.agency,
        object_kind=r.object_kind,
        object_key=r.object_key,
        rating_type=r.rating_type,
        scale=r.scale,
        action_date=r.rating_date,
        action_ref=r.press_release or "",
        rating=r.rating,
        grade=r.grade,
        outlook=r.outlook,
        watch=r.watch,
        action=r.action,
        action_raw=r.action_raw,
        title=None,
    )


def _store(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    states: Iterable[tuple[RatingRecord, int | None, str | None]],
    actions: Iterable[tuple[ActionRecord, int | None]],
) -> int:
    run_id = ctx.run.id
    srows = {r.key: _state_row(r, cap, url, run_id) for r, cap, url in states}
    arows = {a.key: _action_row(a, cap, run_id) for a, cap in actions}
    if srows:
        ctx.run.add_counts(
            upsert_rows(conn, "ref.agency_rating", list(srows.values()), STATE_KEY, run_id=run_id)
        )
        touch_last_seen(
            conn, "ref.agency_rating", STATE_KEY, list(srows), run_id, seen_at=utc_now()
        )
    if arows:
        ctx.run.add_counts(
            upsert_rows(conn, "ref.rating_action", list(arows.values()), ACTION_KEY, run_id=run_id)
        )
    return len(srows)


# ------------------------------------------------------------------------------- NKR


def _load_nkr(ctx: JobContext, http: HttpClient, _budget: list[int], **_kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, endpoint, parse in (
        (nkr.ISSUES_PATH, "issues_xlsx", nkr.parse_issues),
        (nkr.ISSUERS_PATH, "issuers_xlsx", nkr.parse_issuers),
    ):
        resp = nkr.fetch_export(http, path)
        with ctx.connection() as conn:
            cap = record_capture(conn, ctx.run.id, "nkr", endpoint, None, resp, nkr.PARSER_VERSION)
            states = parse(resp.content)
            acts = [a for a in (_observed_action(s) for s in states) if a is not None]
            out[endpoint] = _store(
                ctx,
                conn,
                [(s, cap, nkr.BASE_URL + path) for s in states],
                [(a, cap) for a in acts],
            )
    return out


# ------------------------------------------------------------------------------- NRA


def _load_nra(ctx: JobContext, http: HttpClient, _budget: list[int], **_kw: Any) -> dict[str, Any]:
    page = nra.fetch_ratings_page(http)
    with ctx.connection() as conn:
        record_capture(conn, ctx.run.id, "nra", "ratings_page", None, page, nra.PARSER_VERSION)
    url = nra.parse_export_url(page.text)
    resp = nra.fetch_export(http, url)
    with ctx.connection() as conn:
        cap = record_capture(conn, ctx.run.id, "nra", "export_xlsx", None, resp, nra.PARSER_VERSION)
        export = nra.parse_actions(resp.content)
        stored = _store(
            ctx,
            conn,
            [(s, cap, nra.BASE_URL + nra.RATINGS_PATH) for s in export.states],
            [(a, cap) for a in export.actions],
        )
    return {
        "rows": export.rows,
        "skipped_non_credit": export.skipped_non_credit,
        "actions": len(export.actions),
        "objects": stored,
    }


# ------------------------------------------------------------------------- RA Expert


def _page_upsert(
    conn: psycopg.Connection[Any],
    pages: Iterable[tuple[str, str, date | None]],
    discovered_from: str,
    *,
    linked: bool,
) -> int:
    rows = list(pages)
    if not rows:
        return 0
    cur = conn.execute(
        """
        INSERT INTO ref.agency_page (agency, url, kind, page_date, discovered_from, linked_at)
        SELECT 'raexpert', u.url, u.kind, u.page_date, %(src)s,
               CASE WHEN %(linked)s THEN now() END
        FROM unnest(%(urls)s::text[], %(kinds)s::text[], %(dates)s::date[])
             AS u(url, kind, page_date)
        ON CONFLICT (agency, url) DO UPDATE
            SET linked_at = CASE WHEN %(linked)s THEN now() ELSE ref.agency_page.linked_at END
        """,
        {
            "src": discovered_from,
            "linked": linked,
            "urls": [r[0] for r in rows],
            "kinds": [r[1] for r in rows],
            "dates": [r[2] for r in rows],
        },
    )
    return cur.rowcount


def _page_done(
    conn: psycopg.Connection[Any],
    url: str,
    *,
    status: int | None,
    sha: str | None,
    capture_id: int | None,
    error: str | None,
) -> None:
    conn.execute(
        "UPDATE ref.agency_page SET fetched_at = now(), http_status = %s, content_sha256 = %s, "
        "capture_id = %s, error = %s WHERE agency = 'raexpert' AND url = %s",
        (status, sha, capture_id, error, url),
    )


def _fetch_page(
    ctx: JobContext,
    http: HttpClient,
    url: str,
    kind: str,
    failures: list[str],
    *,
    track: bool = True,
) -> tuple[Any, int] | None:
    """GET + raw capture of one RA Expert page; an HTTP error is recorded on the page
    (``track``: the page is a ``ref.agency_page`` row) and in ``failures``."""
    try:
        resp = raexpert.fetch_page(http, url)
    except HttpStatusError as exc:
        if not track:
            failures.append(f"{url}: HTTP {exc.status_code}")
            return None
        with ctx.connection() as conn:
            _page_done(
                conn, url, status=exc.status_code, sha=None, capture_id=None, error=str(exc)[:300]
            )
        failures.append(f"{url}: HTTP {exc.status_code}")
        return None
    with ctx.connection() as conn:
        cap = record_capture(
            conn, ctx.run.id, "raexpert", kind, None, resp, raexpert.PARSER_VERSION
        )
    return resp, cap


def _spend(budget: list[int]) -> bool:
    """Take one page fetch from the run's RA Expert budget; False when it is used up."""
    if budget[0] <= 0:
        return False
    budget[0] -= 1
    return True


def _load_raexpert(
    ctx: JobContext,
    http: HttpClient,
    budget: list[int],
    *,
    since_days: int,
    refresh_days: int,
    **_kw: Any,
) -> dict[str, Any]:
    today = ctx.today()
    since = today - timedelta(days=since_days)
    failures: list[str] = []
    out: dict[str, Any] = {"releases_fetched": 0, "objects_fetched": 0, "objects_stored": 0}
    start_budget = max(0, budget[0])
    exhausted: str | None = None  # first phase that wanted a page the budget did not allow
    lists_skipped = 0
    index_skipped = False
    sitemaps_skipped = 0

    def in_window(d: date | None) -> bool:
        # a few sitemap URLs carry dates in the future (misfiled): not datable, skipped
        return d is not None and since <= d <= today + timedelta(days=1)

    # 1a. rating lists -> latest releases and (new) objects; objects are only discovered
    #     here: a new release links them for a re-read (step 2)
    listed = 0
    for i, path in enumerate(raexpert.LIST_PATHS):
        url = raexpert.BASE_URL + path
        if not _spend(budget):
            exhausted = "lists"
            lists_skipped = len(raexpert.LIST_PATHS) - i
            break
        got = _fetch_page(ctx, http, url, "list", failures, track=False)
        if got is None:
            continue
        resp, _cap = got
        with ctx.connection() as conn:
            try:
                found = raexpert.parse_list(resp.text, url)
            except FormatDriftError as exc:
                failures.append(f"{url}: {exc}")
                continue
            releases = [
                (u, k, raexpert.release_date(u))
                for k, u in found
                if k == "release" and in_window(raexpert.release_date(u))
            ]
            objects = [(u, k, None) for k, u in found if k != "release"]
            listed += _page_upsert(conn, releases, url, linked=False)
            _page_upsert(conn, objects, url, linked=False)
    out["list_releases_seen"] = listed

    # 1b. sitemap -> releases of the window (the sitemap lags by days; it backfills)
    discovered = 0
    sitemap_urls: list[str] = []
    if exhausted is None and _spend(budget):
        index = raexpert.fetch_page(http, raexpert.BASE_URL + raexpert.SITEMAP_PATH)
        with ctx.connection() as conn:
            record_capture(
                conn, ctx.run.id, "raexpert", "sitemap", None, index, raexpert.PARSER_VERSION
            )
        sitemap_urls = raexpert.parse_sitemap_index(index.content)
    else:
        exhausted = exhausted or "sitemaps"
        index_skipped = True
    for i, sm_url in enumerate(sitemap_urls):
        if not _spend(budget):
            exhausted = "sitemaps"
            sitemaps_skipped = len(sitemap_urls) - i
            break
        sm = raexpert.fetch_page(http, sm_url)
        with ctx.connection() as conn:
            record_capture(
                conn, ctx.run.id, "raexpert", "sitemap", None, sm, raexpert.PARSER_VERSION
            )
            releases = [
                (u, "release", d)
                for u, d in raexpert.parse_sitemap_releases(sm.content)
                if in_window(d)
            ]
            discovered += _page_upsert(conn, releases, "sitemap", linked=False)
    out["sitemap_releases_in_window"] = discovered

    # 2. releases not read yet, newest first
    with ctx.connection() as conn:
        due = [
            r["url"]
            for r in conn.execute(
                "SELECT url FROM ref.agency_page WHERE agency = 'raexpert' AND kind = 'release' "
                "AND fetched_at IS NULL ORDER BY page_date DESC NULLS FIRST, url DESC LIMIT %s",
                (max(0, budget[0]) + 1,),
            ).fetchall()
        ]
    for url in due:
        if not _spend(budget):
            exhausted = exhausted or "releases"
            break
        got = _fetch_page(ctx, http, url, "release", failures)
        if got is None:
            continue
        resp, cap = got
        with ctx.connection() as conn:
            try:
                rel = raexpert.parse_release(resp.text, url)
            except FormatDriftError as exc:
                _page_done(
                    conn,
                    url,
                    status=resp.status_code,
                    sha=None,
                    capture_id=cap,
                    error=str(exc)[:300],
                )
                failures.append(f"{url}: {exc}")
                continue
            _page_upsert(conn, [(u, k, None) for k, u in rel.linked], url, linked=True)
            _page_done(
                conn,
                url,
                status=resp.status_code,
                sha=hashlib.sha256(resp.content).hexdigest(),
                capture_id=cap,
                error=None,
            )
        out["releases_fetched"] += 1

    # 3. object pages: new, linked by a newer release, or stale
    with ctx.connection() as conn:
        due = [
            (r["url"], r["kind"])
            for r in conn.execute(
                """
                SELECT url, kind FROM ref.agency_page
                WHERE agency = 'raexpert' AND kind <> 'release'
                  AND (fetched_at IS NULL OR linked_at > fetched_at
                       OR fetched_at < now() - make_interval(days => %(refresh)s))
                ORDER BY (linked_at IS NOT NULL
                          AND (fetched_at IS NULL OR linked_at > fetched_at)) DESC,
                         fetched_at NULLS FIRST, url
                LIMIT %(n)s
                """,
                {"refresh": refresh_days, "n": max(0, budget[0]) + 1},
            ).fetchall()
        ]
    for url, kind in due:
        if not _spend(budget):
            exhausted = exhausted or "objects"
            break
        got = _fetch_page(ctx, http, url, kind, failures)
        if got is None:
            continue
        resp, cap = got
        with ctx.connection() as conn:
            try:
                entity = raexpert.parse_entity(resp.text, url)
            except FormatDriftError as exc:
                _page_done(
                    conn,
                    url,
                    status=resp.status_code,
                    sha=None,
                    capture_id=cap,
                    error=str(exc)[:300],
                )
                failures.append(f"{url}: {exc}")
                continue
            states, actions = raexpert.entity_records(entity)
            out["objects_stored"] += _store(
                ctx, conn, [(s, cap, url) for s in states], [(a, cap) for a in actions]
            )
            _page_upsert(conn, [(u, k, None) for k, u in entity.linked], url, linked=True)
            _page_done(
                conn,
                url,
                status=resp.status_code,
                sha=hashlib.sha256(resp.content).hexdigest(),
                capture_id=cap,
                error=None,
            )
        out["objects_fetched"] += 1

    with ctx.connection() as conn:
        backlog = conn.execute(
            """
            SELECT count(*) FILTER (WHERE kind = 'release' AND fetched_at IS NULL) AS releases,
                   count(*) FILTER (WHERE kind <> 'release'
                       AND (fetched_at IS NULL OR linked_at > fetched_at)) AS objects
            FROM ref.agency_page WHERE agency = 'raexpert'
            """
        ).fetchone()
    out["backlog"] = dict(backlog) if backlog else {}
    out["requests"] = start_budget - max(0, budget[0])
    out["budget_exhausted"] = exhausted
    out["discovery_complete"] = not (lists_skipped or index_skipped or sitemaps_skipped)
    if not out["discovery_complete"]:
        out["discovery_skipped"] = {
            "lists": lists_skipped,
            "sitemap_index": index_skipped,
            "sitemaps": sitemaps_skipped,
        }
    if exhausted is not None:
        ctx.logger.warning(
            "ratings raexpert: request budget used up (%d pages) in phase %s; "
            "discovery complete: %s; backlog %s",
            out["requests"],
            exhausted,
            out["discovery_complete"],
            out["backlog"],
        )
    if failures:
        out["failed_pages"] = len(failures)
        out["failures"] = failures[:10]
    return out


LOADERS: Final[Mapping[str, Callable[..., dict[str, Any]]]] = {
    "nkr": _load_nkr,
    "nra": _load_nra,
    "raexpert": _load_raexpert,
}


# --------------------------------------------------------------------------- mapping


def _evidence(r: Mapping[str, Any], basis: str) -> Jsonb:
    ev = {
        "mapping_basis": basis,
        "agency_object_key": r["object_key"],
        "agency_object_id": r["agency_object_id"],
        "object_name": r["object_name"],
        "press_release": r["press_release"],
        "source_url": r["source_url"],
    }
    return Jsonb({k: v for k, v in ev.items() if v is not None})


def _latest(
    rows: Iterable[dict[str, Any]], key: Callable[[dict[str, Any]], tuple[Any, ...]]
) -> list[dict[str, Any]]:
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        k = key(r)
        prev = best.get(k)
        if prev is None or (r["rating_date"] or date.min) > (prev["rating_date"] or date.min):
            best[k] = r
    return list(best.values())


def map_ratings(ctx: JobContext, conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Map every agency row to MOEX bonds / issuers (see module docstring)."""
    run_id = ctx.run.id
    issue_rows = []
    for r in conn.execute(
        """
        SELECT a.*, b.secid, b.issuer_id AS bond_issuer_id
        FROM ref.agency_rating a JOIN ref.bond b ON b.isin = a.isin
        WHERE a.object_kind = 'issue' AND a.isin IS NOT NULL
        ORDER BY b.secid, a.agency
        """
    ).fetchall():
        issue_rows.append(
            {
                "secid": r["secid"],
                "agency": AGENCY_NAMES[r["agency"]],
                "scale": r["scale"],
                "source": r["agency"],
                "issuer_id": r["bond_issuer_id"],
                "rating": r["rating"],
                "outlook": r["outlook"],
                "rating_date": r["rating_date"],
                "watch": r["watch"],
                "action": r["action"],
                "grade": r["grade"],
                "rating_type": r["rating_type"],
                "evidence": _evidence(r, "isin"),
                "capture_id": r["capture_id"],
                "run_id": run_id,
            }
        )
    issue_rows = _latest(issue_rows, lambda x: (x["secid"], x["agency"], x["scale"], x["source"]))

    issuer_rows: list[dict[str, Any]] = []

    def issuer_row(r: Mapping[str, Any], issuer_id: int, basis: str) -> dict[str, Any]:
        return {
            "issuer_id": issuer_id,
            "agency": AGENCY_NAMES[r["agency"]],
            "scale": r["scale"],
            "source": r["agency"],
            "rating_type": r["rating_type"],
            "rating": r["rating"],
            "grade": r["grade"],
            "outlook": r["outlook"],
            "watch": r["watch"],
            "action": r["action"],
            "rating_date": r["rating_date"],
            "evidence": _evidence(r, basis),
            "capture_id": r["capture_id"],
            "run_id": run_id,
        }

    for r in conn.execute(
        """
        SELECT a.*, i.issuer_id FROM ref.agency_rating a JOIN ref.issuer i ON i.inn = a.inn
        WHERE a.object_kind = 'issuer' AND a.inn IS NOT NULL ORDER BY i.issuer_id, a.agency
        """
    ).fetchall():
        issuer_rows.append(issuer_row(r, int(r["issuer_id"]), "inn"))
    region_issuers: dict[str, set[int]] = defaultdict(set)
    for r in conn.execute(
        """
        SELECT a.details ->> 'region_page' AS region_page, b.issuer_id
        FROM ref.agency_rating a JOIN ref.bond b ON b.isin = a.isin
        WHERE a.agency = 'raexpert' AND a.object_kind = 'issue'
          AND a.details ? 'region_page' AND b.issuer_id IS NOT NULL
        """
    ).fetchall():
        region_issuers[r["region_page"]].add(int(r["issuer_id"]))
    ambiguous = 0
    for r in conn.execute(
        "SELECT * FROM ref.agency_rating WHERE agency = 'raexpert' AND object_kind = 'issuer' "
        "AND object_key LIKE 'raexpert:region:%%' ORDER BY object_key"
    ).fetchall():
        ids = region_issuers.get(r["agency_object_id"], set())
        if len(ids) == 1:
            issuer_rows.append(issuer_row(r, next(iter(ids)), "region_bond_isin"))
        elif ids:
            ambiguous += 1
    issuer_rows = _latest(
        issuer_rows,
        lambda x: (x["issuer_id"], x["agency"], x["scale"], x["source"], x["rating_type"]),
    )
    ctx.run.add_counts(
        upsert_rows(
            conn,
            "ref.bond_rating",
            issue_rows,
            ["secid", "agency", "scale", "source"],
            run_id=run_id,
        )
    )
    ctx.run.add_counts(
        upsert_rows(
            conn,
            "ref.issuer_rating",
            issuer_rows,
            ["issuer_id", "agency", "scale", "source", "rating_type"],
            run_id=run_id,
        )
    )
    return {
        "bond_ratings": len(issue_rows),
        "issuer_ratings": len(issuer_rows),
        "regions_ambiguous": ambiguous,
    }


def rating_coverage(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Coverage of active non-structured bonds by current (not withdrawn) agency ratings:
    of the bond itself (issue) and of its issuer; all bonds and the RUB corporate subset."""
    row = conn.execute(
        """
        WITH base AS (
            SELECT b.secid, b.issuer_id,
                   b.face_unit = 'RUB' AND b.bond_class = 'corporate'
                       AS rub_corporate
            FROM ref.bond b
            WHERE b.status = 'active' AND NOT COALESCE(b.is_structured, false)
        ), flags AS (
            SELECT base.*,
                   EXISTS (SELECT 1 FROM ref.bond_rating r WHERE r.secid = base.secid
                           AND r.source = ANY(%(src)s) AND r.rating IS NOT NULL) AS issue_rated,
                   EXISTS (SELECT 1 FROM ref.issuer_rating r WHERE r.issuer_id = base.issuer_id
                           AND r.source = ANY(%(src)s) AND r.rating IS NOT NULL) AS issuer_rated
            FROM base
        )
        SELECT count(*) AS bonds,
               count(*) FILTER (WHERE issue_rated) AS issue_rated,
               count(*) FILTER (WHERE issuer_rated) AS issuer_rated,
               count(*) FILTER (WHERE issue_rated OR issuer_rated) AS any_rated,
               count(*) FILTER (WHERE rub_corporate) AS rub_corporate,
               count(*) FILTER (WHERE rub_corporate AND (issue_rated OR issuer_rated))
                   AS rub_corporate_rated
        FROM flags
        """,
        {"src": list(AGENCIES)},
    ).fetchone()
    by_agency = {
        r["agency"]: {"issue": r["issue"], "issuer": r["issuer"], "withdrawn": r["withdrawn"]}
        for r in conn.execute(
            """
            SELECT agency, count(*) FILTER (WHERE object_kind = 'issue') AS issue,
                   count(*) FILTER (WHERE object_kind = 'issuer') AS issuer,
                   count(*) FILTER (WHERE is_withdrawn) AS withdrawn
            FROM ref.agency_rating GROUP BY agency ORDER BY agency
            """
        ).fetchall()
    }
    return {"active_non_structured": dict(row) if row else {}, "agency_rows": by_agency}


# ------------------------------------------------------------------------------- job


@job(
    "ratings_agencies",
    "Credit ratings from NKR / NRA / RA Expert public disclosure, mapped to MOEX bonds/issuers",
)
def ratings_agencies(
    ctx: JobContext,
    *,
    agencies: list[str] | None = None,
    max_requests: int = 400,
    since_days: int = 400,
    refresh_days: int = 35,
) -> dict[str, Any]:
    """Load the agencies' ratings and map them (see module docstring).

    :param agencies: subset of ``nkr``, ``nra``, ``raexpert`` (default all).
    :param max_requests: RA Expert page budget of this run: a hard cap on logical page
        fetches of every phase (lists, sitemap index and children, releases, objects);
        0 = no RA Expert request, negative is rejected.
    :param since_days: RA Expert releases published within this many days are read.
    :param refresh_days: RA Expert object pages older than this are re-read.
    """
    selected = list(agencies or AGENCIES)
    unknown = sorted(set(selected) - set(AGENCIES))
    if unknown:
        raise ValueError(f"ratings_agencies: unknown agencies {unknown} (known: {AGENCIES})")
    if max_requests < 0:
        raise ValueError(f"ratings_agencies: max_requests must be >= 0, got {max_requests}")
    summary: dict[str, Any] = {}
    failures: list[str] = []
    budget = [max_requests]
    with ctx.http() as http:  # 0.5 requests/s per agency host (core.http)
        for agency in selected:
            try:
                summary[agency] = LOADERS[agency](
                    ctx, http, budget, since_days=since_days, refresh_days=refresh_days
                )
            except (HttpError, FormatDriftError) as exc:
                ctx.logger.warning("ratings %s failed: %s", agency, exc)
                failures.append(f"{agency}: {type(exc).__name__}: {str(exc)[:200]}")
                summary[agency] = {"error": failures[-1]}
    if failures and len(failures) == len(selected):
        raise FormatDriftError("ratings_agencies: every agency failed: " + "; ".join(failures))
    with ctx.connection() as conn:
        summary["mapping"] = map_ratings(ctx, conn)
        summary["coverage"] = rating_coverage(conn)
    raex = summary.get("raexpert", {})
    page_failures = raex.get("failed_pages")
    if page_failures:
        failures.append(f"raexpert: {page_failures} pages failed")
    if raex.get("discovery_complete") is False:
        failures.append(
            f"raexpert: request budget {max_requests} used up in {raex['budget_exhausted']} "
            f"before discovery finished (skipped {raex['discovery_skipped']})"
        )
    if failures:
        ctx.run.mark_partial("; ".join(failures))
    return summary
