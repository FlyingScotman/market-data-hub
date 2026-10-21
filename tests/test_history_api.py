"""History read API (``/v1/history/*``): columnar shape, exact values, paging with cursors,
ETag / Last-Modified revalidation, gzip, as-known-at reads, limits, scopes, volume."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

from mdhub.api.app import create_app
from mdhub.api.auth import create_token
from mdhub.config import Settings
from test_api_seed import seed

TOKENS = "tok-hist-1"
AUTH = {"Authorization": "Bearer tok-hist-1"}
Conn = psycopg.Connection[DictRow]


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens=TOKENS)
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded(conn: Conn, client: TestClient) -> TestClient:
    seed(conn)
    conn.execute(
        """
        INSERT INTO md.index_eod (trade_date, secid, close, basis, ingested_at) VALUES
          ('2026-10-15', 'RUSFARIND', 2508.17, 'history', '2026-10-15 16:00Z'),
          ('2026-10-15', 'RUSFARXYZ', 1.23, 'history', '2026-10-15 16:00Z');
        UPDATE md.bond_eod_yield SET icpi = 1.2345, ir = 7.1, bei = 5.9, cbr = 4.0,
               duration_waprice_days = 1000, yield_last_coupon_pct = 15.2
         WHERE secid = 'SU26242RMFS0' AND trade_date = '2026-10-15';
        """
    )
    conn.commit()
    return client


def _json(resp: Any) -> Any:
    return json.loads(resp.content, parse_float=Decimal)


def _records(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(body["columns"], r, strict=True)) for r in body["rows"]]


def _get(client: TestClient, path: str, **params: Any) -> Any:
    r = client.get(path, params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    return _json(r)


# --------------------------------------------------------------------------- bonds


def test_bonds_primary_board_columns_and_exact_values(seeded: TestClient) -> None:
    body = _get(seeded, "/v1/history/bonds", **{"from": "2026-10-01", "to": "2026-10-15"})
    assert body["dataset"] == "bonds"
    assert body["columns"][:3] == ["trade_date", "secid", "board"]
    for name in ("close_pct", "accrued_per_bond", "icpi", "ir", "bei", "cbr", "gspread_bp"):
        assert name in body["columns"]
    recs = _records(body)
    keys = [(r["secid"], r["trade_date"], r["board"]) for r in recs]
    # ordered by secid, trade_date, board; only the main board (no PSOB row)
    assert keys == sorted(keys)
    assert ("SU26242RMFS0", "2026-10-15", "PSOB") not in keys
    assert ("SU26242RMFS0", "2026-10-14", "TQOB") in keys
    ofz = next(r for r in recs if r["secid"] == "SU26242RMFS0" and r["trade_date"] == "2026-10-15")
    assert ofz["close_pct"] == Decimal("97.85")
    assert ofz["effective_yield_pct"] == Decimal("15.42")
    assert ofz["icpi"] == Decimal("1.2345") and ofz["duration_waprice_days"] == 1000
    assert ofz["yield_last_coupon_pct"] == Decimal("15.2")
    assert ofz["value_ccy"] == Decimal("1000000.50")  # stored digits, no float rounding
    assert body["next"] is None and body["known_at"] is None
    assert body["as_of"] and body["data_version"] == "2026-10-15T16:00:00+00:00"
    assert body["meta"]["returned"] == len(recs)


def test_bonds_board_all_secids_and_fields(seeded: TestClient) -> None:
    body = _get(
        seeded,
        "/v1/history/bonds",
        **{"from": "2026-10-15", "secid": "SU26242RMFS0", "board": "all", "fields": "close_pct"},
    )
    assert body["columns"] == ["trade_date", "secid", "board", "close_pct"]
    assert [r[2] for r in body["rows"]] == ["PSOB", "TQOB"]
    body = _get(
        seeded, "/v1/history/bonds", **{"from": "2026-10-15", "board": "PSOB", "fields": "board"}
    )
    assert body["rows"] == [["2026-10-15", "SU26242RMFS0", "PSOB"]]


def test_bonds_validation(seeded: TestClient) -> None:
    many = ",".join(f"S{i}" for i in range(501))
    r = seeded.get("/v1/history/bonds", params={"from": "2026-10-01", "secid": many}, headers=AUTH)
    assert r.status_code == 400 and "500" in r.text
    r = seeded.get(
        "/v1/history/bonds", params={"from": "2026-10-01", "fields": "nope"}, headers=AUTH
    )
    assert r.status_code == 400 and "unknown fields" in r.text
    r = seeded.get(
        "/v1/history/bonds", params={"from": "2026-10-16", "to": "2026-10-01"}, headers=AUTH
    )
    assert r.status_code == 400
    assert seeded.get("/v1/history/bonds", headers=AUTH).status_code == 422  # from required
    r = seeded.get(
        "/v1/history/bonds", params={"from": "2026-10-01", "limit": 50_001}, headers=AUTH
    )
    assert r.status_code == 422
    r = seeded.get(
        "/v1/history/bonds",
        params={"from": "2026-10-01", "known_at": "2026-10-15T19:00:00"},
        headers=AUTH,
    )
    assert r.status_code == 400 and "offset" in r.text


def test_bonds_paging_with_cursor(seeded: TestClient) -> None:
    q = {"from": "2026-10-01", "to": "2026-10-15", "board": "all"}
    full = _get(seeded, "/v1/history/bonds", **q)["rows"]
    assert len(full) == 5
    rows: list[Any] = []
    cursor = None
    pages = 0
    while True:
        params = {**q, "limit": 2, **({"cursor": cursor} if cursor else {})}
        body = _get(seeded, "/v1/history/bonds", **params)
        rows.extend(body["rows"])
        pages += 1
        cursor = body["next"]
        if cursor is None:
            break
    assert rows == full and pages == 3
    # a cursor is bound to its query
    first = _get(seeded, "/v1/history/bonds", **q, limit=2)
    r = seeded.get(
        "/v1/history/bonds",
        params={"from": "2026-10-02", "board": "all", "cursor": first["next"]},
        headers=AUTH,
    )
    assert r.status_code == 400 and "cursor" in r.text
    r = seeded.get("/v1/history/bonds", params={**q, "cursor": "garbage!"}, headers=AUTH)
    assert r.status_code == 400


def test_etag_last_modified_and_gzip(seeded: TestClient, conn: Conn) -> None:
    q = {"from": "2026-10-01", "to": "2026-10-15"}
    r = seeded.get("/v1/history/bonds", params=q, headers={**AUTH, "Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["vary"] == "Accept-Encoding"
    etag, lm = r.headers["etag"], r.headers["last-modified"]
    assert etag.startswith('W/"') and lm.endswith("GMT")
    hit = seeded.get("/v1/history/bonds", params=q, headers={**AUTH, "If-None-Match": etag})
    assert hit.status_code == 304 and hit.headers["etag"] == etag and not hit.content
    hit = seeded.get("/v1/history/bonds", params=q, headers={**AUTH, "If-Modified-Since": lm})
    assert hit.status_code == 304
    plain = seeded.get(
        "/v1/history/bonds", params=q, headers={**AUTH, "Accept-Encoding": "identity"}
    )
    assert "content-encoding" not in plain.headers and plain.headers["etag"] == etag
    # a revision of a past row changes the validators
    conn.execute(
        "UPDATE md.bond_eod SET close = 97.90, updated_at = now() "
        "WHERE secid = 'SU26242RMFS0' AND trade_date = '2026-10-14' AND board = 'TQOB'"
    )
    conn.commit()
    miss = seeded.get("/v1/history/bonds", params=q, headers={**AUTH, "If-None-Match": etag})
    assert miss.status_code == 200 and miss.headers["etag"] != etag
    miss = seeded.get("/v1/history/bonds", params=q, headers={**AUTH, "If-Modified-Since": lm})
    assert miss.status_code == 200
    # another page of the same query has its own ETag
    page = seeded.get("/v1/history/bonds", params={**q, "limit": 1}, headers=AUTH)
    assert page.headers["etag"] != miss.headers["etag"]


def test_bonds_known_at_replays_revisions(seeded: TestClient, conn: Conn) -> None:
    # the main board is read as known at known_at too: the bond must be known by then
    conn.execute("UPDATE ref.bond SET ingested_at = '2026-01-01Z', updated_at = '2026-01-01Z'")
    conn.execute(
        "UPDATE md.bond_eod SET close = 98.10, updated_at = '2026-10-16 05:00Z' "
        "WHERE secid = 'SU26242RMFS0' AND trade_date = '2026-10-15' AND board = 'TQOB'"
    )
    conn.commit()
    q = {"from": "2026-10-15", "secid": "SU26242RMFS0", "fields": "close_pct"}
    now = _get(seeded, "/v1/history/bonds", **q)
    assert now["rows"] == [["2026-10-15", "SU26242RMFS0", "TQOB", Decimal("98.10")]]
    then = _get(seeded, "/v1/history/bonds", **q, known_at="2026-10-15T20:00:00+03:00")
    assert then["rows"] == [["2026-10-15", "SU26242RMFS0", "TQOB", Decimal("97.85")]]
    assert then["known_at"] == "2026-10-15T17:00:00+00:00"
    r = seeded.get(
        "/v1/history/bonds",
        params={**q, "known_at": "2026-10-15T20:00:00+03:00"},
        headers=AUTH,
    )
    again = seeded.get(
        "/v1/history/bonds",
        params={**q, "known_at": "2026-10-15T20:00:00+03:00"},
        headers={**AUTH, "If-None-Match": r.headers["etag"]},
    )
    assert again.status_code == 304


def test_history_needs_read_scope(seeded: TestClient, conn: Conn) -> None:
    token, _ = create_token(conn, "replica", scopes=["snapshots"])
    conn.commit()
    r = seeded.get(
        "/v1/history/bonds",
        params={"from": "2026-10-01"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert seeded.get("/v1/history/index").status_code == 401


# -------------------------------------------------------------------------- curves


def test_curves_eod_rule_and_all(seeded: TestClient, conn: Conn) -> None:
    # snapshots are filtered on fetched_at and ingested_at <= known_at (as the snapshot builder)
    conn.execute("UPDATE md.curve_snapshot SET ingested_at = fetched_at")
    conn.commit()
    body = _get(seeded, "/v1/history/curves/ruonia_ois", **{"from": "2026-10-01"})
    recs = _records(body)
    # one snapshot per (day, source): the latest of the day (12:00Z), quotes in position order
    assert {r["fetched_at"] for r in recs} == {"2026-10-15T12:00:00+00:00"}
    assert [r["tenor"] for r in recs] == ["1Y", "2Y"]
    assert recs[0]["bid_pct"] == Decimal("15.20") and recs[0]["ask_pct"] == Decimal("15.40")
    assert recs[0]["mid_pct"] == Decimal("15.30") and recs[0]["labels"] == []
    assert recs[0]["source"] == "ratescalcs" and recs[0]["quote_basis"] == "bid_ask_mid"
    every = _get(seeded, "/v1/history/curves/ruonia_ois", **{"from": "2026-10-01", "rule": "all"})
    assert len(every["rows"]) == 4
    # the eod rule as known before the second snapshot was fetched
    early = _get(
        seeded,
        "/v1/history/curves/ruonia_ois",
        **{"from": "2026-10-01", "known_at": "2026-10-15T11:00:00Z"},
    )
    assert {r["fetched_at"] for r in _records(early)} == {"2026-10-15T10:00:00+00:00"}
    paged = _get(
        seeded, "/v1/history/curves/ruonia_ois", **{"from": "2026-10-01", "rule": "all", "limit": 3}
    )
    nxt = _get(
        seeded,
        "/v1/history/curves/ruonia_ois",
        **{"from": "2026-10-01", "rule": "all", "limit": 3, "cursor": paged["next"]},
    )
    assert paged["rows"] + nxt["rows"] == every["rows"] and nxt["next"] is None
    r = seeded.get("/v1/history/curves/nope", params={"from": "2026-10-01"}, headers=AUTH)
    assert r.status_code == 404
    r = seeded.get(
        "/v1/history/curves/ruonia_ois", params={"from": "2026-10-01", "rule": "x"}, headers=AUTH
    )
    assert r.status_code == 400


def test_curves_labels(seeded: TestClient, conn: Conn) -> None:
    sid = conn.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,"
        " source_ts, market_closed, content_sha256, parser_version) VALUES"
        " ('otc_monitor', 'ruonia_ois', 'mid_only', '2026-10-14 06:00Z', '2026-10-14',"
        " '2026-10-13 17:00Z', true, %s, '1') RETURNING id",
        ("f" * 64,),
    ).fetchone()
    assert sid is not None
    conn.execute(
        "INSERT INTO md.curve_quote (snapshot_id, tenor, position, mid_pct)"
        " VALUES (%s, '1Y', 0, 15)",
        (sid["id"],),
    )
    conn.commit()
    recs = _records(
        _get(seeded, "/v1/history/curves/ruonia_ois", **{"from": "2026-10-14", "to": "2026-10-14"})
    )
    assert recs[0]["labels"] == ["market_closed", "mid_only", "source_clock_not_on_business_date"]
    assert recs[0]["bid_pct"] is None and recs[0]["mid_pct"] == 15


# ---------------------------------------------------------------------------- ZCYC


def test_zcyc_eod_and_all(seeded: TestClient) -> None:
    body = _get(seeded, "/v1/history/zcyc", **{"from": "2026-10-01"})
    recs = _records(body)
    assert len(recs) == 1 and recs[0]["is_eod"] is True and recs[0]["labels"] == []
    assert recs[0]["trade_time"] == "18:54:59"
    assert recs[0]["B1"] == Decimal("1506.334188") and recs[0]["G9"] == Decimal("0.0")
    assert recs[0]["standard_yields"][0] == {
        "tenor_years": Decimal("0.25"),
        "yield_pct": Decimal("12.4175"),
    }
    every = _records(_get(seeded, "/v1/history/zcyc", **{"from": "2026-10-01", "rule": "all"}))
    assert [r["trade_time"] for r in every] == ["12:00:00", "18:54:59"]
    assert every[0]["labels"] == ["not_end_of_day_publication"]


# ------------------------------------------------------------------------- fixings


def test_fixings_units_rusfar_and_hub_ingested_at(seeded: TestClient) -> None:
    body = _get(
        seeded,
        "/v1/history/fixings",
        **{"from": "2026-10-01", "code": "RUSFAR,RUSFARIND,RUSFARXYZ,RUONIA,KEY_RATE"},
    )
    recs = _records(body)
    by = {(r["index_code"], r["fixing_date"]): r for r in recs}
    assert by[("RUSFAR", "2026-10-15")]["unit"] == "pct"
    assert by[("RUSFARIND", "2026-10-15")]["unit"] == "index"
    assert by[("RUSFARIND", "2026-10-15")]["value"] == Decimal("2508.17")
    assert not any(r["index_code"] == "RUSFARXYZ" for r in recs)
    assert body["meta"]["not_served"] == [
        {"code": "RUSFARXYZ", "reason": "rusfar_series_unit_unknown"}
    ]
    ruonia = by[("RUONIA", "2026-10-14")]
    assert ruonia["hub_ingested_at"] == "2026-10-15T12:00:00+00:00"
    assert ruonia["published_on"] == "2026-10-15" and ruonia["origin"] == "md.fixing"
    assert by[("RUSFAR", "2026-10-15")]["origin"] == "md.index_eod"
    keys = [(r["index_code"], r["source"], r["fixing_date"]) for r in recs]
    assert keys == sorted(keys)
    # all codes by default, including the RUSFARCNY preliminary label
    allf = _records(_get(seeded, "/v1/history/fixings", **{"from": "2026-10-01"}))
    cny = next(r for r in allf if r["index_code"] == "RUSFARCNY")
    assert cny["labels"] == ["moex_marketdata_preliminary"] and cny["unit"] == "pct"
    assert not any(r["index_code"] in ("RUSFARXYZ", "RGBI") for r in allf)
    only_cbr = _records(
        _get(seeded, "/v1/history/fixings", **{"from": "2026-10-01", "source": "cbr"})
    )
    assert {r["source"] for r in only_cbr} == {"cbr"}


# ------------------------------------------------------------------------------ FX


def test_fx_kinds_pairs_and_units(seeded: TestClient) -> None:
    recs = _records(_get(seeded, "/v1/history/fx", **{"from": "2026-10-14", "pair": "USD"}))
    kinds = {(r["kind"], r["date"]) for r in recs}
    assert ("cbr_official", "2026-10-16") in kinds and ("moex_indicative_19", "2026-10-15") in kinds
    cbr = next(r for r in recs if r["kind"] == "cbr_official" and r["date"] == "2026-10-15")
    assert cbr["rate"] == Decimal("81.1200") and cbr["pair"] == "USD/RUB"
    assert cbr["set_date"] == "2026-10-14"
    tc = next(r for r in recs if r["kind"] == "moex_indicative_14")
    assert tc["rate"] == Decimal("81.30") and tc["trade_time"] == "14:00"
    cny = _records(
        _get(
            seeded,
            "/v1/history/fx",
            **{"from": "2026-10-15", "pair": "CNY/RUB", "kind": "moex_eod"},
        )
    )
    assert len(cny) == 1
    assert cny[0]["secid"] == "CNYRUB_TOM" and cny[0]["tenor"] == "TOM"
    assert cny[0]["close"] == Decimal("11.3400") and cny[0]["rate"] is None
    jpy = _records(_get(seeded, "/v1/history/fx", **{"from": "2026-10-15", "pair": "JPY"}))
    assert jpy[0]["rate"] == Decimal("0.543210") and jpy[0]["nominal"] == 100
    cross = _records(_get(seeded, "/v1/history/fx", **{"from": "2026-10-15", "pair": "EUR/USD"}))
    assert [r["kind"] for r in cross] == ["moex_indicative_19"]
    r = seeded.get("/v1/history/fx", params={"from": "2026-10-15", "kind": "x"}, headers=AUTH)
    assert r.status_code == 400
    r = seeded.get("/v1/history/fx", params={"from": "2026-10-15", "pair": "usd-rub"}, headers=AUTH)
    assert r.status_code == 400


def test_fx_per_unit_scaling(seeded: TestClient, conn: Conn) -> None:
    conn.execute(
        "INSERT INTO ref.fx_instrument (secid, board, base_ccy, quote_ccy, kind, tenor, face_value,"
        " first_seen) VALUES ('KZTRUB_TOM', 'CETS', 'KZT', 'RUB', 'outright', 'TOM', 100,"
        " '2026-01-01'), ('USDRUB_SWP', 'CETS', 'USD', 'RUB', 'swap', NULL, 1, '2026-01-01');"
        "INSERT INTO md.fx_eod (trade_date, secid, board, close, waprice) VALUES"
        " ('2026-10-15', 'KZTRUB_TOM', 'CETS', 15.6250, NULL),"
        " ('2026-10-15', 'USDRUB_SWP', 'CETS', 0.01, NULL)"
    )
    conn.commit()
    recs = _records(_get(seeded, "/v1/history/fx", **{"from": "2026-10-15", "kind": "moex_eod"}))
    kzt = next(r for r in recs if r["secid"] == "KZTRUB_TOM")
    assert kzt["quote_units"] == 100 and kzt["close_per_unit"] == Decimal("0.156250")
    assert kzt["waprice_per_unit"] is None
    assert not any(r["secid"] == "USDRUB_SWP" for r in recs)  # swaps are not outrights


# --------------------------------------------------------------------------- index


def test_index_lists_datasets_and_depth(seeded: TestClient) -> None:
    body = _get(seeded, "/v1/history/index")
    assert body["columns"] == [
        "dataset",
        "series",
        "source",
        "unit",
        "date_from",
        "date_to",
        "rows",
        "days",
        "series_count",
        "endpoint",
    ]
    recs = _records(body)
    bonds = next(r for r in recs if r["dataset"] == "bonds")
    assert bonds["date_from"] == "2026-10-14" and bonds["date_to"] == "2026-10-15"
    assert bonds["rows"] == 5 and bonds["series_count"] == 3
    curve = next(r for r in recs if r["dataset"] == "curves")
    assert curve["series"] == "ruonia_ois" and curve["source"] == "ratescalcs"
    assert curve["endpoint"] == "/v1/history/curves/ruonia_ois"
    fix = {r["series"]: r for r in recs if r["dataset"] == "fixings"}
    assert fix["RUSFARIND"]["unit"] == "index" and fix["RUSFAR"]["unit"] == "pct"
    assert "RUSFARXYZ" not in fix and fix["KEY_RATE"]["rows"] == 2
    fx = {(r["series"], r["unit"]) for r in recs if r["dataset"] == "fx"}
    assert ("USD/RUB", "cbr_official") in fx and ("CNY/RUB", "moex_eod:CNYRUB_TOM:CETS") in fx
    assert body["meta"]["limits"]["max_secids"] == 500


# --------------------------------------------------------------------------- volume


def test_year_of_600_bonds_in_a_few_calls(client: TestClient, conn: Conn) -> None:
    """600 bonds x 250 business days (150,000 rows with yields) in 3 pages of 50,000."""
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, name, bond_class, coupon_type, coupon_type_basis,
               primary_board, marketprice_board, status)
        SELECT 'B' || lpad(i::text, 4, '0'), 'RU' || lpad(i::text, 10, '0'), 'bond ' || i,
               'corporate', 'fixed', 'moex_label', 'TQCB', 'TQCB', 'active'
        FROM generate_series(1, 600) i;
        WITH days AS (
            SELECT d::date AS d FROM generate_series('2025-01-01'::date, '2025-12-31', '1 day') d
            WHERE extract(isodow FROM d) < 6 LIMIT 250)
        INSERT INTO md.bond_eod (trade_date, secid, board, close, waprice, accrued_int,
               face_value, face_unit, currency_id, volume, value, num_trades)
        SELECT days.d, 'B' || lpad(i::text, 4, '0'), 'TQCB', 95 + i / 100.0, 95.1, 12.34, 1000,
               'SUR', 'SUR', 1000, 950000, 10 FROM days, generate_series(1, 600) i;
        INSERT INTO md.bond_eod_yield (trade_date, secid, board, effective_yield_pct,
               duration_days, gspread_bp)
        SELECT trade_date, secid, board, 15.5, 700, 120 FROM md.bond_eod;
        ANALYZE md.bond_eod; ANALYZE md.bond_eod_yield;
        """
    )
    conn.commit()
    secids = ",".join(f"B{i:04d}" for i in range(1, 501))
    started = time.monotonic()
    rows = 0
    calls = 0
    cursor = None
    while True:
        params: dict[str, Any] = {"from": "2025-01-01", "to": "2025-12-31", "limit": 50_000}
        params["secid"] = secids
        if cursor:
            params["cursor"] = cursor
        r = client.get(
            "/v1/history/bonds", params=params, headers={**AUTH, "Accept-Encoding": "gzip"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        rows += len(body["rows"])
        calls += 1
        cursor = body["next"]
        if cursor is None:
            break
    elapsed = time.monotonic() - started
    print(f"history volume: {rows} rows in {calls} calls, {elapsed:.2f} s")
    assert rows == 500 * 250 and calls == 3
    assert elapsed < 60, f"{rows} rows in {elapsed:.1f} s"
    # the whole universe without a secid list also pages through
    body = client.get(
        "/v1/history/bonds",
        params={"from": "2025-12-01", "to": "2025-12-31", "fields": "close_pct", "limit": 50000},
        headers=AUTH,
    ).json()
    assert body["meta"]["returned"] == 600 * len({r[0] for r in body["rows"]})
    assert date.fromisoformat(body["rows"][0][0]) >= date(2025, 12, 1)
