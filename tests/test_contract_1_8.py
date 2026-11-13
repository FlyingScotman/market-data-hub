"""Market Snapshot Contract 1.8 (additive; market-data-hub#154): ``eod_preliminary`` carries
``futures_prices_intraday``.

At the preliminary cut (19:45 MSK) ISS has not published the FORTS history of the day, so
``futures_prices`` is empty and a consumer cannot build an FX-implied curve. The cut now holds,
per contract, the latest intraday quote on the business date with quote time and ``source_ts``
at or before ``cut_ts`` (``md.futures_intraday``), labelled by the dataset's coverage
(``basis = intraday_last_at_cut``, ``latest_source_ts``). ``eod_final`` keeps
``not_in_cut: eod`` (its ``futures_prices`` is the ISS history). The chain is run end to end on
the recorded APIM answer: ``futures_intraday`` loader -> ``md.futures_intraday`` -> snapshot
build and publish; there is deliberately no ``futures_eod`` run (no ISS history of the day).
"""

from __future__ import annotations

import copy
import gzip
import json
from collections.abc import Callable
from datetime import date, datetime
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
from mdhub.core.iss import APIM_BASE_URL
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.snapshot.market import BASIS_INTRADAY_LAST
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION, validate_bundle
from mdhub.sources import moex_fx as fx
from test_api_seed import seed
from test_futures import load, seed_ref

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 16)  # the day of the recorded APIM answer
CUT = datetime(2026, 10, 16, 19, 45, tzinfo=MSK)
APIM_FORTS = APIM_BASE_URL + fx.FORTS_SECURITIES
FIX = Path(__file__).parent / "fixtures" / "moex_fx"


def test_version() -> None:
    assert SCHEMA_VERSION.startswith("market_snapshot/1.")  # the 1.8 rules hold in later minors
    assert int(SCHEMA_VERSION.rsplit(".", 1)[1]) >= 8


@pytest.fixture(autouse=True)
def _recorded_today(today_is: Callable[[date], None]) -> None:
    today_is(DAY)


@pytest.fixture
def apim_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        iss_rps=1000.0,
        moex_api_key="test-apim-key",  # type: ignore[arg-type]
        futures_rates_assets="",
    )


def _http(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def _poll(payload: dict[str, Any], settings: Settings, db: Database) -> None:
    out = run_job("futures_intraday", {}, settings=settings, db=db, http_factory=_http)
    assert out.status == "succeeded", out


def _requote(
    base: dict[str, Any], secid: str, *, bid: float, updated: str, sysdate_time: str
) -> dict[str, Any]:
    """The recorded answer with one contract requoted: ``updated`` = UPDATETIME,
    ``sysdate_time`` = the SYSTIME of the poll (``HH:MM:SS`` on the day)."""
    doc = copy.deepcopy(base)
    cols = doc["marketdata"]["columns"]
    for row in doc["marketdata"]["data"]:
        if row[cols.index("SECID")] == secid:
            row[cols.index("BID")] = bid
            row[cols.index("UPDATETIME")] = updated
            row[cols.index("SYSTIME")] = f"{DAY.isoformat()} {sysdate_time}"
    return doc


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def _entry(doc: dict[str, Any], name: str) -> dict[str, Any]:
    entry: dict[str, Any] = next(d for d in doc["datasets"] if d["name"] == name)
    return entry


def _publish(db: Database, cut_type: str, cut_ts: datetime = CUT) -> Any:
    return publish_market_snapshot(
        db, cut_type=cut_type, business_date=DAY, cut_ts=cut_ts, known_at=utc_now()
    )


@respx.mock
def test_preliminary_cut_carries_the_last_intraday_quotes_before_the_cut(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    seed(conn)
    seed_ref(conn)
    recorded = load("forts_md_apim.json")  # every contract quoted at 10:50 MSK
    answer: dict[str, Any] = {}
    respx.get(APIM_FORTS).mock(side_effect=lambda _r: httpx.Response(200, json=answer))
    for doc in (
        recorded,
        # SiZ6 requoted at 19:30 (before the cut): the last quote at the cut
        _requote(recorded, "SiZ6", bid=85100, updated="19:30:00", sysdate_time="19:30:01"),
        # quote time before the cut, SYSTIME after it: not known at the cut
        _requote(recorded, "SiZ6", bid=85200, updated="19:44:59", sysdate_time="19:45:30"),
        # after the cut
        _requote(recorded, "SiZ6", bid=85300, updated="20:10:00", sysdate_time="20:10:01"),
    ):
        answer.clear()
        answer.update(doc)
        _poll(doc, apim_settings, db)
    conn.commit()
    rows = conn.execute(
        "SELECT secid, ts, source_ts FROM md.futures_intraday WHERE secid = 'SiZ6' ORDER BY ts"
    ).fetchall()
    assert len(rows) == 4  # the loader stored all four quotes: only the cut bound hides two

    prelim = _publish(db, "eod_preliminary")
    doc = _bundle(conn, prelim.market.id)
    validate_bundle(doc)
    assert doc["schema_version"] == SCHEMA_VERSION and doc["cut_type"] == "eod_preliminary"

    # no ISS history of the day: the EOD dataset is empty and says so
    assert doc["data"]["futures_prices"] == []
    assert _entry(doc, "futures_prices")["stale_reasons"] == [
        f"no FORTS futures history on {DAY.isoformat()}"
    ]

    live = {r["secid"]: r for r in doc["data"]["futures_prices_intraday"]}
    assert set(live) == {"CNYRUBF", "CRZ6", "EDZ6", "SiZ6", "UCZ6", "USDRUBF"}
    si = live["SiZ6"]
    assert si["bid"] == "85100"  # the 19:30 quote, not the 19:44:59 / 20:10 ones
    assert (si["ts"], si["source_ts"]) == ("2026-10-16T16:30:00Z", "2026-10-16T16:30:01Z")
    assert live["CRZ6"]["ts"] == "2026-10-16T07:50:14Z"  # untouched: the 10:50 MSK quote
    assert all(r["ts"] <= "2026-10-16T16:45:00Z" for r in live.values())
    assert all(r["source_ts"] <= "2026-10-16T16:45:00Z" for r in live.values())

    # the row shape a consumer reads is that of the intraday cut (contract 1.5)
    assert (si["secid"], si["asset_code"], si["is_fx"], si["base_ccy"], si["quote_ccy"]) == (
        "SiZ6",
        "Si",
        True,
        "USD",
        "RUB",
    )
    assert si["last_del_date"] is not None and si["contract_type"] == "dated"
    assert si["bid_rub_per_unit"] is not None and si["last_rub_per_unit"] is not None
    assert {"open_position", "vol_today", "trade_date", "source_ts", "latency"} <= set(si)
    intraday = _bundle(
        conn, _publish(db, "intraday", datetime(2026, 10, 16, 20, 30, tzinfo=MSK)).market.id
    )
    assert {r["secid"]: set(r) for r in intraday["data"]["futures_prices_intraday"]} == {
        k: set(v) for k, v in live.items()
    }

    # the coverage labels the rows and states the source time; the dataset is not "stale"
    entry = _entry(doc, "futures_prices_intraday")
    assert entry["rows"] == 6 and entry["stale"] is False
    assert entry["coverage"] == {
        "basis": BASIS_INTRADAY_LAST,
        "contracts": 6,
        "latest_ts": "2026-10-16T16:30:00Z",
        "latest_source_ts": "2026-10-16T16:30:01Z",
    }
    # an intraday cut is unchanged: no basis, the latest quote known at known_at
    assert _entry(intraday, "futures_prices_intraday")["coverage"] == {
        "contracts": 6,
        "latest_ts": "2026-10-16T17:10:00Z",
    }
    assert {r["secid"]: r["bid"] for r in intraday["data"]["futures_prices_intraday"]}[
        "SiZ6"
    ] == "85300"

    # a rebuild of the cut later (known_at after the evening quotes) is the same cut
    again = _publish(db, "eod_preliminary")
    assert again.market.id == prelim.market.id and again.market.status == "unchanged"

    # eod_final is as before: its futures come from the ISS history
    final = _bundle(
        conn, _publish(db, "eod_final", datetime(2026, 10, 19, 8, 0, tzinfo=MSK)).market.id
    )
    assert final["data"]["futures_prices_intraday"] == []
    assert _entry(final, "futures_prices_intraday")["coverage"] == {"not_in_cut": "eod"}


@respx.mock
def test_preliminary_cut_without_quotes_before_the_cut_says_so(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    """Only a quote after the cut exists: the dataset is empty and stale, never filled with it."""
    seed(conn)
    seed_ref(conn)
    late = copy.deepcopy(load("forts_md_apim.json"))
    cols = late["marketdata"]["columns"]
    for row in late["marketdata"]["data"]:
        row[cols.index("UPDATETIME")] = "20:10:00"
        row[cols.index("SYSTIME")] = f"{DAY.isoformat()} 20:10:01"
    respx.get(APIM_FORTS).mock(return_value=httpx.Response(200, json=late))
    _poll(late, apim_settings, db)
    conn.commit()

    doc = _bundle(conn, _publish(db, "eod_preliminary").market.id)
    validate_bundle(doc)
    assert doc["data"]["futures_prices_intraday"] == []
    entry = _entry(doc, "futures_prices_intraday")
    assert entry["stale"] is True
    assert entry["stale_reasons"] == [
        f"no intraday FORTS futures quotes on {DAY.isoformat()} at or before the cut"
    ]
    assert entry["coverage"] == {
        "basis": BASIS_INTRADAY_LAST,
        "contracts": 0,
        "latest_ts": None,
        "latest_source_ts": None,
    }
