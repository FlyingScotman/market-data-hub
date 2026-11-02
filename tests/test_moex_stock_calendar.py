"""Stock session adapter: do not infer unknown or missing days as holidays."""

from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from mdhub.core.errors import FormatDriftError
from mdhub.sources.moex_stock_calendar import fetch_month, parse_day

COLUMNS = ["tradedate", "is_traded", "trade_session_date", "reason", "updatetime"]


class FakeHttp:
    def __init__(self, rows: list[list[object]], page_size: int = 10) -> None:
        self.rows = rows
        self.page_size = page_size
        self.calls: list[dict[str, object]] = []

    def get_json(self, url: str, *, params: dict[str, object]) -> SimpleNamespace:
        self.calls.append(params)
        start = int(params["start"])
        return SimpleNamespace(
            data={
                "off_days": {"columns": COLUMNS, "data": self.rows[start : start + self.page_size]}
            },
            response=httpx.Response(200, text=f"source page {start}"),
        )


def month_rows() -> list[list[object]]:
    first = date(2026, 10, 1)
    return [
        [(first + timedelta(days=n)).isoformat(), 1, None, "N", "2026-10-01 10:00:00"]
        for n in range(31)
    ]


def test_complete_month_and_weekend_session_attribution() -> None:
    rows = month_rows()
    rows[30] = ["2026-10-31", 1, "2026-11-02", "W", "2026-10-30 10:00:00"]
    rows[3] = ["2026-10-04", 0, None, "H", "2026-10-01 10:00:00"]
    http = FakeHttp(rows)
    result = fetch_month(http, 2026, 10)  # type: ignore[arg-type]
    assert result.complete and len(result.days) == 31
    assert result.days[30].trade_session_date == date(2026, 11, 2)
    assert result.days[3].is_traded is False
    assert [c["start"] for c in http.calls] == [0, 10, 20, 30, 31]
    assert http.calls[0]["show_all_days"] == 1


def test_missing_and_unknown_days_do_not_certify_month() -> None:
    rows = month_rows()
    del rows[5]
    rows[6] = ["2026-10-08", None, None, None, None]
    result = fetch_month(FakeHttp(rows), 2026, 10)  # type: ignore[arg-type]
    assert not result.complete
    assert result.missing_days == (date(2026, 10, 6),)
    assert result.unknown_days == (date(2026, 10, 8),)


def test_duplicate_day_is_not_accepted() -> None:
    rows = month_rows()
    rows.append(rows[0])
    with pytest.raises(FormatDriftError, match="duplicate"):
        fetch_month(FakeHttp(rows), 2026, 10)  # type: ignore[arg-type]


def test_unknown_reason_does_not_certify_month() -> None:
    rows = month_rows()
    rows[0][3] = None
    result = fetch_month(FakeHttp(rows), 2026, 10)  # type: ignore[arg-type]
    assert not result.complete and result.unknown_days == (date(2026, 10, 1),)


def test_raw_response_available_before_invalid_row_is_parsed() -> None:
    rows = month_rows()
    rows[12][4] = {"oops": 1}
    captured: list[str] = []
    with pytest.raises(FormatDriftError, match="updatetime"):
        fetch_month(FakeHttp(rows), 2026, 10, on_response=lambda r: captured.append(r.text))  # type: ignore[arg-type]
    assert captured == ["source page 0", "source page 10"]


@pytest.mark.parametrize("value", [{"oops": 1}, "", "2026-02-30 10:00:00", "2026-10-01T10:00:00"])
def test_rejects_invalid_source_update_timestamp(value: object) -> None:
    rows = month_rows()
    rows[0][4] = value
    with pytest.raises(FormatDriftError, match="updatetime"):
        fetch_month(FakeHttp(rows), 2026, 10)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("flag", "reason", "session"),
    [(1, "W", None), (0, "N", None), (1, "H", None), (0, "H", "2026-10-02"), (2, None, None)],
)
def test_inconsistent_rows_are_rejected(flag: int, reason: str | None, session: str | None) -> None:
    with pytest.raises(FormatDriftError):
        parse_day(
            {
                "tradedate": "2026-10-01",
                "is_traded": flag,
                "reason": reason,
                "trade_session_date": session,
                "updatetime": None,
            }
        )
