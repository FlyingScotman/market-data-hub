"""ECB eurofxref parser and the ecb_fx job (recorded ECB files, real PostgreSQL)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.ecb  # noqa: F401 - registers the job
from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import ecb

FIX = Path(__file__).parent / "fixtures" / "official"
Conn = psycopg.Connection[DictRow]


def _http_factory(**kw: Any) -> HttpClient:
    kw["max_retries"] = 0
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits={"www.ecb.europa.eu": 1e6},
        sleep=lambda _s: None,
        **kw,
    )


def _run(params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job("ecb_fx", params, settings=settings, db=db, http_factory=_http_factory)


def test_parse_daily_xml() -> None:
    rates = ecb.parse_xml((FIX / "ecb_daily.xml").read_bytes())
    by = {r.ccy: r for r in rates}
    assert {r.rate_date for r in rates} == {date(2026, 10, 15)}
    assert by["USD"].rate_per_eur == Decimal("1.1208")
    assert by["DKK"].rate_per_eur == Decimal("7.4750")  # text preserved, no float
    assert len(rates) >= 29


def test_parse_hist_90d_and_zip_agree() -> None:
    xml_rates = {
        (r.rate_date, r.ccy): r.rate_per_eur
        for r in ecb.parse_xml((FIX / "ecb_hist_90d.xml").read_bytes())
    }
    zip_rates = ecb.parse_hist_zip(
        (FIX / "ecb_hist_subset.zip").read_bytes(), date_from=date(2026, 9, 1)
    )
    assert zip_rates
    for r in zip_rates:
        assert xml_rates[(r.rate_date, r.ccy)] == r.rate_per_eur  # 7.475 == 7.4750
    old = ecb.parse_hist_zip((FIX / "ecb_hist_subset.zip").read_bytes(), date_to=date(1999, 1, 4))
    assert {"USD", "CYP"} <= {r.ccy for r in old}  # 1999 row incl. pre-euro CYP
    assert all(r.rate_per_eur > 0 for r in old)


def test_zip_bad_header_fails() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("eurofxref-hist.csv", "Datum,USD\n2026-01-01,1.1\n")
    with pytest.raises(FormatDriftError):
        ecb.parse_hist_zip(buf.getvalue())


@pytest.fixture
def ecb_mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(ecb.DAILY_URL).mock(
            return_value=httpx.Response(200, content=(FIX / "ecb_daily.xml").read_bytes())
        )
        router.get(ecb.HIST_90D_URL).mock(
            return_value=httpx.Response(200, content=(FIX / "ecb_hist_90d.xml").read_bytes())
        )
        router.get(ecb.HIST_ZIP_URL).mock(
            return_value=httpx.Response(200, content=(FIX / "ecb_hist_subset.zip").read_bytes())
        )
        yield router


def test_ecb_fx_job_daily_and_dedup(
    ecb_mock: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    # empty table -> seeded from the history zip
    seed = _run({}, settings, db)
    assert seed.status == "succeeded" and seed.result["endpoint"] == "eurofxref_hist_zip"
    daily = _run({}, settings, db)
    assert daily.result["endpoint"] == "eurofxref_daily"
    again = _run({}, settings, db)
    assert again.result == {"endpoint": "eurofxref_daily", "unchanged": True}
    caps = conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE endpoint = 'eurofxref_daily'"
    ).fetchone()
    assert caps is not None and caps["n"] == 1  # identical payload not stored twice
    row = conn.execute(
        "SELECT * FROM md.ecb_fx WHERE rate_date = '2026-10-15' AND ccy = 'USD'"
    ).fetchone()
    assert row is not None and row["rate_per_eur"] == Decimal("1.1208")
    hist = conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone()
    assert hist is not None and hist["n"] == 0  # 7.475 vs 7.4750 is not a revision


def test_ecb_fx_range_uses_90d(
    ecb_mock: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    out = _run({"date_from": "2026-10-01", "date_to": "2026-10-09"}, settings, db)
    assert out.result["endpoint"] == "eurofxref_hist_90d"
    days = conn.execute("SELECT DISTINCT rate_date FROM md.ecb_fx ORDER BY 1").fetchall()
    assert days[0]["rate_date"] == date(2026, 10, 1)
    assert all(date(2026, 10, 1) <= r["rate_date"] <= date(2026, 10, 9) for r in days)
