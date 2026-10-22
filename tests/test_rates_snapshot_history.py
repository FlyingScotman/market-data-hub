"""WEB-16: the new series in the market snapshot (contract 1.3: cross-rate kinds, proxy curve
labels, €STR / SOFR / CPI y/y fixings), the history API and ``mdhub probe-sources``."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub.api.app import create_app
from mdhub.cli import app as cli_app
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.quality.source_check import CHECKS, run_checks
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION, validate_bundle
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
AUTH = {"Authorization": "Bearer tok-rates-1"}
RATES = Path(__file__).parent / "fixtures" / "rates"


def _seed_new_series(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO md.fixing (index_code, fixing_date, source, value, unit, details, ingested_at)
        VALUES ('ESTR', '2026-10-14', 'ecb_data_portal', 2.44, 'pct', NULL, '2026-10-15 08:20Z'),
               ('SOFR', '2026-10-14', 'nyfed', 3.88, 'pct', NULL, '2026-10-15 13:30Z'),
               ('SOFRINDEX', '2026-10-14', 'nyfed', 1.26303308, 'index', NULL,
                '2026-10-15 13:30Z'),
               ('CPI_RU_YOY', '2026-09-01', 'cbr', 6.35, 'pct', '{"period": "2026-09"}',
                '2026-10-15 07:15Z');
        INSERT INTO md.fx_cross (rate_date, base_ccy, quote_ccy, kind, source, rate, details,
            ingested_at)
        VALUES ('2026-10-15', 'EUR', 'USD', 'cbr_cross', 'cbr', 1.11879971,
                '{"EUR_rub": "93.9229", "USD_rub": "83.9497"}', '2026-10-15 06:15Z'),
               ('2026-10-15', 'USD', 'CNY', 'fred_h10', 'fred', 7.1702, '{"series": "DEXCHUS"}',
                '2026-10-15 06:15Z');
        INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,
            content_sha256, parser_version, label, meta, ingested_at)
        VALUES ('us_treasury', 'usd_ust_par', 'mid_only', '2026-10-15 20:59:59Z', '2026-10-15',
                repeat('a', 64), '1', 'U.S. Treasury par yield curve (proxy, not OIS)',
                '{"proxy": "ust_par", "not_ois": true}', '2026-10-15 21:00Z');
        INSERT INTO md.curve_quote (snapshot_id, tenor, position, tenor_months, mid_pct)
        SELECT id, '10Y', 0, 120, 5.12 FROM md.curve_snapshot WHERE dataset = 'usd_ust_par';
        """
    )
    conn.commit()


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def test_market_snapshot_1_3(conn: Conn, db: Database) -> None:
    seed(conn)
    _seed_new_series(conn)
    nxt = DAY + timedelta(days=1)
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=DAY,
        cut_ts=datetime(nxt.year, nxt.month, nxt.day, 8, 0, tzinfo=MSK),
        known_at=utc_now(),
    )
    doc = _bundle(conn, out.market.id)
    validate_bundle(doc)
    assert doc["schema_version"] == SCHEMA_VERSION  # 1.3 data (SOFR, ESTR...) in later minors
    assert int(SCHEMA_VERSION.rsplit(".", 1)[1]) >= 3  # the 1.3 rules hold in later minors
    data = doc["data"]
    fix = {(f["index_code"], f["source"]): f for f in data["fixings"]}
    assert fix[("ESTR", "ecb_data_portal")]["value"] == "2.44"
    assert fix[("SOFRINDEX", "nyfed")]["unit"] == "index"
    assert fix[("CPI_RU_YOY", "cbr")]["unit"] == "pct"
    cross = {(c["kind"], c["base_ccy"], c["ccy"]): c for c in data["cross_rates"]}
    cbr = cross[("cbr_cross", "EUR", "USD")]
    assert cbr["rate_per_base"] == "1.11879971" and cbr["source"] == "cbr"
    assert cbr["labels"] == ["derived_cross"]
    assert cross[("fred_h10", "USD", "CNY")]["labels"] == []
    assert any(k == "ecb" for k, _, _ in cross)  # the ECB rows of the seed stay
    keys = [(c["kind"], c["rate_date"], c["base_ccy"], c["ccy"]) for c in data["cross_rates"]]
    assert keys == sorted(keys)
    ds = {d["name"]: d for d in doc["datasets"]}
    assert ds["cross_rates"]["coverage"]["latest_by_kind"]["cbr_cross"] == "2026-10-15"
    ust = next(s for s in data["curve_quote_sets"] if s["dataset"] == "usd_ust_par")
    assert ust["labels"] == ["mid_only", "proxy_ust_par"]
    assert ust["quotes"][0]["mid_pct"] == "5.12"
    assert "us_treasury/usd_ust_par" not in ds["curve_quote_sets"]["coverage"]["missing"]


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="tok-rates-1")
    )
    with TestClient(app) as c:
        yield c


def _records(client: TestClient, path: str, **params: Any) -> list[dict[str, Any]]:
    r = client.get(path, params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    body = json.loads(r.content, parse_float=Decimal)
    return [dict(zip(body["columns"], row, strict=True)) for row in body["rows"]]


def test_history_api_serves_the_new_series(conn: Conn, client: TestClient) -> None:
    seed(conn)
    _seed_new_series(conn)
    fx = _records(client, "/v1/history/fx", **{"from": "2026-10-15", "kind": "cbr_cross,ecb"})
    kinds = {(r["kind"], r["pair"]) for r in fx}
    assert ("cbr_cross", "EUR/USD") in kinds and ("ecb", "EUR/USD") in kinds
    cbr = next(r for r in fx if r["kind"] == "cbr_cross")
    assert cbr["rate"] == Decimal("1.11879971") and cbr["base_ccy"] == "EUR"
    one = _records(
        client, "/v1/history/fx", **{"from": "2026-10-15", "kind": "fred_h10", "pair": "USD/CNY"}
    )
    assert [(r["kind"], r["rate"]) for r in one] == [("fred_h10", Decimal("7.1702"))]
    known = _records(
        client,
        "/v1/history/fx",
        **{"from": "2026-10-15", "kind": "cbr_cross", "known_at": "2026-10-15T06:00:00Z"},
    )
    assert known == []  # stored at 06:15Z: not known at 06:00Z
    fixings = _records(client, "/v1/history/fixings", **{"from": "2026-10-01", "code": "ESTR,SOFR"})
    assert [(r["index_code"], r["source"]) for r in fixings] == [
        ("ESTR", "ecb_data_portal"),
        ("SOFR", "nyfed"),
    ]
    curves = _records(client, "/v1/history/curves/usd_ust_par", **{"from": "2026-10-15"})
    assert curves[0]["labels"] == ["mid_only", "proxy_ust_par"]
    assert curves[0]["meta"]["proxy"] == "ust_par"
    index = _records(client, "/v1/history/index")
    series = {(r["dataset"], r["series"], r["unit"]) for r in index}
    assert ("fx", "EUR/USD", "cbr_cross") in series
    assert ("fixings", "ESTR", "pct") in series and ("curves", "usd_ust_par", "pct") in series
    assert ("fx", "EUR/USD", "ecb") in series


# ------------------------------------------------------------------- probe-sources


def _mock_all(router: respx.MockRouter) -> None:
    def ok(name: str, ctype: str = "text/plain") -> httpx.Response:
        return httpx.Response(
            200, content=(RATES / name).read_bytes(), headers={"content-type": ctype}
        )

    router.route(host="data-api.ecb.europa.eu", path__regex="/EST/").mock(
        return_value=ok("ecb_est_2026-09-28_2026-10-20.csv")
    )
    router.route(host="data-api.ecb.europa.eu", path__regex="/EXR/").mock(
        return_value=ok("ecb_exr_2026-10-16_2026-10-20.csv")
    )
    router.route(host="data-api.ecb.europa.eu", path__regex="/YC/").mock(
        return_value=ok("ecb_yc_py_2026-10-15_2026-10-20.csv")
    )
    router.route(host="api.statistiken.bundesbank.de").mock(
        side_effect=httpx.ConnectError("geo-blocked")
    )
    router.route(host="api.db.nomics.world").respond(403)
    router.route(host="fred.stlouisfed.org").mock(side_effect=httpx.ConnectTimeout("slow"))
    router.route(host="webstat.banque-france.fr").respond(200, text="series_key;obs_value\n")
    router.route(host="markets.newyorkfed.org", path__regex="/sofr/").mock(
        return_value=ok("nyfed_sofr_2026-09-28_2026-10-20.json", "application/json")
    )
    router.route(host="markets.newyorkfed.org", path__regex="/sofrai/").mock(
        return_value=ok("nyfed_sofrai_2026-09-28_2026-10-20.json", "application/json")
    )
    router.route(host="www.ecb.europa.eu").mock(side_effect=httpx.ConnectError("geo-blocked"))
    router.route(host="data.snb.ch").mock(return_value=ok("snb_saron_synthetic.csv", "text/csv"))
    official = Path(__file__).parent / "fixtures" / "official"
    router.get("https://www.cbr.ru/scripts/XML_daily.asp").respond(
        200, content=(official / "cbr_xml_daily_20261016.xml").read_bytes()
    )
    router.route(host="www.cbr.ru", path__regex="/hd_base/infl/").respond(
        200, text=(official / "cbr_infl_2024-01_2026-09.html").read_text("utf-8")
    )
    router.route(host="iss.moex.com", path__regex="indicativerates").respond(
        200, json={"securities": {"columns": ["a", "b", "secid"], "data": [[1, 2, "USD/RUB"]]}}
    )
    router.route(host="iss.moex.com", path__regex="/history/").respond(
        200, json={"history": {"columns": ["TRADEDATE", "CLOSE", "NUMTRADES"], "data": []}}
    )
    router.route(host="home.treasury.gov").mock(return_value=ok("ust_par_202610.xml"))
    rosstat_dir = Path(__file__).parent / "fixtures" / "rosstat"
    router.get("https://rosstat.gov.ru/statistics/price").respond(
        200, text=(rosstat_dir / "price_page_excerpt.html").read_text("utf-8")
    )
    router.get("https://rosstat.gov.ru/storage/mediabank/ipc_mes_09-2026.xlsx").respond(
        200, content=(rosstat_dir / "ipc_mes_09-2026.xlsx").read_bytes()
    )


def _factory(**kw: Any) -> HttpClient:
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


def test_probe_sources_reports_every_source() -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_all(router)
        results = run_checks(Settings(), today=date(2026, 10, 21), http_factory=_factory)
    assert len(results) == len(CHECKS)
    by = {(r.source, r.dataset): r for r in results}
    estr = by[("ecb_data_portal", "estr")]
    assert estr.ok and estr.http_status == 200 and "latest 2026-10-20" in estr.result
    assert "= 2.44 %" in estr.result and estr.size_bytes and estr.latency_ms >= 0
    assert not by[("bundesbank", "estr")].ok and "ConnectError" in by[("bundesbank", "estr")].result
    assert by[("dbnomics", "estr")].http_status == 403 and not by[("dbnomics", "estr")].ok
    assert "ConnectTimeout" in by[("fred", "sofr")].result
    assert by[("banque_de_france", "estr")].ok
    assert by[("nyfed", "sofr")].result.endswith("SOFR = 3.88")
    assert by[("us_treasury", "proxy_usd_ust_par")].result.endswith("10Y = 5.12")
    assert "released 2026-10-09" in by[("rosstat", "rosstat_cpi")].result
    assert by[("cbr", "cbr_cpi_yoy")].result == "latest 2026-09 y/y = 6.35 %"
    assert not by[("ecb", "ecb_fx")].ok and by[("ecb_data_portal", "ecb_fx")].ok
    assert by[("snb", "saron")].ok and by[("snb", "saron")].result.endswith("= -0.05 %")


def test_probe_sources_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import mdhub.quality.source_check as sc

    monkeypatch.setattr(sc, "HttpClient", _factory)
    real = sc.run_checks
    monkeypatch.setattr(
        "mdhub.cli_ext.probe_sources.run_checks",
        lambda s, **kw: real(s, http_factory=_factory, today=date(2026, 10, 21), **kw),
    )
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as router:
        _mock_all(router)
        table = runner.invoke(cli_app, ["probe-sources", "-s", "nyfed", "-s", "ecb_data_portal"])
        assert table.exit_code == 0, table.output
        assert table.output.splitlines()[0].split()[:4] == ["source", "dataset", "role", "ok"]
        assert "SOFR = 3.88" in table.output
        js = runner.invoke(cli_app, ["probe-sources", "--json", "-s", "ecb"])
        assert js.exit_code == 1  # a primary source failed
        line = json.loads(js.output.splitlines()[0])
        assert line["source"] == "ecb" and line["ok"] is False
        bad = runner.invoke(cli_app, ["probe-sources", "-s", "nope"])
        assert bad.exit_code == 2
