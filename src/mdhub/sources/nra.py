"""НРА (Национальное Рейтинговое Агентство, www.ra-national.ru): rating actions (WEB-18).

The ratings page (``/ratings/``) links an Excel export of every rating action published since
2020 ("Выгрузить в Excel", a WP All Export public link carrying a ``security_key`` that the
agency publishes on the page). The hub reads the link from the page on every run (never a
stored copy) and stores the page and the workbook as raw captures; the key is removed from
stored URLs by :func:`mdhub.core.redact.redact_url`.

Export columns: ``id, Название организации, ИНН, Название пресс-релиза, Дата опубликования
пресс-релиза, Рейтинг, Статус рейтинга, Прогноз, Вид рейтинга, Сектор организации, Отрасль,
ОСК, ISIN, Ссылка на пресс релиз, Под наблюдением``. One row per press release and rated
object (issue ratings carry the ISIN). Only credit ratings (``Вид рейтинга`` starting with
``Кредитный рейтинг``) are parsed; ESG and non-credit ratings are skipped and counted.

* :func:`parse_actions` - every credit action (``ref.rating_action``);
* :func:`latest_states` - per rated object the latest action (``ref.agency_rating``):
  ``Отозван`` -> withdrawn, ``rating = None``; ``Под наблюдением`` -> ``watch``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.cbr import read_xlsx_rows
from mdhub.sources.ratings_common import (
    NATIONAL_SCALE,
    ActionRecord,
    RatingRecord,
    blank,
    is_watch,
    normalize_action,
    normalize_grade,
    parse_ru_date,
    require_header,
)

__all__ = [
    "BASE_URL",
    "PARSER_VERSION",
    "RATINGS_PATH",
    "NraExport",
    "fetch_export",
    "fetch_ratings_page",
    "latest_states",
    "parse_actions",
    "parse_export_url",
]

PARSER_VERSION: Final = "1"
AGENCY: Final = "nra"
BASE_URL: Final = "https://www.ra-national.ru"
RATINGS_PATH: Final = "/ratings/"
ISSUE_TYPE: Final = "Кредитный рейтинг отдельных выпусков облигаций"
CREDIT_PREFIX: Final = "Кредитный рейтинг"
COLUMNS: Final = (
    "id", "Название организации", "ИНН", "Название пресс-релиза",
    "Дата опубликования пресс-релиза", "Рейтинг", "Статус рейтинга", "Прогноз", "Вид рейтинга",
    "Сектор организации", "Отрасль", "ISIN", "Ссылка на пресс релиз",
)  # fmt: skip
_EXPORT_RE: Final = re.compile(
    r'href="(https://www\.ra-national\.ru/wp-load\.php\?[^"]*export_id=\d+[^"]*action=get_data[^"]*)"'
)
_ISIN_RE: Final = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
_MIN_ROWS: Final = 100


@dataclass(frozen=True, slots=True)
class NraExport:
    actions: list[ActionRecord]
    states: list[RatingRecord]
    rows: int
    skipped_non_credit: int


def fetch_ratings_page(http: HttpClient) -> httpx.Response:
    return http.get(BASE_URL + RATINGS_PATH)


def parse_export_url(page_html: str) -> str:
    """The export link of the ratings page (exactly one distinct link expected)."""
    links = {html.unescape(m.group(1)) for m in _EXPORT_RE.finditer(page_html)}
    if len(links) != 1:
        raise FormatDriftError(f"NRA ratings page: {len(links)} export links (expected 1)")
    return links.pop()


def fetch_export(http: HttpClient, url: str) -> httpx.Response:
    resp = http.get(url)
    if not resp.content.startswith(b"PK"):
        ctype = resp.headers.get("content-type", "")
        raise FormatDriftError(f"NRA export: not an XLSX workbook (content-type {ctype!r})")
    return resp


def _isins(value: object) -> list[str]:
    text = blank(value)
    return sorted(set(_ISIN_RE.findall(text.upper()))) if text else []


def _rows(payload: bytes) -> list[dict[str, str | None]]:
    rows = read_xlsx_rows(payload)
    if not rows:
        raise FormatDriftError("NRA export: empty workbook")
    idx = require_header(rows[0], COLUMNS, "NRA export")
    watch_i = idx.get("Под наблюдением")
    out = []
    for r in rows[1:]:
        if not any(blank(c) for c in r):
            continue
        row = {name: (r[i] if i < len(r) else None) for name, i in idx.items()}
        row["Под наблюдением"] = r[watch_i] if watch_i is not None and watch_i < len(r) else None
        out.append(row)
    if len(out) < _MIN_ROWS:
        raise FormatDriftError(f"NRA export: only {len(out)} rows")
    return out


def _industry(row: dict[str, str | None]) -> str | None:
    """``Сектор организации / Отрасль`` as published (either part may be missing)."""
    parts = [blank(row.get("Сектор организации")), blank(row.get("Отрасль"))]
    return " / ".join(p for p in parts if p) or None


def _objects(row: dict[str, str | None]) -> Iterable[tuple[str, str, str | None]]:
    """``(object_kind, object_key, isin)`` per rated object of an export row."""
    rid = blank(row["id"])
    inn = blank(row["ИНН"])
    if blank(row["Вид рейтинга"]) == ISSUE_TYPE:
        isins = _isins(row["ISIN"])
        if not isins:
            yield "issue", f"{AGENCY}:{rid}", None
        for isin in isins:
            yield "issue", isin, isin
    else:
        yield "issuer", inn or f"{AGENCY}:{rid}", None


def _parse(payload: bytes) -> tuple[list[tuple[ActionRecord, RatingRecord]], int, int]:
    pairs: list[tuple[ActionRecord, RatingRecord]] = []
    rows = _rows(payload)
    skipped = 0
    for row in rows:
        rtype = blank(row["Вид рейтинга"])
        if rtype is None or not rtype.startswith(CREDIT_PREFIX):
            skipped += 1
            continue
        rid = blank(row["id"])
        day = parse_ru_date(row["Дата опубликования пресс-релиза"], field="Дата")
        if rid is None or day is None:
            raise FormatDriftError(f"NRA export: row without id/date: {row}")
        status = blank(row["Статус рейтинга"])
        withdrawn = normalize_action(status) == "withdrawn"
        rating = None if withdrawn else blank(row["Рейтинг"])
        outlook = blank(row["Прогноз"])
        watch_raw = blank(row["Под наблюдением"])
        watch = watch_raw if is_watch(watch_raw) else None
        link = blank(row["Ссылка на пресс релиз"])
        for kind, key, isin in _objects(row):
            action = ActionRecord(
                agency=AGENCY,
                object_kind=kind,
                object_key=key,
                rating_type=rtype,
                scale=NATIONAL_SCALE,
                action_date=day,
                action_ref=link or f"{AGENCY}:{rid}",
                rating=rating,
                grade=normalize_grade(rating),
                outlook=outlook,
                watch=watch_raw,
                action=normalize_action(status),
                action_raw=status,
                title=blank(row["Название пресс-релиза"]),
            )
            state = RatingRecord(
                agency=AGENCY,
                object_kind=kind,
                object_key=key,
                rating_type=rtype,
                scale=NATIONAL_SCALE,
                agency_object_id=rid,
                object_name=blank(row["Название организации"]),
                isin=isin,
                inn=blank(row["ИНН"]),
                ogrn=None,
                rating=rating,
                grade=normalize_grade(rating),
                outlook=outlook,
                watch=watch,
                action=normalize_action(status),
                action_raw=status,
                rating_date=day,
                press_release=link,
                industry=_industry(row),
                is_withdrawn=withdrawn,
            )
            pairs.append((action, state))
    return pairs, len(rows), skipped


def parse_actions(payload: bytes) -> NraExport:
    """Credit rating actions of the export and the latest state per rated object."""
    pairs, nrows, skipped = _parse(payload)
    actions: dict[tuple[object, ...], ActionRecord] = {}
    for a, _s in pairs:
        prev = actions.get(a.key)
        if prev is not None and prev != a:
            raise FormatDriftError(f"NRA export: conflicting rows for {a.key}")
        actions[a.key] = a
    return NraExport(
        actions=sorted(actions.values(), key=lambda a: a.key),
        states=latest_states([s for _a, s in pairs]),
        rows=nrows,
        skipped_non_credit=skipped,
    )


def latest_states(states: list[RatingRecord]) -> list[RatingRecord]:
    """Latest state per rated object: newest ``rating_date``, then the highest export id."""
    best: dict[tuple[str, str, str, str, str], RatingRecord] = {}

    def order(s: RatingRecord) -> tuple[date, int]:
        aid = s.agency_object_id or ""
        return (s.rating_date or date.min, int(aid) if aid.isdigit() else -1)

    for s in states:
        prev = best.get(s.key)
        if prev is None or order(s) > order(prev):
            best[s.key] = s
    return sorted(best.values(), key=lambda s: s.key)
