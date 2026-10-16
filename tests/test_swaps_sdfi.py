"""MOEX SDFI catalogue parser and the entitlement / feature-flag guards."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from mdhub.config import Settings
from mdhub.core.errors import JobSkipped
from mdhub.core.http import EntitlementError, HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL
from mdhub.sources import sdfi

FIX = Path(__file__).parent / "fixtures" / "official"


def _http() -> HttpClient:
    return HttpClient(
        settings=Settings(),
        limiter=RateLimiter(default_rps=None),
        rate_limits={"iss.moex.com": 1e6},
        sleep=lambda _s: None,
        max_retries=0,
    )


def test_catalogue() -> None:
    curves = sdfi.parse_catalogue(json.loads((FIX / "sdfi_curves.json").read_text("utf-8")))
    ids = {c.curve_id for c in curves}
    assert len(curves) == 72
    assert {"RUB-OIS-RUONIA", "CNY-XCS-CNYRUO", "CNY-OIS-RUSFCN"} <= ids


def test_values_disabled_by_default() -> None:
    with pytest.raises(JobSkipped):
        sdfi.fetch_curve_values(_http(), Settings(), "RUB-OIS-RUONIA")


@respx.mock
def test_values_html_is_entitlement_error() -> None:
    respx.get(f"{ISS_BASE_URL}/sdfi/curves/RUB-OIS-RUONIA.json").mock(
        return_value=httpx.Response(
            200, text="<html>subscribers only</html>", headers={"content-type": "text/html"}
        )
    )
    with _http() as http, pytest.raises(EntitlementError):
        sdfi.fetch_curve_values(http, Settings(enable_sdfi=True), "RUB-OIS-RUONIA")
