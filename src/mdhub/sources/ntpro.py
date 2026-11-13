"""Public NTPro FX quote snapshots; the row clock is not a per-side tick clock."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from mdhub.core.errors import FormatDriftError

SOURCE: Final = "ntpro_public"
ENDPOINT: Final = "intraday:ntpro_public_fx"
PARSER_VERSION: Final = "ntpro/1"
URL: Final = "https://ntprog.ru/local_api/public/east/"
INSTRUMENTS: Final = {"USD": "USD/RUB_TOM", "EUR": "EUR/RUB_TOM"}


@dataclass(frozen=True, slots=True)
class Quote:
    currency: str
    instrument: str
    bid: Decimal | None
    offer: Decimal | None
    quote_ts: datetime | None
    reasons: tuple[str, ...] = ()


def _price(value: Any) -> Decimal | None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, int | float | str | Decimal)
    ):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result > 0 else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(UTC) if result.utcoffset() is not None else None


def parse_snapshot(payload: bytes | str) -> dict[str, Quote]:
    """Parse USD/EUR only; missing or malformed individual quotes remain unavailable.

    ``creationDateTime`` is a source row clock. It does not establish independent bid/offer
    update times, execution, or a spot value date. CNH is never treated as CNY.
    """
    try:
        data = json.loads(payload, parse_float=Decimal)
    except (UnicodeDecodeError, ValueError):
        raise FormatDriftError("NTPro public FX: invalid JSON") from None
    if (
        not isinstance(data, dict)
        or data.get("status") != "ok"
        or not isinstance(data.get("data"), list)
    ):
        raise FormatDriftError("NTPro public FX: invalid snapshot shape or status")
    currencies = {instrument: currency for currency, instrument in INSTRUMENTS.items()}
    out: dict[str, Quote] = {}
    for row in data["data"]:
        if not isinstance(row, dict):
            raise FormatDriftError("NTPro public FX: invalid quote row")
        name = row.get("name")
        currency = currencies.get(name) if isinstance(name, str) else None
        if currency is None:
            continue
        if currency in out:
            raise FormatDriftError("NTPro public FX: duplicate quote")
        bid, offer = _price(row.get("bid")), _price(row.get("offer"))
        quote_ts = _timestamp(row.get("creationDateTime"))
        reasons = []
        if bid is None or offer is None:
            reasons.append("spot_price_missing_or_invalid")
        elif bid > offer:
            reasons.append("crossed_spot")
        if quote_ts is None:
            reasons.append("spot_time_unknown")
        out[currency] = Quote(currency, INSTRUMENTS[currency], bid, offer, quote_ts, tuple(reasons))
    for currency, instrument in INSTRUMENTS.items():
        if currency not in out:
            out[currency] = Quote(currency, instrument, None, None, None, ("spot_missing",))
    return out
