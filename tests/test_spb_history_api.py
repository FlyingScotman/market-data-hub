"""SPB daily history stays a separate authenticated series, including without MOEX listings."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

from mdhub.api.app import create_app
from mdhub.api.auth import create_token
from mdhub.api.columnar import encode_cursor, query_hash
from mdhub.config import Settings

Conn = psycopg.Connection[DictRow]
ISIN = "RU000A10DTC8"
OTHER_ISIN = "RU000A10G0R1"
PATH = "/v1/history/spb-bonds"
AUTH = {"Authorization": "Bearer spb-test-token"}
QUERY = {"isin": ISIN, "venue": "SPB", "from": "2026-11-01", "to": "2026-11-10"}


@pytest.fixture
def spb_client(db_url: str, conn: Conn) -> Iterator[TestClient]:
    for index, isin in enumerate((ISIN, OTHER_ISIN)):
        conn.execute(
            """INSERT INTO ref.spb_bond
            (isin, ticker, security_id, name, price_unit, settlement_currency,
             card_price_unit, unit_conflict, unit_reference_date)
            VALUES (%s, %s, %s, 'Russian bond', 'PCT', 'RUB', 'RUB', true, '2026-11-10')""",
            (isin, ("MGKL1P1", "XMED1P1")[index], index + 1),
        )
        for day in ("2026-11-06", "2026-11-10"):
            conn.execute(
                """INSERT INTO md.spb_bond_daily
                (isin, ticker, trade_date, price_unit, unit_reference_date, card_price_unit,
                 unit_conflict, open, high, low, close, ingested_at)
                VALUES (%s, %s, %s, 'PCT', '2026-11-10', 'RUB', true,
                        7.35, 7.50, 7.10, 7.4300, '2026-11-11 05:00Z')""",
                (isin, ("MGKL1P1", "XMED1P1")[index], day),
            )
    conn.commit()
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="spb-test-token")
    )
    with TestClient(app) as client:
        yield client


def records(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    body = json.loads(response.content, parse_float=Decimal)
    return [dict(zip(body["columns"], row, strict=True)) for row in body["rows"]]


def test_spb_identity_units_gaps_and_no_moex_listing(spb_client: TestClient, conn: Conn) -> None:
    assert conn.execute("SELECT count(*) AS n FROM ref.bond").fetchone() == {"n": 0}
    response = spb_client.get(PATH, params=QUERY, headers=AUTH)
    bars = records(response)
    assert [row["trade_date"] for row in bars] == ["2026-11-06", "2026-11-10"]
    assert all(row["isin"] == ISIN and row["ticker"] == "MGKL1P1" for row in bars)
    assert all(row["venue"] == "SPB" and row["source"] == "spb" for row in bars)
    assert all(row["price_basis"] == "chart_close" and row["price_unit"] == "PCT" for row in bars)
    assert bars[0]["close"] == Decimal("7.4300")
    assert b"7.4300" in response.content
    assert bars[0]["unit_conflict"] is True and bars[0]["card_price_unit"] == "RUB"
    assert bars[0]["unit_reference_date"] == "2026-11-10"
    other = records(spb_client.get(PATH, params={**QUERY, "isin": OTHER_ISIN}, headers=AUTH))
    assert len(other) == 2 and all(row["ticker"] == "XMED1P1" for row in other)
    empty = records(
        spb_client.get(
            PATH,
            params={**QUERY, "from": "2026-11-07", "to": "2026-11-09"},
            headers=AUTH,
        )
    )
    assert empty == []
    index = records(spb_client.get("/v1/history/index", headers=AUTH))
    spb = [row for row in index if row["dataset"] == "spb-bonds"]
    assert len(spb) == 2 and {row["series"] for row in spb} == {ISIN, OTHER_ISIN}
    assert spb[0]["unit"] == "PCT" and spb[0]["rows"] == 2
    assert f"isin={spb[0]['series']}&venue=SPB" in spb[0]["endpoint"]


def test_spb_projection_paging_cursor_and_validation(spb_client: TestClient) -> None:
    query = {**QUERY, "fields": "close", "limit": 1}
    first = spb_client.get(PATH, params=query, headers=AUTH)
    assert records(first)[0]["close"] == Decimal("7.4300")
    assert first.json()["columns"] == [
        "trade_date",
        "isin",
        "venue",
        "source",
        "price_basis",
        "price_unit",
        "close",
        "unit_reference_date",
        "unit_conflict",
    ]
    # the unit and its evidence come with every projection, so a consumer can filter on them
    row = records(first)[0]
    assert (row["price_unit"], row["unit_reference_date"], row["unit_conflict"]) == (
        "PCT",
        "2026-11-10",
        True,
    )
    cursor = first.json()["next"]
    assert cursor
    second = spb_client.get(PATH, params={**query, "cursor": cursor}, headers=AUTH)
    assert records(second)[0]["trade_date"] == "2026-11-10"
    assert second.json()["next"] is None
    assert first.json()["data_version"] == second.json()["data_version"]
    for changed in (
        {"isin": OTHER_ISIN},
        {"venue": "MOEX"},
        {"from": "2026-11-02"},
        {"to": "2026-11-09"},
        {"fields": "high"},
        {"known_at": "2026-11-11T06:00Z"},
    ):
        assert (
            spb_client.get(
                PATH, params={**query, "cursor": cursor, **changed}, headers=AUTH
            ).status_code
            == 400
        )
    for changed in (
        {"isin": "RU000A10DTC9"},
        {"isin": "US0378331005"},
        {"isin": "MGKL1P1"},
        {"venue": "spb"},
        {"from": "2026-11-11"},
        {"from": "2025-01-01", "to": "2026-01-02"},
        {"known_at": "2026-11-11T06:00:00"},
        {"fields": "official_close"},
        {"cursor": "bad"},
    ):
        assert spb_client.get(PATH, params={**QUERY, **changed}, headers=AUTH).status_code == 400
    for changed in ({"limit": 0}, {"limit": 50001}, {"from": "bad"}):
        assert spb_client.get(PATH, params={**QUERY, **changed}, headers=AUTH).status_code == 422
    for missing in ("isin", "venue", "from"):
        assert (
            spb_client.get(
                PATH, params={k: v for k, v in QUERY.items() if k != missing}, headers=AUTH
            ).status_code
            == 422
        )
    # Exactly 366 inclusive dates is valid, including leap years.
    assert (
        spb_client.get(
            PATH, params={**QUERY, "from": "2024-01-01", "to": "2024-12-31"}, headers=AUTH
        ).status_code
        == 200
    )


def test_spb_auth_scope_known_at_and_etag(spb_client: TestClient, conn: Conn) -> None:
    assert spb_client.get(PATH, params=QUERY).status_code == 401
    token, _ = create_token(conn, "replica", scopes=["snapshots"])
    conn.commit()
    assert (
        spb_client.get(PATH, params=QUERY, headers={"Authorization": f"Bearer {token}"}).status_code
        == 403
    )
    query = {**QUERY, "limit": 1}
    first = spb_client.get(PATH, params=query, headers=AUTH)
    etag = first.headers["etag"]
    assert "last-modified" in first.headers
    assert (
        spb_client.get(PATH, params=query, headers={**AUTH, "If-None-Match": etag}).status_code
        == 304
    )
    # A revision on a later page invalidates this page's whole-selection version.
    conn.execute(
        "UPDATE md.spb_bond_daily SET close = 7.45, updated_at = '2026-11-12 05:00Z' "
        "WHERE isin = %s AND trade_date = '2026-11-10'",
        (ISIN,),
    )
    conn.commit()
    revised = spb_client.get(PATH, params=query, headers={**AUTH, "If-None-Match": etag})
    assert revised.status_code == 200 and revised.headers["etag"] != etag
    known_query = {**QUERY, "known_at": "2026-11-11T09:00:00+03:00"}
    historical = spb_client.get(PATH, params=known_query, headers=AUTH)
    assert records(historical)[-1]["close"] == Decimal("7.4300")
    assert historical.json()["known_at"] == "2026-11-11T06:00:00+00:00"
    assert historical.json()["data_version"] is None
    assert "last-modified" not in historical.headers
    assert (
        spb_client.get(
            PATH, params=known_query, headers={**AUTH, "If-None-Match": historical.headers["etag"]}
        ).status_code
        == 304
    )
    assert records(spb_client.get(PATH, params=QUERY, headers=AUTH))[-1]["close"] == Decimal("7.45")
    assert (
        records(
            spb_client.get(PATH, params={**QUERY, "known_at": "2026-11-10T00:00:00Z"}, headers=AUTH)
        )
        == []
    )


def test_spb_invalid_cursor_date_is_client_error(spb_client: TestClient) -> None:
    # A syntactically correct envelope must not send invalid dates to PostgreSQL.
    from datetime import date

    from mdhub.api.routes.history import SPB_COLUMNS

    norm = {
        "isin": ISIN,
        "venue": "SPB",
        "from": date(2026, 11, 1),
        "to": date(2026, 11, 10),
        "fields": [col.name for col in SPB_COLUMNS],
        "known_at": None,
    }
    cursor = encode_cursor(query_hash("spb-bonds", norm), ["not-a-date"])
    assert spb_client.get(PATH, params={**QUERY, "cursor": cursor}, headers=AUTH).status_code == 400
