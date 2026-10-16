"""MOEX ISS bond static: fetch + parse + pure normalization (no DB access).

Endpoints (all public ISS, ``iss.meta=off``):

* ``/securities.json?group_by=group&group_by_filter=stock_bonds`` - directory of every bond
  SECID (traded and archived, ~16k), ``start=`` paged by 100 without a cursor
  (:func:`iter_directory_pages`); ``q=<secid>`` for a single SECID (:func:`fetch_directory_one`).
* ``/securitygroups/stock_bonds/collections/stock_bonds_all/securities.json`` - security
  descriptions in bulk (~11.7k, cursor-paged by 1000, :func:`iter_collection_pages`).
* ``/engines/stock/markets/bonds/securities.json`` - one row per SECID x board of the bonds
  market (TQOB, TQCB, TQOD, TQRD, TQOY, SPOB, ...) with LOTSIZE, MINSTEP, DECIMALS, CURRENCYID,
  SETTLEDATE and the current MOEX labels (:func:`fetch_market`).
* ``/securities/{secid}.json`` - per-SECID description + all boards (any engine/market:
  EQRP repo-board structured notes, OTC boards) (:func:`fetch_description`).
* ``/statistics/engines/stock/markets/bonds/bondization.json`` - coupons, amortizations and
  offers of the whole market (100 rows per block and page). ISS answers deep ``start``
  offsets slowly (latency grows linearly, ~10 s at 200k), so :func:`iter_bondization_pages`
  pages coupons + amortizations inside adaptive ``from``/``till`` date windows and offers
  unfiltered, validates every cursor and checks that the window TOTALs add up to the global
  TOTALs; any inconsistency raises :class:`IssPagingError` - never partial schedules.
* ``/statistics/engines/stock/markets/bonds/bondization/{secid}.json?limit=unlimited`` -
  the same for one SECID (used for gaps and the new-listing probe). It is the same data as
  ``/securities/{secid}/bondization.json`` but carries cursor blocks, so completeness can be
  verified (:func:`fetch_bondization_one`).

Normalization rules (documented here, tested in ``tests/test_bonds_static.py``):

* **Classification** uses the *current* MOEX vocabulary (``BOND_TYPE``/``BONDTYPE``,
  ``COUPON_DETAILS``, ``FACEVALUE_TYPE``, ``COUPON_BENCHMARK``, flags ``STRUCTBOND`` ...), see
  :func:`classify_bond` / :func:`classify_coupon_type`.
* **Coupon identity**: ``coupon_no`` = 1-based position in the bond's complete MOEX schedule
  ordered by ``(coupondate, startdate, recorddate, value)`` (:func:`number_coupons`). MOEX gives
  no coupon number; a moved record/pay date keeps the number (revision via row history).
* **Coupon status** (:func:`coupon_status`): ``known`` for published values of fixed coupons
  and for paid coupons; ``current_published`` for the current period of a floater whose value
  MOEX publishes but which no curated formula states to be fixed in advance (it may be a
  projection, e.g. an average key rate over the period paid in arrears - consumers must not
  treat it as a fixed cash flow; it becomes ``known`` when paid or when a curated formula
  says ``fixing_calendar_days_before_start``); ``projected_by_moex`` for floating coupons
  beyond the current period (MOEX forward-fills the current rate), for the current period of
  in-arrears benchmarks (RUONIA, RUONIA index, CPI), and for future amounts on an indexed face
  or of bonds whose coupon type is unknown; ``unknown`` when MOEX publishes nothing
  (amount/rate stored as NULL, also when MOEX sends 0 for a non-zero coupon bond: missing is
  not zero).
* An *active* bond without MOEX fixed/floating labels is not classified ``fixed`` just
  because its whole schedule carries values (``coupon_type = unknown``, basis ``schedule``):
  MOEX forward-fills floating coupons. Exception (:func:`schedule_constant_fixed`, basis
  ``schedule_constant``; never for structured notes): no benchmark / COUPON_DETAILS and no
  rate formula from any source, >= 2 paid coupons, one constant rate (``value_prc``) over all
  paid and future coupons, and a CBR key-rate change between the start of the first and the
  start of the last paid coupon period - a key-rate floater would have shown different paid
  rates, so only a genuinely fixed coupon passes (a floater on an unchanged key rate cannot).
* **Offers** (:func:`normalize_offers`): ``0000-00-00`` -> ``offer_date`` NULL +
  ``date_missing``; type put/call/buyback/unknown from the bond's PUT/CALL/BUYBACK option
  dates, the holder submission window, then ``BOND_SUBTYPE``; status from the MOEX suffix
  ``(состоялось)``/``(отменено)``/``(дефолт)``.
* Currency ``SUR`` is normalized to ``RUB``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx

from mdhub.core.http import HttpClient
from mdhub.core.iss import (
    IssFormatError,
    IssPage,
    IssPagingError,
    iter_pages,
    iter_pages_until_empty,
    parse_block,
    parse_cursor,
    to_date,
    to_decimal,
    to_int,
    to_str,
)

__all__ = [
    "BONDIZATION_BLOCKS",
    "MINFIN_INN",
    "PARSER_VERSION",
    "AmortRow",
    "BondizationData",
    "BondizationPage",
    "ClassifyInput",
    "CouponRate",
    "CouponRow",
    "OfferRow",
    "OptionDates",
    "canonical_sha256",
    "classify_bond",
    "classify_coupon_type",
    "coupon_status",
    "fetch_bondization_one",
    "fetch_description",
    "fetch_directory_one",
    "fetch_market",
    "iter_bondization_pages",
    "iter_collection_pages",
    "iter_directory_pages",
    "map_benchmark",
    "norm_ccy",
    "normalize_offers",
    "number_coupons",
    "ordered_map",
    "parse_bondization",
    "parse_bondization_page",
    "parse_bondization_rows",
    "parse_collection_rows",
    "parse_description",
    "parse_directory_rows",
    "parse_market",
    "schedule_constant_fixed",
    "settle_lag",
    "stable_view",
]

PARSER_VERSION: Final = "1"

DIRECTORY_URL: Final = "/securities.json"
COLLECTION_URL: Final = "/securitygroups/stock_bonds/collections/stock_bonds_all/securities.json"
MARKET_URL: Final = "/engines/stock/markets/bonds/securities.json"
BONDIZATION_URL: Final = "/statistics/engines/stock/markets/bonds/bondization.json"
BONDIZATION_ONE_URL: Final = "/statistics/engines/stock/markets/bonds/bondization/{secid}.json"
DESCRIPTION_URL: Final = "/securities/{secid}.json"

BONDIZATION_BLOCKS: Final = ("coupons", "amortizations", "offers")
BONDIZATION_PAGE: Final = 100
MINFIN_INN: Final = "7710168360"

_SECID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,51}$")

DIRECTORY_REQUIRED: Final = (
    "secid",
    "shortname",
    "regnumber",
    "name",
    "isin",
    "is_traded",
    "emitent_id",
    "emitent_title",
    "emitent_inn",
    "emitent_okpo",
    "type",
    "primary_boardid",
    "marketprice_boardid",
)
COLLECTION_REQUIRED: Final = (
    "SECID",
    "NAME",
    "SHORTNAME",
    "ISIN",
    "ISSUEDATE",
    "MATDATE",
    "INITIALFACEVALUE",
    "FACEUNIT",
    "FACEVALUE",
    "STARTDATEMOEX",
    "LISTLEVEL",
    "ISSUESIZE",
    "ISQUALIFIEDINVESTORS",
    "COUPONFREQUENCY",
    "COUPON_BENCHMARK",
    "COUPON_BENCHMARK_SPREAD",
    "HIGHRISK",
    "BOND_TYPE",
    "BOND_SUBTYPE",
    "AMORTBOND",
    "STRUCTBOND",
    "SUBORDBOND",
    "FACEVALUE_TYPE",
    "COUPON_DETAILS",
    "TYPE",
)
MARKET_REQUIRED: Final = (
    "SECID",
    "BOARDID",
    "BOARDNAME",
    "SHORTNAME",
    "LOTSIZE",
    "FACEVALUE",
    "STATUS",
    "MATDATE",
    "DECIMALS",
    "COUPONPERIOD",
    "ISSUESIZE",
    "MINSTEP",
    "FACEUNIT",
    "ISIN",
    "CURRENCYID",
    "ISSUESIZEPLACED",
    "LISTLEVEL",
    "SETTLEDATE",
    "FACEVALUEONSETTLEDATE",
    "OFFERDATE",
    "BUYBACKDATE",
    "CALLOPTIONDATE",
    "PUTOPTIONDATE",
    "BONDTYPE",
    "BONDSUBTYPE",
    "COUPON_DETAILS",
    "FACEVALUE_TYPE",
)
COUPON_REQUIRED: Final = (
    "isin",
    "name",
    "issuevalue",
    "coupondate",
    "recorddate",
    "startdate",
    "initialfacevalue",
    "facevalue",
    "faceunit",
    "value",
    "valueprc",
    "value_rub",
    "secid",
)
AMORT_REQUIRED: Final = (
    "isin",
    "amortdate",
    "facevalue",
    "initialfacevalue",
    "faceunit",
    "valueprc",
    "value",
    "value_rub",
    "data_source",
    "secid",
)
OFFER_REQUIRED: Final = (
    "isin",
    "offerdate",
    "offerdatestart",
    "offerdateend",
    "facevalue",
    "faceunit",
    "price",
    "value",
    "agent",
    "offertype",
    "secid",
)
DESCRIPTION_REQUIRED: Final = ("name", "title", "value", "type")
BOARDS_REQUIRED: Final = (
    "secid",
    "boardid",
    "title",
    "market",
    "engine",
    "is_traded",
    "decimals",
    "listed_from",
    "is_primary",
    "currencyid",
)

# Fields that change daily (accrued interest, days to maturity, quotes...) are excluded from
# the content hash of ref.static_snapshot so the log records term changes, not ticks.
VOLATILE_FIELDS: Final = frozenset(
    {
        "DAYSTOREDEMPTION",
        "COUPONDATE",
        "COUPONPERCENT",
        "COUPONVALUE",
        "UPDATED_AT",
        "PREVWAPRICE",
        "YIELDATPREVWAPRICE",
        "NEXTCOUPON",
        "ACCRUEDINT",
        "PREVPRICE",
        "PREVLEGALCLOSEPRICE",
        "PREVDATE",
        "SETTLEDATE",
        "FACEVALUEONSETTLEDATE",
        "LOTVALUE",
        "listed_till",
        "history_from",
        "history_till",
    }
)

# ------------------------------------------------------------------------- converters


def norm_ccy(value: Any) -> str | None:
    """MOEX currency code -> ISO (``SUR``/``RUR`` -> ``RUB``); ``None`` stays ``None``."""
    text = to_str(value)
    if text is None:
        return None
    text = text.upper()
    return "RUB" if text in {"SUR", "RUR"} else text


def to_bool(value: Any, *, field: str = "value") -> bool | None:
    """ISS flag (1/0, "1"/"0", true/false) -> bool; ``None``/``""`` -> ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"0", "1", "true", "false"}:
        return value.strip().lower() in {"1", "true"}
    raise IssFormatError(f"{field}: not a flag: {value!r}")


def _check_secid(secid: str) -> str:
    if not _SECID_RE.fullmatch(secid):
        raise ValueError(f"invalid SECID {secid!r}")
    return secid


DATE_FIELDS: Final = frozenset(
    {"ISSUEDATE", "MATDATE", "BUYBACKDATE", "STARTDATEMOEX", "REGISTRY_DATE", "COUPONDATE"}
)
DECIMAL_FIELDS: Final = frozenset(
    {
        "INITIALFACEVALUE",
        "FACEVALUE",
        "ISSUESIZE",
        "ISSUESIZEPLANNED",
        "COUPONPERCENT",
        "COUPONVALUE",
        "COUPON_BENCHMARK_SPREAD",
        "BUYBACKPRICE",
    }
)
INT_FIELDS: Final = frozenset({"LISTLEVEL", "COUPONFREQUENCY", "DAYSTOREDEMPTION", "EMITTER_ID"})
BOOL_FIELDS: Final = frozenset(
    {
        "ISQUALIFIEDINVESTORS",
        "HIGHRISK",
        "AMORTBOND",
        "STRUCTBOND",
        "SUBORDBOND",
        "HASDEFAULT",
        "HASTECHNICALDEFAULT",
        "IS_TRADED",
        "HASPROSPECTUS",
        "ISCONCESSIONAGREEMENT",
        "INCLUDEDBYMOEX",
        "EMITENTMISMATCHFIRST",
        "EMITENTMISMATCHCUR",
        "MORNINGSESSION",
        "EVENINGSESSION",
        "WEEKENDSESSION",
    }
)


def _convert_security_field(name: str, value: Any) -> Any:
    if name in DATE_FIELDS:
        return to_date(value, field=name)
    if name in DECIMAL_FIELDS:
        return to_decimal(value, field=name)
    if name in INT_FIELDS:
        dec = to_decimal(value, field=name)
        if dec is None:
            return None
        if dec != dec.to_integral_value():
            raise IssFormatError(f"{name}: not an integer: {value!r}")
        return int(dec)
    if name in BOOL_FIELDS:
        return to_bool(value, field=name)
    return to_str(value)


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of the canonical JSON of ``obj`` (sorted keys, no whitespace)."""
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def stable_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """``row`` without :data:`VOLATILE_FIELDS` (input of the static_snapshot hash)."""
    return {k: v for k, v in row.items() if k not in VOLATILE_FIELDS}


# -------------------------------------------------------------------------- directory


def iter_directory_pages(http: HttpClient) -> Iterator[IssPage]:
    """Pages (100 rows) of the stock_bonds directory, until an empty page."""
    return iter_pages_until_empty(
        http,
        DIRECTORY_URL,
        {"group_by": "group", "group_by_filter": "stock_bonds", "limit": 100},
        block="securities",
        required=DIRECTORY_REQUIRED,
    )


def fetch_directory_one(http: HttpClient, secid: str) -> httpx.Response:
    """Directory search ``q=<secid>`` (stock_bonds group); see :func:`parse_directory_payload`."""
    res = http.get_json(
        DIRECTORY_URL,
        params={
            "iss.meta": "off",
            "q": _check_secid(secid),
            "group_by": "group",
            "group_by_filter": "stock_bonds",
        },
    )
    return res.response


def parse_directory_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Typed directory rows (keys as in ISS, lower case)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        secid = to_str(r.get("secid"))
        if secid is None:
            raise IssFormatError("directory row without secid")
        out.append(
            {
                "secid": secid,
                "shortname": to_str(r.get("shortname")),
                "regnumber": to_str(r.get("regnumber")),
                "name": to_str(r.get("name")),
                "isin": to_str(r.get("isin")),
                "is_traded": to_bool(r.get("is_traded"), field="is_traded"),
                "emitent_id": to_int(r.get("emitent_id"), field="emitent_id"),
                "emitent_title": to_str(r.get("emitent_title")),
                "emitent_inn": to_str(r.get("emitent_inn")),
                "emitent_okpo": to_str(r.get("emitent_okpo")),
                "type": to_str(r.get("type")),
                "primary_boardid": to_str(r.get("primary_boardid")),
                "marketprice_boardid": to_str(r.get("marketprice_boardid")),
                "_raw": dict(r),
            }
        )
    return out


def parse_directory_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return parse_directory_rows(parse_block(payload, "securities", required=DIRECTORY_REQUIRED))


# ------------------------------------------------------------------------- collection


def iter_collection_pages(http: HttpClient) -> Iterator[IssPage]:
    """Cursor-paged stock_bonds_all collection (1000 rows per page, TOTAL verified)."""
    return iter_pages(
        http,
        COLLECTION_URL,
        {"limit": 1000},
        block="securities",
        cursor_block="securities.cursor",
        required=COLLECTION_REQUIRED,
    )


def parse_collection_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Typed collection rows: ``{FIELD: converted value}`` + ``_raw``."""
    out: list[dict[str, Any]] = []
    for r in rows:
        rec: dict[str, Any] = {k: _convert_security_field(k, v) for k, v in r.items()}
        if not rec.get("SECID"):
            raise IssFormatError("collection row without SECID")
        rec["_raw"] = dict(r)
        out.append(rec)
    return out


# ----------------------------------------------------------------------------- market


@dataclass(frozen=True, slots=True)
class MarketData:
    """Parsed bonds market list: trade date of the data version and typed board rows."""

    trade_date: date | None
    rows: list[dict[str, Any]]


def fetch_market(http: HttpClient) -> httpx.Response:
    """The bonds market securities list (single unpaged response, ~4 MB)."""
    res = http.get_json(
        MARKET_URL, params={"iss.meta": "off", "iss.only": "securities,dataversion"}
    )
    return res.response


def parse_market(payload: Mapping[str, Any]) -> MarketData:
    """Typed market rows (one per SECID x BOARDID); duplicates raise."""
    rows = parse_block(payload, "securities", required=MARKET_REQUIRED)
    dv = parse_block(payload, "dataversion", optional=True)
    trade_date = to_date(dv[0].get("trade_date"), field="trade_date") if dv else None
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        secid, board = to_str(r["SECID"]), to_str(r["BOARDID"])
        if secid is None or board is None:
            raise IssFormatError("market row without SECID/BOARDID")
        if (secid, board) in seen:
            raise IssFormatError(f"market list: duplicate row {secid}/{board}")
        seen.add((secid, board))
        out.append(
            {
                "SECID": secid,
                "BOARDID": board,
                "BOARDNAME": to_str(r["BOARDNAME"]),
                "SHORTNAME": to_str(r["SHORTNAME"]),
                "SECNAME": to_str(r.get("SECNAME")),
                "LATNAME": to_str(r.get("LATNAME")),
                "REGNUMBER": to_str(r.get("REGNUMBER")),
                "ISIN": to_str(r["ISIN"]),
                "LOTSIZE": to_int(r["LOTSIZE"], field="LOTSIZE"),
                "FACEVALUE": to_decimal(r["FACEVALUE"], field="FACEVALUE"),
                "STATUS": to_str(r["STATUS"]),
                "MATDATE": to_date(r["MATDATE"], field="MATDATE"),
                "DECIMALS": to_int(r["DECIMALS"], field="DECIMALS"),
                "COUPONPERIOD": to_int(r["COUPONPERIOD"], field="COUPONPERIOD"),
                "ISSUESIZE": to_decimal(r["ISSUESIZE"], field="ISSUESIZE"),
                "MINSTEP": to_decimal(r["MINSTEP"], field="MINSTEP"),
                "FACEUNIT": norm_ccy(r["FACEUNIT"]),
                "CURRENCYID": norm_ccy(r["CURRENCYID"]),
                "ISSUESIZEPLACED": to_decimal(r["ISSUESIZEPLACED"], field="ISSUESIZEPLACED"),
                "LISTLEVEL": to_int(r["LISTLEVEL"], field="LISTLEVEL"),
                "SETTLEDATE": to_date(r["SETTLEDATE"], field="SETTLEDATE"),
                "FACEVALUEONSETTLEDATE": to_decimal(
                    r["FACEVALUEONSETTLEDATE"], field="FACEVALUEONSETTLEDATE"
                ),
                "OFFERDATE": to_date(r["OFFERDATE"], field="OFFERDATE"),
                "BUYBACKDATE": to_date(r["BUYBACKDATE"], field="BUYBACKDATE"),
                "CALLOPTIONDATE": to_date(r["CALLOPTIONDATE"], field="CALLOPTIONDATE"),
                "PUTOPTIONDATE": to_date(r["PUTOPTIONDATE"], field="PUTOPTIONDATE"),
                "BONDTYPE": to_str(r["BONDTYPE"]),
                "BONDSUBTYPE": to_str(r["BONDSUBTYPE"]),
                "COUPON_DETAILS": to_str(r["COUPON_DETAILS"]),
                "FACEVALUE_TYPE": to_str(r["FACEVALUE_TYPE"]),
                "_raw": dict(r),
            }
        )
    return MarketData(trade_date=trade_date, rows=out)


# ------------------------------------------------------------------------ description


@dataclass(frozen=True, slots=True)
class Description:
    """Per-SECID description: typed fields (collection names) and the boards block."""

    secid: str
    fields: dict[str, Any]
    boards: list[dict[str, Any]]


def fetch_description(http: HttpClient, secid: str) -> httpx.Response:
    res = http.get_json(
        DESCRIPTION_URL.format(secid=_check_secid(secid)),
        params={"iss.meta": "off", "iss.only": "description,boards"},
    )
    return res.response


def parse_description(payload: Mapping[str, Any], secid: str) -> Description:
    """Parse ``/securities/{secid}.json``.

    :raises IssFormatError: blocks missing, the description does not belong to ``secid``.
    """
    rows = parse_block(payload, "description", required=DESCRIPTION_REQUIRED)
    fields: dict[str, Any] = {}
    for r in rows:
        name = to_str(r.get("name"))
        if name is None:
            continue
        name = name.upper()
        value = r.get("value")
        typ = to_str(r.get("type"))
        if name in DATE_FIELDS or typ == "date":
            fields[name] = to_date(value, field=name)
        elif name in BOOL_FIELDS or typ == "boolean":
            fields[name] = to_bool(value, field=name)
        elif name in INT_FIELDS:
            fields[name] = _convert_security_field(name, value)
        elif name in DECIMAL_FIELDS or typ == "number":
            fields[name] = to_decimal(value, field=name)
        else:
            fields[name] = to_str(value)
    if fields and fields.get("SECID") not in (None, secid):
        raise IssFormatError(f"description for {fields.get('SECID')!r}, expected {secid!r}")
    boards: list[dict[str, Any]] = []
    for b in parse_block(payload, "boards", required=BOARDS_REQUIRED, optional=True):
        board = to_str(b.get("boardid"))
        if board is None:
            raise IssFormatError(f"{secid}: board row without boardid")
        boards.append(
            {
                "board": board,
                "title": to_str(b.get("title")),
                "engine": to_str(b.get("engine")),
                "market": to_str(b.get("market")),
                "board_is_traded": to_bool(b.get("is_traded"), field="is_traded"),
                "decimals": to_int(b.get("decimals"), field="decimals"),
                "listed_from": to_date(b.get("listed_from"), field="listed_from"),
                "is_primary": bool(to_bool(b.get("is_primary"), field="is_primary")),
                "currency_id": norm_ccy(b.get("currencyid")),
            }
        )
    return Description(secid=secid, fields=fields, boards=boards)


# ------------------------------------------------------------------------- bondization


@dataclass(frozen=True, slots=True)
class CouponRow:
    secid: str
    isin: str | None
    name: str | None
    pay_date: date
    start_date: date | None
    record_date: date | None
    value: Decimal | None
    value_prc: Decimal | None
    value_rub: Decimal | None
    face_value: Decimal | None
    initial_face_value: Decimal | None
    face_unit: str | None
    issue_value: Decimal | None
    capture_id: int | None


@dataclass(frozen=True, slots=True)
class AmortRow:
    secid: str
    amort_date: date
    data_source: str
    value_prc: Decimal | None
    value: Decimal | None
    value_rub: Decimal | None
    face_value: Decimal | None
    initial_face_value: Decimal | None
    face_unit: str | None
    capture_id: int | None


@dataclass(frozen=True, slots=True)
class OfferRow:
    secid: str
    offer_date_raw: str | None
    offer_date: date | None
    offer_date_start: date | None
    offer_date_end: date | None
    price: Decimal | None
    value: Decimal | None
    face_value: Decimal | None
    face_unit: str | None
    agent: str | None
    offer_type_raw: str | None
    capture_id: int | None


@dataclass(slots=True)
class BondizationData:
    """Schedule rows grouped per SECID (insertion order = source order)."""

    coupons: dict[str, list[CouponRow]] = field(default_factory=dict)
    amortizations: dict[str, list[AmortRow]] = field(default_factory=dict)
    offers: dict[str, list[OfferRow]] = field(default_factory=dict)
    raw: dict[str, dict[str, list[Any]]] = field(default_factory=dict)
    """``secid -> block -> raw row lists`` (input of the static_snapshot hash)."""

    def secids(self) -> set[str]:
        return set(self.coupons) | set(self.amortizations) | set(self.offers)

    def extend(self, other: BondizationData) -> None:
        for name in ("coupons", "amortizations", "offers"):
            mine: dict[str, list[Any]] = getattr(self, name)
            for secid, rows in getattr(other, name).items():
                mine.setdefault(secid, []).extend(rows)
        for secid, blocks in other.raw.items():
            target = self.raw.setdefault(secid, {})
            for block, rows in blocks.items():
                target.setdefault(block, []).extend(rows)


def _secid_of(row: Mapping[str, Any], block: str) -> str:
    secid = to_str(row.get("secid"))
    if secid is None:
        raise IssFormatError(f"bondization {block} row without secid")
    return secid


def parse_bondization(
    payload: Mapping[str, Any], *, capture_id: int | None = None, require_offers: bool = True
) -> BondizationData:
    """Parse the coupons/amortizations/offers blocks of a bondization payload."""
    return parse_bondization_rows(
        {
            "coupons": parse_block(payload, "coupons", required=COUPON_REQUIRED),
            "amortizations": parse_block(payload, "amortizations", required=AMORT_REQUIRED),
            "offers": parse_block(
                payload, "offers", required=OFFER_REQUIRED, optional=not require_offers
            ),
        },
        capture_id=capture_id,
    )


def parse_bondization_rows(
    blocks: Mapping[str, Sequence[Mapping[str, Any]]], *, capture_id: int | None = None
) -> BondizationData:
    """Typed schedule rows from already validated block rows (missing blocks = no rows)."""
    data = BondizationData()
    for r in blocks.get("coupons", ()):
        secid = _secid_of(r, "coupons")
        pay = to_date(r["coupondate"], field="coupondate")
        if pay is None:
            raise IssFormatError(f"{secid}: coupon without coupondate")
        data.coupons.setdefault(secid, []).append(
            CouponRow(
                secid=secid,
                isin=to_str(r["isin"]),
                name=to_str(r["name"]),
                pay_date=pay,
                start_date=to_date(r["startdate"], field="startdate"),
                record_date=to_date(r["recorddate"], field="recorddate"),
                value=to_decimal(r["value"], field="value"),
                value_prc=to_decimal(r["valueprc"], field="valueprc"),
                value_rub=to_decimal(r["value_rub"], field="value_rub"),
                face_value=to_decimal(r["facevalue"], field="facevalue"),
                initial_face_value=to_decimal(r["initialfacevalue"], field="initialfacevalue"),
                face_unit=norm_ccy(r["faceunit"]),
                issue_value=to_decimal(r["issuevalue"], field="issuevalue"),
                capture_id=capture_id,
            )
        )
        data.raw.setdefault(secid, {}).setdefault("coupons", []).append(r)
    for r in blocks.get("amortizations", ()):
        secid = _secid_of(r, "amortizations")
        when = to_date(r["amortdate"], field="amortdate")
        if when is None:
            raise IssFormatError(f"{secid}: amortization without amortdate")
        data.amortizations.setdefault(secid, []).append(
            AmortRow(
                secid=secid,
                amort_date=when,
                data_source=to_str(r["data_source"]) or "unknown",
                value_prc=to_decimal(r["valueprc"], field="valueprc"),
                value=to_decimal(r["value"], field="value"),
                value_rub=to_decimal(r["value_rub"], field="value_rub"),
                face_value=to_decimal(r["facevalue"], field="facevalue"),
                initial_face_value=to_decimal(r["initialfacevalue"], field="initialfacevalue"),
                face_unit=norm_ccy(r["faceunit"]),
                capture_id=capture_id,
            )
        )
        data.raw.setdefault(secid, {}).setdefault("amortizations", []).append(r)
    for r in blocks.get("offers", ()):
        secid = _secid_of(r, "offers")
        raw_date = to_str(r["offerdate"])
        data.offers.setdefault(secid, []).append(
            OfferRow(
                secid=secid,
                offer_date_raw=raw_date,
                offer_date=to_date(raw_date, field="offerdate"),
                offer_date_start=to_date(r["offerdatestart"], field="offerdatestart"),
                offer_date_end=to_date(r["offerdateend"], field="offerdateend"),
                price=to_decimal(r["price"], field="price"),
                value=to_decimal(r["value"], field="value"),
                face_value=to_decimal(r["facevalue"], field="facevalue"),
                face_unit=norm_ccy(r["faceunit"]),
                agent=to_str(r["agent"]),
                offer_type_raw=to_str(r["offertype"]),
                capture_id=capture_id,
            )
        )
        data.raw.setdefault(secid, {}).setdefault("offers", []).append(r)
    return data


@dataclass(frozen=True, slots=True)
class BondizationPage:
    """One validated page of the bulk bondization crawl.

    ``rows`` holds the rows to use (block -> rows). Discovery requests (global totals,
    window probes that get split) are yielded with empty ``rows`` so the caller can still
    record the raw response.
    """

    start: int
    totals: dict[str, int]
    rows: dict[str, list[dict[str, Any]]]
    response: httpx.Response
    payload: Mapping[str, Any]
    window: tuple[date, date] | None = None
    pagesize: int = BONDIZATION_PAGE


class ShortPageError(IssPagingError):
    """A bulk bondization page has fewer rows than its cursor promises (re-requestable)."""


_REQUIRED_BY_BLOCK: Final = {
    "coupons": COUPON_REQUIRED,
    "amortizations": AMORT_REQUIRED,
    "offers": OFFER_REQUIRED,
}
DATED_BLOCKS: Final = ("coupons", "amortizations")
WINDOW_FROM: Final = date(1900, 1, 1)
WINDOW_TILL: Final = date(2300, 12, 31)


def _iss_only(blocks: Sequence[str]) -> str:
    return ",".join(f"{b},{b}.cursor" for b in blocks)


def _validate_bulk_page(
    payload: Mapping[str, Any],
    start: int,
    blocks: Sequence[str],
    totals: Mapping[str, int] | None,
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], int]:
    """Validate one page of ``blocks``. Returns (totals, rows per block, pagesize).

    :raises ShortPageError: row count short (the same page may be re-requested).
    :raises IssPagingError: any other cursor inconsistency.
    """
    new_totals: dict[str, int] = {}
    rows: dict[str, list[dict[str, Any]]] = {}
    pagesize = 0
    for block in blocks:
        block_rows = parse_block(payload, block, required=_REQUIRED_BY_BLOCK[block])
        cursor = parse_cursor(payload, f"{block}.cursor")
        if cursor is None:
            raise IssPagingError(f"bondization: {block}.cursor missing at start={start}")
        if totals is not None and cursor.total != totals[block]:
            raise IssPagingError(
                f"bondization: {block} TOTAL changed {totals[block]} -> {cursor.total}"
            )
        expected = min(cursor.pagesize, max(cursor.total - start, 0))
        if expected and cursor.index != start:
            raise IssPagingError(f"bondization: {block} INDEX {cursor.index} != start {start}")
        if len(block_rows) != expected:
            raise ShortPageError(
                f"bondization: {block} has {len(block_rows)} rows at start={start}, "
                f"expected {expected} (TOTAL {cursor.total})"
            )
        if pagesize and cursor.pagesize != pagesize:
            raise IssPagingError("bondization: blocks report different PAGESIZE")
        pagesize = cursor.pagesize
        new_totals[block] = cursor.total
        rows[block] = block_rows
    if pagesize <= 0:
        raise IssPagingError("bondization: PAGESIZE must be positive")
    return new_totals, rows, pagesize


def _fetch_bulk_page(
    http: HttpClient,
    start: int,
    blocks: Sequence[str],
    totals: Mapping[str, int] | None,
    window: tuple[date, date] | None,
    max_page_attempts: int,
) -> BondizationPage:
    params: dict[str, Any] = {
        "iss.meta": "off",
        "iss.only": _iss_only(blocks),
        "limit": BONDIZATION_PAGE,
        "start": start,
    }
    if window is not None:
        params["from"], params["till"] = window[0].isoformat(), window[1].isoformat()
    attempt = 0
    while True:
        attempt += 1
        res = http.get_json(BONDIZATION_URL, params=params)
        try:
            page_totals, rows, pagesize = _validate_bulk_page(res.data, start, blocks, totals)
        except ShortPageError:
            if attempt < max_page_attempts:
                continue
            raise
        return BondizationPage(
            start=start,
            totals=page_totals,
            rows=rows,
            response=res.response,
            payload=res.data,
            window=window,
            pagesize=pagesize,
        )


def ordered_map[T, R](
    fn: Callable[[T], R], items: Iterable[T], *, workers: int = 1, window: int | None = None
) -> Iterator[R]:
    """``map(fn, items)`` with up to ``workers`` calls in flight, results in input order.

    Used for I/O-bound ISS requests: latency (~0.3-0.5 s) otherwise caps a sequential crawl
    well below the configured rate; the shared per-host rate limiter of :class:`HttpClient`
    still bounds the request rate. At most ``window`` (default ``4 * workers``) results are
    buffered. An exception is raised when its item's turn comes (pending calls are cancelled).
    """
    if workers <= 1:
        yield from map(fn, items)
        return
    window = window or 4 * workers
    it = iter(items)
    pending: deque[Future[R]] = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mdhub-iss") as pool:
        try:
            for item in it:
                pending.append(pool.submit(fn, item))
                if len(pending) >= window:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            for f in pending:
                f.cancel()


def _initial_windows() -> list[tuple[date, date]]:
    out = [(WINDOW_FROM, date(1999, 12, 31))]
    out += [(date(y, 1, 1), date(y, 12, 31)) for y in range(2000, 2061)]
    out.append((date(2061, 1, 1), WINDOW_TILL))
    return out


def _split(window: tuple[date, date]) -> list[tuple[date, date]]:
    a, b = window
    mid = a + (b - a) // 2
    return [(a, mid), (mid + timedelta(days=1), b)]


def iter_bondization_pages(
    http: HttpClient,
    *,
    max_page_attempts: int = 3,
    workers: int = 1,
    split_rows: int = 3000,
) -> Iterator[BondizationPage]:
    """Crawl the bulk bondization endpoint completely, with strict validation.

    ISS answers deep ``start`` offsets of this endpoint slowly (latency grows linearly with
    the offset: ~10 s at 200k rows), so plain paging is quadratic. Instead:

    1. one unfiltered request gives the global TOTAL of every block;
    2. offers (~6.5k rows, including undated ``0000-00-00`` ones) are paged unfiltered;
    3. coupons + amortizations are paged inside date windows (``from``/``till``, inclusive);
       a window whose larger block exceeds ``split_rows`` is split in halves (probe
       responses of split windows are yielded with empty ``rows``);
    4. every page must repeat its window's TOTALs, and the window TOTALs must add up to the
       global TOTALs - otherwise :class:`IssPagingError` (rows outside all windows, data
       changed during the crawl). Never partial schedules.
    """
    first = _fetch_bulk_page(http, 0, BONDIZATION_BLOCKS, None, None, max_page_attempts)
    grand = first.totals
    yield replace(first, rows={})

    # offers: unfiltered paging (small block, includes undated rows)
    offer_starts = list(range(0, grand["offers"], first.pagesize))
    received_offers = 0
    for page in ordered_map(
        lambda st: _fetch_bulk_page(
            http, st, ("offers",), {"offers": grand["offers"]}, None, max_page_attempts
        ),
        offer_starts,
        workers=workers,
    ):
        received_offers += len(page.rows["offers"])
        yield page
    if received_offers != grand["offers"]:
        raise IssPagingError(
            f"bondization: offers received {received_offers}, TOTAL {grand['offers']}"
        )

    # coupons + amortizations: adaptive date windows
    final: list[BondizationPage] = []
    level = _initial_windows()
    while level:
        probes = list(
            ordered_map(
                lambda w: _fetch_bulk_page(http, 0, DATED_BLOCKS, None, w, max_page_attempts),
                level,
                workers=workers,
            )
        )
        level = []
        for probe in probes:
            assert probe.window is not None
            biggest = max(probe.totals.values())
            if biggest > split_rows and probe.window[0] < probe.window[1]:
                level += _split(probe.window)
                yield replace(probe, rows={})
            else:
                final.append(probe)
    sums = dict.fromkeys(DATED_BLOCKS, 0)
    tasks: list[tuple[tuple[date, date], int, dict[str, int]]] = []
    for probe in sorted(final, key=lambda p: p.window or (WINDOW_FROM, WINDOW_FROM)):
        assert probe.window is not None
        for block in DATED_BLOCKS:
            sums[block] += probe.totals[block]
        yield probe
        biggest = max(probe.totals.values())
        tasks += [
            (probe.window, st, probe.totals)
            for st in range(probe.pagesize, biggest, probe.pagesize)
        ]
    for block in DATED_BLOCKS:
        if sums[block] != grand[block]:
            raise IssPagingError(
                f"bondization: {block} windows hold {sums[block]} rows, TOTAL {grand[block]}"
            )
    received = dict.fromkeys(DATED_BLOCKS, 0)
    for page in ordered_map(
        lambda t: _fetch_bulk_page(http, t[1], DATED_BLOCKS, t[2], t[0], max_page_attempts),
        tasks,
        workers=workers,
    ):
        for block in DATED_BLOCKS:
            received[block] += len(page.rows[block])
        yield page
    for probe in final:
        for block in DATED_BLOCKS:
            received[block] += len(probe.rows[block])
    for block in DATED_BLOCKS:
        if received[block] != grand[block]:
            raise IssPagingError(
                f"bondization: {block} received {received[block]} rows, TOTAL {grand[block]}"
            )


def parse_bondization_page(
    page: BondizationPage, *, capture_id: int | None = None
) -> BondizationData:
    """Rows of one crawl page (empty for discovery pages)."""
    return parse_bondization_rows(page.rows, capture_id=capture_id)


def fetch_bondization_one(http: HttpClient, secid: str) -> httpx.Response:
    """Complete per-SECID bondization (``limit=unlimited``); validate with
    :func:`parse_bondization_one`."""
    res = http.get_json(
        BONDIZATION_ONE_URL.format(secid=_check_secid(secid)),
        params={"iss.meta": "off", "limit": "unlimited"},
    )
    return res.response


def parse_bondization_one(
    payload: Mapping[str, Any], secid: str, *, capture_id: int | None = None
) -> BondizationData:
    """Parse a per-SECID bondization and verify coupon/amortization TOTALs and SECIDs."""
    for block in ("coupons", "amortizations"):
        # limit=unlimited: PAGESIZE is the row count (0 for an empty block), so the generic
        # cursor parser (PAGESIZE > 0) does not apply; TOTAL must equal the rows received.
        cursor = parse_block(
            payload, f"{block}.cursor", required=("INDEX", "TOTAL", "PAGESIZE"), optional=True
        )
        rows = parse_block(payload, block)
        if cursor:
            total = to_int(cursor[0]["TOTAL"], field=f"{block}.cursor TOTAL")
            if len(cursor) != 1 or total != len(rows):
                raise IssPagingError(
                    f"bondization/{secid}: {block} has {len(rows)} rows, TOTAL {total}"
                )
    data = parse_bondization(payload, capture_id=capture_id, require_offers=False)
    foreign = data.secids() - {secid}
    if foreign:
        raise IssFormatError(f"bondization/{secid}: rows for other SECIDs {sorted(foreign)}")
    return data


# ------------------------------------------------------------------- classification

BENCHMARK_MAP: Final = {
    "RREFKEYR": "KEY_RATE",
    "RUONIA": "RUONIA",
    "RUONIA3M": "RUONIA_3M",
    "RUONIA6M": "RUONIA_6M",
    "RUONIA_IND": "RUONIA_INDEX",
    "CPI": "CPI",
    "ZR_YLD_CRV": "ZCYC",
    "OTHER": "OTHER",
}
COUPON_DETAILS_BENCHMARK: Final = {
    "Ключевая ставка": "KEY_RATE",
    "Ставка RUONIA": "RUONIA",
    "Срочная ставка RUONIA 3m": "RUONIA_3M",
    "Срочная ставка RUONIA 6m": "RUONIA_6M",
    "Индекс RUONIA": "RUONIA_INDEX",
    "ИПЦ": "CPI",
}
IN_ARREARS_BENCHMARKS: Final = frozenset({"RUONIA", "RUONIA_INDEX", "CPI"})
"""Benchmarks observed over the coupon period itself: even the current coupon is a projection."""

FLOATING_BOND_TYPES: Final = frozenset({"Облигация с плавающим купоном", "Флоатер"})
FIXED_UNKNOWN_BOND_TYPES: Final = frozenset(
    {"Облигация с фиксированным (неизвестным) купоном", "Фикс с неизвестным купоном"}
)
FIXED_KNOWN_BOND_TYPES: Final = frozenset(
    {"Облигация с фиксированным (известным) купоном", "Фикс с известным купоном"}
)
STRUCTURED_BOND_TYPE: Final = "Структурная облигация"
CONVERTIBLE_BOND_TYPE: Final = "Конвертируемая облигация"
DISCOUNT_BOND_TYPE: Final = "Дисконтная облигация"
PERPETUAL_SUBTYPE: Final = "Бессрочные"
DETAILS_FIXED_KNOWN: Final = "Фиксированный с известными купонами"
DETAILS_FIXED_UNKNOWN: Final = "Фиксированный с неизвестными купонами"
FACE_INDEXED: Final = "Индексированный"

OFZ_CLASSES: Final = frozenset({"ofz_pd", "ofz_pk", "ofz_in", "ofz_ad", "ofz_n"})
_OFZ_NAME = (
    (re.compile(r"ОФЗ-ПД", re.IGNORECASE), "ofz_pd"),
    (re.compile(r"ОФЗ-ПК", re.IGNORECASE), "ofz_pk"),
    (re.compile(r"ОФЗ-ИН", re.IGNORECASE), "ofz_in"),
    (re.compile(r"ОФЗ-АД", re.IGNORECASE), "ofz_ad"),
    (re.compile(r"ОФЗ-Н\b", re.IGNORECASE), "ofz_n"),
)
_OFZ_SECID = re.compile(r"^SU(\d{2})\d{3}RMFS\d$")
_OFZ_PREFIX = {
    "25": "ofz_pd",
    "26": "ofz_pd",
    "24": "ofz_pk",
    "29": "ofz_pk",
    "52": "ofz_in",
    "46": "ofz_ad",
    "48": "ofz_ad",
    "53": "ofz_n",
}


def map_benchmark(code: str | None, coupon_details: str | None = None) -> str | None:
    """MOEX ``COUPON_BENCHMARK`` (else ``COUPON_DETAILS``) -> hub benchmark code.

    Unknown MOEX codes are passed through upper-cased (never dropped)."""
    if code:
        return BENCHMARK_MAP.get(code.upper(), code.upper())
    if coupon_details:
        return COUPON_DETAILS_BENCHMARK.get(coupon_details)
    return None


@dataclass(frozen=True, slots=True)
class ClassifyInput:
    """Everything :func:`classify_bond` needs (labels verbatim from MOEX)."""

    secid: str
    name: str | None = None
    short_name: str | None = None
    sec_type: str | None = None
    issuer_inn: str | None = None
    issuer_name: str | None = None
    face_unit: str | None = None
    bond_type: str | None = None
    bond_subtype: str | None = None
    coupon_details: str | None = None
    facevalue_type: str | None = None
    coupon_benchmark: str | None = None
    struct_flag: bool | None = None
    has_description: bool = False
    is_matured: bool = False
    coupon_values: tuple[bool, ...] = ()
    """Per coupon of the schedule: does MOEX publish a value (amount or rate > 0)?"""
    coupon_rates: tuple[CouponRate, ...] = ()
    """The schedule's periods and published rates (for :func:`schedule_constant_fixed`)."""
    has_formula: bool = False
    """A rate formula (``ref.bond_rate_formula``, any source) exists for the bond."""
    key_rate_changes: tuple[date, ...] = ()
    """Effective dates of CBR key-rate changes (``md.fixing`` KEY_RATE)."""
    today: date | None = None


@dataclass(frozen=True, slots=True)
class CouponRate:
    """One coupon period of a schedule: start (``None`` = previous pay date), pay date, rate."""

    start_date: date | None
    pay_date: date
    value_prc: Decimal | None


def schedule_constant_fixed(
    rates: Sequence[CouponRate], today: date | None, key_rate_changes: Sequence[date]
) -> bool:
    """Evidence that an unlabelled active bond pays a fixed coupon (basis
    ``schedule_constant``):

    * at least two paid coupons (``pay_date <= today``) and every coupon - paid and future -
      carries the same positive rate (``value_prc``; amounts may vary with amortization),
    * a CBR key-rate change lies after the start of the first paid period and on/before the
      start of the last paid period: the last paid coupon of a key-rate floater (fixed at or
      before its period start, or averaged over it) would differ from the first one.

    Without key-rate history the rule never passes (the bond stays ``unknown``).
    """
    if today is None or len(rates) < 2:
        return False
    ordered = sorted(rates, key=lambda r: r.pay_date)
    values = {r.value_prc for r in ordered}
    if len(values) != 1:
        return False
    (value,) = values
    if value is None or value <= 0:
        return False
    paid = [(i, r) for i, r in enumerate(ordered) if r.pay_date <= today]
    if len(paid) < 2:
        return False

    def start_of(i: int, r: CouponRate) -> date | None:
        return r.start_date or (ordered[i - 1].pay_date if i > 0 else None)

    first_start = start_of(*paid[0])
    last_start = start_of(*paid[-1])
    if first_start is None or last_start is None or last_start <= first_start:
        return False
    return any(first_start < c <= last_start for c in key_rate_changes)


def _ofz_class(inp: ClassifyInput) -> str | None:
    for text in (inp.name, inp.short_name):
        if text:
            for pattern, cls in _OFZ_NAME:
                if pattern.search(text):
                    return cls
    m = _OFZ_SECID.match(inp.secid)
    if m:
        return _OFZ_PREFIX.get(m.group(1))
    return None


def classify_bond(inp: ClassifyInput) -> str:
    """Derive ``bond_class`` (see ``ref.bond`` column comment); deterministic, label based.

    Russian sovereign = issuer INN of the Ministry of Finance (MOEX ``ofz_bond`` also covers
    Kazakhstan, Belarus, Oman ... -> ``foreign_sovereign``)."""
    ofz_series = _ofz_class(inp)
    minfin = inp.issuer_inn == MINFIN_INN or (
        inp.issuer_inn is None and inp.sec_type == "ofz_bond" and ofz_series is not None
    )
    if minfin:
        if inp.face_unit and inp.face_unit != "RUB":
            return "sovereign_fx"
        return ofz_series or "sovereign_other"
    if inp.sec_type == "ofz_bond":  # MOEX "government bond" of another state
        return "foreign_sovereign"
    if inp.sec_type == "cb_bond":
        return "cbr_bond"
    if inp.sec_type == "ifi_bond":
        return "ifi"
    if inp.struct_flag or inp.bond_type == STRUCTURED_BOND_TYPE:
        return "structured"
    texts = " ".join(t for t in (inp.name, inp.issuer_name) if t).lower()
    if "ипотечн" in texts and "агент" in texts:
        return "mbs"
    if "специализированн" in texts and "финансов" in texts:
        return "securitization"
    if inp.sec_type == "subfederal_bond":
        return "subfederal"
    if inp.sec_type == "municipal_bond":
        return "municipal"
    if inp.sec_type is None:
        return "unknown"
    return "corporate"


def classify_coupon_type(inp: ClassifyInput, bond_class: str) -> tuple[str, str]:
    """Derive ``(coupon_type, coupon_type_basis)``.

    Order: floating evidence (MOEX floating label or COUPON_DETAILS benchmark, or any
    COUPON_BENCHMARK other than CPI on an indexed face) -> indexed face (linker) -> fixed with
    unknown coupons -> discount -> fixed with known coupons -> OFZ series -> published
    schedule (only when MOEX labels exist or the bond has matured; an unlabelled active bond
    stays ``unknown`` because MOEX forward-fills floating coupons).
    """
    details = inp.coupon_details
    bench = map_benchmark(inp.coupon_benchmark, details)
    face_indexed = inp.facevalue_type == FACE_INDEXED or "линкер" in (inp.bond_type or "").lower()
    if inp.bond_type in FLOATING_BOND_TYPES or details in COUPON_DETAILS_BENCHMARK:
        return "floating", "moex_label"
    if bench is not None and (bench != "CPI" or not face_indexed):
        return "floating", "benchmark"
    if face_indexed:
        return "indexed_face", "moex_label"
    if details == DETAILS_FIXED_UNKNOWN or inp.bond_type in FIXED_UNKNOWN_BOND_TYPES:
        return "variable_known", "moex_label"
    if inp.bond_type == DISCOUNT_BOND_TYPE:
        return "zero", "moex_label"
    if details == DETAILS_FIXED_KNOWN or inp.bond_type in FIXED_KNOWN_BOND_TYPES:
        return "fixed", "moex_label"
    if bond_class in OFZ_CLASSES:
        return {
            "ofz_pd": "fixed",
            "ofz_ad": "fixed",
            "ofz_n": "fixed",
            "ofz_pk": "floating",
            "ofz_in": "indexed_face",
        }[bond_class], "ofz_series"
    if inp.has_description or inp.is_matured:
        if not inp.coupon_values:
            return "unknown", "none"
        if all(inp.coupon_values):
            # A fully valued schedule proves nothing for an active bond: MOEX forward-fills
            # floating coupons. Only a matured bond (every coupon paid) is "fixed" by schedule,
            # or an active one whose constant rate survived a key-rate change.
            if inp.is_matured:
                return "fixed", "schedule"
            if (
                bond_class != "structured"
                and not inp.has_formula
                and schedule_constant_fixed(inp.coupon_rates, inp.today, inp.key_rate_changes)
            ):
                return "fixed", "schedule_constant"
            return "unknown", "schedule"
        if any(inp.coupon_values):
            return "variable_known", "schedule"
        return "unknown", "none"
    return "unknown", "none"


# ------------------------------------------------------------------ schedule helpers


def number_coupons(rows: Sequence[CouponRow]) -> list[tuple[int, CouponRow]]:
    """Assign ``coupon_no`` = 1-based position by (pay_date, start_date, record_date, value)."""
    ordered = sorted(
        rows,
        key=lambda r: (
            r.pay_date,
            r.start_date or date.min,
            r.record_date or date.min,
            r.value if r.value is not None else Decimal(-1),
        ),
    )
    return [(i, r) for i, r in enumerate(ordered, start=1)]


def coupon_has_value(row: CouponRow, coupon_type: str) -> bool:
    """Does MOEX publish a value for this coupon? A 0 counts only for zero-coupon bonds."""
    for v in (row.value, row.value_prc):
        if v is not None and (v != 0 or coupon_type == "zero"):
            return True
    return False


def coupon_status(
    row: CouponRow,
    *,
    coupon_type: str,
    benchmark: str | None,
    is_current: bool,
    face_indexed: bool,
    today: date,
    fixed_in_advance: bool = False,
) -> str:
    """``known`` / ``current_published`` / ``projected_by_moex`` / ``unknown`` (module
    docstring). ``fixed_in_advance``: a curated formula states that the floater's rate is
    fixed before the period starts (only then is its current coupon ``known``)."""
    if not coupon_has_value(row, coupon_type):
        return "unknown"
    if row.pay_date <= today:
        return "known"
    if coupon_type == "floating":
        if is_current and (benchmark is None or benchmark not in IN_ARREARS_BENCHMARKS):
            return "known" if fixed_in_advance else "current_published"
        return "projected_by_moex"
    if coupon_type in ("indexed_face", "unknown") or face_indexed:
        return "projected_by_moex"
    return "known"


def current_coupon_index(numbered: Sequence[tuple[int, CouponRow]], today: date) -> int | None:
    """``coupon_no`` of the period containing ``today`` (start <= today < pay)."""
    prev_pay: date | None = None
    for no, r in numbered:
        start = r.start_date or prev_pay
        if r.pay_date > today and (start is None or start <= today):
            return no
        prev_pay = r.pay_date
    return None


@dataclass(frozen=True, slots=True)
class OptionDates:
    """Next option dates from the market list (primary board) and the bond subtype."""

    put: date | None = None
    call: date | None = None
    buyback: date | None = None
    bond_subtype: str | None = None


_OFFER_TYPE_RE = re.compile(r"^(?P<base>[^()]*?)\s*(?:\((?P<suffix>[^)]*)\))?\s*$")
_OFFER_SUFFIX = {
    "состоялось": "completed",
    "отменено": "cancelled",
    "дефолт": "default",
    "технический дефолт": "technical_default",
}


def offer_status_and_kind(
    raw: str | None, offer_date: date | None, today: date
) -> tuple[str, bool]:
    """``(status, is_offer_or_redemption)`` from MOEX ``offertype``."""
    m = _OFFER_TYPE_RE.match(raw or "")
    base = (m.group("base") if m else raw or "").strip().lower()
    suffix = (m.group("suffix") if m else None) or ""
    status = _OFFER_SUFFIX.get(suffix.strip().lower())
    if status is None:
        if offer_date is None:
            status = "undated"
        elif offer_date >= today:
            status = "scheduled"
        else:
            status = "past"
    return status, "погашение" in base


def normalize_offers(
    rows: Sequence[OfferRow], options: OptionDates, today: date
) -> list[dict[str, Any]]:
    """Normalized offer dicts (``ref.bond_offer`` columns without run/meta columns).

    ``offer_key``: ISO date (``#n`` suffix for same-date duplicates ordered by window, price,
    raw type) or ``undated-n`` for rows without a date (ordered the same way).
    """

    def order(r: OfferRow) -> tuple[Any, ...]:
        return (
            r.offer_date or date.max,
            r.offer_date_start or date.min,
            r.offer_date_end or date.min,
            r.price if r.price is not None else Decimal(-1),
            r.offer_type_raw or "",
        )

    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for r in sorted(rows, key=order):
        base_key = r.offer_date.isoformat() if r.offer_date else "undated"
        n = counts.get(base_key, 0) + 1
        counts[base_key] = n
        key = (
            f"undated-{n}" if r.offer_date is None else (base_key if n == 1 else f"{base_key}#{n}")
        )
        status, is_redemption = offer_status_and_kind(r.offer_type_raw, r.offer_date, today)
        offer_type, basis = "unknown", None
        if r.offer_date is not None:
            if options.call is not None and r.offer_date == options.call:
                offer_type, basis = "call", "call_option_date"
            elif options.put is not None and r.offer_date == options.put:
                offer_type, basis = "put", "put_option_date"
            elif options.buyback is not None and r.offer_date == options.buyback:
                offer_type, basis = "buyback", "buyback_date"
            elif r.offer_date_start is not None or r.offer_date_end is not None:
                offer_type, basis = "put", "exercise_window"
            elif options.bond_subtype == "До оферты (call)":
                offer_type, basis = "call", "bond_subtype"
            elif options.bond_subtype == "До оферты (put)":
                offer_type, basis = "put", "bond_subtype"
        out.append(
            {
                "offer_key": key,
                "offer_date": r.offer_date,
                "date_missing": r.offer_date is None,
                "offer_type": offer_type,
                "offer_type_basis": basis,
                "offer_type_raw": r.offer_type_raw,
                "status": status,
                "is_offer_or_redemption": is_redemption,
                "offer_date_start": r.offer_date_start,
                "offer_date_end": r.offer_date_end,
                "price": r.price,
                "value": r.value,
                "face_value": r.face_value,
                "face_unit": r.face_unit,
                "agent": r.agent,
                "capture_id": r.capture_id,
            }
        )
    return out


def settle_lag(
    trade_date: date | None, settle_date: date | None, is_business_day: Callable[[date], bool]
) -> int | None:
    """Business days in ``(trade_date, settle_date]``; ``None`` if either is missing or the
    settle date precedes the trade date."""
    if trade_date is None or settle_date is None or settle_date < trade_date:
        return None
    lag = 0
    d = trade_date
    while d < settle_date:
        d += timedelta(days=1)
        if is_business_day(d):
            lag += 1
    return lag
