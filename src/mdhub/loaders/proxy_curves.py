"""Government par yield curves as **labelled proxies** of USD / EUR rates curves (WEB-16, P2-1)
-> ``md.curve_snapshot`` + ``md.curve_quote``.

Until a licensed SOFR / €STR OIS source exists the desk gets government curves, clearly marked
as proxies - **not OIS** (they carry a sovereign term premium and credit / liquidity effects):

* ``usd_ust_par`` (source ``us_treasury``, ``meta.proxy = ust_par``) - U.S. Treasury daily par
  yield curve (1M .. 30Y, bond-equivalent, ~15:30 New York), history from 2024-01-02,
* ``eur_aaa_par`` (source ``ecb_data_portal``, ``meta.proxy = ecb_aaa_par``) - ECB AAA euro area
  government par yields (Svensson; 3M .. 30Y), TARGET days, from 2024-01-02; fallback when the
  ECB does not answer:
* ``eur_bund_par`` (source ``bundesbank``, ``meta.proxy = bund_par``) - Bundesbank yields of
  German Federal securities with annual coupons from the term structure (1Y .. 30Y). A different
  curve (Germany, not AAA euro area), therefore its own dataset, stored only when it served.

One snapshot per (source, dataset, curve date): ``snapshot_date`` = the curve's own business
date, ``fetched_at`` = 23:59:59 Moscow time on that date (synthetic, ``meta.fetched_at =
synthetic_eod``: the curve is an end-of-day publication, so only end-of-day cuts of that date see
it - the ``eod_final`` cut of D, built the next morning, has the U.S. curve of D; intraday cuts
and ``eod_preliminary`` never do), ``ingested_at`` = when the hub stored it (cuts as known at an
earlier time do not see it). ``quote_basis = mid_only`` (one yield per tenor, in ``mid_pct``).
Revisions (Treasury corrects a day now and then) update the snapshot in place; the old values go
to ``ops.row_history``. The market snapshot labels these quote sets ``proxy_<meta.proxy>``.

``proxy_curves(date_from=None, date_to=None, curves=None, sources=None)`` (07:40 and 23:40 MSK,
all days): without dates the last :data:`LOOKBACK_DAYS` days, an empty dataset from
:data:`HISTORY_START`. ``curves`` = ``usd`` / ``eur`` (default both); one failing curve does not
stop the other (run ``partial``), both failing fails the run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.core.capture import record_capture
from mdhub.core.gaps import record_no_data
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.loaders._fallback import Attempt, Fetched, Served, SourcesFailed, first_answer
from mdhub.sources import bundesbank, ecb_data, us_treasury
from mdhub.sources.ratescalcs import tenor_parts

__all__ = ["DATASETS", "HISTORY_START", "LOOKBACK_DAYS", "DayCurve", "proxy_curves"]

HISTORY_START: Final = date(2024, 1, 2)
LOOKBACK_DAYS: Final = 10
EOD_TIME: Final = time(23, 59, 59)
DATASETS: Final = {
    "usd_ust_par": ("us_treasury", "ust_par", "U.S. Treasury par yield curve (proxy, not OIS)"),
    "eur_aaa_par": ("ecb_data_portal", "ecb_aaa_par", "ECB AAA euro area par yields (proxy)"),
    "eur_bund_par": ("bundesbank", "bund_par", "Bundesbank German Federal par yields (proxy)"),
}
"""dataset -> (source, meta.proxy, label)."""

for _ds, (_src, _proxy, _label) in DATASETS.items():
    register_dataset(
        DatasetSpec(
            name=f"proxy_{_ds}",
            table="md.curve_snapshot",
            date_column="snapshot_date",
            calendar={"usd_ust_par": None, "eur_aaa_par": "TARGET"}.get(_ds),
            frequency="event" if _ds == "eur_bund_par" else "business_daily",
            max_lag_days=3,
            backfill_job=None if _ds == "eur_bund_par" else "proxy_curves",
            history_start=HISTORY_START,
            where=f"source = '{_src}' AND dataset = '{_ds}'",
            description=f"{_label}; daily end-of-day snapshots"
            + ("; only when it served as the EUR fallback" if _ds == "eur_bund_par" else ""),
        )
    )


@dataclass(frozen=True, slots=True)
class DayCurve:
    dataset: str
    day: date
    quotes: tuple[tuple[str, Decimal], ...]
    """``(tenor code, yield % p.a.)`` in curve order."""
    endpoint: str


# ------------------------------------------------------------------------ attempts


def _ust_attempt(d1: date, d2: date) -> Attempt[list[DayCurve]]:
    def fetch(http: Any) -> list[Fetched]:
        months = [
            (y, m)
            for y in range(d1.year, d2.year + 1)
            for m in range(1, 13)
            if (d1.year, d1.month) <= (y, m) <= (d2.year, d2.month)
        ]
        if len(months) <= 3:  # month feeds; longer ranges: one feed per year
            return [
                Fetched(f"par_yield_month_{y:04d}{m:02d}", us_treasury.fetch_month(http, y, m))
                for y, m in months
            ]
        return [
            Fetched(f"par_yield_year_{y}", us_treasury.fetch_year(http, y))
            for y in range(d1.year, d2.year + 1)
        ]

    def parse(fetched: list[Fetched]) -> list[DayCurve]:
        out = []
        for f in fetched:
            for c in us_treasury.parse_xml(f.response.content):
                if d1 <= c.day <= d2:
                    quotes = tuple((us_treasury.TENORS[el][0], v) for el, v in c.quotes)
                    out.append(DayCurve("usd_ust_par", c.day, quotes, f.endpoint))
        return out

    return Attempt("us_treasury", fetch, parse, us_treasury.PARSER_VERSION, "U.S. Treasury")


def _by_day(obs: list[Any], dataset: str, order: tuple[str, ...], endpoint: str) -> list[DayCurve]:
    days: dict[date, dict[str, Decimal]] = {}
    for o in obs:
        days.setdefault(o.day, {})[o.series] = o.value
    return [
        DayCurve(dataset, d, tuple((t, q[t]) for t in order if t in q), endpoint)
        for d, q in sorted(days.items())
    ]


def _eur_attempts(d1: date, d2: date) -> list[Attempt[list[DayCurve]]]:
    return [
        Attempt(
            "ecb_data_portal",
            lambda h: [Fetched("yc_par", ecb_data.fetch(h, ecb_data.yc_flow_key(), d1, d2))],
            lambda f: _by_day(
                ecb_data.yc_from_csv(f[0].response.content),
                "eur_aaa_par",
                ecb_data.YC_PAR_TENORS,
                "yc_par",
            ),
            ecb_data.PARSER_VERSION,
            "ECB AAA euro area curve",
        ),
        Attempt(
            "bundesbank",
            lambda h: [
                Fetched(
                    "bbsis_par",
                    bundesbank.fetch(h, bundesbank.PAR_FLOW, bundesbank.par_key(), d1, d2),
                )
            ],
            lambda f: _by_day(
                bundesbank.par_curve_from_csv(f[0].response.content),
                "eur_bund_par",
                tuple(bundesbank.PAR_TENORS),
                "bbsis_par",
            ),
            bundesbank.PARSER_VERSION,
            "Bundesbank German Federal curve (a different curve: dataset eur_bund_par)",
        ),
    ]


# --------------------------------------------------------------------------- store


def content_hash(curve: DayCurve) -> str:
    body = json.dumps([[t, format(v, "f")] for t, v in curve.quotes], separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _write(ctx: JobContext, served: Served[list[DayCurve]]) -> dict[str, UpsertResult]:
    source = served.source
    with ctx.connection() as conn:
        caps = {
            f.endpoint: record_capture(
                conn,
                ctx.run.id,
                source,
                f.endpoint,
                None,
                f.response,
                served.attempt.parser_version,
            )
            for f in served.fetched
        }
        curves = served.parsed
        snaps = []
        for c in curves:
            _, proxy, label = DATASETS[c.dataset]
            snaps.append(
                {
                    "source": source,
                    "dataset": c.dataset,
                    "quote_basis": "mid_only",
                    "fetched_at": datetime.combine(c.day, EOD_TIME, MSK),
                    "snapshot_date": c.day,
                    "source_ts": None,
                    "market_closed": None,
                    "content_sha256": content_hash(c),
                    "parser_version": served.attempt.parser_version,
                    "label": label,
                    "meta": {
                        "proxy": proxy,
                        "not_ois": True,
                        "curve_date": c.day.isoformat(),
                        "fetched_at": "synthetic_eod",
                    },
                    "capture_id": caps[c.endpoint],
                    "run_id": ctx.run.id,
                }
            )
        res_snaps = upsert_rows(
            conn, "md.curve_snapshot", snaps, ["source", "dataset", "fetched_at"], run_id=ctx.run.id
        )
        ids: dict[tuple[str, date], int] = {}
        if curves:
            with conn.cursor(row_factory=tuple_row) as cur:
                for sid, ds, day in cur.execute(
                    """SELECT id, dataset, snapshot_date FROM md.curve_snapshot
                       WHERE source = %s AND dataset = ANY(%s) AND fetched_at = ANY(%s)""",
                    (source, sorted({c.dataset for c in curves}), [s["fetched_at"] for s in snaps]),
                ):
                    ids[(str(ds), day)] = int(sid)
        quotes = []
        for c in curves:
            for pos, (tenor, value) in enumerate(c.quotes):
                months, days = tenor_parts(tenor)
                quotes.append(
                    {
                        "snapshot_id": ids[(c.dataset, c.day)],
                        "tenor": tenor,
                        "position": pos,
                        "tenor_months": months,
                        "tenor_days": days,
                        "pillar_date": None,
                        "bid_pct": None,
                        "ask_pct": None,
                        "mid_pct": value,
                        "extra": None,
                    }
                )
        res_quotes = upsert_rows(
            conn, "md.curve_quote", quotes, ["snapshot_id", "tenor"], run_id=ctx.run.id
        )
    ctx.run.add_counts(res_snaps)
    ctx.run.add_counts(res_quotes)
    return {"snapshots": res_snaps, "quotes": res_quotes}


def _no_data(
    ctx: JobContext,
    dataset: str,
    source: str,
    d1: date,
    d2: date,
    is_expected: Callable[[date], bool],
) -> list[str]:
    today = ctx.today()
    lo, hi = max(d1, HISTORY_START), min(d2, today - timedelta(days=2))
    out: list[str] = []
    if lo > hi:
        return out
    with ctx.connection() as conn:
        have = {
            r["snapshot_date"]
            for r in conn.execute(
                "SELECT DISTINCT snapshot_date FROM md.curve_snapshot WHERE source = %s "
                "AND dataset = %s AND snapshot_date BETWEEN %s AND %s",
                (source, dataset, lo, hi),
            ).fetchall()
        }
        day = lo
        while day <= hi:
            if is_expected(day) and day not in have:
                record_no_data(
                    conn, f"proxy_{dataset}", day, f"{source}: no curve for {day}", today=today
                )
                out.append(str(day))
            day += timedelta(days=1)
    return out


@job("proxy_curves", "Proxy curves: U.S. Treasury par, ECB AAA par (fallback Bundesbank)")
def proxy_curves(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    curves: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    which = curves or ["usd", "eur"]
    unknown = sorted(set(which) - {"usd", "eur"})
    if unknown:
        raise JobParamError(f"unknown curve(s) {unknown} (usd, eur)")
    today = ctx.today()
    out: dict[str, Any] = {}
    failed: list[str] = []
    for name in which:
        dataset = "usd_ust_par" if name == "usd" else "eur_aaa_par"
        if date_from is None and date_to is None:
            with ctx.connection() as conn:
                empty = (
                    conn.execute(
                        "SELECT 1 FROM md.curve_snapshot WHERE dataset = %s LIMIT 1", (dataset,)
                    ).fetchone()
                    is None
                )
            d1 = HISTORY_START if empty else today - timedelta(days=LOOKBACK_DAYS)
            d2 = today
        else:
            d1, d2 = max(date_from or HISTORY_START, HISTORY_START), date_to or today
        attempts = [_ust_attempt(d1, d2)] if name == "usd" else _eur_attempts(d1, d2)
        chain_sources = [s for s in (sources or []) if s in {a.source for a in attempts}] or None
        try:
            served = first_answer(ctx, f"proxy_curves {name}", attempts, sources=chain_sources)
        except SourcesFailed as exc:
            failed.append(name)
            out[name] = {"error": str(exc), "tried": exc.tried}
            continue
        counts = _write(ctx, served)
        served_ds = served.parsed[0].dataset if served.parsed else dataset
        no_data = (
            _no_data(
                ctx,
                served_ds,
                served.source,
                d1,
                d2,
                (lambda d: d.weekday() < 5)
                if name == "usd"
                else (lambda d: ctx.is_business_day(d, "TARGET")),
            )
            if served_ds != "eur_bund_par"
            else []
        )
        days = sorted({c.day for c in served.parsed})
        out[name] = {
            "source": served.source,
            "dataset": served_ds,
            "tried": served.tried,
            "range": [str(d1), str(d2)],
            "days": len(days),
            "last": str(days[-1]) if days else None,
            "no_data": no_data,
            "snapshots_inserted": counts["snapshots"].inserted,
            "snapshots_updated": counts["snapshots"].updated,
        }
    if failed and len(failed) == len(which):
        raise SourcesFailed("proxy_curves", [t for n in failed for t in out[n]["tried"]])
    if failed:
        ctx.run.mark_partial(f"proxy curves without a source: {', '.join(failed)}")
    return out
