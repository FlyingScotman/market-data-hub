"""MOEX ISS issuer (emitter) reference: fetch + parse (no DB access).

Endpoints (public ISS, ``iss.meta=off``):

* ``/emitters.json`` - list of every emitter (EMITTER_ID, TITLE, INN, SHORT_TITLE, URL;
  ~1.4k rows, cursor-paged by 100, :func:`iter_list_pages`). Cheap daily diff: an issuer whose
  TITLE / SHORT_TITLE / INN changed in the list is refreshed before its weekly turn.
* ``/emitters/{emitter_id}.json`` - one emitter card (TITLE, SHORT_TITLE, INN, OGRN, OKPO,
  OKSM, ...; :func:`fetch_card`). One call per issuer, cached by the loader.

``OKSM`` (the Russian classifier of countries, OK 025-2001) uses the ISO 3166-1 numeric codes;
:func:`country_alpha2` maps it to ISO 3166-1 alpha-2 from the versioned package file
``mdhub/data/oksm_iso3166.json``. An unknown code maps to ``None`` (never guessed).
Market data such as ``CAPITALIZATION`` on the card is not parsed (it is not reference data).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any, Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.core.iss import IssPage, iter_pages, parse_block, to_int, to_str

__all__ = [
    "CARD_REQUIRED",
    "LIST_REQUIRED",
    "OKSM_RESOURCE",
    "PARSER_VERSION",
    "EmitterCard",
    "country_alpha2",
    "fetch_card",
    "iter_list_pages",
    "oksm_table_version",
    "parse_card",
    "parse_list_rows",
]

PARSER_VERSION: Final = "1"
LIST_URL: Final = "/emitters.json"
CARD_URL: Final = "/emitters/{emitter_id}.json"
LIST_REQUIRED: Final = ("EMITTER_ID", "TITLE", "INN", "SHORT_TITLE")
CARD_REQUIRED: Final = ("EMITTER_ID", "TITLE", "SHORT_TITLE", "INN", "OGRN", "OKPO", "OKSM")
OKSM_RESOURCE: Final = "oksm_iso3166.json"


@dataclass(frozen=True, slots=True)
class EmitterCard:
    """Reference fields of one ``/emitters/{id}.json`` card (strings stripped, ``None`` if
    MOEX publishes nothing)."""

    emitter_id: int
    title: str | None
    short_title: str | None
    inn: str | None
    ogrn: str | None
    okpo: str | None
    oksm: int | None
    country: str | None
    """ISO 3166-1 alpha-2 from ``oksm``; ``None`` when OKSM is empty or not in the table."""


@cache
def _oksm() -> tuple[str, dict[int, str]]:
    doc = json.loads(files("mdhub.data").joinpath(OKSM_RESOURCE).read_text("utf-8"))
    codes = {int(k): str(v) for k, v in doc["codes"].items()}
    return str(doc["version"]), codes


def oksm_table_version() -> str:
    """Version of the packaged OKSM -> alpha-2 table."""
    return _oksm()[0]


def country_alpha2(oksm: int | None) -> str | None:
    """ISO 3166-1 alpha-2 of an OKSM / ISO 3166-1 numeric code (``643`` -> ``"RU"``)."""
    if oksm is None:
        return None
    return _oksm()[1].get(oksm)


def iter_list_pages(http: HttpClient) -> Iterator[IssPage]:
    """Pages (100 rows) of ``/emitters.json`` with strict cursor validation."""
    return iter_pages(
        http, LIST_URL, {"limit": 100}, block="emitters", required=LIST_REQUIRED, max_pages=200
    )


def parse_list_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, str | None]]:
    """``{emitter_id: {"title", "short_title", "inn"}}`` of one list page."""
    out: dict[int, dict[str, str | None]] = {}
    for r in rows:
        eid = to_int(r["EMITTER_ID"], field="EMITTER_ID")
        if eid is None:
            raise FormatDriftError("emitters list: row without EMITTER_ID")
        out[eid] = {
            "title": to_str(r["TITLE"]),
            "short_title": to_str(r["SHORT_TITLE"]),
            "inn": to_str(r["INN"]),
        }
    return out


def fetch_card(http: HttpClient, emitter_id: int) -> httpx.Response:
    """GET ``/emitters/{emitter_id}.json`` (JSON required; see :func:`parse_card`)."""
    if emitter_id <= 0:
        raise ValueError(f"bad emitter id {emitter_id!r}")
    url = CARD_URL.format(emitter_id=int(emitter_id))
    return http.get_json(url, params={"iss.meta": "off"}).response


def parse_card(payload: Mapping[str, Any], emitter_id: int) -> EmitterCard | None:
    """Parse one card; ``None`` when ISS knows no such emitter (empty ``emitter`` block).

    :raises FormatDriftError: block or columns missing, several rows, or a row for another id.
    """
    rows = parse_block(payload, "emitter", required=CARD_REQUIRED)
    if not rows:
        return None
    if len(rows) != 1:
        raise FormatDriftError(f"emitter {emitter_id}: {len(rows)} rows, expected 1")
    r = rows[0]
    eid = to_int(r["EMITTER_ID"], field="EMITTER_ID")
    if eid != emitter_id:
        raise FormatDriftError(f"emitter {emitter_id}: card is for EMITTER_ID {eid}")
    oksm = to_int(r["OKSM"], field="OKSM")
    return EmitterCard(
        emitter_id=emitter_id,
        title=to_str(r["TITLE"]),
        short_title=to_str(r["SHORT_TITLE"]),
        inn=to_str(r["INN"]),
        ogrn=to_str(r["OGRN"]),
        okpo=to_str(r["OKPO"]),
        oksm=oksm,
        country=country_alpha2(oksm),
    )
