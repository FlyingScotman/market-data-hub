"""Bank of Russia loaders: fixings, official FX, liquidity balance and board meetings.

Jobs
====

``cbr_daily(date_from=None, date_to=None)`` (09:00, 14:30, 16:30, 18:30, all days) loads, each
part in its own transaction (a failing part marks the run ``partial``, the others commit):

* ``KEY_RATE`` (``/hd_base/KeyRate/``) - one row per CBR business day, ``fixing_date`` =
  ``effective_date`` = the day the rate is in force,
* ``RUONIA`` (``/hd_base/ruonia/dynamics/``) - by observation date with the CBR publication date
  (``published_on``) and the statistics in ``details``,
* ``RUONIA_INDEX`` and ``TERM_RUONIA_1M/3M/6M`` (XLSX ``DownloadExcel/125022``, calendar days),
* ``CBR_DEPOSIT_STANDING`` / ``CBR_LENDING_STANDING`` (``/hd_base/ProcStav/IR_MPO/``),
* ``CBR_REFINANCING_RATE`` (``/statistics/idkp_br/refinancing_rates1/``) - one row per
  calendar day 1992-01-01..2015-12-31 (closed series: from 2016 the refinancing rate equals
  ``KEY_RATE``); read only while not stored or for an explicit range before 2016,
* liquidity balance -> ``md.cbr_liquidity`` (revision counter + ``ops.row_history``),
* official FX of all currencies -> ``md.fx_official`` (see :func:`expand_fx_records`).

Without dates the job re-reads a recent window (fixings 14 days, liquidity 30 days, FX: the
latest and the previous CBR record); a series with no stored rows at all is seeded from its
history start instead. With ``date_from``/``date_to`` (manual runs, ``ops.backfill_task``) the
range is loaded. FX ranges take the exact record dates of the daily set from the USD history
(``XML_dynamic``, one request) and then one ``XML_daily`` per record date (~250 requests per
year at 1 request/second), committed in :data:`FX_CHUNK_DAYS`-day chunks.

``cbr_meetings(mode="calendar")`` (first business day of the month) refreshes
``ref.cbr_meeting`` from ``/dkp/cal_mp/`` and the business-day calendars
(:func:`mdhub.loaders.calendar.refresh_calendar`); ``mode="calendar_full"`` re-seeds the
calendars from 2019; ``mode="post_meeting"`` (daily 15:00) is a no-op unless a meeting took place
in the last :data:`POST_MEETING_DAYS` days and is not yet confirmed - then it reloads the key
rate and resolves the decision.

Known-at: ``ingested_at`` of ``md.fixing``/``md.fx_official``/``md.cbr_liquidity`` is set to the
fetch time and refreshed only when a value changes, so it is the time the hub learnt the value;
the previous image goes to ``ops.row_history``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import psycopg
from psycopg.rows import dict_row

from mdhub.core.capture import last_capture_sha256, record_capture
from mdhub.core.errors import FormatDriftError, MdhubError
from mdhub.core.http import HttpClient
from mdhub.core.timeutil import date_range, utc_now
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.sources import cbr as src

__all__ = [
    "FIXING_KEY",
    "FX_CHUNK_DAYS",
    "FxObservation",
    "capture_unless_unchanged",
    "cbr_daily",
    "cbr_meetings",
    "expand_fx_records",
    "fixing_row",
    "resolve_meetings",
]

log = logging.getLogger(__name__)

SOURCE: Final = "cbr"
RU_CALENDAR: Final = "RU_PRODUCTION"
FIXING_KEY: Final = ["index_code", "fixing_date", "source"]
FX_KEY: Final = ["effective_date", "ccy"]
WINDOW_FIXINGS_DAYS: Final = 14
WINDOW_LIQUIDITY_DAYS: Final = 30
FX_CHUNK_DAYS: Final = 92
USD_CBR_ID: Final = "R01235"
LIQUIDITY_CHUNK_DAYS: Final = 366
POST_MEETING_DAYS: Final = 4

KEY_RATE_START: Final = date(2013, 9, 13)
RUONIA_START: Final = date(2010, 1, 1)
STANDING_START: Final = date(2014, 1, 1)
LIQUIDITY_START: Final = date(2019, 1, 1)
FX_START: Final = date(1998, 1, 1)
"""Official FX history from the 1998 redenomination (earlier rates are in old roubles)."""
REFINANCING_CODE: Final = "CBR_REFINANCING_RATE"
REFINANCING_END: Final = src.REFINANCING_MERGED_ON - timedelta(days=1)

# ------------------------------------------------------------------------ datasets


def _fixing_dataset(
    name: str,
    where: str,
    frequency: Any,
    calendar: str | None,
    max_lag: int,
    start: date,
    description: str,
    *,
    series: tuple[str, ...] = (),
) -> None:
    register_dataset(
        DatasetSpec(
            name=name,
            table="md.fixing",
            date_column="fixing_date",
            calendar=calendar,
            frequency=frequency,
            max_lag_days=max_lag,
            backfill_job="cbr_daily",
            history_start=start,
            series_column="index_code" if series else None,
            series=series,
            where=where,
            description=description,
        )
    )


_fixing_dataset(
    "cbr_key_rate",
    "index_code = 'KEY_RATE' AND source = 'cbr'",
    "business_daily",
    RU_CALENDAR,
    1,
    date(2013, 9, 17),
    "CBR key rate per CBR business day (effective date)",
)
_fixing_dataset(
    "cbr_ruonia",
    "index_code = 'RUONIA' AND source = 'cbr'",
    "business_daily",
    RU_CALENDAR,
    2,
    date(2010, 1, 11),
    "RUONIA by observation date (published next business day)",
)
_fixing_dataset(
    "cbr_ruonia_index",
    "index_code IN ('RUONIA_INDEX', 'TERM_RUONIA_1M', 'TERM_RUONIA_3M', 'TERM_RUONIA_6M') "
    "AND source = 'cbr'",
    "daily",
    None,
    2,
    date(2010, 1, 11),
    "RUONIA compounded index and term RUONIA 1M/3M/6M per calendar day",
    series=("RUONIA_INDEX", "TERM_RUONIA_1M", "TERM_RUONIA_3M", "TERM_RUONIA_6M"),
)
_fixing_dataset(
    "cbr_standing_rates",
    "index_code IN ('CBR_DEPOSIT_STANDING', 'CBR_LENDING_STANDING') AND source = 'cbr'",
    "business_daily",
    RU_CALENDAR,
    1,
    date(2014, 2, 3),
    "CBR standing deposit / lending facility rates",
    series=("CBR_DEPOSIT_STANDING", "CBR_LENDING_STANDING"),
)
register_dataset(
    DatasetSpec(
        name="cbr_refinancing_rate",
        table="md.fixing",
        date_column="fixing_date",
        calendar=None,
        frequency="daily",
        max_lag_days=0,
        backfill_job="cbr_daily",
        history_start=src.REFINANCING_FIRST_DAY,
        history_end=REFINANCING_END,
        where="index_code = 'CBR_REFINANCING_RATE' AND source = 'cbr'",
        description="CBR refinancing rate per calendar day, 1992-2015 (closed: equal to the "
        "key rate from 2016)",
    )
)
register_dataset(
    DatasetSpec(
        name="cbr_fx_official",
        table="md.fx_official",
        date_column="effective_date",
        calendar=None,
        frequency="daily",
        max_lag_days=0,
        backfill_job="cbr_daily",
        history_start=FX_START,
        # 17-18 currencies in 2005-2009, 59 today: the floor catches a partial record only
        min_rows_per_day=15,
        description="CBR official FX, all currencies, every calendar day by effective date",
    )
)
register_dataset(
    DatasetSpec(
        name="cbr_liquidity",
        table="md.cbr_liquidity",
        date_column="observed_on",
        calendar=RU_CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="cbr_daily",
        history_start=LIQUIDITY_START,
        description="CBR banking-sector liquidity balance (revisions in ops.row_history)",
    )
)
register_dataset(
    DatasetSpec(
        name="cbr_meetings",
        table="ref.cbr_meeting",
        date_column="meeting_date",
        calendar=None,
        frequency="event",
        max_lag_days=400,
        description="CBR key-rate board meetings (schedule + decisions)",
    )
)

# -------------------------------------------------------------------- shared helpers


def capture_unless_unchanged(
    conn: psycopg.Connection[Any],
    run_id: int,
    source: str,
    endpoint: str,
    response: httpx.Response,
    parser_version: str,
    *,
    source_ts: datetime | None = None,
) -> int | None:
    """Record ``response`` in ``ops.raw_capture`` unless its body is byte-identical to the
    latest capture of ``(source, endpoint)`` (then ``None``: nothing new to parse)."""
    digest = hashlib.sha256(response.content).hexdigest()
    if last_capture_sha256(conn, source, endpoint) == digest:
        return None
    return record_capture(
        conn, run_id, source, endpoint, None, response, parser_version, source_ts=source_ts
    )


def _jsonable(values: Mapping[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in values.items() if v is not None}


def fixing_row(
    index_code: str,
    fixing_date: date,
    value: Decimal,
    *,
    capture_id: int,
    run_id: int,
    known_at: datetime,
    unit: str = "pct",
    effective_date: date | None = None,
    published_on: date | None = None,
    details: Mapping[str, Any] | None = None,
    source: str = SOURCE,
) -> dict[str, Any]:
    """A ``md.fixing`` row (``ingested_at`` = known-at, refreshed only on change)."""
    return {
        "index_code": index_code,
        "fixing_date": fixing_date,
        "source": source,
        "value": value,
        "unit": unit,
        "effective_date": effective_date,
        "published_on": published_on,
        "published_at": None,
        "details": _jsonable(details) if details else None,
        "capture_id": capture_id,
        "run_id": run_id,
        "ingested_at": known_at,
    }


@dataclass(slots=True)
class _Window:
    date_from: date
    date_to: date
    explicit: bool


def _has_rows(conn: psycopg.Connection[Any], sql_text: str, params: Sequence[Any]) -> bool:
    row = conn.execute(sql_text, params).fetchone()
    return row is not None


def _fixings_window(
    conn: psycopg.Connection[Any],
    win: _Window,
    codes: Sequence[str],
    start: date,
    window_days: int,
) -> tuple[date, date]:
    if win.explicit:
        return win.date_from, win.date_to
    if not _has_rows(
        conn,
        "SELECT 1 FROM md.fixing WHERE index_code = ANY(%s) AND source = %s LIMIT 1",
        (list(codes), SOURCE),
    ):
        return start, win.date_to
    return win.date_to - timedelta(days=window_days), win.date_to


# --------------------------------------------------------------------- fixing parts


def _load_key_rate(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    d1, d2 = _fixings_window(conn, win, ["KEY_RATE"], KEY_RATE_START, WINDOW_FIXINGS_DAYS)
    resp = src.fetch_hd_base(http, src.KEY_RATE_PATH, d1, d2)
    known_at = utc_now()
    cap = capture_unless_unchanged(conn, ctx.run.id, SOURCE, "key_rate", resp, src.PARSER_VERSION)
    if cap is None:
        return UpsertResult()
    rows = [
        fixing_row(
            "KEY_RATE", d, v, capture_id=cap, run_id=ctx.run.id, known_at=known_at, effective_date=d
        )
        for d, v in src.parse_key_rate_page(resp.text)
    ]
    return upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)


def _load_ruonia(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    d1, d2 = _fixings_window(conn, win, ["RUONIA"], RUONIA_START, WINDOW_FIXINGS_DAYS)
    resp = src.fetch_hd_base(http, src.RUONIA_PATH, d1, d2)
    known_at = utc_now()
    cap = capture_unless_unchanged(conn, ctx.run.id, SOURCE, "ruonia", resp, src.PARSER_VERSION)
    if cap is None:
        return UpsertResult()
    rows = []
    for r in src.parse_ruonia_page(resp.text):
        details = {
            "volume_bn": r.volume_bn,
            "deals": r.deals,
            "participants": r.participants,
            "min_pct": r.min_pct,
            "p25_pct": r.p25_pct,
            "p75_pct": r.p75_pct,
            "max_pct": r.max_pct,
            "status": r.status,
        }
        rows.append(
            fixing_row(
                "RUONIA",
                r.fixing_date,
                r.rate_pct,
                capture_id=cap,
                run_id=ctx.run.id,
                known_at=known_at,
                effective_date=r.fixing_date,
                published_on=r.published_on,
                details=details,
            )
        )
    return upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)


def _load_ruonia_index(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    d1, d2 = _fixings_window(conn, win, ["RUONIA_INDEX"], RUONIA_START, WINDOW_FIXINGS_DAYS)
    resp = src.fetch_ruonia_index_xlsx(http, d1, d2)
    known_at = utc_now()
    cap = capture_unless_unchanged(
        conn, ctx.run.id, SOURCE, "ruonia_index_xlsx", resp, src.PARSER_VERSION
    )
    if cap is None:
        return UpsertResult()
    rows = []
    for r in src.parse_ruonia_index_xlsx(resp.content):
        values: list[tuple[str, Decimal | None, str]] = [
            ("RUONIA_INDEX", r.index, "index"),
            ("TERM_RUONIA_1M", r.term_1m_pct, "pct"),
            ("TERM_RUONIA_3M", r.term_3m_pct, "pct"),
            ("TERM_RUONIA_6M", r.term_6m_pct, "pct"),
        ]
        for code, value, unit in values:
            if value is None:
                continue
            rows.append(
                fixing_row(
                    code,
                    r.fixing_date,
                    value,
                    capture_id=cap,
                    run_id=ctx.run.id,
                    known_at=known_at,
                    unit=unit,
                    effective_date=r.fixing_date,
                )
            )
    return upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)


def _load_standing_rates(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    d1, d2 = _fixings_window(
        conn, win, ["CBR_DEPOSIT_STANDING"], STANDING_START, WINDOW_FIXINGS_DAYS
    )
    resp = src.fetch_hd_base(http, src.STANDING_RATES_PATH, d1, d2)
    known_at = utc_now()
    cap = capture_unless_unchanged(
        conn, ctx.run.id, SOURCE, "standing_rates", resp, src.PARSER_VERSION
    )
    if cap is None:
        return UpsertResult()
    rows = []
    for r in src.parse_standing_rates_page(resp.text):
        for code, value in (
            ("CBR_DEPOSIT_STANDING", r.deposit_standing_pct),
            ("CBR_LENDING_STANDING", r.lending_standing_pct),
        ):
            if value is not None:
                rows.append(
                    fixing_row(
                        code,
                        r.rate_date,
                        value,
                        capture_id=cap,
                        run_id=ctx.run.id,
                        known_at=known_at,
                        effective_date=r.rate_date,
                    )
                )
    return upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)


def _load_refinancing_rate(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    """Closed series: loaded when empty or when an explicit range reaches back before 2016."""
    if win.explicit:
        if win.date_from > REFINANCING_END:
            return UpsertResult()
        lo, hi = max(win.date_from, src.REFINANCING_FIRST_DAY), min(win.date_to, REFINANCING_END)
    elif _has_rows(
        conn,
        "SELECT 1 FROM md.fixing WHERE index_code = %s AND source = %s LIMIT 1",
        (REFINANCING_CODE, SOURCE),
    ):
        return UpsertResult()
    else:
        lo, hi = src.REFINANCING_FIRST_DAY, REFINANCING_END
    resp = src.fetch_refinancing_page(http)
    known_at = utc_now()
    cap = record_capture(
        conn, ctx.run.id, SOURCE, "refinancing_rate", None, resp, src.PARSER_VERSION
    )
    rows = []
    for p in src.parse_refinancing_page(resp.text):
        details = {
            "period_from": p.date_from,
            "period_to": p.date_to,
            "document": p.document,
        }
        for d in date_range(max(p.date_from, lo), min(p.date_to, hi)):
            rows.append(
                fixing_row(
                    REFINANCING_CODE,
                    d,
                    p.rate_pct,
                    capture_id=cap,
                    run_id=ctx.run.id,
                    known_at=known_at,
                    effective_date=d,
                    details=details,
                )
            )
    return upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)


# ------------------------------------------------------------------------- liquidity


def _methodology(d: date) -> str:
    return "cbr_bliquidity_v2023_11" if d >= date(2023, 11, 1) else "cbr_bliquidity_pre_2023_11"


def liquidity_rows(
    conn: psycopg.Connection[Any],
    parsed: Sequence[src.LiquidityRow],
    *,
    capture_id: int,
    run_id: int,
    known_at: datetime,
) -> list[dict[str, Any]]:
    """Rows for ``md.cbr_liquidity``; ``revision_no`` is incremented for changed dates."""
    if not parsed:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        existing = {
            r["observed_on"]: r
            for r in cur.execute(
                "SELECT * FROM md.cbr_liquidity WHERE observed_on = ANY(%s)",
                ([p.observed_on for p in parsed],),
            ).fetchall()
        }
    rows = []
    for p in parsed:
        row: dict[str, Any] = {"observed_on": p.observed_on, **p.values}
        row["methodology_version"] = _methodology(p.observed_on)
        old = existing.get(p.observed_on)
        if old is None:
            row["revision_no"] = 1
        elif all(old[c] == row[c] for c in (*src.LIQUIDITY_COLUMNS, "methodology_version")):
            row["revision_no"] = old["revision_no"]
        else:
            row["revision_no"] = int(old["revision_no"]) + 1
        row.update(capture_id=capture_id, run_id=run_id, ingested_at=known_at)
        rows.append(row)
    return rows


def _load_liquidity(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    if win.explicit:
        d1, d2 = win.date_from, win.date_to
    elif _has_rows(conn, "SELECT 1 FROM md.cbr_liquidity LIMIT 1", ()):
        d1, d2 = win.date_to - timedelta(days=WINDOW_LIQUIDITY_DAYS), win.date_to
    else:
        d1, d2 = LIQUIDITY_START, win.date_to
    total = UpsertResult()
    for c1, c2 in src.chunk_range(d1, d2, LIQUIDITY_CHUNK_DAYS):
        resp = src.fetch_hd_base(http, src.LIQUIDITY_PATH, c1, c2)
        known_at = utc_now()
        cap = capture_unless_unchanged(
            conn, ctx.run.id, SOURCE, "liquidity", resp, src.PARSER_VERSION
        )
        if cap is None:
            continue
        rows = liquidity_rows(
            conn,
            src.parse_liquidity_page(resp.text),
            capture_id=cap,
            run_id=ctx.run.id,
            known_at=known_at,
        )
        total += upsert_rows(conn, "md.cbr_liquidity", rows, ["observed_on"], run_id=ctx.run.id)
    return total


# -------------------------------------------------------------------------------- FX


@dataclass(frozen=True, slots=True)
class FxObservation:
    record_date: date
    rate: src.FxRate
    capture_id: int


def expand_fx_records(
    observations: Iterable[FxObservation],
    is_business_day: Callable[[date], bool],
    *,
    lower: date | None = None,
    known_through: date | None = None,
    known_at: datetime,
    run_id: int,
) -> list[dict[str, Any]]:
    """Turn CBR records into one ``md.fx_official`` row per calendar day in force.

    Per currency, record *i* is in force from its ``record_date`` until the day before record
    *i+1*; the latest record until the first RU business day on or after its
    ``record_date`` (the next CBR setting day - the rate set that day starts the next
    record), and at least through ``known_through`` (the caller has checked that no later
    record exists up to that day - e.g. CBR did not set rates on the 2020-03-30..04-06
    non-working days) for currencies present in the overall latest record.
    ``set_date`` = ``record_date - 1``. Days before ``lower`` are dropped.
    """
    by_ccy: dict[str, dict[date, tuple[src.FxRate, int]]] = {}
    latest: date | None = None
    for obs in observations:
        by_ccy.setdefault(obs.rate.ccy, {})[obs.record_date] = (obs.rate, obs.capture_id)
        latest = obs.record_date if latest is None else max(latest, obs.record_date)
    rows: list[dict[str, Any]] = []
    for ccy, recs in by_ccy.items():
        dates = sorted(recs)
        for i, rd in enumerate(dates):
            rate, cap = recs[rd]
            if i + 1 < len(dates):
                end = dates[i + 1] - timedelta(days=1)
            else:
                end = rd
                for _ in range(60):
                    if is_business_day(end):
                        break
                    end += timedelta(days=1)
                if known_through is not None and rd == latest:
                    end = max(end, known_through)
            for eff in date_range(rd, end):
                if lower is not None and eff < lower:
                    continue
                rows.append(
                    {
                        "effective_date": eff,
                        "ccy": ccy,
                        "nominal": rate.nominal,
                        "value": rate.value,
                        "rate_per_unit": rate.rate_per_unit,
                        "vunit_rate": rate.vunit_rate,
                        "record_date": rd,
                        "set_date": rd - timedelta(days=1),
                        "cbr_id": rate.cbr_id,
                        "source": SOURCE,
                        "capture_id": cap,
                        "run_id": run_id,
                        "ingested_at": known_at,
                    }
                )
    return rows


def _daily_record(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], day: date
) -> tuple[src.FxRecord, int]:
    resp = src.fetch_xml_daily(http, day)
    cap = record_capture(conn, ctx.run.id, SOURCE, "fx_xml_daily", None, resp, src.PARSER_VERSION)
    record = src.parse_xml_daily(resp.content)
    if record.record_date > day:
        raise FormatDriftError(f"CBR XML_daily: record {record.record_date} after requested {day}")
    return record, cap


def _load_fx(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any], win: _Window
) -> UpsertResult:
    today = ctx.today()
    known_at = utc_now()
    obs: list[FxObservation] = []
    lower: date | None
    known_through: date
    explicit = win.explicit
    d1, d2 = win.date_from, win.date_to
    if not explicit:
        last, cap_last = _daily_record(ctx, http, conn, today + timedelta(days=1))
        prev, cap_prev = _daily_record(ctx, http, conn, last.record_date - timedelta(days=1))
        for rec, cap in ((prev, cap_prev), (last, cap_last)):
            obs.extend(FxObservation(rec.record_date, r, cap) for r in rec.rates)
        lower = prev.record_date
        known_through = today  # the record returned for today+1 is in force today
    else:
        upper = min(d2 + timedelta(days=1), today + timedelta(days=1))
        lower = d1
        known_through = min(upper, today)  # XML_dynamic lists every record up to upper
        # Exact record dates of the daily set from the USD history (one request), then one
        # XML_daily per record date. (XML_dynamic per currency is cheaper but also returns
        # the monthly-set rates of currencies that were not in the daily set at the time.)
        resp = src.fetch_xml_dynamic(http, USD_CBR_ID, d1 - timedelta(days=31), upper)
        record_capture(conn, ctx.run.id, SOURCE, "fx_xml_dynamic", None, resp, src.PARSER_VERSION)
        record_dates = [r.record_date for r in src.parse_xml_dynamic(resp.content, "USD")]
        before = [d for d in record_dates if d <= d1]
        wanted = before[-1:] + [d for d in record_dates if d > d1]
        for rd in wanted:
            rec, cap = _daily_record(ctx, http, conn, rd)
            if rec.record_date != rd:
                raise FormatDriftError(f"CBR XML_daily({rd}) returned record {rec.record_date}")
            obs.extend(FxObservation(rec.record_date, r, cap) for r in rec.rates)
    rows = expand_fx_records(
        obs,
        lambda d: ctx.is_business_day(d, RU_CALENDAR),
        lower=lower,
        known_through=known_through,
        known_at=known_at,
        run_id=ctx.run.id,
    )
    return upsert_rows(conn, "md.fx_official", rows, FX_KEY, run_id=ctx.run.id)


# ---------------------------------------------------------------------------- job

_Part = Callable[[JobContext, HttpClient, psycopg.Connection[Any], _Window], UpsertResult]
PARTS: Final[tuple[tuple[str, _Part], ...]] = (
    ("key_rate", _load_key_rate),
    ("ruonia", _load_ruonia),
    ("ruonia_index", _load_ruonia_index),
    ("standing_rates", _load_standing_rates),
    ("refinancing_rate", _load_refinancing_rate),
    ("liquidity", _load_liquidity),
    ("fx_official", _load_fx),
)


def _fx_windows(ctx: JobContext, win: _Window) -> list[_Window]:
    """FX ranges in commit chunks; an empty table is seeded from :data:`FX_START`."""
    d1, d2, explicit = win.date_from, win.date_to, win.explicit
    if not explicit:
        with ctx.connection() as conn:
            if _has_rows(conn, "SELECT 1 FROM md.fx_official LIMIT 1", ()):
                return [win]
        d1 = FX_START
    return [_Window(c1, c2, True) for c1, c2 in src.chunk_range(d1, d2, FX_CHUNK_DAYS)]


@job(
    "cbr_daily",
    "CBR key rate, RUONIA (+index, term), standing and refinancing rates, liquidity, official FX",
)
def cbr_daily(
    ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
) -> dict[str, Any]:
    today = ctx.today()
    explicit = date_from is not None or date_to is not None
    d1 = date_from or date_to or today
    d2 = date_to or today
    if d1 > d2:
        raise ValueError(f"date_from {d1} after date_to {d2}")
    win = _Window(d1, d2, explicit)
    summary: dict[str, Any] = {}
    failures: list[str] = []
    with ctx.http() as http:
        for name, part in PARTS:
            # FX ranges: one request per CBR record date -> commit in chunks
            windows = [win]
            if name == "fx_official":
                windows = _fx_windows(ctx, win)
            result = UpsertResult()
            try:
                for w in windows:
                    with ctx.connection() as conn:
                        result += part(ctx, http, conn, w)
            except (MdhubError, psycopg.Error, ValueError) as exc:
                ctx.logger.exception("cbr_daily part %s failed", name)
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                summary[name] = "failed"
                ctx.run.add_counts(result)
                continue
            ctx.run.add_counts(result)
            summary[name] = {"inserted": result.inserted, "updated": result.updated}
            if name == "key_rate" and result.changed:
                with ctx.connection() as conn:
                    resolve_meetings(conn, ctx)
    if failures and len(failures) == len(PARTS):
        raise FormatDriftError("cbr_daily: all parts failed: " + "; ".join(failures))
    if failures:
        ctx.run.mark_partial("; ".join(failures))
    return summary


# -------------------------------------------------------------------------- meetings


def _effective_day_ok(ctx: JobContext, d: date) -> bool:
    """A key-rate decision takes effect on a RU business day that is a weekday (a working
    Saturday of the production calendar does not count)."""
    return d.weekday() < 5 and ctx.is_business_day(d, RU_CALENDAR)


def _next_business_day(ctx: JobContext, d: date) -> date:
    """First RU business weekday after ``d`` (``ref.calendar_day`` RU_PRODUCTION, weekday
    fallback) - never a weekend."""
    cur = d + timedelta(days=1)
    for _ in range(30):
        if _effective_day_ok(ctx, cur):
            return cur
        cur += timedelta(days=1)
    cur = d + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def _key_rate_series(conn: psycopg.Connection[Any], since: date) -> list[tuple[date, Decimal]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rates = cur.execute(
            """SELECT fixing_date, value FROM md.fixing
               WHERE index_code = 'KEY_RATE' AND source = 'cbr' AND fixing_date >= %s
               ORDER BY fixing_date""",
            (since,),
        ).fetchall()
    return [(r["fixing_date"], r["value"]) for r in rates]


def _decide(
    ctx: JobContext, meeting_date: date, series: Sequence[tuple[date, Decimal]]
) -> dict[str, Any] | None:
    """Confirmed effective date / decision of a past meeting, or ``None`` if not yet known.

    effective = the meeting day itself if the rate already changed on it (e.g. 2022-02-28),
    else the first KEY_RATE date on/after the next RU business day (within 10 days)."""
    before = [v for d, v in series if d < meeting_date]
    after = [(d, v) for d, v in series if meeting_date <= d <= meeting_date + timedelta(days=10)]
    if not before or not after:
        return None
    prev = before[-1]
    same_day = [v for d, v in after if d == meeting_date]
    if same_day and same_day[0] != prev:
        eff, rate = meeting_date, same_day[0]
    else:
        nxt = _next_business_day(ctx, meeting_date)
        later = [(d, v) for d, v in after if d >= nxt and _effective_day_ok(ctx, d)]
        if not later:
            return None
        eff, rate = later[0]
    return {
        "effective_date": eff,
        "is_estimated": False,
        "decision_rate_pct": rate,
        "prev_rate_pct": prev,
        "decision": "hike" if rate > prev else "cut" if rate < prev else "hold",
    }


_DECISION_COLS: Final = (
    "effective_date",
    "is_estimated",
    "decision",
    "decision_rate_pct",
    "prev_rate_pct",
)


def resolve_meetings(conn: psycopg.Connection[Any], ctx: JobContext) -> int:
    """Fill effective date / decision of past, unconfirmed meetings from the stored KEY_RATE
    series (see :func:`_decide`). Returns the number of rows changed."""
    with conn.cursor(row_factory=dict_row) as cur:
        meetings = cur.execute(
            "SELECT * FROM ref.cbr_meeting WHERE meeting_date <= %s ORDER BY meeting_date",
            (ctx.today(),),
        ).fetchall()
    if not meetings:
        return 0
    series = _key_rate_series(conn, meetings[0]["meeting_date"] - timedelta(days=60))
    rows = []
    for m in meetings:
        decided = _decide(ctx, m["meeting_date"], series)
        if decided is None and not _effective_day_ok(ctx, m["effective_date"]):
            # e.g. legacy rows dated on a Saturday: the rule is the next RU business day
            decided = {c: m[c] for c in _DECISION_COLS}
            decided["effective_date"] = _next_business_day(ctx, m["meeting_date"])
        if decided is not None and any(m[c] != decided[c] for c in _DECISION_COLS):
            rows.append({"meeting_date": m["meeting_date"], **decided})
    if not rows:
        return 0
    return upsert_rows(conn, "ref.cbr_meeting", rows, ["meeting_date"]).changed


def _load_meetings(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any]
) -> UpsertResult:
    resp = src.fetch_meetings_page(http)
    cap = record_capture(conn, ctx.run.id, SOURCE, "meetings", None, resp, src.PARSER_VERSION)
    meetings = src.parse_meetings_page(resp.text)
    today = ctx.today()
    series = _key_rate_series(conn, meetings[0].meeting_date - timedelta(days=60))
    with conn.cursor(row_factory=dict_row) as cur:
        confirmed = {
            r["meeting_date"]: {c: r[c] for c in _DECISION_COLS}
            for r in cur.execute("SELECT * FROM ref.cbr_meeting WHERE NOT is_estimated")
        }
    rows = []
    for m in meetings:
        decided = _decide(ctx, m.meeting_date, series) if m.meeting_date <= today else None
        if decided is None:
            decided = confirmed.get(m.meeting_date)
        if decided is None:
            decided = {
                "effective_date": _next_business_day(ctx, m.meeting_date),
                "is_estimated": True,
                "decision": None,
                "decision_rate_pct": None,
                "prev_rate_pct": None,
            }
        rows.append(
            {
                "meeting_date": m.meeting_date,
                **decided,
                "is_extraordinary": m.is_extraordinary,
                "title": m.title,
                "source": SOURCE,
                "capture_id": cap,
                "run_id": ctx.run.id,
            }
        )
    return upsert_rows(conn, "ref.cbr_meeting", rows, ["meeting_date"], run_id=ctx.run.id)


@job("cbr_meetings", "CBR board meeting calendar (+ business-day calendars) / post-meeting check")
def cbr_meetings(ctx: JobContext, *, mode: str = "calendar") -> dict[str, Any]:
    if mode not in ("calendar", "calendar_full", "post_meeting"):
        raise ValueError(f"unknown mode {mode!r} (calendar, calendar_full, post_meeting)")
    if mode == "post_meeting":
        return _post_meeting(ctx)
    from mdhub.loaders.calendar import refresh_calendar  # avoid import cycle at module load

    summary: dict[str, Any] = {}
    # Calendars first: meeting effective dates are "next RU business day" by the calendar.
    summary["calendar"] = refresh_calendar(ctx, full=(mode == "calendar_full"))
    with ctx.http() as http:
        with ctx.connection() as conn:
            res = _load_meetings(ctx, http, conn)
        ctx.run.add_counts(res)
        summary["meetings"] = {"inserted": res.inserted, "updated": res.updated}
    with ctx.connection() as conn:
        summary["meetings"]["resolved"] = resolve_meetings(conn, ctx)
    return summary


def _post_meeting(ctx: JobContext) -> dict[str, Any]:
    today = ctx.today()
    with ctx.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        pending = cur.execute(
            """SELECT meeting_date FROM ref.cbr_meeting
               WHERE meeting_date BETWEEN %s AND %s AND is_estimated""",
            (today - timedelta(days=POST_MEETING_DAYS), today),
        ).fetchall()
    if not pending:
        ctx.run.mark_skipped("no unconfirmed board meeting in the last days")
        return {"meetings": []}
    with ctx.http() as http, ctx.connection() as conn:
        win = _Window(today - timedelta(days=WINDOW_FIXINGS_DAYS), today, True)
        res = _load_key_rate(ctx, http, conn, win)
        ctx.run.add_counts(res)
        changed = resolve_meetings(conn, ctx)
    return {"meetings": [str(r["meeting_date"]) for r in pending], "resolved": changed}
