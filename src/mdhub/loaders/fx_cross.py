"""Non-RUB cross rates besides the ECB reference rates -> ``md.fx_cross`` (WEB-16).

The ECB reference rates (``ecb_fx``, ``md.ecb_fx``) do not answer from the hub VPS (WEB-4), so
the hub keeps independent cross-rate series, each a separate ``kind`` (never merged with the ECB
rates or with each other; DESIGN §2):

* ``cbr_cross`` (source ``cbr``) - derived from two CBR official rates in force on the same
  **effective date** (``md.fx_official``, every calendar day a rate is in force): EUR/USD =
  RUB per EUR / RUB per USD. Labelled ``basis = cbr_cross`` in the snapshot. The CBR sets its
  rates from the Moscow market (and, for USD/EUR since 2024-06-13, from OTC trades), so this is
  an official Moscow-based cross, not a global market rate,
* ``moex_indicative_cross`` (source ``moex_iss``) - derived from the two MOEX 19:00 (main
  clearing ``mc``) indicative RUB rates of the trade date (``md.fx_indicative``),
* ``moex_cets`` (source ``moex_iss``) - a **traded** CETS cross outright (``*_TOM``,
  e.g. ``EURUSD000TOM``, ``USDCNY_TOM``; ``md.fx_eod`` + ``ref.fx_instrument``): close per unit,
  only on days with trades. The USD crosses have had no trades since 2024-06-11 (USD and EUR
  trading on MOEX stopped on 2024-06-12), so this kind currently has history only,
* ``fred_h10`` (source ``fred``) - Federal Reserve H.10 noon buying rates in New York via FRED
  (``DEXUSEU`` ...; published weekly with daily values, so the latest days lag up to a week).

The derived kinds read stored rows only (no request, no raw capture; ``details`` name both
inputs and their dates) and are rounded half-even to :data:`SIGNIFICANT_DIGITS` significant
digits. A pair whose input is missing on a day has no row that day (no forward fill).

``fx_cross(date_from=None, date_to=None, sources=None)``: without dates the last
:data:`LOOKBACK_DAYS` days (an empty table: from :data:`HISTORY_START`). ``sources`` limits the
kinds (``cbr``, ``moex_iss``, ``fred``). A failing FRED request is logged and listed in the
result (``fred_error``); the derived kinds are stored anyway.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any, Final

import httpx

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.gaps import record_no_data
from mdhub.core.http import HttpError
from mdhub.core.redact import sanitize_text
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.db.pool import DictConnection
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.sources import fred

__all__ = ["HISTORY_START", "LOOKBACK_DAYS", "PAIRS", "SIGNIFICANT_DIGITS", "cross", "fx_cross"]

KEY: Final = ["rate_date", "base_ccy", "quote_ccy", "kind"]
LOOKBACK_DAYS: Final = 10
HISTORY_START: Final = date(2015, 1, 1)
SIGNIFICANT_DIGITS: Final = 10
PAIRS: Final = (
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "CNY"),
    ("USD", "JPY"),
    ("USD", "CHF"),
    ("USD", "HKD"),
    ("USD", "INR"),
    ("USD", "KZT"),
    ("USD", "TRY"),
    ("USD", "AED"),
    ("EUR", "CNY"),
)
"""Derived pairs (market convention: units of quote per 1 base)."""
_SOURCES: Final = ("cbr", "moex_iss", "fred")
_FRED_SERIES: Final = tuple(fred.H10_PAIRS)

for _kind, _source, _cal, _freq, _start, _desc in (
    ("cbr_cross", "cbr", None, "daily", HISTORY_START, "CBR official rates, by effective date"),
    (
        "moex_indicative_cross",
        "moex_iss",
        "MOEX_FX",
        "business_daily",
        date(2015, 3, 30),
        "MOEX 19:00 indicative rates, by trade date",
    ),
    ("fred_h10", "fred", None, "event", HISTORY_START, "FRED H.10 noon buying rates (weekly)"),
    ("moex_cets", "moex_iss", "MOEX_FX", "event", HISTORY_START, "traded CETS cross outrights"),
):
    register_dataset(
        DatasetSpec(
            name=f"fx_cross_{_kind}",
            table="md.fx_cross",
            date_column="rate_date",
            calendar=_cal,
            frequency=_freq,  # type: ignore[arg-type]
            max_lag_days=10 if _kind == "fred_h10" else 3,
            backfill_job="fx_cross" if _freq != "event" else None,
            history_start=_start,
            where=f"kind = '{_kind}'",
            description=f"Cross rates {_kind}: {_desc}",
        )
    )


def cross(base_rub: Decimal, quote_rub: Decimal) -> Decimal:
    """Units of quote per 1 base from two RUB-per-unit rates, :data:`SIGNIFICANT_DIGITS`
    significant digits, half-even."""
    ratio = Context(prec=40).divide(base_rub, quote_rub)
    exp = ratio.adjusted() - (SIGNIFICANT_DIGITS - 1)
    return ratio.quantize(Decimal(1).scaleb(exp), rounding=ROUND_HALF_EVEN).normalize()


def _derived(
    by_day: dict[date, dict[str, tuple[Decimal, str]]], kind: str, source: str, run_id: int
) -> list[dict[str, Any]]:
    rows = []
    for day, rates in sorted(by_day.items()):
        for base, quote in PAIRS:
            if base not in rates or quote not in rates:
                continue
            (b, b_note), (q, q_note) = rates[base], rates[quote]
            rows.append(
                {
                    "rate_date": day,
                    "base_ccy": base,
                    "quote_ccy": quote,
                    "kind": kind,
                    "source": source,
                    "rate": cross(b, q),
                    "details": {
                        f"{base}_rub": str(b),
                        f"{quote}_rub": str(q),
                        f"{base}_input": b_note,
                        f"{quote}_input": q_note,
                    },
                    "capture_id": None,
                    "run_id": run_id,
                }
            )
    return rows


def _cbr_rows(conn: DictConnection, d1: date, d2: date, run_id: int) -> list[dict[str, Any]]:
    ccys = sorted({c for p in PAIRS for c in p})
    by_day: dict[date, dict[str, tuple[Decimal, str]]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT effective_date, ccy, rate_per_unit, set_date FROM md.fx_official "
        "WHERE effective_date BETWEEN %s AND %s AND ccy = ANY(%s)",
        (d1, d2, ccys),
    ).fetchall():
        by_day[r["effective_date"]][r["ccy"]] = (r["rate_per_unit"], f"set {r['set_date']}")
    return _derived(by_day, "cbr_cross", "cbr", run_id)


def _indicative_rows(conn: DictConnection, d1: date, d2: date, run_id: int) -> list[dict[str, Any]]:
    by_day: dict[date, dict[str, tuple[Decimal, str]]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT trade_date, base_ccy, rate, trade_time FROM md.fx_indicative "
        "WHERE clearing = 'mc' AND quote_ccy = 'RUB' AND trade_date BETWEEN %s AND %s",
        (d1, d2),
    ).fetchall():
        when = r["trade_time"].strftime("%H:%M") if r["trade_time"] else "mc"
        by_day[r["trade_date"]][r["base_ccy"]] = (r["rate"], f"indicative {when}")
    return _derived(by_day, "moex_indicative_cross", "moex_iss", run_id)


def _cets_rows(conn: DictConnection, d1: date, d2: date, run_id: int) -> list[dict[str, Any]]:
    rows = []
    for r in conn.execute(
        """
        SELECT e.trade_date, e.secid, e.board, e.close, e.num_trades, i.base_ccy, i.quote_ccy,
               i.face_value
        FROM md.fx_eod e JOIN ref.fx_instrument i ON i.secid = e.secid AND i.board = e.board
        WHERE e.trade_date BETWEEN %s AND %s AND i.kind = 'outright' AND i.tenor = 'TOM'
          AND i.quote_ccy <> 'RUB' AND i.base_ccy ~ '^[A-Z]{3}$' AND i.quote_ccy ~ '^[A-Z]{3}$'
          AND i.base_ccy NOT IN ('GLD', 'SLV', 'PLT', 'PLD')
          AND e.num_trades > 0 AND e.close > 0
        ORDER BY e.trade_date, e.secid
        """,
        (d1, d2),
    ).fetchall():
        units = r["face_value"]
        if units is None or units <= 0:
            continue
        norm = units.normalize()
        _sign, digits, exp = norm.as_tuple()
        if digits != (1,) or not isinstance(exp, int) or exp < 0:
            continue  # quote units not a power of ten: no exact per-unit value
        rows.append(
            {
                "rate_date": r["trade_date"],
                "base_ccy": r["base_ccy"],
                "quote_ccy": r["quote_ccy"],
                "kind": "moex_cets",
                "source": "moex_iss",
                "rate": r["close"].scaleb(-exp),
                "details": {
                    "secid": r["secid"],
                    "board": r["board"],
                    "num_trades": str(r["num_trades"]),
                },
                "capture_id": None,
                "run_id": run_id,
            }
        )
    return rows


def _fred_rows(ctx: JobContext, d1: date, d2: date) -> tuple[list[dict[str, Any]], str | None]:
    try:
        with ctx.http(max_retries=1) as http:
            resp = fred.fetch(http, _FRED_SERIES, d1, d2)
        obs = fred.parse_csv(resp.content, _FRED_SERIES)
        fred.check_positive(obs)
    except (HttpError, httpx.HTTPError, OSError, FormatDriftError) as exc:
        error = sanitize_text(f"{type(exc).__name__}: {exc}", ctx.settings.secret_values())
        ctx.logger.warning("fx_cross: FRED H.10 failed: %s", error[:300])
        return [], error[:300]
    with ctx.connection() as conn:
        cap = record_capture(
            conn, ctx.run.id, "fred", "fredgraph_h10", None, resp, fred.PARSER_VERSION
        )
    rows = []
    for o in obs:
        base, quote = fred.H10_PAIRS[o.series]
        rows.append(
            {
                "rate_date": o.day,
                "base_ccy": base,
                "quote_ccy": quote,
                "kind": "fred_h10",
                "source": "fred",
                "rate": o.value,
                "details": {"series": o.series},
                "capture_id": cap,
                "run_id": ctx.run.id,
            }
        )
    return rows, None


_GAP_KINDS: Final = {
    "cbr_cross": ("cbr", HISTORY_START, None),
    "moex_indicative_cross": ("moex_iss", date(2015, 3, 30), "MOEX_FX"),
}
"""Derived kinds with a gap scan: (source, first day, calendar; ``None`` = every day)."""


def _record_no_data(
    ctx: JobContext, conn: DictConnection, use: set[str], d1: date, d2: date
) -> list[str]:
    """Backfill contract: an expected day without any row of a derived kind (its inputs are not
    stored) is recorded as no data, so the gap does not stay open for ever."""
    out: list[str] = []
    for kind, (source, start, calendar) in _GAP_KINDS.items():
        lo = max(d1, start)
        if source not in use or lo > d2:
            continue
        have = {
            r["rate_date"]
            for r in conn.execute(
                "SELECT DISTINCT rate_date FROM md.fx_cross WHERE kind = %s "
                "AND rate_date BETWEEN %s AND %s",
                (kind, lo, d2),
            ).fetchall()
        }
        day = lo
        while day <= d2:
            expected = calendar is None or ctx.is_business_day(day, calendar)
            if expected and day not in have:
                record_no_data(
                    conn,
                    f"fx_cross_{kind}",
                    day,
                    f"{kind}: input rates not stored for {day}",
                    today=ctx.today(),
                )
                out.append(f"{kind}:{day}")
            day += timedelta(days=1)
    return out


@job("fx_cross", "Cross rates: CBR / MOEX indicative derived, MOEX CETS, FRED H.10 -> md.fx_cross")
def fx_cross(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    unknown = sorted(set(sources or ()) - set(_SOURCES))
    if unknown:
        raise JobParamError(f"unknown source(s) {unknown} (known: {list(_SOURCES)})")
    use = set(sources or _SOURCES) - (ctx.settings.skipped_sources() if not sources else set())
    today = ctx.today()
    if date_from is None and date_to is None:
        with ctx.connection() as conn:
            empty = conn.execute("SELECT 1 FROM md.fx_cross LIMIT 1").fetchone() is None
        d1 = HISTORY_START if empty else today - timedelta(days=LOOKBACK_DAYS)
        d2 = today + timedelta(days=7)  # CBR rates set today are in force from tomorrow
    else:
        d1, d2 = date_from or HISTORY_START, date_to or today
    if d1 > d2:
        raise ValueError(f"date_from {d1} after date_to {d2}")
    known_at = utc_now()
    rows: list[dict[str, Any]] = []
    fred_error = None
    if "fred" in use:
        fred_rows, fred_error = _fred_rows(ctx, d1, min(d2, today))
        rows += fred_rows
    with ctx.connection() as conn:
        if "cbr" in use:
            rows += _cbr_rows(conn, d1, d2, ctx.run.id)
        if "moex_iss" in use:
            rows += _indicative_rows(conn, d1, d2, ctx.run.id)
            rows += _cets_rows(conn, d1, d2, ctx.run.id)
        for r in rows:
            r["ingested_at"] = known_at
        result = upsert_rows(conn, "md.fx_cross", rows, KEY, run_id=ctx.run.id)
        no_data = _record_no_data(ctx, conn, use, d1, min(d2, today - timedelta(days=2)))
    ctx.run.add_counts(result)
    by_kind: dict[str, int] = defaultdict(int)
    for r in rows:
        by_kind[r["kind"]] += 1
    return {
        "range": [str(d1), str(d2)],
        "rows_by_kind": dict(sorted(by_kind.items())),
        "fred_error": fred_error,
        "no_data": no_data,
        "inserted": result.inserted,
        "updated": result.updated,
    }
