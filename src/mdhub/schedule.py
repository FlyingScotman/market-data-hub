"""Update schedule (DESIGN §6) as data, and the APScheduler worker that executes it.

Times are Europe/Moscow (``MDHUB_TZ``). Each :class:`Rule` is a 5-field crontab
(``minute hour day month day_of_week``) plus the parameters passed to the job and a day
filter evaluated on the run date (or on the date ``gate_offset_days`` before it):

* ``"business"`` - only on business days of ``calendar`` (``ref.calendar_day``, fallback
  Monday-Friday),
* ``"trading"`` - business days plus the weekend/holiday sessions of ``calendar``'s engine
  (``ref.weekend_session``, :meth:`~mdhub.core.bizday.CalendarPredicate.is_trading_day`):
  polls of markets that trade on such days (FORTS FX futures),
* ``"all"`` - every day,
* ``"first_business_of_month"`` - only on the first business day of the month.

Parameter conventions shared by all loaders (a loader must accept every parameter its rules
pass - enforced by ``tests/test_schedule.py::test_registered_jobs_accept_rule_params``):

* ``mode: str`` - variant of the run: ``full``/``probe`` (static), ``prelim``/``final``
  (EOD), ``intraday``/``eod``/``final`` (ZCYC), ``post_meeting`` (CBR meetings),
  ``backup``/``retention`` (maintenance),
* ``days_back: list[int]`` - business-day offsets to process, ``0`` = run date, ``1`` =
  previous business day (resolve with ``ctx.business_dates(days_back)``),
* ``group: str`` - subset of instruments (indices: ``rusfar``, ``rusfarrt``, ``all``),
* ``keep: int`` - number of backups to keep (maintenance ``backup``).

Lock contention (DESIGN §6): a scheduled fire waits up to :data:`SCHEDULED_LOCK_WAIT_S` for
the job's lock (a backfill of the same job, or a previous run, may hold it); backfill runs yield
to a waiting fire. A fire that still cannot run is recorded in ``ops.job_run`` as ``skipped``
with an error starting ``lock_busy:``; job health reports it as ``missed`` and the ``quality``
job re-runs it (rules with ``catch_up``). Polling rules (``_poll``: AlgoPack, Ratescalcs, OTC
Monitor, the backfill worker, source probes) neither wait nor catch up - the next fire supersedes
a missed one. Intraday snapshot cuts wait up to half their slot (:func:`intraday_lock_wait_s`),
are not caught up, and a cut that still cannot run opens a gap (:attr:`Rule.missed_gap`).

Drain (ADMIN_DESIGN §6.3): while ``ops.worker_control.drain`` is set the worker starts no run;
non-polling fires are recorded as skipped with :data:`mdhub.jobs.DRAINING` (a ``lock_busy``
reason), so the ``quality`` job catches them up after the update.

The scheduler skips rules whose job is not registered (with a warning), so loaders can be
added incrementally.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from mdhub.config import Settings, parse_hhmm, parse_window
from mdhub.core.bizday import DEFAULT_CALENDAR, CalendarPredicate
from mdhub.core.gaps import record_missed
from mdhub.db.pool import Database
from mdhub.jobs import (
    DRAINING,
    LOCK_BUSY,
    REGISTRY,
    JobRegistry,
    record_skipped_run,
    run_job,
    worker_draining,
)

__all__ = [
    "INTRADAY_CUT_GAP_DATASET",
    "KNOWN_JOBS",
    "RULES",
    "SCHEDULED_LOCK_WAIT_S",
    "DayFilter",
    "Rule",
    "build_scheduler",
    "check_rules",
    "effective_rules",
    "intraday_lock_wait_s",
    "rule_applies",
    "snapshot_rules",
]

log = logging.getLogger(__name__)

DayFilter = Literal["business", "trading", "all", "first_business_of_month"]

SCHEDULED_LOCK_WAIT_S: Final = 900
"""A scheduled (non-polling) run waits up to 15 minutes for its job lock. Backfill tasks of
heavy datasets are short (``DatasetSpec.max_task_days``) and yield to a waiting scheduled run,
so the wait normally ends after the current task."""

KNOWN_JOBS: Final = (
    "bonds_static",
    "bonds_static_probe",
    "issuers_static",
    "cbonds_enrich",
    "ratings_agencies",
    "bond_terms_curated",
    "bonds_eod",
    "bonds_intraday",
    "fx_moex_intraday",
    "fx_moex_eod",
    "fx_indicative",
    "futures_intraday",
    "futures_eod",
    "algopack_fx_futures",
    "indices_eod",
    "zcyc",
    "cbr_daily",
    "cbr_meetings",
    "ecb_fx",
    "rosstat_cpi",
    "cbr_inflation",
    "estr_fixing",
    "sofr_fixing",
    "fx_cross",
    "proxy_curves",
    "swaps_ratescalcs",
    "swaps_otc_monitor",
    "quality",
    "backfill_worker",
    "maintenance",
    "source_probe",
    "snapshot_static",
    "snapshot_market",
)
"""All scheduled job names. Loaders must register exactly these names."""


@dataclass(frozen=True, slots=True)
class Rule:
    """One cron trigger for a job."""

    cron: str
    params: Mapping[str, Any] = field(default_factory=dict)
    days: DayFilter = "business"
    calendar: str = DEFAULT_CALENDAR
    gate_offset_days: int = 0
    """Evaluate ``days`` on ``run date - gate_offset_days`` (e.g. 00:20 runs for the
    previous day's session)."""
    note: str = ""
    lock_wait_s: int = SCHEDULED_LOCK_WAIT_S
    """How long a fire waits for the job's lock (held by a backfill or a previous run) before
    it is recorded as ``skipped`` with ``lock_busy``. 0 = skip at once (polling rules)."""
    catch_up: bool = True
    """Re-run a fire that was skipped with ``lock_busy`` (the ``quality`` job does it).
    False for polling rules whose next fire supersedes a missed one."""
    missed_gap: str | None = None
    """Dataset of the ``ops.data_gap`` (key ``{"cut": "HH:MM"}``, the fire's minute) opened when
    a fire of this rule does not run - lock still busy after ``lock_wait_s``, or the worker
    draining - so a missed slot is visible in ``/v1/gaps`` (intraday snapshot cuts, review
    §6.5)."""


def _r(
    cron: str,
    days: DayFilter = "business",
    note: str = "",
    calendar: str = DEFAULT_CALENDAR,
    **params: Any,
) -> Rule:
    return Rule(cron=cron, params=params, days=days, calendar=calendar, note=note)


def _poll(
    cron: str,
    days: DayFilter = "business",
    note: str = "",
    calendar: str = DEFAULT_CALENDAR,
    **params: Any,
) -> Rule:
    """A polling rule: does not wait for a busy lock and is not caught up (the next fire
    supersedes it)."""
    return Rule(
        cron=cron,
        params=params,
        days=days,
        calendar=calendar,
        note=note,
        lock_wait_s=0,
        catch_up=False,
    )


INTRADAY_CUT_GAP_DATASET: Final = "snapshot_market_intraday"
"""``ops.data_gap`` dataset of scheduled intraday cuts that did not run (key ``{"cut":
"HH:MM"}``); closed when the cut is published later (``mdhub run snapshot_market -p
mode=intraday -p day=D -p cut=HH:MM -p known_at=...``)."""
INTRADAY_LOCK_WAIT_MAX_S: Final = 600


def intraday_lock_wait_s(every_minutes: int) -> int:
    """How long an intraday cut waits for its lock: half its slot, at most 10 minutes (a cut
    that starts later than that is no longer the slot's cut)."""
    return min(every_minutes * 30, INTRADAY_LOCK_WAIT_MAX_S)


def _at(hhmm: str) -> str:
    h, m = parse_hhmm(hhmm)
    return f"{m} {h} * * *"


def snapshot_rules(settings: Settings | None = None) -> dict[str, tuple[Rule, ...]]:
    """Rules of the snapshot jobs from the cut-time settings (DESIGN §11; owner decision
    DEC04, so configurable): daily static, market ``eod_preliminary`` (D, evening),
    ``eod_final`` (next morning, D-1) and ``intraday`` every N minutes inside the window.
    Without ``settings``: the defaults (the environment is not read)."""
    cfg = settings or Settings.model_construct()
    intraday: tuple[Rule, ...] = ()
    every = cfg.snapshot_intraday_minutes
    if every:
        first, last = parse_window(cfg.snapshot_intraday_window)
        minutes = "0" if every == 60 else f"*/{every}"
        wait = intraday_lock_wait_s(every)
        intraday = tuple(
            Rule(
                cron=cron,
                params={"mode": "intraday"},
                note=note,
                lock_wait_s=wait,
                catch_up=False,
                missed_gap=INTRADAY_CUT_GAP_DATASET,
            )
            for cron, note in (
                (
                    f"{minutes} {first}-{last - 1} * * *",
                    f"every {every} min {first:02d}:00-{last:02d}:00; waits up to {wait} s "
                    "inside its slot, a missed cut opens a gap",
                ),
                (f"0 {last} * * *", ""),
            )
        )
    return {
        "snapshot_static": (
            _r(
                _at(cfg.snapshot_static_time),
                mode="daily",
                note="after bonds_static / cbonds_enrich; changes also at each market cut",
            ),
        ),
        "snapshot_market": (
            _r(
                _at(cfg.snapshot_eod_preliminary_time),
                mode="eod_preliminary",
                days_back=[0],
                cut=cfg.snapshot_eod_preliminary_time,
                note="EOD preliminary for D (after the 19:05-19:30 EOD loads)",
            ),
            _r(
                _at(cfg.snapshot_eod_final_time),
                mode="eod_final",
                days_back=[1],
                cut=cfg.snapshot_eod_final_time,
                note="EOD final for D-1 (after the 07:10-07:25 final loads)",
            ),
            *intraday,
        ),
    }


RULES: Final[Mapping[str, tuple[Rule, ...]]] = {
    "bonds_static": (_r("30 6 * * *", mode="full", note="full universe incl. bondization"),),
    "bonds_static_probe": (_poll("5 9-19 * * *", mode="probe", note="hourly new-listing probe"),),
    "issuers_static": (
        _r("0 7 * * *", mode="full", note="emitter list diff + weekly cards; after bonds_static"),
    ),
    "cbonds_enrich": (_r("30 7 * * *", note="auto-skip while auth returns 401"),),
    "ratings_agencies": (
        _r("50 6 * * *", note="NKR/NRA lists + RA Expert crawl (budget); before issuers_static"),
    ),
    "bond_terms_curated": (
        _r("55 6 * * *", note="curated issue terms with evidence; after bonds_static"),
    ),
    "bonds_eod": (
        _r("20 19 * * *", mode="prelim", days_back=[0]),
        _r("15 7 * * *", mode="final", days_back=[1, 2], note="post-session revisions"),
    ),
    "bonds_intraday": (
        _poll("*/15 10-18 * * *", note="every 15 min 10:00-19:00"),
        _poll("0 19 * * *"),
    ),
    "fx_moex_intraday": (
        _poll("*/5 7-23 * * *", calendar="MOEX_FX", note="every 5 min 07:00-23:55"),
    ),
    "fx_moex_eod": (
        Rule(
            cron="20 0 * * *",
            params={"mode": "prelim", "days_back": [1]},
            calendar="MOEX_FX",
            gate_offset_days=1,
            note="00:20 for the session that just ended (D)",
        ),
        _r("20 7 * * *", calendar="MOEX_FX", mode="final", days_back=[1], note="D-1 recheck"),
    ),
    "fx_indicative": (_r("10 14 * * *"), _r("10 19 * * *")),
    "futures_intraday": (
        _poll(
            "50-59 8 * * *",
            days="trading",
            calendar="MOEX_FORTS",
            note="every 1 min 08:50-23:50, business days and weekend sessions",
        ),
        _poll("* 9-22 * * *", days="trading", calendar="MOEX_FORTS"),
        _poll("0-50 23 * * *", days="trading", calendar="MOEX_FORTS"),
    ),
    "futures_eod": (
        _r("25 19 * * *", calendar="MOEX_FORTS", mode="prelim", days_back=[0]),
        _r("25 7 * * *", calendar="MOEX_FORTS", mode="final", days_back=[1]),
    ),
    "algopack_fx_futures": (
        _poll(
            "1-56/5 7-23 * * *",
            days="trading",
            calendar="MOEX_FORTS",
            note="every 5 min, +1 min offset, 07:01-23:56, business days and weekend sessions; "
            "incremental (latest bars), whole-day re-read every 30 min or on holes",
        ),
    ),
    "indices_eod": (
        _r("40 12 * * *", group="rusfar", note="RUSFAR fixed 12:30"),
        _r("10 18 * * *", group="rusfarrt", note="RUSFARRT fixed 18:00"),
        _r("30 19 * * *", group="all"),
    ),
    "zcyc": (
        _poll("*/15 10-18 * * *", mode="intraday", note="every 15 min 10:00-19:00"),
        _poll("0 19 * * *", mode="intraday"),
        _r("5 19 * * *", mode="eod", days_back=[0]),
        _r("10 7 * * *", mode="final", days_back=[1]),
    ),
    "cbr_daily": (
        _r("0 9 * * *", days="all"),
        _r("30 14 * * *", days="all"),
        _r("30 16 * * *", days="all"),
        _r("30 18 * * *", days="all"),
    ),
    "cbr_meetings": (
        _r("10 9 * * *", days="first_business_of_month", note="monthly calendar refresh"),
        _r(
            "0 15 * * *",
            mode="post_meeting",
            note="no-op unless today/yesterday was a board meeting",
        ),
    ),
    "ecb_fx": (_r("30 18 * * *", days="all"),),
    "rosstat_cpi": (
        _r("5 10 * * *", days="all"),
        _r("35 19 * * *", days="all", note="monthly CPI release ~19:00 mid-month"),
    ),
    "cbr_inflation": (
        _r("15 10 * * *", days="all"),
        _r("45 19 * * *", days="all", note="CBR republishes the Rosstat y/y after the release"),
    ),
    "estr_fixing": (
        _r("20 10 * * *", days="all", note="ECB publishes D at 08:00 CET on D+1"),
        _r("20 13 * * *", days="all"),
    ),
    "sofr_fixing": (
        _r("30 16 * * *", days="all", note="NY Fed publishes D at ~08:00 New York on D+1"),
        _r("30 22 * * *", days="all", note="after revisions (by 14:30 New York)"),
    ),
    "fx_cross": (
        _r("15 9 * * *", days="all", note="after cbr_daily 09:00"),
        _r("45 18 * * *", days="all", note="after cbr_daily 18:30 (rates set today)"),
        _r("20 19 * * *", days="all", note="after fx_indicative 19:10"),
    ),
    "proxy_curves": (
        _r("40 7 * * *", days="all", note="before eod_final 08:00: Treasury curve of D-1"),
        _r("40 23 * * *", days="all", note="Treasury publishes ~23:00-01:00 MSK"),
    ),
    "swaps_ratescalcs": (
        _poll("0,30 9-22 * * *", note="every 30 min 09:00-23:00; skip identical content"),
        _poll("0 23 * * *"),
    ),
    "swaps_otc_monitor": (
        _poll(
            "*/15 9-19 * * *",
            note="every 15 min 09:00-20:00 (legacy collector 10:00-19:45); skip identical content",
        ),
        _poll("0 20 * * *"),
    ),
    "quality": (
        Rule(cron="30 8 * * *", catch_up=False),
        Rule(cron="30 20 * * *", catch_up=False),
    ),
    "backfill_worker": (_poll("*/10 * * * *", days="all", note="DB-driven; runs on all days"),),
    "source_probe": (
        _poll(
            "*/5 * * * *",
            days="all",
            note="reachability, credentials, TLS expiry per source (Cbonds hourly)",
        ),
    ),
    "maintenance": (
        _r("0 2 * * *", days="all", mode="backup", keep=14, note="pg_dump, keep 14"),
        _r("0 3 * * *", days="all", mode="retention", note="raw retention + VACUUM ANALYZE"),
    ),
    **snapshot_rules(),
}
"""Job name -> cron rules (DESIGN §6); snapshot jobs with the default cut times
(:func:`effective_rules` applies the configured ones)."""


def effective_rules(settings: Settings) -> dict[str, tuple[Rule, ...]]:
    """:data:`RULES` with the snapshot cut times of ``settings``."""
    return {**RULES, **snapshot_rules(settings)}


# ----------------------------------------------------------------------------- gating


def rule_applies(rule: Rule, run_date: date, calendar: CalendarPredicate) -> bool:
    """Whether ``rule`` should fire on ``run_date`` (MSK) given the day filter."""
    day = run_date - timedelta(days=rule.gate_offset_days)
    if rule.days == "all":
        return True
    if rule.days == "business":
        return calendar(day, rule.calendar)
    if rule.days == "trading":
        return calendar.is_trading_day(day, rule.calendar)
    if rule.days == "first_business_of_month":
        if not calendar(day, rule.calendar):
            return False
        cur = day.replace(day=1)
        while cur < day:
            if calendar(cur, rule.calendar):
                return False
            cur += timedelta(days=1)
        return True
    raise ValueError(f"unknown day filter {rule.days!r}")


def check_rules(
    rules: Mapping[str, tuple[Rule, ...]] = RULES, registry: JobRegistry | None = None
) -> list[str]:
    """Validate the schedule; returns a list of problems (empty = OK).

    Checks job names against :data:`KNOWN_JOBS`, cron syntax, and - for jobs registered in
    ``registry`` - that the job function accepts every parameter its rules pass.
    """
    problems: list[str] = []
    for name, job_rules in rules.items():
        if name not in KNOWN_JOBS:
            problems.append(f"{name}: not in KNOWN_JOBS")
        for rule in job_rules:
            try:
                CronTrigger.from_crontab(rule.cron)
            except ValueError as exc:
                problems.append(f"{name}: bad cron {rule.cron!r}: {exc}")
            if registry is not None and name in registry:
                accepted, var_kw = registry.get(name).accepted_params()
                unknown = [p for p in rule.params if p not in accepted and not var_kw]
                if unknown:
                    problems.append(f"{name}: job does not accept rule params {unknown}")
    return problems


# -------------------------------------------------------------------------- scheduler


class _RuleRunner:
    """Executes one rule: day gating, per-job in-process lock, error logging."""

    def __init__(
        self,
        name: str,
        rule: Rule,
        *,
        settings: Settings,
        db: Database,
        registry: JobRegistry,
        calendar: CalendarPredicate,
        lock: threading.Lock,
    ) -> None:
        self.name = name
        self.rule = rule
        self.settings = settings
        self.db = db
        self.registry = registry
        self.calendar = calendar
        self.lock = lock

    def _missed(self, fired: datetime, reason: str) -> None:
        """A fire of a rule with :attr:`Rule.missed_gap` did not run: open its gap."""
        if self.rule.missed_gap is None:
            return
        cut = fired.strftime("%H:%M")
        log.error("%s: scheduled %s fire did not run: %s", self.name, cut, reason)
        try:
            with self.db.connection() as conn:
                record_missed(
                    conn,
                    self.rule.missed_gap,
                    fired.date(),
                    f"scheduled {self.name} {dict(self.rule.params)} at {cut} did not run: "
                    f"{reason}",
                    key={"cut": cut},
                )
        except Exception:
            log.exception("%s: recording the missed fire failed", self.name)

    def __call__(self) -> None:
        fired = datetime.now(self.settings.zone).replace(second=0, microsecond=0)
        run_date = fired.date()
        if not rule_applies(self.rule, run_date, self.calendar):
            log.debug("%s: %s not applicable on %s", self.name, self.rule.cron, run_date)
            return
        wait = float(self.rule.lock_wait_s)
        started = time.monotonic()
        acquired = self.lock.acquire(timeout=wait) if wait > 0 else self.lock.acquire(False)
        if not acquired:
            log.warning("%s: previous run still in progress; skipping this trigger", self.name)
            reason = (
                f"{LOCK_BUSY}: job {self.name}: previous run in this process still in "
                f"progress after {wait:g} s"
            )
            if wait > 0:  # a fire that should have run: record it so health can flag it
                record_skipped_run(
                    self.db,
                    self.name,
                    dict(self.rule.params),
                    reason,
                    settings=self.settings,
                    trigger="schedule",
                )
            self._missed(fired, reason)
            return
        try:
            # Checked after the lock wait: a fire that waited must not start once draining.
            if worker_draining(self.db):
                log.info("%s: worker draining for an update; not starting", self.name)
                if wait > 0:
                    record_skipped_run(
                        self.db,
                        self.name,
                        dict(self.rule.params),
                        DRAINING,
                        settings=self.settings,
                        trigger="schedule",
                    )
                self._missed(fired, DRAINING)
                return
            outcome = run_job(
                self.name,
                dict(self.rule.params),
                settings=self.settings,
                db=self.db,
                registry=self.registry,
                calendar=self.calendar,
                lock_wait_s=max(0.0, wait - (time.monotonic() - started)),
                trigger="schedule",
            )
            log.info("%s finished: %s (run %s)", self.name, outcome.status, outcome.run_id)
            if outcome.status == "skipped" and self.rule.missed_gap is not None:
                error = _run_error(self.db, outcome.run_id)
                if error.startswith(LOCK_BUSY):
                    self._missed(fired, error)
        except Exception:
            log.exception("%s failed", self.name)
        finally:
            self.lock.release()


def _run_error(db: Database, run_id: int | None) -> str:
    if run_id is None:
        return ""
    with db.connection() as conn:
        row = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (run_id,)).fetchone()
    return str(row["error"] or "") if row else ""


def build_scheduler(
    settings: Settings,
    db: Database,
    *,
    calendar: CalendarPredicate,
    registry: JobRegistry = REGISTRY,
    rules: Mapping[str, tuple[Rule, ...]] = RULES,
    max_workers: int = 8,
    misfire_grace_time: int = 300,
) -> BlockingScheduler:
    """Create a :class:`BlockingScheduler` with one APScheduler job per rule.

    Jobs use ``max_instances=1``, ``coalesce=True`` and ``misfire_grace_time`` seconds; an
    in-process lock per job name additionally prevents overlapping runs of different rules
    of the same job, and :func:`run_job`'s advisory lock prevents overlap across processes.
    Rules of unregistered jobs are skipped with a warning.
    """
    tz = settings.zone
    scheduler = BlockingScheduler(
        timezone=tz,
        executors={"default": ThreadPoolExecutor(max_workers=max_workers)},
        job_defaults={
            "max_instances": 1,
            "coalesce": True,
            "misfire_grace_time": misfire_grace_time,
        },
    )
    for name, job_rules in rules.items():
        if name not in registry:
            log.warning(
                "job %s is not registered; its %d rule(s) are skipped", name, len(job_rules)
            )
            continue
        spec = registry.get(name)
        locks: dict[str, threading.Lock] = {}
        for i, rule in enumerate(job_rules):
            # one in-process lock per job lock name (JobSpec.lock_key: snapshot_market intraday
            # cuts do not queue behind EOD cuts)
            lock = locks.setdefault(spec.lock_name(rule.params), threading.Lock())
            scheduler.add_job(
                _RuleRunner(
                    name,
                    rule,
                    settings=settings,
                    db=db,
                    registry=registry,
                    calendar=calendar,
                    lock=lock,
                ),
                trigger=CronTrigger.from_crontab(rule.cron, timezone=tz),
                id=f"{name}#{i}",
                name=f"{name} [{rule.cron}]",
                replace_existing=True,
            )
    return scheduler
