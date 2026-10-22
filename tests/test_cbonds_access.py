"""Cbonds access diagnostics and the documented minute quota, without live credentials."""

from __future__ import annotations

import json
from itertools import pairwise

import httpx
import pytest
import respx

from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.quality.source_probe import PROBES, probe
from mdhub.sources import cbonds as cb

LOGIN, PASSWORD = "test-api-user", "test-api-password"
SPEC = next(spec for spec in PROBES if spec.source == "cbonds")


@pytest.mark.parametrize(
    ("status", "body", "auth", "ok", "diagnostic"),
    [
        (401, None, "rejected", False, "401"),
        (403, {"error": {"err_no": 500100, "err_str": "Too many records"}}, "ok", False, "500100"),
        (403, None, "ok", False, "access denied"),
        (
            200,
            {"error": {"err_no": 900000, "err_str": "Minute quota exceeded"}},
            "ok",
            False,
            "Minute quota exceeded",
        ),
        (200, {"items": "not an array"}, "ok", False, "items"),
        (200, {"items": [{"id": 1002}], "total": 1}, "ok", True, None),
    ],
)
@respx.mock
def test_probe_validates_cbonds_response(
    status: int, body: object, auth: str, ok: bool, diagnostic: str | None
) -> None:
    route = respx.post(f"{cb.CBONDS_BASE_URL}/get_emissions/").respond(status, json=body)
    settings = Settings(cbonds_login=LOGIN, cbonds_password=PASSWORD)
    with HttpClient(limiter=RateLimiter(default_rps=None), max_retries=0) as http:
        result = probe(http, SPEC, settings)
    assert result.ok is ok
    assert result.auth == auth
    assert result.http_status == status
    if diagnostic:
        assert diagnostic in (result.error or "")
    request = json.loads(route.calls.last.request.content)
    assert request["filters"] == [{"field": "isin_code", "operator": "eq", "value": "RU000A1038V6"}]
    assert request["quantity"]["limit"] == 1
    assert route.call_count == 1


@respx.mock
def test_result_size_error_is_not_auth_and_diagnostic_is_redacted() -> None:
    route = respx.post(f"{cb.CBONDS_BASE_URL}/get_emissions/").respond(
        403,
        json={
            "error": {
                "err_no": 500100,
                "err_str": f"Too many records\nfor {LOGIN}, password {PASSWORD}",
            }
        },
    )
    with (
        HttpClient(limiter=RateLimiter(default_rps=None), base_url=cb.CBONDS_BASE_URL) as http,
        pytest.raises(cb.CbondsError) as exc,
    ):
        cb.query(
            http,
            "get_emissions",
            creds=cb.CbondsCredentials(LOGIN, PASSWORD),
            filters=[],
            fields=["id"],
        )
    assert not isinstance(exc.value, cb.CbondsAuthError)
    assert "500100" in str(exc.value) and "Too many records" in str(exc.value)
    assert LOGIN not in str(exc.value) and PASSWORD not in str(exc.value)
    assert "\n" not in str(exc.value)
    assert route.call_count == 1


@respx.mock
def test_cbonds_quota_shared_between_clients_and_retries() -> None:
    now = 0.0
    sent: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    limiter = RateLimiter(clock=lambda: now, sleep=sleep)

    def response(_request: httpx.Request) -> httpx.Response:
        sent.append(now)
        return httpx.Response(503 if len(sent) == 1 else 200, json={"items": []})

    respx.post(f"{cb.CBONDS_BASE_URL}/get_flow_new/").mock(side_effect=response)
    # Constructing another client must not refill the shared bucket.
    for _ in range(2):
        with HttpClient(base_url=cb.CBONDS_BASE_URL, limiter=limiter, sleep=sleep) as http:
            for _ in range(16):
                http.request("POST", "/get_flow_new/", retry=True)
    assert len(sent) == 33  # includes one retry
    assert all(b - a >= 2.0 for a, b in pairwise(sent))
    assert all(sum(t <= other < t + 60 for other in sent) <= 30 for t in sent)
