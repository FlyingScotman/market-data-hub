"""Decimal helpers: prices and rates are ``numeric`` / :class:`~decimal.Decimal`, never float.

Floats coming from JSON are converted through ``str()`` so that ``0.1`` becomes
``Decimal('0.1')``, not ``Decimal('0.1000000000000000055511151231257827')``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from mdhub.core.errors import FormatDriftError

__all__ = ["bp_to_pct", "mid", "pct_to_bp", "quantize", "require_decimal", "to_decimal"]


def to_decimal(value: Any, *, field: str = "value") -> Decimal | None:
    """Convert a source value to :class:`Decimal`; ``None``/``""`` -> ``None``.

    Accepts ``int``, ``float`` (via ``str``), ``Decimal`` and numeric strings (``","`` as the
    decimal separator is accepted, spaces/NBSP thousands separators are removed).

    :raises FormatDriftError: booleans, NaN/Infinity or unparsable text.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise FormatDriftError(f"{field}: boolean is not a number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip().replace(" ", "").replace(" ", "").replace(",", ".")
        if not text:
            return None
        try:
            result = Decimal(text)
        except InvalidOperation:
            raise FormatDriftError(f"{field}: not a number: {value!r}") from None
    else:
        raise FormatDriftError(f"{field}: unsupported type {type(value).__name__}")
    if not result.is_finite():
        raise FormatDriftError(f"{field}: non-finite number {value!r}")
    return result


def require_decimal(value: Any, *, field: str = "value") -> Decimal:
    """Like :func:`to_decimal` but a missing value raises :class:`FormatDriftError`."""
    result = to_decimal(value, field=field)
    if result is None:
        raise FormatDriftError(f"{field}: value is missing")
    return result


def quantize(value: Decimal, places: int) -> Decimal:
    """Round half-even to ``places`` decimal places."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def mid(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    """``(bid + ask) / 2`` or ``None`` if either side is missing (never one-sided)."""
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def pct_to_bp(value: Decimal) -> Decimal:
    """Percent -> basis points."""
    return value * 100


def bp_to_pct(value: Decimal) -> Decimal:
    """Basis points -> percent."""
    return value / 100
