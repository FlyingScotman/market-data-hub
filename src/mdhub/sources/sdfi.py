"""MOEX SDFI (OTC derivatives) swap-curve catalogue and values - fetch + parse, no DB.

* ``GET /iss/sdfi/curves.json`` - free catalogue: ``curveid, date_from, date_till, title,
  methodology, unit`` (72 curves: RUB/CNY/EUR/USD discount, forward, OIS, IRS, XCS, quotes).
* ``GET /iss/sdfi/curves/{curveid}.json`` - values; without a subscription MOEX answers
  ``200 text/html`` ("subscribers only"), which :meth:`HttpClient.get_json` turns into
  :class:`~mdhub.core.http.EntitlementError`.

Not scheduled. Use only when ``MDHUB_ENABLE_SDFI=true`` (:func:`require_enabled`); the value
payload layout is unknown until entitled, so :func:`fetch_curve_values` returns the raw ISS
blocks for the caller to capture and inspect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError, JobSkipped
from mdhub.core.http import HttpClient, JsonResult
from mdhub.core.iss import ISS_BASE_URL, parse_block, to_date, to_str

__all__ = [
    "CATALOGUE_PATH",
    "PARSER_VERSION",
    "SdfiCurve",
    "fetch_catalogue",
    "fetch_curve_values",
    "parse_catalogue",
    "require_enabled",
]

CATALOGUE_PATH: Final = "/sdfi/curves.json"
PARSER_VERSION: Final = "1"
_CURVE_ID = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")


@dataclass(frozen=True, slots=True)
class SdfiCurve:
    curve_id: str
    date_from: date | None
    date_till: date | None
    title: str | None
    methodology: str | None
    unit: str | None


def require_enabled(settings: Settings) -> None:
    """Raise :class:`JobSkipped` unless ``MDHUB_ENABLE_SDFI`` is true."""
    if not settings.enable_sdfi:
        raise JobSkipped("MOEX SDFI disabled (MDHUB_ENABLE_SDFI=false)")


def parse_catalogue(payload: Any) -> list[SdfiCurve]:
    rows = parse_block(
        payload,
        "curves",
        required=["curveid", "date_from", "date_till", "title", "methodology", "unit"],
    )
    out = []
    for r in rows:
        cid = to_str(r["curveid"]) or ""
        if not _CURVE_ID.match(cid):
            raise FormatDriftError(f"SDFI catalogue: bad curve id {cid!r}")
        out.append(
            SdfiCurve(
                curve_id=cid,
                date_from=to_date(r["date_from"]),
                date_till=to_date(r["date_till"]),
                title=to_str(r["title"]),
                methodology=to_str(r["methodology"]),
                unit=to_str(r["unit"]),
            )
        )
    return out


def fetch_catalogue(http: HttpClient) -> tuple[JsonResult, list[SdfiCurve]]:
    """The free curve catalogue (raw result for capture + parsed curves)."""
    res = http.get_json(ISS_BASE_URL + CATALOGUE_PATH, params={"iss.meta": "off"})
    return res, parse_catalogue(res.data)


def fetch_curve_values(
    http: HttpClient, settings: Settings, curve_id: str, *, day: date | None = None
) -> JsonResult:
    """Values of one curve. Raises ``JobSkipped`` when disabled and ``EntitlementError`` when
    MOEX answers with the HTML "subscribers only" page."""
    require_enabled(settings)
    if not _CURVE_ID.match(curve_id):
        raise ValueError(f"bad SDFI curve id {curve_id!r}")
    params: dict[str, Any] = {"iss.meta": "off"}
    if day is not None:
        params["date"] = day.isoformat()
    return http.get_json(f"{ISS_BASE_URL}/sdfi/curves/{curve_id}.json", params=params)
