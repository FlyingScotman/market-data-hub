"""Cbonds licensed JSON API (``https://ws2.cbonds.info/services/json/<operation>/``).

Read-only query methods are ``POST`` with the credentials in the JSON body::

    {"auth": {"login": ..., "password": ...},
     "filters": [{"field": "isin_code", "operator": "in", "value": "RU000A...;RU000A..."}],
     "quantity": {"limit": 50, "offset": 0}, "sorting": [], "fields": [{"field": "id"}, ...]}

Only the operations in :data:`ALLOWED_OPERATIONS` are used. Credentials are passed in by the
loader (from ``CBONDS_LOGIN`` / ``CBONDS_PASSWORD``); they are never logged, never part of a
URL, and never stored (``record_capture`` stores the *response* only). HTTP 401/403 raises
:class:`CbondsAuthError` (the account is currently rejected, likely an IP allow-list).

Parsers return typed dicts; units: Cbonds ``margin``/``floor``/``cup`` are percent,
``cupon_rate`` in flows is a decimal fraction (x100 -> percent).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Final

import httpx

from mdhub.core.decimalutil import to_decimal
from mdhub.core.errors import FormatDriftError, MdhubError
from mdhub.core.http import HttpClient
from mdhub.core.iss import to_date, to_str

__all__ = [
    "ALLOWED_OPERATIONS",
    "CBONDS_BASE_URL",
    "EMISSION_FIELDS",
    "FLOW_FIELDS",
    "OFFER_FIELDS",
    "PARSER_VERSION",
    "RATING_FIELDS",
    "CbondsAuthError",
    "CbondsCredentials",
    "CbondsError",
    "CbondsResult",
    "map_benchmark_name",
    "normalize_day_count",
    "normalize_option_type",
    "parse_emission",
    "parse_flow",
    "parse_offer",
    "parse_rating",
    "query",
    "valid_isin",
]

PARSER_VERSION: Final = "1"
CBONDS_BASE_URL: Final = "https://ws2.cbonds.info/services/json"
ALLOWED_OPERATIONS: Final = frozenset(
    {"get_emissions", "get_flow_new", "get_offert", "get_rating_emission_maxdate"}
)
MAX_BATCH: Final = 50
ISIN_RE: Final = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

EMISSION_FIELDS: Final = (
    "id",
    "isin_code",
    "currency_name",
    "maturity_date",
    "floating_rate",
    "bond_type",
    "coupon_type_name_rus",
    "cupon_period",
    "reference_rate_name_rus",
    "margin",
    "floor",
    "cup",
    "emission_cupon_basis_title",
    "variable_rate_type_id",
    "floating_rates_day",
    "updating_date",
)
FLOW_FIELDS: Final = (
    "id",
    "emission_isin_code",
    "start_date",
    "date",
    "coupon_num",
    "cupon_rate",
    "cupon_sum",
    "redemtion",
    "floating_rate",
    "updated_at",
)
OFFER_FIELDS: Final = (
    "id",
    "emission_isin_code",
    "date",
    "date_open",
    "date_close",
    "price",
    "type_rus",
    "option_type_name_rus",
    "status_str",
    "updating_date",
)
RATING_FIELDS: Final = (
    "id",
    "emission_isin_code",
    "agency_name_rus",
    "scale_name_rus",
    "scale_point_name",
    "forecast_name_rus",
    "rating_date",
)


class CbondsError(MdhubError):
    """Cbonds transport/provider error (message never contains credentials)."""


class CbondsAuthError(CbondsError):
    """Credentials rejected (HTTP 401/403 or provider auth error)."""


@dataclass(frozen=True, slots=True)
class CbondsCredentials:
    """Login/password; excluded from ``repr`` so they never end up in logs or tracebacks."""

    login: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CbondsResult:
    response: httpx.Response
    items: list[dict[str, Any]]
    total: int | None


def valid_isin(isin: str) -> bool:
    return bool(ISIN_RE.fullmatch(isin))


def query(
    http: HttpClient,
    operation: str,
    *,
    creds: CbondsCredentials,
    filters: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
    limit: int = 100,
    offset: int = 0,
    sorting: Sequence[Mapping[str, str]] = (),
) -> CbondsResult:
    """POST one query. ``http`` must have ``base_url=CBONDS_BASE_URL``.

    :raises CbondsAuthError: HTTP 401/403.
    :raises CbondsError: other HTTP errors, provider ``error`` object, malformed body.
    """
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError(f"unsupported Cbonds operation {operation!r}")
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("limit must be 1..1000 and offset >= 0")
    body = {
        "auth": {"login": creds.login, "password": creds.password},
        "filters": [dict(f) for f in filters],
        "quantity": {"limit": limit, "offset": offset},
        "sorting": [dict(s) for s in sorting],
        "fields": [{"field": f} for f in fields],
    }
    response = http.request("POST", f"/{operation}/", json=body, raise_for_status=False, retry=True)
    if response.status_code in (401, 403):
        raise CbondsAuthError(f"Cbonds {operation}: HTTP {response.status_code} (auth rejected)")
    if not response.is_success:
        raise CbondsError(f"Cbonds {operation}: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise CbondsError(f"Cbonds {operation}: response is not JSON") from None
    if not isinstance(data, dict):
        raise FormatDriftError(f"Cbonds {operation}: response is not an object")
    error = data.get("error")
    if error is not None:
        err_no = error.get("err_no") if isinstance(error, dict) else None
        raise CbondsError(f"Cbonds {operation}: provider error {err_no}")
    items = data.get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise FormatDriftError(f"Cbonds {operation}: 'items' is not a list of objects")
    total = data.get("total")
    try:
        total_int = int(total) if total is not None else None
    except (TypeError, ValueError):
        raise FormatDriftError(f"Cbonds {operation}: bad total {total!r}") from None
    return CbondsResult(response=response, items=items, total=total_int)


# ----------------------------------------------------------------------- normalizers


def _flag(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    try:
        return bool(int(Decimal(str(value))))
    except (ArithmeticError, ValueError):
        raise FormatDriftError(f"Cbonds: not a flag: {value!r}") from None


def _int(value: Any, field: str) -> int | None:
    dec = to_decimal(value, field=field)
    if dec is None:
        return None
    if dec != dec.to_integral_value():
        raise FormatDriftError(f"Cbonds {field}: not an integer: {value!r}")
    return int(dec)


def _date(value: Any, field: str) -> date | None:
    text = to_str(value)
    if text is None:
        return None
    return to_date(text[:10], field=field)


_DAY_COUNTS: Final = {
    "act/365": "ACT/365F",
    "act/365f": "ACT/365F",
    "actual/365": "ACT/365F",
    "act/360": "ACT/360",
    "actual/360": "ACT/360",
    "act/act": "ACT/ACT",
    "actual/actual": "ACT/ACT",
    "act/actisda": "ACT/ACT ISDA",
    "act/acticma": "ACT/ACT ICMA",
    "30/360": "30/360",
    "30e/360": "30E/360",
    "30/360isda": "30/360 ISDA",
}


def normalize_day_count(title: str | None) -> str | None:
    """Cbonds ``emission_cupon_basis_title`` -> canonical day count (unknown kept upper-cased)."""
    if not title:
        return None
    key = re.sub(r"[\s()\-]", "", title.lower())
    return _DAY_COUNTS.get(key, title.strip().upper())


def map_benchmark_name(name: str | None) -> str | None:
    """Cbonds ``reference_rate_name_rus`` -> hub benchmark code (unknown -> ``OTHER``)."""
    if not name:
        return None
    low = name.lower()
    if "ключев" in low:
        return "KEY_RATE"
    if "ruonia" in low or "руониа" in low:
        if "индекс" in low or "index" in low:
            return "RUONIA_INDEX"
        if re.search(r"\b3\s*(м|m)", low):
            return "RUONIA_3M"
        if re.search(r"\b6\s*(м|m)", low):
            return "RUONIA_6M"
        return "RUONIA"
    if "инфляц" in low or "ипц" in low or "cpi" in low:
        return "CPI"
    if "кбд" in low or "zero" in low or "g-curve" in low:
        return "ZCYC"
    return "OTHER"


def normalize_option_type(name: str | None) -> str:
    """Cbonds option/offer type text -> put / call / buyback / unknown."""
    low = (name or "").lower()
    if "колл" in low or "call" in low or ("досрочн" in low and "погаш" in low):
        return "call"
    if "пут" in low or "put" in low:
        return "put"
    if "выкуп" in low or "оферт" in low:
        return "put"
    return "unknown"


def parse_emission(row: Mapping[str, Any]) -> dict[str, Any]:
    isin = to_str(row.get("isin_code"))
    if isin is None:
        raise FormatDriftError("Cbonds emission without isin_code")
    basis = to_str(row.get("emission_cupon_basis_title"))
    return {
        "isin": isin.upper(),
        "cbonds_id": _int(row.get("id"), "id"),
        "currency": to_str(row.get("currency_name")),
        "maturity_date": _date(row.get("maturity_date"), "maturity_date"),
        "floating": _flag(row.get("floating_rate")),
        "amortizing": _flag(row.get("bond_type")),
        "coupon_type_name": to_str(row.get("coupon_type_name_rus")),
        "coupon_frequency": _int(row.get("cupon_period"), "cupon_period"),
        "reference_rate": to_str(row.get("reference_rate_name_rus")),
        "benchmark": map_benchmark_name(to_str(row.get("reference_rate_name_rus"))),
        "margin_pct": to_decimal(row.get("margin"), field="margin"),
        "floor_pct": to_decimal(row.get("floor"), field="floor"),
        "cap_pct": to_decimal(row.get("cup"), field="cup"),
        "day_count_raw": basis,
        "day_count": normalize_day_count(basis),
        "variable_rate_type": to_str(row.get("variable_rate_type_id")),
        "fixing_day": _int(row.get("floating_rates_day"), "floating_rates_day"),
        "updating_date": _date(row.get("updating_date"), "updating_date"),
    }


def parse_flow(row: Mapping[str, Any]) -> dict[str, Any]:
    isin = to_str(row.get("emission_isin_code"))
    if isin is None:
        raise FormatDriftError("Cbonds flow without emission_isin_code")
    rate = to_decimal(row.get("cupon_rate"), field="cupon_rate")
    return {
        "isin": isin.upper(),
        "pay_date": _date(row.get("date"), "date"),
        "start_date": _date(row.get("start_date"), "start_date"),
        "coupon_no": _int(row.get("coupon_num"), "coupon_num"),
        "rate_pct": rate * 100 if rate is not None else None,
        "amount": to_decimal(row.get("cupon_sum"), field="cupon_sum"),
        "redemption": to_decimal(row.get("redemtion"), field="redemtion"),
    }


def parse_offer(row: Mapping[str, Any]) -> dict[str, Any]:
    isin = to_str(row.get("emission_isin_code"))
    if isin is None:
        raise FormatDriftError("Cbonds offer without emission_isin_code")
    raw_type = to_str(row.get("option_type_name_rus")) or to_str(row.get("type_rus"))
    return {
        "isin": isin.upper(),
        "offer_date": _date(row.get("date"), "date"),
        "offer_date_start": _date(row.get("date_open"), "date_open"),
        "offer_date_end": _date(row.get("date_close"), "date_close"),
        "price": to_decimal(row.get("price"), field="price"),
        "offer_type_raw": raw_type,
        "offer_type": normalize_option_type(raw_type),
        "status_raw": to_str(row.get("status_str")),
    }


def parse_rating(row: Mapping[str, Any]) -> dict[str, Any]:
    isin = to_str(row.get("emission_isin_code"))
    agency = to_str(row.get("agency_name_rus"))
    if isin is None or agency is None:
        raise FormatDriftError("Cbonds rating without emission_isin_code/agency")
    return {
        "isin": isin.upper(),
        "agency": agency,
        "scale": to_str(row.get("scale_name_rus")) or "",
        "rating": to_str(row.get("scale_point_name")),
        "outlook": to_str(row.get("forecast_name_rus")),
        "rating_date": _date(row.get("rating_date"), "rating_date"),
    }
