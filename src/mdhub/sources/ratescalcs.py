"""Ratescalcs (``https://ratescalcs.ru``) dealer runs: fetch + parse, no DB.

HTTP Basic auth from ``RATESCALC_USERNAME``/``RATESCALC_PASSWORD`` (passed in by the loader),
**GET only** on the pages below - never the editor forms (POST) or ``admin/*`` pages. The
service answers Mon-Fri 08:50-23:00 MSK. Page bodies are never logged (they are short and the
credentials are weak - see docs/STATUS.md).

* ``/cnyfuturesyields/key-rate-editor/ois`` - RUONIA OIS run (textarea, first line label
  ``RUB RUONIA OIS run``), tenors 1W..10Y, bid ask in %
* ``/cnyfuturesyields/key-rate-editor/krs`` - key-rate swaps (``RUB KRS run``), 3M..10Y
* ``/cnyfuturesyields/cny-xccy-curve``      - CNY fixed vs RUONIA xccy (no label line; page
  title ``CNY XCCY``), tenors ``on, 1w .. 12m``
* ``/cnyfuturesyields/snapshot``            - JSON: ``timestamp``, ``market_closed``,
  ``cny_results``/``usd_results`` (futures-implied CNY/USD yields per futures contract), ...

Validation (fail closed): every textarea line must be ``TENOR BID ASK``; unique tenors; at
least two quotes; ``0 <= bid <= ask <= 100``. Futures yields: ``bid <= ask``, ``|x| < 100``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Final

import httpx

from mdhub.core.decimalutil import to_decimal
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient

__all__ = [
    "BASE_URL",
    "PAGES",
    "PARSER_VERSION",
    "CurveQuote",
    "ParsedCurve",
    "content_hash",
    "fetch",
    "parse_snapshot",
    "parse_textarea_page",
    "tenor_parts",
]

BASE_URL: Final = "https://ratescalcs.ru"
PARSER_VERSION: Final = "1"


@dataclass(frozen=True, slots=True)
class PageSpec:
    dataset: str
    path: str
    label: str
    """Expected label: first textarea line (``label_in_text``) or page title/h2."""
    label_in_text: bool


PAGES: Final = (
    PageSpec("ruonia_ois", "/cnyfuturesyields/key-rate-editor/ois", "RUB RUONIA OIS run", True),
    PageSpec("key_rate_swap", "/cnyfuturesyields/key-rate-editor/krs", "RUB KRS run", True),
    PageSpec("cny_ruonia_xccy", "/cnyfuturesyields/cny-xccy-curve", "CNY XCCY", False),
)
SNAPSHOT_PATH: Final = "/cnyfuturesyields/snapshot"

_QUOTE_RE = re.compile(
    r"^(?P<tenor>ON|TN|SN|\d+[DWMY])\s+(?P<bid>-?\d+(?:[.,]\d+)?)\s+(?P<ask>-?\d+(?:[.,]\d+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CurveQuote:
    tenor: str
    position: int
    bid_pct: Decimal | None
    ask_pct: Decimal | None
    tenor_months: int | None = None
    tenor_days: int | None = None
    pillar_date: date | None = None
    extra: Mapping[str, str] | None = None
    mid: Decimal | None = None
    """Quoted mid of a mid-only quote (no bid/ask, e.g. OTC Monitor off-hours); ignored when
    both bid and ask are present."""

    @property
    def mid_pct(self) -> Decimal | None:
        """(bid+ask)/2 when both are present, else the quoted :attr:`mid` (``None`` if absent)."""
        if self.bid_pct is None or self.ask_pct is None:
            return self.mid
        return (self.bid_pct + self.ask_pct) / 2


@dataclass(frozen=True, slots=True)
class ParsedCurve:
    dataset: str
    label: str
    quotes: tuple[CurveQuote, ...]
    source_ts: datetime | None = None
    market_closed: bool | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)
    quote_basis: str = "bid_ask_mid"


def tenor_parts(tenor: str) -> tuple[int | None, int | None]:
    """``(tenor_months, tenor_days)``: ``3M`` -> (3, None), ``2Y`` -> (24, None),
    ``1W`` -> (None, 7), ``ON``/``TN``/``SN`` -> (None, 1/2/3)."""
    t = tenor.upper()
    if t in ("ON", "TN", "SN"):
        return None, {"ON": 1, "TN": 2, "SN": 3}[t]
    n, unit = int(t[:-1]), t[-1]
    if unit == "M":
        return n, None
    if unit == "Y":
        return n * 12, None
    if unit == "W":
        return None, 7 * n
    return None, n


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.textareas: list[tuple[dict[str, str | None], str]] = []
        self.titles: list[str] = []
        self._in: str | None = None
        self._attrs: dict[str, str | None] = {}
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("textarea", "title", "h2"):
            self._in, self._attrs, self._buf = tag, dict(attrs), []

    def handle_endtag(self, tag: str) -> None:
        if tag == self._in:
            text = "".join(self._buf)
            if tag == "textarea":
                self.textareas.append((self._attrs, text))
            else:
                self.titles.append(" ".join(text.split()))
            self._in = None

    def handle_data(self, data: str) -> None:
        if self._in is not None:
            self._buf.append(data)


def _validated(dataset: str, tenor: str, bid: Decimal, ask: Decimal) -> None:
    if not (Decimal(0) <= bid <= ask <= Decimal(100)):
        raise FormatDriftError(f"Ratescalcs {dataset}: invalid bid/ask for {tenor}")


def parse_textarea_page(page: str, dataset: str) -> ParsedCurve:
    """Parse one editor page (``ruonia_ois``, ``key_rate_swap``, ``cny_ruonia_xccy``)."""
    spec = next((p for p in PAGES if p.dataset == dataset), None)
    if spec is None:
        raise ValueError(f"unknown Ratescalcs dataset {dataset!r}")
    parser = _PageParser()
    parser.feed(page)
    parser.close()
    areas = [text for attrs, text in parser.textareas if attrs.get("name") == "content"]
    if len(areas) != 1:
        raise FormatDriftError(f"Ratescalcs {dataset}: expected one content textarea")
    lines = [ln.strip() for ln in areas[0].splitlines() if ln.strip()]
    if spec.label_in_text:
        if not lines or not lines[0].casefold().startswith(spec.label.casefold()):
            raise FormatDriftError(f"Ratescalcs {dataset}: expected label {spec.label!r}")
        label, quote_lines = lines[0], lines[1:]
    else:
        label = next((t for t in parser.titles if spec.label.casefold() in t.casefold()), "")
        if not label:
            raise FormatDriftError(f"Ratescalcs {dataset}: page title without {spec.label!r}")
        quote_lines = lines
    quotes: list[CurveQuote] = []
    seen: set[str] = set()
    for i, line in enumerate(quote_lines):
        m = _QUOTE_RE.fullmatch(line)
        if m is None:
            raise FormatDriftError(f"Ratescalcs {dataset}: invalid quote row {i + 1}")
        tenor = m.group("tenor").upper()
        if tenor in seen:
            raise FormatDriftError(f"Ratescalcs {dataset}: duplicate tenor {tenor}")
        seen.add(tenor)
        bid = Decimal(m.group("bid").replace(",", "."))
        ask = Decimal(m.group("ask").replace(",", "."))
        _validated(dataset, tenor, bid, ask)
        months, days = tenor_parts(tenor)
        quotes.append(CurveQuote(tenor, i, bid, ask, tenor_months=months, tenor_days=days))
    if len(quotes) < 2:
        raise FormatDriftError(f"Ratescalcs {dataset}: fewer than two quotes")
    return ParsedCurve(dataset=dataset, label=label, quotes=tuple(quotes))


def _ts(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        raise FormatDriftError(f"Ratescalcs snapshot: bad {field_name} {value!r}") from None
    if dt.tzinfo is None:
        raise FormatDriftError(f"Ratescalcs snapshot: naive {field_name}")
    return dt


def _futures_quotes(items: Any, name: str) -> tuple[CurveQuote, ...]:
    if not isinstance(items, list):
        raise FormatDriftError(f"Ratescalcs snapshot: {name} is not a list")
    out: list[CurveQuote] = []
    seen: set[str] = set()
    for i, it in enumerate(items):
        if not isinstance(it, Mapping):
            raise FormatDriftError(f"Ratescalcs snapshot: {name}[{i}] is not an object")
        ticker = str(it.get("future_ticker") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{2,12}", ticker) or ticker in seen:
            raise FormatDriftError(f"Ratescalcs snapshot: {name}[{i}] bad/duplicate ticker")
        seen.add(ticker)
        bid = to_decimal(it.get("yield_bid"), field=f"{name}.yield_bid")
        ask = to_decimal(it.get("yield_offer"), field=f"{name}.yield_offer")
        for v in (bid, ask):
            if v is not None and abs(v) >= 100:
                raise FormatDriftError(f"Ratescalcs snapshot: {name} {ticker} yield {v}")
        if bid is not None and ask is not None and bid > ask:
            raise FormatDriftError(f"Ratescalcs snapshot: {name} {ticker} bid > ask")
        settle = it.get("future_settle")
        try:
            pillar = date.fromisoformat(str(settle)) if settle else None
        except ValueError:
            raise FormatDriftError(f"Ratescalcs snapshot: bad future_settle {settle!r}") from None
        days = it.get("days")
        if days is not None and (not isinstance(days, int) or isinstance(days, bool)):
            raise FormatDriftError(f"Ratescalcs snapshot: {name} {ticker} bad days")
        extra = {
            k: str(to_decimal(it.get(k), field=f"{name}.{k}"))
            for k in (
                "spot_bid",
                "spot_offer",
                "future_bid",
                "future_offer",
                "ois_bid",
                "ois_offer",
            )
            if it.get(k) is not None
        }
        out.append(
            CurveQuote(
                ticker,
                i,
                bid,
                ask,
                tenor_days=days,
                pillar_date=pillar,
                extra=extra or None,
            )
        )
    return tuple(out)


def parse_snapshot(payload: bytes | str) -> list[ParsedCurve]:
    """``/cnyfuturesyields/snapshot`` -> ``cny_futures_yields`` and ``usd_futures_yields``."""
    try:
        data = json.loads(payload)
    except ValueError:
        raise FormatDriftError("Ratescalcs snapshot: invalid JSON") from None
    if not isinstance(data, Mapping):
        raise FormatDriftError("Ratescalcs snapshot: not an object")
    for key in ("timestamp", "market_closed", "cny_results", "usd_results"):
        if key not in data:
            raise FormatDriftError(f"Ratescalcs snapshot: missing {key!r}")
    if data.get("error"):
        raise FormatDriftError("Ratescalcs snapshot: source reports an error")
    ts = _ts(data["timestamp"], "timestamp")
    closed = data["market_closed"]
    if not isinstance(closed, bool):
        raise FormatDriftError("Ratescalcs snapshot: market_closed is not boolean")
    common = {
        k: data.get(k)
        for k in ("ois_loaded_at", "inst_refreshed_at", "spot_settle", "usd_source")
        if data.get(k) is not None
    }
    out = []
    for dataset, key, stale_key in (
        ("cny_futures_yields", "cny_results", "cny_stale"),
        ("usd_futures_yields", "usd_results", "usd_yield_stale"),
    ):
        quotes = _futures_quotes(data[key], key)
        meta = {**common, "stale": data.get(stale_key)}
        out.append(
            ParsedCurve(
                dataset=dataset,
                label=key,
                quotes=quotes,
                source_ts=ts,
                market_closed=closed,
                meta={k: v for k, v in meta.items() if v is not None},
            )
        )
    return out


def content_hash(curve: ParsedCurve) -> str:
    """SHA-256 over the normalized quotes (tenor order, decimals as text) + basis.

    Clocks and status fields are excluded, so an unchanged run hashes the same."""
    payload = [
        curve.quote_basis,
        [
            [
                q.tenor,
                None if q.bid_pct is None else str(q.bid_pct.normalize()),
                None if q.ask_pct is None else str(q.ask_pct.normalize()),
                None if q.pillar_date is None else q.pillar_date.isoformat(),
            ]
            for q in curve.quotes
        ],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def fetch(http: HttpClient, path: str) -> httpx.Response:
    """GET one of the allow-listed Ratescalcs pages (``http`` must carry the Basic auth).

    The snapshot must be JSON (an HTML answer raises ``EntitlementError``); editor pages must
    be ``text/html``."""
    if path == SNAPSHOT_PATH:
        return http.get_json(BASE_URL + path).response
    if path not in {p.path for p in PAGES}:
        raise ValueError(f"refusing Ratescalcs path {path!r} (GET allow-list only)")
    resp = http.get(BASE_URL + path)
    if "text/html" not in resp.headers.get("content-type", ""):
        raise FormatDriftError(f"Ratescalcs {path}: unexpected content type")
    return resp
