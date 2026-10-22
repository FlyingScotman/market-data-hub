"""``mdhub probe-sources``: which external sources of the reference-rate, cross-rate, proxy-curve
and CPI datasets answer **from this machine** (WEB-16).

The production hub is in Russia, where many European and U.S. hosts may be geo-blocked; every
dataset therefore has a chain of independent sources (:mod:`mdhub.loaders._fallback`). This
check sends one real request per (source, dataset) - the request the loader sends, for a short
recent range - through :class:`~mdhub.core.http.HttpClient` (same TLS bundle, proxy and
User-Agent as the loaders; no retries), **parses** the answer with the loader's parser and
reports HTTP status, latency, size and what came back (latest date and value). It writes
nothing (no database needed) and never prints credentials (none are used: every source here is
public).

Unlike the ``source_probe`` job (one lightweight request per source every 5 minutes, stored in
``ops.source_probe``) this proves the payload still parses, per dataset and per fallback
position, so the owner can order or skip sources (``MDHUB_SKIP_SOURCES``) for the server.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Final

import httpx

from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, HttpError, HttpStatusError
from mdhub.core.iss import ISS_BASE_URL
from mdhub.core.redact import sanitize_text
from mdhub.sources import (
    bundesbank,
    cbr,
    dbnomics,
    ecb,
    ecb_data,
    fred,
    nyfed,
    rosstat,
    snb,
    us_treasury,
)
from mdhub.sources.observations import Observation

__all__ = ["CHECKS", "Check", "CheckResult", "run_checks"]

LOOKBACK_DAYS: Final = 10
DEFAULT_TIMEOUT_S: Final = 20.0
BDF_ESTR_URL: Final = (
    "https://webstat.banque-france.fr/export/csv/fr/catalog/ESTR/ESTR.B.EU000A2X2A25.WT"
)
"""Banque de France Webstat keyless export of €STR (undocumented; the documented API needs a
registered key). Probed only - no loader uses it."""


@dataclass(frozen=True, slots=True)
class Check:
    source: str
    dataset: str
    role: str
    """Position in the loader's chain: ``primary``, ``fallback 1`` ..., ``input`` (derived data),
    ``probe only``."""
    run: Callable[[HttpClient, date], tuple[httpx.Response, str]]
    """Send the request(s), parse, return the last response and a one-line summary."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    source: str
    dataset: str
    role: str
    ok: bool
    http_status: int | None
    latency_ms: float
    size_bytes: int | None
    result: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _latest(obs: list[Observation], unit: str = "") -> str:
    if not obs:
        return "answered, no observation in the range"
    last = max(obs, key=lambda o: (o.day, o.series))
    series = {o.series for o in obs}
    tail = f" ({len(series)} series)" if len(series) > 1 else ""
    return f"{len(obs)} obs, latest {last.day} {last.series} = {last.value}{unit}{tail}"


def _ecb_estr(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = ecb_data.fetch(http, ecb_data.ESTR_FLOW_KEY, today - timedelta(days=LOOKBACK_DAYS), today)
    return r, _latest(ecb_data.estr_from_csv(r.content), " %")


def _bbk_estr(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = bundesbank.fetch(
        http,
        bundesbank.ESTR_FLOW,
        bundesbank.ESTR_KEY,
        today - timedelta(days=LOOKBACK_DAYS),
        today,
    )
    return r, _latest(bundesbank.estr_from_csv(r.content), " %")


def _dbn_estr(http: HttpClient, _today: date) -> tuple[httpx.Response, str]:
    r = dbnomics.fetch_estr(http)
    return r, _latest(dbnomics.estr_from_json(r.content)[-LOOKBACK_DAYS:], " %")


def _fred(series: tuple[str, ...]) -> Callable[[HttpClient, date], tuple[httpx.Response, str]]:
    def run(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
        r = fred.fetch(http, series, today - timedelta(days=LOOKBACK_DAYS + 7), today)
        return r, _latest(fred.parse_csv(r.content, series))

    return run


def _bdf_estr(http: HttpClient, _today: date) -> tuple[httpx.Response, str]:
    r = http.get(BDF_ESTR_URL)
    return r, f"answered ({len(r.content)} bytes; not parsed - no loader, the API needs a key)"


def _nyfed(rate: str) -> Callable[[HttpClient, date], tuple[httpx.Response, str]]:
    def run(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
        r = nyfed.fetch(http, rate, today - timedelta(days=LOOKBACK_DAYS), today)
        rows = nyfed.parse_sofr(r.content) if rate == "sofr" else nyfed.parse_sofrai(r.content)
        if not rows:
            return r, "answered, no rate in the range"
        last = max(rows, key=lambda x: (x.day, x.index_code))
        return r, f"{len(rows)} rows, latest {last.day} {last.index_code} = {last.value}"

    return run


def _snb_saron(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = snb.fetch(http, today - timedelta(days=LOOKBACK_DAYS), today)
    return r, _latest(snb.parse_csv(r.content), " %")


def _eurofxref(http: HttpClient, _today: date) -> tuple[httpx.Response, str]:
    r = ecb.fetch(http, ecb.DAILY_URL)
    rates = ecb.parse_xml(r.content)
    usd = next((x for x in rates if x.ccy == "USD"), None)
    return r, f"{len(rates)} rates of {rates[0].rate_date}" + (
        f", USD = {usd.rate_per_eur}" if usd else ""
    )


def _ecb_exr(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = ecb_data.fetch(http, ecb_data.EXR_FLOW_KEY, today - timedelta(days=LOOKBACK_DAYS), today)
    obs = [o for o in ecb_data.exr_from_csv(r.content) if o.series == "USD"]
    return r, _latest(obs, " USD per EUR")


def _cbr_daily(http: HttpClient, _today: date) -> tuple[httpx.Response, str]:
    r = http.get(cbr.BASE_URL + cbr.XML_DAILY_PATH)
    rec = cbr.parse_xml_daily(r.content)
    usd = next((x for x in rec.rates if x.ccy == "USD"), None)
    return r, f"{len(rec.rates)} rates set {rec.record_date}" + (
        f", USD = {usd.value}/{usd.nominal}" if usd else ""
    )


def _moex_indicative(http: HttpClient, _today: date) -> tuple[httpx.Response, str]:
    r = http.get(
        f"{ISS_BASE_URL}/statistics/engines/futures/markets/indicativerates/securities.json",
        params={"iss.meta": "off", "iss.only": "securities"},
        expect_json=True,
    )
    data = r.json().get("securities", {}).get("data", [])
    pairs = sorted({str(row[2]) for row in data if len(row) > 2})
    return r, f"{len(data)} indicative rates: {', '.join(pairs)}"


def _moex_cets(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = http.get(
        f"{ISS_BASE_URL}/history/engines/currency/markets/selt/boards/CETS/securities/"
        "USDCNY_TOM.json",
        params={
            "iss.meta": "off",
            "from": (today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
            "history.columns": "TRADEDATE,CLOSE,NUMTRADES",
        },
        expect_json=True,
    )
    data = r.json().get("history", {}).get("data", [])
    traded = [row for row in data if row[2]]
    return r, f"USDCNY_TOM: {len(data)} days, {len(traded)} with trades"


def _ust(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = us_treasury.fetch_month(http, today.year, today.month)
    curves = us_treasury.parse_xml(r.content)
    if not curves and today.day < 8:  # early in the month: the previous month
        prev = today.replace(day=1) - timedelta(days=1)
        r = us_treasury.fetch_month(http, prev.year, prev.month)
        curves = us_treasury.parse_xml(r.content)
    if not curves:
        return r, "answered, no curve this month"
    last = curves[-1]
    ten = dict(last.quotes).get("BC_10YEAR")
    return r, f"{len(curves)} curves, latest {last.day}, {len(last.quotes)} tenors, 10Y = {ten}"


def _ecb_yc(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = ecb_data.fetch(http, ecb_data.yc_flow_key(), today - timedelta(days=LOOKBACK_DAYS), today)
    return r, _latest(ecb_data.yc_from_csv(r.content), " %")


def _bbk_par(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = bundesbank.fetch(
        http,
        bundesbank.PAR_FLOW,
        bundesbank.par_key(),
        today - timedelta(days=LOOKBACK_DAYS),
        today,
    )
    return r, _latest(bundesbank.par_curve_from_csv(r.content), " %")


def _rosstat(http: HttpClient, _today: date) -> tuple[httpx.Response, str]:
    path, r, released = rosstat.fetch_cpi_workbook(http)
    months = rosstat.parse_cpi_monthly(r.content)
    last = months[-1]
    return (
        r,
        f"{path.rsplit('/', 1)[-1]}: latest {last.period:%Y-%m} = {last.index_pct}, "
        f"released {released}",
    )


def _cbr_infl(http: HttpClient, today: date) -> tuple[httpx.Response, str]:
    r = cbr.fetch_hd_base(http, cbr.INFLATION_PATH, today - timedelta(days=120), today)
    rows = cbr.parse_inflation_page(r.text)
    if not rows:
        return r, "answered, no month in the range"
    return r, f"latest {rows[-1].period:%Y-%m} y/y = {rows[-1].inflation_yoy_pct} %"


CHECKS: Final = (
    Check("ecb_data_portal", "estr", "primary", _ecb_estr),
    Check("bundesbank", "estr", "fallback 1", _bbk_estr),
    Check("dbnomics", "estr", "fallback 2", _dbn_estr),
    Check("fred", "estr", "fallback 3", _fred((fred.ESTR_SERIES,))),
    Check("banque_de_france", "estr", "probe only", _bdf_estr),
    Check("nyfed", "sofr", "primary", _nyfed("sofr")),
    Check("nyfed", "sofr_averages", "primary", _nyfed("sofrai")),
    Check("fred", "sofr", "fallback 1", _fred((fred.SOFR_SERIES, *fred.SOFR_AVERAGES))),
    Check("snb", "saron", "primary", _snb_saron),
    Check("ecb", "ecb_fx", "primary", _eurofxref),
    Check("ecb_data_portal", "ecb_fx", "fallback 1", _ecb_exr),
    Check("cbr", "fx_cross_cbr_cross", "input", _cbr_daily),
    Check("moex_iss", "fx_cross_moex_indicative_cross", "input", _moex_indicative),
    Check("moex_iss", "fx_cross_moex_cets", "input", _moex_cets),
    Check("fred", "fx_cross_fred_h10", "primary", _fred(tuple(fred.H10_PAIRS))),
    Check("us_treasury", "proxy_usd_ust_par", "primary", _ust),
    Check("ecb_data_portal", "proxy_eur_aaa_par", "primary", _ecb_yc),
    Check("bundesbank", "proxy_eur_bund_par", "fallback 1 (other curve)", _bbk_par),
    Check("rosstat", "rosstat_cpi", "primary", _rosstat),
    Check("cbr", "cbr_cpi_yoy", "primary", _cbr_infl),
)


def run_checks(
    settings: Settings,
    *,
    sources: set[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    today: date | None = None,
    http_factory: Callable[..., HttpClient] = HttpClient,
) -> list[CheckResult]:
    """Run :data:`CHECKS` (only ``sources`` when given) one after another."""
    day = today or date.today()
    out: list[CheckResult] = []
    with http_factory(settings=settings, max_retries=0, timeout=timeout) as http:
        for c in CHECKS:
            if sources and c.source not in sources:
                continue
            started = time.perf_counter()
            status: int | None = None
            size: int | None = None
            try:
                resp, summary = c.run(http, day)
                ok, status, size = True, resp.status_code, len(resp.content)
            except HttpStatusError as exc:
                ok, status, summary = False, exc.status_code, f"HTTP {exc.status_code}"
            except (HttpError, httpx.HTTPError, OSError, FormatDriftError, ValueError) as exc:
                ok, summary = False, f"{type(exc).__name__}: {exc}"
            latency = round((time.perf_counter() - started) * 1000, 1)
            summary = sanitize_text(summary, settings.secret_values())[:300]
            out.append(CheckResult(c.source, c.dataset, c.role, ok, status, latency, size, summary))
    return out
