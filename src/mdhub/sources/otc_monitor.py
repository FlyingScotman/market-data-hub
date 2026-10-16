"""OTC Monitor (``https://otc-monitor.ru``) swap runs: fetch + parse, no DB.

``GET /api/bond/`` with ``Authorization: Bearer <OTC_MONITOR_TOKEN>`` (passed in by the loader;
never logged, never in a URL) answers a JSON list of ~440 rows ``{isin, series, bid, offer,
mid, date_update}`` - bonds, FX and spread rows plus four swap curves. Without a valid token the
server answers 401 (:class:`OtcMonitorAuthError`). There is no history endpoint (``?date`` is
ignored, ``/api/history`` is 404): only the current state can be read.

Curves (``series`` after normalization: strip, collapse whitespace, upper-case tenor unit):

=================== ======================== ==============================================
dataset             series                   tenors
=================== ======================== ==============================================
``ruonia_ois``      ``<T> OIS RUONIA``       1W 2W 1M 2M 3M 6M 9M 1Y 2Y .. 10Y (17)
``key_rate_swap``   ``<T> IRS KeyRate``      3M 6M 9M 1Y 2Y .. 10Y (13)
``cny_ruonia_xccy`` ``<T> XCCY CNYRUONIA``   1W 2W 1M 2M 3M 4M 5M 6M 9M 1Y 2Y 3Y 4Y 5Y (14)
``cny_rusfar_ois``  ``<T> OIS RUSFARCNY``    1W 2W 1M 2M 3M 6M 9M 1Y (8)
=================== ======================== ==============================================

Raw series look like ``6m OIS RUONIA``, ``1W  XCCY CNYRUONIA`` (two spaces) or
``1Y OIS RUSFARCNY `` (trailing space). Spread rows (``26224 vs 4Y KRS``,
``1Y KRS vs 4Y KRS``, ``РЖД 1Р-27R vs 3Y OIS``) and bonds never match a curve pattern.

Validation fails closed **per curve** (the other curves are still returned): the tenor set
must equal the expected set exactly (no missing, extra or duplicate tenor); a quote has either
bid and offer (``lo <= bid <= offer <= hi``, mid recomputed as (bid+offer)/2, the provider's
``mid`` is ignored) or neither (mid-only: the provider ``mid`` is kept, ``lo <= mid <= hi``),
with sanity bounds per curve (:attr:`CurveSpec.min_pct`/:attr:`CurveSpec.max_pct`): RUB curves
0..100; CNY curves -50..300 (CNY rates went negative and RUSFARCNY spiked above 200% in the
2024 CNY liquidity squeezes - real quotes, not errors);
``date_update`` (``dd.mm.yyyy HH:MM:SS``, Moscow wall clock) must parse. The curve's source
clock is the latest ``date_update`` of its rows.

Conventions match the legacy mapping of ``otc_monitor`` history (``mdhub.legacy.map_md``), so
live snapshots continue it: tenors upper case in the expected order (positions 0-based),
``rc.tenor_parts``; snapshot ``quote_basis`` ``bid_ask_mid`` when every quote is two-sided,
else ``mid_only`` with ``extra.quote_basis = 'bid_ask_mid'`` on the two-sided quotes;
:func:`content_hash` equals the legacy ``curve_content_hash``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from mdhub.core.decimalutil import to_decimal
from mdhub.core.errors import FormatDriftError, MdhubError
from mdhub.core.http import HttpClient, HttpStatusError, JsonResult
from mdhub.core.timeutil import MSK
from mdhub.sources import ratescalcs as rc

__all__ = [
    "BASE_URL",
    "BOND_PATH",
    "CURVES",
    "ENDPOINT",
    "PARSER_VERSION",
    "CurveSpec",
    "OtcMonitorAuthError",
    "ParsedPayload",
    "content_hash",
    "fetch",
    "normalize_series",
    "parse_curve",
    "parse_payload",
]

BASE_URL: Final = "https://otc-monitor.ru"
BOND_PATH: Final = "/api/bond/"
ENDPOINT: Final = "bond"
"""Logical ``ops.raw_capture.endpoint`` of the ``/api/bond/`` payload."""
PARSER_VERSION: Final = "1"

_TENOR_RE: Final = re.compile(r"\d{1,2}[WMY]")
_DATE_FORMAT: Final = "%d.%m.%Y %H:%M:%S"


@dataclass(frozen=True, slots=True)
class CurveSpec:
    dataset: str
    suffix: str
    """Series text after the tenor, exactly as normalized (case-sensitive)."""
    tenors: tuple[str, ...]
    """Expected tenors in curve order (= quote positions)."""
    min_pct: Decimal = Decimal(0)
    max_pct: Decimal = Decimal(100)
    """Sanity bounds for bid/offer/mid in percent (inclusive)."""


_CNY_MIN_PCT: Final = Decimal(-50)
_CNY_MAX_PCT: Final = Decimal(300)
_YEARS_1_10: Final = tuple(f"{n}Y" for n in range(1, 11))
CURVES: Final = (
    CurveSpec("ruonia_ois", "OIS RUONIA", ("1W", "2W", "1M", "2M", "3M", "6M", "9M", *_YEARS_1_10)),
    CurveSpec("key_rate_swap", "IRS KeyRate", ("3M", "6M", "9M", *_YEARS_1_10)),
    CurveSpec(
        "cny_ruonia_xccy",
        "XCCY CNYRUONIA",
        ("1W", "2W", "1M", "2M", "3M", "4M", "5M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y"),
        _CNY_MIN_PCT,
        _CNY_MAX_PCT,
    ),
    CurveSpec(
        "cny_rusfar_ois",
        "OIS RUSFARCNY",
        ("1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y"),
        _CNY_MIN_PCT,
        _CNY_MAX_PCT,
    ),
)


class OtcMonitorAuthError(MdhubError):
    """The token was rejected (HTTP 401/403)."""


@dataclass(frozen=True, slots=True)
class ParsedPayload:
    curves: tuple[rc.ParsedCurve, ...]
    """Curves that passed validation, in :data:`CURVES` order."""
    failures: Mapping[str, str] = field(default_factory=dict)
    """dataset -> reason for curves that failed closed."""


def normalize_series(series: str) -> tuple[str, str] | None:
    """``"1W  XCCY CNYRUONIA "`` -> ``("1W", "XCCY CNYRUONIA")``; ``None`` unless the first
    token is a tenor (``<n>W|M|Y``, any case)."""
    parts = " ".join(series.split()).split(" ", 1)
    if len(parts) != 2:
        return None
    tenor = parts[0].upper()
    if not _TENOR_RE.fullmatch(tenor):
        return None
    return tenor, parts[1]


def _pct(value: Any, dataset: str, tenor: str, name: str) -> Decimal | None:
    return to_decimal(value, field=f"OTC Monitor {dataset} {tenor} {name}")  # NaN/inf raise


def _source_ts(value: Any, dataset: str, tenor: str) -> datetime:
    if not isinstance(value, str):
        raise FormatDriftError(f"OTC Monitor {dataset}: {tenor} date_update missing")
    try:
        return datetime.strptime(value.strip(), _DATE_FORMAT).replace(tzinfo=MSK)
    except ValueError:
        raise FormatDriftError(
            f"OTC Monitor {dataset}: {tenor} bad date_update {value!r}"
        ) from None


def parse_curve(rows: Sequence[Mapping[str, Any]], spec: CurveSpec) -> rc.ParsedCurve:
    """Build one curve from the payload rows whose series matches ``spec`` (fail closed)."""
    ds = spec.dataset
    by_tenor: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        series = row.get("series")
        norm = normalize_series(series) if isinstance(series, str) else None
        if norm is None or norm[1] != spec.suffix:
            continue
        tenor = norm[0]
        if tenor in by_tenor:
            raise FormatDriftError(f"OTC Monitor {ds}: duplicate tenor {tenor}")
        by_tenor[tenor] = row
    missing = [t for t in spec.tenors if t not in by_tenor]
    extra = sorted(set(by_tenor) - set(spec.tenors))
    if missing or extra:
        raise FormatDriftError(
            f"OTC Monitor {ds}: tenor set differs (missing {missing or '-'}, "
            f"unexpected {extra or '-'})"
        )
    parsed: list[tuple[str, Decimal | None, Decimal | None, Decimal | None]] = []
    clocks: list[datetime] = []
    for tenor in spec.tenors:
        row = by_tenor[tenor]
        bid = _pct(row.get("bid"), ds, tenor, "bid")
        ask = _pct(row.get("offer"), ds, tenor, "offer")
        mid = _pct(row.get("mid"), ds, tenor, "mid")
        if bid is not None and ask is not None:
            if not (spec.min_pct <= bid <= ask <= spec.max_pct):
                raise FormatDriftError(f"OTC Monitor {ds}: invalid bid/offer for {tenor}")
            mid = None  # recomputed from bid/offer
        elif bid is None and ask is None:
            if mid is None:
                raise FormatDriftError(f"OTC Monitor {ds}: {tenor} has no bid, offer or mid")
            if not (spec.min_pct <= mid <= spec.max_pct):
                raise FormatDriftError(f"OTC Monitor {ds}: invalid mid for {tenor}")
        else:
            raise FormatDriftError(f"OTC Monitor {ds}: one-sided quote for {tenor}")
        clocks.append(_source_ts(row.get("date_update"), ds, tenor))
        parsed.append((tenor, bid, ask, mid))
    two_sided = all(b is not None for _t, b, _a, _m in parsed)
    basis = "bid_ask_mid" if two_sided else "mid_only"
    quotes = []
    for pos, (tenor, bid, ask, mid) in enumerate(parsed):
        q_basis = "bid_ask_mid" if bid is not None else "mid_only"
        months, days = rc.tenor_parts(tenor)
        quotes.append(
            rc.CurveQuote(
                tenor,
                pos,
                bid,
                ask,
                tenor_months=months,
                tenor_days=days,
                extra={"quote_basis": q_basis} if q_basis != basis else None,
                mid=mid,
            )
        )
    return rc.ParsedCurve(
        dataset=ds,
        label=spec.suffix,
        quotes=tuple(quotes),
        source_ts=max(clocks),
        quote_basis=basis,
    )


def parse_payload(data: Any) -> ParsedPayload:
    """Parse the ``/api/bond/`` JSON (already decoded). A malformed payload (not a list of
    objects with a ``series`` key) raises :class:`FormatDriftError`; a curve that fails
    validation is reported in ``failures`` and left out."""
    if not isinstance(data, list):
        raise FormatDriftError("OTC Monitor bond: payload is not a list")
    for i, row in enumerate(data):
        if not isinstance(row, Mapping) or "series" not in row:
            raise FormatDriftError(f"OTC Monitor bond: row {i} is not an object with 'series'")
    curves: list[rc.ParsedCurve] = []
    failures: dict[str, str] = {}
    for spec in CURVES:
        try:
            curves.append(parse_curve(data, spec))
        except FormatDriftError as exc:
            failures[spec.dataset] = str(exc)
    return ParsedPayload(curves=tuple(curves), failures=failures)


def content_hash(curve: rc.ParsedCurve) -> str:
    """Normalized content hash, equal to the legacy mapping's ``curve_content_hash``:
    :func:`mdhub.sources.ratescalcs.content_hash` when every quote has bid/ask (so the hash of
    a two-sided curve is the same as Ratescalcs computes it); otherwise every quote adds its
    mid, so mid-only curves do not all hash alike. Clocks are excluded."""
    if all(q.bid_pct is not None and q.ask_pct is not None for q in curve.quotes):
        return rc.content_hash(curve)
    payload = [
        curve.quote_basis,
        [
            [
                q.tenor,
                None if q.bid_pct is None else str(q.bid_pct.normalize()),
                None if q.ask_pct is None else str(q.ask_pct.normalize()),
                None,
                None if q.mid_pct is None else str(q.mid_pct.normalize()),
            ]
            for q in curve.quotes
        ],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def fetch(http: HttpClient, token: str) -> JsonResult:
    """GET ``/api/bond/`` (JSON required). The token goes only into the ``Authorization``
    header of this request.

    :raises OtcMonitorAuthError: HTTP 401/403 (token missing, wrong or revoked).
    """
    try:
        return http.get_json(BASE_URL + BOND_PATH, headers={"Authorization": f"Bearer {token}"})
    except HttpStatusError as exc:
        if exc.status_code in (401, 403):
            raise OtcMonitorAuthError(
                f"OTC Monitor {BOND_PATH}: HTTP {exc.status_code} (token rejected)"
            ) from None
        raise
