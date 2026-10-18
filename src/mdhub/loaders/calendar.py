"""Business-day calendars -> ``ref.calendar_day`` (read by :class:`mdhub.core.bizday.DbCalendar`).

Calendars and sources (``DbCalendar`` / ``ref.calendar_effective`` take, per day, the row of
the highest-priority source - :data:`SOURCE_PRIORITY`: observed > MOEX ISS calendar > curated >
rule > curated_unverified; ``known_at`` only breaks ties):

=============== ============================ ===============================================
calendar        past days (observed)          future days
=============== ============================ ===============================================
MOEX_STOCK      ``observed_moex_stock``: IMOEX ``moex_iss_calendar`` (only the days MOEX ISS
                or RGBI has a history row     ``/engines/{engine}/calendar.json`` lists), then
MOEX_FX         ``observed_moex_fx``: CETS    ``curated`` / ``curated_unverified`` from the
                CNYRUB_TOM history row        versioned file ``mdhub/data/calendar_curated.json``
MOEX_FORTS      ``observed_moex_forts``: Si
                futures volume > 0
RU_PRODUCTION   ``observed_cbr_ruonia``: a    ``curated`` (Government decrees on transfers)
                RUONIA fixing exists (md.fixing)
TARGET          (none - rule only)            ``rule_target`` for past *and* future days:
                                              :func:`target_closing_days` (Jan 1, Good Friday,
                                              Easter Monday, May 1, Dec 25, Dec 26) + weekends,
                                              from :data:`TARGET_RULE_START` (2002) through the
                                              end of next year
=============== ============================ ===============================================

Observed rows cover every calendar day from the start date (:data:`OBSERVED_START`, 2019) to the
last observed day (days without trading / a fixing -> ``false``). Curated and ISS-calendar rows
are written only for days after the last observed day of the calendar. TARGET has no observed
source: the rule is exact (ECB rate dates 2015-2026 match it day for day), and deriving the
calendar from ``md.ecb_fx`` would turn a failed ECB load into a holiday. ECB data only
cross-checks the rule (:func:`target_crosscheck`): a rule business day without rates (past the
no-data grace period, inside the ECB data range) or rates on a rule closing day -> warning and
run ``partial``, the calendar is not changed. Known limitation of the observed calendars
(RU_PRODUCTION from RUONIA, MOEX from trading history): a day whose source data failed to load
before a refresh becomes a non-business day. A row is (re)written
only when ``is_business_day`` changes (``known_at`` = when that became known; the old image
goes to ``ops.row_history``). Repair on every refresh: ISS-calendar rows that the current ISS
answer no longer lists are deleted (image kept in ``ops.row_history``), and an observed
history that starts after :data:`OBSERVED_START` is extended backwards. The deletion is skipped
(warning, run ``partial``) when the ISS answer of that engine looks incomplete - fewer than
:data:`ISS_CALENDAR_MIN_ROWS` rows of the engine or none in the current year - so a transient
empty answer never wipes the stored ISS rows.

Weekend sessions (``ref.weekend_session``): ISS rows with ``is_work_day=1, reason='W'`` (the
engine trades on a weekend/holiday; trades belong to ``trade_session_date``) stay
non-business in ``ref.calendar_day`` and are stored separately for every listed day (also
past days of the year: the observed calendars cannot see them). They make the day a *trading
day* (:meth:`mdhub.core.bizday.CalendarPredicate.is_trading_day`). Future session rows that ISS
no longer lists are deleted under the same sanity rule.

Entry points: :func:`refresh_calendar` (called by ``cbr_meetings`` in ``calendar`` /
``calendar_full`` mode) and :func:`seed_calendar` (initial seeding without a scheduled job).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib import resources
from typing import Any, Final

import psycopg
from psycopg.rows import tuple_row

from mdhub import credentials
from mdhub.config import Settings
from mdhub.core.bizday import CALENDARS, weekday_calendar
from mdhub.core.capture import job_run, record_capture
from mdhub.core.errors import FormatDriftError, MdhubError
from mdhub.core.gaps import no_data_status
from mdhub.core.http import HttpClient
from mdhub.core.iss import ISS_BASE_URL, iter_pages, parse_block, to_date, to_decimal
from mdhub.core.timeutil import date_range, utc_now
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.db.pool import Database
from mdhub.jobs import JobContext

__all__ = [
    "CURATED_RESOURCE",
    "OBSERVED_START",
    "SOURCE_PRIORITY",
    "TARGET_RULE_SOURCE",
    "TARGET_RULE_START",
    "CuratedCalendar",
    "CuratedYear",
    "easter_sunday",
    "iss_calendar_days",
    "iss_weekend_sessions",
    "load_curated",
    "refresh_calendar",
    "seed_calendar",
    "target_closing_days",
    "target_crosscheck",
    "target_rule_days",
    "write_calendar_days",
]

log = logging.getLogger(__name__)

CURATED_RESOURCE: Final = "calendar_curated.json"
OBSERVED_START: Final = date(2019, 1, 1)
"""Observed calendars start here (CBR meeting history starts 2019: effective dates need the
RU business-day calendar)."""
ISS_SOURCE: Final = "moex_iss_calendar"
SOURCE_PRIORITY: Final = {
    "observed": 1,
    ISS_SOURCE: 2,
    "curated": 3,
    "rule": 4,
    "curated_unverified": 5,
}
"""Source precedence per (calendar, day), lower wins (``observed`` = any ``observed_*``,
``rule`` = any ``rule_*``): observed trading/fixings/rates > days the official MOEX ISS
calendar lists > curated file > computed holiday rule (e.g. ``rule_target``) > unverified
curated file > anything else; the weekday rule applies only when no row exists. Recency
(``known_at``) only breaks ties within a rank. Mirrored by ``ref.calendar_source_rank``
(migrations 0420, 0422) used by ``ref.calendar_effective`` and
:class:`mdhub.core.bizday.DbCalendar`."""
TARGET_CALENDAR: Final = "TARGET"
TARGET_RULE_SOURCE: Final = "rule_target"
"""The only TARGET source: :func:`target_rule_days` for every day (past and future)."""
TARGET_RULE_START: Final = date(2002, 1, 1)
"""The six TARGET closing days apply since 2002 (TARGET calendar default start)."""
TARGET_RULE_VERSION: Final = "target-closing-days-1"
CROSSCHECK_MAX_LISTED: Final = 10
"""Mismatching days listed in the cross-check warning (the count is always given)."""
INCREMENTAL_OVERLAP_DAYS: Final = 14
PARSER_VERSION: Final = "1"
KEY: Final = ["calendar", "day", "source"]
ISS_ENGINES: Final = {"MOEX_STOCK": "stock", "MOEX_FX": "currency", "MOEX_FORTS": "futures"}
ISS_CALENDAR_MIN_ROWS: Final = 5
"""An ISS engine calendar answer with fewer rows of its own engine (or none in the current
year) is treated as incomplete: listed days are written, nothing is deleted."""
SESSION_KEY: Final = ["calendar", "day", "source"]

# ------------------------------------------------------------------------ curated file


@dataclass(frozen=True, slots=True)
class CuratedYear:
    calendar: str
    year: int
    source: str
    reference: str
    non_working: frozenset[date]
    working_weekends: frozenset[date]

    def days(self) -> dict[date, bool]:
        """Every day of the year -> is business day."""
        out = {}
        for d in date_range(date(self.year, 1, 1), date(self.year, 12, 31)):
            out[d] = (d.weekday() < 5 and d not in self.non_working) or d in self.working_weekends
        return out


@dataclass(frozen=True, slots=True)
class CuratedCalendar:
    version: str
    known_at: datetime
    years: tuple[CuratedYear, ...]


def load_curated(text: str | None = None) -> CuratedCalendar:
    """Parse and validate the curated calendar file (package data by default)."""
    if text is None:
        text = resources.files("mdhub.data").joinpath(CURATED_RESOURCE).read_text("utf-8")
    try:
        data = json.loads(text)
        version = str(data["version"])
        known_at = datetime.fromisoformat(data["known_at"])
        calendars = data["calendars"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FormatDriftError(f"curated calendar: bad file: {exc}") from None
    if data.get("format") != 1 or known_at.tzinfo is None:
        raise FormatDriftError("curated calendar: unsupported format or naive known_at")
    years: list[CuratedYear] = []
    for cal, entries in calendars.items():
        if cal not in CALENDARS:
            raise FormatDriftError(f"curated calendar: unknown calendar {cal!r}")
        seen: set[int] = set()
        for e in entries:
            year = int(e["year"])
            if year in seen:
                raise FormatDriftError(f"curated calendar: {cal} {year} listed twice")
            seen.add(year)
            if e["source"] not in ("curated", "curated_unverified"):
                raise FormatDriftError(f"curated calendar: bad source {e['source']!r}")
            nw = frozenset(date.fromisoformat(x) for x in e["non_working"])
            ww = frozenset(date.fromisoformat(x) for x in e["working_weekends"])
            for d in nw | ww:
                if d.year != year:
                    raise FormatDriftError(f"curated calendar: {cal} {year}: {d} outside year")
            if any(d.weekday() >= 5 for d in nw):
                raise FormatDriftError(f"curated calendar: {cal} {year}: weekend in non_working")
            if any(d.weekday() < 5 for d in ww):
                raise FormatDriftError(
                    f"curated calendar: {cal} {year}: weekday in working_weekends"
                )
            years.append(CuratedYear(cal, year, e["source"], str(e["reference"]), nw, ww))
    return CuratedCalendar(version=version, known_at=known_at, years=tuple(years))


# ------------------------------------------------------------------------ TARGET rule


def easter_sunday(year: int) -> date:
    """Western (Gregorian) Easter Sunday - anonymous Gregorian computus (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def target_closing_days(year: int) -> frozenset[date]:
    """TARGET (T2) closing days of ``year`` besides weekends: New Year's Day, Good Friday,
    Easter Monday, Labour Day (May 1), Christmas Day and Boxing Day (Dec 26) - the ECB
    publishes no euro reference rates on these days."""
    easter = easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter - timedelta(days=2),
            easter + timedelta(days=1),
            date(year, 5, 1),
            date(year, 12, 25),
            date(year, 12, 26),
        }
    )


def target_rule_days(start: date, end: date) -> dict[date, bool]:
    """Every day of ``[start, end]`` -> TARGET business day (weekday, not a closing day)."""
    closed: dict[int, frozenset[date]] = {}
    out: dict[date, bool] = {}
    for d in date_range(start, end):
        if d.year not in closed:
            closed[d.year] = target_closing_days(d.year)
        out[d] = d.weekday() < 5 and d not in closed[d.year]
    return out


def target_crosscheck(
    rate_dates: Iterable[date], is_business_day: Callable[[date], bool], today: date
) -> tuple[list[date], list[date]]:
    """Compare ECB rate dates with the TARGET calendar ``is_business_day``.

    Returns ``(missing, unexpected)``: business days inside ``[first, last]`` rate date without
    rates whose no-data grace period is over (:func:`mdhub.core.gaps.no_data_status`: at least
    one TARGET business day lies between the day and ``today``), and rate dates that are not
    business days. Both empty when the rule and the ECB data agree."""
    dates = set(rate_dates)
    if not dates:
        return [], []
    missing = [
        d
        for d in date_range(min(dates), max(dates))
        if d not in dates
        and is_business_day(d)
        and no_data_status(d, today, is_business_day) == "unfillable"
    ]
    unexpected = sorted(d for d in dates if not is_business_day(d))
    return missing, unexpected


# --------------------------------------------------------------------------- writing


def write_calendar_days(
    conn: psycopg.Connection[Any],
    calendar: str,
    source: str,
    days: Mapping[date, bool],
    *,
    known_at: datetime,
    run_id: int | None,
    version: str | None = None,
    note: str | None = None,
    capture_id: int | None = None,
) -> UpsertResult:
    """Write ``days`` for ``(calendar, source)``; only new days or changed flags are written."""
    if not days:
        return UpsertResult()
    if calendar not in CALENDARS:
        raise ValueError(f"unknown calendar {calendar!r}")
    with conn.cursor(row_factory=tuple_row) as cur:
        existing: dict[date, bool] = dict(
            cur.execute(
                """SELECT day, is_business_day FROM ref.calendar_day
                   WHERE calendar = %s AND source = %s AND day BETWEEN %s AND %s""",
                (calendar, source, min(days), max(days)),
            ).fetchall()
        )
    rows = [
        {
            "calendar": calendar,
            "day": d,
            "source": source,
            "is_business_day": flag,
            "known_at": known_at,
            "version": version,
            "note": note,
            "capture_id": capture_id,
            "run_id": run_id,
        }
        for d, flag in sorted(days.items())
        if existing.get(d) != flag
    ]
    result = upsert_rows(conn, "ref.calendar_day", rows, KEY, run_id=run_id)
    return result + UpsertResult(unchanged=len(days) - len(rows))


def _drop_unlisted(
    conn: psycopg.Connection[Any],
    calendar: str,
    source: str,
    keep: set[date],
    *,
    after: date | None,
    run_id: int | None,
) -> UpsertResult:
    """Delete ``(calendar, source)`` rows after ``after`` whose day is not in ``keep``; the
    deleted row images go to ``ops.row_history`` first (the history trigger covers updates
    only). Counted as updates."""
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            """
            WITH gone AS (
                DELETE FROM ref.calendar_day c
                WHERE c.calendar = %(cal)s AND c.source = %(src)s
                  AND (%(after)s::date IS NULL OR c.day > %(after)s::date)
                  AND NOT (c.day = ANY(%(keep)s::date[]))
                RETURNING c.*
            )
            INSERT INTO ops.row_history (table_name, row_key, old_row, replaced_by_run)
            SELECT 'ref.calendar_day',
                   jsonb_build_object('calendar', g.calendar, 'day', g.day, 'source', g.source),
                   to_jsonb(g) || jsonb_build_object('_deleted', true), %(run)s
            FROM gone g
            """,
            {"cal": calendar, "src": source, "after": after, "keep": sorted(keep), "run": run_id},
        )
        n = cur.rowcount
    if n:
        log.warning("calendar %s: removed %d %s row(s) no longer listed", calendar, n, source)
    return UpsertResult(updated=max(n, 0))


def _last_observed(conn: psycopg.Connection[Any], calendar: str) -> date | None:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """SELECT max(day) FROM ref.calendar_day
               WHERE calendar = %s AND source LIKE 'observed\\_%%'""",
            (calendar,),
        ).fetchone()
    return row[0] if row else None


def _observed_start(
    conn: psycopg.Connection[Any], calendar: str, source: str, *, full: bool, since: date
) -> date:
    """``since`` for a full refresh, when nothing is stored yet, or when the stored history
    starts after ``since`` (it is extended backwards, e.g. to 2019 for the CBR meetings);
    else the last observed day minus :data:`INCREMENTAL_OVERLAP_DAYS`."""
    if full:
        return since
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT min(day), max(day) FROM ref.calendar_day WHERE calendar = %s AND source = %s",
            (calendar, source),
        ).fetchone()
    first: date | None = row[0] if row else None
    last: date | None = row[1] if row else None
    if last is None or first is None or first > since:
        return since
    return max(since, last - timedelta(days=INCREMENTAL_OVERLAP_DAYS))


def observed_days(traded: Iterable[date], start: date) -> dict[date, bool]:
    """``start .. max(traded)`` -> traded or not (empty if nothing traded)."""
    traded_set = {d for d in traded if d >= start}
    if not traded_set:
        return {}
    return {d: d in traded_set for d in date_range(start, max(traded_set))}


# ------------------------------------------------------------------------- MOEX ISS


def _iss_dates(
    ctx: JobContext,
    http: HttpClient,
    conn: psycopg.Connection[Any],
    path: str,
    start: date,
    end: date,
    *,
    volume_column: str | None = None,
) -> set[date]:
    """TRADEDATEs of an ISS history endpoint (rows with volume > 0 if ``volume_column``)."""
    columns = "TRADEDATE" + (f",{volume_column}" if volume_column else "")
    out: set[date] = set()
    for page in iter_pages(
        http,
        ISS_BASE_URL + path,
        {"from": start.isoformat(), "till": end.isoformat(), "history.columns": columns},
        required=columns.split(","),
    ):
        record_capture(
            conn, ctx.run.id, "moex_iss", "calendar_history", None, page.response, PARSER_VERSION
        )
        for r in page.rows:
            d = to_date(r["TRADEDATE"], field="TRADEDATE")
            if d is None:
                raise FormatDriftError(f"ISS {path}: empty TRADEDATE")
            if volume_column is not None:
                vol = to_decimal(r[volume_column], field=volume_column)
                if not vol:
                    continue
            out.add(d)
    return out


def _si_contracts(start: date, end: date) -> list[tuple[str, date, date]]:
    """Quarterly Si futures (code, from, till) whose life overlaps ``[start, end]``."""
    out = []
    for year in range(start.year, end.year + 2):
        for letter, month in (("H", 3), ("M", 6), ("U", 9), ("Z", 12)):
            expiry = date(year, month, 28)
            first = expiry - timedelta(days=400)
            if expiry < start or first > end:
                continue
            out.append((f"Si{letter}{year % 10}", max(start, first), min(end, expiry)))
    return out


def _observed_moex(
    ctx: JobContext,
    http: HttpClient,
    conn: psycopg.Connection[Any],
    calendar: str,
    start: date,
    end: date,
) -> set[date]:
    if calendar == "MOEX_STOCK":
        dates: set[date] = set()
        for secid in ("IMOEX", "RGBI"):
            dates |= _iss_dates(
                ctx,
                http,
                conn,
                f"/history/engines/stock/markets/index/securities/{secid}.json",
                start,
                end,
            )
        return dates
    if calendar == "MOEX_FX":
        return _iss_dates(
            ctx,
            http,
            conn,
            "/history/engines/currency/markets/selt/boards/CETS/securities/CNYRUB_TOM.json",
            start,
            end,
        )
    if calendar == "MOEX_FORTS":
        dates = set()
        for code, c1, c2 in _si_contracts(start, end):
            dates |= _iss_dates(
                ctx,
                http,
                conn,
                f"/history/engines/futures/markets/forts/securities/{code}.json",
                c1,
                c2,
                volume_column="VOLUME",
            )
        return dates
    raise ValueError(calendar)


def iss_calendar_days(rows: Iterable[Mapping[str, Any]], own: str) -> dict[date, bool]:
    """Business-day flags for exactly the days the ISS ``calendar`` rows *list* (``own``
    engine, plus holidays of the ``currency`` engine) - never a synthesized year.

    * a day listed as a holiday (``is_work_day=0``) by ``own`` or ``currency`` -> ``False``,
    * a weekend session (``reason='W'``, trading attributed to another session date) ->
      ``False``,
    * a weekend listed as a working day of ``own`` whose session is that day -> ``True``,
    * a weekday listed as a working day of ``own`` -> ``True``.

    Days the rows do not list are left to lower-priority sources (curated file, weekday
    fallback): ISS often lists only part of a year (e.g. in December a few January days).
    """
    holidays: set[date] = set()
    flags: dict[date, bool] = {}
    for r in rows:
        d = to_date(r["date"], field="calendar date")
        if d is None:
            raise FormatDriftError("ISS calendar: empty date")
        engine = str(r["engine"])
        if engine not in (own, "currency"):
            continue
        work = r["is_work_day"]
        if work not in (0, 1):
            raise FormatDriftError(f"ISS calendar: bad is_work_day {work!r}")
        if work == 0:
            holidays.add(d)
            continue
        if engine != own:
            continue
        if r["reason"] == "W":
            flags.setdefault(d, False)
        elif d.weekday() >= 5:
            session = to_date(r["trade_session_date"]) if r["trade_session_date"] else None
            flags[d] = session in (None, d)
        else:
            flags[d] = True
    for d in holidays:
        flags[d] = False
    return dict(sorted(flags.items()))


def iss_weekend_sessions(rows: Iterable[Mapping[str, Any]], own: str) -> dict[date, date | None]:
    """Weekend/holiday sessions of engine ``own``: listed day -> ``trade_session_date``
    (``is_work_day=1``, ``reason='W'``)."""
    out: dict[date, date | None] = {}
    for r in rows:
        if str(r["engine"]) != own or r["is_work_day"] != 1 or r["reason"] != "W":
            continue
        d = to_date(r["date"], field="calendar date")
        if d is None:
            raise FormatDriftError("ISS calendar: empty date")
        out[d] = to_date(r["trade_session_date"]) if r["trade_session_date"] else None
    return dict(sorted(out.items()))


def iss_answer_complete(rows: Iterable[Mapping[str, Any]], own: str, today: date) -> bool:
    """Sanity check before deleting rows the answer does not list: at least
    :data:`ISS_CALENDAR_MIN_ROWS` rows of engine ``own``, one of them in ``today``'s year."""
    own_days = [
        d
        for r in rows
        if str(r["engine"]) == own and (d := to_date(r["date"], field="calendar date")) is not None
    ]
    return len(own_days) >= ISS_CALENDAR_MIN_ROWS and any(d.year == today.year for d in own_days)


def write_weekend_sessions(
    conn: psycopg.Connection[Any],
    calendar: str,
    sessions: Mapping[date, date | None],
    *,
    known_at: datetime,
    run_id: int | None,
    capture_id: int | None,
    drop_after: date | None,
    source: str = ISS_SOURCE,
) -> UpsertResult:
    """Write ``ref.weekend_session`` rows of ``(calendar, source)`` (new or changed only); with
    ``drop_after``, delete rows after that day the answer no longer lists (image to
    ``ops.row_history``)."""
    total = UpsertResult()
    if drop_after is not None:
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(
                """
                WITH gone AS (
                    DELETE FROM ref.weekend_session s
                    WHERE s.calendar = %(cal)s AND s.source = %(src)s AND s.day > %(after)s
                      AND NOT (s.day = ANY(%(keep)s::date[]))
                    RETURNING s.*
                )
                INSERT INTO ops.row_history (table_name, row_key, old_row, replaced_by_run)
                SELECT 'ref.weekend_session',
                       jsonb_build_object('calendar', g.calendar, 'day', g.day,
                                          'source', g.source),
                       to_jsonb(g) || jsonb_build_object('_deleted', true), %(run)s
                FROM gone g
                """,
                {
                    "cal": calendar,
                    "src": source,
                    "after": drop_after,
                    "keep": sorted(sessions),
                    "run": run_id,
                },
            )
            if cur.rowcount > 0:
                log.warning(
                    "calendar %s: removed %d weekend session(s) no longer listed",
                    calendar,
                    cur.rowcount,
                )
                total += UpsertResult(updated=cur.rowcount)
    if not sessions:
        return total
    with conn.cursor(row_factory=tuple_row) as cur:
        existing: dict[date, date | None] = dict(
            cur.execute(
                """SELECT day, trade_session_date FROM ref.weekend_session
                   WHERE calendar = %s AND source = %s AND day BETWEEN %s AND %s""",
                (calendar, source, min(sessions), max(sessions)),
            ).fetchall()
        )
    rows = [
        {
            "calendar": calendar,
            "day": d,
            "source": source,
            "trade_session_date": session,
            "known_at": known_at,
            "note": "MOEX ISS calendar reason W (weekend/holiday session)",
            "capture_id": capture_id,
            "run_id": run_id,
        }
        for d, session in sessions.items()
        if d not in existing or existing[d] != session
    ]
    total += upsert_rows(conn, "ref.weekend_session", rows, SESSION_KEY, run_id=run_id)
    return total + UpsertResult(unchanged=len(sessions) - len(rows))


def _fetch_iss_calendars(
    ctx: JobContext, http: HttpClient, conn: psycopg.Connection[Any]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    caps: dict[str, int] = {}
    for engine in ("stock", "currency", "futures"):
        res = http.get_json(
            f"{ISS_BASE_URL}/engines/{engine}/calendar.json", params={"iss.meta": "off"}
        )
        caps[engine] = record_capture(
            conn, ctx.run.id, "moex_iss", "engine_calendar", None, res.response, PARSER_VERSION
        )
        rows.extend(
            parse_block(
                res.data,
                "calendar",
                required=["engine", "date", "is_work_day", "trade_session_date", "reason"],
            )
        )
    return rows, caps


# --------------------------------------------------------------------------- refresh


def refresh_calendar(
    ctx: JobContext, *, full: bool = False, since: date | None = None
) -> dict[str, Any]:
    """Refresh ``ref.calendar_day`` (observed history, MOEX ISS calendar, curated file,
    TARGET rule) and cross-check TARGET against the ECB rate dates.

    ``full`` re-reads observed history from ``since``; otherwise from the last observed day
    minus :data:`INCREMENTAL_OVERLAP_DAYS`. ``since`` defaults to :data:`OBSERVED_START`
    (TARGET: :data:`TARGET_RULE_START`; an explicit ``since`` earlier than that is raised to
    it). Each step commits separately; a failing step (or a TARGET cross-check mismatch) marks
    the run ``partial``. Returns per-step counts.
    """
    today = ctx.today()
    base = since if since is not None else OBSERVED_START
    summary: dict[str, Any] = {}
    failures: list[str] = []

    def step(name: str, fn: Callable[[psycopg.Connection[Any]], UpsertResult]) -> None:
        try:
            with ctx.connection() as conn:
                res = fn(conn)
        except (MdhubError, psycopg.Error, ValueError) as exc:
            ctx.logger.exception("calendar step %s failed", name)
            failures.append(f"calendar {name}: {type(exc).__name__}: {exc}")
            summary[name] = "failed"
            return
        ctx.run.add_counts(res)
        summary[name] = {"written": res.changed, "unchanged": res.unchanged}

    with ctx.http() as http:
        for cal in ("MOEX_STOCK", "MOEX_FX", "MOEX_FORTS"):
            source = f"observed_{cal.lower()}"

            def observed(
                conn: psycopg.Connection[Any], cal: str = cal, source: str = source
            ) -> UpsertResult:
                start = _observed_start(conn, cal, source, full=full, since=base)
                traded = _observed_moex(ctx, http, conn, cal, start, today)
                return write_calendar_days(
                    conn,
                    cal,
                    source,
                    observed_days(traded, start),
                    known_at=utc_now(),
                    run_id=ctx.run.id,
                    note="observed MOEX trading days",
                )

            step(source, observed)

        def ruonia(conn: psycopg.Connection[Any]) -> UpsertResult:
            source = "observed_cbr_ruonia"
            start = _observed_start(conn, "RU_PRODUCTION", source, full=full, since=base)
            with conn.cursor(row_factory=tuple_row) as cur:
                dates = [
                    r[0]
                    for r in cur.execute(
                        """SELECT fixing_date FROM md.fixing
                           WHERE index_code = 'RUONIA' AND source = 'cbr' AND fixing_date >= %s""",
                        (start,),
                    ).fetchall()
                ]
            return write_calendar_days(
                conn,
                "RU_PRODUCTION",
                source,
                observed_days(dates, start),
                known_at=utc_now(),
                run_id=ctx.run.id,
                note="CBR RUONIA observation days (md.fixing)",
            )

        step("observed_cbr_ruonia", ruonia)

        def iss_calendar(conn: psycopg.Connection[Any]) -> UpsertResult:
            rows, caps = _fetch_iss_calendars(ctx, http, conn)
            total = UpsertResult()
            known = utc_now()
            for cal, engine in ISS_ENGINES.items():
                last = _last_observed(conn, cal)
                days = {
                    d: v
                    for d, v in iss_calendar_days(rows, engine).items()
                    if last is None or d > last
                }
                complete = iss_answer_complete(rows, engine, today)
                if complete:
                    # Repair: rows of earlier versions that ISS no longer lists (synthesized
                    # whole years) are removed - they would outrank the curated file.
                    total += _drop_unlisted(
                        conn, cal, ISS_SOURCE, set(days), after=last, run_id=ctx.run.id
                    )
                else:
                    msg = (
                        f"calendar {cal}: ISS engines/{engine}/calendar.json answer looks "
                        f"incomplete (< {ISS_CALENDAR_MIN_ROWS} rows of the engine or none in "
                        f"{today.year}); stored ISS rows kept"
                    )
                    ctx.logger.warning(msg)
                    failures.append(msg)
                total += write_weekend_sessions(
                    conn,
                    cal,
                    iss_weekend_sessions(rows, engine),
                    known_at=known,
                    run_id=ctx.run.id,
                    capture_id=caps.get(engine),
                    drop_after=today if complete else None,
                )
                total += write_calendar_days(
                    conn,
                    cal,
                    ISS_SOURCE,
                    days,
                    known_at=known,
                    run_id=ctx.run.id,
                    capture_id=caps.get(engine),
                    note=f"MOEX ISS engines/{engine}+currency calendar.json",
                )
            return total

        step("moex_iss_calendar", iss_calendar)

    def curated(conn: psycopg.Connection[Any]) -> UpsertResult:
        cc = load_curated()
        total = UpsertResult()
        for cy in cc.years:
            last = _last_observed(conn, cy.calendar)
            days = {d: v for d, v in cy.days().items() if last is None or d > last}
            total += write_calendar_days(
                conn,
                cy.calendar,
                cy.source,
                days,
                known_at=cc.known_at,
                run_id=ctx.run.id,
                version=cc.version,
                note=cy.reference[:500],
            )
        return total

    step("curated", curated)

    def target_rule(conn: psycopg.Connection[Any]) -> UpsertResult:
        start = TARGET_RULE_START if since is None else max(since, TARGET_RULE_START)
        return write_calendar_days(
            conn,
            TARGET_CALENDAR,
            TARGET_RULE_SOURCE,
            target_rule_days(start, date(today.year + 1, 12, 31)),
            known_at=utc_now(),
            run_id=ctx.run.id,
            version=TARGET_RULE_VERSION,
            note="TARGET closing days rule: Jan 1, Good Friday, Easter Monday, May 1, Dec 25, "
            "Dec 26 (+ weekends)",
        )

    step(TARGET_RULE_SOURCE, target_rule)

    def target_check(conn: psycopg.Connection[Any]) -> None:
        """ECB rate dates vs the effective TARGET calendar (rule, or a curated override; rule
        for days without a row). Reports only - never changes the calendar."""
        with conn.cursor(row_factory=tuple_row) as cur:
            dates = [
                r[0] for r in cur.execute("SELECT DISTINCT rate_date FROM md.ecb_fx").fetchall()
            ]
            if not dates:
                summary["target_crosscheck"] = {"checked_days": 0}
                return
            lo, hi = min(dates), today
            flags = target_rule_days(lo, hi)
            flags.update(
                cur.execute(
                    """SELECT day, is_business_day FROM ref.calendar_effective
                       WHERE calendar = %s AND day BETWEEN %s AND %s""",
                    (TARGET_CALENDAR, lo, hi),
                ).fetchall()
            )
        missing, unexpected = target_crosscheck(
            dates, lambda d: flags.get(d, d.weekday() < 5), today
        )
        result = {
            "checked_days": len(dates),
            "missing": len(missing),
            "unexpected": len(unexpected),
        }
        if missing or unexpected:
            msg = (
                f"calendar TARGET cross-check: {len(missing)} TARGET business day(s) without "
                f"ECB rates {_short(missing)}, {len(unexpected)} ECB rate date(s) on TARGET "
                f"closing days {_short(unexpected)}; calendar unchanged"
            )
            ctx.logger.warning(msg)
            failures.append(msg)
        summary["target_crosscheck"] = result

    try:
        with ctx.connection() as conn:
            target_check(conn)
    except (psycopg.Error, ValueError) as exc:
        ctx.logger.exception("calendar TARGET cross-check failed")
        failures.append(f"calendar target_crosscheck: {type(exc).__name__}: {exc}")
        summary["target_crosscheck"] = "failed"
    if failures:
        ctx.run.mark_partial("; ".join(failures))
    return summary


def _short(days: list[date]) -> str:
    listed = ", ".join(d.isoformat() for d in days[:CROSSCHECK_MAX_LISTED])
    more = ", ..." if len(days) > CROSSCHECK_MAX_LISTED else ""
    return f"[{listed}{more}]"


def seed_calendar(
    settings: Settings,
    db: Database,
    *,
    since: date | None = None,
    http_factory: Callable[..., HttpClient] = HttpClient,
) -> dict[str, Any]:
    """Initial full seeding outside the scheduler (one ``ops.job_run`` named
    ``calendar_seed``). ``since`` defaults to :data:`OBSERVED_START` (TARGET:
    :data:`TARGET_RULE_START`). Load RUONIA history first (``cbr_daily``) for RU_PRODUCTION
    (and ECB history, ``ecb_fx``, for the TARGET cross-check)."""
    settings = credentials.resolve(settings)  # not run_job: resolve as it would
    with job_run(db, "calendar_seed", {"since": since}, settings=settings, trigger="cli") as run:
        ctx = JobContext(
            settings=settings,
            db=db,
            run=run,
            job="calendar_seed",
            params={"since": since},
            logger=logging.getLogger("mdhub.job.calendar_seed"),
            calendar=weekday_calendar,
            http_factory=http_factory,
        )
        return refresh_calendar(ctx, full=True, since=since)
