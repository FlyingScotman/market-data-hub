"""USD and EUR FORTS futures reach the market snapshot in the form a consumer needs for a
futures-implied FX curve (market-data-hub#152).

MOEX stopped exchange trading in USD and EUR (no CETS swap points), so the dated FORTS futures on
USD/RUB (``Si``) and EUR/RUB (``Eu``), cash-settled on the CBR rate, are the exchange inputs of
the USD and EUR implied curves. The chain is checked end to end on synthetic data: ISS FORTS
listing and history -> ``futures_eod`` -> ``md.futures_eod`` / ``ref.futures_contract`` ->
``futures_prices`` of the EOD market snapshot. The consumer's row selection (trading-desk-monitor
``MarketView.fx_futures``) is restated in :func:`consumer_rows`: ``is_fx``, ``base_ccy``,
``quote_ccy`` RUB, the business date, a RUB-per-unit settlement price, a delivery date, not
perpetual.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.futures  # noqa: F401 - registers the jobs
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.sources import moex_fx as fx
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
FIX = Path(__file__).parent / "fixtures" / "moex_fx"
HISTORY_COLUMNS = json.loads((FIX / "forts_history_p1.json").read_text("utf-8"))["history"][
    "columns"
]


def _history_row(secid: str, shortname: str, asset: str, settle: float) -> list[Any]:
    """One synthetic FORTS history row (the columns of the recorded answer)."""
    row: dict[str, Any] = {
        "BOARDID": "RFUD",
        "TRADEDATE": DAY.isoformat(),
        "SECID": secid,
        "OPEN": settle - 100,
        "LOW": settle - 200,
        "HIGH": settle + 300,
        "CLOSE": settle - 50,
        "OPENPOSITIONVALUE": 1.0e9,
        "VALUE": 1.0e8,
        "VOLUME": 1000,
        "OPENPOSITION": 5000,
        "SETTLEPRICE": settle,
        "SWAPRATE": 0.0,
        "WAPRICE": settle - 20,
        "CHANGE": 0.1,
        "QTY": 1,
        "NUMTRADES": 300,
        "SHORTNAME": shortname,
        "ASSETCODE": asset,
        "SWAPRATE_CURR": 0.0,
    }
    return [row[c] for c in HISTORY_COLUMNS]


def _history_page() -> dict[str, Any]:
    return {
        "history": {
            "columns": HISTORY_COLUMNS,
            "data": [
                _history_row("SiZ6", "Si-12.26", "Si", 85219.0),
                _history_row("SiH7", "Si-3.27", "Si", 86500.0),
                _history_row("EuZ6", "Eu-12.26", "Eu", 97390.0),
                # a contract the listing does not know (no description answer): minimal row, no FX
                _history_row("RBZ6", "RGBI-12.26", "RGBI", 11200.0),
            ],
        },
        "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 4, 100]]},
    }


def _http(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def consumer_rows(
    futures_prices: list[dict[str, Any]], ccy: str, day: date
) -> list[dict[str, Any]]:
    """The rows trading-desk-monitor's ``MarketView.fx_futures(ccy)`` takes, by delivery date."""
    rows = [
        r
        for r in futures_prices
        if r.get("is_fx")
        and r.get("base_ccy") == ccy
        and (r.get("quote_ccy") or "RUB") == "RUB"
        and r.get("trade_date") == day.isoformat()
        and r.get("settle_rub_per_unit")
        and r.get("last_del_date")
        and r.get("contract_type") != "perpetual"
    ]
    return sorted(rows, key=lambda r: r["last_del_date"])


@respx.mock
def test_usd_and_eur_futures_are_in_the_eod_snapshot(
    settings: Settings, db: Database, conn: Conn
) -> None:
    seed(conn)
    respx.get(ISS_BASE_URL + fx.RFUD_SECURITIES).mock(
        return_value=httpx.Response(
            200, json=json.loads((FIX / "rfud_securities.json").read_text())
        )
    )
    respx.get(ISS_BASE_URL + fx.FORTS_HISTORY).mock(
        return_value=httpx.Response(200, json=_history_page())
    )
    params = {"date_from": DAY.isoformat(), "date_to": DAY.isoformat(), "mode": "final"}
    out = run_job("futures_eod", params, settings=settings, db=db, http_factory=_http)
    assert out.status == "succeeded", out

    published = publish_market_snapshot(
        db,
        cut_type="eod_preliminary",
        business_date=DAY,
        cut_ts=datetime(2026, 10, 15, 19, 45, tzinfo=MSK),
        known_at=utc_now(),
    )
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (published.market.id,)
    ).fetchone()
    assert row is not None
    doc = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    prices = doc["data"]["futures_prices"]

    usd = consumer_rows(prices, "USD", DAY)
    assert [(r["secid"], r["last_del_date"]) for r in usd] == [
        ("SiZ6", "2026-12-17"),
        ("SiH7", "2027-03-18"),
    ]
    assert Decimal(usd[0]["settle_rub_per_unit"]) == Decimal("85.219")  # 85219 x 1 / 1 / 1000
    assert Decimal(usd[1]["settle_rub_per_unit"]) == Decimal("86.5")

    eur = consumer_rows(prices, "EUR", DAY)
    assert [(r["secid"], r["last_del_date"]) for r in eur] == [("EuZ6", "2026-12-17")]
    assert Decimal(eur[0]["settle_rub_per_unit"]) == Decimal("97.39")
    assert (eur[0]["asset_code"], eur[0]["quote_ccy"], eur[0]["contract_type"]) == (
        "Eu",
        "RUB",
        "dated",
    )
    assert eur[0]["labels"] == []  # a contract with static and a settlement price: no flag

    # the RGBI future is stored but is not an FX input
    assert not [r for r in prices if r["secid"] == "RBZ6" and r["is_fx"]]
    coverage = next(d for d in doc["datasets"] if d["name"] == "futures_prices")["coverage"]
    assert coverage["fx_contracts"] >= 3


@pytest.mark.parametrize("ccy", ["USD", "EUR"])
def test_consumer_rows_skip_perpetuals_and_crosses(ccy: str) -> None:
    """The perpetual proxy (``USDRUBF``) and the cross futures (``ED``: EUR in USD) are never an
    outright of the RUB implied curve."""
    base = {
        "is_fx": True,
        "trade_date": DAY.isoformat(),
        "settle_rub_per_unit": "1",
        "last_del_date": "2026-12-17",
        "contract_type": "dated",
        "quote_ccy": "RUB",
    }
    rows = [
        {**base, "secid": "A", "base_ccy": ccy},
        {
            **base,
            "secid": "PERP",
            "base_ccy": ccy,
            "contract_type": "perpetual",
            "last_del_date": None,
        },
        {**base, "secid": "CROSS", "base_ccy": ccy, "quote_ccy": "USD"},
        {**base, "secid": "NOPRICE", "base_ccy": ccy, "settle_rub_per_unit": None},
    ]
    assert [r["secid"] for r in consumer_rows(rows, ccy, DAY)] == ["A"]
