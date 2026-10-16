"""FORTS (FX) futures loaders: EOD history, realtime marketdata, AlgoPack 5-minute datasets.

Jobs (names fixed by :data:`mdhub.schedule.KNOWN_JOBS`):

* ``futures_eod`` - ``md.futures_eod`` from ISS FORTS history for **all** outright FORTS
  futures (one date = ~850 rows / 9 pages, cheap; RGBI/KEYRATE/RUONIA futures are useful to
  other consumers). Calendar spreads are skipped. Refreshes ``ref.futures_contract`` from the
  RFUD listing; expired contracts met in history are added (FX ones with dates from
  ``/securities/{secid}.json``).
* ``futures_intraday`` - ``md.futures_intraday`` for every listed FX future via APIM realtime
  marketdata; on any APIM failure (no key, 401, ``EntitlementError``, transport) falls back
  to public ISS (15-min delayed, ``source_variant='iss_public'``) and marks the run partial.
  Change-only.
* ``algopack_fx_futures`` - AlgoPack FO tradestats and FUTOI for FX futures (order-book
  obstats are not collected: owner decision, dropped by migration 0423). Without dates
  (scheduled polls, business days and weekend sessions): incremental polls read the latest bar
  of every contract (``latest=1``, all contracts in one paged request) and FUTOI ``latest=1``;
  every ``MDHUB_ALGOPACK_FULL_REREAD_MINUTES`` (default 30) - and whenever
  :func:`interval_holes` reports holes - the poll re-reads the whole current trading day per
  FX contract and FUTOI ticker (``from=<first weekend-session day before today, else
  today>``, ``till=today``), so bars published late or missed by failed polls are filled. See
  :func:`_algopack_poll` for request counts. With ``date_from/date_to``: per-contract
  ``from/till`` backfill, clamped to the rolling window (``MDHUB_ALGOPACK_RETENTION_DAYS``,
  default 90 calendar days; a range wholly before it is refused). Dedup by primary key
  ``(secid, interval_ts)`` - polling the same interval twice is a no-op. FUTOI answers of 1000
  rows fail closed (possible truncation).

**Rolling window.** Both AlgoPack datasets keep only the last
``MDHUB_ALGOPACK_RETENTION_DAYS`` calendar days (:func:`mdhub.datasets.rolling_window_start`):
``maintenance mode=retention`` deletes older rows (:data:`ALGOPACK_RETENTION_TABLES`) and raw
captures (:data:`ALGOPACK_RAW_ENDPOINTS`).

Normalized prices (``*_rub_per_unit`` = raw x STEPPRICE / MINSTEP / LOTVOLUME): RUB-quoted
FX contracts always (their STEPPRICE is constant; an expired contract without a spec uses a
listed sibling of the same asset); crosses (ED, UCNY, UJPY...) only with the STEPPRICE of the
same session (intraday / the current session's AlgoPack bars), else NULL.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row, tuple_row

from mdhub.core.capture import record_capture
from mdhub.core.gaps import close_gap, record_no_data
from mdhub.core.http import HttpClient, HttpError
from mdhub.core.iss import (
    APIM_BASE_URL,
    ISS_BASE_URL,
    IssFormatError,
    IssPagingError,
    iter_pages,
    parse_block,
)
from mdhub.core.timeutil import MSK, msk_now
from mdhub.core.upsert import UpsertResult, touch_last_seen, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset, rolling_window_start
from mdhub.jobs import JobContext, JobParamError, JobSkipped, job
from mdhub.loaders.fx_moex import only_changed, resolve_dates
from mdhub.sources.moex_apim import (
    ALGOPACK_FO_TRADESTATS,
    ALGOPACK_FO_TRADESTATS_SECID,
    FUTOI_ALL,
    FUTOI_TICKER,
    apim_headers,
    parse_futoi,
    parse_tradestats,
)
from mdhub.sources.moex_apim import PARSER_VERSION as APIM_PARSER_VERSION
from mdhub.sources.moex_fx import (
    FORTS_HISTORY,
    FORTS_SECURITIES,
    FUTURES_INTRADAY_VALUE_COLUMNS,
    PARSER_VERSION,
    RFUD_SECURITIES,
    SECURITY_DESCRIPTION,
    fx_asset,
    normalize_price,
    parse_forts_history,
    parse_futures_marketdata,
    parse_futures_securities,
    parse_security_description,
    price_factor,
)

__all__ = [
    "ALGOPACK_HISTORY_ENDPOINTS",
    "ALGOPACK_RAW_ENDPOINTS",
    "ALGOPACK_RETENTION_TABLES",
    "ALGOPACK_WINDOW_SETTING",
    "ContractSpec",
    "algopack_fx_futures",
    "algopack_window",
    "full_reread_due",
    "futures_eod",
    "futures_intraday",
    "interval_holes",
    "load_specs",
    "refresh_futures_contracts",
    "session_span_start",
]

FORTS_CALENDAR: Final = "MOEX_FORTS"
CONTRACT_KEY: Final = ["secid"]
EOD_KEY: Final = ["trade_date", "secid"]
INTRADAY_SERIES: Final = ["secid", "source", "source_variant"]
INTRADAY_KEY: Final = [*INTRADAY_SERIES, "ts"]
FUTOI_MAX_ROWS: Final = 1000
"""FUTOI ignores ``start=`` and returns at most 1000 rows (newest first): backfill one day per
request (~400 rows per ticker-day) and fail closed on a full page."""
BAR_KEY: Final = ["secid", "interval_ts"]
INTERVAL: Final = timedelta(minutes=5)
INTERVAL_GRACE: Final = timedelta(minutes=15)
"""An interval missing for longer than this (three polls) counts as a hole."""
ALGOPACK_BREAKS: Final = (
    (dt_time(13, 55), dt_time(14, 10)),
    (dt_time(18, 45), dt_time(19, 10)),
)
"""FORTS clearing windows (intermediate 14:00-14:05, evening 18:50-19:05, MSK) with a margin
for the interval-label convention: no trading, so no bars - not holes."""
FUTOI_KEY: Final = ["ticker", "interval_ts", "clgroup"]
BACKFILL_CHUNK_DAYS: Final = 31
POLL_EVERY_MIN: Final = 5
"""AlgoPack poll interval of the schedule (``1-56/5``)."""
REREAD_ENDPOINT_PREFIX: Final = "intraday:reread:"
"""Endpoint prefix of whole-day re-read captures (shorter raw retention,
``MDHUB_RAW_RETENTION_DAYS_REREAD``; :data:`mdhub.loaders.maintenance.REREAD_ENDPOINT_PREFIX`)."""
DEFAULT_FUTOI_TICKERS: Final = frozenset(
    {"Si", "Eu", "CR", "ED", "UC", "USDRUBF", "EURRUBF", "CNYRUBF"}
)
"""FX FUTOI tickers seen 2026-10-16; merged with tickers derived from ref.futures_contract."""
ALGOPACK_WINDOW_SETTING: Final = "algopack_retention_days"
"""Settings field (``MDHUB_ALGOPACK_RETENTION_DAYS``) holding the AlgoPack rolling window."""
ALGOPACK_RETENTION_TABLES: Final = (
    ("md.algopack_fo_tradestats", "trade_date"),
    ("md.algopack_futoi", "trade_date"),
)
"""``(table, business-date column)`` of the AlgoPack datasets: rows older than the rolling
window are deleted by ``maintenance mode=retention``."""
ALGOPACK_RAW_ENDPOINTS: Final = (
    "intraday:algopack_fo_tradestats",
    "intraday:futoi",
    f"{REREAD_ENDPOINT_PREFIX}algopack_fo_tradestats",
    f"{REREAD_ENDPOINT_PREFIX}futoi",
    "algopack_fo_tradestats_history",
    "futoi_history",
)
"""``ops.raw_capture.endpoint`` values written by ``algopack_fx_futures`` (source
``moex_apim``): captures fetched before the rolling window are deleted by retention - also the
backfill (``*_history``) captures, which other raw retention keeps forever."""
ALGOPACK_HISTORY_ENDPOINTS: Final = ("algopack_fo_tradestats_history", "futoi_history")
"""Backfill captures (``?from=&till=``): also deleted when the requested range ends before the
window, whenever they were fetched."""

register_dataset(
    DatasetSpec(
        name="futures_eod",
        table="md.futures_eod",
        date_column="trade_date",
        calendar=FORTS_CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="futures_eod",
        history_start=date(2024, 1, 3),
        min_rows_per_day=200,
        max_task_days=10,
        description="FORTS futures daily history (all outright futures; settle, OI)",
    )
)
register_dataset(
    DatasetSpec(
        name="futures_intraday",
        table="md.futures_intraday",
        date_column="ts",
        calendar=FORTS_CALENDAR,
        frequency="intraday",
        max_lag_days=1,
        description="FX futures marketdata (APIM realtime / ISS delayed fallback), change-only",
    )
)
for _name, _desc in (
    ("algopack_fo_tradestats", "AlgoPack FO tradestats, FX futures, 5-min (rolling window)"),
    ("algopack_futoi", "FUTOI open interest by client group, FX assets, 5-min (rolling window)"),
):
    register_dataset(
        DatasetSpec(
            name=_name,
            table=f"md.{_name}",
            date_column="trade_date",
            calendar=FORTS_CALENDAR,
            frequency="intraday",
            max_lag_days=1,
            backfill_job="algopack_fx_futures",
            history_start=date(2024, 1, 3),
            min_rows_per_day=100,
            max_task_days=3,
            description=_desc,
            rolling_window_setting=ALGOPACK_WINDOW_SETTING,
        )
    )


# ------------------------------------------------------------------------ specs


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """What the loaders need from ``ref.futures_contract``."""

    secid: str
    asset_code: str | None
    sec_type: str | None
    is_fx: bool
    quote_ccy: str | None
    contract_type: str
    first_trade_date: date | None
    last_trade_date: date | None
    factor: Decimal | None
    last_seen: date | None

    @property
    def rub_quoted(self) -> bool:
        return self.quote_ccy == "RUB"


def load_specs(conn: psycopg.Connection[Any]) -> dict[str, ContractSpec]:
    """All contracts of ``ref.futures_contract``; RUB-quoted contracts without a full spec
    inherit the factor of a sibling with the same asset code (constant contract terms)."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT secid, asset_code, sec_type, is_fx, quote_ccy, contract_type,
                   first_trade_date, last_trade_date, step_price, min_step, lot_volume,
                   last_seen
            FROM ref.futures_contract
            """
        ).fetchall()
    own = {r["secid"]: price_factor(r["step_price"], r["min_step"], r["lot_volume"]) for r in rows}
    sibling: dict[str, Decimal] = {}
    for r in sorted(rows, key=lambda x: x["last_seen"] or date.min, reverse=True):
        f = own[r["secid"]]
        if f is not None and r["quote_ccy"] == "RUB" and r["asset_code"]:
            sibling.setdefault(r["asset_code"], f)
    specs: dict[str, ContractSpec] = {}
    for r in rows:
        factor = own[r["secid"]]
        if factor is None and r["quote_ccy"] == "RUB" and r["asset_code"]:
            factor = sibling.get(r["asset_code"])
        specs[r["secid"]] = ContractSpec(
            secid=r["secid"],
            asset_code=r["asset_code"],
            sec_type=r["sec_type"],
            is_fx=bool(r["is_fx"]),
            quote_ccy=r["quote_ccy"],
            contract_type=r["contract_type"],
            first_trade_date=r["first_trade_date"],
            last_trade_date=r["last_trade_date"],
            factor=factor,
            last_seen=r["last_seen"],
        )
    return specs


def _eod_factor(spec: ContractSpec | None) -> Decimal | None:
    """Factor usable for any date: RUB-quoted FX contracts only."""
    if spec is None or not spec.is_fx or not spec.rub_quoted:
        return None
    return spec.factor


def _session_factor(spec: ContractSpec | None, trade_date: date) -> Decimal | None:
    """Factor for a bar of ``trade_date``: crosses only if the spec was seen that day."""
    if spec is None or not spec.is_fx:
        return None
    if spec.rub_quoted:
        return spec.factor
    return spec.factor if spec.last_seen == trade_date else None


def refresh_futures_contracts(
    conn: psycopg.Connection[Any],
    rows: list[dict[str, Any]],
    *,
    capture_id: int,
    run_id: int,
    today: date,
    touch: bool = True,
) -> UpsertResult:
    """Upsert ``ref.futures_contract`` rows (``first_seen`` insert-only)."""
    if not rows:
        return UpsertResult()
    for r in rows:
        r["first_seen"] = today
        r["capture_id"] = capture_id
        r["run_id"] = run_id
    update_cols = [c for c in rows[0] if c not in (*CONTRACT_KEY, "first_seen")]
    result = upsert_rows(
        conn, "ref.futures_contract", rows, CONTRACT_KEY, update_cols, run_id=run_id
    )
    if touch:
        touch_last_seen(conn, "ref.futures_contract", CONTRACT_KEY, rows, run_id)
    return result


def _provenance(rows: list[dict[str, Any]], capture_id: int, run_id: int) -> list[dict[str, Any]]:
    for r in rows:
        r["capture_id"] = capture_id
        r["run_id"] = run_id
    return rows


def _dedup(rows: Iterable[dict[str, Any]], key: list[str]) -> list[dict[str, Any]]:
    """Last row wins per key (pages of a moving dataset may repeat an interval)."""
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        out[tuple(r[k] for k in key)] = r
    return list(out.values())


# ---------------------------------------------------------------------- futures_eod


@job("futures_eod", "FORTS futures daily history (settle, OI) -> md.futures_eod")
def futures_eod(
    ctx: JobContext,
    *,
    mode: str = "final",
    days_back: list[int] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> None:
    dates = resolve_dates(
        ctx, FORTS_CALENDAR, days_back=days_back, date_from=date_from, date_to=date_to
    )
    run_id = ctx.run.id
    with ctx.http(base_url=ISS_BASE_URL) as http:
        res = http.get_json(RFUD_SECURITIES, params={"iss.meta": "off", "iss.only": "securities"})
        with ctx.connection() as conn:
            cap = record_capture(
                conn, run_id, "moex_iss", "rfud_securities", None, res.response, PARSER_VERSION
            )
            ctx.run.add_counts(
                refresh_futures_contracts(
                    conn,
                    parse_futures_securities(res.data),
                    capture_id=cap,
                    run_id=run_id,
                    today=ctx.today(),
                )
            )
        for d in dates:
            with ctx.connection() as conn:
                rows: list[dict[str, Any]] = []
                skipped: list[dict[str, Any]] = []
                for page in iter_pages(http, FORTS_HISTORY, {"date": d.isoformat()}):
                    cap = record_capture(
                        conn,
                        run_id,
                        "moex_iss",
                        "forts_history",
                        None,
                        page.response,
                        PARSER_VERSION,
                    )
                    parsed, spreads = parse_forts_history(page.rows)
                    rows.extend(_provenance(parsed, cap, run_id))
                    skipped.extend(spreads)
                wrong = {r["trade_date"] for r in rows} - {d}
                if wrong:
                    raise IssFormatError(f"FORTS history for {d} returned dates {sorted(wrong)}")
                if not rows:
                    note = f"ISS FORTS history returned 0 rows for {d} (mode={mode})"
                    ctx.logger.warning(note)
                    if mode != "prelim":
                        record_no_data(conn, "futures_eod", d, note, today=ctx.today())
                    continue
                _ensure_contracts(ctx, http, conn, rows)
                specs = load_specs(conn)
                for r in rows:
                    factor = _eod_factor(specs.get(r["secid"]))
                    r["settle_rub_per_unit"] = normalize_price(r["settle_price"], factor)
                    r["close_rub_per_unit"] = normalize_price(r["close"], factor)
                    r.pop("short_name")
                ctx.run.add_counts(
                    upsert_rows(conn, "md.futures_eod", rows, EOD_KEY, run_id=run_id)
                )
                close_gap(conn, "futures_eod", d)
                ctx.logger.info(
                    "futures_eod %s: %d rows (%d spreads skipped)", d, len(rows), len(skipped)
                )


def _ensure_contracts(
    ctx: JobContext,
    http: HttpClient,
    conn: psycopg.Connection[Any],
    rows: list[dict[str, Any]],
) -> None:
    """Add ref rows for contracts in history but not in ``ref.futures_contract`` (expired):
    FX ones from ``/securities/{secid}.json``, others minimal (insert-only)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT secid FROM ref.futures_contract")
        known = {str(r[0]) for r in cur.fetchall()}
    run_id = ctx.run.id
    minimal: list[dict[str, Any]] = []
    for r in rows:
        if r["secid"] in known:
            continue
        known.add(r["secid"])
        asset = fx_asset(r["asset_code"])
        if asset is not None:
            res = http.get_json(
                SECURITY_DESCRIPTION.format(secid=r["secid"]),
                params={"iss.meta": "off", "iss.only": "description"},
            )
            cap = record_capture(
                conn, run_id, "moex_iss", "security_description", None, res.response, PARSER_VERSION
            )
            spec = parse_security_description(res.data, r["secid"])
            ctx.run.add_counts(
                refresh_futures_contracts(
                    conn, [spec], capture_id=cap, run_id=run_id, today=ctx.today(), touch=False
                )
            )
            continue
        minimal.append(
            {
                "secid": r["secid"],
                "short_name": r["short_name"],
                "asset_code": r["asset_code"],
                "is_fx": False,
                "contract_type": "dated",
                "spec_source": "history",
                "first_seen": ctx.today(),
                "capture_id": r["capture_id"],
                "run_id": run_id,
            }
        )
    if minimal:
        ctx.run.add_counts(
            upsert_rows(conn, "ref.futures_contract", minimal, CONTRACT_KEY, [], run_id=run_id)
        )


# ------------------------------------------------------------------ futures_intraday


def _fx_listed(specs: Mapping[str, ContractSpec], today: date) -> list[str]:
    return sorted(
        s.secid
        for s in specs.values()
        if s.is_fx and s.last_seen is not None and s.last_seen >= today
    )


@job("futures_intraday", "FX futures marketdata (APIM realtime, ISS fallback), change-only")
def futures_intraday(ctx: JobContext) -> None:
    run_id = ctx.run.id
    today = ctx.today()
    key = ctx.settings.moex_api_key.get_secret_value() if ctx.settings.moex_api_key else ""
    fallback_reason: str | None = None if key else "MOEX_API_KEY not set"

    def fetch(apim: bool, path: str, params: dict[str, Any]) -> Any:
        if apim:
            with ctx.http(base_url=APIM_BASE_URL, headers=apim_headers(key)) as http:
                return http.get_json(path, params=params)
        with ctx.http(base_url=ISS_BASE_URL) as http:
            return http.get_json(path, params=params)

    with ctx.connection() as conn:
        specs = load_specs(conn)
    secids = _fx_listed(specs, today)
    if not secids:
        # Daily bootstrap: today's FX listing from the RFUD board (static data, public ISS).
        res = fetch(False, RFUD_SECURITIES, {"iss.meta": "off", "iss.only": "securities"})
        with ctx.connection() as conn:
            cap = record_capture(
                conn, run_id, "moex_iss", "rfud_securities", None, res.response, PARSER_VERSION
            )
            ctx.run.add_counts(
                refresh_futures_contracts(
                    conn,
                    parse_futures_securities(res.data),
                    capture_id=cap,
                    run_id=run_id,
                    today=today,
                )
            )
            specs = load_specs(conn)
        secids = _fx_listed(specs, today)
    if not secids:
        raise IssFormatError("no FX futures listed on RFUD")

    params = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities": ",".join(secids),
    }
    variant, source = "apim_marketdata", "moex_apim"
    res = None
    if key:
        try:
            res = fetch(True, FORTS_SECURITIES, params)
        except HttpError as exc:  # EntitlementError, 401, transport, ...
            fallback_reason = f"APIM {type(exc).__name__}"
    if res is None:
        variant, source = "iss_public", "moex_iss"
        res = fetch(False, FORTS_SECURITIES, params)
        ctx.run.mark_partial(f"{fallback_reason}; stored delayed public ISS")

    contracts = parse_futures_securities(res.data)
    factors = {
        c["secid"]: price_factor(c["step_price"], c["min_step"], c["lot_volume"])
        for c in contracts
        if c["is_fx"]
    }
    # FORTS trades in weekend sessions: a Monday pre-open snapshot carries Sunday's last
    # trade, so quotes older than SYSTIME are dated on the previous *trading* day.
    rows, latency = parse_futures_marketdata(
        res.data, factors, is_trading_day=lambda d: ctx.is_trading_day(d, FORTS_CALENDAR)
    )
    missing = set(secids) - {c["secid"] for c in contracts}
    if missing:
        ctx.logger.warning("FORTS marketdata lacks %s", sorted(missing))
    for r in rows:
        r["source"] = source
        r["source_variant"] = variant
    source_ts = max((r["source_ts"] for r in rows if r["source_ts"]), default=None)
    with ctx.connection() as conn:
        cap = record_capture(
            conn,
            run_id,
            source,
            f"intraday:futures_{'apim' if source == 'moex_apim' else 'iss'}_marketdata",
            None,
            res.response,
            PARSER_VERSION,
            source_ts=source_ts,
        )
        ctx.run.add_counts(
            refresh_futures_contracts(conn, contracts, capture_id=cap, run_id=run_id, today=today)
        )
        changed, unchanged = only_changed(
            conn,
            "md.futures_intraday",
            rows,
            INTRADAY_SERIES,
            FUTURES_INTRADAY_VALUE_COLUMNS,
            logger=ctx.logger,
        )
        _provenance(changed, cap, run_id)
        result = upsert_rows(conn, "md.futures_intraday", changed, INTRADAY_KEY, run_id=run_id)
        ctx.run.add_counts(result + UpsertResult(unchanged=unchanged))
    ctx.logger.info(
        "futures_intraday %s: %d rows, %d changed, latency %s",
        variant,
        len(rows),
        len(changed),
        latency,
    )


# --------------------------------------------------------------- algopack_fx_futures


def _fx_tickers(specs: Mapping[str, ContractSpec]) -> set[str]:
    tickers = set(DEFAULT_FUTOI_TICKERS)
    for s in specs.values():
        if not s.is_fx:
            continue
        if s.contract_type == "perpetual":
            tickers.add(s.secid)
        elif s.sec_type:
            tickers.add(s.sec_type)
    return tickers


def _bars(
    parsed: list[dict[str, Any]],
    specs: Mapping[str, ContractSpec],
    capture_id: int,
    run_id: int,
) -> list[dict[str, Any]]:
    """FX-contract tradestats bars with normalized prices and provenance."""
    out = []
    for r in parsed:
        spec = specs.get(r["secid"])
        if fx_asset(r["asset_code"]) is None and not (spec and spec.is_fx):
            continue
        factor = _session_factor(spec, r["trade_date"])
        r["pr_close_rub_per_unit"] = normalize_price(r["pr_close"], factor)
        r["pr_vwap_rub_per_unit"] = normalize_price(r["pr_vwap"], factor)
        r["capture_id"] = capture_id
        r["run_id"] = run_id
        out.append(r)
    return out


TRADESTATS_TABLE: Final = "md.algopack_fo_tradestats"


def algopack_window(ctx: JobContext) -> date:
    """First day of the AlgoPack rolling window today (``MDHUB_ALGOPACK_RETENTION_DAYS``)."""
    return rolling_window_start(ctx.today(), ctx.settings.algopack_retention_days)


@job("algopack_fx_futures", "AlgoPack FO tradestats + FUTOI for FX futures (rolling window)")
def algopack_fx_futures(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    full: bool | None = None,
) -> dict[str, Any]:
    """Today's 5-minute intervals (scheduled polls), or a per-contract backfill of
    ``date_from..date_to`` clamped to the rolling window (:func:`algopack_window`; a range
    that ends before the window raises :class:`~mdhub.jobs.JobParamError`).

    :param full: poll mode - ``True`` whole-day re-read, ``False`` incremental (still a
        re-read when holes are found), ``None`` (schedule) automatic: re-read every
        ``MDHUB_ALGOPACK_FULL_REREAD_MINUTES`` and on holes.
    """
    span = _backfill_span(ctx, date_from, date_to)
    if not ctx.settings.moex_api_key:
        raise JobSkipped("MOEX_API_KEY not set (AlgoPack needs APIM)")
    headers = apim_headers(ctx.settings.moex_api_key.get_secret_value())
    with ctx.connection() as conn:
        specs = load_specs(conn)
    with ctx.http(base_url=APIM_BASE_URL, headers=headers) as http:
        if span is None:
            return _algopack_poll(ctx, http, specs, full=full)
        start, end = span
        _algopack_backfill(ctx, http, specs, start, end)
        result: dict[str, Any] = {
            "mode": "backfill",
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
        }
        requested = date_from or date_to
        if requested is not None and requested < start:
            result["requested_from"] = requested.isoformat()
        return result


def _backfill_span(
    ctx: JobContext, date_from: date | None, date_to: date | None
) -> tuple[date, date] | None:
    """Backfill range clamped to the rolling window (``None``: a scheduled poll).

    :raises JobParamError: ``date_to`` before ``date_from``, or the whole range is before the
        window (nothing to load).
    """
    start = date_from or date_to
    end = date_to or date_from
    if start is None or end is None:
        return None
    if end < start:
        raise JobParamError("date_to is before date_from")
    window = algopack_window(ctx)
    if end < window:
        raise JobParamError(
            f"AlgoPack range {start}..{end} ends before the rolling window (from {window}, "
            f"MDHUB_ALGOPACK_RETENTION_DAYS={ctx.settings.algopack_retention_days}): "
            "nothing to load"
        )
    if start < window:
        ctx.logger.warning(
            "algopack backfill %s..%s clamped to the rolling window: %s..%s",
            start,
            end,
            window,
            end,
        )
        start = window
    return start, end


def _poll_secids(specs: Mapping[str, ContractSpec], today: date) -> list[str]:
    """FX contracts to poll today: listed recently (RFUD listing seen in the last week) or
    perpetual."""
    horizon = today - timedelta(days=7)
    return sorted(
        s.secid
        for s in specs.values()
        if s.is_fx
        and (s.contract_type == "perpetual" or (s.last_seen is not None and s.last_seen >= horizon))
        and (s.last_trade_date is None or s.last_trade_date >= today)
    )


def full_reread_due(now: datetime, every_min: int) -> bool:
    """Whether the poll at ``now`` (MSK) is a whole-day re-read: the first poll of every
    ``every_min``-minute block of the day (with the ``1-56/5`` schedule and 30: 07:01, 07:31,
    08:01 ...)."""
    minute = now.hour * 60 + now.minute
    return minute % every_min < POLL_EVERY_MIN


def session_span_start(ctx: JobContext, today: date) -> date:
    """First day of the weekend/holiday sessions (trading days that are not business days)
    immediately before ``today``, else ``today``: a Monday re-read also covers the bars of a
    Saturday/Sunday session."""
    start = today
    cur = today - timedelta(days=1)
    for _ in range(7):
        if ctx.is_business_day(cur, FORTS_CALENDAR) or not ctx.is_trading_day(cur, FORTS_CALENDAR):
            break
        start = cur
        cur -= timedelta(days=1)
    return start


def _algopack_poll(
    ctx: JobContext,
    http: HttpClient,
    specs: Mapping[str, ContractSpec],
    *,
    full: bool | None,
) -> dict[str, Any]:
    """One scheduled poll: incremental, or a whole-day re-read.

    * **incremental** (default): the ``latest=1`` all-contracts snapshot of tradestats (FX
      contracts kept) and FUTOI ``latest=1`` - ~2-3 requests (paged answers); captures
      ``intraday:algopack_fo_tradestats`` / ``intraday:futoi`` (rolling-window retention).
    * **re-read** - every ``MDHUB_ALGOPACK_FULL_REREAD_MINUTES`` (:func:`full_reread_due`),
      when :func:`interval_holes` finds holes before the poll, or ``full=True``: the whole
      current trading day per FX contract (``from=`` :func:`session_span_start`, ``till=today``)
      and per FUTOI ticker and day, filling bars published late or missed by failed polls.
      1 request per contract + 1 per FUTOI ticker and day (seed: 64 contracts, ~10 tickers:
      ~75 requests); captures ``intraday:reread:*`` (short retention,
      ``MDHUB_RAW_RETENTION_DAYS_REREAD``, default 7 days).

    Daily volume with the default schedule (204 polls 07:01-23:56) and a 30-minute re-read: 34
    re-reads x ~75 + 170 incremental polls x ~3 = **~3,100 APIM requests per trading day**
    (with obstats, dropped in 0423: ~5,400). Without known FX contracts (fresh database)
    the incremental snapshot is used (bootstrap). Ends with the interval completeness check.
    """
    today = ctx.today()
    now = msk_now()
    secids = _poll_secids(specs, today)
    if not secids:
        _algopack_snapshot(ctx, http, specs)
        return {"mode": "latest_snapshot", "contracts": 0}
    with ctx.connection() as conn:
        holes_before = interval_holes(conn, today, now=now)
    reread = (
        full
        if full is not None
        else full_reread_due(now, ctx.settings.algopack_full_reread_minutes)
    ) or bool(holes_before)
    if reread:
        start = session_span_start(ctx, today)
        _algopack_reread(ctx, http, specs, secids, start, today)
        mode = "reread"
    else:
        _algopack_snapshot(ctx, http, specs)
        mode = "incremental"
    with ctx.connection() as conn:
        holes = interval_holes(conn, today, now=now)
    if holes:
        shown = ", ".join(h.strftime("%H:%M") for h in holes[:12])
        ctx.logger.warning("algopack %s: missing 5-min intervals %s", today, shown)
        ctx.run.mark_partial(
            f"AlgoPack FX tradestats {today}: {len(holes)} missing 5-min interval(s) "
            f"older than {INTERVAL_GRACE}: {shown}"
        )
    return {
        "mode": mode,
        "contracts": len(secids),
        "holes_before": len(holes_before),
        "missing_intervals": [h.isoformat() for h in holes],
    }


def _algopack_reread(
    ctx: JobContext,
    http: HttpClient,
    specs: Mapping[str, ContractSpec],
    secids: list[str],
    start: date,
    today: date,
) -> None:
    """Whole-day re-read ``start..today`` per FX contract and FUTOI ticker."""
    run_id = ctx.run.id
    span = {"from": start.isoformat(), "till": today.isoformat()}
    for secid in secids:
        path = ALGOPACK_FO_TRADESTATS_SECID.format(secid=secid)
        with ctx.connection() as conn:
            rows: list[dict[str, Any]] = []
            for page in iter_pages(http, path, span, block="data"):
                cap = record_capture(
                    conn,
                    run_id,
                    "moex_apim",
                    f"{REREAD_ENDPOINT_PREFIX}algopack_fo_tradestats",
                    None,
                    page.response,
                    APIM_PARSER_VERSION,
                )
                rows.extend(_bars(parse_tradestats(page.rows), specs, cap, run_id))
            rows = [r for r in rows if r["secid"] == secid and start <= r["trade_date"] <= today]
            ctx.run.add_counts(
                upsert_rows(conn, TRADESTATS_TABLE, _dedup(rows, BAR_KEY), BAR_KEY, run_id=run_id)
            )
    tickers = sorted(_fx_tickers(specs))
    day = start
    while day <= today:
        _futoi_day(ctx, http, tickers, day, endpoint=f"{REREAD_ENDPOINT_PREFIX}futoi")
        day += timedelta(days=1)


def _algopack_snapshot(
    ctx: JobContext, http: HttpClient, specs: Mapping[str, ContractSpec]
) -> None:
    """``latest=1`` snapshot of all contracts (bootstrap without known FX contracts)."""
    run_id = ctx.run.id
    with ctx.connection() as conn:
        rows: list[dict[str, Any]] = []
        for page in iter_pages(http, ALGOPACK_FO_TRADESTATS, {"latest": 1}, block="data"):
            cap = record_capture(
                conn,
                run_id,
                "moex_apim",
                "intraday:algopack_fo_tradestats",
                None,
                page.response,
                APIM_PARSER_VERSION,
            )
            rows.extend(_bars(parse_tradestats(page.rows), specs, cap, run_id))
        ctx.run.add_counts(
            upsert_rows(conn, TRADESTATS_TABLE, _dedup(rows, BAR_KEY), BAR_KEY, run_id=run_id)
        )
    tickers = _fx_tickers(specs)
    res = http.get_json(FUTOI_ALL, params={"iss.meta": "off", "latest": 1})
    with ctx.connection() as conn:
        cap = record_capture(
            conn, run_id, "moex_apim", "intraday:futoi", None, res.response, APIM_PARSER_VERSION
        )
        raw = parse_block(res.data, "futoi")
        if len(raw) >= FUTOI_MAX_ROWS:
            raise IssPagingError(f"FUTOI latest: {len(raw)} rows, page may be truncated")
        parsed = parse_futoi(raw)
        rows = _provenance([r for r in parsed if r["ticker"] in tickers], cap, run_id)
        ctx.run.add_counts(
            upsert_rows(
                conn, "md.algopack_futoi", _dedup(rows, FUTOI_KEY), FUTOI_KEY, run_id=run_id
            )
        )


def _in_break(t: dt_time) -> bool:
    return any(lo <= t <= hi for lo, hi in ALGOPACK_BREAKS)


def interval_holes(conn: psycopg.Connection[Any], day: date, *, now: datetime) -> list[datetime]:
    """Missing 5-minute intervals of ``day`` in ``md.algopack_fo_tradestats`` (FX contracts,
    union over contracts) between the first and the last stored interval, ignoring the
    clearing breaks (:data:`ALGOPACK_BREAKS`) and intervals younger than
    :data:`INTERVAL_GRACE` (publication latency). The most liquid FX futures trade in every
    session interval, so a hole in the union is a lost bar."""
    with conn.cursor(row_factory=tuple_row) as cur:
        stamps = sorted(
            r[0].astimezone(MSK)
            for r in cur.execute(
                """
                SELECT DISTINCT t.interval_ts FROM md.algopack_fo_tradestats t
                JOIN ref.futures_contract c ON c.secid = t.secid AND c.is_fx
                WHERE t.trade_date = %s
                """,
                (day,),
            ).fetchall()
        )
    if len(stamps) < 2:
        return []
    have = set(stamps)
    holes: list[datetime] = []
    cur_ts = stamps[0] + INTERVAL
    while cur_ts < stamps[-1]:
        if cur_ts not in have and not _in_break(cur_ts.time()) and cur_ts < now - INTERVAL_GRACE:
            holes.append(cur_ts)
        cur_ts += INTERVAL
    return holes


def _backfill_secids(
    conn: psycopg.Connection[Any], specs: Mapping[str, ContractSpec], start: date, end: date
) -> list[str]:
    """FX contracts that traded in ``start..end`` (md.futures_eod) or whose life overlaps it."""
    fx_assets = sorted({s.asset_code for s in specs.values() if s.is_fx and s.asset_code})
    with conn.cursor(row_factory=tuple_row) as cur:
        traded = {
            r[0]
            for r in cur.execute(
                """
                SELECT DISTINCT secid FROM md.futures_eod
                WHERE trade_date BETWEEN %s AND %s AND num_trades > 0
                  AND asset_code = ANY(%s)
                """,
                (start, end, fx_assets),
            ).fetchall()
        }
    for s in specs.values():
        if not s.is_fx:
            continue
        begins = s.first_trade_date is None or s.first_trade_date <= end
        alive = s.contract_type == "perpetual" or (
            s.last_trade_date is not None and s.last_trade_date >= start
        )
        if begins and alive and s.first_trade_date is not None:
            traded.add(s.secid)
    return sorted(traded)


def _chunks(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    cur = start
    while cur <= end:
        last = min(end, cur + timedelta(days=BACKFILL_CHUNK_DAYS - 1))
        out.append((cur, last))
        cur = last + timedelta(days=1)
    return out


def _algopack_backfill(
    ctx: JobContext,
    http: HttpClient,
    specs: Mapping[str, ContractSpec],
    start: date,
    end: date,
) -> None:
    run_id = ctx.run.id
    with ctx.connection() as conn:
        secids = _backfill_secids(conn, specs, start, end)
    if not secids:
        ctx.run.mark_partial(f"no FX futures known for {start}..{end} (run futures_eod first)")
    for c_start, c_end in _chunks(start, end):
        span = {"from": c_start.isoformat(), "till": c_end.isoformat()}
        for secid in secids:
            path = ALGOPACK_FO_TRADESTATS_SECID.format(secid=secid)
            with ctx.connection() as conn:
                rows: list[dict[str, Any]] = []
                for page in iter_pages(http, path, span, block="data"):
                    cap = record_capture(
                        conn,
                        run_id,
                        "moex_apim",
                        "algopack_fo_tradestats_history",
                        None,
                        page.response,
                        APIM_PARSER_VERSION,
                    )
                    rows.extend(_bars(parse_tradestats(page.rows), specs, cap, run_id))
                rows = [
                    r for r in rows if r["secid"] == secid and c_start <= r["trade_date"] <= c_end
                ]
                ctx.run.add_counts(
                    upsert_rows(
                        conn, TRADESTATS_TABLE, _dedup(rows, BAR_KEY), BAR_KEY, run_id=run_id
                    )
                )
        tickers = sorted(_fx_tickers(specs))
        day = c_start
        while day <= c_end:
            if ctx.is_trading_day(day, FORTS_CALENDAR):
                _futoi_day(ctx, http, tickers, day)
            day += timedelta(days=1)
        ctx.logger.info("algopack backfill %s..%s: %d contracts", c_start, c_end, len(secids))


def _futoi_day(
    ctx: JobContext,
    http: HttpClient,
    tickers: list[str],
    day: date,
    *,
    endpoint: str = "futoi_history",
) -> None:
    """FUTOI of one trading day for each FX ticker (one request per ticker)."""
    run_id = ctx.run.id
    span = {"iss.meta": "off", "from": day.isoformat(), "till": day.isoformat()}
    with ctx.connection() as conn:
        rows: list[dict[str, Any]] = []
        for ticker in tickers:
            res = http.get_json(FUTOI_TICKER.format(ticker=ticker.lower()), params=span)
            cap = record_capture(
                conn, run_id, "moex_apim", endpoint, None, res.response, APIM_PARSER_VERSION
            )
            raw = parse_block(res.data, "futoi")
            if len(raw) >= FUTOI_MAX_ROWS:
                raise IssPagingError(
                    f"FUTOI {ticker} {day}: {len(raw)} rows, page may be truncated"
                )
            parsed = parse_futoi(raw)
            rows.extend(_provenance([r for r in parsed if r["trade_date"] == day], cap, run_id))
        ctx.run.add_counts(
            upsert_rows(
                conn, "md.algopack_futoi", _dedup(rows, FUTOI_KEY), FUTOI_KEY, run_id=run_id
            )
        )
