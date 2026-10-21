"""WEB-16: €STR / SOFR fixings, cross rates, proxy curves, CPI y/y and publication dates -
parsers on recorded (and documented-format) fixtures, loaders with their fallback chains on a
real PostgreSQL. No network: every request is mocked with respx (tests/fixtures/rates/README.md
says which fixtures are recorded and which follow a documented format)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.ecb
import mdhub.loaders.proxy_curves
import mdhub.loaders.ref_rates  # noqa: F401 - registers the jobs
from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, JobParamError, run_job
from mdhub.loaders._fallback import SourcesFailed
from mdhub.loaders.fx_cross import cross
from mdhub.loaders.rosstat import chained_yoy
from mdhub.sources import (
    bundesbank,
    cbr,
    dbnomics,
    ecb,
    ecb_data,
    fred,
    nyfed,
    rosstat,
    us_treasury,
)

FIX = Path(__file__).parent / "fixtures"
RATES = FIX / "rates"
Conn = psycopg.Connection[DictRow]


def _b(name: str) -> bytes:
    return (RATES / name).read_bytes()


ECB_EST = _b("ecb_est_2026-09-28_2026-10-20.csv")
ECB_EXR = _b("ecb_exr_2026-10-16_2026-10-20.csv")
ECB_YC = _b("ecb_yc_py_2026-10-15_2026-10-20.csv")
BBK_EST = _b("bundesbank_bbmmb_estr_2026-09-28_2026-10-20.csv")
BBK_PAR = _b("bundesbank_bbsis_par_synthetic.csv")
DBN_EST = _b("dbnomics_ecb_est.json")
FRED_EST = _b("fred_estr_2026-09-28_2026-10-20.csv")
FRED_SOFR = _b("fred_sofr_2026-09-28_2026-10-20.csv")
NYFED_SOFR = _b("nyfed_sofr_2026-09-28_2026-10-20.json")
NYFED_SOFRAI = _b("nyfed_sofrai_2026-09-28_2026-10-20.json")
NYFED_FIRST = _b("nyfed_sofr_2018-04_first.json")
UST = _b("ust_par_202610.xml")
CBR_INFL = (FIX / "official" / "cbr_infl_2024-01_2026-09.html").read_text("utf-8")
WORKBOOK = (FIX / "rosstat" / "ipc_mes_09-2026.xlsx").read_bytes()
PRICE_PAGE = (FIX / "rosstat" / "price_page_excerpt.html").read_text("utf-8")


# ------------------------------------------------------------------------- parsers


def test_ecb_estr_csv() -> None:
    obs = ecb_data.estr_from_csv(ECB_EST)
    assert len(obs) == 17 and {o.series for o in obs} == {ecb_data.ESTR_SERIES}
    by = {o.day: o.value for o in obs}
    assert by[date(2026, 10, 20)] == Decimal("2.44") and by[date(2026, 9, 29)] == Decimal("2.439")
    assert date(2026, 10, 17) not in by  # Saturday: absent, not zero
    assert ecb_data.parse_csvdata(b"", "EST") == []  # empty range: no data


def test_ecb_exr_and_yc_csv() -> None:
    exr = ecb_data.exr_from_csv(ECB_EXR)
    usd = {o.day: o.value for o in exr if o.series == "USD"}
    assert usd and all(v > 1 for v in usd.values())
    yc = ecb_data.yc_from_csv(ECB_YC)
    assert {o.series for o in yc} == set(ecb_data.YC_PAR_TENORS)
    assert {o.value for o in yc if o.series == "10Y" and o.day == date(2026, 10, 20)} == {
        Decimal("3.409065995")
    }


def test_ecb_csv_drift_fails_closed() -> None:
    with pytest.raises(FormatDriftError, match="header"):
        ecb_data.parse_csvdata(b"SERIES,DATE,VALUE\nx,2026-01-01,1\n", "EST")
    foreign = ECB_EST.replace(b"EST.B.EU000A2X2A25.WT,B", b"EST.B.EU000A2X2A25.CR,B", 1)
    with pytest.raises(FormatDriftError, match="unexpected series"):
        ecb_data.estr_from_csv(foreign)
    with pytest.raises(FormatDriftError, match="out of range"):
        ecb_data.estr_from_csv(ECB_EST.replace(b",2.44\r\n", b",244\r\n", 1))


def test_estr_mirrors_agree_with_the_ecb() -> None:
    ecb_values = {o.day: o.value for o in ecb_data.estr_from_csv(ECB_EST)}
    bbk = {o.day: o.value for o in bundesbank.estr_from_csv(BBK_EST)}
    dbn = {o.day: o.value for o in dbnomics.estr_from_json(DBN_EST)}
    fr = {o.day: o.value for o in fred.parse_csv(FRED_EST, (fred.ESTR_SERIES,))}
    assert bbk == ecb_values == dbn  # weekends ("."), "NA" skipped
    assert date(2026, 10, 2) not in fr and len(fr) == 16  # empty FRED field = no value
    assert all(fr[d] == v for d, v in ecb_values.items() if d in fr)


def test_bundesbank_csv_layouts() -> None:
    german = (
        b'\xef\xbb\xbf"";BBMMB.D.EU000A2X2A25.WT;BBMMB.D.EU000A2X2A25.WT_FLAGS\r\n'
        b'"";Euro short-term rate;\r\nStand vom;2026-10-21;\r\n'
        b"2026-10-19;2,441;\r\n2026-10-20;.;Kein Wert vorhanden\r\n"
    )
    obs = bundesbank.estr_from_csv(german)
    assert [(o.day, o.value) for o in obs] == [(date(2026, 10, 19), Decimal("2.441"))]
    par = bundesbank.par_curve_from_csv(BBK_PAR)
    assert {o.series for o in par} == {"1Y", "2Y", "5Y", "10Y", "30Y"}
    assert len(par) == 10  # weekend days are "."
    with pytest.raises(FormatDriftError, match="no data lines"):
        bundesbank.parse_csv(b'"",BBMMB.D.X\r\nlast update,\r\n', "BBMMB")


def test_fred_csv() -> None:
    obs = fred.parse_csv(FRED_SOFR, (fred.SOFR_SERIES, *fred.SOFR_AVERAGES))
    idx = [o for o in obs if o.series == "SOFRINDEX"]
    assert idx[-1].value == Decimal("1.26357728")
    noisy = b"DATE,ECBESTRVOLWGTTRMDMNRT\n2019-10-01,-0.54899999999999993\n2019-10-02,.\n"
    assert fred.parse_csv(noisy, (fred.ESTR_SERIES,))[0].value == Decimal("-0.549")
    with pytest.raises(FormatDriftError, match="unexpected header"):
        fred.parse_csv(FRED_SOFR, (fred.SOFR_SERIES,))


def test_nyfed_sofr_and_averages() -> None:
    rates = nyfed.parse_sofr(NYFED_SOFR)
    last = rates[-1]
    assert last.day == date(2026, 10, 20) and last.value == Decimal("3.88")
    assert last.details["volumeInBillions"] == "2925" and not last.revised
    first = nyfed.parse_sofr(NYFED_FIRST)
    assert first[0].day == date(2018, 4, 2) and first[0].value == Decimal("1.80")
    ai = nyfed.parse_sofrai(NYFED_SOFRAI)
    assert {r.index_code for r in ai} == {"SOFR30DAVG", "SOFR90DAVG", "SOFR180DAVG", "SOFRINDEX"}
    index = [r for r in ai if r.index_code == "SOFRINDEX"]
    assert index[-1].unit == "index" and index[-1].value == Decimal("1.26357728")
    with pytest.raises(FormatDriftError):
        nyfed.parse_sofr(NYFED_SOFRAI)  # type SOFRAI is not SOFR


def test_treasury_par_xml() -> None:
    curves = us_treasury.parse_xml(UST)
    assert len(curves) == 13 and curves[-1].day == date(2026, 10, 20)
    q = dict(curves[-1].quotes)
    assert q["BC_10YEAR"] == Decimal("5.12") and q["BC_1_5MONTH"] == Decimal("4.13")
    assert "BC_30YEARDISPLAY" not in q
    with pytest.raises(FormatDriftError, match="unknown tenor"):
        us_treasury.parse_xml(UST.replace(b"BC_7YEAR", b"BC_8YEAR"))


def test_cbr_inflation_page() -> None:
    rows = cbr.parse_inflation_page(CBR_INFL)
    assert len(rows) == 33 and rows[-1].period == date(2026, 9, 1)
    assert rows[-1].inflation_yoy_pct == Decimal("6.35") and rows[-1].target_pct == Decimal("4.00")
    with pytest.raises(FormatDriftError, match="header"):
        cbr.parse_inflation_page(CBR_INFL.replace("Инфляция, % г/г", "Инфляция, % м/м"))


def test_rosstat_release_dates() -> None:
    path = rosstat.find_cpi_workbook(PRICE_PAGE)
    assert path == "/storage/mediabank/ipc_mes_09-2026.xlsx"
    assert rosstat.page_release_date(PRICE_PAGE, path) == date(2026, 10, 9)
    assert rosstat.page_release_date(PRICE_PAGE, "/storage/mediabank/none.xlsx") is None
    assert rosstat.workbook_release_date(WORKBOOK) == date(2026, 10, 9)


def test_cross_rounding_and_chain() -> None:
    assert cross(Decimal("93.9229"), Decimal("83.9497")) == Decimal("1.118799710")
    assert cross(Decimal("83.9497"), Decimal("0.52985")) == Decimal("158.4405020")
    moms = {m.period: m.index_pct for m in rosstat.parse_cpi_monthly(WORKBOOK)}
    chained = chained_yoy(moms, date(2026, 9, 1))
    assert chained is not None and abs(chained - Decimal("6.35")) < Decimal("0.01")
    assert chained_yoy(moms, date(1991, 6, 1)) is None


# ---------------------------------------------------------------------------- jobs


def _http_factory(**kw: Any) -> HttpClient:
    kw["max_retries"] = 0
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


def _run(job: str, params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job(job, params, settings=settings, db=db, http_factory=_http_factory)


@pytest.fixture
def web() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _fixings(conn: Conn, code: str) -> list[dict[str, Any]]:
    conn.rollback()
    return conn.execute(
        "SELECT * FROM md.fixing WHERE index_code = %s ORDER BY fixing_date, source", (code,)
    ).fetchall()


def test_estr_primary_then_fallback(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    ecb_route = web.route(host="data-api.ecb.europa.eu").respond(200, content=ECB_EST)
    web.route(host="api.statistiken.bundesbank.de").respond(200, content=BBK_EST)
    out = _run("estr_fixing", {"date_from": "2026-09-28", "date_to": "2026-10-20"}, settings, db)
    assert out.status == "succeeded" and out.result["source"] == "ecb_data_portal"
    assert out.result["tried"] == [{"source": "ecb_data_portal", "ok": True, "error": None}]
    rows = _fixings(conn, "ESTR")
    assert len(rows) == 17 and {r["source"] for r in rows} == {"ecb_data_portal"}
    assert rows[-1]["value"] == Decimal("2.44") and rows[-1]["unit"] == "pct"
    assert rows[-1]["details"]["series"] == ecb_data.ESTR_SERIES
    assert rows[-1]["capture_id"] is not None
    assert out.result["no_data"] == []  # every TARGET weekday of the range has a value

    # the ECB does not answer: the Bundesbank republication serves, under its own source code
    ecb_route.mock(side_effect=httpx.ConnectError("blocked"))
    out = _run("estr_fixing", {"date_from": "2026-10-19", "date_to": "2026-10-20"}, settings, db)
    assert out.status == "succeeded" and out.result["source"] == "bundesbank"
    assert [t["source"] for t in out.result["tried"]] == ["ecb_data_portal", "bundesbank"]
    assert "ConnectError" in out.result["tried"][0]["error"]
    rows = _fixings(conn, "ESTR")
    assert {(r["fixing_date"], r["source"]) for r in rows if r["source"] == "bundesbank"} == {
        (date(2026, 10, 19), "bundesbank"),
        (date(2026, 10, 20), "bundesbank"),
    }
    caps = conn.execute(
        "SELECT source, endpoint FROM ops.raw_capture WHERE run_id = %s", (out.run_id,)
    ).fetchall()
    assert [(c["source"], c["endpoint"]) for c in caps] == [("bundesbank", "bbmmb_estr")]


def test_estr_skip_sources_order_and_all_failing(
    web: respx.MockRouter,
    db_url: str,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    settings = Settings(database_url=db_url, skip_sources="ecb_data_portal, bundesbank")
    ecb_route = web.route(host="data-api.ecb.europa.eu").respond(200, content=ECB_EST)
    web.route(host="fred.stlouisfed.org").mock(side_effect=httpx.ConnectError("blocked"))
    web.route(host="api.db.nomics.world").respond(
        200, content=DBN_EST, headers={"content-type": "application/json"}
    )
    out = _run("estr_fixing", {"date_from": "2026-10-01", "date_to": "2026-10-20"}, settings, db)
    assert out.status == "succeeded" and out.result["source"] == "dbnomics"
    assert not ecb_route.called  # skipped, no timeout spent
    # an explicit order wins over the skip list
    out = _run(
        "estr_fixing",
        {"date_from": "2026-10-19", "date_to": "2026-10-20", "sources": "fred,ecb_data_portal"},
        settings,
        db,
    )
    assert out.result["source"] == "ecb_data_portal"  # fred is not mocked: falls through
    assert out.result["tried"][0]["source"] == "fred" and not out.result["tried"][0]["ok"]
    with pytest.raises(JobParamError, match="unknown source"):
        _run("estr_fixing", {"sources": "nonsense"}, settings, db)
    web.route(host="api.db.nomics.world").respond(503)
    with pytest.raises(SourcesFailed, match="no source answered"):
        _run("estr_fixing", {"sources": "dbnomics,fred"}, settings, db)
    run = conn.execute("SELECT status, error FROM ops.job_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run is not None and run["status"] == "failed"
    assert "dbnomics: HttpStatusError: HTTP 503" in run["error"]


def test_estr_no_data_day_is_recorded(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    without_oct_14 = b"\n".join(
        line for line in ECB_EST.split(b"\n") if b",2026-10-14," not in line
    )
    web.route(host="data-api.ecb.europa.eu").respond(200, content=without_oct_14)
    out = _run("estr_fixing", {"date_from": "2026-10-12", "date_to": "2026-10-20"}, settings, db)
    assert out.result["no_data"] == ["estr:2026-10-14"]
    gap = conn.execute(
        "SELECT status FROM ops.data_gap WHERE dataset = 'estr' AND gap_date = '2026-10-14'"
    ).fetchone()
    assert gap is not None and gap["status"] == "unfillable"  # a TARGET day has passed since


def test_estr_default_range_seeds_then_looks_back(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    route = web.route(host="data-api.ecb.europa.eu").respond(200, content=ECB_EST)
    first = _run("estr_fixing", {}, settings, db)
    assert first.result["range"] == ["2019-10-01", "2026-10-21"]  # empty series: history start
    again = _run("estr_fixing", {}, settings, db)
    assert again.result["range"] == ["2026-10-11", "2026-10-21"]
    assert again.result["inserted"] == 0 and again.result["updated"] == 0  # idempotent
    assert route.calls.last.request.url.params["startPeriod"] == "2026-10-11"


def test_sofr_nyfed_then_fred(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    sofr = web.get(f"{nyfed.BASE_URL}/sofr/search.json").respond(
        200, content=NYFED_SOFR, headers={"content-type": "application/json"}
    )
    web.get(f"{nyfed.BASE_URL}/sofrai/search.json").respond(
        200, content=NYFED_SOFRAI, headers={"content-type": "application/json"}
    )
    out = _run("sofr_fixing", {"date_from": "2026-09-28", "date_to": "2026-10-20"}, settings, db)
    assert out.status == "succeeded" and out.result["source"] == "nyfed"
    rows = _fixings(conn, "SOFR")
    assert len(rows) == 16 and rows[-1]["value"] == Decimal("3.88")
    assert rows[-1]["details"]["percentPercentile99"] == "3.96"
    idx = _fixings(conn, "SOFRINDEX")
    assert idx[-1]["unit"] == "index" and idx[-1]["value"] == Decimal("1.26357728")
    caps = {
        c["endpoint"]: c["id"]
        for c in conn.execute(
            "SELECT id, endpoint FROM ops.raw_capture WHERE run_id = %s", (out.run_id,)
        ).fetchall()
    }
    assert rows[-1]["capture_id"] == caps["sofr"] and idx[-1]["capture_id"] == caps["sofrai"]
    # 2026-10-12 (Columbus Day) has no SOFR: recorded as source-confirmed no data
    assert "sofr:2026-10-12" in out.result["no_data"]

    sofr.respond(503)
    web.route(host="fred.stlouisfed.org").respond(200, content=FRED_SOFR)
    out = _run("sofr_fixing", {"date_from": "2026-10-19", "date_to": "2026-10-20"}, settings, db)
    assert out.status == "succeeded" and out.result["source"] == "fred"
    fred_rows = [r for r in _fixings(conn, "SOFR30DAVG") if r["source"] == "fred"]
    assert [r["fixing_date"] for r in fred_rows] == [date(2026, 10, 19), date(2026, 10, 20)]
    assert fred_rows[-1]["details"]["series"] == "SOFR30DAYAVG"


def test_ecb_fx_falls_back_to_the_data_portal(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    conn.execute(
        "INSERT INTO md.ecb_fx (rate_date, ccy, rate_per_eur) VALUES ('2026-10-15', 'USD', 1.12)"
    )
    conn.commit()
    web.get(ecb.DAILY_URL).mock(side_effect=httpx.ConnectTimeout("blocked"))
    web.route(host="data-api.ecb.europa.eu").respond(200, content=ECB_EXR)
    out = _run("ecb_fx", {}, settings, db)
    assert out.status == "succeeded" and out.result["source"] == "ecb_data_portal"
    assert out.result["primary_error"].startswith("TransportFailure")
    row = conn.execute(
        "SELECT r.rate_per_eur, c.source, c.endpoint FROM md.ecb_fx r "
        "JOIN ops.raw_capture c ON c.id = r.capture_id "
        "WHERE r.rate_date = '2026-10-20' AND r.ccy = 'USD'"
    ).fetchone()
    assert row is not None and (row["source"], row["endpoint"]) == ("ecb_data_portal", "exr_daily")


def _seed_fx_inputs(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO md.fx_official (effective_date, ccy, nominal, value, rate_per_unit,
            record_date, set_date, cbr_id) VALUES
          ('2026-10-16', 'USD', 1, 81.9739, 81.9739, '2026-10-16', '2026-10-15', 'R01235'),
          ('2026-10-16', 'EUR', 1, 95.0001, 95.0001, '2026-10-16', '2026-10-15', 'R01239'),
          ('2026-10-16', 'CNY', 1, 11.4999, 11.4999, '2026-10-16', '2026-10-15', 'R01375'),
          ('2026-10-17', 'USD', 1, 81.9739, 81.9739, '2026-10-16', '2026-10-15', 'R01235');
        INSERT INTO md.fx_indicative (trade_date, clearing, ccy_pair, base_ccy, quote_ccy,
            trade_time, rate) VALUES
          ('2026-10-15', 'mc', 'USD/RUB', 'USD', 'RUB', '19:00', 83.76),
          ('2026-10-15', 'mc', 'EUR/RUB', 'EUR', 'RUB', '19:00', 93.7693),
          ('2026-10-15', 'tc', 'GBP/RUB', 'GBP', 'RUB', '14:00', 111.225);
        INSERT INTO ref.fx_instrument (secid, board, kind, base_ccy, quote_ccy, tenor, face_value,
            first_seen)
        VALUES ('USDCNY_TOM', 'CETS', 'outright', 'USD', 'CNY', 'TOM', 1, '2024-01-03'),
               ('EURUSD000TOM', 'CETS', 'outright', 'EUR', 'USD', 'TOM', 1, '2024-01-03');
        INSERT INTO md.fx_eod (trade_date, secid, board, close, num_trades) VALUES
          ('2024-06-11', 'USDCNY_TOM', 'CETS', 7.2888, 12160),
          ('2024-06-11', 'EURUSD000TOM', 'CETS', NULL, 0);
        """
    )
    conn.commit()


def test_fx_cross_kinds(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    _seed_fx_inputs(conn)
    h10 = (
        b"observation_date,DEXUSEU,DEXUSUK,DEXCHUS,DEXJPUS,DEXSZUS,DEXHKUS,DEXINUS\n"
        b"2026-10-15,1.1203,1.3301,7.1702,150.21,0.8001,7.7702,88.12\n2026-10-16,,,,,,,\n"
    )
    web.route(host="fred.stlouisfed.org").respond(200, content=h10)
    out = _run("fx_cross", {"date_from": "2024-06-01", "date_to": "2026-10-17"}, settings, db)
    assert out.status == "succeeded" and out.result["fred_error"] is None
    assert out.result["rows_by_kind"] == {
        "cbr_cross": 3,  # EUR/USD, USD/CNY, EUR/CNY on 10-16; 10-17 has USD only
        "fred_h10": 7,
        "moex_cets": 1,  # the day with trades only
        "moex_indicative_cross": 1,  # EUR/USD from the 19:00 rates (GBP has only 14:00)
    }
    rows = {
        (r["kind"], r["base_ccy"], r["quote_ccy"]): r
        for r in conn.execute("SELECT * FROM md.fx_cross").fetchall()
    }
    eurusd = rows[("cbr_cross", "EUR", "USD")]
    assert eurusd["rate"] == cross(Decimal("95.0001"), Decimal("81.9739"))
    assert eurusd["source"] == "cbr" and eurusd["capture_id"] is None
    assert eurusd["details"]["EUR_rub"] == "95.0001" and eurusd["rate_date"] == date(2026, 10, 16)
    assert rows[("moex_cets", "USD", "CNY")]["rate"] == Decimal("7.2888")
    assert rows[("fred_h10", "EUR", "USD")]["rate"] == Decimal("1.1203")
    assert rows[("fred_h10", "USD", "CNY")]["capture_id"] is not None
    # days whose CBR inputs are not stored: source-confirmed no data (no endless backfill)
    assert "cbr_cross:2026-10-14" in out.result["no_data"]
    assert "cbr_cross:2026-10-16" not in out.result["no_data"]
    again = _run("fx_cross", {"date_from": "2024-06-01", "date_to": "2026-10-17"}, settings, db)
    assert again.result["inserted"] == 0 and again.result["updated"] == 0

    web.route(host="fred.stlouisfed.org").mock(side_effect=httpx.ConnectError("blocked"))
    out = _run("fx_cross", {}, settings, db)
    assert out.status == "succeeded" and out.result["fred_error"].startswith("TransportFailure")


def test_proxy_curves_usd_and_eur_fallback(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    ust = web.route(host="home.treasury.gov").respond(200, content=UST)
    web.route(host="data-api.ecb.europa.eu").respond(200, content=ECB_YC)
    out = _run("proxy_curves", {"date_from": "2026-10-01", "date_to": "2026-10-20"}, settings, db)
    assert out.status == "succeeded"
    assert out.result["usd"]["days"] == 13 and out.result["eur"]["days"] == 4
    assert ust.calls.last.request.url.params["field_tdr_date_value_month"] == "202610"
    snap = conn.execute(
        "SELECT * FROM md.curve_snapshot WHERE dataset = 'usd_ust_par' "
        "ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchone()
    assert snap is not None and snap["snapshot_date"] == date(2026, 10, 20)
    assert snap["source"] == "us_treasury" and snap["quote_basis"] == "mid_only"
    assert snap["meta"]["proxy"] == "ust_par" and snap["meta"]["not_ois"] is True
    assert snap["fetched_at"].isoformat() == "2026-10-20T20:59:59+00:00"  # 23:59:59 MSK
    quotes = conn.execute(
        "SELECT tenor, mid_pct, tenor_months, tenor_days FROM md.curve_quote "
        "WHERE snapshot_id = %s ORDER BY position",
        (snap["id"],),
    ).fetchall()
    assert [q["tenor"] for q in quotes][:3] == ["1M", "6W", "2M"]
    assert quotes[1]["tenor_days"] == 42 and quotes[-1]["tenor_months"] == 360
    assert {q["tenor"]: q["mid_pct"] for q in quotes}["10Y"] == Decimal("5.12")
    # a Treasury revision of a day updates the snapshot in place, old quote in row history
    web.route(host="home.treasury.gov").respond(
        200, content=UST.replace(b">5.12</d:BC_10YEAR>", b">5.13</d:BC_10YEAR>", 1)
    )
    rev = _run(
        "proxy_curves",
        {"date_from": "2026-10-01", "date_to": "2026-10-20", "curves": "usd"},
        settings,
        db,
    )
    assert rev.status == "succeeded" and rev.result["usd"]["snapshots_updated"] >= 1
    hist = conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE table_name = 'md.curve_quote'"
    ).fetchone()
    assert hist is not None and hist["n"] >= 1

    # ECB down: the Bundesbank curve serves as its own dataset
    web.route(host="data-api.ecb.europa.eu").respond(503)
    web.route(host="api.statistiken.bundesbank.de").respond(200, content=BBK_PAR)
    eur = _run(
        "proxy_curves",
        {"date_from": "2026-10-17", "date_to": "2026-10-20", "curves": "eur"},
        settings,
        db,
    )
    assert eur.status == "succeeded"
    assert eur.result["eur"]["source"] == "bundesbank"
    assert eur.result["eur"]["dataset"] == "eur_bund_par"
    # both curves failing fails the run
    web.route(host="home.treasury.gov").respond(503)
    web.route(host="api.statistiken.bundesbank.de").respond(503)
    with pytest.raises(SourcesFailed):
        _run("proxy_curves", {}, settings, db)


def test_rosstat_published_on_and_cbr_yoy(
    web: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 21))
    web.get(rosstat.BASE_URL + rosstat.PRICE_PAGE_PATH).respond(200, text=PRICE_PAGE)
    web.get(rosstat.BASE_URL + "/storage/mediabank/ipc_mes_09-2026.xlsx").respond(
        200, content=WORKBOOK
    )
    out = _run("rosstat_cpi", {}, settings, db)
    assert out.status == "succeeded" and out.result["released"] == "2026-10-09"
    rows = {r["fixing_date"]: r for r in _fixings(conn, "CPI_RU_MOM")}
    assert rows[date(2026, 9, 1)]["published_on"] == date(2026, 10, 9)
    assert rows[date(2026, 8, 1)]["published_on"] is None  # never guessed for older months
    # a later page date does not move the release date of an already published month
    web.get(rosstat.BASE_URL + rosstat.PRICE_PAGE_PATH).respond(
        200, text=PRICE_PAGE.replace("09.10.2026", "16.10.2026")
    )
    _run("rosstat_cpi", {"date_from": "2026-09-01"}, settings, db)
    assert _fixings(conn, "CPI_RU_MOM")[-1]["published_on"] == date(2026, 10, 9)

    infl = web.get(cbr.BASE_URL + cbr.INFLATION_PATH).respond(200, text=CBR_INFL)
    out = _run("cbr_inflation", {}, settings, db)
    assert out.status == "succeeded", out.error
    assert out.result["months"] == 33 and out.result["chain_mismatches"] == []
    assert out.result["chain_checked"] == 33
    yoy = _fixings(conn, "CPI_RU_YOY")
    assert yoy[-1]["value"] == Decimal("6.35") and yoy[-1]["unit"] == "pct"
    assert yoy[-1]["source"] == "cbr" and yoy[-1]["fixing_date"] == date(2026, 9, 1)
    assert infl.calls.last.request.url.params["UniDbQuery.From"] == "01.09.2013"
    unchanged = _run("cbr_inflation", {}, settings, db)
    assert unchanged.result == {"unchanged": True}
    # a y/y that the Rosstat chain does not reproduce: warning, partial, value stored as published
    infl.respond(200, text=CBR_INFL.replace("<td>6,35</td>", "<td>7,35</td>", 1))
    off = _run("cbr_inflation", {"date_from": "2026-01-01"}, settings, db)
    assert off.status == "partial" and off.result["chain_mismatches"][0].startswith("2026-09")
