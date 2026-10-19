"""Canonical JSON of published snapshots (Market Snapshot Contract v1, §3.1 and §3.4).

The snapshot ID is the SHA-256 of the canonical bytes, so the encoding must be stable:

* keys sorted (code points), no whitespace (``","``/``":"``), UTF-8 without escaping non-ASCII;
* decimals as JSON **strings** with exactly the stored digits (``format(d, "f")``:
  ``Decimal("14.500")`` -> ``"14.500"``, never float-rounded, never exponent notation);
* dates ``YYYY-MM-DD``; instants in UTC with a ``Z`` suffix (microseconds only when non-zero);
* binary floats and non-finite decimals are rejected (a float would carry rounding noise);
* ``None`` -> ``null``; tuples are lists. Array order is the builder's responsibility (every
  array is sorted by its natural key before encoding).

The bundle is the gzip (``mtime=0``, so the bytes are stable too) of the canonical bytes; a
consumer verifies ``sha256(gunzip(bundle)) == snapshot_id``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

__all__ = [
    "canonical_bytes",
    "decimal_str",
    "gunzip",
    "gzip_bytes",
    "sha256_hex",
    "to_wire",
    "utc_str",
]


def decimal_str(value: Decimal | int | None) -> str | None:
    """Exact digits of a stored ``numeric`` as a string (``None`` stays ``None``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("bool is not a decimal")
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, Decimal):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"non-finite decimal {value!r}")
    return format(value, "f")


def utc_str(value: datetime | None) -> str | None:
    """Aware timestamp -> ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` (UTC)."""
    if value is None:
        return None
    if value.utcoffset() is None:
        raise ValueError(f"naive timestamp {value!r}")
    text = value.astimezone(UTC).replace(tzinfo=None).isoformat()
    return text + "Z"


def to_wire(value: Any) -> Any:
    """Convert Python values to their wire form (strings for decimals, dates, instants)."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, float):
        raise TypeError("binary floats are not allowed in snapshots (use Decimal)")
    if isinstance(value, datetime):
        return utc_str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): to_wire(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_wire(v) for v in value]
    raise TypeError(f"unsupported type in snapshot: {type(value).__name__}")


def _reject_float(value: float) -> Any:
    raise TypeError("binary floats are not allowed in snapshots (use Decimal)")


def canonical_bytes(document: Any) -> bytes:
    """Canonical UTF-8 JSON of ``document`` (converted with :func:`to_wire` first)."""
    wire = to_wire(document)
    text = json.dumps(
        wire,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_reject_float,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gzip_bytes(data: bytes) -> bytes:
    """Deterministic gzip (no file name, ``mtime=0``)."""
    return gzip.compress(data, compresslevel=9, mtime=0)


def gunzip(data: bytes) -> bytes:
    return gzip.decompress(data)
