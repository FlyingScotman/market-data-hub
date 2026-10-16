"""Export hub data as Trading Desk Monitor (TDM) contract 1.3 CSV files.

TDM loads CSV files dropped into a folder (``python -m tdm load <dir>``); the dataset is
chosen by the file-name prefix (``<dataset>.csv`` / ``<dataset>_<anything>.csv``), the
separator by the header line. Files written here use ``;``, ``.`` as decimal point, UTF-8
with BOM (as the ``contract/csv`` templates), ISO dates, ``1``/``0`` booleans and the exact
header order of the templates (TDM@a6ba30b ``contract/csv/*.csv``, ``tdm/datasets.py``).

Produced (when the hub has data): ``instruments``, ``issuers``, ``bond_cashflows``,
``curves`` + ``md_curves`` (``RUB OFZ`` only), ``md_fx_cbr``, ``md_fixings``,
``md_bond_quotes``, ``md_fx`` (labelled MOEX 19:00 candidate), ``cbr_meetings``,
``calendar``. Everything else is listed in the manifest under ``absent`` with the reason.

Rules: no forward fill (a missing observation is a missing row); a row with a missing
*required* TDM field is left out (TDM rejects a whole file for one bad row) and counted in
``manifest.json`` under ``excluded``. ``instrument_id`` = ISIN when the ISIN is present and
unique across SECIDs, else the MOEX SECID (same key in all files). TDM ``issuer_id`` = INN,
else ``MOEX:<emitent_id>``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from mdhub import __version__
from mdhub.core.timeutil import utc_now
from mdhub.db.pool import DictConnection
from mdhub.sources.moex_market import ZCYC_PARAM_NAMES, kbd_yield_pct

__all__ = [
    "ABSENT",
    "HEADERS",
    "PRICE_BASES",
    "TDM_CONTRACT_VERSION",
    "TdmExport",
    "TdmExportOptions",
    "TdmFile",
    "build_tdm_export",
]

TDM_CONTRACT_VERSION: Final = "1.3"

HEADERS: Final[dict[str, tuple[str, ...]]] = {
    "issuers": (
        "issuer_id",
        "name",
        "issuer_group",
        "sector",
        "country",
        "is_gov",
        "is_quasi",
    ),
    "instruments": (
        "instrument_id",
        "isin",
        "name",
        "issuer_id",
        "asset_class",
        "segment",
        "ccy",
        "face_value",
        "issue_date",
        "maturity",
        "coupon_type",
        "coupon_rate",
        "coupon_freq",
        "day_count",
        "index_name",
        "margin",
        "amortizing",
        "offer_date",
        "is_mbs",
        "collateral_eligible",
        "call_date",
        "is_subord",
    ),
    "bond_cashflows": ("instrument_id", "date", "type", "rate", "amount"),
    "curves": ("curve_id", "name", "ccy", "kind", "index_name", "source", "ord"),
    "calendar": ("date", "is_business_day"),
    "cbr_meetings": ("date", "effective_date"),
    "md_curves": ("date", "curve_id", "tenor_years", "rate", "source"),
    "md_fx": ("date", "ccy", "rate", "source"),
    "md_fx_cbr": ("date", "ccy", "rate", "set_date"),
    "md_fixings": ("date", "index_name", "value"),
    "md_bond_quotes": (
        "date",
        "instrument_id",
        "clean_price",
        "ytm",
        "z_spread_bp",
        "g_spread_bp",
        "accrued",
        "volume_rub",
        "source",
    ),
}
"""Header of every produced dataset, in template order (TDM contract 1.3)."""

REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "issuers": ("issuer_id", "name", "issuer_group"),
    "instruments": (
        "instrument_id",
        "name",
        "issuer_id",
        "asset_class",
        "segment",
        "ccy",
        "face_value",
        "maturity",
        "coupon_type",
    ),
    "bond_cashflows": ("instrument_id", "date", "type"),
    "curves": ("curve_id", "name", "ccy", "kind"),
    "calendar": ("date", "is_business_day"),
    "cbr_meetings": ("date",),
    "md_curves": ("date", "curve_id", "tenor_years", "rate"),
    "md_fx": ("date", "ccy", "rate"),
    "md_fx_cbr": ("date", "ccy", "rate"),
    "md_fixings": ("date", "index_name", "value"),
    "md_bond_quotes": ("date", "instrument_id", "clean_price"),
}
"""Required (●) columns per TDM ``tdm/datasets.py`` 1.3."""

KEYS: Final[dict[str, tuple[str, ...]]] = {
    "issuers": ("issuer_id",),
    "instruments": ("instrument_id",),
    "bond_cashflows": ("instrument_id", "date", "type"),
    "curves": ("curve_id",),
    "calendar": ("date",),
    "cbr_meetings": ("date",),
    "md_curves": ("date", "curve_id", "tenor_years"),
    "md_fx": ("date", "ccy"),
    "md_fx_cbr": ("date", "ccy"),
    "md_fixings": ("date", "index_name"),
    "md_bond_quotes": ("date", "instrument_id"),
}
"""TDM upsert keys (a duplicate key makes TDM reject the file)."""

ABSENT: Final[dict[str, str]] = {
    "issuer_ratings": (
        "hub holds issue-level ratings only (Cbonds enrichment, currently HTTP 401); "
        "no issuer rating history"
    ),
    "md_cds": "no licensed CDS source",
    "md_curves:RUB RUONIA,RUB KEY": (
        "need bootstrapping from swap quotes (analytics, not the hub); raw quotes at "
        "/v1/curves/{dataset}"
    ),
    "md_curves:USD SOFR,EUR ESTR,CHF SARON,CNY": "no source in the hub",
    "md_fixings:SOFR,€STR,SARON": "no source in the hub",
    "md_bond_quotes:z_spread_bp,g_spread_bp": (
        "left empty: the hub does not compute spread analytics (MOEX-published Z/G spreads "
        "are in /v1/bonds/eod)"
    ),
    "instruments:collateral_eligible": "no Lombard / CCP repo list in the hub (left empty)",
    "books,positions,funding,funding_rates,cash_balances,trades,pnl_official,otc_deals,"
    "counterparties,desk_history": "private desk data: never on the hub",
}

PRICE_BASES: Final[dict[str, str]] = {
    "waprice": "WAP",
    "close": "close",
    "legal_close": "legal close",
    "admitted_quote": "admitted quote",
    "market_price3": "market price 3",
}
"""``md_bond_quotes.clean_price`` basis (column of ``md.bond_eod``) -> label in ``source``."""

TDM_PILLARS: Final[tuple[str, ...]] = (
    "0.0833",
    "0.25",
    "0.5",
    "1",
    "2",
    "3",
    "5",
    "7",
    "10",
    "15",
    "20",
)
"""TDM's 11 pillars 1M 3M 6M 1Y 2Y 3Y 5Y 7Y 10Y 15Y 20Y (``0.0833 = 1М``)."""

FIXING_NAMES: Final[dict[str, str]] = {"KEY_RATE": "КС", "RUONIA": "RUONIA"}
"""md.fixing (source cbr) index_code -> TDM index_name."""

RUSFAR_NAMES: Final[dict[str, str]] = {
    "RUSFAR": "RUSFAR O/N",
    "RUSFAR1W": "RUSFAR 1W",
    "RUSFAR2W": "RUSFAR 2W",
    "RUSFAR1M": "RUSFAR 1M",
    "RUSFAR3M": "RUSFAR 3M",
    "RUSFARCNY": "RUSFAR CNY",
}
"""md.index_eod secid (MOEX RUSFAR fixings, close in %) -> TDM index_name."""

BENCHMARK_NAMES: Final[dict[str, str]] = {"KEY_RATE": "КС", "RUONIA": "RUONIA"}

RUB_OFZ_CURVE: Final = ("RUB OFZ", "ОФЗ (КБД)", "RUB", "bond", None, "МосБиржа КБД", 1)

FX_CANDIDATE_SOURCE: Final = "MOEX индикативный курс 19:00 (mc), кандидат"


# ------------------------------------------------------------------------- options


@dataclass(frozen=True, slots=True)
class TdmExportOptions:
    """What to export.

    :param date_to: export date (the TDM run date); market series cover
        ``[date_from, date_to]``.
    :param date_from: range start (default: ``date_to``).
    :param price_basis: ``md_bond_quotes`` price (see :data:`PRICE_BASES`).
    :param calendar: hub calendar exported as TDM ``calendar``.
    :param include_fx_candidate: write ``md_fx`` from MOEX 19:00 indicative rates (labelled
        candidate; the desk's authoritative mark is internal).
    :param lookback_days: instruments maturing / cash flows paid this many days before
        ``date_from`` are still exported (TDM keeps a year of history).
    :param calendar_days: calendar span before and after ``date_to``.
    """

    date_to: date
    date_from: date | None = None
    price_basis: str = "waprice"
    calendar: str = "MOEX_STOCK"
    include_fx_candidate: bool = False
    lookback_days: int = 366
    calendar_days: int = 366

    def __post_init__(self) -> None:
        if self.price_basis not in PRICE_BASES:
            raise ValueError(f"price_basis must be one of {', '.join(PRICE_BASES)}")
        if self.date_from is not None and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")

    @property
    def start(self) -> date:
        return self.date_from or self.date_to

    @property
    def suffix(self) -> str:
        if self.start == self.date_to:
            return self.date_to.isoformat()
        return f"{self.start.isoformat()}_{self.date_to.isoformat()}"


# --------------------------------------------------------------------------- output


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "1"
    if value is False:
        return "0"
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else ""
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


@dataclass(slots=True)
class TdmFile:
    """One CSV file: ``name`` (``<dataset>_<suffix>.csv``) and rows in header order."""

    name: str
    dataset: str
    rows: list[tuple[Any, ...]]

    @property
    def header(self) -> tuple[str, ...]:
        return HEADERS[self.dataset]

    def to_bytes(self) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(self.header)
        for row in self.rows:
            writer.writerow([_cell(v) for v in row])
        return buf.getvalue().encode("utf-8-sig")


@dataclass(slots=True)
class TdmExport:
    """Files plus a manifest (counts, exclusions, absent datasets)."""

    options: TdmExportOptions
    files: list[TdmFile] = field(default_factory=list)
    excluded: Counter[str] = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=utc_now)

    def add(self, dataset: str, rows: list[tuple[Any, ...]], name: str | None = None) -> None:
        if not rows:
            return
        fname = name or f"{dataset}_{self.options.suffix}.csv"
        self.files.append(TdmFile(fname, dataset, rows))

    def rendered(self) -> dict[str, bytes]:
        out = {f.name: f.to_bytes() for f in self.files}
        out["manifest.json"] = json.dumps(
            self.manifest(out), ensure_ascii=False, indent=2, sort_keys=False
        ).encode("utf-8")
        return out

    def manifest(self, rendered: dict[str, bytes] | None = None) -> dict[str, Any]:
        rendered = rendered or {f.name: f.to_bytes() for f in self.files}
        o = self.options
        return {
            "format": "tdm-csv",
            "tdm_contract_version": TDM_CONTRACT_VERSION,
            "generator": f"market-data-hub {__version__}",
            "generated_at": self.generated_at.isoformat(),
            "date_from": o.start.isoformat(),
            "date_to": o.date_to.isoformat(),
            "options": {
                "price_basis": o.price_basis,
                "calendar": o.calendar,
                "include_fx_candidate": o.include_fx_candidate,
                "lookback_days": o.lookback_days,
                "calendar_days": o.calendar_days,
            },
            "conventions": {
                "separator": ";",
                "decimal_point": ".",
                "encoding": "utf-8-sig",
                "instrument_id": "ISIN if present and unique across SECIDs, else MOEX SECID",
                "issuer_id": "INN, else MOEX:<emitent_id>",
                "missing": "absent row (no forward fill)",
            },
            "files": [
                {
                    "name": f.name,
                    "dataset": f.dataset,
                    "rows": len(f.rows),
                    "sha256": hashlib.sha256(rendered[f.name]).hexdigest(),
                }
                for f in self.files
            ],
            "excluded": dict(sorted(self.excluded.items())),
            "absent": ABSENT,
            "notes": self.notes,
        }

    def write_dir(self, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, data in self.rendered().items():
            path = out_dir / name
            path.write_bytes(data)
            paths.append(path)
        return paths

    def to_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in self.rendered().items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
        return buf.getvalue()


# ------------------------------------------------------------------------- helpers


def _norm_ccy(code: str | None) -> str | None:
    if code is None:
        return None
    code = code.strip().upper()
    return "RUB" if code in {"SUR", "RUR"} else (code or None)


def _dedupe(
    export: TdmExport, dataset: str, rows: Iterable[tuple[Any, ...]]
) -> list[tuple[Any, ...]]:
    """Drop rows lacking a required field or repeating a key (first wins); count both."""
    header = HEADERS[dataset]
    req = [header.index(c) for c in REQUIRED[dataset]]
    key = [header.index(c) for c in KEYS[dataset]]
    seen: set[tuple[Any, ...]] = set()
    out = []
    for row in rows:
        if any(row[i] is None or row[i] == "" for i in req):
            export.excluded[f"{dataset}: missing required field"] += 1
            continue
        k = tuple(row[i] for i in key)
        if k in seen:
            export.excluded[f"{dataset}: duplicate key"] += 1
            continue
        seen.add(k)
        out.append(row)
    return out


def instrument_ids(conn: DictConnection) -> dict[str, str]:
    """SECID -> TDM ``instrument_id`` (ISIN when present and unique, else SECID)."""
    rows = conn.execute(
        """
        SELECT b.secid,
               CASE WHEN b.isin IS NOT NULL AND btrim(b.isin) <> ''
                         AND count(*) OVER (PARTITION BY b.isin) = 1
                    THEN b.isin ELSE b.secid END AS instrument_id
        FROM ref.bond b
        """
    ).fetchall()
    return {r["secid"]: r["instrument_id"] for r in rows}


def _issuer_key(row: dict[str, Any]) -> str | None:
    inn = (row.get("inn") or "").strip()
    if inn:
        return inn
    if row.get("issuer_id") is not None:
        return f"MOEX:{row['issuer_id']}"
    return None


# -------------------------------------------------------------------------- static

_GOV_CLASSES: Final[dict[str, str]] = {
    "ofz_pd": "ОФЗ-ПД",
    "ofz_pk": "ОФЗ-ПК",
    "ofz_in": "ОФЗ-ИН",
    "ofz_ad": "ОФЗ-АД",
    "ofz_n": "ОФЗ-н",
    "cbr_bond": "Облигации Банка России",
    "subfederal": "Субфед/муни",
    "municipal": "Субфед/муни",
}


def classify(bond_class: str, coupon_type: str, is_fx_face: bool | None) -> tuple[str, str]:
    """``(asset_class, segment)`` in TDM vocabulary for a hub ``bond_class``.

    TDM list: ОФЗ-ПД, ОФЗ-ПК, Корп RUB фикс, Корп RUB флоатер, Корп валютные, Суверен
    валютный, Секьюритизация; segments Гос / Корп / Секьюритизация. Classes TDM does not
    name (ОФЗ-ИН/АД/н, Банк России, субфед/муни, иностранный суверен) get descriptive labels.
    """
    if bond_class in {"mbs", "securitization"}:
        return "Секьюритизация", "Секьюритизация"
    if bond_class in _GOV_CLASSES:
        return _GOV_CLASSES[bond_class], "Гос"
    if bond_class == "sovereign_fx" or (bond_class == "sovereign_other" and is_fx_face):
        return "Суверен валютный", "Гос"
    if bond_class == "sovereign_other":
        return "Суверен RUB", "Гос"
    if bond_class == "foreign_sovereign":
        return "Иностранный суверен", "Корп"
    if is_fx_face:
        return "Корп валютные", "Корп"
    if coupon_type == "floating":
        return "Корп RUB флоатер", "Корп"
    return "Корп RUB фикс", "Корп"


_COUPON_TYPES: Final[dict[str, str]] = {
    "fixed": "fix",
    "zero": "fix",
    "variable_known": "fix",
    "indexed_face": "fix",
    "floating": "float",
}


def _static(export: TdmExport, conn: DictConnection, ids: dict[str, str]) -> None:
    o = export.options
    asof = o.date_to
    horizon = o.start - timedelta(days=o.lookback_days)
    bonds = conn.execute(
        """
        WITH nxt AS (
            SELECT DISTINCT ON (secid) secid, value_prc
            FROM ref.bond_coupon
            WHERE NOT is_deleted AND pay_date > %(asof)s
            ORDER BY secid, pay_date, coupon_no
        ), offer AS (
            SELECT DISTINCT ON (secid) secid, offer_date
            FROM ref.bond_offer
            WHERE NOT is_deleted AND offer_date >= %(asof)s AND status <> 'cancelled'
            ORDER BY secid, offer_date
        ), call AS (
            SELECT DISTINCT ON (secid) secid, offer_date
            FROM ref.bond_offer
            WHERE NOT is_deleted AND offer_date >= %(asof)s AND status <> 'cancelled'
              AND offer_type = 'call'
            ORDER BY secid, offer_date
        )
        SELECT b.secid, b.isin, COALESCE(b.short_name, b.name) AS name, b.issuer_id,
               b.bond_class, b.coupon_type, b.is_fx_face, b.face_unit, b.face_value,
               b.issue_date, b.maturity_date, b.coupon_frequency, b.day_count,
               b.is_amortizing, b.is_subordinated, b.coupon_benchmark,
               b.coupon_benchmark_spread_pct,
               f.benchmark, f.spread_bp, nxt.value_prc AS next_coupon_pct,
               offer.offer_date, call.offer_date AS call_date,
               i.inn, i.name AS issuer_name, i.short_name AS issuer_short_name,
               i.sector, i.country, i.is_sovereign
        FROM ref.bond b
        LEFT JOIN ref.bond_rate_formula_resolved f ON f.secid = b.secid
        LEFT JOIN nxt ON nxt.secid = b.secid
        LEFT JOIN offer ON offer.secid = b.secid
        LEFT JOIN call ON call.secid = b.secid
        LEFT JOIN ref.issuer i ON i.issuer_id = b.issuer_id
        WHERE b.maturity_date IS NULL OR b.maturity_date >= %(horizon)s
        ORDER BY b.secid
        """,
        {"asof": asof, "horizon": horizon},
    ).fetchall()

    issuers: dict[str, tuple[Any, ...]] = {}
    instruments: list[tuple[Any, ...]] = []
    exported: dict[str, str] = {}
    for b in bonds:
        if b["maturity_date"] is None:
            export.excluded["instruments: no maturity (perpetual)"] += 1
            continue
        ctype = _COUPON_TYPES.get(b["coupon_type"])
        if ctype is None:
            export.excluded["instruments: coupon_type unknown"] += 1
            continue
        issuer_id = _issuer_key({"inn": b["inn"], "issuer_id": b["issuer_id"]})
        if b["issuer_id"] is None or issuer_id is None:
            export.excluded["instruments: no issuer"] += 1
            continue
        if issuer_id not in issuers:
            iname = b["issuer_short_name"] or b["issuer_name"]
            country = (b["country"] or "").strip().upper()
            issuers[issuer_id] = (
                issuer_id,
                iname,
                b["issuer_name"] or iname,  # own group: desk groupings stay local
                b["sector"],
                country if len(country) == 2 else None,
                bool(b["is_sovereign"]),
                None,
            )
        asset_class, segment = classify(b["bond_class"], b["coupon_type"], b["is_fx_face"])
        floating = ctype == "float"
        benchmark = b["benchmark"] or (b["coupon_benchmark"] if floating else None)
        margin: Decimal | None = None
        if floating:
            if b["spread_bp"] is not None:
                margin = b["spread_bp"] / Decimal(100)
            elif b["coupon_benchmark_spread_pct"] is not None:
                margin = b["coupon_benchmark_spread_pct"]
        iid = str(ids.get(b["secid"]) or b["secid"])
        instruments.append(
            (
                iid,
                b["isin"],
                b["name"],
                issuer_id,
                asset_class,
                segment,
                _norm_ccy(b["face_unit"]),
                b["face_value"],
                b["issue_date"],
                b["maturity_date"],
                ctype,
                None if floating else b["next_coupon_pct"],
                b["coupon_frequency"],
                b["day_count"],
                BENCHMARK_NAMES.get(benchmark, benchmark) if floating and benchmark else None,
                margin,
                b["is_amortizing"],
                b["offer_date"],
                b["bond_class"] in {"mbs", "securitization"},
                None,
                b["call_date"],
                b["is_subordinated"],
            )
        )
        exported[b["secid"]] = iid
    instruments = _dedupe(export, "instruments", instruments)
    kept = {r[0] for r in instruments}
    exported = {s: i for s, i in exported.items() if i in kept}
    export.add("issuers", _dedupe(export, "issuers", issuers.values()))
    export.add("instruments", instruments)
    export.add("bond_cashflows", _cashflows(export, conn, exported, horizon))


def _cashflows(
    export: TdmExport, conn: DictConnection, exported: dict[str, str], horizon: date
) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        """
        SELECT secid, pay_date AS date, 'coupon' AS type, value_prc AS rate, amount, 0 AS prio
        FROM ref.bond_coupon WHERE NOT is_deleted AND pay_date >= %(h)s
        UNION ALL
        SELECT secid, amort_date, CASE WHEN data_source = 'maturity' THEN 'redemption'
                                       ELSE 'amort' END, NULL, value, 0
        FROM ref.bond_amortization WHERE NOT is_deleted AND amort_date >= %(h)s
        UNION ALL
        SELECT secid, offer_date, 'offer', NULL, value,
               CASE WHEN source = 'moex_iss' THEN 0 ELSE 1 END
        FROM ref.bond_offer
        WHERE NOT is_deleted AND offer_date IS NOT NULL AND offer_date >= %(h)s
          AND status <> 'cancelled'
        ORDER BY 1, 2, 3, 6
        """,
        {"h": horizon},
    ).fetchall()
    merged: dict[tuple[str, date, str], list[Any]] = {}
    for r in rows:
        iid = exported.get(r["secid"])
        if iid is None:
            continue
        key = (iid, r["date"], r["type"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = [iid, r["date"], r["type"], r["rate"], r["amount"]]
            continue
        if r["type"] == "offer":
            continue  # same-date offers (e.g. MOEX and Cbonds rows): first wins
        export.excluded["bond_cashflows: same-date flows merged"] += 1
        cur[3] = cur[3] if cur[3] == r["rate"] else None
        cur[4] = None if cur[4] is None or r["amount"] is None else cur[4] + r["amount"]
    return _dedupe(export, "bond_cashflows", (tuple(v) for v in merged.values()))


# -------------------------------------------------------------------------- market


def _curves(export: TdmExport, conn: DictConnection) -> None:
    o = export.options
    rows = conn.execute(
        "SELECT trade_date, params FROM md.zcyc_snapshot "
        "WHERE is_eod AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (o.start, o.date_to),
    ).fetchall()
    out: list[tuple[Any, ...]] = []
    for r in rows:
        params = r["params"] or {}
        try:
            p = {k: float(params[k]) for k in ZCYC_PARAM_NAMES}
            day = [
                (
                    r["trade_date"],
                    "RUB OFZ",
                    Decimal(tenor),
                    Decimal(f"{kbd_yield_pct(p, float(tenor)):.4f}"),
                    "MOEX КБД",
                )
                for tenor in TDM_PILLARS
            ]
        except (KeyError, TypeError, ValueError, OverflowError):
            export.excluded["md_curves: RUB OFZ day with invalid KBD params"] += 1
            continue
        out.extend(day)
    out = _dedupe(export, "md_curves", out)
    if out:
        export.add("curves", [RUB_OFZ_CURVE])
        export.add("md_curves", out)


def _fx_cbr(export: TdmExport, conn: DictConnection) -> None:
    o = export.options
    rows = conn.execute(
        """
        SELECT effective_date, ccy, rate_per_unit, set_date FROM md.fx_official
        WHERE effective_date >= %(f)s AND (effective_date <= %(t)s OR set_date <= %(t)s)
        ORDER BY effective_date, ccy
        """,
        {"f": o.start, "t": o.date_to},
    ).fetchall()
    export.add(
        "md_fx_cbr",
        _dedupe(
            export,
            "md_fx_cbr",
            ((r["effective_date"], r["ccy"], r["rate_per_unit"], r["set_date"]) for r in rows),
        ),
    )


def _fixings(export: TdmExport, conn: DictConnection) -> None:
    o = export.options
    p = {"f": o.start, "t": o.date_to}
    cbr = conn.execute(
        """
        SELECT index_code, fixing_date, value FROM md.fixing
        WHERE source = 'cbr' AND index_code = ANY(%(codes)s)
          AND (fixing_date BETWEEN %(f)s AND %(t)s OR published_on BETWEEN %(f)s AND %(t)s)
        ORDER BY fixing_date, index_code
        """,
        {**p, "codes": list(FIXING_NAMES)},
    ).fetchall()
    moex = conn.execute(
        """
        SELECT secid, trade_date, close FROM md.index_eod
        WHERE secid = ANY(%(secids)s) AND trade_date BETWEEN %(f)s AND %(t)s
          AND close IS NOT NULL
        ORDER BY trade_date, secid
        """,
        {**p, "secids": list(RUSFAR_NAMES)},
    ).fetchall()
    rows = [(r["fixing_date"], FIXING_NAMES[r["index_code"]], r["value"]) for r in cbr]
    rows += [(r["trade_date"], RUSFAR_NAMES[r["secid"]], r["close"]) for r in moex]
    rows.sort(key=lambda x: (x[0], x[1]))
    export.add("md_fixings", _dedupe(export, "md_fixings", rows))


def _accrued_pct(r: dict[str, Any]) -> Decimal | None:
    acc, face = r["accrued_int"], r["face_value"]
    if acc is None or face is None or face <= 0:
        return None
    if _norm_ccy(r["face_unit"]) != _norm_ccy(r["currency_id"]):
        return None  # accrued and face in different currencies: no % without an FX rule
    pct: Decimal = (acc / face * 100).quantize(Decimal("0.000001"))
    return pct


def _ytm(basis: str, clean: Decimal, r: dict[str, Any]) -> Decimal | None:
    """Yield consistent with the chosen price: MOEX effective yield to its yield date
    (nearest offer while ahead, else maturity) when the yields row priced the same number."""
    column: str | None = None
    if r["y_price"] is not None and r["y_price"] == clean:
        column = "effective_yield_pct"
    elif basis == "waprice" and r["y_waprice"] is not None and r["y_waprice"] == clean:
        column = "effective_yield_waprice_pct"
    elif basis == "close":
        column = "yield_close_pct"
    value: Decimal | None = r[column] if column else None
    return value


def _bond_quotes(export: TdmExport, conn: DictConnection, ids: dict[str, str]) -> None:
    o = export.options
    rows = conn.execute(
        """
        SELECT e.trade_date, e.secid, e.board, e.close, e.waprice, e.legal_close,
               e.admitted_quote, e.market_price3, e.yield_close_pct, e.accrued_int,
               e.face_value, e.face_unit, e.currency_id, e.value,
               y.price AS y_price, y.waprice AS y_waprice, y.effective_yield_pct,
               y.effective_yield_waprice_pct
        FROM md.bond_eod e
        JOIN ref.bond b
          ON b.secid = e.secid AND e.board = COALESCE(b.marketprice_board, b.primary_board)
        LEFT JOIN md.bond_eod_yield y
          ON y.trade_date = e.trade_date AND y.secid = e.secid AND y.board = e.board
        WHERE e.trade_date BETWEEN %s AND %s
        ORDER BY e.trade_date, e.secid
        """,
        (o.start, o.date_to),
    ).fetchall()
    label = PRICE_BASES[o.price_basis]
    out: list[tuple[Any, ...]] = []
    for r in rows:
        clean = r[o.price_basis]
        if clean is None or clean <= 0:
            continue  # no price on that day: no row
        volume = r["value"] if _norm_ccy(r["currency_id"]) == "RUB" else None
        out.append(
            (
                r["trade_date"],
                ids.get(r["secid"], r["secid"]),
                clean,
                _ytm(o.price_basis, clean, r),
                None,
                None,
                _accrued_pct(r),
                volume,
                f"MOEX {r['board']} {label}",
            )
        )
    export.add("md_bond_quotes", _dedupe(export, "md_bond_quotes", out))


def _fx_candidate(export: TdmExport, conn: DictConnection) -> None:
    o = export.options
    rows = conn.execute(
        """
        SELECT trade_date, base_ccy, rate FROM md.fx_indicative
        WHERE clearing = 'mc' AND quote_ccy = 'RUB' AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date, base_ccy
        """,
        (o.start, o.date_to),
    ).fetchall()
    out = _dedupe(
        export,
        "md_fx",
        ((r["trade_date"], r["base_ccy"], r["rate"], FX_CANDIDATE_SOURCE) for r in rows),
    )
    if out:
        export.add("md_fx", out, name=f"md_fx_moex_indicative_{o.suffix}.csv")
        export.notes.append(
            "md_fx_moex_indicative_*.csv is a CANDIDATE market mark (MOEX indicative 19:00); "
            "the desk FX mark is internal - do not load it over internal md_fx rows"
        )


def _refs(export: TdmExport, conn: DictConnection) -> None:
    o = export.options
    meetings = conn.execute(
        "SELECT meeting_date, effective_date FROM ref.cbr_meeting ORDER BY meeting_date"
    ).fetchall()
    export.add(
        "cbr_meetings",
        _dedupe(
            export, "cbr_meetings", ((m["meeting_date"], m["effective_date"]) for m in meetings)
        ),
    )
    days = conn.execute(
        "SELECT day, is_business_day FROM ref.calendar_effective "
        "WHERE calendar = %s AND day BETWEEN %s AND %s ORDER BY day",
        (
            o.calendar,
            o.date_to - timedelta(days=o.calendar_days),
            o.date_to + timedelta(days=o.calendar_days),
        ),
    ).fetchall()
    export.add(
        "calendar",
        _dedupe(export, "calendar", ((d["day"], d["is_business_day"]) for d in days)),
    )


def build_tdm_export(conn: DictConnection, options: TdmExportOptions) -> TdmExport:
    """Read the hub tables and build the TDM files (nothing is written)."""
    export = TdmExport(options)
    ids = instrument_ids(conn)
    _static(export, conn, ids)
    _curves(export, conn)
    _fx_cbr(export, conn)
    _fixings(export, conn)
    _bond_quotes(export, conn, ids)
    if options.include_fx_candidate:
        _fx_candidate(export, conn)
    _refs(export, conn)
    return export


def dataset_of(filename: str, datasets: Sequence[str] | None = None) -> str | None:
    """TDM's file-name rule (``tdm/loader.py dataset_for``): longest dataset prefix wins."""
    stem = Path(filename).stem.lower()
    names = sorted(datasets or [*HEADERS, *_TDM_OTHER], key=len, reverse=True)
    for name in names:
        if stem == name or stem.startswith(name + "_") or stem.startswith(name + "-"):
            return name
    return None


_TDM_OTHER: Final = (
    "books",
    "funding_rates",
    "issuer_ratings",
    "counterparties",
    "otc_deals",
    "positions",
    "funding",
    "cash_balances",
    "trades",
    "pnl_official",
    "md_cds",
    "desk_history",
)
