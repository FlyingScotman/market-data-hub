"""Shared model and normalization of credit ratings from the agencies' public disclosure
(WEB-16; sources :mod:`mdhub.sources.nkr`, :mod:`mdhub.sources.nra`,
:mod:`mdhub.sources.raexpert`). No network, no DB.

Values are kept **verbatim** (``rating``, ``outlook``, ``watch``, ``action_raw``) next to two
normalized helpers that are documented rules, never guesses:

* :func:`normalize_grade` - the letter grade of a national-scale credit rating without the
  agency's scale marks (``ruAA-``, ``AA-.ru``, ``AA–|ru|`` -> ``AA-``); Cyrillic look-alike
  letters (``ruAАА``) and en dashes are mapped to ASCII. Anything that is not a grade of the
  ``AAA..D`` alphabet (withdrawn, ESG, unknown) -> ``None``.
* :func:`normalize_action` - the published action verb or status (``Подтверждён``,
  ``присвоило``) as ``assigned`` / ``affirmed`` / ``upgraded`` / ``downgraded`` /
  ``withdrawn`` / ``outlook_changed``; no recognised verb -> ``None`` (the hub does not derive
  an action from consecutive ratings).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from mdhub.core.errors import FormatDriftError

__all__ = [
    "AGENCY_NAMES",
    "NATIONAL_SCALE",
    "ActionRecord",
    "RatingRecord",
    "blank",
    "is_watch",
    "normalize_action",
    "normalize_grade",
    "parse_ru_date",
    "require_header",
]

NATIONAL_SCALE: Final = "national_ru"
"""Scale code of every rating these sources publish: the national rating scale for the
Russian Federation (agency marks ``ru…`` / ``….ru`` / ``…|ru|``)."""

AGENCY_NAMES: Final = {"nkr": "НКР", "nra": "НРА", "raexpert": "Эксперт РА", "acra": "АКРА"}
"""Display name per hub source code (``ref.bond_rating.agency`` / ``ref.issuer_rating``)."""

_LOOKALIKES: Final = str.maketrans(
    {
        "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O",
        "Р": "P", "Т": "T", "Х": "X", "а": "a", "с": "c", "е": "e", "о": "o", "р": "p",
        "–": "-", "—": "-", "‐": "-", "‑": "-", "−": "-",
    }
)  # fmt: skip
_GRADE_RE: Final = re.compile(
    r"(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|CC|C|RD|SD|DDD|DD|D)"
)
_MARKS_RE: Final = re.compile(r"^ru(?=[A-DRS])|\.ru$|\|ru\|$|\(RU\)$", re.IGNORECASE)

_ACTIONS: Final = (
    ("withdrawn", re.compile(r"отозва|отзыв", re.IGNORECASE)),
    ("upgraded", re.compile(r"повыси|повыш", re.IGNORECASE)),
    ("downgraded", re.compile(r"понизи|пониж|снизи|сниж", re.IGNORECASE)),
    ("assigned", re.compile(r"присвои|присвоен|присво", re.IGNORECASE)),
    ("affirmed", re.compile(r"подтверди|подтвержд", re.IGNORECASE)),
    ("outlook_changed", re.compile(r"изменил\w*\s+прогноз", re.IGNORECASE)),
)
_WATCH_RE: Final = re.compile(r"пересмотр|под наблюдени", re.IGNORECASE)
_BLANKS: Final = frozenset({"", "-", "—", "–", "n/a", "нет"})
_DATE_RE: Final = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


@dataclass(frozen=True, slots=True)
class RatingRecord:
    """Latest state of one rated object as an agency publishes it (``ref.agency_rating``)."""

    agency: str
    object_kind: str  # issuer | issue
    object_key: str  # issue: ISIN; issuer: INN, else "<agency>:<id>"
    rating_type: str
    scale: str
    agency_object_id: str | None
    object_name: str | None
    isin: str | None
    inn: str | None
    ogrn: str | None
    rating: str | None
    grade: str | None
    outlook: str | None
    watch: str | None
    action: str | None
    action_raw: str | None
    rating_date: date | None
    press_release: str | None
    industry: str | None
    is_withdrawn: bool
    extra: dict[str, str] = field(default_factory=dict)
    """Parser-side context that is not stored in ``ref.agency_rating`` (e.g. the region page
    of an RA Expert bond, used to map regions to MOEX issuers)."""

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.agency, self.object_kind, self.object_key, self.rating_type, self.scale)


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One published rating action (``ref.rating_action``)."""

    agency: str
    object_kind: str
    object_key: str
    rating_type: str
    scale: str
    action_date: date
    action_ref: str  # press release URL or agency action id; "" when none
    rating: str | None
    grade: str | None
    outlook: str | None
    watch: str | None
    action: str | None
    action_raw: str | None
    title: str | None

    @property
    def key(self) -> tuple[str, str, str, str, str, date, str]:
        return (
            self.agency, self.object_kind, self.object_key, self.rating_type, self.scale,
            self.action_date, self.action_ref,
        )  # fmt: skip


def blank(value: object) -> str | None:
    """Stripped text, ``None`` for empty cells and the agencies' dash placeholders."""
    if value is None:
        return None
    text = " ".join(str(value).replace("\xa0", " ").split())
    return None if text.lower() in _BLANKS else text


def normalize_grade(rating: str | None) -> str | None:
    """Letter grade of a national-scale credit rating (see module docstring)."""
    text = blank(rating)
    if text is None:
        return None
    text = text.translate(_LOOKALIKES).replace(" ", "")
    text = _MARKS_RE.sub("", text)
    if not text or text != text.upper():
        # lower-case grades are standalone credit assessments (e.g. NKR SCA 'a+.ru')
        return None
    m = _GRADE_RE.fullmatch(text)
    return m.group(1) if m else None


def normalize_action(text: str | None) -> str | None:
    """Action code of a published verb or status (see module docstring)."""
    t = blank(text)
    if t is None:
        return None
    for code, pattern in _ACTIONS:
        if pattern.search(t):
            return code
    return None


def is_watch(text: str | None) -> bool:
    """``True`` for a published review / watch status (``рейтинг на пересмотре …``,
    ``Под наблюдением``)."""
    t = blank(text)
    return t is not None and bool(_WATCH_RE.search(t)) and "снят" not in t.lower()


def parse_ru_date(value: object, *, field: str = "date") -> date | None:
    """``DD.MM.YYYY`` (or an XLSX date serial / ISO date) -> date; blank -> ``None``."""
    t = blank(value)
    if t is None:
        return None
    m = _DATE_RE.fullmatch(t)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            raise FormatDriftError(f"{field}: bad date {t!r}") from None
    try:
        return datetime.strptime(t[:10], "%Y-%m-%d").date()
    except ValueError:
        raise FormatDriftError(f"{field}: not a date: {t!r}") from None


def require_header(
    header: list[str | None], required: tuple[str, ...], what: str
) -> dict[str, int]:
    """Column index per name of ``header``; :class:`FormatDriftError` if one is missing."""
    names = [blank(h) or "" for h in header]
    missing = [r for r in required if r not in names]
    if missing:
        raise FormatDriftError(f"{what}: missing columns {missing} (header {names})")
    return {n: i for i, n in enumerate(names) if n}
