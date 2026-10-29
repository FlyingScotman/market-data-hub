"""Repo with the central counterparty: per-OFZ boards (market ``ccp``) and GC baskets (``gcc``).

Jobs (names fixed by :data:`mdhub.schedule.KNOWN_JOBS`):

* ``repo_ccp_bond`` - boards EQRP (1 day), EQWP (1 week), EQMP (1 month), EQTP (3 months) of the
  ``ccp`` market, OFZ only (:func:`~mdhub.sources.moex_intraday.is_ofz`).
* ``repo_gc_term`` - GC repo by term (``gcc``: GCRP, GCOW, GCSW, GCOM, GCSM, GCTM, GCUM, GCNM,
  GCOY), every basket (GC Bonds ``RU000A0JW4Z1`` among them; the baskets are few).

Modes (``mode``):

* ``intraday`` - one marketdata request per board (``securities`` + ``marketdata`` blocks with
  only the needed columns; ccp: ``securities=`` the active OFZ of ``ref.bond``, else the whole
  board filtered here) -> ``md.repo_intraday``, a row only when a rate, volume or status changed
  (change-only per (market, board, secid)). Public ISS is 15 minutes delayed here
  (``latency='delayed'``).
* ``eod`` / ``final`` - daily history per board and business day (``days_back``, or
  ``date_from``/``date_to`` for backfill; cursor-paged) -> ``md.repo_eod``. A day whose every
  board is empty is recorded with :func:`~mdhub.core.gaps.record_no_data`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.core.capture import record_capture
from mdhub.core.gaps import close_gap, record_no_data
from mdhub.core.http import HttpClient
from mdhub.core.iss import ISS_BASE_URL, iter_pages
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.loaders.fx_moex import only_changed, resolve_dates
from mdhub.sources.moex_intraday import (
    CCP_BOARDS,
    GCC_BOARDS,
    PARSER_VERSION,
    REPO_HISTORY,
    REPO_HISTORY_REQUIRED,
    REPO_MARKETDATA,
    REPO_MD_COMPARE,
    REPO_MD_FIELDS,
    is_ofz,
    parse_repo_history,
    parse_repo_marketdata,
)

__all__ = ["repo_ccp_bond", "repo_gc_term"]

SOURCE: Final = "moex_iss"
CALENDAR: Final = "MOEX_STOCK"
INTRADAY_SERIES: Final = ["market", "board", "secid"]
INTRADAY_KEY: Final = [*INTRADAY_SERIES, "ts"]
EOD_KEY: Final = ["market", "trade_date", "board", "secid"]
MAX_BACKFILL_DAYS: Final = 120
_MD_COLUMNS: Final = ",".join(
    ("SECID", "BOARDID", "UPDATETIME", "SYSTIME", *(f[1] for f in REPO_MD_FIELDS))
)

for _name, _market, _desc in (
    ("repo_ccp_eod", "ccp", "Repo with the CCP per OFZ (EQRP/EQWP/EQMP/EQTP), daily history"),
    ("repo_gc_eod", "gcc", "GC repo with the CCP by term (GCRP...GCOY), daily history"),
):
    register_dataset(
        DatasetSpec(
            name=_name,
            table="md.repo_eod",
            date_column="trade_date",
            calendar=CALENDAR,
            frequency="business_daily",
            max_lag_days=1,
            backfill_job="repo_ccp_bond" if _market == "ccp" else "repo_gc_term",
            history_start=date(2024, 1, 3),
            min_rows_per_day=10,
            max_task_days=10,
            where=f"market = '{_market}'",
            description=_desc,
        )
    )
for _name, _market, _desc in (
    ("repo_ccp_intraday", "ccp", "Repo with the CCP per OFZ, intraday (delayed ISS), change-only"),
    ("repo_gc_intraday", "gcc", "GC repo by term, intraday (delayed ISS), change-only"),
):
    register_dataset(
        DatasetSpec(
            name=_name,
            table="md.repo_intraday",
            date_column="ts",
            calendar=CALENDAR,
            frequency="intraday",
            max_lag_days=1,
            where=f"market = '{_market}'",
            description=_desc,
        )
    )


@job("repo_ccp_bond", "Repo with the CCP per OFZ (EQRP/EQWP/EQMP/EQTP): intraday + EOD")
def repo_ccp_bond(
    ctx: JobContext,
    *,
    mode: str = "intraday",
    days_back: list[int] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    return _run(ctx, "ccp", CCP_BOARDS, is_ofz, mode, days_back, date_from, date_to)


@job("repo_gc_term", "GC repo with the CCP by term (gcc boards): intraday + EOD")
def repo_gc_term(
    ctx: JobContext,
    *,
    mode: str = "intraday",
    days_back: list[int] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    return _run(ctx, "gcc", GCC_BOARDS, lambda _s: True, mode, days_back, date_from, date_to)


def _run(
    ctx: JobContext,
    market: str,
    boards: tuple[str, ...],
    keep: Callable[[str], bool],
    mode: str,
    days_back: list[int] | None,
    date_from: date | None,
    date_to: date | None,
) -> dict[str, Any]:
    backfill = date_from is not None or date_to is not None
    if mode not in ("intraday", "eod", "final"):
        raise JobParamError("mode must be intraday, eod or final")
    with ctx.http(base_url=ISS_BASE_URL) as http:
        if mode == "intraday" and not backfill:
            secids = _active_ofz(ctx) if market == "ccp" else None
            changed = sum(_poll_board(ctx, http, market, board, keep, secids) for board in boards)
            return {"changed": changed}
        days = resolve_dates(
            ctx,
            CALENDAR,
            days_back=days_back,
            date_from=date_from,
            date_to=date_to,
            default_days_back=(0,) if mode == "eod" else (1,),
        )
        if len(days) > MAX_BACKFILL_DAYS:
            raise JobParamError(f"at most {MAX_BACKFILL_DAYS} business days per run")
        dataset = "repo_ccp_eod" if market == "ccp" else "repo_gc_eod"
        loaded = 0
        for d in days:
            n = sum(_history_board(ctx, http, market, board, d, keep) for board in boards)
            loaded += n
            with ctx.connection() as conn:
                if n == 0:
                    note = f"ISS {market} repo history empty on every board for {d} (mode={mode})"
                    ctx.logger.warning(note)
                    if mode != "eod":  # the evening run may precede the publication
                        record_no_data(conn, dataset, d, note, today=ctx.today())
                else:
                    close_gap(conn, dataset, d)
        return {"days": len(days), "rows": loaded}


def _active_ofz(ctx: JobContext) -> list[str] | None:
    """Active OFZ of ``ref.bond`` (``None`` when there are none: read the whole board)."""
    with ctx.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            r"SELECT secid FROM ref.bond WHERE secid ~ '^SU\d{5}RMFS\d$' AND status = 'active' "
            "AND (maturity_date IS NULL OR maturity_date >= %s) ORDER BY secid",
            (ctx.today(),),
        )
        secids = [str(r[0]) for r in cur.fetchall()]
    return secids or None


def _poll_board(
    ctx: JobContext,
    http: HttpClient,
    market: str,
    board: str,
    keep: Callable[[str], bool],
    secids: list[str] | None,
) -> int:
    params: dict[str, Any] = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": "SECID,BOARDID,SETTLEDATE"
        + (",SETTLEDATE2" if market == "ccp" else ""),
        "marketdata.columns": _MD_COLUMNS,
    }
    if secids:
        params["securities"] = ",".join(secids)
    res = http.get_json(REPO_MARKETDATA.format(market=market, board=board), params=params)
    rows = parse_repo_marketdata(
        res.data,
        market,
        board,
        keep=keep,
        is_trading_day=lambda d: ctx.is_business_day(d, CALENDAR),
    )
    with ctx.connection() as conn:
        cap = record_capture(
            conn,
            ctx.run.id,
            SOURCE,
            f"intraday:repo_{market}_marketdata",
            None,
            res.response,
            PARSER_VERSION,
            source_ts=max((r["source_ts"] for r in rows), default=None),
        )
        changed, unchanged = only_changed(
            conn, "md.repo_intraday", rows, INTRADAY_SERIES, REPO_MD_COMPARE, logger=ctx.logger
        )
        for r in changed:
            r["capture_id"] = cap
            r["run_id"] = ctx.run.id
        result = upsert_rows(conn, "md.repo_intraday", changed, INTRADAY_KEY, run_id=ctx.run.id)
        ctx.run.add_counts(result + UpsertResult(unchanged=unchanged))
    return result.changed


def _history_board(
    ctx: JobContext,
    http: HttpClient,
    market: str,
    board: str,
    d: date,
    keep: Callable[[str], bool],
) -> int:
    rows: list[dict[str, Any]] = []
    with ctx.connection() as conn:
        for page in iter_pages(
            http,
            REPO_HISTORY.format(market=market, board=board),
            {"date": d.isoformat()},
            required=REPO_HISTORY_REQUIRED,
        ):
            cap = record_capture(
                conn,
                ctx.run.id,
                SOURCE,
                f"repo_{market}_history",
                None,
                page.response,
                PARSER_VERSION,
            )
            for r in parse_repo_history(page.rows, market, board, d, keep=keep):
                r["capture_id"] = cap
                r["run_id"] = ctx.run.id
                rows.append(r)
        ctx.run.add_counts(upsert_rows(conn, "md.repo_eod", rows, EOD_KEY, run_id=ctx.run.id))
    return len(rows)
