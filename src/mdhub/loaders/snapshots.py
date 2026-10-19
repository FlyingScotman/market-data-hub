"""Published snapshot jobs (Market Snapshot Contract v1; DESIGN §11).

Jobs (names fixed by :data:`mdhub.schedule.KNOWN_JOBS`; cut times from settings, DEC04):

* ``snapshot_static`` - daily static snapshot (``mode=daily``, after ``bonds_static`` and
  ``cbonds_enrich``; ``MDHUB_SNAPSHOT_STATIC_TIME``). Static changes during the day are
  published by the next market cut (cut type ``change``).
* ``snapshot_market`` - market cuts: ``mode=eod_preliminary`` (business date D, evening),
  ``eod_final`` (next morning, D = previous business day), ``intraday`` (every
  ``MDHUB_SNAPSHOT_INTRADAY_MINUTES`` inside ``MDHUB_SNAPSHOT_INTRADAY_WINDOW``).

Parameters: ``day`` (explicit business date), ``days_back`` (business-day offsets, defaults
per mode), ``cut`` (nominal cut time ``HH:MM`` MSK on the business date, ``eod_final`` on the
next business day; default: the configured time of the cut type, ``intraday`` / ``change``:
the start minute of the run), ``known_at`` (replay: build as known at that instant; default:
now). A rebuild of a cut with different content publishes a correction that supersedes the
earlier snapshot.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Final

from mdhub.config import parse_hhmm
from mdhub.core.timeutil import MSK, add_business_days, utc_now
from mdhub.core.upsert import UpsertResult
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.snapshot.runner import publish_market_snapshot, publish_static_snapshot
from mdhub.snapshot.static import BOND_CALENDAR

__all__ = ["snapshot_market", "snapshot_static"]

MARKET_MODES: Final = {"eod_preliminary": [0], "eod_final": [1], "intraday": [0]}


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


@job("snapshot_market", "Publish a market snapshot cut (EOD preliminary / final, intraday)")
def snapshot_market(
    ctx: JobContext,
    *,
    mode: str = "eod_preliminary",
    days_back: list[int] | None = None,
    day: date | None = None,
    cut: str | None = None,
    known_at: datetime | None = None,
) -> dict[str, Any]:
    """Publish the market cut ``mode`` for business date ``day`` (or ``days_back``)."""
    if mode not in MARKET_MODES:
        raise JobParamError(f"unknown snapshot_market mode {mode!r} ({', '.join(MARKET_MODES)})")
    ka = _known_at(known_at)
    if day is None:
        today = ka.astimezone(MSK).date() if known_at is not None else ctx.today()
        day = ctx.business_dates(
            days_back if days_back is not None else MARKET_MODES[mode],
            today=today,
            calendar=BOND_CALENDAR,
        )[0]
    cut_ts = _cut_ts(ctx, mode, day, cut, ka)
    out = publish_market_snapshot(
        ctx.db,
        cut_type=mode,
        business_date=day,
        cut_ts=cut_ts,
        known_at=ka,
        run_id=ctx.run.id,
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
    return {"market": _result(out.market), "static": _result(out.static)}
