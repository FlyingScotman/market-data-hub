"""Deutsche Bundesbank time series REST API (``api.statistiken.bundesbank.de``): fetch + parse.

Series used by the hub (keys from the Bundesbank time series database; the host does not answer
from the development environment, so the format below follows the documented CSV export and
recorded third-party downloads, not a response recorded by the hub - check with
``mdhub probe-sources`` on the server):

* ``BBMMB/D.EU000A2X2A25.WT`` - the ECB's euro short-term rate (€STR) as republished by the
  Bundesbank (money market rates), % p.a.,
* ``BBSIS/D.I.ZAR.ZI.EUR.S1311.B.A604.<RnnXX>.R.A.A._Z._Z.A`` - yields of listed German Federal
  securities with annual coupons derived from the term structure (par-style yields by residual
  maturity), % p.a. (a **proxy** of a EUR curve: German government, not AAA euro area, not OIS).

CSV (``format=csv&lang=en``): a UTF-8 BOM, a header line ``"",<series id>,<series id>_FLAGS``
(several series: one value/flags column pair each), metadata lines (title, unit, last update,
...), then one line per **calendar day** ``YYYY-MM-DD,<value>,<flag>``; a missing value is ``.``
(flag ``No value available``). Without ``lang=en`` the export is semicolon-separated with decimal
commas: the delimiter is taken from the header line, a decimal comma is accepted.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.observations import Observation, check_range, parse_day, parse_value, unique

__all__ = [
    "BASE_URL",
    "ESTR_FLOW",
    "ESTR_KEY",
    "PARSER_VERSION",
    "PAR_TENORS",
    "estr_from_csv",
    "fetch",
    "par_curve_from_csv",
    "par_key",
    "parse_csv",
]

BASE_URL: Final = "https://api.statistiken.bundesbank.de/rest/data"
PARSER_VERSION: Final = "1"
ESTR_FLOW: Final = "BBMMB"
ESTR_KEY: Final = "D.EU000A2X2A25.WT"
PAR_FLOW: Final = "BBSIS"
PAR_TENORS: Final = {
    "1Y": "R01XX",
    "2Y": "R02XX",
    "3Y": "R03XX",
    "5Y": "R05XX",
    "7Y": "R07XX",
    "10Y": "R10XX",
    "15Y": "R15XX",
    "20Y": "R20XX",
    "30Y": "R30XX",
}
_PAR_PREFIX: Final = "BBSIS.D.I.ZAR.ZI.EUR.S1311.B.A604."
_PAR_SUFFIX: Final = ".R.A.A._Z._Z.A"
_DATA_LINE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MISSING: Final = frozenset({"", ".", "..."})


def par_key(tenors: tuple[str, ...] = tuple(PAR_TENORS)) -> str:
    codes = "+".join(PAR_TENORS[t] for t in tenors)
    return f"D.I.ZAR.ZI.EUR.S1311.B.A604.{codes}{_PAR_SUFFIX}"


def parse_csv(payload: bytes, flow: str) -> list[Observation]:
    """Bundesbank CSV export -> observations (``series`` = full series id)."""
    text = payload.decode("utf-8-sig")
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows or not rows[0] or rows[0][0] != "":
        raise FormatDriftError(f"Bundesbank {flow}: unexpected header {rows[:1]!r}")
    header = rows[0]
    columns = [(i, h) for i, h in enumerate(header) if i > 0 and h and not h.endswith("_FLAGS")]
    if not columns or any(not h.startswith(f"{flow}.") for _, h in columns):
        raise FormatDriftError(f"Bundesbank {flow}: unexpected series columns {header!r}")
    out: list[Observation] = []
    data_lines = 0
    for row in rows[1:]:
        if not row or not _DATA_LINE.match(row[0].strip()):
            continue  # metadata lines (title, unit, last update ...)
        data_lines += 1
        where = f"Bundesbank {flow} {row[0]}"
        day = parse_day(row[0], where)
        for i, series in columns:
            cell = row[i].strip() if i < len(row) else ""
            if cell in _MISSING:
                continue
            out.append(Observation(series, day, parse_value(cell.replace(",", "."), where)))
    if data_lines == 0:
        raise FormatDriftError(f"Bundesbank {flow}: no data lines")
    return unique(out, f"Bundesbank {flow}")


def estr_from_csv(payload: bytes) -> list[Observation]:
    obs = parse_csv(payload, ESTR_FLOW)
    foreign = {o.series for o in obs} - {f"{ESTR_FLOW}.{ESTR_KEY}"}
    if foreign:
        raise FormatDriftError(f"Bundesbank €STR: unexpected series {sorted(foreign)}")
    check_range(obs, Decimal("-2"), Decimal("20"), "Bundesbank €STR")
    return obs


def par_curve_from_csv(payload: bytes) -> list[Observation]:
    """Par-style yields; ``series`` = the tenor (``10Y``), value % p.a."""
    codes = {code: tenor for tenor, code in PAR_TENORS.items()}
    out: list[Observation] = []
    for o in parse_csv(payload, PAR_FLOW):
        code = o.series.removeprefix(_PAR_PREFIX).removesuffix(_PAR_SUFFIX)
        if not o.series.startswith(_PAR_PREFIX) or code not in codes:
            raise FormatDriftError(f"Bundesbank BBSIS: unexpected series {o.series!r}")
        out.append(Observation(codes[code], o.day, o.value))
    check_range(out, Decimal("-3"), Decimal("25"), "Bundesbank par yields")
    return unique(out, "Bundesbank BBSIS")


def fetch(http: HttpClient, flow: str, key: str, date_from: date, date_to: date) -> httpx.Response:
    """GET one flow/key (``+`` joins series) as English CSV for ``[date_from, date_to]``.

    HTTP 404 (unknown key, or no observation in the range) raises :class:`HttpStatusError`:
    the two cannot be told apart, so a fallback chain moves on to the next source."""
    return http.get(
        f"{BASE_URL}/{flow}/{key}",
        params={
            "format": "csv",
            "lang": "en",
            "startPeriod": date_from.isoformat(),
            "endPeriod": date_to.isoformat(),
        },
    )
