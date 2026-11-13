"""Synthetic spot/futures observations and the four-contract matching boundary."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mdhub.sources import fx_pair_inputs as fx


def block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = list(rows[0]) if rows else []
    return {"columns": columns, "data": [[row.get(column) for column in columns] for row in rows]}


def cny_payload() -> dict[str, Any]:
    return {
        "dataversion": block([{"trade_date": "2026-01-05"}]),
        "securities": block(
            [{"SECID": "CNYRUB_TOM", "BOARDID": "CETS", "SETTLEDATE": "2026-01-06"}]
        ),
        "marketdata": block(
            [
                {
                    "SECID": "CNYRUB_TOM",
                    "BOARDID": "CETS",
                    "BID": None,
                    "OFFER": None,
                    "LAST": Decimal("10.01"),
                    "WAPRICE": None,
                    "OPEN": None,
                    "HIGH": None,
                    "LOW": None,
                    "NUMTRADES": 10,
                    "VOLTODAY": None,
                    "VALTODAY": None,
                    "TRADINGSTATUS": "T",
                    "TIME": "10:45:00",
                    "UPDATETIME": "10:45:01",
                    "SYSTIME": "2026-01-05 11:00:00",
                }
            ]
        ),
    }


def test_cny_last_trade_clock_is_not_the_response_clock() -> None:
    spot = fx.parse_cny_spot(cny_payload())
    assert spot.quote_ts == datetime(2026, 1, 5, 7, 45, tzinfo=UTC)
    assert spot.update_ts == datetime(2026, 1, 5, 7, 45, 1, tzinfo=UTC)
    assert spot.source_ts == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    assert spot.last == Decimal("10.01") and spot.bid is None and spot.offer is None
    assert spot.settle_date == date(2026, 1, 6)


def test_cny_missing_trade_time_does_not_invent_a_clock() -> None:
    payload = cny_payload()
    payload["marketdata"]["data"][0][payload["marketdata"]["columns"].index("TIME")] = None
    spot = fx.parse_cny_spot(payload)
    assert spot.quote_ts is None
    assert "spot_time_unknown" in spot.reasons


def test_cny_trade_date_must_match_the_market_session() -> None:
    payload = cny_payload()
    payload["dataversion"]["data"][0][0] = "2026-01-02"
    spot = fx.parse_cny_spot(payload)
    assert spot.quote_ts is None
    assert "spot_time_unknown" in spot.reasons
    assert "spot_session_clock_mismatch" in spot.reasons


def test_cny_missing_update_clock_keeps_last_and_independent_source_clock() -> None:
    payload = cny_payload()
    market = payload["marketdata"]
    market["data"][0][market["columns"].index("UPDATETIME")] = None
    spot = fx.parse_cny_spot(payload)
    assert spot.last == Decimal("10.01")
    assert spot.source_ts == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    assert spot.trade_time.isoformat() == "10:45:00"
    assert spot.quote_ts is None
    assert "spot_update_clock_unknown" in spot.reasons


def test_cny_missing_source_clock_keeps_update_time_without_guessing_date() -> None:
    payload = cny_payload()
    market = payload["marketdata"]
    market["data"][0][market["columns"].index("SYSTIME")] = None
    spot = fx.parse_cny_spot(payload)
    assert spot.last == Decimal("10.01")
    assert spot.update_time.isoformat() == "10:45:01"
    assert spot.update_ts is None
    assert spot.quote_ts is None
    assert "spot_source_clock_unknown" in spot.reasons


def test_cny_bad_settlement_keeps_independent_price_and_clocks() -> None:
    payload = cny_payload()
    securities = payload["securities"]
    securities["data"][0][securities["columns"].index("SETTLEDATE")] = "2026-01-04"
    spot = fx.parse_cny_spot(payload)
    assert spot.last == Decimal("10.01")
    assert spot.source_ts == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    assert spot.update_time.isoformat() == "10:45:01"
    assert spot.settle_date is None
    assert spot.quote_ts is None
    assert "spot_session_or_settlement_invalid" in spot.reasons


def test_cny_reversed_source_clocks_do_not_produce_an_eligible_time() -> None:
    payload = cny_payload()
    market = payload["marketdata"]
    market["data"][0][market["columns"].index("UPDATETIME")] = "11:00:01"
    spot = fx.parse_cny_spot(payload)
    assert spot.last == Decimal("10.01")
    assert spot.quote_ts is None
    assert "spot_clock_order_unknown" in spot.reasons


def catalog() -> list[dict[str, Any]]:
    dates = [
        date(2026, 3, 19),
        date(2026, 6, 18),
        date(2026, 9, 17),
        date(2026, 12, 17),
        date(2027, 3, 18),
    ]
    result = []
    for secid, expiry in zip(("SiH6", "SiM6", "SiU6", "SiZ6", "SiH7"), dates, strict=True):
        result.append(
            {
                "secid": secid,
                "asset_code": "Si",
                "base_ccy": "USD",
                "quote_ccy": "RUB",
                "is_fx": True,
                "contract_type": "dated",
                "first_trade_date": date(2025, 1, 1),
                "last_trade_date": expiry,
                "last_del_date": expiry,
            }
        )
    result.append({**result[0], "secid": "UMH6", "asset_code": "USDM"})
    result.append({**result[0], "secid": "USDRUBF", "contract_type": "perpetual"})
    return result


def futures_payload(
    contracts: list[dict[str, Any]],
    *,
    omit: set[str] | None = None,
    update: str = "10:59:59",
    systime: str = "2026-01-05 11:00:00",
) -> dict[str, Any]:
    omitted = omit or set()
    specs, market = [], []
    for contract in contracts:
        secid = contract["secid"]
        if (
            secid in omitted
            or contract["asset_code"] != "Si"
            or contract["contract_type"] != "dated"
        ):
            continue
        expiry = contract["last_trade_date"].isoformat()
        specs.append(
            {
                "SECID": secid,
                "BOARDID": "RFUD",
                "SHORTNAME": secid,
                "SECNAME": secid,
                "LATNAME": secid,
                "MINSTEP": 1,
                "LASTTRADEDATE": expiry,
                "LASTDELDATE": expiry,
                "SECTYPE": "Si",
                "ASSETCODE": "Si",
                "LOTVOLUME": 1000,
                "STEPPRICE": 1,
                "DECIMALS": 0,
                "FRSTTRADE": "2025-01-01",
            }
        )
        market.append(
            {
                "SECID": secid,
                "BID": 10100,
                "OFFER": 10120,
                "LAST": None,
                "OPEN": None,
                "HIGH": None,
                "LOW": None,
                "SETTLEPRICE": None,
                "OPENPOSITION": None,
                "NUMTRADES": 1,
                "VOLTODAY": None,
                "VALTODAY": None,
                "UPDATETIME": update,
                "SYSTIME": systime,
                "TRADEDATE": "2026-01-05",
                "SWAPRATE": None,
            }
        )
    return {"securities": block(specs), "marketdata": block(market)}


def test_missing_fourth_is_not_replaced_by_fifth() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    rows = catalog()
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        rows,
        futures_payload(rows, omit={"SiZ6"}),
        captured_at=spot_ts - timedelta(seconds=2),
        capture_sha256="a" * 64,
    )
    assert [node["secid"] for node in nodes] == ["SiH6", "SiM6", "SiU6", "SiZ6"]
    assert nodes[0]["bid_rub_per_unit"] == Decimal("10.1")
    assert nodes[3]["bid_rub_per_unit"] is None
    assert "future_contract_missing" in nodes[3]["reasons"]


def test_later_capture_and_future_row_clocks_never_supply_a_price() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    rows = catalog()
    late = fx.match_futures(
        "USD",
        spot_ts,
        rows,
        futures_payload(rows),
        captured_at=spot_ts + timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert all(node["bid_rub_per_unit"] is None for node in late)
    assert "future_capture_after_spot" in late[0]["reasons"]
    future = fx.match_futures(
        "USD",
        spot_ts,
        rows,
        futures_payload(rows, update="11:00:04", systime="2026-01-05 11:00:04"),
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="b" * 64,
    )
    assert future[0]["bid_rub_per_unit"] is None
    assert "future_clock_after_spot" in future[0]["reasons"]


def test_repeat_observation_keeps_its_own_individual_clocks() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    rows = catalog()
    first = fx.match_futures(
        "USD",
        spot_ts,
        rows,
        futures_payload(rows, update="10:59:01", systime="2026-01-05 10:59:02"),
        captured_at=spot_ts - timedelta(seconds=30),
        capture_sha256="a" * 64,
    )
    repeated = fx.match_futures(
        "USD",
        spot_ts,
        rows,
        futures_payload(rows),
        captured_at=spot_ts - timedelta(seconds=2),
        capture_sha256="b" * 64,
    )
    assert first[0]["bid_rub_per_unit"] == repeated[0]["bid_rub_per_unit"]
    assert repeated[0]["update_ts"] > first[0]["update_ts"]
    assert repeated[0]["source_ts"] != repeated[0]["captured_at"]
    assert repeated[0]["capture_sha256"] == "b" * 64


def test_individual_source_clock_is_not_the_fresh_batch_maximum() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    payload = futures_payload(catalog())
    market = payload["marketdata"]
    market["data"][0][market["columns"].index("UPDATETIME")] = "10:49:59"
    market["data"][0][market["columns"].index("SYSTIME")] = "2026-01-05 10:50:00"
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        catalog(),
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["source_skew_seconds"] == Decimal(603)
    assert nodes[1]["source_skew_seconds"] == Decimal(3)


def test_missing_reversed_clocks_and_crossed_quotes_are_explicit() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    payload = futures_payload(catalog())
    market = payload["marketdata"]
    cols = market["columns"]
    market["data"][0][cols.index("UPDATETIME")] = None
    market["data"][1][cols.index("UPDATETIME")] = "11:00:02"
    market["data"][1][cols.index("SYSTIME")] = "2026-01-05 10:59:59"
    market["data"][2][cols.index("BID")] = 10200
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        catalog(),
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["reasons"] == ["future_clock_unknown"]
    assert nodes[0]["source_ts"] == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    assert nodes[0]["source_skew_seconds"] == Decimal(3)
    assert nodes[1]["reasons"] == ["future_clock_order_unknown"]
    assert nodes[2]["reasons"] == ["crossed_future"]
    assert all(node["bid_rub_per_unit"] is None for node in nodes[:3])


def test_update_time_without_source_date_is_preserved_without_a_guessed_date() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    payload = futures_payload(catalog())
    market = payload["marketdata"]
    market["data"][0][market["columns"].index("SYSTIME")] = None
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        catalog(),
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["reasons"] == ["future_clock_unknown"]
    assert nodes[0]["update_time"].isoformat() == "10:59:59"
    assert nodes[0]["update_ts"] is None
    assert nodes[0]["source_ts"] is None
    assert nodes[0]["bid_rub_per_unit"] is None


def test_invalid_clock_does_not_erase_the_other_clock() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    payload = futures_payload(catalog())
    market = payload["marketdata"]
    columns = market["columns"]
    market["data"][0][columns.index("UPDATETIME")] = "bad"
    market["data"][1][columns.index("SYSTIME")] = "bad"
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        catalog(),
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["reasons"] == ["future_clock_invalid"]
    assert nodes[0]["source_ts"] == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    assert nodes[1]["reasons"] == ["future_clock_invalid"]
    assert nodes[1]["update_time"].isoformat() == "10:59:59"
    assert nodes[1]["update_ts"] is None


def test_missing_contract_spec_keeps_available_future_clocks() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    payload = futures_payload(catalog())
    payload["securities"]["data"].pop(0)
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        catalog(),
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["reasons"] == ["future_contract_missing"]
    assert nodes[0]["source_ts"] == datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    assert nodes[0]["source_skew_seconds"] == Decimal(3)
    assert nodes[0]["bid_rub_per_unit"] is None


def test_non_terminating_normalization_is_not_silently_rounded() -> None:
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    payload = futures_payload(catalog())
    specs = payload["securities"]
    specs["data"][0][specs["columns"].index("MINSTEP")] = 3
    nodes = fx.match_futures(
        "USD",
        spot_ts,
        catalog(),
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["reasons"] == ["future_normalization_inexact"]
    assert nodes[0]["bid_rub_per_unit"] is None


@pytest.mark.parametrize(
    ("currency", "asset", "prefix", "min_step", "bid", "offer", "expected"),
    [
        ("EUR", "Eu", "Eu", "1", "10500", "10510", Decimal("10.5")),
        ("CNY", "CNY", "CR", "0.001", "12.30", "12.31", Decimal("12.30")),
    ],
)
def test_currency_specific_contract_normalization(
    currency: str, asset: str, prefix: str, min_step: str, bid: str, offer: str, expected: Decimal
) -> None:
    original = catalog()[:5]
    payload = futures_payload(original)
    contracts = []
    for contract in original:
        secid = prefix + contract["secid"][2:]
        contracts.append({**contract, "secid": secid, "asset_code": asset, "base_ccy": currency})
    specs = payload["securities"]
    quotes = payload["marketdata"]
    for spec, quote in zip(specs["data"], quotes["data"], strict=True):
        spec[specs["columns"].index("SECID")] = prefix + spec[0][2:]
        spec[specs["columns"].index("ASSETCODE")] = asset
        spec[specs["columns"].index("MINSTEP")] = min_step
        quote[quotes["columns"].index("SECID")] = prefix + quote[0][2:]
        quote[quotes["columns"].index("BID")] = bid
        quote[quotes["columns"].index("OFFER")] = offer
    spot_ts = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)
    nodes = fx.match_futures(
        currency,
        spot_ts,
        contracts,
        payload,
        captured_at=spot_ts - timedelta(seconds=1),
        capture_sha256="a" * 64,
    )
    assert nodes[0]["bid_rub_per_unit"] == expected
    assert nodes[0]["reasons"] == []
