"""Cbonds enrichment: client, parsers, 401 auto-skip and DB writes (respx mocks)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.cbonds_enrich  # noqa: F401 - registers the job
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import cbonds as cb

Conn = psycopg.Connection[DictRow]
BASE = cb.CBONDS_BASE_URL
LOGIN, PASSWORD = "probe-user", "s3cr3t-Pa55"


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        sleep=lambda _s: None,
        max_retries=0,
        rate_limits={"ws2.cbonds.info": 1e9},
        **kw,
    )


@pytest.fixture
def cb_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url, iss_rps=1000.0, cbonds_login=LOGIN, cbonds_password=PASSWORD
    )


@pytest.fixture
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))


def run(settings: Settings, db: Database) -> JobOutcome:
    return run_job("cbonds_enrich", {}, settings=settings, db=db, http_factory=_http_factory)


# ------------------------------------------------------------------------ parsers


def test_normalizers() -> None:
    assert cb.normalize_day_count("Act/365") == "ACT/365F"
    assert cb.normalize_day_count("30/360") == "30/360"
    assert cb.normalize_day_count("Act/Act (ICMA)") == "ACT/ACT ICMA"
    assert cb.normalize_day_count(None) is None
    assert cb.map_benchmark_name("Ключевая ставка Банка России") == "KEY_RATE"
    assert cb.map_benchmark_name("RUONIA") == "RUONIA"
    assert cb.map_benchmark_name("Срочная RUONIA 3M") == "RUONIA_3M"
    assert cb.map_benchmark_name("Индекс потребительских цен") == "OTHER"
    assert cb.map_benchmark_name("Инфляция (ИПЦ)") == "CPI"
    assert cb.normalize_option_type("Пут-опцион") == "put"
    assert cb.normalize_option_type("Колл-опцион") == "call"
    assert cb.normalize_option_type("что-то") == "unknown"
    flow = cb.parse_flow(
        {
            "emission_isin_code": "RU000A10A356",
            "date": "2026-12-21",
            "coupon_num": "12",
            "cupon_rate": "0.1745",
        }
    )
    assert flow["rate_pct"] == Decimal("17.4500") and flow["coupon_no"] == 12
    em = cb.parse_emission({"isin_code": "ru000a10a356", "floating_rate": "1", "margin": "1.95"})
    assert em["isin"] == "RU000A10A356" and em["floating"] is True
    assert em["margin_pct"] == Decimal("1.95")


@respx.mock
def test_query_auth_and_errors() -> None:
    route = respx.post(f"{BASE}/get_emissions/")
    creds = cb.CbondsCredentials(LOGIN, PASSWORD)
    assert LOGIN not in repr(creds) and PASSWORD not in repr(creds)
    with HttpClient(base_url=BASE, limiter=RateLimiter(default_rps=None), max_retries=0) as http:
        route.mock(return_value=httpx.Response(401, text="", headers={"content-type": "text/html"}))
        with pytest.raises(cb.CbondsAuthError) as exc:
            cb.query(http, "get_emissions", creds=creds, filters=[], fields=["id"])
        assert PASSWORD not in str(exc.value) and LOGIN not in str(exc.value)
        body = json.loads(route.calls.last.request.content)
        assert body["auth"] == {"login": LOGIN, "password": PASSWORD}
        assert "password" not in str(route.calls.last.request.url)
        route.mock(return_value=httpx.Response(200, json={"error": {"err_no": 7}}))
        with pytest.raises(cb.CbondsError, match="provider error 7"):
            cb.query(http, "get_emissions", creds=creds, filters=[], fields=["id"])
        route.mock(return_value=httpx.Response(200, json={"items": "x"}))
        with pytest.raises(cb.FormatDriftError):
            cb.query(http, "get_emissions", creds=creds, filters=[], fields=["id"])
        with pytest.raises(ValueError, match="unsupported"):
            cb.query(http, "get_everything", creds=creds, filters=[], fields=["id"])


# ---------------------------------------------------------------------- job: skip


def test_skipped_without_credentials(settings: Settings, db: Database, conn: Conn) -> None:
    out = run(settings, db)
    assert out.status == "skipped"


@respx.mock
def test_skipped_on_401_after_one_probe(cb_settings: Settings, db: Database, conn: Conn) -> None:
    route = respx.post(url__startswith=BASE).mock(
        return_value=httpx.Response(401, text="", headers={"content-type": "text/html"})
    )
    out = run(cb_settings, db)
    assert out.status == "skipped"
    assert route.call_count == 1  # never hammer a rejected account
    row = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert row is not None and "401" in row["error"] and PASSWORD not in row["error"]


# ---------------------------------------------------------------------- job: writes


def seed(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, bond_class, coupon_type, coupon_type_basis, status,
                              maturity_date, coupon_frequency, coupon_benchmark,
                              coupon_benchmark_spread_pct)
        VALUES ('RU000A10A356', 'RU000A10A356', 'corporate', 'floating', 'moex_label', 'active',
                '2030-12-16', 4, 'RREFKEYR', 1.95),
               ('SU26238RMFS4', 'RU000A1038V6', 'ofz_pd', 'fixed', 'moex_label', 'active',
                '2041-05-15', 2, NULL, NULL),
               ('RU000OLD0001', 'RU000OLD0001', 'corporate', 'fixed', 'moex_label', 'matured',
                '2020-01-01', 2, NULL, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_coupon (secid, coupon_no, pay_date, status)
        VALUES ('RU000A10A356', 1, '2026-12-21', 'unknown'),
               ('RU000A10A356', 2, '2027-03-22', 'unknown')
        """
    )
    conn.commit()


def cbonds_api(maturity_b: str = "2041-05-16") -> Callable[[httpx.Request], httpx.Response]:
    emissions = {
        "RU000A10A356": {
            "id": 1001,
            "isin_code": "RU000A10A356",
            "currency_name": "RUB",
            "maturity_date": "2030-12-16",
            "floating_rate": "1",
            "bond_type": "0",
            "cupon_period": "4",
            "reference_rate_name_rus": "Ключевая ставка Банка России",
            "margin": "1.95",
            "floor": "0",
            "cup": None,
            "emission_cupon_basis_title": "Act/365",
            "variable_rate_type_id": "2",
            "floating_rates_day": "3",
            "updating_date": "2026-10-01",
        },
        "RU000A1038V6": {
            "id": 1002,
            "isin_code": "RU000A1038V6",
            "maturity_date": maturity_b,
            "floating_rate": "0",
            "cupon_period": "2",
            "emission_cupon_basis_title": "Act/Act",
        },
    }
    offers = [
        {
            "id": 1,
            "emission_isin_code": "RU000A10A356",
            "date": "2027-06-21",
            "date_open": "2027-06-10",
            "date_close": "2027-06-16",
            "price": "100",
            "option_type_name_rus": "Пут-опцион",
            "status_str": "Ожидается",
        },
    ]
    ratings = [
        {
            "id": 1,
            "emission_isin_code": "RU000A10A356",
            "agency_name_rus": "АКРА",
            "scale_name_rus": "Национальная шкала",
            "scale_point_name": "AA(RU)",
            "forecast_name_rus": "Стабильный",
            "rating_date": "2026-05-01",
        },
    ]
    flows = [
        {"id": 1, "emission_isin_code": "RU000A10A356", "date": "2026-12-21", "coupon_num": "1"},
        {"id": 2, "emission_isin_code": "RU000A10A356", "date": "2027-03-22", "coupon_num": "2"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["auth"] == {"login": LOGIN, "password": PASSWORD}
        op = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        flt = body["filters"][0]
        wanted = set(str(flt["value"]).split(";"))
        if op == "get_emissions":
            items = [v for k, v in emissions.items() if k in wanted]
        elif op == "get_offert":
            items = [o for o in offers if o["emission_isin_code"] in wanted]
        elif op == "get_rating_emission_maxdate":
            items = [r for r in ratings if r["emission_isin_code"] in wanted]
        else:
            items = [f for f in flows if f["emission_isin_code"] in wanted]
        q = body["quantity"]
        page = items[q["offset"] : q["offset"] + q["limit"]]
        return httpx.Response(200, json={"items": page, "total": len(items), "count": len(page)})

    return handler


@respx.mock
def test_enrichment_writes_and_conflicts(
    cb_settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    seed(conn)
    respx.post(url__startswith=BASE).mock(side_effect=cbonds_api())
    out = run(cb_settings, db)
    assert out.status == "succeeded"
    bonds = {r["secid"]: r for r in conn.execute("SELECT * FROM ref.bond").fetchall()}
    assert bonds["RU000A10A356"]["day_count"] == "ACT/365F"
    assert bonds["RU000A10A356"]["day_count_source"] == "cbonds"
    assert bonds["SU26238RMFS4"]["day_count"] == "ACT/ACT"
    assert bonds["RU000OLD0001"]["day_count"] is None  # matured bonds are not requested
    f = conn.execute(
        "SELECT * FROM ref.bond_rate_formula WHERE secid = 'RU000A10A356' AND source = 'cbonds'"
    ).fetchone()
    assert f is not None
    assert (f["benchmark"], f["spread_bp"], f["floor_pct"], f["fixing_day"]) == (
        "KEY_RATE",
        Decimal("195.00"),
        Decimal("0"),
        3,
    )
    assert f["lag_days"] is None and f["status"] == "inferred"
    resolved = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = 'RU000A10A356'"
    ).fetchone()
    assert resolved is not None and resolved["floor_pct"] == Decimal("0")
    rating = conn.execute("SELECT * FROM ref.bond_rating").fetchone()
    assert rating is not None and rating["rating"] == "AA(RU)" and rating["source"] == "cbonds"
    offer = conn.execute("SELECT * FROM ref.bond_offer WHERE source = 'cbonds'").fetchone()
    assert offer is not None and offer["offer_type"] == "put" and offer["offer_key"] == "2027-06-21"
    conflicts = {
        (c["secid"], c["field"]): c
        for c in conn.execute("SELECT * FROM ref.static_conflict").fetchall()
    }
    assert set(conflicts) == {("SU26238RMFS4", "maturity_date"), ("RU000A10A356", "offer_dates")}
    assert conflicts[("SU26238RMFS4", "maturity_date")]["selected_source"] == "moex_iss"
    # credentials never stored
    for cap in conn.execute("SELECT url_redacted, payload_gz FROM ops.raw_capture").fetchall():
        text = cap["url_redacted"] + gzip.decompress(cap["payload_gz"]).decode()
        assert PASSWORD not in text and LOGIN not in text

    again = run(cb_settings, db)
    row = conn.execute("SELECT * FROM ops.job_run WHERE id = %s", (again.run_id,)).fetchone()
    assert row is not None and (row["rows_inserted"], row["rows_updated"]) == (0, 0)

    respx.post(url__startswith=BASE).mock(side_effect=cbonds_api(maturity_b="2041-05-15"))
    assert run(cb_settings, db).status == "succeeded"
    c = conn.execute(
        "SELECT resolved FROM ref.static_conflict WHERE secid = 'SU26238RMFS4'"
    ).fetchone()
    assert c is not None and c["resolved"] is True
    hist = conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE table_name = 'ref.static_conflict'"
    ).fetchone()
    assert hist is not None and hist["n"] == 1


@respx.mock
def test_denied_ratings_preserves_previous_data_and_loads_other_batches_and_flows(
    cb_settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    seed(conn)
    normal = cbonds_api()
    route = respx.post(url__startswith=BASE).mock(side_effect=normal)
    assert run(cb_settings, db).status == "succeeded"
    rating = conn.execute("SELECT * FROM ref.bond_rating WHERE source = 'cbonds'").fetchone()
    calls: list[str] = []

    def denied(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        calls.append(operation)
        if operation == "get_rating_emission_maxdate":
            return httpx.Response(403, text="Forbidden")
        return normal(request)

    route.mock(side_effect=denied)
    out = run_job(
        "cbonds_enrich",
        {"batch_size": 1},
        settings=cb_settings,
        db=db,
        http_factory=_http_factory,
    )
    assert out.status == "partial"
    assert calls.count("get_rating_emission_maxdate") == 1
    assert calls.count("get_emissions") == 3  # probe plus both batches
    assert calls.count("get_offert") == 2
    assert calls.count("get_flow_new") == 1
    assert (
        conn.execute("SELECT * FROM ref.bond_rating WHERE source = 'cbonds'").fetchone() == rating
    )
    row = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert row is not None and "get_rating_emission_maxdate" in row["error"]
    assert "access lost" not in row["error"]


@respx.mock
def test_service_error_after_static_keeps_partial_writes(
    cb_settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    seed(conn)
    normal = cbonds_api()

    def quota(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_flow_new/"):
            return httpx.Response(
                200,
                json={
                    "error": {
                        "err_no": 900000,
                        "err_str": "Max requests per minute limit exceeded",
                    }
                },
            )
        return normal(request)

    route = respx.post(url__startswith=BASE).mock(side_effect=quota)
    out = run(cb_settings, db)
    assert out.status == "partial"
    row = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert row is not None and "Max requests per minute limit exceeded" in row["error"]
    bond = conn.execute("SELECT day_count FROM ref.bond WHERE secid = 'RU000A10A356'").fetchone()
    assert bond is not None and bond["day_count"] == "ACT/365F"
    assert len([c for c in route.calls if c.request.url.path.endswith("/get_flow_new/")]) == 1


@respx.mock
def test_complete_flow_batch_beyond_former_stop_point(
    cb_settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    isins = [f"RU{i:010d}" for i in range(35)]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ref.bond "
            "(secid, isin, bond_class, coupon_type, coupon_type_basis, status) "
            "VALUES (%s, %s, 'corporate', 'floating', 'moex_label', 'active')",
            [(isin, isin) for isin in isins],
        )
    conn.commit()
    flows: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_flow_new/"):
            isin = json.loads(request.content)["filters"][0]["value"]
            flows.append(isin)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "emission_isin_code": isin,
                            "date": "2026-12-21",
                            "coupon_num": "1",
                        }
                    ],
                    "total": 1,
                },
            )
        return httpx.Response(200, json={"items": [], "total": 0})

    respx.post(url__startswith=BASE).mock(side_effect=response)
    out = run(cb_settings, db)
    assert out.status == "succeeded"
    assert flows == sorted(isins)
    row = conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE run_id = %s "
        "AND endpoint = 'cbonds_get_flow_new'",
        (out.run_id,),
    ).fetchone()
    assert row is not None and row["n"] == 35
    row = conn.execute(
        "SELECT count(*) AS n FROM ref.static_snapshot WHERE run_id = %s "
        "AND endpoint = 'cbonds_flows'",
        (out.run_id,),
    ).fetchone()
    assert row is not None and row["n"] == 35
