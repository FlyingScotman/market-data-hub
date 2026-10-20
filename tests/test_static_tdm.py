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


# ------------------------------------------------------------------------ HS11 face date

from decimal import Decimal  # noqa: E402

from mdhub.snapshot.static import face_value_date  # noqa: E402

TODAY = date(2026, 10, 20)


def _bond(face: str, initial: str = "1000") -> dict[str, Any]:
    return {
        "face_value": Decimal(face),
        "initial_face_value": Decimal(initial),
        "issue_date": date(2017, 10, 1),
    }


# ЯрОбл 2017-like amortizer: 750 already repaid, 125 due today, 125 in 2027
REDEMPTIONS = [
    {"date": date(2024, 10, 20), "value_per_bond": Decimal("375")},
    {"date": date(2025, 10, 20), "value_per_bond": Decimal("375")},
    {"date": TODAY, "value_per_bond": Decimal("125")},
    {"date": date(2027, 10, 20), "value_per_bond": Decimal("125")},
]


def test_face_value_date_on_a_payment_day() -> None:
    # MOEX already deducted today's redemption (static built after the intraday update)
    assert face_value_date(_bond("125"), REDEMPTIONS, TODAY) == TODAY
    # not yet deducted: the face is outstanding since the previous redemption
    assert face_value_date(_bond("250"), REDEMPTIONS, TODAY) == date(2025, 10, 20)
    # before any redemption: since issue
    assert face_value_date(_bond("1000"), REDEMPTIONS, date(2024, 1, 1)) == date(2017, 10, 1)


def test_face_value_date_unknown_when_schedule_does_not_explain_face() -> None:
    assert face_value_date(_bond("400"), REDEMPTIONS, TODAY) is None
    missing = [*REDEMPTIONS, {"date": date(2028, 1, 1), "value_per_bond": None}]
    assert face_value_date(_bond("125"), missing, TODAY) is None
    assert face_value_date({**_bond("125"), "initial_face_value": None}, REDEMPTIONS, TODAY) is None
    # kopeck rounding of per-bond amounts
    rounded = [{"date": TODAY, "value_per_bond": Decimal("333.33")}]
    assert face_value_date(_bond("666.67"), rounded, TODAY) == TODAY
