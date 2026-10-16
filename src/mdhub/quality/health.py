"""Job health: last runs per job compared with the schedule (:data:`mdhub.schedule.RULES`).

For every job in :data:`~mdhub.schedule.KNOWN_JOBS` (plus any other registered or recorded
job) the check computes the **last expected fire time** - the latest cron fire of any of its
rules that passes the rule's day filter, at least ``grace`` before now - and classifies:

* ``not_registered`` - scheduled but no module registers the job (loader not deployed),
* ``never_run`` - registered, nothing expected yet and no run recorded,
* ``missed`` - a fire was expected but no run started since (scheduler down / overlapping);
  a run recorded as ``skipped`` with ``lock_busy`` (the job lock stayed busy for the whole
  wait) does not count as started - :attr:`JobHealth.missed_by_lock` is then set and the
  ``quality`` job re-runs the missed fire (:func:`catch_up_params`),
* ``failing`` - the last ``failing_threshold`` or more runs since the last success failed,
* ``degraded`` - the last run failed, fewer than ``failing_threshold`` times in a row,
* ``ok`` - otherwise; ``manual`` - not scheduled (e.g. ad-hoc jobs) and not failing.

``missed`` and ``failing`` are problems (counted in the quality report).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import psycopg
from apscheduler.triggers.cron import CronTrigger
from psycopg.rows import tuple_row

from mdhub.core.bizday import CalendarPredicate
from mdhub.core.timeutil import business_days_back
from mdhub.jobs import LOCK_BUSY, REGISTRY, JobRegistry
from mdhub.schedule import KNOWN_JOBS, RULES, Rule, rule_applies

__all__ = [
    "PROBLEM_STATUSES",
    "JobHealth",
    "catch_up_params",
    "job_health",
    "last_expected",
    "last_expected_fire",
]

PROBLEM_STATUSES: Final = frozenset({"missed", "failing"})
_SCAN_DAYS: Final = 8
_START_TOLERANCE: Final = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class JobHealth:
    job: str
    status: str
    registered: bool
    scheduled: bool
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_expected_at: datetime | None = None
    last_error: str | None = None
    missed_by_lock: bool = False
    """The expected fire was attempted but skipped because the job lock stayed busy."""
    expected_rule: Rule | None = None
    """The rule of :attr:`last_expected_at` (for catch-up)."""

    @property
    def problem(self) -> bool:
        return self.status in PROBLEM_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "status": self.status,
            "registered": self.registered,
            "scheduled": self.scheduled,
            "last_status": self.last_status,
            "last_started_at": _iso(self.last_started_at),
            "last_success_at": _iso(self.last_success_at),
            "consecutive_failures": self.consecutive_failures,
            "last_expected_at": _iso(self.last_expected_at),
            "last_error": (self.last_error or "")[:300] or None,
            "missed_by_lock": self.missed_by_lock,
        }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _last_fire_on(
    trigger: CronTrigger, day: date, cutoff: datetime, tz: ZoneInfo
) -> datetime | None:
    day_start = datetime.combine(day, time(0), tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    last: datetime | None = None
    t: datetime | None = trigger.get_next_fire_time(None, day_start)
    while t is not None and t < day_end and t <= cutoff:
        last = t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return last


def last_expected(
    rules: Iterable[Rule], cutoff: datetime, calendar: CalendarPredicate, tz: ZoneInfo
) -> tuple[datetime, Rule] | None:
    """Latest fire time ``<= cutoff`` of any rule whose day filter applies (searching back
    :data:`_SCAN_DAYS` days), with its rule."""
    best: tuple[datetime, Rule] | None = None
    cutoff = cutoff.astimezone(tz)
    for rule in rules:
        trigger = CronTrigger.from_crontab(rule.cron, timezone=tz)
        for back in range(_SCAN_DAYS):
            day = cutoff.date() - timedelta(days=back)
            if (
                best is not None
                and datetime.combine(day + timedelta(days=1), time(0), tz) <= best[0]
            ):
                break
            if not rule_applies(rule, day, calendar):
                continue
            fire = _last_fire_on(trigger, day, cutoff, tz)
            if fire is not None:
                if best is None or fire > best[0]:
                    best = (fire, rule)
                break
    return best


def last_expected_fire(
    rules: Iterable[Rule], cutoff: datetime, calendar: CalendarPredicate, tz: ZoneInfo
) -> datetime | None:
    """Latest fire time ``<= cutoff`` of any rule whose day filter applies (searching back
    :data:`_SCAN_DAYS` days)."""
    found = last_expected(rules, cutoff, calendar, tz)
    return found[0] if found else None


def catch_up_params(
    rule: Rule,
    fire: datetime,
    *,
    calendar: CalendarPredicate,
    tz: ZoneInfo,
    accepted: set[str],
) -> dict[str, Any] | None:
    """Parameters that redo ``rule``'s fire at ``fire`` now.

    ``days_back`` offsets are resolved against the fire's date (with the rule's calendar)
    and passed as ``date_from``/``date_to`` when the job accepts them; otherwise the rule's
    parameters are reused only when the fire was today. ``None``: cannot be redone safely.
    """
    params = dict(rule.params)
    fire_day = fire.astimezone(tz).date()
    if "days_back" in params:
        if {"date_from", "date_to"} <= accepted:
            offsets = [int(x) for x in params.pop("days_back")]
            dates = business_days_back(fire_day, offsets, lambda d: calendar(d, rule.calendar))
            if not dates:
                return None
            params["date_from"], params["date_to"] = min(dates), max(dates)
            return params
        if fire_day != datetime.now(tz).date():
            return None
    return params


def _run_stats(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT h.job, h.last_status, h.last_started_at, h.last_success_at,
                   h.consecutive_failures, h.last_error, real.started_at
            FROM ops.v_job_health h
            LEFT JOIN LATERAL (
                SELECT r.started_at FROM ops.job_run r
                WHERE r.job = h.job
                  AND NOT (r.status = 'skipped' AND coalesce(r.error, '') LIKE %s)
                ORDER BY r.started_at DESC, r.id DESC
                LIMIT 1
            ) real ON true
            """,
            (LOCK_BUSY + "%",),
        ).fetchall()
    return {
        str(r[0]): {
            "last_status": r[1],
            "last_started_at": r[2],
            "last_success_at": r[3],
            "consecutive_failures": int(r[4] or 0),
            "last_error": r[5],
            "last_real_started_at": r[6],
        }
        for r in rows
    }


def job_health(
    conn: psycopg.Connection[Any],
    *,
    now: datetime,
    calendar: CalendarPredicate,
    tz: ZoneInfo,
    rules: Mapping[str, tuple[Rule, ...]] = RULES,
    known: Iterable[str] = KNOWN_JOBS,
    registry: JobRegistry = REGISTRY,
    grace: timedelta = timedelta(minutes=30),
    failing_threshold: int = 3,
) -> list[JobHealth]:
    """Health of every known/registered/recorded job, sorted by name."""
    stats = _run_stats(conn)
    names = sorted(set(known) | set(rules) | set(registry.names()) | set(stats))
    cutoff = now - grace
    out: list[JobHealth] = []
    for name in names:
        st = stats.get(name, {})
        registered = name in registry
        job_rules = rules.get(name, ())
        found = last_expected(job_rules, cutoff, calendar, tz) if job_rules else None
        expected, expected_rule = found if found else (None, None)
        last_started: datetime | None = st.get("last_started_at")
        real_started: datetime | None = st.get("last_real_started_at")
        missed_by_lock = False
        fails = int(st.get("consecutive_failures", 0))
        last_status: str | None = st.get("last_status")
        if not registered and not st:
            status = "not_registered"
        elif last_status == "failed" and fails >= failing_threshold:
            status = "failing"
        elif expected is not None and (
            real_started is None or real_started < expected - _START_TOLERANCE
        ):
            status = "not_registered" if not registered else "missed"
            missed_by_lock = (
                registered
                and last_started is not None
                and last_started >= expected - _START_TOLERANCE
                and last_status == "skipped"
                and (st.get("last_error") or "").startswith(LOCK_BUSY)
            )
        elif last_status == "failed":
            status = "degraded"
        elif not job_rules:
            status = "manual"
        elif last_started is None:
            status = "never_run"
        else:
            status = "ok"
        out.append(
            JobHealth(
                job=name,
                status=status,
                registered=registered,
                scheduled=bool(job_rules),
                last_status=last_status,
                last_started_at=last_started,
                last_success_at=st.get("last_success_at"),
                consecutive_failures=fails,
                last_expected_at=expected,
                last_error=st.get("last_error"),
                missed_by_lock=missed_by_lock,
                expected_rule=expected_rule,
            )
        )
    return out
