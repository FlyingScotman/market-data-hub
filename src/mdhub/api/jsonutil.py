"""JSON rendering without float rounding.

Database ``numeric`` values arrive as :class:`~decimal.Decimal`. The standard encoders turn
them into binary floats (``14.1`` may become ``14.099999999999999``) or strings. The read
API instead writes the decimal digits verbatim as a JSON **number** (``format(d, "f")``):
clients that parse JSON numbers as decimals (e.g. ``json.loads(..., parse_float=Decimal)``)
get the stored value exactly. Non-finite decimals/floats become ``null``.

Other types: ``date``/``datetime``/``time`` -> ISO 8601 strings (timestamps keep their UTC
offset), ``dict``/``list``/``tuple`` recursively, everything else via ``str()``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

__all__ = ["dumps"]

_str = json.encoder.encode_basestring
"""Quote and escape a string (control characters, quotes, backslash; non-ASCII kept)."""


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_str(value))
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, Decimal):
        out.append(format(value, "f") if value.is_finite() else "null")
    elif isinstance(value, float):
        out.append(repr(value) if math.isfinite(value) else "null")
    elif isinstance(value, datetime | date | time):
        out.append(_str(value.isoformat()))
    elif isinstance(value, Mapping):
        out.append("{")
        first = True
        for k, v in value.items():
            if not first:
                out.append(",")
            first = False
            out.append(_str(str(k)))
            out.append(":")
            _write(v, out)
        out.append("}")
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, v in enumerate(value):
            if i:
                out.append(",")
            _write(v, out)
        out.append("]")
    else:
        out.append(_str(str(value)))


def dumps(value: Any) -> str:
    """Serialize ``value`` to compact JSON text (UTF-8 safe, decimals verbatim)."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)
