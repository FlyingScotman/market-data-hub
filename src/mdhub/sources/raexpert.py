"""Эксперт РА (raexpert.ru): credit ratings from press releases and rating pages (WEB-16).

The agency has no free list export (its Excel export and SOAP/REST service are paid
subscriptions, and its rating lists paginate through a session hash). Its public pages are
enough to rebuild every live rating, because each rating is reviewed at least once a year
and every review is a press release:

1. **Sitemap** (``/sitemap.xml`` -> ``/sitemap0.xml``): every press release URL
   ``/releases/YYYY/<mon><DD>[suffix]`` (the date is in the URL).
2. **Release** pages link the rated objects: ``/database/companies/<id>/`` (INN, OGRN,
   industry), ``/database/regions/<slug>/`` and ``/database/securities/bonds/<id>/`` (ISIN).
3. **Object** pages carry, per rating type (credit ratings only), the full history table
   ``Национальная шкала | Прогноз | Рейтинг под наблюдением | Дата`` (newest first, each
   date linking its release). Company / region pages also link the pages of their rated
   bonds.

robots.txt forbids URLs with a query string and ``/search/``; the hub fetches only the paths
above. Values are verbatim; ``отозван`` -> withdrawn (``rating = None``). The history rows
carry no action verb, so ``action`` is ``withdrawn`` or ``None`` (never derived from
consecutive grades).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.htmldom import Node, parse_html
from mdhub.core.http import HttpClient
from mdhub.sources.ratings_common import (
    NATIONAL_SCALE,
    ActionRecord,
    RatingRecord,
    blank,
    is_watch,
    normalize_grade,
    parse_ru_date,
)

__all__ = [
    "BASE_URL",
    "LIST_PATHS",
    "PARSER_VERSION",
    "SITEMAP_PATH",
    "Entity",
    "HistoryRow",
    "RatingBlock",
    "Release",
    "canonical_url",
    "entity_records",
    "fetch_page",
    "page_kind",
    "parse_entity",
    "parse_list",
    "parse_release",
    "parse_sitemap_index",
    "parse_sitemap_releases",
    "release_date",
]

PARSER_VERSION: Final = "1"
AGENCY: Final = "raexpert"
HOST: Final = "raexpert.ru"
BASE_URL: Final = f"https://{HOST}"
SITEMAP_PATH: Final = "/sitemap.xml"
LIST_PATHS: Final = tuple(
    f"/ratings/{p}/"
    for p in (
        "bankcredit", "credits_fin", "credits", "credits_holding", "credits_project", "factor",
        "leasing_rel", "regioncredit", "municipalities", "mfi_credits",
        "securitization_credit_all", "debt_inst",
    )
)  # fmt: skip
"""Credit-rating lists on the Russian national scale (the agency's rating-export form names
them); each page shows its 20 most recently updated objects. The sitemap lags by days, so
these pages discover the latest actions."""
_SM_NS: Final = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_MONTHS: Final = {
    m: i + 1
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}
_RELEASE_RE: Final = re.compile(r"^/releases/(\d{4})/([a-z]{3})(\d{1,2})[a-z0-9_]*/?$", re.I)
_KINDS: Final = (
    ("bond", re.compile(r"^/database/securities/bonds/([^/]+)/?$")),
    ("company", re.compile(r"^/database/companies/([^/]+)/?$")),
    ("region", re.compile(r"^/database/regions/([^/]+)/?$")),
)
_ISIN_RE: Final = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


@dataclass(frozen=True, slots=True)
class Release:
    url: str
    title: str
    day: date | None
    category: str | None
    linked: list[tuple[str, str]]
    """``(kind, canonical url)`` of the rated objects the release links."""


@dataclass(frozen=True, slots=True)
class HistoryRow:
    rating: str | None  # verbatim; None when withdrawn
    raw: str  # rating cell as published (e.g. 'отозван')
    outlook: str | None
    watch: str | None
    day: date
    release: str | None


@dataclass(frozen=True, slots=True)
class RatingBlock:
    rating_type: str
    rows: list[HistoryRow]


@dataclass(frozen=True, slots=True)
class Entity:
    kind: str  # company | region | bond
    url: str
    name: str | None
    inn: str | None
    ogrn: str | None
    isin: str | None
    region_url: str | None
    industry: str | None
    blocks: list[RatingBlock]
    linked: list[tuple[str, str]] = field(default_factory=list)
    """Rated bonds the page links (company / region pages)."""


def canonical_url(href: str, base: str = BASE_URL) -> str | None:
    """``https://raexpert.ru/<path>/`` of an agency link (no query, no fragment); ``None`` for
    other hosts."""
    parts = urlsplit(urljoin(base + "/", href.strip()))
    if (parts.hostname or "").lower() not in (HOST, f"www.{HOST}"):
        return None
    path = parts.path.rstrip("/")
    return f"{BASE_URL}{path}/" if path else f"{BASE_URL}/"


def page_kind(url: str) -> str | None:
    """``release`` / ``company`` / ``region`` / ``bond`` of a canonical URL, else ``None``."""
    path = urlsplit(url).path
    if _RELEASE_RE.match(path):
        return "release"
    for kind, pattern in _KINDS:
        if pattern.match(path):
            return kind
    return None


def release_date(url: str) -> date | None:
    """Publication date encoded in a release URL (``/releases/2026/oct20d`` -> 2026-10-20)."""
    m = _RELEASE_RE.match(urlsplit(url).path)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(m.group(1)), month, int(m.group(3)))
    except ValueError:
        return None


def fetch_page(http: HttpClient, url: str) -> httpx.Response:
    return http.get(url)


def _xml(payload: bytes, what: str) -> ET.Element:
    try:
        return ET.fromstring(payload)  # noqa: S314 - agency sitemap, no entities expanded
    except ET.ParseError as exc:
        raise FormatDriftError(f"RA Expert {what}: bad XML: {exc}") from None


def parse_sitemap_index(payload: bytes) -> list[str]:
    """Sitemap URLs of the index (``https``, agency host only)."""
    root = _xml(payload, "sitemap index")
    out = []
    for loc in root.iter(f"{_SM_NS}loc"):
        url = canonical_url(loc.text or "")
        if url is not None:
            out.append(url.rstrip("/"))
    if not out:
        raise FormatDriftError("RA Expert sitemap index: no sitemaps")
    return out


def parse_sitemap_releases(payload: bytes) -> list[tuple[str, date]]:
    """``(canonical url, date)`` of every press release in a sitemap, newest first."""
    root = _xml(payload, "sitemap")
    out: dict[str, date] = {}
    for loc in root.iter(f"{_SM_NS}loc"):
        url = canonical_url(loc.text or "")
        if url is None or page_kind(url) != "release":
            continue
        day = release_date(url)
        if day is not None:
            out[url] = day
    if not out:
        raise FormatDriftError("RA Expert sitemap: no press releases")
    return sorted(out.items(), key=lambda t: (t[1], t[0]), reverse=True)


def _linked_objects(
    nodes: list[Node], base: str, kinds: tuple[str, ...] = ("company", "region", "bond")
) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for n in nodes:
        for href in n.links():
            url = canonical_url(href, base)
            kind = page_kind(url) if url else None
            if url and kind in kinds:
                seen[url] = kind
    return sorted((k, u) for u, k in seen.items())


def parse_list(page: str, url: str) -> list[tuple[str, str]]:
    """``(kind, url)`` of the objects and releases in the rating table of a list page (the
    20 most recently updated objects of the list, each with its latest release)."""
    tables = [t for t in parse_html(page).find_all("table") if t.find("thead") is not None]
    found = _linked_objects(tables, url, ("company", "region", "bond", "release"))
    if not any(k != "release" for k, _ in found):
        raise FormatDriftError(f"RA Expert list {url}: no rated objects")
    return found


def parse_release(page: str, url: str) -> Release:
    """Title, date, category and the rated objects a press release links."""
    root = parse_html(page)
    h1 = root.find("h1")
    if h1 is None:
        raise FormatDriftError(f"RA Expert release {url}: no title")
    sub = root.find("span", "b-subheader__date")
    sub_text = sub.text() if sub else ""
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})", sub_text)
    day = parse_ru_date(m.group(1)) if m else release_date(url)
    category = sub_text.split(",")[0].strip() or None if sub_text else None
    content = root.find_all("div", "b-content")
    return Release(
        url=url,
        title=h1.text(),
        day=day,
        category=category,
        linked=_linked_objects(content or [root], url),
    )


def _requisites(root: Node, base: str) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for item in root.find_all("div", "b-table__item"):
        title = item.find("div", "b-table__title")
        data = item.find("div", "b-table__data")
        if title is None or data is None:
            continue
        links = [canonical_url(h, base) for h in data.links()]
        out[title.text()] = (data.text(), next((u for u in links if u), None))
    return out


def _cell_rating(td: Node) -> str:
    spans = td.find_all("span")
    if len(spans) >= 2 and spans[1].text():
        return spans[1].text()
    text = td.text()
    if not text:
        raise FormatDriftError("RA Expert: empty rating cell")
    return text.split()[0].rstrip(",")


def _history(table: Node, base: str, what: str) -> list[HistoryRow]:
    header = [th.text() for th in table.find_all("th")]
    if not header or header[0] != "Национальная шкала" or header[-1] != "Дата":
        raise FormatDriftError(f"{what}: unexpected rating table header {header}")
    rows: list[HistoryRow] = []
    for tr in table.find_all("tr"):
        tds = [c for c in tr.children if isinstance(c, Node) and c.tag == "td"]
        if not tds:
            continue
        if len(tds) != len(header):
            raise FormatDriftError(f"{what}: row with {len(tds)} cells, header {header}")
        cells = dict(zip(header, tds, strict=True))
        raw = _cell_rating(cells["Национальная шкала"])
        withdrawn = raw.lower().startswith("отозван")
        day = parse_ru_date(cells["Дата"].text(), field="Дата")
        if day is None:
            raise FormatDriftError(f"{what}: rating row without date")
        release = next(
            (u for u in (canonical_url(h, base) for h in cells["Дата"].links()) if u), None
        )
        outlook = blank(cells["Прогноз"].text()) if "Прогноз" in cells else None
        watch_cell = cells.get("Рейтинг под наблюдением")
        watch = blank(watch_cell.text()) if watch_cell is not None else None
        rows.append(
            HistoryRow(
                rating=None if withdrawn else raw,
                raw=raw,
                outlook=outlook,
                watch=watch if is_watch(watch) else None,
                day=day,
                release=release,
            )
        )
    return rows


def _is_credit(rating_type: str) -> bool:
    t = rating_type.lower()
    return "кредит" in t and "esg" not in t and "некредит" not in t


def parse_entity(page: str, url: str) -> Entity:
    """Requisites and credit-rating history of a company / region / bond page."""
    kind = page_kind(url)
    if kind not in ("company", "region", "bond"):
        raise ValueError(f"not an RA Expert object page: {url}")
    root = parse_html(page)
    title = root.find("title")
    name = title.text().split(":", 1)[1].strip() if title and ":" in title.text() else None
    req = _requisites(root, url)
    isin = req.get("ISIN", (None, None))[0]
    if kind == "bond" and (isin is None or not _ISIN_RE.match(isin)):
        raise FormatDriftError(f"RA Expert bond page {url}: no ISIN")
    blocks: list[RatingBlock] = []
    linked: list[tuple[str, str]] = []
    for box in root.find_all("div", "b-actions__rates"):
        sub = box.find("span", "b-actions__subtitle")
        table = box.find("table")
        if sub is None:
            continue
        sub_links = [u for u in (canonical_url(h, url) for h in sub.links()) if u]
        bond_links = [u for u in sub_links if page_kind(u) == "bond"]
        linked.extend(("bond", u) for u in bond_links)
        rating_type = sub.text()
        if bond_links or table is None or not _is_credit(rating_type):
            continue  # a bond's own page carries its history; non-credit ratings skipped
        blocks.append(RatingBlock(rating_type, _history(table, url, f"RA Expert {url}")))
    if kind == "bond":
        name = req.get("Эмитент", (None, None))[0] or req.get("Регион", (None, None))[0] or name
    return Entity(
        kind=kind,
        url=url,
        name=name,
        inn=req.get("ИНН", (None, None))[0],
        ogrn=req.get("ОГРН", (None, None))[0],
        isin=isin,
        region_url=req.get("Регион", (None, None))[1] if kind == "bond" else None,
        industry=req.get("Отрасль", (None, None))[0],
        blocks=blocks,
        linked=sorted(set(linked)),
    )


def _object_key(e: Entity) -> tuple[str, str]:
    slug = urlsplit(e.url).path.rstrip("/").rsplit("/", 1)[-1]
    if e.kind == "bond":
        return "issue", e.isin or f"{AGENCY}:bond:{slug}"
    if e.kind == "company" and e.inn:
        return "issuer", e.inn
    return "issuer", f"{AGENCY}:{e.kind}:{slug}"


def entity_records(e: Entity) -> tuple[list[RatingRecord], list[ActionRecord]]:
    """Latest state per rating type and every history row of an object page."""
    kind, key = _object_key(e)
    states: list[RatingRecord] = []
    actions: dict[tuple[object, ...], ActionRecord] = {}
    extra = {"page": e.url}
    if e.region_url:
        extra["region_page"] = e.region_url
    for b in e.blocks:
        if not b.rows:
            continue
        for r in b.rows:
            a = ActionRecord(
                agency=AGENCY,
                object_kind=kind,
                object_key=key,
                rating_type=b.rating_type,
                scale=NATIONAL_SCALE,
                action_date=r.day,
                action_ref=r.release or "",
                rating=r.rating,
                grade=normalize_grade(r.rating),
                outlook=r.outlook,
                watch=r.watch,
                action="withdrawn" if r.rating is None else None,
                action_raw=r.raw if r.rating is None else None,
                title=None,
            )
            actions[a.key] = a
        latest = max(b.rows, key=lambda r: r.day)
        states.append(
            RatingRecord(
                agency=AGENCY,
                object_kind=kind,
                object_key=key,
                rating_type=b.rating_type,
                scale=NATIONAL_SCALE,
                agency_object_id=e.url,
                object_name=e.name,
                isin=e.isin,
                inn=e.inn,
                ogrn=e.ogrn,
                rating=latest.rating,
                grade=normalize_grade(latest.rating),
                outlook=latest.outlook,
                watch=latest.watch,
                action="withdrawn" if latest.rating is None else None,
                action_raw=latest.raw if latest.rating is None else None,
                rating_date=latest.day,
                press_release=latest.release,
                industry=e.industry,
                is_withdrawn=latest.rating is None,
                extra=extra,
            )
        )
    return states, sorted(actions.values(), key=lambda a: a.key)
