"""Clocks and business dates.

Conventions (DESIGN §2 "separate clocks"):

* every stored timestamp is timezone-aware (``timestamptz``); never use naive datetimes,
* business dates (``trade_date``, ``effective_date``) are :class:`datetime.date` in
  Europe/Moscow (:data:`MSK`),
* ``fetched_at`` is UTC (:func:`utc_now`), ``source_ts`` is the source's own clock converted
  to an aware datetime (MOEX ISS times are MSK wall-clock: :func:`msk_datetime`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

__all__ = [
    "MSK",
    "UTC",
    "BusinessDayPredicate",
    "add_business_days",
    "business_days_back",
    "date_range",
    "ensure_aware",
    "is_weekday",
    "msk_datetime",
    "msk_now",
    "msk_today",
    "next_business_day",
    "previous_business_day",
    "previous_session_day",
    "to_msk",
    "utc_now",
]

MSK: Final = ZoneInfo("Europe/Moscow")
"""Moscow time zone (UTC+3, no DST since 2014)."""

BusinessDayPredicate = Callable[[date], bool]
"""``predicate(day) -> True`` if ``day`` is a business day for some calendar."""


def utc_now() -> datetime:
    """Current time, aware, UTC."""
    return datetime.now(UTC)


def msk_now() -> datetime:
    """Current time, aware, Europe/Moscow."""
    return datetime.now(MSK)


def msk_today() -> date:
    """Current business-calendar date in Moscow."""
    return msk_now().date()


def ensure_aware(dt: datetime, tz: ZoneInfo = MSK) -> datetime:
    """Attach ``tz`` (default MSK) to a naive datetime; aware datetimes pass through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)


def to_msk(dt: datetime) -> datetime:
    """Convert an aware datetime to MSK (naive input is rejected)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; attach a time zone first")
    return dt.astimezone(MSK)


def msk_datetime(d: date, t: time) -> datetime:
    """Combine an MSK wall-clock date and time into an aware datetime."""
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=MSK)


def is_weekday(d: date) -> bool:
    """Monday-Friday (fallback business-day predicate when no calendar is known)."""
    return d.weekday() < 5


def previous_business_day(d: date, is_business_day: BusinessDayPredicate = is_weekday) -> date:
    """Last business day strictly before ``d``."""
    return add_business_days(d, -1, is_business_day)


def previous_session_day(d: date, is_trading_day: BusinessDayPredicate | None = None) -> date:
    """Last **trading** day strictly before ``d`` - the session a quote older than the
    snapshot clock belongs to. ``is_trading_day`` must count weekend/holiday sessions of the
    engine (:meth:`mdhub.core.bizday.CalendarPredicate.is_trading_day`), not only settlement
    business days: a Monday pre-open snapshot after a Sunday session carries Sunday's last
    trade. Default Monday-Friday; scans at most 31 days (then ``d - 1``)."""
    pred = is_trading_day or is_weekday
    cur = d - timedelta(days=1)
    for _ in range(31):
        if pred(cur):
            return cur
        cur -= timedelta(days=1)
    return d - timedelta(days=1)


def next_business_day(d: date, is_business_day: BusinessDayPredicate = is_weekday) -> date:
    """First business day strictly after ``d``."""
    return add_business_days(d, 1, is_business_day)


def add_business_days(
    d: date, n: int, is_business_day: BusinessDayPredicate = is_weekday, *, max_scan: int = 3660
) -> date:
    """Move ``n`` business days from ``d`` (negative = backwards). ``n == 0`` returns ``d``."""
    step = 1 if n > 0 else -1
    remaining = abs(n)
    cur = d
    scanned = 0
    while remaining:
        cur += timedelta(days=step)
        scanned += 1
        if scanned > max_scan:
            raise ValueError(f"no business day found within {max_scan} days of {d}")
        if is_business_day(cur):
            remaining -= 1
    return cur


def business_days_back(
    today: date,
    days_back: Sequence[int],
    is_business_day: BusinessDayPredicate = is_weekday,
) -> list[date]:
    """Resolve schedule ``days_back`` offsets to business dates.

    ``0`` means ``today`` itself (omitted if ``today`` is not a business day); ``n > 0`` is the
    n-th business day before ``today``. Result is de-duplicated, in the order given.
    """
    out: list[date] = []
    for n in days_back:
        if n < 0:
            raise ValueError("days_back offsets must be >= 0")
        if n == 0:
            if not is_business_day(today):
                continue
            d = today
        else:
            d = add_business_days(today, -n, is_business_day)
        if d not in out:
            out.append(d)
    return out


def date_range(start: date, end: date) -> Iterator[date]:
    """Calendar days from ``start`` to ``end`` inclusive."""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)
