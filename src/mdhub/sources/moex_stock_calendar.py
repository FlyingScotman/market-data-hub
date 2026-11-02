"""MOEX APIM stock calendar: physical session dates, separate from settlement days.

The returned month is complete only when every requested calendar day is present and
has a known trading flag. This adapter does not publish an API or alter existing
business-day calendars. Source entitlement and historical coverage need live validation.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.core.iss import APIM_BASE_URL, iter_pages_until_empty, to_date, to_int

STOCK_CALENDAR_URL = f"{APIM_BASE_URL}/calendars/stock.json"
REQUIRED_COLUMNS = ("tradedate", "is_traded", "trade_session_date", "reason", "updatetime")


@dataclass(frozen=True, slots=True)
class StockCalendarDay:
    day: date
    is_traded: bool | None
    trade_session_date: date | None
    reason: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class StockCalendarMonth:
    year: int
    month: int
    days: tuple[StockCalendarDay, ...]
    missing_days: tuple[date, ...]
    unknown_days: tuple[date, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_days and not self.unknown_days


def parse_day(row: dict[str, Any]) -> StockCalendarDay:
    """Parse an ISS ``off_days`` row; retain unknown flags and session attribution."""
    day = to_date(row.get("tradedate"), field="tradedate")
    if day is None:
        raise FormatDriftError("stock calendar: missing tradedate")
    flag = to_int(row.get("is_traded"), field="is_traded")
    if flag not in (None, 0, 1):
        raise FormatDriftError(f"stock calendar {day}: invalid is_traded")
    session_date = to_date(row.get("trade_session_date"), field="trade_session_date")
    reason = row.get("reason") or None
    if reason is not None and reason not in ("H", "W", "N", "T"):
        raise FormatDriftError(f"stock calendar {day}: unrecognized reason {reason!r}")
    if (
        (reason == "W" and (flag != 1 or session_date is None))
        or (reason == "H" and flag != 0)
        or (reason in ("N", "T") and flag != 1)
    ):
        raise FormatDriftError(f"stock calendar {day}: inconsistent flag and reason")
    if flag == 0 and session_date is not None:
        raise FormatDriftError(f"stock calendar {day}: closed day has a session date")
    updated = row.get("updatetime")
    return StockCalendarDay(
        day,
        None if flag is None else bool(flag),
        session_date,
        reason,
        str(updated) if updated else None,
    )


def fetch_month(http: HttpClient, year: int, month: int) -> StockCalendarMonth:
    """Fetch all pages for one month. Caller supplies APIM-authenticated HTTP client.

    Fail closed for malformed, duplicate or incomplete data; never infer a missing
    date from weekdays or from observed prices. Calendar date and assigned trade date
    remain distinct, even when the latter falls outside this month.
    """
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    rows: dict[date, StockCalendarDay] = {}
    seen: set[date] = set()
    for page in iter_pages_until_empty(
        http,
        STOCK_CALENDAR_URL,
        {
            "from": first.isoformat(),
            "till": last.isoformat(),
            "show_all_days": 1,
            "iss.only": "off_days",
        },
        block="off_days",
        required=REQUIRED_COLUMNS,
    ):
        for row in page.rows:
            item = parse_day(row)
            if item.day in seen:
                raise FormatDriftError(f"stock calendar: duplicate date {item.day}")
            seen.add(item.day)
            if first <= item.day <= last:
                rows[item.day] = item
    expected = tuple(first + timedelta(days=n) for n in range((last - first).days + 1))
    missing = tuple(d for d in expected if d not in rows)
    unknown = tuple(
        d for d in expected if d in rows and (rows[d].is_traded is None or rows[d].reason is None)
    )
    return StockCalendarMonth(year, month, tuple(rows[d] for d in sorted(rows)), missing, unknown)
