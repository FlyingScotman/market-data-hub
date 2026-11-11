"""SPB Russian bond discovery and chart bars. Reports supply units only, never prices.

Public website endpoints, not a contracted feed. Daily timestamps are UTC-midnight bucket
labels. A closed calendar date is eligible; no exchange calendar or official-final claim.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from mdhub.core.decimalutil import require_decimal
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient

BASE_URL: Final = "https://spbexchange.ru"
UNIVERSE_PATH: Final = "/api/reader/marketdata/default/rus"
CARD_PATH: Final = "/api/listing/v1/security/cardBond/ru/{}"
ARCHIVE_PATH: Final = "/api/reader/marketdata/archive/cb/active-days"
HISTORY_PATH: Final = "/api/reader/marketdata/charts/chistory"
PARSER_VERSION: Final = "1"
MAX_LOAD_DAYS: Final = 31
SKIP_SHARE_DENOMINATOR: Final = 5
VENUE: Final = "SPB"
PRICE_BASIS: Final = "chart_close"
_ISIN: Final = re.compile(r"^RU[A-Z0-9]{9}[0-9]$")
_TICKER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
_CURRENCIES: Final = frozenset({"RUB", "USD", "EUR", "CNY", "HKD", "CHF", "GBP", "JPY"})


@dataclass(frozen=True)
class Candidate:
    security_id: int
    ticker: str
    name: str
    category: str


def skip_limit(total: int) -> int:
    """Most bonds a run may skip (one that fails validation is skipped with a diagnostic and the
    run ends ``partial``); one more is treated as format drift and fails the run.

    A single odd bond is routine (a new issue not yet in yesterday's report, an unexpected card
    category, one malformed chart), while a changed format breaks every card, report row or chart
    alike. One fifth of the discovered bonds (at least one) separates the two: 4 of today's 21,
    1 of a universe of 2-9."""
    return max(1, total // SKIP_SHARE_DENOMINATOR)


def json_body(payload: bytes) -> Any:
    try:
        return json.loads(payload, parse_float=Decimal)
    except (ValueError, UnicodeError) as exc:
        raise FormatDriftError(f"SPB: invalid JSON: {exc}") from None


def valid_isin(value: str) -> bool:
    """Russian ISIN with the ISO 6166 check digit (identity is also checked on the card)."""
    if not _ISIN.fullmatch(value):
        return False
    digits = "".join(c if c.isdigit() else str(ord(c) - 55) for c in value)
    return (
        sum(sum(divmod(int(c) * (2 if i % 2 else 1), 10)) for i, c in enumerate(reversed(digits)))
        % 10
        == 0
    )


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or value.strip() == "-":
        raise FormatDriftError(f"SPB: missing/invalid {key}")
    return value.strip()


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FormatDriftError("SPB: expected an object")
    return value


def parse_universe(payload: bytes) -> list[Candidate]:
    data = json_body(payload)
    if not isinstance(data, list):
        raise FormatDriftError("SPB: expected the Russian market array")
    out = []
    ids: set[int] = set()
    tickers: set[str] = set()
    for raw in data:
        row = _object(raw)
        kind = _text(row, "securityKind")
        if kind != "Облигации":
            continue
        category = _text(row, "securityCategory")
        if "иностран" in category.lower():
            continue
        sid = row.get("idSecurities")
        ticker = _text(row, "srtsCode")
        if type(sid) is not int or sid <= 0 or not _TICKER.fullmatch(ticker):
            raise FormatDriftError("SPB: invalid bond id/ticker")
        if sid in ids or ticker in tickers:
            raise FormatDriftError("SPB: duplicate bond id/ticker")
        ids.add(sid)
        tickers.add(ticker)
        out.append(Candidate(sid, ticker, _text(row, "fullnameRu"), category))
    return sorted(out, key=lambda c: c.ticker)


def parse_card(payload: bytes, candidate: Candidate) -> dict[str, Any]:
    row = _object(json_body(payload))
    if (
        type(row.get("idSecurities")) is not int
        or row["idSecurities"] != candidate.security_id
        or _text(row, "srtsCode") != candidate.ticker
        or _text(row, "securityKind") != "облигация"
        or _text(row, "securityCategory") != candidate.category
        or "иностран" in candidate.category.lower()
        or _text(row, "sgroupSec") != "Российские ценные бумаги"
    ):
        raise FormatDriftError("SPB: card contradicts Russian bond identity/classification")
    isin = _text(row, "sisinCode")
    if not valid_isin(isin):
        raise FormatDriftError("SPB: invalid Russian ISIN/check digit")
    return {
        "venue": VENUE,
        "isin": isin,
        "ticker": candidate.ticker,
        "security_id": candidate.security_id,
        "name": candidate.name,
        "card_price_unit": _text(row, "scurrencyKotir"),
        "settlement_currency": _text(row, "scurrencyCalc"),
    }


def parse_report_archive(payload: bytes, *, today: date) -> tuple[date, str]:
    """Latest prior-date full report advertised by SPB, at most 31 days old.

    Reports are reference evidence; their presence does not finalize chart bars.
    """
    data = json_body(payload)
    if not isinstance(data, list):
        raise FormatDriftError("SPB: expected report archive array")
    reports: dict[date, str] = {}
    for raw in data:
        row = _object(raw)
        try:
            d = date.fromisoformat(_text(row, "date"))
        except ValueError:
            raise FormatDriftError("SPB: invalid archive date") from None
        events = row.get("eventTypes")
        if not isinstance(events, list):
            raise FormatDriftError("SPB: missing archive eventTypes")
        if not today - timedelta(days=31) <= d < today:
            continue
        for event in events:
            path = _object(event).get("fullPath")
            if not path:
                continue
            # Only this official route, no arbitrary external downloads/redirects.
            expected = f"/static/reports/results/{d.isoformat()}-full.pdf"
            if not isinstance(path, str) or path.replace("//", "/") != expected:
                raise FormatDriftError("SPB: unexpected full-report path")
            if d in reports:
                raise FormatDriftError("SPB: ambiguous full report")
            reports[d] = expected
    if not reports:
        raise FormatDriftError("SPB: no recent completed-date full report for unit reference")
    latest = max(reports)
    return latest, reports[latest]


def parse_unit_report(
    payload: bytes,
    report_date: date,
    identities: dict[str, str],
    *,
    missing_ok: bool = False,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Ticker+ISIN -> price-unit code, settlement currency from fixed report columns.

    This parser deliberately ignores price values after checking the numeric column boundary.
    It requires complete ticker/ISIN rows; wrapped or missing rows fail closed. Units are
    never guessed from an instrument name. With ``missing_ok`` a discovered bond without a row
    (a new issue) is left out of the result for the caller to skip; malformed rows still raise.
    """
    try:
        pdf = PdfReader(io.BytesIO(payload), strict=True)
        if not pdf.pages or len(pdf.pages) > 300:
            raise FormatDriftError("SPB: unexpected unit report size")
        texts = [p.extract_text() for p in pdf.pages]
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise FormatDriftError(f"SPB: unreadable unit report: {exc}") from None
    if f"итогах торгов ценными бумагами за {report_date.isoformat()}" not in texts[0]:
        raise FormatDriftError("SPB: report date/title mismatch")
    out: dict[tuple[str, str], tuple[str, str]] = {}
    # Required header order and the ISIN -> unit -> currency -> first numeric value boundary.
    for text in texts:
        if not re.search(r"ISIN\s+Валюта цены\s+Валюта расчетов", text):
            raise FormatDriftError("SPB: unit-report columns changed")
        for line in text.splitlines():
            cells = line.split()
            if not cells or cells[0] not in identities:
                continue
            positions = [i for i, c in enumerate(cells) if i > 0 and _ISIN.fullmatch(c)]
            if not positions:
                continue
            if len(positions) != 1:
                raise FormatDriftError("SPB: ambiguous ISIN in unit report row")
            i = positions[0]
            if i < 2 or len(cells) != i + 14:
                raise FormatDriftError("SPB: unit-report row columns changed")
            ticker, isin = cells[0], cells[i]
            unit, currency = cells[i + 1 : i + 3]
            if not _TICKER.fullmatch(ticker) or not valid_isin(isin) or identities[ticker] != isin:
                raise FormatDriftError("SPB: invalid identity in unit report")
            if unit not in _CURRENCIES | {"PCT"} or currency not in _CURRENCIES:
                raise FormatDriftError("SPB: unknown unit/currency in report")
            for price in cells[i + 3 :]:
                require_decimal(price, field="SPB report numeric column")
            key = ticker, isin
            if key in out:
                raise FormatDriftError("SPB: duplicate ticker/ISIN in unit report")
            out[key] = unit, currency
    if len(out) != len(identities) and not missing_ok:
        raise FormatDriftError("SPB: unit report lacks discovered ticker/ISIN rows")
    return out


def history_params(ticker: str, start: date, end: date) -> dict[str, str | int]:
    return {
        "symbol": ticker,
        "resolution": 1440,
        "from": int(datetime.combine(start, time(), UTC).timestamp()),
        "to": int(datetime.combine(end + timedelta(days=1), time(), UTC).timestamp()) - 1,
    }


def fetch(http: HttpClient, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
    return http.get(BASE_URL + path, params=params, expect_json=not path.endswith(".pdf"))


def parse_bars(payload: bytes, *, start: date, end: date, today: date) -> list[dict[str, Any]]:
    data = json_body(payload)
    if not isinstance(data, list):
        raise FormatDriftError("SPB: expected chart-bar array")
    out = []
    seen: set[date] = set()
    for raw in data:
        row = _object(raw)
        ts = row.get("bar_unixtime")
        if type(ts) is not int or ts % 86400:
            raise FormatDriftError("SPB: daily bar timestamp must be UTC midnight seconds")
        try:
            d = datetime.fromtimestamp(ts, UTC).date()
        except (OverflowError, OSError, ValueError):
            raise FormatDriftError("SPB: invalid bar timestamp") from None
        if not start <= d <= end or d >= today:
            raise FormatDriftError("SPB: bar outside completed requested dates")
        if d in seen:
            raise FormatDriftError("SPB: duplicate daily bar")
        seen.add(d)
        values = {
            key: require_decimal(row.get(key), field=f"SPB {key}")
            for key in ("open", "high", "low", "close")
        }
        if (
            min(values.values()) <= 0
            or values["low"] > values["high"]
            or not values["low"] <= values["open"] <= values["high"]
            or not values["low"] <= values["close"] <= values["high"]
        ):
            raise FormatDriftError("SPB: invalid OHLC bounds/positive prices")
        out.append({"trade_date": d, **values})
    return sorted(out, key=lambda r: r["trade_date"])
