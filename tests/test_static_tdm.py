"""Static snapshot additions for Trading Desk Monitor (WEB-13, TDM brief R9 HS2-HS13)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from mdhub.snapshot.static import coupon_frequency_from_schedule


def _coupons(start: date, lengths: list[int]) -> list[dict[str, Any]]:
    out = []
    d = start
    for n in lengths:
        out.append({"start_date": d, "pay_date": d + timedelta(days=n)})
        d += timedelta(days=n)
    return out


def test_coupon_frequency_from_schedule() -> None:
    # irregular long first period (MOEX then omits COUPONFREQUENCY): the median is regular
    assert coupon_frequency_from_schedule(_coupons(date(2024, 1, 10), [240, 91, 91, 92, 91])) == 4
    assert coupon_frequency_from_schedule(_coupons(date(2024, 1, 10), [182, 182, 183])) == 2
    assert coupon_frequency_from_schedule(_coupons(date(2024, 1, 10), [30, 31, 30, 31])) == 12
    assert coupon_frequency_from_schedule(_coupons(date(2024, 1, 10), [28, 28, 28])) == 13
    assert coupon_frequency_from_schedule(_coupons(date(2024, 1, 10), [364, 365])) == 1
    # fewer than two periods with dates: unknown, not guessed
    assert coupon_frequency_from_schedule(_coupons(date(2024, 1, 10), [182])) is None
    assert (
        coupon_frequency_from_schedule([{"start_date": None, "pay_date": date(2025, 1, 1)}] * 3)
        is None
    )
    assert coupon_frequency_from_schedule([]) is None
