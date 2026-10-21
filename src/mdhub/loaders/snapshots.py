"""Published snapshot jobs (Market Snapshot Contract v1; DESIGN §11).

Jobs (names fixed by :data:`mdhub.schedule.KNOWN_JOBS`; cut times from settings, DEC04):

* ``snapshot_static`` - daily static snapshot (``mode=daily``, after ``bonds_static`` and
  ``cbonds_enrich``; ``MDHUB_SNAPSHOT_STATIC_TIME``). Static changes during the day are
  published by the next market cut (cut type ``change``).
* ``snapshot_market`` - market cuts: ``mode=eod_preliminary`` (business date D, evening),
  ``eod_final`` (next morning, D = previous business day), ``intraday`` (every
  ``MDHUB_SNAPSHOT_INTRADAY_MINUTES`` inside ``MDHUB_SNAPSHOT_INTRADAY_WINDOW``).

Locks (review §6.5): :func:`market_lock` - intraday cuts, scheduled EOD cuts and rebuilds /
replays (explicit ``day`` or ``known_at``) hold separate locks, so a manual history rebuild never
skips a scheduled cut. A scheduled intraday cut that still cannot run opens an ``ops.data_gap``
(:data:`mdhub.schedule.INTRADAY_CUT_GAP_DATASET`, key ``cut``) that the publication of that cut
(a replay with ``day``, ``cut`` and ``known_at``) closes.

* ``snapshot_rebuild`` (not scheduled; the backfill job of dataset ``snapshot_eod_final``) -
  rebuilds the ``eod_final`` cuts of ``date_from..date_to`` that are already published, as
  known now: unchanged content publishes nothing, different content publishes a correction
  (``supersedes``). Queued by the backfill worker after a backfill run that changed inputs of
  such a day (``MDHUB_SNAPSHOT_AUTOCORRECT``, :mod:`mdhub.snapshot.readiness`).

Parameters: ``day`` (explicit business date), ``days_back`` (business-day offsets, defaults
per mode), ``cut`` (nominal cut time ``HH:MM`` MSK on the business date, ``eod_final`` on the
next business day; default: the configured time of the cut type, ``intraday`` / ``change``:
the start minute of the run), ``known_at`` (replay: build as known at that instant; default:
now), ``wait_minutes`` (``eod_final`` only: how long to wait for the final ``bonds_eod`` /
``zcyc`` runs of the day; default ``MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES``; no wait for a
replay with ``known_at``). A rebuild of a cut with different content publishes a correction
that supersedes the earlier snapshot.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from typing import Any, Final

from mdhub.config import parse_hhmm
from mdhub.core.gaps import close_gap
from mdhub.core.timeutil import MSK, add_business_days, utc_now
from mdhub.core.upsert import UpsertResult
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.loaders.bonds_eod import resolve_trade_dates
from mdhub.schedule import INTRADAY_CUT_GAP_DATASET
from mdhub.snapshot.market import MarketParams
from mdhub.snapshot.readiness import (
    EOD_FINAL_DATASET,
    InputRule,
    eod_final_cuts,
    missing_eod_final_inputs,
)
from mdhub.snapshot.runner import publish_market_snapshot, publish_static_snapshot
from mdhub.snapshot.static import BOND_CALENDAR

__all__ = [
    "EOD_FINAL_DATASET",
    "market_lock",
    "market_params",
    "snapshot_market",
    "snapshot_rebuild",
    "snapshot_static",
]

MARKET_MODES: Final = {"eod_preliminary": [0], "eod_final": [1], "intraday": [0]}
INPUT_POLL_S: Final = 30.0
"""Re-check interval while ``eod_final`` waits for its inputs."""

register_dataset(
    DatasetSpec(
        name=EOD_FINAL_DATASET,
        table="ops.published_snapshot",
        date_column="business_date",
        calendar=BOND_CALENDAR,
        # freshness only: a missing eod_final is not built automatically (owner decision, HR5)
        frequency="event",
        max_lag_days=4,
        backfill_job="snapshot_rebuild",
        where="kind = 'market' AND cut_type = 'eod_final'",
        # one rebuild (static + market build, schema validation) takes up to ~1 minute
        max_task_days=5,
        description="Published eod_final market cuts; backfill = rebuild of published cuts",
    )
)

# indirection for tests (no real sleeping)
_sleep: Callable[[float], None] = _time.sleep
_clock: Callable[[], float] = _time.monotonic


def _known_at(known_at: datetime | None) -> datetime:
    """The build instant: ``known_at`` (offset required) or now."""
    if known_at is not None and known_at.utcoffset() is None:
        raise JobParamError("known_at must include a UTC offset")
    return known_at or utc_now()


def _cut_ts(
    ctx: JobContext, cut_type: str, business_date: date, cut: str | None, known_at: datetime
) -> datetime:
    """Nominal cut: ``cut`` (default: the configured time of ``daily`` / ``eod_preliminary`` /
    ``eod_final``) MSK on the cut's day (``business_date``; ``eod_final``: the next business
    day), so a rebuild on any later day hits the same cut. ``intraday`` / ``change`` without
    ``cut``: the start minute of the run (several cuts per day)."""
    s = ctx.settings
    hhmm = cut or {
        "daily": s.snapshot_static_time,
        "eod_preliminary": s.snapshot_eod_preliminary_time,
        "eod_final": s.snapshot_eod_final_time,
    }.get(cut_type)
    if hhmm is None:
        return known_at.astimezone(MSK).replace(second=0, microsecond=0)
    try:
        h, m = parse_hhmm(hhmm)
    except ValueError as exc:
        raise JobParamError(str(exc)) from None
    day = business_date
    if cut_type == "eod_final":
        day = add_business_days(day, 1, lambda d: ctx.calendar(d, BOND_CALENDAR))
    return datetime.combine(day, time(h, m), tzinfo=MSK)


def _wait_for_inputs(ctx: JobContext, day: date, wait_minutes: int) -> list[InputRule]:
    """Wait up to ``wait_minutes`` until the final inputs of ``day`` are loaded; returns the
    inputs still missing (:mod:`mdhub.snapshot.readiness`)."""

    def is_bday(d: date) -> bool:
        return ctx.calendar(d, BOND_CALENDAR)

    deadline = _clock() + wait_minutes * 60
    while True:
        with ctx.connection() as conn:
            missing = missing_eod_final_inputs(conn, day, is_bday)
        remaining = deadline - _clock()
        if not missing or remaining <= 0:
            return missing
        ctx.logger.info(
            "eod_final %s: waiting for %s (%.0f s left)",
            day,
            ", ".join(m.job for m in missing),
            remaining,
        )
        _sleep(min(INPUT_POLL_S, remaining))


def market_params(ctx: JobContext) -> MarketParams:
    """Builder parameters from the settings (prelim bond threshold, curve EOD cutoff)."""
    return MarketParams(
        prelim_bond_min_rows=ctx.settings.bonds_eod_prelim_min_rows,
        curve_eod_cutoff=ctx.settings.curve_eod_cutoff,
    )


def _result(res: Any) -> dict[str, Any]:
    return {
        "id": res.id,
        "status": res.status,
        "kind": res.kind,
        "cut_type": res.cut_type,
        "business_date": res.business_date.isoformat(),
        "supersedes": res.supersedes,
        "size_bytes": res.size_bytes,
        "gzip_bytes": res.gzip_bytes,
        "excluded": res.excluded,
    }


@job("snapshot_static", "Publish the static snapshot (instruments, schedules, calendars)")
def snapshot_static(
    ctx: JobContext,
    *,
    mode: str = "daily",
    day: date | None = None,
    cut: str | None = None,
    known_at: datetime | None = None,
) -> dict[str, Any]:
    """Publish the static snapshot of ``day`` (default: today, MSK)."""
    if mode not in ("daily", "change"):
        raise JobParamError(f"unknown snapshot_static mode {mode!r} (daily, change)")
    ka = _known_at(known_at)
    business_date = day or ka.astimezone(MSK).date()
    cut_ts = _cut_ts(ctx, mode, business_date, cut, ka)
    res = publish_static_snapshot(
        ctx.db,
        business_date=business_date,
        cut_ts=cut_ts,
        known_at=ka,
        cut_type=mode,
        run_id=ctx.run.id,
    )
    ctx.logger.info("static snapshot %s: %s", res.id, res.status)
    ctx.run.add_counts(
        UpsertResult(inserted=1) if res.status == "published" else UpsertResult(unchanged=1)
    )
    return _result(res)


def market_lock(params: Mapping[str, Any]) -> str:
    """Lock class of a ``snapshot_market`` run (review §6.5: intraday cuts were skipped with
    ``lock_busy`` for 2.5 hours while ~170 manual ``eod_final`` rebuilds held the job's single
    lock): ``snapshot_market:intraday`` (scheduled intraday cuts), ``snapshot_market:eod``
    (scheduled EOD cuts: ``days_back``) and ``snapshot_market:rebuild`` (explicit ``day`` or
    ``known_at``: manual rebuilds and replays). Classes run side by side - builds only read and
    publishing is serialized by the publish lock - and runs of one class stay exclusive."""
    if params.get("day") is not None or params.get("known_at") is not None:
        return "snapshot_market:rebuild"
    mode = str(params.get("mode") or "eod_preliminary")
    return "snapshot_market:intraday" if mode == "intraday" else "snapshot_market:eod"


@job(
    "snapshot_market",
    "Publish a market snapshot cut (EOD preliminary / final, intraday)",
    lock_key=market_lock,
)
def snapshot_market(
    ctx: JobContext,
    *,
    mode: str = "eod_preliminary",
    days_back: list[int] | None = None,
    day: date | None = None,
    cut: str | None = None,
    known_at: datetime | None = None,
    wait_minutes: int | None = None,
) -> dict[str, Any]:
    """Publish the market cut ``mode`` for business date ``day`` (or ``days_back``)."""
    if mode not in MARKET_MODES:
        raise JobParamError(f"unknown snapshot_market mode {mode!r} ({', '.join(MARKET_MODES)})")
    if wait_minutes is not None and wait_minutes < 0:
        raise JobParamError("wait_minutes must be >= 0")
    ka = _known_at(known_at)
    if day is None:
        today = ka.astimezone(MSK).date() if known_at is not None else ctx.today()
        day = ctx.business_dates(
            days_back if days_back is not None else MARKET_MODES[mode],
            today=today,
            calendar=BOND_CALENDAR,
        )[0]
    missing: list[InputRule] = []
    waited = 0
    run_day = ka.astimezone(MSK).date() if known_at is not None else ctx.today()
    live = day >= ctx.business_dates([1], today=run_day, calendar=BOND_CALENDAR)[0]
    if mode == "eod_final" and live:
        # only the regular cut of the previous business day waits / is checked: an older day
        # is a rebuild whose inputs may predate the job log (legacy imports)
        if known_at is None:
            waited = (
                ctx.settings.snapshot_eod_final_wait_minutes
                if wait_minutes is None
                else wait_minutes
            )
            missing = _wait_for_inputs(ctx, day, waited)
            ka = utc_now()  # build as known after the wait
        else:
            with ctx.connection() as conn:
                missing = missing_eod_final_inputs(
                    conn, day, lambda d: ctx.calendar(d, BOND_CALENDAR), until=ka
                )
    extra_stale = {
        m.dataset: [
            f"input not ready: no successful final {m.job} run for {day.isoformat()} finished "
            f"by known_at (waited {waited} min)"
        ]
        for m in missing
    }
    cut_ts = _cut_ts(ctx, mode, day, cut, ka)
    out = publish_market_snapshot(
        ctx.db,
        cut_type=mode,
        business_date=day,
        cut_ts=cut_ts,
        known_at=ka,
        run_id=ctx.run.id,
        params=market_params(ctx),
        extra_stale=extra_stale,
    )
    if missing:
        ctx.run.mark_partial(
            f"eod_final {day} published without final inputs: " + ", ".join(m.job for m in missing)
        )
    if mode == "intraday":
        # a scheduled cut that did not run left a gap (mdhub.schedule.Rule.missed_gap); the
        # cut published now - scheduled or a replay with cut= / known_at= - closes it
        with ctx.connection() as conn:
            close_gap(
                conn,
                INTRADAY_CUT_GAP_DATASET,
                day,
                key={"cut": cut_ts.astimezone(MSK).strftime("%H:%M")},
                note=f"cut published: snapshot {out.market.id} ({out.market.status})",
            )
    ctx.logger.info(
        "market snapshot %s %s: %s %s (static %s: %s)",
        mode,
        day,
        out.market.id,
        out.market.status,
        out.static.id,
        out.static.status,
    )
    published = int(out.market.status == "published") + int(out.static.status == "published")
    ctx.run.add_counts(UpsertResult(inserted=published, unchanged=2 - published))
    return {
        "market": _result(out.market),
        "static": _result(out.static),
        "inputs_missing": [m.job for m in missing],
    }


@job("snapshot_rebuild", "Rebuild published eod_final cuts of a date range (corrections)")
def snapshot_rebuild(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    """Rebuild, as known now, every published ``eod_final`` cut of the business dates in the
    range; a cut whose content is unchanged publishes nothing, a changed one publishes a
    correction that supersedes it. Days without a published ``eod_final`` are not built (a
    missing cut is an owner decision, ``mdhub run snapshot_market -p mode=eod_final -p day=``)."""
    dates = resolve_trade_dates(
        ctx,
        days_back=None,
        default_days_back=[1],
        day=day,
        date_from=date_from,
        date_to=date_to,
        calendar=BOND_CALENDAR,
    )
    if not dates:
        ctx.run.mark_skipped("no business dates in the range")
        return {}
    with ctx.connection() as conn:
        cuts = eod_final_cuts(conn, min(dates), max(dates))
    wanted = set(dates)
    results: dict[str, Any] = {
        d.isoformat(): {"status": "no_eod_final"} for d in dates if d not in {c[0] for c in cuts}
    }
    corrected = unchanged = 0
    for d, cut_ts in cuts:
        if d not in wanted:
            continue
        ka = utc_now()
        out = publish_market_snapshot(
            ctx.db,
            cut_type="eod_final",
            business_date=d,
            cut_ts=cut_ts,  # the published cut: a changed build supersedes it
            known_at=ka,
            run_id=ctx.run.id,
            params=market_params(ctx),
        )
        results[f"{d.isoformat()} {cut_ts.isoformat()}"] = {
            "market": _result(out.market),
            "static": _result(out.static),
        }
        if out.market.status == "published":
            corrected += 1
        else:
            unchanged += 1
        ctx.logger.info("eod_final %s rebuilt: %s %s", d, out.market.id, out.market.status)
    ctx.run.add_counts(UpsertResult(inserted=corrected, unchanged=unchanged))
    return results
