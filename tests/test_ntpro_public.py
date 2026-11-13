"""The public NTPro feed is indicative and its row clock is not a side tick."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mdhub.core.errors import FormatDriftError
from mdhub.sources import ntpro


def test_public_quotes_preserve_exact_prices_and_row_clock() -> None:
    raw = b"""{"status":"ok","data":[
      {"name":"USD/RUB_TOM","bid":10.123456789,"offer":10.223456789,
       "creationDateTime":"2026-01-05 09:00:01.123000+00:00"},
      {"name":"EUR/RUB_TOM","bid":11.1,"offer":11.3,
       "creationDateTime":"2026-01-05 12:00:02+03:00"},
      {"name":"CNH/RUB_TOM","bid":1,"offer":2,
       "creationDateTime":"2026-01-05 09:00:02+00:00"}]}"""
    quotes = ntpro.parse_snapshot(raw)

    assert set(quotes) == {"USD", "EUR"}
    assert quotes["USD"].bid == Decimal("10.123456789")
    assert quotes["USD"].offer == Decimal("10.223456789")
    assert quotes["USD"].quote_ts == datetime(2026, 1, 5, 9, 0, 1, 123000, tzinfo=UTC)
    assert quotes["EUR"].quote_ts == datetime(2026, 1, 5, 9, 0, 2, tzinfo=UTC)
    assert quotes["USD"].reasons == ()


def test_missing_quote_is_not_replaced_by_another_currency() -> None:
    quotes = ntpro.parse_snapshot(b'{"status":"ok","data":[]}')
    assert quotes["USD"].quote_ts is None
    assert quotes["EUR"].bid is None
    assert quotes["EUR"].reasons == ("spot_missing",)


def test_bad_clock_and_crossed_sides_are_visible_per_currency() -> None:
    quotes = ntpro.parse_snapshot(
        b'{"status":"ok","data":['
        b'{"name":"USD/RUB_TOM","bid":11,"offer":10,"creationDateTime":"2026-01-05 09:00:00"},'
        b'{"name":"EUR/RUB_TOM","bid":10,"offer":11,"creationDateTime":"2026-01-05T09:00:00Z"}]}'
    )
    assert quotes["USD"].quote_ts is None
    assert set(quotes["USD"].reasons) == {"spot_time_unknown", "crossed_spot"}
    assert quotes["EUR"].reasons == ()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"status":"error","data":[]}',
        b'{"status":"ok","data":[{"name":"USD/RUB_TOM"},{"name":"USD/RUB_TOM"}]}',
    ],
)
def test_malformed_or_duplicate_snapshot_fails_closed(raw: bytes) -> None:
    with pytest.raises(FormatDriftError):
        ntpro.parse_snapshot(raw)
