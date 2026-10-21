"""НКР (Национальные Кредитные Рейтинги, www.ratings.ru): public rating lists (WEB-17).

The agency's rating pages offer two Excel exports ("Выгрузить в Excel"), each the complete
list of its credit ratings including withdrawn ones:

* ``/issues.php`` - issue ratings: ``ID, Issuer Name, Rating Date, Rating, ISIN, Security
  name, Maturity, Issuer TIN, Issue volume, Currency, Issue date, Press release``;
* ``/issuers.php`` - issuer ratings: ``ID, Issuer Name, Date, Rating, Outlook, SCA, Sector,
  TIN, OGRN, ESG-Rating, Press release``.

Two GET requests per run (robots.txt allows both paths). Values are verbatim; a withdrawn
rating (``рейтинг отозван``) is a row with ``rating = None`` and ``action = withdrawn``; an
outlook of the form ``рейтинг на пересмотре …`` is also the row's ``watch``. The lists carry
the latest action only (no verb), so ``action`` is ``None`` for other rows.
"""

from __future__ import annotations

from datetime import date
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.cbr import read_xlsx_rows
from mdhub.sources.ratings_common import (
    NATIONAL_SCALE,
    RatingRecord,
    blank,
    is_watch,
    normalize_grade,
    parse_ru_date,
    require_header,
)

__all__ = [
    "BASE_URL",
    "ISSUERS_PATH",
    "ISSUES_PATH",
    "PARSER_VERSION",
    "RATING_TYPE_ISSUE",
    "RATING_TYPE_ISSUER",
    "fetch_export",
    "parse_issuers",
    "parse_issues",
]

PARSER_VERSION: Final = "1"
AGENCY: Final = "nkr"
BASE_URL: Final = "https://www.ratings.ru"
ISSUES_PATH: Final = "/issues.php"
ISSUERS_PATH: Final = "/issuers.php"
RATING_TYPE_ISSUE: Final = "Рейтинги выпусков"
RATING_TYPE_ISSUER: Final = "Рейтинги эмитентов"
"""List names as the agency titles them (``ref.agency_rating.rating_type``)."""

ISSUES_COLUMNS: Final = ("ID", "Issuer Name", "Rating Date", "Rating", "ISIN", "Security name",
                         "Issuer TIN", "Press release")  # fmt: skip
ISSUERS_COLUMNS: Final = ("ID", "Issuer Name", "Date", "Rating", "Outlook", "Sector", "TIN",
                          "OGRN", "Press release")  # fmt: skip
_WITHDRAWN: Final = "рейтинг отозван"
_MIN_ROWS: Final = 20
"""Both lists have hundreds of rows; fewer means a broken export, not a fact."""


def fetch_export(http: HttpClient, path: str) -> httpx.Response:
    """GET one export (``ISSUES_PATH`` / ``ISSUERS_PATH``); must be an XLSX workbook."""
    resp = http.get(BASE_URL + path)
    if not resp.content.startswith(b"PK"):
        ctype = resp.headers.get("content-type", "")
        raise FormatDriftError(f"NKR {path}: not an XLSX workbook (content-type {ctype!r})")
    return resp


def _release_url(value: object) -> str | None:
    text = blank(value)
    if text is None:
        return None
    return text if text.startswith("http") else "https://" + text.lstrip("/")


def _rows(payload: bytes, required: tuple[str, ...], what: str) -> list[dict[str, str | None]]:
    rows = read_xlsx_rows(payload)
    if not rows:
        raise FormatDriftError(f"{what}: empty workbook")
    idx = require_header(rows[0], required, what)
    out = [
        {name: (r[i] if i < len(r) else None) for name, i in idx.items()}
        for r in rows[1:]
        if any(blank(c) for c in r)
    ]
    if len(out) < _MIN_ROWS:
        raise FormatDriftError(f"{what}: only {len(out)} rows")
    return out


def _rating(value: object) -> tuple[str | None, bool]:
    text = blank(value)
    if text is not None and text.lower() == _WITHDRAWN:
        return None, True
    return text, False


def _order(rec: RatingRecord) -> tuple[date, int]:
    """Latest row wins for a repeated key: rating date, then the agency's (numeric) ID."""
    aid = rec.agency_object_id or ""
    return (rec.rating_date or date.min, int(aid) if aid.isdigit() else -1)


def parse_issues(payload: bytes) -> list[RatingRecord]:
    """Issue ratings of the ``/issues.php`` export (one record per ISIN; a repeated ISIN keeps
    its latest row)."""
    out: dict[str, RatingRecord] = {}
    for r in _rows(payload, ISSUES_COLUMNS, "NKR issues"):
        aid = blank(r["ID"])
        isin = blank(r["ISIN"])
        if aid is None:
            raise FormatDriftError(f"NKR issues: row without ID: {r}")
        rating, withdrawn = _rating(r["Rating"])
        rec = RatingRecord(
            agency=AGENCY,
            object_kind="issue",
            object_key=isin or f"{AGENCY}:{aid}",
            rating_type=RATING_TYPE_ISSUE,
            scale=NATIONAL_SCALE,
            agency_object_id=aid,
            object_name=blank(r["Security name"]),
            isin=isin,
            inn=blank(r["Issuer TIN"]),
            ogrn=None,
            rating=rating,
            grade=normalize_grade(rating),
            outlook=None,
            watch=None,
            action="withdrawn" if withdrawn else None,
            action_raw=blank(r["Rating"]) if withdrawn else None,
            rating_date=parse_ru_date(r["Rating Date"], field="Rating Date"),
            press_release=_release_url(r["Press release"]),
            industry=None,
            is_withdrawn=withdrawn,
        )
        prev = out.get(rec.object_key)
        if prev is None or _order(rec) > _order(prev):
            out[rec.object_key] = rec
    return sorted(out.values(), key=lambda x: x.object_key)


def parse_issuers(payload: bytes) -> list[RatingRecord]:
    """Issuer ratings of the ``/issuers.php`` export (one record per INN; a repeated INN keeps
    its latest row)."""
    out: dict[str, RatingRecord] = {}
    for r in _rows(payload, ISSUERS_COLUMNS, "NKR issuers"):
        aid = blank(r["ID"])
        if aid is None:
            raise FormatDriftError(f"NKR issuers: row without ID: {r}")
        inn = blank(r["TIN"])
        rating, withdrawn = _rating(r["Rating"])
        outlook = blank(r["Outlook"])
        rec = RatingRecord(
            agency=AGENCY,
            object_kind="issuer",
            object_key=inn or f"{AGENCY}:{aid}",
            rating_type=RATING_TYPE_ISSUER,
            scale=NATIONAL_SCALE,
            agency_object_id=aid,
            object_name=blank(r["Issuer Name"]),
            isin=None,
            inn=inn,
            ogrn=blank(r["OGRN"]),
            rating=rating,
            grade=normalize_grade(rating),
            outlook=outlook,
            watch=outlook if is_watch(outlook) else None,
            action="withdrawn" if withdrawn else None,
            action_raw=blank(r["Rating"]) if withdrawn else None,
            rating_date=parse_ru_date(r["Date"], field="Date"),
            press_release=_release_url(r["Press release"]),
            industry=blank(r["Sector"]),
            is_withdrawn=withdrawn,
        )
        prev = out.get(rec.object_key)
        if prev is None or _order(rec) > _order(prev):
            out[rec.object_key] = rec
    return sorted(out.values(), key=lambda x: x.object_key)
