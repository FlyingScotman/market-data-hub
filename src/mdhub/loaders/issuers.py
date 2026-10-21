"""Issuer reference -> ``ref.issuer_card`` / ``ref.issuer`` (WEB-13, TDM brief R9 HS1).

``issuers_static()`` (daily 07:00 MSK, after ``bonds_static`` 06:30):

1. **List diff.** ``/iss/emitters.json`` (~14 pages of 100): issuers whose TITLE, SHORT_TITLE
   or INN in the list differ from their stored card are refreshed in this run.
2. **Cards.** ``/iss/emitters/{id}.json`` for the issuers in scope - issuers of bonds that the
   static snapshot may publish (``active``; ``delisted`` with ``maturity_date >= today`` or
   perpetual; ``matured`` within :data:`MATURED_SCOPE_DAYS`) - that have no card yet, whose
   card is older than ``refresh_days`` (oldest first, at most ``refresh_limit`` per run) or
   whose list row changed. One raw capture per card (endpoint ``emitter``); an identical card
   is a no-op (only ``last_seen`` moves). An empty ``emitter`` block is stored as
   ``found = false``, never as values.
3. **Issuer fields.** ``ref.issuer.short_name``, ``ogrn``, ``oksm`` and ``country`` (ISO
   3166-1 alpha-2 of OKSM) come from the card. ``name``, ``inn`` and ``okpo`` stay as
   ``bonds_static`` loads them from the bond directory (one writer per column, so the two
   jobs never overwrite each other).
4. **Groups** (HS4) from the curated ``mdhub/data/issuer_groups_curated.json`` (keyed by
   INN, evidence per row): ``group_id``, ``group_name``, ``group_relation``,
   ``is_state_owned``, ``group_evidence`` on every issuer with a listed INN (removed rows
   clear the group: a versioned change).
5. **Sector** (HS3) of every issuer in scope from the versioned rules of
   ``mdhub/data/sector_map.json`` (:mod:`mdhub.loaders._issuer_rules`): ``sector`` and
   ``sector_source`` (``curated`` / ``group`` / ``okved`` / ``rule`` / ``agency``: the
   industry a rating agency publishes for the issuer's INN, ``ref.agency_rating``, WEB-18);
   no rule -> NULL.

A failing card is logged and the run ends ``partial``; the other cards are kept. Changes of
both tables are versioned in ``ops.row_history`` (as-known-at readers 0520).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpError
from mdhub.core.iss import ISS_BASE_URL
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import touch_last_seen, upsert_rows
from mdhub.jobs import JobContext, job
from mdhub.loaders import _issuer_rules as rules
from mdhub.sources import moex_emitters as me

__all__ = ["MATURED_SCOPE_DAYS", "issuers_static", "scope_issuer_ids"]

SOURCE: Final = "moex_iss"
EP_LIST: Final = "emitters_list"
EP_CARD: Final = "emitter"
MATURED_SCOPE_DAYS: Final = 31
"""Same lookback as the static snapshot's matured universe (``MATURED_LOOKBACK_DAYS``)."""

CARD_COLUMNS: Final = ("title", "short_title", "inn", "ogrn", "okpo", "oksm", "country", "found")
ISSUER_FROM_CARD: Final = ("short_name", "ogrn", "oksm", "country")


def scope_issuer_ids(conn: psycopg.Connection[Any], today: date) -> list[int]:
    """Issuers of bonds the static snapshot may publish (see module docstring)."""
    rows = conn.execute(
        """
        SELECT DISTINCT b.issuer_id FROM ref.bond b
        WHERE b.issuer_id IS NOT NULL AND (
              b.status = 'active'
           OR (b.status = 'delisted' AND (b.is_perpetual OR b.maturity_date >= %(today)s))
           OR (b.status = 'matured' AND b.maturity_date >= %(since)s))
        ORDER BY 1
        """,
        {"today": today, "since": today - timedelta(days=MATURED_SCOPE_DAYS)},
    ).fetchall()
    return [int(r["issuer_id"]) for r in rows]


def _due(
    conn: psycopg.Connection[Any],
    scope: Sequence[int],
    listed: Mapping[int, Mapping[str, str | None]],
    *,
    refresh_days: int,
    refresh_limit: int,
) -> list[int]:
    """Issuers of ``scope`` to fetch: no card, changed list row, then stale cards (limited)."""
    cards = {
        int(r["issuer_id"]): r
        for r in conn.execute(
            "SELECT issuer_id, title, short_title, inn, found, last_seen FROM ref.issuer_card "
            "WHERE issuer_id = ANY(%s)",
            (list(scope),),
        ).fetchall()
    }
    stale_before = utc_now() - timedelta(days=refresh_days)
    new: list[int] = []
    changed: list[int] = []
    stale: list[tuple[Any, int]] = []
    for iid in scope:
        card = cards.get(iid)
        if card is None:
            new.append(iid)
            continue
        row = listed.get(iid)
        if (
            row is not None
            and card["found"]
            and any(row[k] != card[k] for k in ("title", "short_title", "inn"))
        ):
            changed.append(iid)
        elif card["last_seen"] is None or card["last_seen"] < stale_before:
            stale.append((card["last_seen"], iid))
    stale.sort(key=lambda t: (t[0] is not None, t[0], t[1]))
    return new + changed + [iid for _, iid in stale[: max(0, refresh_limit)]]


@job(
    "issuers_static",
    "Issuer reference: MOEX emitter cards (short name, OGRN, country), curated sectors",
)
def issuers_static(
    ctx: JobContext,
    *,
    mode: str = "full",
    refresh_days: int = 7,
    refresh_limit: int = 200,
) -> dict[str, Any]:
    """Refresh the issuer reference (see module docstring)."""
    if mode != "full":
        raise ValueError(f"issuers_static: unknown mode {mode!r} (expected 'full')")
    today = ctx.today()
    summary: dict[str, Any] = {}
    with ctx.http(base_url=ISS_BASE_URL) as http:
        listed: dict[int, dict[str, str | None]] = {}
        with ctx.connection() as conn:
            for page in me.iter_list_pages(http):
                record_capture(
                    conn, ctx.run.id, SOURCE, EP_LIST, None, page.response, me.PARSER_VERSION
                )
                listed.update(me.parse_list_rows(page.rows))
            if not listed:
                raise FormatDriftError("emitters list is empty")
            scope = scope_issuer_ids(conn, today)
            due = _due(conn, scope, listed, refresh_days=refresh_days, refresh_limit=refresh_limit)
        summary.update(listed=len(listed), scope=len(scope), due=len(due))
        failures: list[str] = []
        fetched = 0
        for iid in due:
            try:
                resp = me.fetch_card(http, iid)
                with ctx.connection() as conn:
                    cap = record_capture(
                        conn, ctx.run.id, SOURCE, EP_CARD, None, resp, me.PARSER_VERSION
                    )
                    card = me.parse_card(resp.json(), iid)
                    _store_card(ctx, conn, iid, card, cap)
                fetched += 1
            except (HttpError, FormatDriftError, ValueError) as exc:
                ctx.logger.warning("emitter %s failed: %s", iid, exc)
                failures.append(f"{iid}: {type(exc).__name__}: {str(exc)[:120]}")
    summary["fetched"] = fetched
    with ctx.connection() as conn:
        summary.update(_apply_issuer_fields(ctx, conn, today))
    if failures:
        summary["failed"] = len(failures)
        ctx.run.mark_partial(f"{len(failures)} emitter cards failed: " + "; ".join(failures[:5]))
    return summary


def _store_card(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    iid: int,
    card: me.EmitterCard | None,
    capture_id: int,
) -> None:
    row: dict[str, Any] = {"issuer_id": iid, **dict.fromkeys(CARD_COLUMNS)}
    if card is None:
        row["found"] = False
    else:
        row.update(
            title=card.title,
            short_title=card.short_title,
            inn=card.inn,
            ogrn=card.ogrn,
            okpo=card.okpo,
            oksm=card.oksm,
            country=card.country,
            found=True,
        )
    row.update(source=SOURCE, run_id=ctx.run.id, capture_id=capture_id)
    ctx.run.add_counts(
        upsert_rows(conn, "ref.issuer_card", [row], ["issuer_id"], run_id=ctx.run.id)
    )
    touch_last_seen(conn, "ref.issuer_card", ["issuer_id"], [(iid,)], ctx.run.id, seen_at=utc_now())


def _apply_issuer_fields(
    ctx: JobContext, conn: psycopg.Connection[Any], today: date
) -> dict[str, Any]:
    """Write card fields (issuers with a found card) and sectors (issuers in scope) to
    ``ref.issuer`` in one upsert, so one run makes at most one version per issuer. Issuers
    are never created here; values outside these two sets are kept as stored."""
    scope = set(scope_issuer_ids(conn, today))
    groups = rules.issuer_groups()
    group_inns = sorted(groups)
    cards = {
        int(r["issuer_id"]): r
        for r in conn.execute(
            "SELECT issuer_id, short_title, ogrn, oksm, country FROM ref.issuer_card WHERE found"
        ).fetchall()
    }
    grouped_ids = {
        int(r["issuer_id"])
        for r in conn.execute(
            "SELECT issuer_id FROM ref.issuer WHERE inn = ANY(%s) OR group_id IS NOT NULL",
            (group_inns,),
        ).fetchall()
    }
    ids = sorted(scope | set(cards) | grouped_ids)
    classes: dict[int, set[str]] = {}
    for r in conn.execute(
        """
        SELECT issuer_id, bond_class FROM ref.bond
        WHERE issuer_id = ANY(%(ids)s) AND (status = 'active'
           OR (status = 'delisted' AND (is_perpetual OR maturity_date >= %(today)s))
           OR (status = 'matured' AND maturity_date >= %(since)s))
        """,
        {"ids": sorted(scope), "today": today,
         "since": today - timedelta(days=MATURED_SCOPE_DAYS)},
    ).fetchall():  # fmt: skip
        classes.setdefault(int(r["issuer_id"]), set()).add(str(r["bond_class"]))
    industries: dict[str, set[tuple[str, str]]] = {}
    for r in conn.execute(
        "SELECT DISTINCT inn, agency, industry FROM ref.agency_rating "
        "WHERE object_kind = 'issuer' AND inn IS NOT NULL AND industry IS NOT NULL"
    ).fetchall():
        industries.setdefault(str(r["inn"]), set()).add((str(r["agency"]), str(r["industry"])))
    out: list[dict[str, Any]] = []
    assigned = unassigned = grouped = 0
    for i in conn.execute(
        "SELECT issuer_id, inn, name, short_name, ogrn, oksm, country, okved, sector, "
        "sector_source, group_id, group_name, group_relation, is_state_owned, group_evidence "
        "FROM ref.issuer WHERE issuer_id = ANY(%s) ORDER BY issuer_id",
        (ids,),
    ).fetchall():
        iid = int(i["issuer_id"])
        row = {k: i[k] for k in ("issuer_id", *ISSUER_FROM_CARD, "sector", "sector_source")}
        group = groups.get(i["inn"]) if i["inn"] else None
        if group is not None and group.emitter_id not in (None, iid):
            ctx.logger.warning(
                "issuer group %s: INN %s matched issuer %s, file names emitter %s",
                group.group_id, i["inn"], iid, group.emitter_id,
            )  # fmt: skip
        row.update(
            group_id=group.group_id if group else None,
            group_name=group.group_name if group else None,
            group_relation=group.group_relation if group else None,
            is_state_owned=group.is_state_owned if group else None,
            group_evidence=Jsonb(dict(group.evidence)) if group else None,
        )
        grouped += group is not None
        card = cards.get(iid)
        if card is not None:
            row.update(short_name=card["short_title"], ogrn=card["ogrn"], oksm=card["oksm"],
                       country=card["country"])  # fmt: skip
        if iid in scope:
            res = rules.assign_sector(
                inn=i["inn"],
                name=i["name"],
                short_name=row["short_name"],
                bond_classes=classes.get(iid, ()),
                okved=i["okved"],
                group_sector=group.sector if group else None,
                group_relation=group.group_relation if group else None,
                agency_industries=industries.get(i["inn"], ()) if i["inn"] else (),
            )
            row.update(
                sector=res.sector if res else None, sector_source=res.source if res else None
            )
            assigned += res is not None
            unassigned += res is None
        out.append(row)
    if out:
        ctx.run.add_counts(upsert_rows(conn, "ref.issuer", out, ["issuer_id"], run_id=ctx.run.id))
    return {
        "sector_map_version": rules.sector_map().version,
        "sector_assigned": assigned,
        "sector_unassigned": unassigned,
        "groups_version": rules.groups_version(),
        "grouped_issuers": grouped,
    }
