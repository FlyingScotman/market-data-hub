"""Market Snapshot Contract 1.4 (additive; package P6, TDM brief H10-H13, FX forwards H1-H4):
``fx_rates`` value dates and settlement calendars, dataset ``fx_swaps`` (MOEX swaps, formerly
excluded as ``fx_swap``), ``futures_prices`` / ``futures_prices_intraday`` (FORTS), ``cpi``
(monthly CPI history), ``fixings`` coverage ``ruonia_unpublished_days`` and the foreign
settlement calendars ``CN_INTERBANK``, ``US_FED``, ``CH_ZURICH`` in the static snapshot."""

from __future__ import annotations

import copy
import gzip
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.core.iss import IssFormatError
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.loaders.fx_moex import refresh_fx_instruments
from mdhub.snapshot.market import settlement_calendars
from mdhub.snapshot.runner import publish_market_snapshot, publish_static_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION
from mdhub.sources.moex_fx import parse_fx_settle_dates
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
FIX = Path(__file__).parent / "fixtures" / "moex_fx"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def test_version() -> None:
    assert SCHEMA_VERSION == "market_snapshot/1.4"


def test_settlement_calendar_mapping() -> None:
    assert settlement_calendars("CNY") == (["MOEX_FX", "CN_INTERBANK"], True)
    assert settlement_calendars("USD") == (["MOEX_FX", "US_FED"], True)
    assert settlement_calendars("EUR") == (["MOEX_FX", "TARGET"], True)
    assert settlement_calendars("CHF") == (["MOEX_FX", "CH_ZURICH"], True)
    assert settlement_calendars("KZT") == (["MOEX_FX"], False)


# ------------------------------------------------------------------- H2: value dates


def test_listing_settle_dates_need_the_session() -> None:
    board = _load("cets_board.json")  # CETS board of the 2026-10-16 session
    session, rows = parse_fx_settle_dates(board)
    assert session == date(2026, 10, 16)
    by = {(r["secid"], r["board"]): r["settle_date"] for r in rows}
    assert by[("CNY000000TOD", "CETS")] == date(2026, 10, 16)
    assert by[("CNYRUB_TOM", "CETS")] == date(2026, 10, 19)
    assert by[("CNYRUBTODTOM", "CETS")] == date(2026, 10, 16)  # swaps: the near leg
    assert by[("CNYRUB_TOM1W", "CETS")] == date(2026, 10, 19)
    # a listing without dataversion (the selt fixture) is never dated by a guess
    assert parse_fx_settle_dates(_load("selt_securities.json")) == (None, [])
    bad = copy.deepcopy(board)
    cols = bad["securities"]["columns"]
    bad["securities"]["data"][0][cols.index("SETTLEDATE")] = "2026-10-15"
    with pytest.raises(IssFormatError, match="before the session"):
        parse_fx_settle_dates(bad)


def test_refresh_stores_settle_dates_per_session(conn: Conn) -> None:
    board = _load("cets_board.json")
    kw: Any = {"capture_id": None, "run_id": None, "today": date(2026, 10, 16)}
    refresh_fx_instruments(conn, board, **kw)
    again = refresh_fx_instruments(conn, board, **kw)
    assert again.inserted == 0 and again.updated == 0  # idempotent
    conn.commit()
    rows = conn.execute(
        "SELECT trade_date, secid, settle_date FROM md.fx_settle_date "
        "WHERE secid IN ('CNYRUB_TOM', 'CNYRUB_TOM1D') ORDER BY secid"
    ).fetchall()
    session, monday = date(2026, 10, 16), date(2026, 10, 19)
    assert rows == [
        {"trade_date": session, "secid": "CNYRUB_TOM", "settle_date": monday},
        {"trade_date": session, "secid": "CNYRUB_TOM1D", "settle_date": monday},
    ]


# ------------------------------------------------------------------- market datasets

_EXTRA = """
INSERT INTO ref.fx_instrument (secid, board, base_ccy, quote_ccy, kind, tenor, near_leg,
    far_leg, face_value, first_seen) VALUES
  ('CNY000000TOD', 'CETS', 'CNY', 'RUB', 'outright', 'TOD', NULL, NULL, 1, '2026-01-01'),
  ('CNYRUBTODTOM', 'CETS', 'CNY', 'RUB', 'swap', 'TOD/TOM', 'TOD', 'TOM', 1, '2026-01-01'),
  ('CNYRUB_TOM1W', 'CETS', 'CNY', 'RUB', 'swap', 'TOM/1W', 'TOM', '1W', 1, '2026-01-01'),
  ('CHFRUBTODTOM', 'CETS', 'CHF', 'RUB', 'swap', 'TOD/TOM', 'TOD', 'TOM', 1, '2026-01-01'),
  ('KZTRUB_TOM1W', 'CETS', 'KZT', 'RUB', 'swap', 'TOM/1W', 'TOM', '1W', 100, '2026-01-01'),
  ('GLDRUBTODTOM', 'CETS', 'GLD', 'RUB', 'swap', 'TOD/TOM', 'TOD', 'TOM', 1, '2026-01-01'),
  ('USDCNYTODTOM', 'CETS', 'USD', 'CNY', 'swap', 'TOD/TOM', 'TOD', 'TOM', 1, '2026-01-01');
UPDATE ref.fx_instrument SET face_value = 1 WHERE secid = 'CNYRUB_TOM';
INSERT INTO md.fx_eod (trade_date, secid, board, close, waprice, num_trades) VALUES
  ('2026-10-15', 'CNY000000TOD', 'CETS', 11.3300, 11.3310, 900),
  ('2026-10-15', 'CNYRUBTODTOM', 'CETS', 0.00472, 0.00471, 800),
  ('2026-10-15', 'CNYRUB_TOM1W', 'CETS', 0.0335, 0.0334, 12),
  ('2026-10-15', 'CHFRUBTODTOM', 'CETS', 0.0291, 0.0290, 3),
  ('2026-10-15', 'KZTRUB_TOM1W', 'CETS', 0.0012, NULL, 1),
  ('2026-10-15', 'GLDRUBTODTOM', 'CETS', 0.31, 0.31, 2),
  ('2026-10-15', 'USDCNYTODTOM', 'CETS', NULL, NULL, 0);
INSERT INTO md.fx_settle_date (trade_date, secid, board, settle_date) VALUES
  ('2026-10-15', 'CNY000000TOD', 'CETS', '2026-10-15'),
  ('2026-10-15', 'CNYRUB_TOM', 'CETS', '2026-10-16'),
  ('2026-10-15', 'CNYRUBTODTOM', 'CETS', '2026-10-15'),
  ('2026-10-15', 'CNYRUB_TOM1W', 'CETS', '2026-10-16'),
  ('2026-10-14', 'CHFRUBTODTOM', 'CETS', '2026-10-14');
INSERT INTO md.futures_intraday (ts, secid, source, source_variant, trade_date, bid, offer,
    last, settle_price, latency, source_ts) VALUES
  ('2026-10-15 10:00Z', 'SiZ6', 'moex_apim', 'apim_marketdata', '2026-10-15', 82400, 82410,
   82405, 82406, 'realtime', '2026-10-15 10:00Z'),
  ('2026-10-15 11:00Z', 'SiZ6', 'moex_iss', 'iss_public', '2026-10-15', 82500, 82510, 82505,
   82506, 'delayed', '2026-10-15 11:15Z'),
  ('2026-10-14 11:00Z', 'USDRUBF', 'moex_iss', 'iss_public', '2026-10-14', 81, 82, 81.5, NULL,
   'delayed', '2026-10-14 11:15Z');
INSERT INTO md.fixing (index_code, fixing_date, source, value, unit, published_on, details)
VALUES
  ('CPI_RU_MOM', '2015-01-01', 'rosstat', 103.85, 'index', NULL, '{"period": "2015-01"}'),
  ('CPI_RU_MOM', '2026-09-01', 'rosstat', 100.31, 'index', '2026-10-09', '{"period": "2026-09"}'),
  ('CPI_RU_MOM', '2014-12-01', 'rosstat', 102.62, 'index', NULL, '{"period": "2014-12"}'),
  ('CPI_RU_YOY', '2026-09-01', 'cbr', 6.35, 'pct', NULL, '{"period": "2026-09"}'),
  ('RUONIA', '2026-10-09', 'cbr', 16.25, 'pct', '2026-10-12', NULL),
  ('RUONIA', '2026-10-12', 'cbr', 16.28, 'pct', '2026-10-13', NULL);
INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) VALUES
  ('RU_PRODUCTION', '2026-10-10', 'curated', true, '2026-01-01Z');
"""


@pytest.fixture
def market(conn: Conn, db: Database) -> tuple[dict[str, Any], dict[str, Any]]:
    seed(conn)
    conn.execute(_EXTRA)  # type: ignore[arg-type]
    conn.commit()
    eod = publish_market_snapshot(
        db,
        cut_type="eod_preliminary",
        business_date=DAY,
        cut_ts=datetime(2026, 10, 15, 19, 45, tzinfo=MSK),
        known_at=utc_now(),
    )
    intraday = publish_market_snapshot(
        db,
        cut_type="intraday",
        business_date=DAY,
        cut_ts=datetime(2026, 10, 15, 15, 0, tzinfo=MSK),
        known_at=utc_now(),
    )
    return _bundle(conn, eod.market.id), _bundle(conn, intraday.market.id)


def _ds(doc: dict[str, Any], name: str) -> dict[str, Any]:
    entry: dict[str, Any] = next(d for d in doc["datasets"] if d["name"] == name)
    return entry


def test_fx_rates_value_dates(market: tuple[dict[str, Any], dict[str, Any]]) -> None:
    eod, _ = market
    assert eod["schema_version"] == "market_snapshot/1.4"
    moex = {r["secid"]: r for r in eod["data"]["fx_rates"] if r["kind"] == "moex_eod"}
    assert moex["CNY000000TOD"]["settle_date"] == "2026-10-15"
    assert moex["CNYRUB_TOM"]["settle_date"] == "2026-10-16"
    assert moex["CNYRUB_TOM"]["settlement_calendars"] == ["MOEX_FX", "CN_INTERBANK"]
    assert "settle_date_not_observed" not in moex["CNYRUB_TOM"]["labels"]
    others = [r for r in eod["data"]["fx_rates"] if r["kind"] != "moex_eod"]
    assert others and all(r["settle_date"] is None for r in others)
    assert all(r["settlement_calendars"] is None for r in others)
    assert _ds(eod, "fx_rates")["coverage"]["moex_eod_with_settle_date"] == 2


def test_fx_swaps_dataset(market: tuple[dict[str, Any], dict[str, Any]]) -> None:
    eod, intraday = market
    swaps = {r["secid"]: r for r in eod["data"]["fx_swaps"]}
    assert set(swaps) == {"CNYRUBTODTOM", "CNYRUB_TOM1W", "CHFRUBTODTOM", "KZTRUB_TOM1W"}
    todtom = swaps["CNYRUBTODTOM"]
    assert (todtom["near_settle_date"], todtom["far_settle_date"]) == ("2026-10-15", "2026-10-16")
    assert (todtom["close"], todtom["close_rub_per_unit"]) == ("0.00472", "0.00472")
    assert todtom["labels"] == []
    tom1w = swaps["CNYRUB_TOM1W"]
    assert tom1w["near_settle_date"] == "2026-10-16" and tom1w["far_settle_date"] is None
    assert tom1w["labels"] == ["far_settle_date_not_published"]
    chf = swaps["CHFRUBTODTOM"]  # value dates of another session are not used
    assert chf["near_settle_date"] is None and chf["settlement_calendars"][-1] == "CH_ZURICH"
    assert set(chf["labels"]) == {"near_settle_date_not_observed", "far_settle_date_not_observed"}
    kzt = swaps["KZTRUB_TOM1W"]
    assert kzt["close_rub_per_unit"] == "0.000012"  # 0.0012 RUB per 100 KZT
    assert "foreign_calendar_unknown" in kzt["labels"]
    excluded = {(e["dataset"], e["key"].get("secid"), e["reason"]) for e in eod["excluded"]}
    assert ("fx_swaps", "GLDRUBTODTOM", "precious_metal") in excluded
    assert ("fx_swaps", "USDCNYTODTOM", "non_rub_pair") in excluded
    assert not any(e["reason"] == "fx_swap" for e in eod["excluded"])
    assert intraday["data"]["fx_swaps"] == []
    assert _ds(intraday, "fx_swaps")["coverage"] == {"not_in_cut": "intraday"}


def test_futures_datasets(market: tuple[dict[str, Any], dict[str, Any]]) -> None:
    eod, intraday = market
    fut = {r["secid"]: r for r in eod["data"]["futures_prices"]}
    assert set(fut) == {"SiZ6", "USDRUBF", "BRZ6"}
    si = fut["SiZ6"]
    assert (si["settle_price"], si["settle_rub_per_unit"]) == ("82450", "82.45")
    assert (si["is_fx"], si["base_ccy"], si["last_trade_date"]) == (True, "USD", "2026-12-17")
    assert "perpetual" in fut["USDRUBF"]["labels"] and fut["BRZ6"]["is_fx"] is False
    assert _ds(eod, "futures_prices")["coverage"]["fx_contracts"] == 2
    assert eod["data"]["futures_prices_intraday"] == []
    assert intraday["data"]["futures_prices"] == []
    live = intraday["data"]["futures_prices_intraday"]
    assert [(r["secid"], r["source"], r["settle_price"]) for r in live] == [
        ("SiZ6", "moex_iss", "82506")  # the latest quote of the day, any source
    ]
    assert live[0]["contract_type"] == "dated" and live[0]["latency"] == "delayed"


def test_cpi_and_ruonia_unpublished(market: tuple[dict[str, Any], dict[str, Any]]) -> None:
    eod, intraday = market
    cpi = eod["data"]["cpi"]
    assert [(r["index_code"], r["fixing_date"]) for r in cpi] == [
        ("CPI_RU_MOM", "2015-01-01"),
        ("CPI_RU_MOM", "2026-09-01"),
        ("CPI_RU_YOY", "2026-09-01"),
    ]  # from cpi_from (2015-01) on, whatever the fixing window
    assert cpi[1]["published_on"] == "2026-10-09" and cpi[1]["labels"] == []
    assert cpi[0]["labels"] == ["published_on_unknown"]
    assert eod["parameters"]["cpi_from"] == "2015-01-01"
    assert _ds(eod, "cpi")["coverage"]["latest_month"]["CPI_RU_MOM"] == "2026-09-01"
    assert intraday["data"]["cpi"] == cpi
    # RUONIA 09.10, 12.10, 13.10, 14.10 published; 10.10 (a curated working Saturday here) not
    assert _ds(eod, "fixings")["coverage"]["ruonia_unpublished_days"] == ["2026-10-10"]


def test_static_snapshot_has_the_settlement_calendars(conn: Conn, db: Database) -> None:
    seed(conn)
    conn.execute(
        "INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) VALUES"
        " ('US_FED', '2026-10-12', 'rule_us_fed', false, '2026-01-01Z'),"
        " ('CN_INTERBANK', '2026-10-10', 'curated', true, '2026-01-01Z')"
    )
    conn.commit()
    out = publish_static_snapshot(
        db,
        cut_type="daily",
        business_date=DAY,
        cut_ts=datetime(2026, 10, 15, 7, 45, tzinfo=MSK),
        known_at=utc_now(),
    )
    doc = _bundle(conn, out.id)
    cals = {c["calendar"]: c for c in doc["data"]["calendars"]}
    assert cals["US_FED"]["holidays"] == ["2026-10-12"]
    assert cals["CN_INTERBANK"]["working_weekends"] == ["2026-10-10"]
    missing = _ds(doc, "calendars")["coverage"]["missing"]
    assert "CH_ZURICH" in missing and "US_FED" not in missing
