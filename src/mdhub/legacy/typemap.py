"""SQLite storage classes -> PostgreSQL types, value conversion and canonical row hashing.

SQLite is dynamically typed: the declared type only gives an *affinity*, every value has
its own storage class (``typeof``). The PostgreSQL type of a copied column is therefore
chosen from the storage classes actually present (:func:`choose_pg_type`):

==========================  =======================  =========================
present (non-null)          PostgreSQL type          note
==========================  =======================  =========================
none (empty / all NULL)     from declared affinity   INTEGER->bigint, REAL->double
                                                     precision, TEXT->text,
                                                     BLOB->bytea, NUMERIC->numeric
integer                     bigint                   SQLite ints are int64
real                        double precision         IEEE double, bit-exact
text                        text                     Decimal strings stay text
blob                        bytea
integer + real              numeric + ``__typeof``   exact (repr of the double);
                                                     text if a real is ±0.0 / ±inf
text containing U+0000      bytea + ``__typeof``     UTF-8 bytes
any other mix               text + ``__typeof``      bytea if a blob is present
==========================  =======================  =========================

A *tagged* column (any mix of storage classes) gets a companion column
``<name>__typeof`` (``text``: integer/real/text/blob, NULL for NULL) so the original
value and its storage class can be reconstructed exactly (:func:`from_pg`). None of the
real legacy files needs a tagged column; the mechanism exists so that the copy is lossless
for any input.

Checksums (:func:`row_digest`, :class:`TableHasher`) are computed from the reconstructed
Python values on both sides with one canonical, self-delimiting serialization: storage
class tag + length + payload, floats via ``repr`` (shortest round-trip form), text as
UTF-8, blobs raw. The table checksum is order independent (sorted row digests), so no
collation or physical order has to agree between SQLite and PostgreSQL.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from mdhub.legacy.sqlite_source import ColumnInfo

__all__ = [
    "COPY_TYPE",
    "TYPEOF_SUFFIX",
    "PgColumn",
    "TableHasher",
    "choose_pg_type",
    "encode_value",
    "from_pg",
    "row_digest",
    "to_pg",
]

TYPEOF_SUFFIX: Final = "__typeof"

COPY_TYPE: Final = {
    "bigint": "int8",
    "double precision": "float8",
    "text": "text",
    "bytea": "bytea",
    "numeric": "numeric",
}
"""PostgreSQL type -> psycopg type name for binary COPY ``set_types``."""

_AFFINITY_TYPE: Final = {
    "INTEGER": "bigint",
    "REAL": "double precision",
    "TEXT": "text",
    "BLOB": "bytea",
    "NUMERIC": "numeric",
}


def choose_pg_type(col: ColumnInfo) -> tuple[str, bool]:
    """Return ``(pg_type, tagged)`` for a column from its observed storage classes."""
    present = col.present
    if not present:
        return _AFFINITY_TYPE[col.affinity], False
    if len(present) == 1:
        (only,) = present
        if only == "integer":
            return "bigint", False
        if only == "real":
            return "double precision", False
        if only == "blob":
            return "bytea", False
        if col.text_with_nul:
            return "bytea", True
        return "text", False
    if present == {"integer", "real"}:
        return ("text" if col.real_special else "numeric"), True
    if "blob" in present or col.text_with_nul:
        return "bytea", True
    return "text", True


@dataclass(frozen=True, slots=True)
class PgColumn:
    """Target column of a copied SQLite column."""

    sqlite_name: str
    name: str
    pg_type: str
    tagged: bool
    notnull: bool

    @property
    def typeof_name(self) -> str | None:
        """Companion storage-class column, if tagged."""
        return self.name + TYPEOF_SUFFIX if self.tagged else None


_TAG: Final[dict[type, str]] = {int: "integer", float: "real", str: "text", bytes: "blob"}


def to_pg(pg_type: str) -> Callable[[Any], tuple[Any, str | None]]:
    """Converter for a *tagged* column: SQLite value -> ``(pg value, storage class)``."""

    def conv(v: Any) -> tuple[Any, str | None]:
        if v is None:
            return None, None
        tag = _TAG[type(v)]
        if pg_type == "numeric":
            return (Decimal(v) if tag == "integer" else Decimal(repr(v))), tag
        if tag == "integer":
            s = str(v)
        elif tag == "real":
            s = repr(v)
        elif tag == "text":
            s = v
        else:  # blob: only stored in bytea columns
            return bytes(v), tag
        return (s.encode("utf-8", "surrogatepass") if pg_type == "bytea" else s), tag

    return conv


def from_pg(value: Any, tag: str | None) -> Any:
    """Reconstruct the SQLite value of a *tagged* column from ``(value, storage class)``."""
    if tag is None:
        if value is not None:
            raise ValueError("value without storage class")
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if tag == "blob":
            return raw
        value = raw.decode("utf-8", "surrogatepass")
    if tag == "integer":
        return int(value)
    if tag == "real":
        return float(value)
    if tag == "text":
        if not isinstance(value, str):
            raise ValueError(f"text storage class with {type(value).__name__} value")
        return value
    raise ValueError(f"unexpected storage class {tag!r}")


def encode_value(v: Any) -> bytes:
    """Canonical self-delimiting serialization of one SQLite value."""
    if v is None:
        return b"n"
    t = type(v)
    if t is int:
        p = str(v).encode()
        tag = b"i"
    elif t is float:
        p = repr(v).encode()
        tag = b"r"
    elif t is str:
        p = v.encode("utf-8", "surrogatepass")
        tag = b"t"
    elif t is bytes:
        p = v
        tag = b"b"
    else:
        raise TypeError(f"unexpected value type {t.__name__} in canonical serialization")
    return tag + str(len(p)).encode() + b":" + p


def row_digest(row: Sequence[Any]) -> bytes:
    """SHA-256 digest of one row's canonical serialization."""
    return hashlib.sha256(b"".join(encode_value(v) for v in row)).digest()


class TableHasher:
    """Order-independent content checksum of a table (multiset of rows)."""

    def __init__(self) -> None:
        self._digests: list[bytes] = []

    def add(self, row: Sequence[Any]) -> None:
        self._digests.append(row_digest(row))

    @property
    def rows(self) -> int:
        return len(self._digests)

    def hexdigest(self) -> str:
        h = hashlib.sha256()
        for d in sorted(self._digests):
            h.update(d)
        return h.hexdigest()
