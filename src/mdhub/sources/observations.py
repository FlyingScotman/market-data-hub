"""Dated observations of a time series, shared by the SDMX / CSV sources of reference rates
(:mod:`mdhub.sources.ecb_data`, :mod:`~mdhub.sources.bundesbank`, :mod:`~mdhub.sources.dbnomics`,
:mod:`~mdhub.sources.fred`): ``(series, day, value)`` with the value as published.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from mdhub.core.errors import FormatDriftError

__all__ = ["Observation", "check_range", "parse_day", "parse_value", "unique"]

_DAY_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class Observation:
    series: str
    """Series id of the source (``EST.B.EU000A2X2A25.WT``, ``SOFR``, ``PY_10Y`` ...)."""
    day: date
    value: Decimal


def parse_day(text: str, where: str) -> date:
    text = text.strip()
    if not _DAY_RE.match(text):
        raise FormatDriftError(f"{where}: bad date {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise FormatDriftError(f"{where}: bad date {text!r}") from None


def parse_value(text: str, where: str) -> Decimal:
    """A decimal number as published (``"2.44"``, ``"-0.549"``); never through float."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise FormatDriftError(f"{where}: not a number {text!r}") from None
    if not value.is_finite():
        raise FormatDriftError(f"{where}: not a number {text!r}")
    return value


def check_range(obs: Iterable[Observation], lo: Decimal, hi: Decimal, where: str) -> None:
    """Fail closed when a value lies outside ``[lo, hi]`` (unit or column drift)."""
    for o in obs:
        if not lo <= o.value <= hi:
            raise FormatDriftError(f"{where}: {o.series} {o.day} value {o.value} out of range")


def unique(obs: list[Observation], where: str) -> list[Observation]:
    """``obs`` sorted by (series, day); a duplicate (series, day) raises."""
    seen: set[tuple[str, date]] = set()
    for o in obs:
        if (o.series, o.day) in seen:
            raise FormatDriftError(f"{where}: duplicate {o.series} {o.day}")
        seen.add((o.series, o.day))
    return sorted(obs, key=lambda o: (o.series, o.day))
