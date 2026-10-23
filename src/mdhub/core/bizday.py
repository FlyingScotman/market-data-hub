"""Business-day predicates backed by ``ref.calendar_day`` with a weekday fallback.

Source precedence (not recency) decides between rows of one day: see
:data:`mdhub.loaders.calendar.SOURCE_PRIORITY`.

``ref.calendar_day(calendar, day, is_business_day, source, known_at)`` (DESIGN §4) is owned by
the calendar loader. Until that table exists - or for days it does not cover - the
Monday-Friday fallback applies.

Two notions per calendar (DESIGN §4 ``ref.weekend_session``):

* **business day** (``predicate(day, calendar)``) - the settlement calendar: trade dates of
  EOD history, ``days_back`` offsets, gap expectations;
* **trading day** (:meth:`CalendarPredicate.is_trading_day`) - a business day *or* a day on
  which the engine holds a weekend/holiday session (MOEX ISS calendar ``reason='W'``, stored in
  ``ref.weekend_session``; trades belong to the next business day's session). Used to date
  quotes ("the previous session") and to gate polling of markets that trade on such days.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Final

import psycopg
from psycopg.rows import tuple_row

from mdhub.core.timeutil import is_weekday
from mdhub.db.pool import Database

__all__ = ["CALENDARS", "DEFAULT_CALENDAR", "CalendarPredicate", "DbCalendar", "weekday_calendar"]

log = logging.getLogger(__name__)

CALENDARS: Final = (
    "MOEX_STOCK",
    "MOEX_FX",
    "MOEX_FORTS",
    "RU_PRODUCTION",
    "TARGET",
    "CN_INTERBANK",
    "US_FED",
    "CH_ZURICH",
    "MOEX_SPFI",
    "CBR_RUONIA",
    "MOEX_RUSFAR",
)
"""Known business-day calendars (``ref.calendar_day.calendar`` CHECK, migrations 0422, 0428,
0429): the MOEX engines, the Russian production calendar, TARGET (euro area; ECB reference-rate
days), the foreign settlement calendars of FX value dates (contract 1.5): ``CN_INTERBANK`` (China
interbank market: State Council public holidays and adjusted working weekends), ``US_FED``
(Federal Reserve Banks holidays: USD settlement), ``CH_ZURICH`` (Zurich bank holidays: CHF
settlement, SARON days), and the calendars of RUB swap and fixing conventions (contract 1.6, TDM
brief H17): ``MOEX_SPFI`` (MOEX standardised derivatives market trading days), ``CBR_RUONIA``
(days for which the Bank of Russia publishes RUONIA) and ``MOEX_RUSFAR`` (RUB overnight RUSFAR
calculation days) - see :mod:`mdhub.loaders.calendar`."""
DEFAULT_CALENDAR: Final = "MOEX_STOCK"


class CalendarPredicate:
    """Callable ``(day, calendar) -> bool``; base implementation = weekday fallback."""

    def __call__(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        return is_weekday(day)

    def is_trading_day(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        """Business day or a weekend/holiday session day of ``calendar``'s engine. The base
        implementation knows no weekend sessions (= business day)."""
        return self(day, calendar)


weekday_calendar: Final = CalendarPredicate()
"""Monday-Friday for every calendar."""


class DbCalendar(CalendarPredicate):
    """Looks up ``ref.calendar_day`` - per day the row of the highest-priority source
    (``ref.calendar_source_rank``: observed > MOEX ISS calendar > curated > rule >
    curated_unverified, ``known_at`` breaks ties; same rule as ``ref.calendar_effective``);
    falls back to weekdays.

    Results are cached per ``(calendar, day)`` for ``ttl`` seconds. Database errors (table
    missing, connection down) fall back to weekdays with a warning - gating must never stop
    the scheduler.
    """

    def __init__(self, db: Database, *, ttl: float = 3600.0) -> None:
        self._db = db
        self._ttl = ttl
        self._cache: dict[tuple[str, date], tuple[float, bool]] = {}
        self._sessions: dict[tuple[str, date], tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def __call__(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        key = (calendar, day)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        value = self._lookup(day, calendar)
        result = is_weekday(day) if value is None else value
        with self._lock:
            self._cache[key] = (now, result)
        return result

    def is_trading_day(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        """Business day, or a day listed in ``ref.weekend_session`` for ``calendar`` (any
        source). Database errors / a missing table: business days only."""
        if self(day, calendar):
            return True
        key = (calendar, day)
        now = time.monotonic()
        with self._lock:
            hit = self._sessions.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        result = self._session_lookup(day, calendar)
        with self._lock:
            self._sessions[key] = (now, result)
        return result

    def _session_lookup(self, day: date, calendar: str) -> bool:
        try:
            with self._db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
                exists = cur.execute("SELECT to_regclass('ref.weekend_session')").fetchone()
                if not exists or exists[0] is None:
                    return False
                row = cur.execute(
                    "SELECT 1 FROM ref.weekend_session "
                    "WHERE calendar = %s AND day = %s AND NOT is_deleted LIMIT 1",
                    (calendar, day),
                ).fetchone()
        except psycopg.Error as exc:
            log.warning("weekend-session lookup failed (%s); business days only", exc)
            return False
        return row is not None

    def _lookup(self, day: date, calendar: str) -> bool | None:
        try:
            with self._db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
                exists = cur.execute("SELECT to_regclass('ref.calendar_day')").fetchone()
                if not exists or exists[0] is None:
                    return None
                row = cur.execute(
                    """
                    SELECT is_business_day FROM ref.calendar_day
                    WHERE calendar = %s AND day = %s AND NOT is_deleted
                    ORDER BY ref.calendar_source_rank(source), known_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (calendar, day),
                ).fetchone()
        except psycopg.Error as exc:
            log.warning("calendar lookup failed (%s); using weekday fallback", exc)
            return None
        return None if row is None or row[0] is None else bool(row[0])
