"""Import of the owner's Andromeda CNY swap history export into ``md.curve_snapshot``.

``mdhub legacy andromeda-cny FILE [FILE ...]`` runs :func:`import_andromeda_cny`. Inputs
(untrusted files, parsed with the standard library only):

* the workbook ``andromeda_cny_swaps.xlsx`` - sheets ``CNY_IRS_RUSFAR_OIS`` (-> dataset
  ``cny_rusfar_ois``) and ``XCCY`` (CNY vs RUONIA, -> ``cny_ruonia_xccy``): first column the date
  (Excel serial), then one column per tenor, values in percent, empty cell = missing;
* or the same tables as CSV (``Дата,1W,2W,...``; ``"0,80%"``; ``-`` = missing), the dataset taken
  from the file name (``*rusfar*`` / ``*xccy*``).

Rules (MAPPING.md "andromeda CNY export"):

* **Raw first.** Every input file is stored verbatim in ``ops.raw_capture`` (source
  ``andromeda``, endpoint ``file:andromeda_cny.xlsx`` / ``file:andromeda_cny.csv:<dataset>``,
  ``fetched_at`` = ``source_ts`` = the export time), so a re-import reuses the capture.
* **One snapshot per (dataset, date)**, ``quote_basis = mid_only``, bid/ask NULL, mid = the
  value; tenors upper case, positions 0-based in file column order over the tenors present;
  ``tenor_months``/``tenor_days`` from :func:`mdhub.sources.ratescalcs.tenor_parts`;
  ``content_sha256`` = :func:`mdhub.sources.otc_monitor.content_hash` (= the legacy mapping's
  hash). A missing cell writes no quote; a day with some tenors present is stored with those
  (``meta.quality_flags`` ``andromeda_partial_curve``, ``meta.missing_tenors``); a day without
  any value is not stored.
* **fetched_at.** Days before the export's Moscow date: 23:59:59 Moscow time on the date,
  flagged ``fetched_at_synthetic`` (as the legacy andromeda archive). The export-day row is an
  intraday state: ``fetched_at`` = the real export time (``meta.export = 'intraday'``) - from the
  workbook's ``docProps/core.xml`` ``dcterms:modified`` (written in UTC) or ``--exported-at``
  (required for CSV input, which carries no metadata; file system times of extracted files are
  not trusted). A later export that contains that day as a past day adds its EOD snapshot.
* **Idempotent, revisable.** Snapshots are upserted by ``(source, dataset, fetched_at)``, quotes
  by ``(snapshot_id, tenor)``; history is enabled on both tables (migration 0424), so a revised
  value writes the previous image to ``ops.row_history``. A tenor that a later export no longer
  has for a stored day is deleted, its old row written to ``ops.row_history`` by this module.
  Snapshots are never deduplicated against the latest stored one (that is the live-poll rule)
  and never deleted.
* **Fail closed.** Bad header/date/number, duplicate date, a value outside the dataset's sanity
  bounds (:data:`mdhub.sources.otc_monitor.CURVES`, CNY -50..300 %), a date after the export date,
  or two inputs disagreeing on a day -> nothing is written.
* **Quality flags** (``meta.quality_flags``; days are kept): ``jump_gt_500bp`` (a tenor moved
  more than :data:`JUMP_BP` bp against the previous date; ``meta.jumps_bp``), ``integer_grid_curve``
  (>= 6 values, all whole numbers: indicative/placeholder-like), ``flat_curve`` (>= 6 equal
  values), plus curated ``meta.quality_note`` texts (:data:`CURATED_NOTES`).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Final
from xml.etree import ElementTree as ET

import psycopg
from psycopg.rows import tuple_row

from mdhub.core.capture import job_run, record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.db.pool import Database
from mdhub.sources import otc_monitor as otc
from mdhub.sources import ratescalcs as rc
from mdhub.sources.cbr import safe_xml_fromstring

__all__ = [
    "CURATED_NOTES",
    "JUMP_BP",
    "AndromedaImportError",
    "ImportReport",
    "InputFile",
    "Sheet",
    "build_snapshots",
    "import_andromeda_cny",
    "load_file",
    "parse_csv",
    "parse_xlsx",
    "xlsx_modified",
]

log = logging.getLogger(__name__)

SOURCE: Final = "andromeda"
JOB: Final = "import_andromeda_cny"
PARSER_VERSION: Final = "andromeda_cny_v1"
SHEETS: Final = {"CNY_IRS_RUSFAR_OIS": "cny_rusfar_ois", "XCCY": "cny_ruonia_xccy"}
LABELS: Final = {ds: name for name, ds in SHEETS.items()}
CSV_NAME_HINTS: Final = (("rusfar", "cny_rusfar_ois"), ("xccy", "cny_ruonia_xccy"))
EOD_TIME: Final = time(23, 59, 59)
JUMP_BP: Final = Decimal(500)
"""Day-over-day move per tenor that is flagged. The 2024 CNY squeezes moved the front end by
300-500 bp on many days (about 270 days per sheet exceed 300 bp); 500 bp keeps ~60 days per
sheet, 1000 bp ~15."""
MIN_GRID_TENORS: Final = 6
BOUNDS: Final = {s.dataset: (s.min_pct, s.max_pct) for s in otc.CURVES if s.dataset in LABELS}
CURATED_NOTES: Final[Mapping[tuple[str, date], str]] = {
    ("cny_rusfar_ois", date(2026, 3, 18)): (
        "stylized placeholder-like curve 17/16/14/14/13/12/11/10/10/10/10/10 (whole-number "
        "steps; 2026-03-13 shows 5-7, 2026-03-25 a flat 10); same pattern on the XCCY sheet - "
        "not a market curve, do not use as a mark"
    ),
    ("cny_ruonia_xccy", date(2026, 3, 18)): (
        "stylized placeholder-like curve 18/16/16/14/14/12/10/10/10/10/10/10 (whole-number "
        "steps; 2026-03-13 shows 6.75-7.75, then 14 flat 2026-03-20, 5 flat 2026-03-23, 10 flat "
        "2026-03-25); same pattern on the RUSFAR sheet - not a market curve"
    ),
    ("cny_rusfar_ois", date(2024, 9, 18)): (
        "series maximum 1W 43.15 % (2W 39.14): peak of the September 2024 CNY liquidity "
        "squeeze; 1W 20.58 the day before, 42.69 on 2024-09-19, 18.86 on 2024-09-20; whole "
        "curve +4-5 pp the same day and the XCCY sheet shows the same front end - judged real"
    ),
    ("cny_ruonia_xccy", date(2024, 9, 18)): (
        "series maximum 1W 43.15 % (2W 39.14, 1M 37.49): peak of the September 2024 CNY "
        "liquidity squeeze; 1W 21.73 the day before, 42.69 on 2024-09-19, 18.86 on 2024-09-20; "
        "same front end on the RUSFAR sheet - judged real"
    ),
}

_TENOR_RE: Final = re.compile(r"^(?:\d{1,2}[DWMY]|ON|TN|SN)$")
_MISSING: Final = {"", "-", "—", "–"}
_XLSX_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_DCTERMS: Final = "{http://purl.org/dc/terms/}"
_CELL_REF: Final = re.compile(r"^([A-Z]+)(\d+)$")
_EXCEL_EPOCH: Final = date(1899, 12, 30)


class AndromedaImportError(ValueError):
    """The input cannot be imported (format, bounds, conflicting inputs, missing export time)."""


# ------------------------------------------------------------------------------- parsing


@dataclass(frozen=True, slots=True)
class Sheet:
    dataset: str
    label: str
    tenors: tuple[str, ...]
    rows: tuple[tuple[date, tuple[Decimal | None, ...]], ...]
    """(date, value per tenor) in file order."""


@dataclass(frozen=True, slots=True)
class InputFile:
    name: str
    kind: str  # "xlsx" | "csv"
    payload: bytes
    sheets: tuple[Sheet, ...]
    exported_at: datetime | None
    """Export time from the file's own metadata (xlsx ``dcterms:modified``), else ``None``."""

    @property
    def endpoint(self) -> str:
        if self.kind == "xlsx":
            return "file:andromeda_cny.xlsx"
        return f"file:andromeda_cny.csv:{self.sheets[0].dataset}"


def _canonical(v: Decimal) -> Decimal:
    """Excel precision (15 significant digits) and no trailing zeros: the workbook's float
    noise ``8.029999999999999`` and the CSV's ``8,03%`` both become ``8.03``; ``0,80%`` -> 0.8."""
    if v.is_zero():
        return Decimal(0)
    v = round(v, 14 - v.adjusted())
    return v.quantize(Decimal(1)) if v == v.to_integral_value() else v.normalize()


def _value(text: str | None, where: str) -> Decimal | None:
    s = (text or "").strip().replace("\u00a0", "").replace(" ", "")
    if s in _MISSING:
        return None
    s = s.removesuffix("%").replace(",", ".")
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise AndromedaImportError(f"{where}: not a number {text!r}") from None
    if not v.is_finite():
        raise AndromedaImportError(f"{where}: not finite {text!r}")
    return _canonical(v)


def _tenors(header: Sequence[str | None], where: str) -> tuple[str, ...]:
    if not header or (header[0] or "").strip().casefold() not in ("дата", "date"):
        raise AndromedaImportError(f"{where}: first column must be 'Дата'")
    tenors = tuple((h or "").strip().upper() for h in header[1:])
    while tenors and not tenors[-1]:
        tenors = tenors[:-1]
    bad = [t for t in tenors if not _TENOR_RE.fullmatch(t)]
    if not tenors or bad:
        raise AndromedaImportError(f"{where}: bad tenor header {bad or 'empty'}")
    if len(set(tenors)) != len(tenors):
        raise AndromedaImportError(f"{where}: duplicate tenor in header")
    return tenors


def _sheet(
    dataset: str,
    tenors: tuple[str, ...],
    raw: Iterable[tuple[date, Sequence[str | None]]],
    where: str,
) -> Sheet:
    lo, hi = BOUNDS[dataset]
    rows: list[tuple[date, tuple[Decimal | None, ...]]] = []
    seen: set[date] = set()
    for day, cells in raw:
        if day in seen:
            raise AndromedaImportError(f"{where}: duplicate date {day}")
        seen.add(day)
        if len(cells) > len(tenors) and any((c or "").strip() for c in cells[len(tenors) :]):
            raise AndromedaImportError(f"{where} {day}: more values than tenors")
        padded = [*cells, *([None] * len(tenors))][: len(tenors)]
        values = tuple(_value(c, f"{where} {day} {t}") for c, t in zip(padded, tenors, strict=True))
        for v, t in zip(values, tenors, strict=True):
            if v is not None and not lo <= v <= hi:
                raise AndromedaImportError(f"{where} {day} {t}: {v} outside [{lo}, {hi}] %")
        rows.append((day, values))
    if not rows:
        raise AndromedaImportError(f"{where}: no data rows")
    return Sheet(dataset=dataset, label=LABELS[dataset], tenors=tenors, rows=tuple(rows))


def _iso_date(text: str, where: str) -> date:
    s = text.strip()
    try:
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
            return datetime.strptime(s, "%d.%m.%Y").date()
        return date.fromisoformat(s[:10])
    except ValueError:
        raise AndromedaImportError(f"{where}: bad date {text!r}") from None


def csv_dataset(name: str) -> str:
    """Dataset of a CSV export from its file name (``*rusfar*`` / ``*xccy*``)."""
    low = name.casefold()
    hits = {ds for hint, ds in CSV_NAME_HINTS if hint in low}
    if len(hits) != 1:
        raise AndromedaImportError(f"{name}: cannot tell the dataset from the file name")
    return hits.pop()


def parse_csv(payload: bytes, dataset: str, *, where: str = "csv") -> Sheet:
    """``Дата,1W,...`` CSV (UTF-8, optional BOM, ``"0,80%"`` values, ``-`` = missing)."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AndromedaImportError(f"{where}: not UTF-8") from None
    lines = [r for r in csv.reader(io.StringIO(text, newline="")) if any(c.strip() for c in r)]
    if not lines:
        raise AndromedaImportError(f"{where}: empty file")
    tenors = _tenors(lines[0], where)
    raw = [(_iso_date(r[0], where), r[1:]) for r in lines[1:]]
    return _sheet(dataset, tenors, raw, where)


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_rows(root: ET.Element, shared: Sequence[str]) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    for row in root.iter(f"{_XLSX_NS}row"):
        cells: dict[int, str | None] = {}
        for c in row.iter(f"{_XLSX_NS}c"):
            m = _CELL_REF.match(c.get("r", ""))
            if not m:
                raise AndromedaImportError(f"xlsx: bad cell reference {c.get('r')!r}")
            kind = c.get("t")
            text: str | None
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{_XLSX_NS}t"))
            else:
                v = c.find(f"{_XLSX_NS}v")
                text = v.text if v is not None else None
                if kind == "s" and text is not None:
                    try:
                        text = shared[int(text)]
                    except (ValueError, IndexError):
                        raise AndromedaImportError("xlsx: bad shared string index") from None
            cells[_col(m.group(1))] = text
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(i) for i in range(width)])
    return rows


def _xlsx_date(text: str | None, where: str) -> date:
    s = (text or "").strip()
    if re.fullmatch(r"\d+(?:\.0+)?", s):  # Excel serial (date style), whole days only
        return _EXCEL_EPOCH + timedelta(days=int(Decimal(s)))
    if re.fullmatch(r"\d+\.\d+", s):
        raise AndromedaImportError(f"{where}: date with a time part {s!r}")
    return _iso_date(s, where)


def _read_workbook(payload: bytes) -> tuple[dict[str, list[list[str | None]]], datetime | None]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = set(zf.namelist())
            wb = safe_xml_fromstring(zf.read("xl/workbook.xml"))
            rels = safe_xml_fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            targets = {
                r.get("Id"): r.get("Target", "") for r in rels.iter(f"{_REL_NS}Relationship")
            }
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sroot = safe_xml_fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter(f"{_XLSX_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
            sheets: dict[str, list[list[str | None]]] = {}
            for sh in wb.iter(f"{_XLSX_NS}sheet"):
                target = targets.get(sh.get(f"{_DOC_REL_NS}id"), "")
                path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                path = str(PurePosixPath(path))
                sheets[sh.get("name", "")] = _xlsx_rows(safe_xml_fromstring(zf.read(path)), shared)
            modified = None
            if "docProps/core.xml" in names:
                core = safe_xml_fromstring(zf.read("docProps/core.xml"))
                el = core.find(f"{_DCTERMS}modified")
                if el is not None and el.text:
                    modified = datetime.fromisoformat(el.text.strip().replace("Z", "+00:00"))
                    if modified.tzinfo is None:
                        modified = None  # a naive time is not an export clock
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, FormatDriftError) as exc:
        raise AndromedaImportError(f"xlsx: unreadable workbook: {exc}") from None
    return sheets, modified


def xlsx_modified(payload: bytes) -> datetime | None:
    """``docProps/core.xml`` ``dcterms:modified`` (aware; openpyxl/Excel write UTC ``Z``)."""
    return _read_workbook(payload)[1]


def parse_xlsx(payload: bytes, *, where: str = "xlsx") -> list[Sheet]:
    """Sheets ``CNY_IRS_RUSFAR_OIS`` and ``XCCY`` (others are ignored; at least one needed)."""
    sheets, _ = _read_workbook(payload)
    out: list[Sheet] = []
    for name, rows in sheets.items():
        dataset = SHEETS.get(name.strip())
        if dataset is None:
            log.info("andromeda xlsx: sheet %r ignored", name)
            continue
        rows = [r for r in rows if any((c or "").strip() for c in r)]
        if not rows:
            raise AndromedaImportError(f"{where}/{name}: empty sheet")
        here = f"{where}/{name}"
        tenors = _tenors(rows[0], here)
        raw = [(_xlsx_date(r[0], here), r[1:]) for r in rows[1:]]
        out.append(_sheet(dataset, tenors, raw, here))
    if not out:
        raise AndromedaImportError(f"{where}: none of the sheets {sorted(SHEETS)}")
    return out


def load_file(path: Path) -> InputFile:
    """Read and parse one input file (``.xlsx`` or ``.csv``)."""
    payload = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return InputFile(
            name=path.name,
            kind="xlsx",
            payload=payload,
            sheets=tuple(parse_xlsx(payload, where=path.name)),
            exported_at=xlsx_modified(payload),
        )
    if suffix == ".csv":
        sheet = parse_csv(payload, csv_dataset(path.name), where=path.name)
        return InputFile(path.name, "csv", payload, (sheet,), None)
    raise AndromedaImportError(f"{path.name}: expected .xlsx or .csv")


# ------------------------------------------------------------------------------ building


@dataclass(frozen=True, slots=True)
class DayCurve:
    dataset: str
    day: date
    fetched_at: datetime
    intraday: bool
    quotes: tuple[tuple[str, Decimal], ...]
    """(tenor, mid) in file column order, present tenors only."""
    meta: Mapping[str, Any]
    content_sha256: str
    source_file: str


@dataclass
class DatasetStats:
    rows: int = 0
    first: date | None = None
    last: date | None = None
    empty_days: list[str] = field(default_factory=list)
    partial_days: int = 0
    stored_days: int = 0
    quotes: int = 0
    negative_values: int = 0
    flags: Counter[str] = field(default_factory=Counter)
    jumps_gt_1000bp: list[str] = field(default_factory=list)


def _bp(a: Decimal, b: Decimal) -> int:
    return int(((b - a) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def build_snapshots(
    files: Sequence[InputFile],
    exported_at: datetime,
    *,
    previous: Callable[[str, date], tuple[date | None, Mapping[str, Decimal]]] | None = None,
) -> tuple[list[DayCurve], dict[str, DatasetStats]]:
    """Combine the parsed inputs into one curve per (dataset, date) with quality flags.

    ``previous(dataset, day)`` returns the date and mids of the last stored day before ``day``
    (jump check of the first imported day, so a later, shorter export flags it the same way).
    The jump check compares each tenor with the previous date that has values."""
    export_day = exported_at.astimezone(MSK).date()
    merged: dict[str, dict[date, tuple[tuple[tuple[str, Decimal | None], ...], str]]] = {}
    order: dict[str, tuple[str, ...]] = {}
    for f in files:
        for sh in f.sheets:
            by_day = merged.setdefault(sh.dataset, {})
            for day, values in sh.rows:
                if day > export_day:
                    raise AndromedaImportError(
                        f"{f.name}/{sh.label}: date {day} after the export date {export_day}"
                    )
                pairs = tuple(zip(sh.tenors, values, strict=True))
                present = {t: v for t, v in pairs if v is not None}
                if day in by_day:
                    other = {t: v for t, v in by_day[day][0] if v is not None}
                    if other != present:
                        raise AndromedaImportError(
                            f"{sh.dataset} {day}: {f.name} disagrees with {by_day[day][1]}"
                        )
                    continue
                by_day[day] = (pairs, f.name)
            order.setdefault(sh.dataset, sh.tenors)
    curves: list[DayCurve] = []
    stats: dict[str, DatasetStats] = {}
    for dataset in sorted(merged):
        st = stats[dataset] = DatasetStats()
        days = sorted(merged[dataset])
        st.rows, st.first, st.last = len(days), days[0], days[-1]
        prev_day, prev = previous(dataset, days[0]) if previous else (None, {})
        for day in days:
            pairs, fname = merged[dataset][day]
            quotes = tuple((t, v) for t, v in pairs if v is not None)
            if not quotes:
                st.empty_days.append(day.isoformat())
                continue
            mids = [v for _t, v in quotes]
            st.negative_values += sum(1 for v in mids if v < 0)
            flags: list[str] = []
            meta: dict[str, Any] = {}
            intraday = day == export_day
            if intraday:
                fetched_at = exported_at
                meta["export"] = "intraday"
            else:
                fetched_at = datetime.combine(day, EOD_TIME, MSK)
                flags.append("fetched_at_synthetic")
            missing = [t for t, v in pairs if v is None]
            if missing:
                flags.append("andromeda_partial_curve")
                meta["missing_tenors"] = missing
                st.partial_days += 1
            jumps = {t: _bp(prev[t], v) for t, v in quotes if t in prev}
            big = {t: bp for t, bp in jumps.items() if abs(bp) > JUMP_BP}
            if big:
                flags.append("jump_gt_500bp")
                meta["jumps_bp"] = big
                if prev_day is not None:
                    meta["jump_prev_date"] = prev_day.isoformat()
                if any(abs(bp) > 1000 for bp in big.values()):
                    st.jumps_gt_1000bp.append(
                        f"{day} "
                        + ", ".join(f"{t} {bp:+d}" for t, bp in big.items() if abs(bp) > 1000)
                    )
            if len(mids) >= MIN_GRID_TENORS and all(v == v.to_integral_value() for v in mids):
                flags.append("integer_grid_curve")
            if len(mids) >= MIN_GRID_TENORS and len(set(mids)) == 1:
                flags.append("flat_curve")
            note = CURATED_NOTES.get((dataset, day))
            if note:
                meta["quality_note"] = note
            meta["quality_flags"] = flags
            st.flags.update(flags)
            parsed = rc.ParsedCurve(
                dataset=dataset,
                label=LABELS[dataset],
                quote_basis="mid_only",
                quotes=tuple(
                    rc.CurveQuote(t, i, None, None, *rc.tenor_parts(t), mid=v)
                    for i, (t, v) in enumerate(quotes)
                ),
            )
            curves.append(
                DayCurve(
                    dataset=dataset,
                    day=day,
                    fetched_at=fetched_at,
                    intraday=intraday,
                    quotes=quotes,
                    meta=meta,
                    content_sha256=otc.content_hash(parsed),
                    source_file=fname,
                )
            )
            st.stored_days += 1
            st.quotes += len(quotes)
            prev = dict(quotes)
            prev_day = day
    return curves, stats


# ------------------------------------------------------------------------------- writing


@dataclass
class ImportReport:
    run_id: int | None
    exported_at: datetime
    exported_at_basis: str
    files: list[dict[str, Any]]
    datasets: dict[str, DatasetStats]
    snapshots: UpsertResult = field(default_factory=UpsertResult)
    quotes: UpsertResult = field(default_factory=UpsertResult)
    quotes_deleted: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "exported_at": self.exported_at.isoformat(),
            "exported_at_msk": self.exported_at.astimezone(MSK).isoformat(),
            "exported_at_basis": self.exported_at_basis,
            "files": self.files,
            "snapshots": _counts(self.snapshots),
            "quotes": {**_counts(self.quotes), "deleted": self.quotes_deleted},
            "datasets": {
                ds: {
                    "rows": st.rows,
                    "first": None if st.first is None else st.first.isoformat(),
                    "last": None if st.last is None else st.last.isoformat(),
                    "stored_days": st.stored_days,
                    "quotes": st.quotes,
                    "empty_days_skipped": st.empty_days,
                    "partial_days": st.partial_days,
                    "negative_values": st.negative_values,
                    "flags": dict(sorted(st.flags.items())),
                    "jumps_gt_1000bp": st.jumps_gt_1000bp,
                }
                for ds, st in self.datasets.items()
            },
        }


def _counts(res: UpsertResult) -> dict[str, int]:
    return {"inserted": res.inserted, "updated": res.updated, "unchanged": res.unchanged}


def _previous_mids(
    conn: psycopg.Connection[Any],
) -> Callable[[str, date], tuple[date | None, dict[str, Decimal]]]:
    def lookup(dataset: str, day: date) -> tuple[date | None, dict[str, Decimal]]:
        with conn.cursor(row_factory=tuple_row) as cur:
            rows = cur.execute(
                """SELECT s.snapshot_date, q.tenor, q.mid_pct
                   FROM md.curve_quote q JOIN md.curve_snapshot s ON s.id = q.snapshot_id
                   WHERE s.id = (
                       SELECT id FROM md.curve_snapshot
                       WHERE source = %s AND dataset = %s AND snapshot_date < %s
                       ORDER BY snapshot_date DESC, fetched_at DESC LIMIT 1)""",
                (SOURCE, dataset, day),
            ).fetchall()
        if not rows:
            return None, {}
        return rows[0][0], {str(t): Decimal(m) for _d, t, m in rows if m is not None}

    return lookup


def _resolve_export_time(
    files: Sequence[InputFile], exported_at: datetime | None
) -> tuple[datetime, str]:
    if exported_at is not None:
        if exported_at.tzinfo is None:
            raise AndromedaImportError("--exported-at needs a UTC offset")
        return exported_at, "--exported-at"
    stamps = {f.exported_at for f in files if f.exported_at is not None}
    if not stamps:
        raise AndromedaImportError(
            "no export time: CSV files carry none (file system times of extracted files are "
            "not reliable) - pass --exported-at or import the .xlsx"
        )
    if len(stamps) > 1:
        raise AndromedaImportError("workbooks with different export times: import separately")
    return stamps.pop(), "xlsx docProps/core.xml dcterms:modified"


def _write(
    conn: psycopg.Connection[Any],
    run_id: int,
    files: Sequence[InputFile],
    curves: Sequence[DayCurve],
    exported_at: datetime,
    report: ImportReport,
) -> None:
    caps: dict[str, int] = {}
    for f in files:
        caps[f.name] = record_capture(
            conn,
            run_id,
            SOURCE,
            f.endpoint,
            f"file:{f.name}",
            f.payload,
            PARSER_VERSION,
            source_ts=exported_at,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if f.kind == "xlsx"
                else "text/csv; charset=utf-8"
            ),
            fetched_at=exported_at,
        )
    snaps = [
        {
            "source": SOURCE,
            "dataset": c.dataset,
            "quote_basis": "mid_only",
            "fetched_at": c.fetched_at,
            "snapshot_date": c.fetched_at.astimezone(MSK).date(),
            "source_ts": None,
            "market_closed": None,
            "content_sha256": c.content_sha256,
            "parser_version": PARSER_VERSION,
            "label": LABELS[c.dataset],
            "meta": dict(c.meta),
            "capture_id": caps[c.source_file],
            "run_id": run_id,
        }
        for c in curves
    ]
    report.snapshots = upsert_rows(
        conn, "md.curve_snapshot", snaps, ["source", "dataset", "fetched_at"], run_id=run_id
    )
    ids: dict[tuple[str, datetime], int] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        for sid, ds, at in cur.execute(
            """SELECT id, dataset, fetched_at FROM md.curve_snapshot
               WHERE source = %s AND dataset = ANY(%s) AND fetched_at = ANY(%s)""",
            (SOURCE, sorted({c.dataset for c in curves}), sorted({c.fetched_at for c in curves})),
        ):
            ids[(str(ds), at)] = int(sid)
        existing: dict[int, set[str]] = defaultdict(set)
        for sid, tenor in cur.execute(
            "SELECT snapshot_id, tenor FROM md.curve_quote WHERE snapshot_id = ANY(%s)",
            (sorted(ids.values()),),
        ):
            existing[int(sid)].add(str(tenor))
    quotes: list[dict[str, Any]] = []
    stale: list[tuple[int, str]] = []
    for c in curves:
        sid = ids[(c.dataset, c.fetched_at)]
        present = {t for t, _v in c.quotes}
        stale.extend((sid, t) for t in sorted(existing.get(sid, set()) - present))
        for pos, (tenor, mid) in enumerate(c.quotes):
            months, days = rc.tenor_parts(tenor)
            quotes.append(
                {
                    "snapshot_id": sid,
                    "tenor": tenor,
                    "position": pos,
                    "tenor_months": months,
                    "tenor_days": days,
                    "pillar_date": None,
                    "bid_pct": None,
                    "ask_pct": None,
                    "mid_pct": mid,
                    "extra": None,
                }
            )
    if stale:
        # The history trigger covers UPDATE only: keep the image of removed quotes ourselves.
        for sid, tenor in stale:
            conn.execute(
                """INSERT INTO ops.row_history (table_name, row_key, old_row, replaced_by_run)
                   SELECT 'md.curve_quote', jsonb_build_object('snapshot_id', snapshot_id,
                          'tenor', tenor), to_jsonb(q), %s
                   FROM md.curve_quote q WHERE snapshot_id = %s AND tenor = %s""",
                (run_id, sid, tenor),
            )
            conn.execute(
                "DELETE FROM md.curve_quote WHERE snapshot_id = %s AND tenor = %s", (sid, tenor)
            )
    report.quotes_deleted = len(stale)
    report.quotes = upsert_rows(
        conn, "md.curve_quote", quotes, ["snapshot_id", "tenor"], run_id=run_id
    )


def import_andromeda_cny(
    db: Database,
    paths: Sequence[Path],
    *,
    exported_at: datetime | None = None,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ImportReport:
    """Import Andromeda CNY swap exports (see module docstring). Everything is written in one
    transaction; ``dry_run`` parses and checks against the DB but writes nothing.

    :raises AndromedaImportError: unreadable/invalid input (nothing written).
    """
    say = progress or (lambda msg: log.info("%s", msg))
    if not paths:
        raise AndromedaImportError("no input files")
    files = [load_file(p) for p in paths]
    when, basis = _resolve_export_time(files, exported_at)
    file_info = [
        {
            "file": f.name,
            "kind": f.kind,
            "endpoint": f.endpoint,
            "sheets": [s.label for s in f.sheets],
            "exported_at": None if f.exported_at is None else f.exported_at.isoformat(),
        }
        for f in files
    ]
    if dry_run:
        with db.connection() as conn:
            curves, stats = build_snapshots(files, when, previous=_previous_mids(conn))
            conn.rollback()
        return ImportReport(None, when, basis, file_info, stats, dry_run=True)
    params = {"files": [f.name for f in files], "exported_at": when.isoformat()}
    with job_run(db, JOB, params, trigger="cli") as run:
        with db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (JOB,))
            curves, stats = build_snapshots(files, when, previous=_previous_mids(conn))
            report = ImportReport(run.id, when, basis, file_info, stats)
            _write(conn, run.id, files, curves, when, report)
        run.add_counts(report.snapshots)
        run.add_counts(report.quotes)
    for ds, st in stats.items():
        say(
            f"{ds}: {st.rows} rows {st.first}..{st.last}, stored {st.stored_days} days / "
            f"{st.quotes} quotes, partial {st.partial_days}, empty skipped {len(st.empty_days)}"
        )
    return report
