"""Business-day predicates backed by ``ref.calendar_day`` with a weekday fallback.

Source precedence (not recency) decides between rows of one day: see
:data:`mdhub.loaders.calendar.SOURCE_PRIORITY`.

``ref.calendar_day(calendar, day, is_business_day, source, known_at)`` (DESIGN §4) is owned by
the calendar loader. Until that table exists - or for days it does not cover - the
Monday-Friday fallback applies.
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

CALENDARS: Final = ("MOEX_STOCK", "MOEX_FX", "MOEX_FORTS", "RU_PRODUCTION")
DEFAULT_CALENDAR: Final = "MOEX_STOCK"


class CalendarPredicate:
    """Callable ``(day, calendar) -> bool``; base implementation = weekday fallback."""

    def __call__(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        return is_weekday(day)


weekday_calendar: Final = CalendarPredicate()
"""Monday-Friday for every calendar."""


class DbCalendar(CalendarPredicate):
    """Looks up ``ref.calendar_day`` - per day the row of the highest-priority source
    (``ref.calendar_source_rank``: observed > MOEX ISS calendar > curated > curated_unverified,
    ``known_at`` breaks ties; same rule as ``ref.calendar_effective``); falls back to weekdays.

    Results are cached per ``(calendar, day)`` for ``ttl`` seconds. Database errors (table
    missing, connection down) fall back to weekdays with a warning - gating must never stop
    the scheduler.
    """

    def __init__(self, db: Database, *, ttl: float = 3600.0) -> None:
        self._db = db
        self._ttl = ttl
        self._cache: dict[tuple[str, date], tuple[float, bool]] = {}
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

    def _lookup(self, day: date, calendar: str) -> bool | None:
        try:
            with self._db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
                exists = cur.execute("SELECT to_regclass('ref.calendar_day')").fetchone()
                if not exists or exists[0] is None:
                    return None
                row = cur.execute(
                    """
                    SELECT is_business_day FROM ref.calendar_day
                    WHERE calendar = %s AND day = %s
                    ORDER BY ref.calendar_source_rank(source), known_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (calendar, day),
                ).fetchone()
        except psycopg.Error as exc:
            log.warning("calendar lookup failed (%s); using weekday fallback", exc)
            return None
        return None if row is None or row[0] is None else bool(row[0])
