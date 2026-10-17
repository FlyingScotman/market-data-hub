"""Rosstat monthly CPI: workbook parser and the rosstat_cpi job (recorded file, real PostgreSQL)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.rosstat as loader
from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import rosstat

FIX = Path(__file__).parent / "fixtures" / "rosstat"
WORKBOOK = (FIX / "ipc_mes_09-2026.xlsx").read_bytes()
SHEET = "xl/worksheets/sheet2.xml"  # sheet "01"
Conn = psycopg.Connection[DictRow]
PAGE = (
    '<a href="/storage/mediabank/ipc_mes_08-2026.xlsx">old</a>'
    '<a href="/storage/mediabank/ipc_mes_09-2026.xlsx">CPI</a>'
    '<a href="/storage/mediabank/ipc_mes_12-2025.xlsx">older year</a>'
    '<a href="/storage/mediabank/ipc_RF_fo_sub_09-2026.xlsx">weekly</a>'
)


def _patched(old: bytes, new: bytes) -> bytes:
    """The workbook with one replacement in sheet ``01``."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(WORKBOOK)) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == SHEET:
                assert data.count(old) == 1, "fixture layout changed"
                data = data.replace(old, new)
            dst.writestr(item, data)
    return out.getvalue()


def test_parse_cpi_monthly() -> None:
    months = rosstat.parse_cpi_monthly(WORKBOOK)
    by = {m.period: m.index_pct for m in months}
    assert len(months) == 35 * 12 + 9  # 1991-01 .. 2026-09
    assert months[0].period == date(1991, 1, 1) and months[-1].period == date(2026, 9, 1)
    assert by[date(1992, 1, 1)] == Decimal("345.3")
    assert by[date(2022, 3, 1)] == Decimal("107.61")
    assert by[date(2026, 9, 1)] == Decimal("100.35")
    assert all(m.period.day == 1 for m in months)


def test_find_cpi_workbook_takes_newest_link() -> None:
    assert rosstat.find_cpi_workbook(PAGE) == "/storage/mediabank/ipc_mes_09-2026.xlsx"
    with pytest.raises(FormatDriftError):
        rosstat.find_cpi_workbook("<html>no links</html>")


def test_shifted_value_fails_dec_dec_check() -> None:
    with pytest.raises(FormatDriftError, match="Dec/Dec"):
        rosstat.parse_cpi_monthly(_patched(b"<v>107.61</v>", b"<v>101.61</v>"))


def test_hole_inside_a_year_fails() -> None:
    with pytest.raises(FormatDriftError, match="missing months"):
        rosstat.parse_cpi_monthly(_patched(b"<v>107.61</v>", b""))


def test_out_of_range_value_fails() -> None:
    with pytest.raises(FormatDriftError, match="out of range"):
        rosstat.parse_cpi_monthly(_patched(b"<v>107.61</v>", b"<v>1076.1</v>"))


# --------------------------------------------------------------------------------- job


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits={"rosstat.gov.ru": 1e6},
        sleep=lambda _s: None,
        max_retries=0,
        **kw,
    )


def _run(params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job("rosstat_cpi", params, settings=settings, db=db, http_factory=_http_factory)


@pytest.fixture
def served() -> Any:
    with respx.mock(base_url=rosstat.BASE_URL) as router:
        router.get(rosstat.PRICE_PAGE_PATH).mock(
            return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        )
        yield router.get("/storage/mediabank/ipc_mes_09-2026.xlsx").mock(
            return_value=httpx.Response(200, content=WORKBOOK)
        )


def test_rosstat_cpi_loads_unchanged_and_range(
    served: Any, settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    today_is(date(2026, 10, 16))
    out = _run({}, settings, db)
    assert out.status == "succeeded" and out.result["months"] == 429
    rows = conn.execute(
        "SELECT * FROM md.fixing WHERE index_code = 'CPI_RU_MOM' ORDER BY fixing_date"
    ).fetchall()
    assert len(rows) == 429
    sep = rows[-1]
    assert sep["fixing_date"] == date(2026, 9, 1) and sep["value"] == Decimal("100.35")
    assert sep["source"] == "rosstat" and sep["unit"] == "index"
    assert sep["details"] == {"period": "2026-09"} and sep["capture_id"] is not None

    again = _run({}, settings, db)  # same workbook: not captured or parsed again
    assert again.result == {
        "workbook": "/storage/mediabank/ipc_mes_09-2026.xlsx",
        "unchanged": True,
    }

    ranged = _run({"date_from": "2022-02-15", "date_to": "2022-04-30"}, settings, db)
    assert ranged.result["first"] == "2022-02-01" and ranged.result["months"] == 3
    assert ranged.result["inserted"] == 0 and ranged.result["updated"] == 0


def test_rosstat_cpi_revision_goes_to_history(
    served: Any, settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    today_is(date(2026, 10, 16))
    _run({}, settings, db)
    revised = _patched(b'<c r="AK14" s="12"><v>100.35</v>', b'<c r="AK14" s="12"><v>100.36</v>')
    served.mock(return_value=httpx.Response(200, content=revised))
    out = _run({}, settings, db)
    assert out.result["updated"] == 1
    hist = conn.execute(
        "SELECT old_row FROM ops.row_history WHERE table_name = 'md.fixing'"
    ).fetchall()
    assert [h["old_row"]["value"] for h in hist] == [100.35]


def test_dataset_registered() -> None:
    spec = get_dataset("rosstat_cpi")
    assert spec.frequency == "monthly" and spec.backfill_job == "rosstat_cpi"
    assert spec.history_start == loader.HISTORY_START == date(1991, 1, 1)
