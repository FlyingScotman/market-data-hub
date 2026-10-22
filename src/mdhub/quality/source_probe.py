"""The ``source_probe`` job: can the hub reach every source, and are its credentials accepted?
(ADMIN_DESIGN §3.2, every 5 minutes).

One lightweight request per source through :class:`mdhub.core.http.HttpClient` (same TLS
bundle, proxy, User-Agent and per-host rate limits as the loaders; no retries, 15 s timeout),
recorded as one ``ops.source_probe`` row: ``ok``, HTTP status, latency, ``auth``, the server
certificate's ``notAfter`` and a sanitized error. Probe payloads are not stored in
``ops.raw_capture`` (they are not data).

``auth``:

* ``n/a`` - public source (MOEX ISS, CBR, ECB, Rosstat, reference-rate sources): ``ok`` =
  2xx/3xx,
* ``ok`` / ``rejected`` - credentials configured and the answer was 2xx / 401 or 403
  (``rejected`` is never ``ok``),
* ``not_configured`` - the source needs credentials that are not set: the same request goes out
  without them and ``ok`` only says the source answered (any status below 500).

Sources and requests (:data:`PROBES`): ``moex_iss`` ``/iss/engines.json``, ``moex_apim``
``/iss/engines.json`` with ``MOEX_API_KEY``, ``cbr`` ``XML_daily.asp``, ``ecb``
``eurofxref-daily.xml``, ``rosstat`` ``/statistics/price``, the reference-rate sources (WEB-16:
``ecb_data_portal`` / ``bundesbank`` / ``dbnomics`` the latest €STR, ``fred`` SOFR of the last 10
days, ``nyfed`` the latest SOFR, ``snb`` the dimensions of the SARON cube, ``us_treasury`` the
par-curve feed of the month - at most hourly,
the feed is slow), ``ratescalcs`` the JSON snapshot
(Basic auth; only Mon-Fri 08:50-23:00 MSK, when the service answers), ``otc_monitor``
``/api/bond/`` (bearer), ``cbonds`` one ``get_emissions`` query with ``limit=1`` - at most
hourly (:attr:`ProbeSpec.min_interval_s`), so a rejected login does not hit the provider 288
times a day; the rating agencies ``nkr``, ``nra``, ``raexpert`` (WEB-18) their ``robots.txt``,
at most hourly.
"""

from __future__ import annotations

import base64
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any, Final

import httpx

from mdhub.config import Settings
from mdhub.core.http import HttpClient, HttpError
from mdhub.core.iss import APIM_BASE_URL, ISS_BASE_URL
from mdhub.core.redact import redact_url, sanitize_text
from mdhub.core.timeutil import MSK
from mdhub.credentials import REGISTRY as CREDENTIALS
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.sources import (
    bundesbank,
    cbonds,
    cbr,
    dbnomics,
    ecb,
    ecb_data,
    fred,
    nkr,
    nra,
    nyfed,
    otc_monitor,
    raexpert,
    ratescalcs,
    rosstat,
    snb,
    us_treasury,
)
from mdhub.sources.moex_apim import apim_headers

__all__ = ["PROBES", "PROBE_TIMEOUT_S", "ProbeRequest", "ProbeResult", "ProbeSpec", "probe"]

PROBE_TIMEOUT_S: Final = 15.0
_MAX_ERROR = 500


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """What to send. ``configured`` is ``None`` for a public source."""

    url: str
    method: str = "GET"
    headers: dict[str, str] | None = None
    json: Any = None
    configured: bool | None = None


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    source: str
    build: Callable[[Settings], ProbeRequest]
    min_interval_s: float = 0.0
    """Probe at most this often (0 = every run)."""
    active: Callable[[datetime], bool] | None = None
    """Only probe when this returns true for the current time (service hours)."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One ``ops.source_probe`` row."""

    source: str
    endpoint: str
    ok: bool
    auth: str
    http_status: int | None = None
    latency_ms: float | None = None
    tls_expires_at: datetime | None = None
    error: str | None = None


def _public(url: str) -> Callable[[Settings], ProbeRequest]:
    return lambda _s: ProbeRequest(url=url)


def _apim(settings: Settings) -> ProbeRequest:
    url = f"{APIM_BASE_URL}/engines.json?iss.meta=off"
    if settings.moex_api_key is None:
        return ProbeRequest(url=url, configured=False)
    return ProbeRequest(
        url=url, headers=apim_headers(settings.moex_api_key.get_secret_value()), configured=True
    )


def _ratescalcs(settings: Settings) -> ProbeRequest:
    url = ratescalcs.BASE_URL + ratescalcs.SNAPSHOT_PATH
    user, password = settings.ratescalc_username, settings.ratescalc_password
    if user is None or password is None:
        return ProbeRequest(url=url, configured=False)
    pair = f"{user.get_secret_value()}:{password.get_secret_value()}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    return ProbeRequest(url=url, headers=headers, configured=True)


def _otc_monitor(settings: Settings) -> ProbeRequest:
    url = otc_monitor.BASE_URL + otc_monitor.BOND_PATH
    token = settings.otc_monitor_token
    if token is None:
        return ProbeRequest(url=url, configured=False)
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
    return ProbeRequest(url=url, headers=headers, configured=True)


def _cbonds(settings: Settings) -> ProbeRequest:
    login, password = settings.cbonds_login, settings.cbonds_password
    configured = login is not None and password is not None
    body = {
        "auth": {
            "login": login.get_secret_value() if login else "",
            "password": password.get_secret_value() if password else "",
        },
        "filters": [],
        "quantity": {"limit": 1, "offset": 0},
        "sorting": [],
        "fields": [{"field": "id"}],
    }
    return ProbeRequest(
        url=f"{cbonds.CBONDS_BASE_URL}/get_emissions/",
        method="POST",
        json=body,
        configured=configured,
    )


def _fred(_settings: Settings) -> ProbeRequest:
    start = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()
    return ProbeRequest(url=f"{fred.BASE_URL}?id={fred.SOFR_SERIES}&cosd={start}")


def _us_treasury(_settings: Settings) -> ProbeRequest:
    month = datetime.now(UTC).strftime("%Y%m")
    return ProbeRequest(
        url=f"{us_treasury.XML_URL}?data=daily_treasury_yield_curve"
        f"&field_tdr_date_value_month={month}"
    )


def ratescalcs_hours(now: datetime) -> bool:
    """Ratescalcs answers Mon-Fri 08:50-23:00 MSK."""
    local = now.astimezone(MSK)
    return local.weekday() < 5 and dtime(8, 50) <= local.time() < dtime(23, 0)


PROBES: Final = (
    ProbeSpec("moex_iss", _public(f"{ISS_BASE_URL}/engines.json?iss.meta=off")),
    ProbeSpec("moex_apim", _apim),
    ProbeSpec("cbr", _public(cbr.BASE_URL + cbr.XML_DAILY_PATH)),
    ProbeSpec("ecb", _public(ecb.DAILY_URL)),
    ProbeSpec("rosstat", _public(rosstat.BASE_URL + rosstat.PRICE_PAGE_PATH)),
    ProbeSpec(
        "ecb_data_portal",
        _public(
            f"{ecb_data.BASE_URL}/{ecb_data.ESTR_FLOW_KEY}"
            "?format=csvdata&detail=dataonly&lastNObservations=1"
        ),
    ),
    ProbeSpec(
        "bundesbank",
        _public(
            f"{bundesbank.BASE_URL}/{bundesbank.ESTR_FLOW}/{bundesbank.ESTR_KEY}"
            "?format=csv&lang=en&lastNObservations=1"
        ),
    ),
    ProbeSpec("dbnomics", _public(f"{dbnomics.BASE_URL}{dbnomics.ESTR_PATH}")),
    ProbeSpec("fred", _fred),
    ProbeSpec("nyfed", _public(f"{nyfed.BASE_URL}/sofr/last/1.json")),
    ProbeSpec("snb", _public(f"{snb.BASE_URL}/{snb.CUBE}/dimensions/en")),
    ProbeSpec("us_treasury", _us_treasury, min_interval_s=3600.0),
    ProbeSpec("ratescalcs", _ratescalcs, active=ratescalcs_hours),
    ProbeSpec("otc_monitor", _otc_monitor),
    ProbeSpec("cbonds", _cbonds, min_interval_s=3600.0),
    # rating agencies (WEB-18): robots.txt, at most hourly (their pages are read once a day)
    ProbeSpec("nkr", _public(nkr.BASE_URL + "/robots.txt"), min_interval_s=3600.0),
    ProbeSpec("nra", _public(nra.BASE_URL + "/robots.txt"), min_interval_s=3600.0),
    ProbeSpec("raexpert", _public(raexpert.BASE_URL + "/robots.txt"), min_interval_s=3600.0),
)
"""One probe per source, in probe order."""


def tls_expiry(response: httpx.Response) -> datetime | None:
    """``notAfter`` of the peer certificate of the connection that carried ``response``."""
    try:
        stream = response.extensions.get("network_stream")
        ssl_object = stream.get_extra_info("ssl_object") if stream is not None else None
        cert = ssl_object.getpeercert() if ssl_object is not None else None
        not_after = cert.get("notAfter") if cert else None
        if not isinstance(not_after, str):
            return None
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC)
    except Exception:  # no TLS (tests, plain HTTP) or a stream without the extra info
        return None


def probe(http: HttpClient, spec: ProbeSpec, settings: Settings) -> ProbeResult:
    """Send ``spec``'s request once and classify the answer (never raises for HTTP errors)."""
    req = spec.build(settings)
    endpoint = redact_url(req.url)
    auth = "n/a" if req.configured is None else "ok" if req.configured else "not_configured"
    started = time.perf_counter()
    try:
        response = http.request(
            req.method, req.url, headers=req.headers, json=req.json, raise_for_status=False
        )
    except (HttpError, httpx.HTTPError, OSError) as exc:
        text = sanitize_text(f"{type(exc).__name__}: {exc}", settings.secret_values())
        return ProbeResult(
            source=spec.source,
            endpoint=endpoint,
            ok=False,
            auth=auth,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=text[:_MAX_ERROR],
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    status = response.status_code
    error: str | None = None
    if req.configured is None:
        ok = 200 <= status < 400
    elif not req.configured:
        ok = status < 500
    elif status in (401, 403):
        auth, ok = "rejected", False
    else:
        ok = 200 <= status < 400
    if not ok:
        error = f"HTTP {status}"
    return ProbeResult(
        source=spec.source,
        endpoint=endpoint,
        ok=ok,
        auth=auth,
        http_status=status,
        latency_ms=latency_ms,
        tls_expires_at=tls_expiry(response),
        error=error,
    )


def _last_probe(ctx: JobContext, source: str) -> datetime | None:
    with ctx.connection() as conn:
        row = conn.execute(
            "SELECT max(ts) AS ts FROM ops.source_probe WHERE source = %s", (source,)
        ).fetchone()
    return row["ts"] if row else None


@job("source_probe", "Reachability, credentials and TLS expiry of every source -> ops.source_probe")
def source_probe(
    ctx: JobContext, *, sources: list[str] | None = None, force: bool = False
) -> dict[str, Any]:
    """Probe every source of :data:`PROBES` (or only ``sources``) and record the results.

    :param sources: subset of source codes (default: all).
    :param force: ignore service hours and minimum intervals.
    """
    known = {p.source for p in PROBES}
    unknown = sorted(set(sources or ()) - known)
    if unknown:
        raise JobParamError(f"unknown probe source(s) {unknown} (known: {sorted(known)})")
    now = datetime.now(UTC)
    results: list[ProbeResult] = []
    skipped: list[str] = []
    with ctx.http(max_retries=0, timeout=PROBE_TIMEOUT_S) as http:
        for spec in PROBES:
            if sources and spec.source not in sources:
                continue
            if not force and spec.active is not None and not spec.active(now):
                skipped.append(spec.source)
                continue
            if not force and spec.min_interval_s > 0:
                last = _last_probe(ctx, spec.source)
                if last is not None and (now - last).total_seconds() < spec.min_interval_s - 60:
                    skipped.append(spec.source)
                    continue
            results.append(probe(http, spec, ctx.settings))
    with ctx.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ops.source_probe (run_id, source, endpoint, ok, http_status, latency_ms,
                auth, tls_expires_at, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    ctx.run.id,
                    r.source,
                    r.endpoint,
                    r.ok,
                    r.http_status,
                    r.latency_ms,
                    r.auth,
                    r.tls_expires_at,
                    r.error,
                )
                for r in results
            ],
        )
        # The console's "last check" of each credential row of a probed source that has some.
        # shortcut: MDHUB_HTTP_PROXY gets no verdict (it spans every source).
        cur.executemany(
            "UPDATE admin.source_credential SET verified_at = now(), verify_result = %s "
            "WHERE name = ANY(%s)",
            [
                (
                    "rejected" if r.auth == "rejected" else "ok" if r.ok else "unreachable",
                    [c.name for c in CREDENTIALS.values() if c.source == r.source],
                )
                for r in results
                if r.auth in ("ok", "rejected")
            ],
        )
    failing = [r.source for r in results if not r.ok]
    if failing:
        ctx.logger.info("source probe: not ok: %s", ", ".join(failing))
    return {
        "probed": {
            r.source: {"ok": r.ok, "status": r.http_status, "auth": r.auth} for r in results
        },
        "skipped": skipped,
    }
