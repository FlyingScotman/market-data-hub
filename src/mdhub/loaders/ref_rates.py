"""International overnight reference rates -> ``md.fixing`` (WEB-16, review P1-2).

``estr_fixing(date_from=None, date_to=None, sources=None)`` (10:20 and 13:20 MSK, all days):
the euro short-term rate **ESTR** (% p.a., by TARGET business day of the rate; the ECB
publishes D at 08:00 CET on D+1). Fallback chain (:mod:`mdhub.loaders._fallback`):

1. ``ecb_data_portal`` - ECB Data Portal ``EST.B.EU000A2X2A25.WT`` (the administrator),
2. ``bundesbank`` - Bundesbank ``BBMMB/D.EU000A2X2A25.WT`` (republication),
3. ``dbnomics`` - DBnomics ``ECB/EST/B.EU000A2X2A25.WT`` (mirror, may lag),
4. ``fred`` - FRED ``ECBESTRVOLWGTTRMDMNRT`` (mirror).

``sofr_fixing(...)`` (16:30 and 22:30 MSK, all days): **SOFR** (% p.a., NY Fed, by U.S.
government securities business day; D is published ~08:00 New York time on D+1, revisions by
14:30) plus the NY Fed averages **SOFR30DAVG**, **SOFR90DAVG**, **SOFR180DAVG** (% p.a.) and
the **SOFRINDEX** (``unit = index``, 2018-04-02 = 1). Chain: ``nyfed`` (administrator), then
``fred`` (``SOFR``, ``SOFR30DAYAVG``, ``SOFR90DAYAVG``, ``SOFR180DAYAVG``, ``SOFRINDEX``).

Rows: ``fixing_date`` = the rate's business day, ``source`` = the source that served the run
(a mirror's rows are a separate series of the same ``index_code``: a consumer that needs one
value per day takes, per day, the first present source of the chain order above - the values
are republications of the administrator's figure), ``details`` = the source's series id, the
administrator and extras (SOFR percentiles and volume, a revision flag). ``published_on`` stays
NULL (no source states it); ``hub_ingested_at`` in the snapshot / history is the hub's own
first-seen time.

Without dates a run reads the last :data:`LOOKBACK_DAYS` calendar days (late publications and
revisions); an empty series is seeded from its history start. A business day before yesterday
for which the answering source has no value and no source has a row is recorded with
:func:`mdhub.core.gaps.record_no_data` (backfill contract). All sources failing fails the run.

``saron_fixing(...)`` (19:20 and 09:20 MSK, all days): **SARON** (Swiss Average Rate
Overnight, % p.a.; SIX administers it and fixes it at the close of each Zurich business day, the
SNB republishes it) from the SNB data portal cube ``snbgwdzid`` (``snb``,
:mod:`mdhub.sources.snb`). One source: SIX publishes its own figures only to licensees. The SNB
host does not answer from the development environment - the parser follows the portal's
documented CSV export; check it with ``mdhub probe-sources`` on the server.

Datasets (any source counts): ``estr`` (calendar TARGET, from 2019-10-01), ``sofr`` (Monday to
Friday - U.S. holidays become ``unfillable`` once the source confirms no data - from
2018-04-02), ``sofr_averages`` (from 2020-03-02, the first NY Fed publication), ``saron``
(calendar CH_ZURICH, from :data:`SARON_HISTORY_START`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.gaps import record_no_data
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders._fallback import Attempt, Fetched, Served, first_answer
from mdhub.loaders.cbr import FIXING_KEY, fixing_row
from mdhub.sources import bundesbank, dbnomics, ecb_data, fred, nyfed, snb

__all__ = [
    "ESTR_ATTEMPTS",
    "ESTR_HISTORY_START",
    "LOOKBACK_DAYS",
    "SARON_ATTEMPTS",
    "SARON_CALENDAR",
    "SARON_HISTORY_START",
    "SOFR_ATTEMPTS",
    "SOFR_AVERAGES_START",
    "SOFR_HISTORY_START",
    "estr_fixing",
    "saron_fixing",
    "sofr_fixing",
]

LOOKBACK_DAYS: Final = 10
ESTR_HISTORY_START: Final = date(2019, 10, 1)
SOFR_HISTORY_START: Final = date(2018, 4, 2)
SOFR_AVERAGES_START: Final = date(2020, 3, 2)
SOFR_AVERAGE_CODES: Final = ("SOFR30DAVG", "SOFR90DAVG", "SOFR180DAVG", "SOFRINDEX")
SARON_HISTORY_START: Final = date(2019, 1, 3)
"""First SARON day the hub seeds (the desk's history starts 2019; SARON itself since 2009)."""
SARON_CALENDAR: Final = "CH_ZURICH"

register_dataset(
    DatasetSpec(
        name="estr",
        table="md.fixing",
        date_column="fixing_date",
        calendar="TARGET",
        frequency="business_daily",
        max_lag_days=2,  # D is published at 08:00 CET on D+1; the 08:30 MSK quality run is before
        backfill_job="estr_fixing",
        history_start=ESTR_HISTORY_START,
        where="index_code = 'ESTR'",
        description="Euro short-term rate (% p.a.), TARGET days; any source of the chain "
        "(ECB Data Portal, Bundesbank, DBnomics, FRED)",
    )
)
register_dataset(
    DatasetSpec(
        name="sofr",
        table="md.fixing",
        date_column="fixing_date",
        calendar=None,
        frequency="business_daily",
        max_lag_days=2,
        backfill_job="sofr_fixing",
        history_start=SOFR_HISTORY_START,
        where="index_code = 'SOFR'",
        description="SOFR (% p.a.), Monday-Friday (U.S. holidays confirmed empty -> "
        "unfillable); NY Fed, fallback FRED",
    )
)
register_dataset(
    DatasetSpec(
        name="sofr_averages",
        table="md.fixing",
        date_column="fixing_date",
        calendar=None,
        frequency="business_daily",
        max_lag_days=2,
        backfill_job="sofr_fixing",
        history_start=SOFR_AVERAGES_START,
        min_rows_per_day=4,
        where="index_code IN ('SOFR30DAVG', 'SOFR90DAVG', 'SOFR180DAVG', 'SOFRINDEX')",
        description="SOFR 30/90/180-day averages (% p.a.) and SOFR index; NY Fed, fallback FRED",
    )
)


register_dataset(
    DatasetSpec(
        name="saron",
        table="md.fixing",
        date_column="fixing_date",
        calendar=SARON_CALENDAR,
        frequency="business_daily",
        max_lag_days=2,
        backfill_job="saron_fixing",
        history_start=SARON_HISTORY_START,
        where="index_code = 'SARON'",
        description="SARON (% p.a.), Zurich business days; SNB data portal (republishes SIX)",
    )
)


@dataclass(frozen=True, slots=True)
class Rate:
    index_code: str
    day: date
    value: Decimal
    unit: str = "pct"
    details: dict[str, str] = field(default_factory=dict)
    endpoint: str | None = None
    """Raw-capture endpoint the value came from (default: the attempt's first response)."""


# ---------------------------------------------------------------------------- €STR


def _estr_attempts(d1: date, d2: date) -> list[Attempt[list[Rate]]]:
    def rates(obs: list[Any], series: str, administrator: str = "ecb") -> list[Rate]:
        return [
            Rate("ESTR", o.day, o.value, details={"series": series, "administrator": administrator})
            for o in obs
            if d1 <= o.day <= d2
        ]

    def one(fetched: list[Fetched]) -> bytes:
        return fetched[0].response.content

    return [
        Attempt(
            "ecb_data_portal",
            lambda h: [Fetched("est", ecb_data.fetch(h, ecb_data.ESTR_FLOW_KEY, d1, d2))],
            lambda f: rates(ecb_data.estr_from_csv(one(f)), ecb_data.ESTR_SERIES),
            ecb_data.PARSER_VERSION,
            "ECB Data Portal, administrator",
        ),
        Attempt(
            "bundesbank",
            lambda h: [
                Fetched(
                    "bbmmb_estr",
                    bundesbank.fetch(h, bundesbank.ESTR_FLOW, bundesbank.ESTR_KEY, d1, d2),
                )
            ],
            lambda f: rates(
                bundesbank.estr_from_csv(one(f)), f"{bundesbank.ESTR_FLOW}.{bundesbank.ESTR_KEY}"
            ),
            bundesbank.PARSER_VERSION,
            "Bundesbank republication",
        ),
        Attempt(
            "dbnomics",
            lambda h: [Fetched("ecb_est", dbnomics.fetch_estr(h))],
            lambda f: rates(dbnomics.estr_from_json(one(f)), "ECB/EST/B.EU000A2X2A25.WT"),
            dbnomics.PARSER_VERSION,
            "DBnomics mirror",
        ),
        Attempt(
            "fred",
            lambda h: [Fetched("fredgraph_estr", fred.fetch(h, (fred.ESTR_SERIES,), d1, d2))],
            lambda f: rates(fred.parse_csv(one(f), (fred.ESTR_SERIES,)), fred.ESTR_SERIES),
            fred.PARSER_VERSION,
            "FRED mirror",
        ),
    ]


ESTR_ATTEMPTS: Final = ("ecb_data_portal", "bundesbank", "dbnomics", "fred")


# ---------------------------------------------------------------------------- SOFR


def _sofr_attempts(d1: date, d2: date) -> list[Attempt[list[Rate]]]:
    def nyfed_parse(fetched: list[Fetched]) -> list[Rate]:
        parsers = {"sofr": nyfed.parse_sofr, "sofrai": nyfed.parse_sofrai}
        out = [
            Rate(
                r.index_code,
                r.day,
                r.value,
                r.unit,
                {"administrator": "nyfed", **r.details},
                endpoint=f.endpoint,
            )
            for f in fetched
            for r in parsers[f.endpoint](f.response.content)
        ]
        return [r for r in out if d1 <= r.day <= d2]

    def nyfed_fetch(h: Any) -> list[Fetched]:
        out = [Fetched("sofr", nyfed.fetch(h, "sofr", d1, d2))]
        if d2 >= SOFR_AVERAGES_START:  # the averages start later than SOFR
            out.append(
                Fetched("sofrai", nyfed.fetch(h, "sofrai", max(d1, SOFR_AVERAGES_START), d2))
            )
        return out

    fred_series = (fred.SOFR_SERIES, *fred.SOFR_AVERAGES)

    def fred_parse(fetched: list[Fetched]) -> list[Rate]:
        out: list[Rate] = []
        for o in fred.parse_csv(fetched[0].response.content, fred_series):
            code, unit = fred.SOFR_AVERAGES.get(o.series, ("SOFR", "pct"))
            if not d1 <= o.day <= d2:
                continue
            out.append(
                Rate(code, o.day, o.value, unit, {"series": o.series, "administrator": "nyfed"})
            )
        return out

    return [
        Attempt(
            "nyfed",
            nyfed_fetch,
            nyfed_parse,
            nyfed.PARSER_VERSION,
            "NY Fed, administrator",
        ),
        Attempt(
            "fred",
            lambda h: [Fetched("fredgraph_sofr", fred.fetch(h, fred_series, d1, d2))],
            fred_parse,
            fred.PARSER_VERSION,
            "FRED mirror",
        ),
    ]


SOFR_ATTEMPTS: Final = ("nyfed", "fred")


# ---------------------------------------------------------------------------- SARON


def _saron_attempts(d1: date, d2: date) -> list[Attempt[list[Rate]]]:
    def parse(fetched: list[Fetched]) -> list[Rate]:
        return [
            Rate(
                "SARON",
                o.day,
                o.value,
                details={"series": f"{snb.CUBE}/{snb.SARON_SERIES}", "administrator": "six"},
            )
            for o in snb.parse_csv(fetched[0].response.content)
            if d1 <= o.day <= d2
        ]

    return [
        Attempt(
            "snb",
            lambda h: [Fetched("snbgwdzid_saron", snb.fetch(h, d1, d2))],
            parse,
            snb.PARSER_VERSION,
            "SNB data portal, republishes the SIX fixing",
        )
    ]


SARON_ATTEMPTS: Final = ("snb",)


# ----------------------------------------------------------------------------- job


def _range(
    ctx: JobContext, codes: tuple[str, ...], start: date, d1: date | None, d2: date | None
) -> tuple[date, date, bool]:
    today = ctx.today()
    explicit = d1 is not None or d2 is not None
    if not explicit:
        with ctx.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM md.fixing WHERE index_code = ANY(%s) LIMIT 1", (list(codes),)
            ).fetchone()
        if row is None:
            return start, today, True
        return today - timedelta(days=LOOKBACK_DAYS), today, False
    lo, hi = max(d1 or start, start), d2 or today
    if lo > hi:
        raise ValueError(f"date_from {lo} after date_to {hi}")
    return lo, hi, True


def _store(
    ctx: JobContext,
    served: Served[list[Rate]],
    *,
    datasets: dict[str, tuple[tuple[str, ...], date]],
    is_expected: Callable[[date], bool],
    d1: date,
    d2: date,
) -> dict[str, Any]:
    known_at = utc_now()
    rows: list[dict[str, Any]] = []
    with ctx.connection() as conn:
        caps = {
            f.endpoint: record_capture(
                conn,
                ctx.run.id,
                served.source,
                f.endpoint,
                None,
                f.response,
                served.attempt.parser_version,
            )
            for f in served.fetched
        }
        cap = next(iter(caps.values()))
        for r in served.parsed:
            rows.append(
                fixing_row(
                    r.index_code,
                    r.day,
                    r.value,
                    capture_id=caps.get(r.endpoint or "", cap),
                    run_id=ctx.run.id,
                    known_at=known_at,
                    unit=r.unit,
                    details=r.details or None,
                    source=served.source,
                )
            )
        result = upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)
        # backfill contract: a past expected day without any value -> source-confirmed no data
        today = ctx.today()
        no_data: list[str] = []
        for name, (codes, start) in datasets.items():
            lo = max(d1, start)
            hi = min(d2, today - timedelta(days=2))
            if lo > hi:
                continue
            have = {
                r["fixing_date"]
                for r in conn.execute(
                    "SELECT DISTINCT fixing_date FROM md.fixing WHERE index_code = ANY(%s) "
                    "AND fixing_date BETWEEN %s AND %s",
                    (list(codes), lo, hi),
                ).fetchall()
            }
            day = lo
            while day <= hi:
                if is_expected(day) and day not in have:
                    record_no_data(
                        conn, name, day, f"{served.source}: no value for {day}", today=today
                    )
                    no_data.append(f"{name}:{day}")
                day += timedelta(days=1)
    ctx.run.add_counts(result)
    days = sorted({r.day for r in served.parsed})
    return {
        "source": served.source,
        "tried": served.tried,
        "range": [str(d1), str(d2)],
        "rows": len(rows),
        "first": str(days[0]) if days else None,
        "last": str(days[-1]) if days else None,
        "no_data": no_data,
        "inserted": result.inserted,
        "updated": result.updated,
    }


@job("estr_fixing", "Euro short-term rate (ECB; fallbacks Bundesbank, DBnomics, FRED) -> md.fixing")
def estr_fixing(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    d1, d2, _ = _range(ctx, ("ESTR",), ESTR_HISTORY_START, date_from, date_to)
    served = first_answer(ctx, "estr_fixing", _estr_attempts(d1, d2), sources=sources)
    return _store(
        ctx,
        served,
        datasets={"estr": (("ESTR",), ESTR_HISTORY_START)},
        is_expected=lambda d: ctx.is_business_day(d, "TARGET"),
        d1=d1,
        d2=d2,
    )


@job("sofr_fixing", "SOFR, SOFR averages and index (NY Fed; fallback FRED) -> md.fixing")
def sofr_fixing(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    d1, d2, _ = _range(ctx, ("SOFR",), SOFR_HISTORY_START, date_from, date_to)
    served = first_answer(ctx, "sofr_fixing", _sofr_attempts(d1, d2), sources=sources)
    return _store(
        ctx,
        served,
        datasets={
            "sofr": (("SOFR",), SOFR_HISTORY_START),
            "sofr_averages": (SOFR_AVERAGE_CODES, SOFR_AVERAGES_START),
        },
        is_expected=lambda d: d.weekday() < 5,
        d1=d1,
        d2=d2,
    )


@job("saron_fixing", "SARON (SNB data portal, republishes the SIX fixing) -> md.fixing")
def saron_fixing(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    d1, d2, _ = _range(ctx, ("SARON",), SARON_HISTORY_START, date_from, date_to)
    served = first_answer(ctx, "saron_fixing", _saron_attempts(d1, d2), sources=sources)
    return _store(
        ctx,
        served,
        datasets={"saron": (("SARON",), SARON_HISTORY_START)},
        is_expected=lambda d: ctx.is_business_day(d, SARON_CALENDAR),
        d1=d1,
        d2=d2,
    )
