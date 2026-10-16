"""Read API (mdhub.api): auth, health, endpoints, paging, ETags, as-known-at reconstruction."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from mdhub.api.app import create_app
from mdhub.api.jsonutil import dumps
from mdhub.config import Settings
from test_api_seed import seed

TOKENS = "tok-alpha-123,tok-beta-456"
AUTH = {"Authorization": "Bearer tok-alpha-123"}


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=db_url, api_tokens=TOKENS))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded(conn: psycopg.Connection[DictRow], client: TestClient) -> TestClient:
    seed(conn)
    return client


def _json(resp: Any) -> Any:
    """Parse with decimals preserved (the API writes numbers verbatim)."""
    return json.loads(resp.content, parse_float=Decimal)


# ------------------------------------------------------------------------ json + auth


def test_dumps_keeps_decimal_digits() -> None:
    out = dumps({"a": Decimal("14.10"), "b": Decimal("1E+2"), "c": Decimal("NaN"), "d": 0.1})
    assert out == '{"a":14.10,"b":100,"c":null,"d":0.1}'
    assert json.loads(out, parse_float=Decimal)["a"] == Decimal("14.10")
    assert dumps(["ключ", None, True]) == '["ключ",null,true]'


def test_health_is_public_and_ready(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    r = client.get("/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["migrations"]["pending"] == 0
    assert body["migrations"]["applied"] == body["migrations"]["known"]


def test_ready_reports_pending_migrations(db_url: str) -> None:
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DELETE FROM ops.schema_migration WHERE version = '0410_api_views'")
    app = create_app(Settings(database_url=db_url, api_tokens=TOKENS))
    with TestClient(app) as c:
        r = c.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["migrations"]["pending"] == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic dG9rLWFscGhhLTEyMw=="},
        {"Authorization": "Bearer "},
        {"Authorization": "tok-alpha-123"},
    ],
)
def test_v1_requires_token(client: TestClient, headers: dict[str, str]) -> None:
    r = client.get("/v1/coverage", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"]["message"] == "unauthorized"
    assert "tok-" not in r.text


def test_auth_checked_before_validation(client: TestClient) -> None:
    r = client.get("/v1/bonds/eod?date=not-a-date")
    assert r.status_code == 401
    r = client.get("/v1/bonds/eod?date=not-a-date", headers=AUTH)
    assert r.status_code == 422


def test_second_token_and_openapi(client: TestClient) -> None:
    r = client.get("/v1/openapi.json", headers={"Authorization": "bearer tok-beta-456"})
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/v1/bonds/{secid}" in paths
    assert "/v1/export/tdm" in paths
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_no_tokens_configured_rejects_everything(db_url: str) -> None:
    app = create_app(Settings(database_url=db_url))
    with TestClient(app) as c:
        assert c.get("/v1/coverage", headers=AUTH).status_code == 401
        assert c.get("/health/live").status_code == 200


def test_request_id_echo_and_generation(client: TestClient) -> None:
    r = client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = client.get("/health/live", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["x-request-id"] != "bad id with spaces"
    assert len(r.headers["x-request-id"]) == 32
    r = client.get("/v1/coverage", headers={"X-Request-ID": "rid-401"})
    assert r.json()["request_id"] == "rid-401"


def test_tokens_never_logged(seeded: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seeded.get("/v1/bonds", headers=AUTH)
    seeded.get("/v1/bonds", headers={"Authorization": "Bearer tok-wrong-999"})
    assert "tok-alpha-123" not in caplog.text
    assert "tok-wrong-999" not in caplog.text
    assert "/v1/bonds" in caplog.text


# --------------------------------------------------------------------------- ops


def test_coverage_and_runs(seeded: TestClient) -> None:
    cov = seeded.get("/v1/coverage", headers=AUTH).json()["data"]
    assert [c["dataset"] for c in cov] == ["bonds_eod"]
    assert cov[0]["last_run_status"] == "succeeded"
    runs = seeded.get("/v1/runs", headers=AUTH).json()
    assert [r["job"] for r in runs["data"]] == ["bonds_eod", "cbr_daily"]
    failed = seeded.get("/v1/runs?status=failed", headers=AUTH).json()["data"]
    assert [r["job"] for r in failed] == ["cbr_daily"]
    health = seeded.get("/v1/runs/health", headers=AUTH).json()["data"]
    assert {h["job"] for h in health} == {"bonds_eod", "cbr_daily"}
    assert seeded.get("/v1/gaps", headers=AUTH).json()["data"] == []


# ------------------------------------------------------------------------- bonds


def test_bonds_list_filters_and_paging(seeded: TestClient) -> None:
    body = seeded.get("/v1/bonds?limit=2", headers=AUTH).json()
    assert [b["secid"] for b in body["data"]] == ["DUPA", "DUPB"]
    assert body["meta"]["next_offset"] == 2
    assert "capture_id" not in body["data"][0]
    page2 = seeded.get("/v1/bonds?limit=2&offset=2", headers=AUTH).json()
    assert [b["secid"] for b in page2["data"]] == ["OLD1", "RU000A105X12"]
    last = seeded.get("/v1/bonds?limit=2&offset=4", headers=AUTH).json()
    assert last["meta"]["next_offset"] is None
    assert len(last["data"]) == 2

    def secids(qs: str) -> list[str]:
        return [b["secid"] for b in seeded.get(f"/v1/bonds?{qs}", headers=AUTH).json()["data"]]

    assert secids("bond_class=ofz_pd") == ["SU26242RMFS0"]
    assert secids("status=matured") == ["OLD1"]
    assert secids("board=tqob") == ["SU26242RMFS0"]
    assert secids("board=TQOD") == []  # DUPB is no longer listed there
    assert secids("q=26242") == ["SU26242RMFS0"]
    assert secids("q=дубль") == ["DUPA", "DUPB"]
    assert secids("q=%25") == []  # LIKE wildcards are escaped
    assert secids("coupon_type=floating") == ["RU000A105X12"]
    assert seeded.get("/v1/bonds?limit=0", headers=AUTH).status_code == 422


def test_bond_detail(seeded: TestClient) -> None:
    r = seeded.get("/v1/bonds/ru000a105x12", headers=AUTH)
    assert r.status_code == 200
    b = _json(r)["data"]
    assert b["secid"] == "RU000A105X12"
    assert b["issuer"]["inn"] == "7700000002"
    assert [c["coupon_no"] for c in b["coupons"]] == [8, 9]
    assert b["coupons"][0]["amount"] == Decimal("40.00")
    assert [o["offer_type"] for o in b["offers"]] == ["put", "call"]
    assert b["formula"]["resolved"]["benchmark"] == "KEY_RATE"
    assert b["formula"]["resolved"]["spread_bp"] == Decimal(150)
    assert [s["source"] for s in b["formula"]["sources"]] == ["moex_iss"]
    assert [x["board"] for x in b["boards"]] == ["TQCB"]
    assert b["ratings"] == []
    assert seeded.get("/v1/bonds/NOPE", headers=AUTH).status_code == 404
    ofz = _json(seeded.get("/v1/bonds/SU26242RMFS0", headers=AUTH))["data"]
    assert [a["data_source"] for a in ofz["amortizations"]] == ["maturity"]
    assert [x["board"] for x in ofz["boards"]] == ["TQOB", "PSOB"]


def test_bonds_eod_joined_yields_units(seeded: TestClient) -> None:
    r = seeded.get("/v1/bonds/eod?date=2026-10-15", headers=AUTH)
    assert r.status_code == 200
    body = _json(r)
    rows = {(x["secid"], x["board"]): x for x in body["data"]}
    assert set(rows) == {
        ("DUPA", "TQCB"),
        ("RU000A105X12", "TQCB"),
        ("SU26242RMFS0", "PSOB"),
        ("SU26242RMFS0", "TQOB"),
    }
    ofz = rows[("SU26242RMFS0", "TQOB")]
    assert ofz["close_pct"] == Decimal("97.85")
    assert ofz["value_ccy"] == Decimal("1000000.50")
    assert ofz["accrued_int_per_bond"] == Decimal("1.23")
    assert ofz["y_effective_yield_pct"] == Decimal("15.42")
    assert ofz["y_gspread_bp"] == Decimal(12)
    assert ofz["y_yield_date"] == "2029-08-29"
    assert rows[("SU26242RMFS0", "PSOB")]["y_effective_yield_pct"] is None
    assert "units" in body["meta"]
    only = seeded.get("/v1/bonds/eod?date=2026-10-15&secid=SU26242RMFS0&board=TQOB", headers=AUTH)
    assert len(only.json()["data"]) == 1
    assert seeded.get("/v1/bonds/eod", headers=AUTH).status_code == 422


def test_bond_history(seeded: TestClient) -> None:
    body = _json(
        seeded.get(
            "/v1/bonds/SU26242RMFS0/history?from=2026-10-01&to=2026-10-31&board=TQOB",
            headers=AUTH,
        )
    )
    assert [(r["trade_date"], r["close_pct"]) for r in body["data"]] == [
        ("2026-10-14", Decimal("97.70")),
        ("2026-10-15", Decimal("97.85")),
    ]
    bad = seeded.get("/v1/bonds/SU26242RMFS0/history?from=2026-10-31&to=2026-10-01", headers=AUTH)
    assert bad.status_code == 400


def test_etag_conditional_get(seeded: TestClient) -> None:
    r = seeded.get("/v1/bonds/eod?date=2026-10-15", headers=AUTH)
    etag = r.headers["etag"]
    again = seeded.get("/v1/bonds/eod?date=2026-10-15", headers={**AUTH, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    weak = seeded.get(
        "/v1/bonds/eod?date=2026-10-15", headers={**AUTH, "If-None-Match": f'"x", W/{etag}'}
    )
    assert weak.status_code == 304
    other = seeded.get("/v1/bonds/eod?date=2026-10-14", headers={**AUTH, "If-None-Match": etag})
    assert other.status_code == 200


# ----------------------------------------------------------------------- as-of


def _revise_bond(conn: psycopg.Connection[DictRow]) -> None:
    """97.85 (ingested 15 Oct 16:00Z) -> 98.00 (16 Oct 05:00Z) -> 98.10 (16 Oct 07:00Z)."""
    for close, at in (("98.00", "2026-10-16 05:00Z"), ("98.10", "2026-10-16 07:00Z")):
        conn.execute(
            "UPDATE md.bond_eod SET close = %s WHERE trade_date = '2026-10-15' "
            "AND secid = 'SU26242RMFS0' AND board = 'TQOB'",
            (close,),
        )
        conn.execute(
            "UPDATE ops.row_history SET replaced_at = %s WHERE id = "
            "(SELECT max(id) FROM ops.row_history WHERE table_name = 'md.bond_eod')",
            (at,),
        )
    # a yields revision too, and a row first inserted after the first cut
    conn.execute(
        "UPDATE md.bond_eod_yield SET effective_yield_pct = 15.50 WHERE trade_date = '2026-10-15'"
        " AND secid = 'SU26242RMFS0'"
    )
    conn.execute(
        "UPDATE ops.row_history SET replaced_at = '2026-10-16 05:00Z' "
        "WHERE table_name = 'md.bond_eod_yield'"
    )
    conn.execute(
        "INSERT INTO md.bond_eod (trade_date, secid, board, close, ingested_at) VALUES "
        "('2026-10-15', 'LATE1', 'TQCB', 50, '2026-10-16 06:00Z')"
    )
    conn.commit()


@pytest.mark.parametrize(
    ("known_at", "close", "ytm", "late"),
    [
        ("2026-10-15T15:59:59Z", None, None, False),  # before first ingestion: absent
        ("2026-10-15T20:00:00+03:00", "97.85", "15.42", False),
        ("2026-10-16T05:00:00Z", "98.00", "15.50", False),  # new version known from replaced_at
        ("2026-10-16T05:30:00Z", "98.00", "15.50", False),
        ("2026-10-16T06:30:00Z", "98.00", "15.50", True),
        ("2026-10-16T08:00:00Z", "98.10", "15.50", True),
    ],
)
def test_bonds_eod_known_at(
    seeded: TestClient,
    conn: psycopg.Connection[DictRow],
    known_at: str,
    close: str | None,
    ytm: str | None,
    late: bool,
) -> None:
    _revise_bond(conn)
    r = seeded.get(
        "/v1/bonds/eod", params={"date": "2026-10-15", "known_at": known_at}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    rows = {(x["secid"], x["board"]): x for x in _json(r)["data"]}
    ofz = rows.get(("SU26242RMFS0", "TQOB"))
    if close is None:
        assert rows == {}
    else:
        assert ofz is not None
        assert ofz["close_pct"] == Decimal(close)
        assert ofz["y_effective_yield_pct"] == Decimal(ytm or "0")
    assert (("LATE1", "TQCB") in rows) is late


def test_known_at_requires_offset(seeded: TestClient) -> None:
    r = seeded.get("/v1/bonds/eod?date=2026-10-15&known_at=2026-10-16T08:00:00", headers=AUTH)
    assert r.status_code == 400
    assert "offset" in r.json()["error"]["message"]


def test_known_at_function_matches_view(
    seeded: TestClient, conn: psycopg.Connection[DictRow]
) -> None:
    _revise_bond(conn)
    cur = conn.execute(
        "SELECT * FROM md.v_bond_eod WHERE trade_date BETWEEN '2026-10-14' AND '2026-10-15' "
        "ORDER BY secid, board, trade_date"
    ).fetchall()
    asof = conn.execute(
        "SELECT * FROM md.v_bond_eod_known_at(now() + interval '1 hour', '2026-10-14', "
        "'2026-10-15') ORDER BY secid, board, trade_date"
    ).fetchall()
    assert len(cur) == 6
    assert cur == asof


def _revise_key_rate(conn: psycopg.Connection[DictRow]) -> None:
    """KEY_RATE 2026-10-15: 16.5 (known 14 Oct 09:00Z) -> 16.0 (15 Oct 12:00Z) -> 16.75 (14:00Z)."""
    for value, at in (("16.0", "2026-10-15 12:00Z"), ("16.75", "2026-10-15 14:00Z")):
        conn.execute(
            "UPDATE md.fixing SET value = %s, ingested_at = %s WHERE index_code = 'KEY_RATE' "
            "AND fixing_date = '2026-10-15'",
            (value, at),
        )
        conn.execute(
            "UPDATE ops.row_history SET replaced_at = %s WHERE id = "
            "(SELECT max(id) FROM ops.row_history WHERE table_name = 'md.fixing')",
            (at,),
        )
    conn.commit()


@pytest.mark.parametrize(
    ("known_at", "value"),
    [
        ("2026-10-14T08:00:00Z", None),
        ("2026-10-15T11:00:00Z", "16.5"),
        ("2026-10-15T13:00:00Z", "16.0"),
        ("2026-10-15T15:00:00Z", "16.75"),
        (None, "16.75"),
    ],
)
def test_fixings_known_at(
    seeded: TestClient, conn: psycopg.Connection[DictRow], known_at: str | None, value: str | None
) -> None:
    _revise_key_rate(conn)
    params = {"index": "KEY_RATE", "date": "2026-10-15"}
    if known_at:
        params["known_at"] = known_at
    r = seeded.get("/v1/fixings", params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    data = _json(r)["data"]
    if value is None:
        assert data == []
    else:
        assert [(d["index_code"], d["value"]) for d in data] == [("KEY_RATE", Decimal(value))]


def test_fixings_semantics(seeded: TestClient) -> None:
    body = _json(
        seeded.get("/v1/fixings?index=key_rate,ruonia&from=2026-10-13&to=2026-10-15", headers=AUTH)
    )
    got = [
        (d["index_code"], d["fixing_date"], d["effective_date"], d["published_on"])
        for d in body["data"]
    ]
    assert got == [
        ("KEY_RATE", "2026-10-14", "2026-10-14", None),
        ("KEY_RATE", "2026-10-15", "2026-10-15", None),
        ("RUONIA", "2026-10-13", None, "2026-10-14"),
        ("RUONIA", "2026-10-14", None, "2026-10-15"),
    ]
    assert body["meta"]["date_from"] == "2026-10-13"


# ---------------------------------------------------------------------- market


def test_fx_endpoints(seeded: TestClient) -> None:
    off = _json(seeded.get("/v1/fx/official?date=2026-10-15", headers=AUTH))["data"]
    assert [(r["ccy"], r["rate_per_unit"], r["nominal"]) for r in off] == [
        ("JPY", Decimal("0.543210"), 100),
        ("USD", Decimal("81.1200"), 1),
    ]
    assert off[1]["set_date"] == "2026-10-14"
    rng = seeded.get("/v1/fx/official?from=2026-10-14&to=2026-10-16&ccy=usd", headers=AUTH)
    assert [r["effective_date"] for r in rng.json()["data"]] == [
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
    ]
    assert seeded.get("/v1/fx/official", headers=AUTH).status_code == 400
    both = seeded.get("/v1/fx/official?date=2026-10-15&from=2026-10-14", headers=AUTH)
    assert both.status_code == 400

    eod = seeded.get("/v1/fx/eod?date=2026-10-15&secid=CNYRUB_TOM", headers=AUTH).json()["data"]
    assert eod[0]["base_ccy"] == "CNY"
    assert eod[0]["tenor"] == "TOM"
    ind = _json(seeded.get("/v1/fx/indicative?date=2026-10-15&clearing=mc", headers=AUTH))
    assert [(r["ccy_pair"], r["rate"]) for r in ind["data"]] == [
        ("CNY/RUB", Decimal("11.35")),
        ("EUR/USD", Decimal("1.0850")),
        ("USD/RUB", Decimal("81.45")),
    ]
    ecb = seeded.get("/v1/fx/ecb?date=2026-10-15&ccy=CHF", headers=AUTH).json()["data"]
    assert [r["ccy"] for r in ecb] == ["CHF"]


def test_futures(seeded: TestClient) -> None:
    fx = _json(seeded.get("/v1/futures/eod?date=2026-10-15", headers=AUTH))["data"]
    assert [(r["secid"], r["settle_rub_per_unit"], r["contract_type"]) for r in fx] == [
        ("SiZ6", Decimal("82.45"), "dated"),
        ("USDRUBF", Decimal("81.55"), "perpetual"),
    ]
    all_ = seeded.get("/v1/futures/eod?date=2026-10-15&fx_only=false", headers=AUTH).json()
    assert len(all_["data"]) == 3
    con = seeded.get("/v1/futures/contracts?active_on=2026-12-18", headers=AUTH).json()["data"]
    assert [c["secid"] for c in con] == ["USDRUBF"]
    si = seeded.get("/v1/futures/contracts?asset_code=Si", headers=AUTH).json()["data"]
    assert [c["secid"] for c in si] == ["SiZ6"]


def test_curves_as_of(seeded: TestClient) -> None:
    lst = seeded.get("/v1/curves", headers=AUTH).json()["data"]
    assert lst[0]["dataset"] == "ruonia_ois"
    assert lst[0]["snapshots"] == 2

    def mids(as_of: str) -> list[Any]:
        r = seeded.get("/v1/curves/ruonia_ois", params={"as_of": as_of}, headers=AUTH)
        assert r.status_code == 200, r.text
        data = _json(r)["data"]
        return [[q["mid_pct"] for q in s["quotes"]] for s in data]

    assert mids("2026-10-15T09:00:00Z") == []
    assert mids("2026-10-15T11:00:00Z") == [[Decimal("15.20"), Decimal("14.70")]]
    assert mids("2026-10-15T15:00:00+03:00") == [[Decimal("15.30"), Decimal("14.80")]]
    latest = _json(seeded.get("/v1/curves/ruonia_ois", headers=AUTH))["data"]
    assert latest[0]["source"] == "ratescalcs"
    assert [q["tenor"] for q in latest[0]["quotes"]] == ["1Y", "2Y"]


def test_zcyc_indices_calendar(seeded: TestClient) -> None:
    z = _json(seeded.get("/v1/zcyc?date=2026-10-15", headers=AUTH))["data"]
    assert len(z) == 1
    assert z[0]["is_eod"] is True
    assert z[0]["params"]["B1"] == Decimal("1506.334188")
    assert z[0]["standard_yields"][0]["yield_pct"] == Decimal("12.4175")
    both = seeded.get("/v1/zcyc?date=2026-10-15&eod_only=false", headers=AUTH).json()["data"]
    assert [x["trade_time"] for x in both] == ["12:00:00", "18:54:59"]

    idx = seeded.get("/v1/indices/eod?secid=RUSFAR,RUSFARCNY&date=2026-10-15", headers=AUTH)
    assert [(r["secid"], r["basis"]) for r in idx.json()["data"]] == [
        ("RUSFAR", "history"),
        ("RUSFARCNY", "marketdata"),
    ]
    cal = seeded.get(
        "/v1/calendar?calendar=MOEX_STOCK&from=2026-10-15&to=2026-10-18", headers=AUTH
    ).json()["data"]
    # latest known_at wins; days without information are absent
    assert [(c["day"], c["is_business_day"], c["source"]) for c in cal] == [
        ("2026-10-15", True, "curated"),
        ("2026-10-17", True, "moex_iss_calendar"),
    ]


def test_export_tdm_zip(seeded: TestClient) -> None:
    r = seeded.get("/v1/export/tdm?date=2026-10-15", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "tdm_2026-10-15.zip" in r.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "manifest.json" in names
    assert "md_bond_quotes_2026-10-15.csv" in names
    bad = seeded.get("/v1/export/tdm?date=2026-10-15&price_basis=mid", headers=AUTH)
    assert bad.status_code == 400
    assert seeded.get("/v1/export/tdm?date=2026-10-15").status_code == 401
