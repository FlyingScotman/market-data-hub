from __future__ import annotations

import logging
from datetime import date

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.bizday import CalendarPredicate, DbCalendar, weekday_calendar
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, JobRegistry, discover_jobs
from mdhub.schedule import KNOWN_JOBS, RULES, Rule, build_scheduler, check_rules, rule_applies

EXPECTED_JOBS = {
    "bonds_static", "bonds_static_probe", "cbonds_enrich", "bonds_eod", "bonds_intraday",
    "fx_moex_intraday", "fx_moex_eod", "fx_indicative", "futures_intraday", "futures_eod",
    "algopack_fx_futures", "indices_eod", "zcyc", "cbr_daily", "cbr_meetings", "ecb_fx",
    "swaps_ratescalcs", "quality", "backfill_worker", "maintenance",
}  # fmt: skip


def test_known_jobs_are_exactly_the_design_set() -> None:
    assert set(KNOWN_JOBS) == EXPECTED_JOBS and len(KNOWN_JOBS) == len(EXPECTED_JOBS)


def test_rules_reference_only_known_jobs_and_parse() -> None:
    assert set(RULES) == EXPECTED_JOBS  # every job is scheduled, nothing else
    assert check_rules(RULES) == []
    bad = {"nonsense": (Rule("0 25 * * *"),)}
    problems = check_rules(bad)
    assert any("not in KNOWN_JOBS" in p for p in problems)
    assert any("bad cron" in p for p in problems)


def test_registered_jobs_accept_rule_params() -> None:
    """Guard for loader authors: every registered job accepts the params its rules pass."""
    report = discover_jobs()
    assert report.failed == {}
    assert check_rules(RULES, REGISTRY) == []


def test_param_mismatch_detected() -> None:
    reg = JobRegistry()

    def bonds_eod(ctx: JobContext) -> None: ...

    reg.register("bonds_eod", "d", bonds_eod)
    problems = check_rules(RULES, reg)
    assert problems and all(p.startswith("bonds_eod") for p in problems)


class Holidays(CalendarPredicate):
    def __init__(self, holidays: set[date]) -> None:
        self.holidays = holidays

    def __call__(self, day: date, calendar: str = "MOEX_STOCK") -> bool:
        return day.weekday() < 5 and day not in self.holidays


def test_rule_gating() -> None:
    biz = Rule("0 9 * * *")
    every = Rule("0 9 * * *", days="all")
    first = Rule("0 9 * * *", days="first_business_of_month")
    shifted = Rule("20 0 * * *", gate_offset_days=1)
    sat, sun, mon = date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 19)
    assert not rule_applies(biz, sat, weekday_calendar)
    assert rule_applies(biz, mon, weekday_calendar)
    assert rule_applies(every, sun, weekday_calendar)
    assert rule_applies(shifted, sat, weekday_calendar)  # gates on Friday
    assert not rule_applies(shifted, mon, weekday_calendar)  # gates on Sunday
    # 2026-11-02 is Monday; with Nov 2 a holiday the first business day is Nov 3.
    cal = Holidays({date(2026, 11, 2)})
    assert not rule_applies(first, date(2026, 11, 2), cal)
    assert rule_applies(first, date(2026, 11, 3), cal)
    assert not rule_applies(first, date(2026, 11, 4), cal)


def test_db_calendar(db: Database, conn: psycopg.Connection[DictRow]) -> None:
    cal = DbCalendar(db, ttl=0)
    assert cal(date(2026, 10, 16)) is True  # Friday, no rows -> weekday fallback
    assert cal(date(2026, 10, 17)) is False
    conn.execute(
        """
        INSERT INTO ref.calendar_day (calendar, day, is_business_day, source, known_at) VALUES
          ('MOEX_STOCK', '2026-11-04', false, 'test', '2026-01-01'),
          ('MOEX_STOCK', '2026-11-04', true,  'old',  '2025-01-01'),
          ('MOEX_STOCK', '2026-11-07', true,  'test', '2026-01-01')
        """
    )
    conn.commit()
    assert cal(date(2026, 11, 4)) is False  # holiday on a Wednesday (latest known_at wins)
    assert cal(date(2026, 11, 7)) is True  # working Saturday
    assert cal(date(2026, 11, 4), "MOEX_FX") is True  # no row for that calendar -> weekday


def test_build_scheduler_skips_unregistered(
    db: Database,
    conn: psycopg.Connection[DictRow],
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reg = JobRegistry()
    ran: list[str] = []

    def ecb_fx(ctx: JobContext) -> None:
        ran.append(ctx.job)

    reg.register("ecb_fx", "ECB reference rates", ecb_fx)
    with caplog.at_level(logging.WARNING, logger="mdhub.schedule"):
        scheduler = build_scheduler(settings, db, calendar=weekday_calendar, registry=reg)
    jobs = scheduler.get_jobs()
    assert [j.id for j in jobs] == ["ecb_fx#0"]
    assert "bonds_eod is not registered" in caplog.text
    trigger = jobs[0].trigger
    assert str(trigger.timezone) == "Europe/Moscow"
    assert scheduler._job_defaults == {
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 300,
    }
    jobs[0].func()  # executes through run_job (rule is days="all")
    assert ran == ["ecb_fx"]
    row = conn.execute("SELECT job, status FROM ops.job_run").fetchone()
    assert row == {"job": "ecb_fx", "status": "succeeded"}


def test_rule_helper_calendar_is_gating_not_param() -> None:
    for rule in RULES["fx_moex_intraday"] + RULES["futures_eod"]:
        assert "calendar" not in rule.params
    assert RULES["fx_moex_intraday"][0].calendar == "MOEX_FX"
    assert RULES["futures_eod"][0].calendar == "MOEX_FORTS"
