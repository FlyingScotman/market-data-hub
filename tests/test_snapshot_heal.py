"""``snapshot_heal`` (market-data-hub#138): the hub finds the business days without an
``eod_final`` cut and the old cuts built on the static of their build day, repairs them on its
own (night window, bounded per run), reports them as ``ops.data_gap`` rows, resumes after an
interruption and does nothing when nothing is left."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import mdhub.loaders.snapshot_heal as heal
import mdhub.quality.backfill as backfill
import mdhub.quality.history_load as history_load
from mdhub.config import Settings, in_clock_window, parse_clock_window
from mdhub.core.bizday import DbCalendar
from mdhub.core.errors import NOT_CONFIGURED, SourceNotConfigured
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import UpsertResult
from mdhub.datasets import (
    DatasetSpec,
    datasets_for_job,
    get_dataset,
    register_dataset,
    unregister_dataset,
)
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, JobOutcome, discover_jobs, run_job
from mdhub.quality.checks import queue_range
from mdhub.snapshot.readiness import SNAPSHOT_INPUT_JOBS
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.static import STATIC_BASIS_AS_OF_DATE
from test_api_seed import seed
from test_contract_1_7 import curve
from test_snapshots import DAY, Conn, _all_snapshots_valid, _bundle, _cut_ts

TODAY = date(2026, 10, 20)  # Tuesday: the previous business day is Monday 10-19
FROM = date(2026, 10, 12)
QUOTED = [date(2026, 10, 13), date(2026, 10, 14), DAY]  # Tue, Wed, Thu; Mon 12 and Fri 16 empty
NIGHT = datetime(2026, 10, 20, 23, 30, tzinfo=MSK)


def _copy_day(conn: Conn, table: str, src: date, dst: date) -> None:
    """The rows of ``table`` of day ``src`` again on day ``dst``."""
    conn.execute(
        f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "
        f"to_jsonb(t) || jsonb_build_object('trade_date', %s::date))).* "
        f"FROM {table} t WHERE trade_date = %s ON CONFLICT DO NOTHING",
        (dst, src),
    )


@pytest.fixture
def world(conn: Conn, settings: Settings, today_is: Callable[[date], None]) -> Conn:
    """Quotes on three days (the seed's day copied), today = 2026-10-20, night, no cuts."""
    seed(conn)
    for table in ("md.bond_eod", "md.bond_eod_yield"):
        for d in QUOTED:
            if d != DAY:
                _copy_day(conn, table, DAY, d)
    conn.commit()
    today_is(TODAY)
    return conn


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """The job's clock (night by default) and no real pause between cuts."""
    clock = [NIGHT.astimezone(UTC)]
    monkeypatch.setattr(heal, "_now", lambda: clock[0])
    monkeypatch.setattr(backfill, "_now", lambda: clock[0])
    monkeypatch.setattr(heal, "_sleep", lambda _s: None)
    return clock


def _heal(settings: Settings, db: Database, **params: Any) -> JobOutcome:
    # the history load of new datasets is tested at the end of this file
    params = {
        "date_from": FROM.isoformat(),
        "min_bond_rows": 1,
        "history": False,
        "history_only": [],  # no dataset of the test database is looked at (previews: their tests)
        **params,
    }
    # the database calendar, as in production (it counts the Saturday 2026-10-17 of the seed as a
    # business day: a working weekend day)
    return run_job("snapshot_heal", params, settings=settings, db=db, calendar=DbCalendar(db))


def _gaps(conn: Conn, dataset: str) -> dict[date, tuple[str, str]]:
    return {
        r["gap_date"]: (r["status"], r["note"])
        for r in conn.execute(
            "SELECT gap_date, status, note FROM ops.data_gap WHERE dataset = %s", (dataset,)
        ).fetchall()
    }


def _eod_final(conn: Conn) -> dict[date, list[str]]:
    out: dict[date, list[str]] = {}
    for r in conn.execute(
        "SELECT business_date, id FROM ops.published_snapshot "
        "WHERE kind = 'market' AND cut_type = 'eod_final' ORDER BY published_at, id"
    ).fetchall():
        out.setdefault(r["business_date"], []).append(r["id"])
    return out


def _count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM ops.published_snapshot").fetchone()
    assert row is not None
    return int(row["n"])


# ------------------------------------------------------------------------ missing cuts


def test_missing_cuts_are_found_built_and_reported(
    world: Conn, settings: Settings, db: Database
) -> None:
    out = _heal(settings, db)
    assert out.status == "succeeded", out
    assert [b["day"] for b in out.result["built"]] == [d.isoformat() for d in QUOTED]
    cuts = _eod_final(world)
    assert sorted(cuts) == QUOTED and all(len(v) == 1 for v in cuts.values())
    # a cut of a past day is bound to a static built for that day
    first = _bundle(world, cuts[QUOTED[0]][0])
    static = _bundle(world, first["static_snapshot_id"])
    assert static["business_date"] == QUOTED[0].isoformat()
    assert static["parameters"]["static_basis"] == STATIC_BASIS_AS_OF_DATE
    assert first["cut_ts"] == "2026-10-14T05:00:00Z"  # 08:00 MSK the next business day
    assert _all_snapshots_valid(world) >= 3

    gaps = _gaps(world, heal.MISSING_GAPS)
    for d in QUOTED:
        assert gaps[d][0] == "backfilled" and gaps[d][1].startswith("cut published")
    # days without quotes are not cut: open, with the reason, listed by the status view
    no_quotes = (
        date(2026, 10, 12),
        date(2026, 10, 16),
        date(2026, 10, 17),
    )  # 17: a working Saturday
    for d in no_quotes:
        assert gaps[d][0] == "open" and "no bond quotes" in gaps[d][1]
    open_days = {
        r["gap_date"]
        for r in world.execute(
            "SELECT gap_date FROM ops.v_open_gaps WHERE dataset = %s", (heal.MISSING_GAPS,)
        ).fetchall()
    }
    assert open_days == set(no_quotes)
    # the previous business day (the scheduled cut's) and non-business days are left alone
    assert date(2026, 10, 19) not in gaps and date(2026, 10, 18) not in gaps


def test_second_run_is_a_noop_and_quotes_arriving_later_are_cut(
    world: Conn, settings: Settings, db: Database
) -> None:
    _heal(settings, db)
    before = _count(world)
    again = _heal(settings, db)
    assert again.status == "skipped" and again.result["built"] == []
    assert _count(world) == before
    # the quotes of Friday arrive (the ordinary bonds_eod backfill): the next run cuts the day
    _copy_day(world, "md.bond_eod", DAY, date(2026, 10, 16))
    world.commit()
    out = _heal(settings, db)
    assert out.status == "succeeded"
    assert [b["day"] for b in out.result["built"]] == ["2026-10-16"]
    assert _gaps(world, heal.MISSING_GAPS)[date(2026, 10, 16)][0] == "backfilled"


def _row_versions(conn: Conn) -> dict[tuple[str, date, str], str]:
    """The physical version of every gap row: it changes whenever the row is written."""
    return {
        (r["dataset"], r["gap_date"], str(r["key"])): r["v"]
        for r in conn.execute(
            "SELECT dataset, gap_date, key, xmin::text AS v FROM ops.data_gap"
        ).fetchall()
    }


def test_gap_rows_are_written_only_when_they_change(
    world: Conn, settings: Settings, db: Database
) -> None:
    _old_style_cut(world, db, DAY)
    _heal(settings, db, mode="detect", date_from=FROM.isoformat())
    before = _row_versions(world)
    assert len(before) >= 6  # missing days of both kinds and the reissue
    for _ in range(2):
        _heal(settings, db, mode="detect", date_from=FROM.isoformat())
        assert _row_versions(world) == before
    # a changed reason is written, and only on that row
    _copy_day(world, "md.bond_eod", DAY, date(2026, 10, 16))
    world.commit()
    _heal(settings, db, mode="detect", date_from=FROM.isoformat())
    after = _row_versions(world)
    changed = {k for k in before if after[k] != before[k]}
    assert {k[1] for k in changed} == {date(2026, 10, 16)}
    assert "will be built" in _gaps(world, heal.MISSING_GAPS)[date(2026, 10, 16)][1]


def test_gaps_no_longer_detected_say_why_they_were_closed(
    world: Conn, settings: Settings, db: Database
) -> None:
    d12, d13, d14, d15, d16 = (date(2026, 10, n) for n in (12, 13, 14, 15, 16))
    _heal(settings, db, mode="detect")
    assert {d for d, (st, _) in _gaps(world, heal.MISSING_GAPS).items() if st == "open"} >= {
        d12,
        d13,
        d14,
        d15,
        d16,
    }

    # the checked range moved (MDHUB_SNAPSHOT_HEAL_FROM): not repaired, just not looked at
    _heal(settings, db, mode="detect", date_from=d15.isoformat())
    gaps = _gaps(world, heal.MISSING_GAPS)
    for d in (d12, d13, d14):
        assert gaps[d][0] == "not_expected" and "outside the checked range" in gaps[d][1]
    assert gaps[d15][0] == "open"
    _heal(settings, db, mode="detect")  # the range is back: the gaps are open again
    assert _gaps(world, heal.MISSING_GAPS)[d13][0] == "open"

    # the calendar changed: the day is not a business day (a correction of ref.calendar_day)
    world.execute(
        "INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) "
        "VALUES ('MOEX_STOCK', %s, 'curated', false, '2026-01-01Z')",
        (d16,),
    )
    world.commit()
    _heal(settings, db, mode="detect")
    status, note = _gaps(world, heal.MISSING_GAPS)[d16]
    assert status == "not_expected" and "not a business day" in note

    # cuts published by someone else: really repaired. One of the days had been given up
    world.execute(
        "UPDATE ops.data_gap SET status = 'unfillable', note = 'heal failed (5): boom "
        "(gave up after 5 attempts; hub 1.0)' WHERE dataset = %s AND gap_date = %s",
        (heal.MISSING_GAPS, d14),
    )
    world.commit()
    for d in (d13, d14):
        publish_market_snapshot(
            db,
            cut_type="eod_final",
            business_date=d,
            cut_ts=_cut_ts(d, 8, 0) + timedelta(days=1),
            known_at=datetime.now(UTC),
            historical=True,
        )
    _heal(settings, db, mode="detect")
    gaps = _gaps(world, heal.MISSING_GAPS)
    for d in (d13, d14):
        assert gaps[d] == ("backfilled", "an eod_final cut exists now")


def test_a_reissue_repaired_elsewhere_is_closed_as_repaired(
    world: Conn, settings: Settings, db: Database
) -> None:
    old = _old_style_cut(world, db, DAY)
    _heal(settings, db, mode="detect", date_from=DAY.isoformat())
    assert _gaps(world, heal.REISSUE_GAPS)[DAY][0] == "open"
    # the correction comes from the autocorrection of an input backfill (same cut_ts)
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=DAY,
        cut_ts=_cut_ts(DAY, 8, 0) + timedelta(days=1),
        known_at=datetime.now(UTC),
        historical=True,
    )
    assert out.market.id != old
    _heal(settings, db, mode="detect", date_from=DAY.isoformat())
    assert _gaps(world, heal.REISSUE_GAPS)[DAY] == (
        "backfilled",
        "the cut is no longer on the static of its build day",
    )


def test_a_day_waits_while_its_input_data_is_being_filled(
    world: Conn, settings: Settings, db: Database
) -> None:
    """An open gap or a pending / running backfill of an input dataset of the day holds the
    cut back (the autocorrection would otherwise publish a second version of it at once); the
    day is built when that is done. Other days and finished tasks do not matter."""
    discover_jobs()
    zcyc = datasets_for_job("zcyc")[0].name
    bonds = datasets_for_job("bonds_eod")[0].name
    d13, d14, d15 = QUOTED
    world.execute(
        "INSERT INTO ops.data_gap (dataset, key, gap_date, status, note) "
        "VALUES (%s, '{}', %s, 'open', 'queued')",
        (zcyc, d13),
    )

    def task(dataset: str, lo: date, hi: date, status: str) -> None:
        world.execute(
            "INSERT INTO ops.backfill_task (dataset, params, status) VALUES (%s, %s, %s)",
            (dataset, Jsonb({"date_from": lo.isoformat(), "date_to": hi.isoformat()}), status),
        )

    task(bonds, d14 - timedelta(days=1), d14, "running")  # covers 14
    task(bonds, d15, d15, "done")  # finished: no reason to wait
    task(bonds, d15 + timedelta(days=1), d15 + timedelta(days=2), "pending")  # other days
    world.commit()

    out = _heal(settings, db)
    assert [b["day"] for b in out.result["built"]] == [d15.isoformat()]
    assert out.result["missing_waiting_inputs"] == 2
    gaps = _gaps(world, heal.MISSING_GAPS)
    assert gaps[d13][0] == "open" and "waiting for the input data" in gaps[d13][1]
    assert f"{zcyc} (open gap)" in gaps[d13][1]
    assert gaps[d14][0] == "open" and f"{bonds} (backfill running)" in gaps[d14][1]
    assert sorted(_eod_final(world)) == [d15]
    assert _heal(settings, db).status == "skipped"  # nothing due: still waiting
    assert _eod_final(world).keys() == {d15}

    # the backfills end: the next run builds both days
    world.execute("UPDATE ops.data_gap SET status = 'backfilled' WHERE dataset = %s", (zcyc,))
    world.execute("UPDATE ops.backfill_task SET status = 'done' WHERE status = 'running'")
    world.commit()
    out = _heal(settings, db)
    assert [b["day"] for b in out.result["built"]] == [d13.isoformat(), d14.isoformat()]
    assert out.result["missing_waiting_inputs"] == 0
    final = _gaps(world, heal.MISSING_GAPS)
    assert final[d13][0] == "backfilled" and final[d14][0] == "backfilled"


def test_runs_are_bounded_and_resume_where_they_stopped(
    world: Conn, settings: Settings, db: Database
) -> None:
    first = _heal(settings, db, max_cuts=2)
    assert [b["day"] for b in first.result["built"]] == ["2026-10-13", "2026-10-14"]
    assert first.result["remaining"] == 1
    assert sorted(_eod_final(world)) == QUOTED[:2]
    # a new run (after a restart, a crash, the next night) starts from the database state
    second = _heal(settings, db, max_cuts=2)
    assert [b["day"] for b in second.result["built"]] == ["2026-10-15"]
    assert sorted(_eod_final(world)) == QUOTED
    assert _heal(settings, db, max_cuts=2).status == "skipped"


def test_a_failing_day_is_retried_later_and_given_up_after_five_attempts(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = heal.publish_market_snapshot
    bad = QUOTED[1]

    def flaky(*a: Any, **kw: Any) -> Any:
        if kw["business_date"] == bad:
            raise RuntimeError("build failed")
        return real(*a, **kw)

    monkeypatch.setattr(heal, "publish_market_snapshot", flaky)
    out = _heal(settings, db)
    assert out.status == "partial" and "build failed" in str(out.result["failed"])
    assert sorted(_eod_final(world)) == [QUOTED[0], QUOTED[2]]  # the others were still built
    status, note = _gaps(world, heal.MISSING_GAPS)[bad]
    assert status == "open" and note.startswith("heal failed (1)")
    # not retried at once ...
    assert _heal(settings, db).status == "skipped"
    # ... but after the wait, until it is given up
    for attempt in range(2, 6):
        fixed_clock[0] = NIGHT.astimezone(UTC) + timedelta(days=attempt)  # a later night
        out = _heal(settings, db)
        assert out.status == "partial", attempt
    status, note = _gaps(world, heal.MISSING_GAPS)[bad]
    assert status == "unfillable" and "gave up after 5 attempts" in note
    fixed_clock[0] = NIGHT.astimezone(UTC) + timedelta(days=9)
    assert _heal(settings, db).status == "skipped"  # given up: not tried within a week


def test_a_day_given_up_is_tried_again_after_a_week_and_with_a_new_hub_version(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No manual action: errors that stopped a day are retried later, with one attempt per
    cycle; the day is repaired when the cause is gone."""
    real = heal.publish_market_snapshot
    bad = QUOTED[1]
    broken = [True]

    def flaky(*a: Any, **kw: Any) -> Any:
        if broken[0] and kw["business_date"] == bad:
            raise RuntimeError("build failed")
        return real(*a, **kw)

    monkeypatch.setattr(heal, "publish_market_snapshot", flaky)
    monkeypatch.setattr(heal, "_hub_version", "1.0")
    start = NIGHT.astimezone(UTC)
    for attempt in range(1, 6):
        fixed_clock[0] = start + timedelta(days=attempt)  # a later night each time
        assert _heal(settings, db).status == "partial", attempt
    given_up = start + timedelta(days=5)
    status, note = _gaps(world, heal.MISSING_GAPS)[bad]
    assert status == "unfillable" and "gave up after 5 attempts; hub 1.0)" in note

    # within a week, same version: left alone
    fixed_clock[0] = given_up + timedelta(days=6)
    assert _heal(settings, db).status == "skipped"
    # after a week: one more attempt; it fails again and the day is given up for another week
    fixed_clock[0] = given_up + timedelta(days=7, hours=1)
    assert _heal(settings, db).status == "partial"
    status, note = _gaps(world, heal.MISSING_GAPS)[bad]
    assert status == "unfillable" and note.startswith("heal failed (6)")
    fixed_clock[0] = given_up + timedelta(days=9)
    assert _heal(settings, db).status == "skipped"
    # a new hub version tries at once; the cause is gone: the day is cut
    monkeypatch.setattr(heal, "_hub_version", "1.1")
    broken[0] = False
    out = _heal(settings, db)
    assert out.status == "succeeded" and [b["day"] for b in out.result["built"]] == [
        bad.isoformat()
    ]
    status, note = _gaps(world, heal.MISSING_GAPS)[bad]
    assert status == "backfilled" and note.startswith("cut published")
    assert bad in _eod_final(world)


def test_a_rebuild_that_changes_nothing_is_never_retried(
    world: Conn, settings: Settings, db: Database, fixed_clock: list[datetime]
) -> None:
    """A reissue that leaves the cut on the old static gives the same result every time: its
    ``unfillable`` gap is not an error to retry, however long ago or whichever hub version."""
    _old_style_cut(world, db, DAY)
    world.execute(
        "INSERT INTO ops.data_gap (dataset, key, gap_date, status, note, detected_at) "
        "VALUES (%s, %s, %s, 'unfillable', 'rebuild (unchanged) left the cut on the static "
        "of its build day; not retried', %s)",
        (
            heal.REISSUE_GAPS,
            Jsonb({"cut_ts": "2026-10-16T05:00:00Z"}),
            DAY,
            NIGHT - timedelta(days=60),
        ),
    )
    world.commit()
    fixed_clock[0] = NIGHT.astimezone(UTC) + timedelta(days=30)
    out = _heal(settings, db, date_from=DAY.isoformat())
    assert out.status == "skipped" and out.result["built"] == []


def test_a_long_window_uses_the_calendar_for_every_day(
    world: Conn, settings: Settings, db: Database
) -> None:
    """Holidays and working weekend days far from the window start are honoured (a year of days is
    read with one calendar query, not just the first two months)."""
    world.execute(
        "INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) VALUES "
        "('MOEX_STOCK', '2026-10-12', 'curated', false, '2026-01-01Z')"  # a Monday holiday
    )
    world.commit()
    out = _heal(settings, db, date_from="2026-01-05", mode="detect")
    assert out.status == "skipped"
    gaps = _gaps(world, heal.MISSING_GAPS)
    assert date(2026, 10, 12) not in gaps  # holiday: not expected
    assert date(2026, 10, 17) in gaps  # a working Saturday by the calendar, no quotes
    assert (
        date(2026, 6, 6) not in gaps and date(2026, 6, 8) in gaps
    )  # an ordinary weekend (not built), a Monday


# -------------------------------------------------------------- reissue of old cuts


def _old_style_cut(conn: Conn, db: Database, day: date, *, hour: int = 8) -> str:
    """A cut published the old way: on the static of its build day (no ``static_basis``).
    ``hour``: 08:00 MSK of the next day is the usual cut; another hour is another cut of the day."""
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=day,
        cut_ts=_cut_ts(day, hour, 0) + timedelta(days=1),
        known_at=datetime(2026, 10, 16, 5, tzinfo=UTC),
        historical=False,
    )
    static = _bundle(conn, out.static.id)
    assert "static_basis" not in static["parameters"]
    return out.market.id


def test_old_cuts_in_the_106_range_are_reissued_once(
    world: Conn, settings: Settings, db: Database
) -> None:
    old = _old_style_cut(world, db, DAY)
    out = _heal(settings, db, date_from=DAY.isoformat())
    assert out.status == "succeeded", out
    done = [b for b in out.result["built"] if b["dataset"] == heal.REISSUE_GAPS]
    assert [b["day"] for b in done] == [DAY.isoformat()] and done[0]["status"] == "published"
    new = done[0]["snapshot"]
    assert _eod_final(world)[DAY] == [old, new]  # the old snapshot stays readable
    row = world.execute(
        "SELECT supersedes, cut_ts FROM ops.published_snapshot WHERE id = %s", (new,)
    ).fetchone()
    assert row is not None and row["supersedes"] == old
    assert row["cut_ts"] == _cut_ts(DAY, 8, 0) + timedelta(days=1)  # the same cut
    static = _bundle(world, _bundle(world, new)["static_snapshot_id"])
    assert static["parameters"]["static_basis"] == STATIC_BASIS_AS_OF_DATE
    assert _gaps(world, heal.REISSUE_GAPS)[DAY][0] == "backfilled"
    # run again: nothing is detected, nothing is published
    before = _count(world)
    assert _heal(settings, db, date_from=DAY.isoformat()).status == "skipped"
    assert _count(world) == before


def test_a_day_with_two_current_cuts_is_reissued_cut_by_cut(
    world: Conn, settings: Settings, db: Database
) -> None:
    """Gaps of a reissue are per ``(day, cut_ts)``: reissuing the first cut must not close the
    day or give it up while the second cut is still on the old static, also when a run's limit
    stops between the two."""
    old_a = _old_style_cut(world, db, DAY, hour=8)
    old_b = _old_style_cut(world, db, DAY, hour=9)
    assert old_a != old_b
    first = _heal(settings, db, date_from=DAY.isoformat(), max_cuts=1)
    assert [b["day"] for b in first.result["built"]] == [DAY.isoformat()]
    assert first.result["remaining"] == 1
    rows = world.execute(
        "SELECT key ->> 'cut_ts' AS ts, status FROM ops.data_gap WHERE dataset = %s ORDER BY 1",
        (heal.REISSUE_GAPS,),
    ).fetchall()
    assert [(r["ts"], r["status"]) for r in rows] == [
        ("2026-10-16T05:00:00Z", "backfilled"),
        ("2026-10-16T06:00:00Z", "open"),
    ]
    assert len(_eod_final(world)[DAY]) == 3  # two old cuts and the first correction
    # the next run takes the second cut, not the first one again
    second = _heal(settings, db, date_from=DAY.isoformat(), max_cuts=1)
    assert [b["status"] for b in second.result["built"]] == ["published"]
    assert len(_eod_final(world)[DAY]) == 4
    assert {
        r["status"]
        for r in world.execute(
            "SELECT status FROM ops.data_gap WHERE dataset = %s", (heal.REISSUE_GAPS,)
        ).fetchall()
    } == {"backfilled"}
    assert _heal(settings, db, date_from=DAY.isoformat()).status == "skipped"
    assert heal.pending_reissue(world) == []


def test_cuts_outside_the_106_range_are_not_reissued(
    world: Conn, settings: Settings, db: Database, today_is: Callable[[date], None]
) -> None:
    late = heal.REISSUE_TO + timedelta(days=2)  # 2026-10-23, a Friday
    for table in ("md.bond_eod", "md.bond_eod_yield"):
        _copy_day(world, table, DAY, late)
    world.commit()
    today_is(date(2026, 11, 5))
    old = _old_style_cut(world, db, late)
    out = _heal(settings, db, date_from=late.isoformat())
    assert out.status == "skipped", out
    assert _eod_final(world)[late] == [old]
    assert _gaps(world, heal.REISSUE_GAPS) == {}
    assert heal.pending_reissue(world) == []  # the range of the job is fixed


# -------------------------------------------------------------------- modes, window


def test_nothing_is_built_outside_the_night_window_or_in_detect_mode(
    world: Conn, settings: Settings, db: Database, fixed_clock: list[datetime]
) -> None:
    fixed_clock[0] = datetime(2026, 10, 20, 12, 0, tzinfo=MSK).astimezone(UTC)
    out = _heal(settings, db)
    assert out.status == "skipped" and "21:00-06:00" in (_run_error(world, out) or "")
    assert _eod_final(world) == {}
    gaps = _gaps(world, heal.MISSING_GAPS)  # detected and reported at any hour
    assert {d for d, (st, _) in gaps.items() if st == "open"} >= set(QUOTED)

    fixed_clock[0] = NIGHT.astimezone(UTC)
    assert _heal(settings, db, mode="detect").status == "skipped"
    assert _eod_final(world) == {}
    assert _heal(settings, db, mode="off").status == "skipped"
    assert _eod_final(world) == {}
    assert _heal(settings, db, mode="repair").status == "succeeded"
    assert sorted(_eod_final(world)) == QUOTED


def test_the_run_stops_when_the_window_closes(
    world: Conn, settings: Settings, db: Database, fixed_clock: list[datetime]
) -> None:
    fixed_clock[0] = datetime(2026, 10, 20, 5, 59, tzinfo=MSK).astimezone(UTC)
    done: list[date] = []

    def advance(_s: float) -> None:  # the pause after the first cut crosses 06:00
        fixed_clock[0] = datetime(2026, 10, 20, 6, 1, tzinfo=MSK).astimezone(UTC)

    heal._sleep = advance  # restored by the monkeypatch of the autouse fixture
    out = _heal(settings, db)
    done = [date.fromisoformat(b["day"]) for b in out.result["built"]]
    assert done == QUOTED[:1]
    assert out.result["remaining"] == 2


def _run_error(conn: Conn, out: JobOutcome) -> str | None:
    row = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    return None if row is None else row["error"]


def test_the_job_is_scheduled_and_its_settings_have_safe_defaults(settings: Settings) -> None:
    from mdhub.schedule import RULES

    (rule,) = RULES["snapshot_heal"]
    assert rule.cron == "*/30 * * * *" and rule.lock_wait_s == 0 and not rule.catch_up
    assert settings.snapshot_heal == "repair"
    # the history load of new datasets is OFF until the owner switches it on (it only previews)
    assert settings.snapshot_heal_history is False
    assert settings.history_load_max_tasks_per_night == 30
    assert settings.snapshot_heal_from == date(2025, 10, 29)
    assert (settings.snapshot_heal_max_per_run, settings.snapshot_heal_pause_s) == (10, 20)
    assert parse_clock_window(settings.snapshot_heal_window) == (time(21, 0), time(6, 0))


def test_clock_window_start_is_the_start_of_the_night_that_contains_the_time() -> None:
    from mdhub.config import clock_window_start

    night = (time(21, 0), time(6, 0))
    assert clock_window_start(datetime(2026, 10, 20, 23, 30, tzinfo=MSK), night) == datetime(
        2026, 10, 20, 21, 0, tzinfo=MSK
    )
    assert clock_window_start(datetime(2026, 10, 21, 3, 0, tzinfo=MSK), night) == datetime(
        2026, 10, 20, 21, 0, tzinfo=MSK
    )  # after midnight: the night that began the evening before
    assert clock_window_start(datetime(2026, 10, 21, 12, 0, tzinfo=MSK), night) is None
    assert clock_window_start(datetime(2026, 10, 21, 6, 0, tzinfo=MSK), night) is None
    day = (time(9, 0), time(18, 0))
    assert clock_window_start(datetime(2026, 10, 21, 10, 0, tzinfo=MSK), day) == datetime(
        2026, 10, 21, 9, 0, tzinfo=MSK
    )


def test_clock_window_wraps_midnight() -> None:
    night = parse_clock_window("21:00-06:00")
    assert [in_clock_window(time(h), night) for h in (20, 21, 23, 0, 5, 6, 12)] == [
        False, True, True, True, True, False, False,
    ]  # fmt: skip
    day = parse_clock_window("09:30-18:00")
    assert in_clock_window(time(9, 30), day) and not in_clock_window(time(18, 0), day)
    for bad in ("21:00", "10:00-10:00", "25:00-06:00", "9-18"):
        with pytest.raises(ValueError, match=r"expected HH:MM|must differ"):
            parse_clock_window(bad)
    with pytest.raises(ValueError, match="snapshot_heal_window"):
        Settings(database_url="x", snapshot_heal_window="nonsense")
    with pytest.raises(ValueError, match="snapshot_heal"):
        Settings(database_url="x", snapshot_heal="sometimes")


# ---------------------------------------------------- carried curves (contract 1.7, #143)


def _doc_of_current(conn: Conn, day: date) -> dict[str, Any]:
    """The bundle of the current (not superseded) ``eod_final`` cut of ``day``."""
    row = conn.execute(
        "SELECT id FROM ops.published_snapshot p WHERE kind = 'market' AND cut_type = 'eod_final' "
        "AND business_date = %s AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n "
        "WHERE n.supersedes = p.id)",
        (day,),
    ).fetchone()
    assert row is not None
    return _bundle(conn, row["id"])


def _carried_of(doc: dict[str, Any]) -> dict[str, str]:
    """Carried dataset -> source day, of a bundle."""
    return {c["dataset"]: c["source_day"] for c in doc.get("carried_curves", [])}


def test_a_legitimately_carried_cut_is_built_once_and_never_looped_on(
    world: Conn, settings: Settings, db: Database
) -> None:
    """Days whose curves are missing are cut with the curves carried from Monday; nothing newer is
    stored, so the cuts are not defects: the next runs publish nothing and open no gap."""
    mon = date(2026, 10, 12)
    curve(world, "ratescalcs", "key_rate_swap", mon, 1)
    curve(world, "ratescalcs", "ruonia_ois", mon, 2)
    world.commit()
    out = _heal(settings, db)
    assert out.status == "succeeded", out
    d13, d14, d15 = QUOTED
    assert _carried_of(_doc_of_current(world, d13)) == {
        "key_rate_swap": "2026-10-12",
        "ruonia_ois": "2026-10-12",
    }
    assert _carried_of(_doc_of_current(world, d14)) == _carried_of(_doc_of_current(world, d13))
    # the seed has ruonia_ois on the 15th: only the key-rate swaps are carried there
    assert _carried_of(_doc_of_current(world, d15)) == {"key_rate_swap": "2026-10-12"}
    assert [c["age_working_days"] for c in _doc_of_current(world, d14)["carried_curves"]] == [2, 2]

    before = _count(world)
    for _ in range(2):
        again = _heal(settings, db)
        assert again.status == "skipped" and again.result["built"] == []
        assert again.result["carried_pending"] == 0
    assert _count(world) == before
    assert _gaps(world, heal.CARRIED_GAPS) == {}


def test_a_carried_cut_is_reissued_once_when_the_real_data_arrives(
    world: Conn, settings: Settings, db: Database
) -> None:
    mon, wed = date(2026, 10, 12), date(2026, 10, 14)
    curve(world, "ratescalcs", "key_rate_swap", mon, 1)
    curve(world, "ratescalcs", "ruonia_ois", mon, 2)
    world.commit()
    _heal(settings, db)
    d13, d14, d15 = QUOTED
    old = _eod_final(world)

    # the key-rate swaps of Wednesday are imported later
    real = curve(world, "ratescalcs", "key_rate_swap", wed, 3)
    world.commit()
    detected = _heal(settings, db, mode="detect")
    assert detected.result["carried_pending"] == 2  # the 14th (its own day) and the 15th (newer)
    gaps = _gaps(world, heal.CARRIED_GAPS)
    assert sorted(gaps) == [d14, d15] and {st for st, _ in gaps.values()} == {"open"}
    assert "newer data" in gaps[d14][1]
    listed = world.execute(
        "SELECT gap_date FROM ops.v_open_gaps WHERE dataset = %s ORDER BY 1", (heal.CARRIED_GAPS,)
    ).fetchall()
    assert [r["gap_date"] for r in listed] == [d14, d15]  # visible in /v1/gaps

    out = _heal(settings, db)
    assert out.status == "succeeded", out
    assert [(b["day"], b["status"]) for b in out.result["built"]] == [
        (d14.isoformat(), "published"),
        (d15.isoformat(), "published"),
    ]
    assert all(b["datasets"] == [heal.CARRIED_GAPS] for b in out.result["built"])
    # the 13th is untouched; the 14th and the 15th are corrections of their cuts
    cuts = _eod_final(world)
    assert cuts[d13] == old[d13]
    for d in (d14, d15):
        assert len(cuts[d]) == 2
        row = world.execute(
            "SELECT supersedes FROM ops.published_snapshot WHERE id = %s", (cuts[d][1],)
        ).fetchone()
        assert row is not None and row["supersedes"] == cuts[d][0]  # the old one stays readable
    fresh = _doc_of_current(world, d14)
    (krs,) = [q for q in fresh["data"]["curve_quote_sets"] if q["dataset"] == "key_rate_swap"]
    assert krs["quote_set_id"] == real and "carried_forward" not in krs
    assert _carried_of(fresh) == {"ruonia_ois": "2026-10-12"}  # nothing newer for ruonia_ois yet
    assert _carried_of(_doc_of_current(world, d15)) == {"key_rate_swap": "2026-10-14"}
    assert all(st == "backfilled" for st, _ in _gaps(world, heal.CARRIED_GAPS).values())
    assert _gaps(world, heal.CARRIED_GAPS)[d14][1].startswith("reissued: snapshot")

    # a second run is a no-op: the remaining carried cuts are legitimate
    before = _count(world)
    again = _heal(settings, db)
    assert again.status == "skipped" and again.result["built"] == []
    assert _count(world) == before


def test_a_cut_that_stays_outdated_after_a_rebuild_is_not_retried(
    world: Conn, settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard against a loop: if a rebuild leaves the cut carrying a curve for which newer data
    is stored (it cannot with the real builder), the gap is closed ``unfillable`` and not tried
    again."""
    from types import SimpleNamespace

    mon = date(2026, 10, 12)
    curve(world, "ratescalcs", "key_rate_swap", mon, 1)
    world.commit()
    _heal(settings, db)
    curve(world, "ratescalcs", "key_rate_swap", date(2026, 10, 14), 2)
    world.commit()
    calls: list[date] = []

    def fake_publish(*_a: Any, **kw: Any) -> Any:
        calls.append(kw["business_date"])
        return SimpleNamespace(
            market=SimpleNamespace(status="unchanged", id="m"), static=SimpleNamespace(id="s")
        )

    monkeypatch.setattr(heal, "publish_market_snapshot", fake_publish)
    out = _heal(settings, db)
    assert sorted(calls) == [date(2026, 10, 14), date(2026, 10, 15)]
    assert out.status == "succeeded"
    gaps = _gaps(world, heal.CARRIED_GAPS)
    assert {st for st, _ in gaps.values()} == {"unfillable"}
    assert "carrying a curve for which newer data is stored" in gaps[date(2026, 10, 14)][1]
    calls.clear()
    again = _heal(settings, db)
    assert calls == [] and again.result["built"] == []
    # the same data never loops; NEW data does: a newer set of the curve arrives (the 15th can use
    # it, the 14th cannot), so only the 15th is looked at again - and given up again, with the
    # id of the set it saw
    new = curve(world, "ratescalcs", "key_rate_swap", date(2026, 10, 15), 3)
    world.commit()
    third = _heal(settings, db)
    assert calls == [date(2026, 10, 15)] and third.status == "succeeded"
    gaps = _gaps(world, heal.CARRIED_GAPS)
    assert {st for st, _ in gaps.values()} == {"unfillable"}
    assert f"newest quote set {new})" in gaps[date(2026, 10, 15)][1]
    assert f"newest quote set {new})" not in gaps[date(2026, 10, 14)][1]
    calls.clear()
    assert _heal(settings, db).result["built"] == [] and calls == []


def test_a_cut_on_an_old_static_that_also_carries_is_rebuilt_once(
    world: Conn, settings: Settings, db: Database
) -> None:
    """Both defects on one cut: one rebuild, both gaps closed."""
    first = curve(world, "ratescalcs", "key_rate_swap", date(2026, 10, 12), 1)
    # stored back then (the old-style cut is built as known on 2026-10-16)
    world.execute(
        "UPDATE md.curve_snapshot SET ingested_at = '2026-10-12Z' WHERE id = %s", (first,)
    )
    world.commit()
    old = _old_style_cut(world, db, DAY)  # carries key_rate_swap from Monday, old static
    assert _carried_of(_bundle(world, old)) == {"key_rate_swap": "2026-10-12"}
    curve(world, "ratescalcs", "key_rate_swap", date(2026, 10, 14), 2)
    world.commit()
    out = _heal(settings, db, date_from=DAY.isoformat())
    (done,) = [b for b in out.result["built"] if b["day"] == DAY.isoformat()]
    assert done["datasets"] == [heal.REISSUE_GAPS, heal.CARRIED_GAPS]
    assert len(_eod_final(world)[DAY]) == 2
    assert _gaps(world, heal.REISSUE_GAPS)[DAY][0] == "backfilled"
    assert _gaps(world, heal.CARRIED_GAPS)[DAY][0] == "backfilled"


# ----------------------------------------- history of new datasets and its reissue (#138)


@dataclass
class Series:
    """A fake dataset on ``md.fixing`` (index code ``code``) with its backfill job."""

    code: str
    job: str
    dataset: str
    calls: list[tuple[date, date]] = field(default_factory=list)
    fail: bool = False
    unconfigured: bool = False
    no_data: set[date] = field(default_factory=set)


FIRST_STORED = date(2026, 10, 12)  # the live loader started on Monday the 12th
HISTORY_START = date(2026, 10, 1)


def _weekdays(lo: date, hi: date) -> list[date]:
    out, d = [], lo
    while d <= hi:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def new_series(world: Conn, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], Series]]:
    """Factory of a registered fake dataset: ``md.fixing`` rows of one index code, stored by its
    own backfill job from the 12th on (the live loader), history start 2026-10-01. Its job feeds
    the ``eod_final`` cut like the real input jobs."""
    made: list[Series] = []

    def make(code: str) -> Series:
        s = Series(code=code, job=f"test_hl_{code.lower()}", dataset=f"test_hl_{code.lower()}")

        def loader(
            ctx: JobContext,
            *,
            date_from: date | None = None,
            date_to: date | None = None,
        ) -> None:
            assert date_from is not None and date_to is not None
            if s.unconfigured:
                raise SourceNotConfigured("TEST_KEY not configured")
            s.calls.append((date_from, date_to))
            if s.fail:
                raise RuntimeError("source down")
            days = [d for d in _weekdays(date_from, date_to) if d not in s.no_data]
            with ctx.connection() as c, c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
                    "VALUES (%s, %s, 'ecb', 1.5) ON CONFLICT DO NOTHING",
                    [(s.code, d) for d in days],
                )
            ctx.run.add_counts(UpsertResult(inserted=len(days)))

        REGISTRY.register(s.job, "fake history loader", loader)
        register_dataset(
            DatasetSpec(
                name=s.dataset,
                table="md.fixing",
                date_column="fixing_date",
                calendar="MOEX_STOCK",
                frequency="business_daily",
                max_lag_days=1,
                description="t",
                backfill_job=s.job,
                history_start=HISTORY_START,
                where=f"index_code = '{s.code}'",
            )
        )
        for d in (FIRST_STORED, date(2026, 10, 13)):
            world.execute(
                "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
                "VALUES (%s, %s, 'ecb', 1.5)",
                (code, d),
            )
        world.commit()
        made.append(s)
        jobs = frozenset({*SNAPSHOT_INPUT_JOBS, *(m.job for m in made)})
        monkeypatch.setattr(history_load, "SNAPSHOT_INPUT_JOBS", jobs)
        monkeypatch.setattr(backfill, "SNAPSHOT_INPUT_JOBS", jobs)
        return s

    try:
        yield make
    finally:
        for s in made:
            REGISTRY.unregister(s.job)
            unregister_dataset(s.dataset)


def _hl(settings: Settings, db: Database, only: list[Series], **params: Any) -> JobOutcome:
    """A heal run with the history load of exactly ``only`` (night, default window)."""
    return _heal(
        settings,
        db,
        history=True,
        history_only=[s.dataset for s in only],
        **params,
    )


def _work(settings: Settings, db: Database) -> backfill.WorkerSummary:
    """What the backfill worker does with the queued tasks (the history load switched on)."""
    on = settings.model_copy(update={"snapshot_heal_history": True})
    return backfill.process_tasks(settings=on, db=db, calendar=DbCalendar(db))


def _tasks(conn: Conn, dataset: str, source: str = history_load.HISTORY_SOURCE) -> list[Any]:
    return conn.execute(
        "SELECT params ->> 'date_from' AS a, params ->> 'date_to' AS b, status "
        "FROM ops.backfill_task WHERE dataset = %s AND source LIKE %s ORDER BY id",
        (dataset, source),
    ).fetchall()


def _history_row(conn: Conn, dataset: str) -> Any:
    return conn.execute(
        "SELECT * FROM ops.dataset_history_load WHERE dataset = %s", (dataset,)
    ).fetchone()


def _cuts_with(conn: Conn, code: str) -> dict[date, bool]:
    """Whether the current eod_final cut of each day holds the history (before the first stored
    day) of the series ``code``."""
    out = {}
    for d in QUOTED:
        doc = _doc_of_current(conn, d)
        out[d] = any(
            f["index_code"] == code and f["fixing_date"] < FIRST_STORED.isoformat()
            for f in doc["data"]["fixings"]
        )
    return out


def test_new_dataset_history_is_loaded_and_the_cuts_reissued_once(
    world: Conn,
    settings: Settings,
    db: Database,
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    _heal(settings, db)  # the cuts of the 13th..15th, built before the history exists
    base = {d: ids[:] for d, ids in _eod_final(world).items()}
    assert sorted(base) == QUOTED and all(len(v) == 1 for v in base.values())

    # 1) the next night run finds the dataset, queues its history and says so
    out = _hl(settings, db, [s])
    assert out.status == "skipped" and out.result["history"]["new"] == [s.dataset]
    assert [(t["a"], t["b"], t["status"]) for t in _tasks(world, s.dataset)] == [
        ("2026-10-01", "2026-10-09", "pending")  # up to the Friday before the first stored day
    ]
    row = _history_row(world, s.dataset)
    assert row["status"] == "loading" and row["first_before"] == FIRST_STORED
    assert row["history_from"] == HISTORY_START and row["backfill_job"] == s.job
    gap = _gaps_by_key(world, heal.HISTORY_GAPS)[s.dataset]
    assert gap[0] == "open" and "1 backfill task" in gap[1]
    listed = world.execute(
        "SELECT key ->> 'dataset' AS d FROM ops.v_open_gaps WHERE dataset = %s",
        (heal.HISTORY_GAPS,),
    ).fetchall()
    assert [r["d"] for r in listed] == [s.dataset]  # visible in /v1/gaps
    # a second run queues nothing again and builds nothing
    count = _count(world)
    again = _hl(settings, db, [s])
    assert again.result["history"].get("new") is None and len(_tasks(world, s.dataset)) == 1
    assert _count(world) == count and _cuts_with(world, s.code) == dict.fromkeys(QUOTED, False)

    # 2) the ordinary backfill worker loads it; it does not queue a rebuild per task
    summary = _work(settings, db)
    assert summary.done == 1 and s.calls == [(HISTORY_START, date(2026, 10, 9))]
    assert _tasks(world, s.dataset)[0]["status"] == "done"
    assert "queued" not in str(summary.tasks[0].get("snapshot_rebuilds_queued", ""))
    assert world.execute(
        "SELECT count(*) AS n FROM ops.backfill_task WHERE dataset = 'snapshot_eod_final'"
    ).fetchone() == {"n": 0}
    assert _eod_final(world) == base  # no cut was touched yet

    # 3) the next night run reissues the cuts of the last 12 months, once
    out = _hl(settings, db, [s])
    assert out.status == "succeeded", out
    assert [b["day"] for b in out.result["built"]] == [d.isoformat() for d in QUOTED]
    assert all(b["datasets"] == [heal.NEW_SERIES_GAPS] for b in out.result["built"])
    assert out.result["history"]["pass_started"] == {"datasets": [s.dataset], "cuts": 3}
    assert out.result["history"]["finished"] == [s.dataset]
    cuts = _eod_final(world)
    for d in QUOTED:
        assert len(cuts[d]) == 2 and cuts[d][0] == base[d][0]  # a correction; the old one stays
        row = world.execute(
            "SELECT supersedes FROM ops.published_snapshot WHERE id = %s", (cuts[d][1],)
        ).fetchone()
        assert row is not None and row["supersedes"] == cuts[d][0]
    assert _cuts_with(world, s.code) == dict.fromkeys(QUOTED, True)  # the past cuts hold the series
    gaps = _gaps(world, heal.NEW_SERIES_GAPS)
    assert {d: st for d, (st, _n) in gaps.items()} == dict.fromkeys(QUOTED, "backfilled")
    assert gaps[QUOTED[0]][1].startswith("reissued: snapshot")
    row = _history_row(world, s.dataset)
    assert row["status"] == "done" and row["first_after"] == date(2026, 10, 1)
    assert row["pass_started_at"] is not None
    assert _gaps_by_key(world, heal.HISTORY_GAPS)[s.dataset][0] == "backfilled"
    assert _all_snapshots_valid(world) >= 6

    # 4) it never happens again: nothing detected, queued or reissued
    count, tasks = _count(world), len(_tasks(world, s.dataset))
    for _ in range(2):
        idle = _hl(settings, db, [s])
        assert idle.status == "skipped" and idle.result["built"] == []
        assert idle.result["history"] == {"finished": [], "loading": 0, "reissue": 0}
    assert _count(world) == count and len(_tasks(world, s.dataset)) == tasks
    assert _eod_final(world) == cuts


def _gaps_by_key(conn: Conn, dataset: str) -> dict[str, tuple[str, str]]:
    return {
        r["k"]: (r["status"], r["note"])
        for r in conn.execute(
            "SELECT coalesce(key ->> 'dataset', key ->> 'cut_ts') AS k, status, note "
            "FROM ops.data_gap WHERE dataset = %s",
            (dataset,),
        ).fetchall()
    }


def test_the_pass_waits_for_every_loading_dataset_and_reissues_once(
    world: Conn,
    settings: Settings,
    db: Database,
    new_series: Callable[[str], Series],
) -> None:
    a, b = new_series("TESTHLA"), new_series("TESTHLB")
    _heal(settings, db)
    base = _eod_final(world)
    _hl(settings, db, [a, b])
    assert len(_tasks(world, a.dataset)) == 1 and len(_tasks(world, b.dataset)) == 1
    # only A is loaded so far (B's task waits behind a stuck source)
    world.execute(
        "UPDATE ops.backfill_task SET not_before = now() + interval '1 day' WHERE dataset = %s",
        (b.dataset,),
    )
    world.commit()
    _work(settings, db)
    out = _hl(settings, db, [a, b])
    assert out.status == "skipped" and out.result["built"] == []
    assert _history_row(world, a.dataset)["status"] == "reissue"  # waits for B
    assert _history_row(world, b.dataset)["status"] == "loading"
    assert _eod_final(world) == base and "pass_started" not in out.result["history"]
    # B is loaded: ONE pass for both
    world.execute("UPDATE ops.backfill_task SET not_before = NULL WHERE dataset = %s", (b.dataset,))
    world.commit()
    _work(settings, db)
    out = _hl(settings, db, [a, b])
    assert out.result["history"]["pass_started"] == {"datasets": [a.dataset, b.dataset], "cuts": 3}
    assert sorted(out.result["history"]["finished"]) == [a.dataset, b.dataset]
    cuts = _eod_final(world)
    assert all(len(cuts[d]) == 2 for d in QUOTED)  # each cut reissued once, not once per dataset
    assert (
        _cuts_with(world, "TESTHLA") == _cuts_with(world, "TESTHLB") == dict.fromkeys(QUOTED, True)
    )


def _fail_open_tasks(conn: Conn, dataset: str, finished: datetime) -> None:
    """The worker gave up on the open tasks of ``dataset`` after its own attempts."""
    conn.execute(
        "UPDATE ops.backfill_task SET status = 'failed', attempts = 5, done_at = %s "
        "WHERE dataset = %s AND status IN ('pending', 'running')",
        (finished, dataset),
    )
    conn.commit()


def test_a_failed_range_is_retried_over_days_and_given_up_visibly(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    s.fail = True
    _heal(settings, db)
    base = _eod_final(world)
    _hl(settings, db, [s])
    now = fixed_clock[0]
    assert history_load.MAX_ROUNDS == len(history_load.RETRY_DELAYS) + 1 == 5
    assert history_load.RETRY_DELAYS[-1] >= timedelta(days=7)  # spread over more than a week
    for rnd, delay in enumerate(history_load.RETRY_DELAYS, start=1):
        _fail_open_tasks(world, s.dataset, now - delay + timedelta(hours=1))
        _hl(settings, db, [s])
        assert _history_row(world, s.dataset)["load_round"] == rnd  # too soon to retry
        world.execute(
            "UPDATE ops.backfill_task SET done_at = %s WHERE status = 'failed'",
            (now - delay - timedelta(hours=1),),
        )
        world.commit()
        out = _hl(settings, db, [s])
        row = _history_row(world, s.dataset)
        assert row["load_round"] == rnd + 1 and "queued again" in row["note"]
        assert row["status"] == "loading" and out.status == "skipped"
    # the last round fails too: no further round; the load ends with what there is - nothing
    _fail_open_tasks(world, s.dataset, now)
    out = _hl(settings, db, [s])
    row = _history_row(world, s.dataset)
    assert row["status"] == "done" and row["failed_ranges"] == 1
    assert "could not be loaded after 5 round(s)" in row["note"]
    assert "no rows were added" in row["note"] and row["pass_started_at"] is None
    assert len(_tasks(world, s.dataset)) == 5 and _eod_final(world) == base
    assert out.result["built"] == []
    # visible: not a finished gap but an unfillable one with the reason
    gap = _gaps_by_key(world, heal.HISTORY_GAPS)[s.dataset]
    assert gap[0] == "unfillable" and "could not be loaded" in gap[1]


def test_an_outage_of_a_few_days_does_not_lose_the_head_of_the_history(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    s.fail = True
    _heal(settings, db)
    _hl(settings, db, [s])
    now = fixed_clock[0]
    _fail_open_tasks(world, s.dataset, now - timedelta(hours=7))
    _hl(settings, db, [s])  # round 2 after 6 hours
    _fail_open_tasks(world, s.dataset, now - timedelta(days=1, hours=1))
    _hl(settings, db, [s])  # round 3 after a day
    assert _history_row(world, s.dataset)["load_round"] == 3
    s.fail = False  # the provider is back
    _work(settings, db)
    out = _hl(settings, db, [s])
    row = _history_row(world, s.dataset)
    assert row["status"] == "done" and row["failed_ranges"] == 0
    assert row["first_after"] == HISTORY_START and out.result["history"]["pass_started"]
    assert _cuts_with(world, s.code) == dict.fromkeys(QUOTED, True)
    assert _gaps_by_key(world, heal.HISTORY_GAPS)[s.dataset][0] == "backfilled"


def test_nothing_is_loaded_for_a_complete_dataset_or_outside_repair_in_the_window(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    # a series whose stored data already reaches the history start has no head to load
    world.execute(
        "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
        "VALUES (%s, %s, 'ecb', 1.5)",
        (s.code, HISTORY_START),
    )
    world.commit()
    _hl(settings, db, [s])
    assert _history_row(world, s.dataset) is None and _tasks(world, s.dataset) == []
    world.execute(
        "DELETE FROM md.fixing WHERE index_code = %s AND fixing_date = %s", (s.code, HISTORY_START)
    )
    world.commit()

    # detect mode, the switch, and the daytime do nothing
    _hl(settings, db, [s], mode="detect")
    _heal(settings, db, history=False, history_only=[s.dataset])
    assert _history_row(world, s.dataset) is None
    fixed_clock[0] = datetime(2026, 10, 20, 12, 0, tzinfo=MSK).astimezone(UTC)  # daytime
    _hl(settings, db, [s])
    assert _history_row(world, s.dataset) is None and _tasks(world, s.dataset) == []
    # the night, repair: now it is found
    fixed_clock[0] = NIGHT.astimezone(UTC)
    _hl(settings, db, [s])
    assert _history_row(world, s.dataset)["status"] == "loading"


def test_an_empty_dataset_is_loaded_up_to_the_expected_latest_day(
    world: Conn,
    settings: Settings,
    db: Database,
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    world.execute("DELETE FROM md.fixing WHERE index_code = %s", (s.code,))
    world.commit()
    _hl(settings, db, [s])
    row = _history_row(world, s.dataset)
    assert row["first_before"] is None and row["status"] == "loading"
    # today is Tuesday 10-20, max_lag_days 1: the expected latest day is Monday 10-19
    assert row["head_to"] == date(2026, 10, 19)
    assert [(t["a"], t["b"]) for t in _tasks(world, s.dataset)] == [("2026-10-01", "2026-10-19")]


def test_the_history_load_applies_only_to_datasets_that_can_have_one() -> None:
    spec = DatasetSpec(
        name="x",
        table="md.fixing",
        date_column="fixing_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=1,
        description="t",
        backfill_job="snapshot_heal",
        history_start=HISTORY_START,
    )
    assert history_load.eligible(spec)
    for change in (
        {"backfill_job": None},
        {"backfill_job": "no_such_job"},
        {"history_start": None},
        {"frequency": "event"},
        {"frequency": "intraday"},
        {"min_rows_per_day": 0},
        {"rolling_window_setting": "algopack_retention_days"},
    ):
        assert not history_load.eligible(dataclasses.replace(spec, **change)), change


def test_history_tasks_go_last_and_only_other_tasks_queue_the_autocorrection(
    world: Conn,
    settings: Settings,
    db: Database,
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    _heal(settings, db)
    _hl(settings, db, [s])  # a history task is queued first ...
    spec = get_dataset(s.dataset)
    quality = queue_range(world, spec, date(2026, 10, 13), date(2026, 10, 15), source="quality")
    world.commit()
    assert quality[0][0] is not None
    # ... yet the worker takes the other task first: live gap repairs are not delayed by it
    first = backfill.claim_task(db)
    assert first is not None and first.source == "quality"
    backfill.process_task(first, settings=settings, db=db, calendar=DbCalendar(db))
    queued = world.execute(
        "SELECT count(*) AS n FROM ops.backfill_task WHERE dataset = 'snapshot_eod_final'"
    ).fetchone()
    assert queued is not None and queued["n"] >= 1  # the ordinary autocorrection of the cuts
    second = backfill.claim_task(db)  # the rebuild that autocorrection queued, then the history
    assert second is not None and second.source == "snapshot_autocorrect"
    third = backfill.claim_task(db)
    assert third is not None and third.source == history_load.HISTORY_SOURCE


def test_a_dataset_that_cannot_be_checked_does_not_stop_the_others(
    world: Conn,
    settings: Settings,
    db: Database,
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    REGISTRY.register("test_hl_bad", "fake", lambda ctx, *, date_from=None, date_to=None: None)
    register_dataset(
        DatasetSpec(
            name="test_hl_bad",
            table="md.fixing",
            date_column="no_such_column",
            calendar="MOEX_STOCK",
            frequency="business_daily",
            max_lag_days=1,
            description="t",
            backfill_job="test_hl_bad",
            history_start=HISTORY_START,
        )
    )
    try:
        out = _hl(settings, db, [s])
        assert out.result["history"]["new"] == [s.dataset]
        assert _history_row(world, "test_hl_bad") is None
        other = _heal(settings, db, history=True, history_only=["test_hl_bad", s.dataset])
        assert other.result["history"].get("new") is None
    finally:
        REGISTRY.unregister("test_hl_bad")
        unregister_dataset("test_hl_bad")


@pytest.fixture
def shared_job(world: Conn, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Series]]:
    """Factory of fake datasets that share ONE backfill job (``md.fixing``, one index code each).
    The job's fake source has history only for the codes made with ``at_source=True`` (from their
    ``history_start`` on) and loads all of them for every range it is run for."""
    job = "test_hl_shared"
    calls: list[tuple[date, date]] = []
    source_from: dict[str, date] = {}
    specs: list[str] = []
    monkeypatch.setattr(history_load, "SNAPSHOT_INPUT_JOBS", frozenset({*SNAPSHOT_INPUT_JOBS, job}))
    monkeypatch.setattr(backfill, "SNAPSHOT_INPUT_JOBS", frozenset({*SNAPSHOT_INPUT_JOBS, job}))

    def loader(
        ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
    ) -> None:
        assert date_from is not None and date_to is not None
        calls.append((date_from, date_to))
        with ctx.connection() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
                "VALUES (%s, %s, 'ecb', 1.5) ON CONFLICT DO NOTHING",
                [
                    (code, d)
                    for code, floor in source_from.items()
                    for d in _weekdays(max(date_from, floor), date_to)
                ],
            )

    REGISTRY.register(job, "fake shared history loader", loader)

    def make(code: str, start: date, first_stored: date, *, at_source: bool = True) -> Series:
        s = Series(code=code, job=job, dataset=f"test_hl_{code.lower()}", calls=calls)
        if at_source:
            source_from[code] = start
        register_dataset(
            DatasetSpec(
                name=s.dataset,
                table="md.fixing",
                date_column="fixing_date",
                calendar="MOEX_STOCK",
                frequency="business_daily",
                max_lag_days=1,
                description="t",
                backfill_job=job,
                history_start=start,
                where=f"index_code = '{code}'",
            )
        )
        specs.append(s.dataset)
        for d in _weekdays(first_stored, date(2026, 10, 13)):
            world.execute(
                "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
                "VALUES (%s, %s, 'ecb', 1.5)",
                (code, d),
            )
        world.commit()
        return s

    try:
        yield make
    finally:
        REGISTRY.unregister(job)
        for name in specs:
            unregister_dataset(name)


def test_datasets_sharing_a_job_are_judged_when_the_whole_job_has_loaded(
    world: Conn,
    settings: Settings,
    db: Database,
    shared_job: Callable[..., Series],
) -> None:
    """One task set serves both datasets; B (shorter, later head) must wait for it, and must not
    be closed 'no rows were added' before the shared task loads its rows. A is empty at the
    source."""
    a = shared_job("TESTSHA", date(2026, 10, 1), FIRST_STORED, at_source=False)
    b = shared_job("TESTSHB", date(2026, 10, 6), FIRST_STORED)
    _heal(settings, db)
    base = _eod_final(world)
    # the preview already says that they share one set of tasks
    _preview(settings, db, [a, b])
    for x in (a, b):
        assert (
            "shared by 2 dataset(s), 1 task(s) in all"
            in _gaps_by_key(world, heal.HISTORY_GAPS)[x.dataset][1]
        )
    _hl(settings, db, [a, b])
    # one task set over the union of both heads (the head of B lies inside the head of A)
    assert [(t["a"], t["b"]) for t in _tasks(world, a.dataset)] == [("2026-10-01", "2026-10-09")]
    for s in (a, b):
        assert _history_row(world, s.dataset)["status"] == "loading"
        assert "1 backfill task" in _history_row(world, s.dataset)["note"]
    # still loading: the next runs do not close the shorter head 'done' on an empty task list
    out = _hl(settings, db, [a, b])
    assert out.result["history"]["loading"] == 2
    assert _tasks(world, b.dataset) == []  # the tasks are queued under one dataset's name

    _work(settings, db)
    assert a.calls == [(date(2026, 10, 1), date(2026, 10, 9))]  # one download for both
    out = _hl(settings, db, [a, b])
    row_a, row_b = _history_row(world, a.dataset), _history_row(world, b.dataset)
    assert row_a["status"] == "done" and "no rows were added" in row_a["note"]
    assert row_a["pass_started_at"] is None
    # B gained its rows (from 10-06): it is reissued, not closed with "nothing added"
    assert row_b["first_after"] == date(2026, 10, 6)
    assert row_b["status"] == "done" and row_b["pass_started_at"] is not None
    assert out.result["history"]["pass_started"] == {"datasets": [b.dataset], "cuts": 3}
    assert all(len(v) == 2 for v in _eod_final(world).values()) and _eod_final(world) != base


def test_the_load_of_datasets_sharing_a_job_skips_the_years_between_their_heads(
    world: Conn,
    settings: Settings,
    db: Database,
    shared_job: Callable[..., Series],
) -> None:
    """Heads 10-01..10-02 (A) and 10-08..10-09 (B): two tasks, not the hull 10-01..10-09 that
    would download the stored days in between again."""
    a = shared_job("TESTSHA", date(2026, 10, 1), date(2026, 10, 5))
    b = shared_job("TESTSHB", date(2026, 10, 8), FIRST_STORED)
    _hl(settings, db, [a, b])
    assert [(t["a"], t["b"]) for t in _tasks(world, a.dataset)] == [
        ("2026-10-01", "2026-10-02"),
        ("2026-10-08", "2026-10-09"),
    ]
    assert "1 backfill task" in _history_row(world, a.dataset)["note"]
    assert "1 backfill task" in _history_row(world, b.dataset)["note"]
    _work(settings, db)
    assert sorted(a.calls) == [
        (date(2026, 10, 1), date(2026, 10, 2)),
        (date(2026, 10, 8), date(2026, 10, 9)),
    ]
    _hl(settings, db, [a, b])
    assert _history_row(world, a.dataset)["first_after"] == date(2026, 10, 1)
    assert _history_row(world, b.dataset)["first_after"] == date(2026, 10, 8)


def test_a_new_load_ignores_the_old_failed_tasks_of_an_earlier_load_of_the_same_job(
    world: Conn,
    settings: Settings,
    db: Database,
    shared_job: Callable[..., Series],
) -> None:
    """Review of #142: three datasets on one job. A was loaded long ago and left a failed task for
    2026-10-01..10-09; the new loads of B and C overlap that range but must neither count it as
    their own failure nor queue it again (no 'round 1, 1 failed', no unfillable)."""
    a = shared_job("TESTSHA", date(2026, 10, 1), date(2026, 10, 1))  # fully stored
    world.execute(
        "INSERT INTO ops.dataset_history_load (dataset, backfill_job, status, history_from, "
        "head_to, hub_version, note, failed_ranges) VALUES (%s, %s, 'done', '2026-10-01', "
        "'2026-10-09', 'x', 'old', 1)",
        (a.dataset, a.job),
    )
    world.execute(
        "INSERT INTO ops.backfill_task (dataset, params, source, status, attempts, done_at, "
        "last_error) VALUES (%s, %s, 'history_load', 'failed', 5, now() - interval '40 days', "
        "'old boom')",
        (a.dataset, json.dumps({"date_from": "2026-10-01", "date_to": "2026-10-09"})),
    )
    world.commit()
    b = shared_job("TESTSHB", date(2026, 10, 1), FIRST_STORED)
    c = shared_job("TESTSHC", date(2026, 10, 6), FIRST_STORED)
    _hl(settings, db, [b, c])
    for x in (b, c):
        assert "failed" not in _history_row(world, x.dataset)["note"]
        assert _history_row(world, x.dataset)["status"] == "loading"
    _work(settings, db)
    for _ in range(2):
        _hl(settings, db, [b, c])
    for x in (b, c):
        row = _history_row(world, x.dataset)
        assert (row["status"], row["load_round"], row["failed_ranges"]) == ("done", 1, 0), row
    # the old failed task of A was not queued again by anybody
    assert [t["status"] for t in _tasks(world, a.dataset)] == ["failed"]


def test_a_load_adopts_an_identical_task_that_is_open_already(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    shared_job: Callable[..., Series],
) -> None:
    """An operator queued the very range the load needs (same dataset, same dates) and it is still
    open: no second task is queued, the open one is the load's own, so its failure is counted and
    retried."""
    a = shared_job("TESTSHA", date(2026, 10, 1), FIRST_STORED)
    [(task_id, _, _)] = queue_range(
        world, get_dataset(a.dataset), date(2026, 10, 1), date(2026, 10, 9), source="cli"
    )
    world.commit()
    assert task_id is not None
    _hl(settings, db, [a])
    row = _history_row(world, a.dataset)
    assert list(row["task_ids"]) == [task_id] and row["status"] == "loading"
    world.execute(
        "UPDATE ops.backfill_task SET status = 'failed', attempts = 5, "
        "done_at = %s, last_error = 'boom' WHERE id = %s",
        (fixed_clock[0] - timedelta(hours=7), task_id),
    )
    world.commit()
    _hl(settings, db, [a])  # the retry delay is over: the range is queued again
    row = _history_row(world, a.dataset)
    assert (row["status"], row["load_round"]) == ("loading", 2), row
    assert [t["status"] for t in _tasks(world, a.dataset, "%")] == ["failed", "pending"]


# ----------------------- preview, night window and cap, a source that is not configured (#138)


def _preview(settings: Settings, db: Database, only: list[Series], **params: Any) -> JobOutcome:
    """A heal run with the history setting left as it is (the default: off)."""
    p = {
        "date_from": FROM.isoformat(),
        "min_bond_rows": 1,
        "history_only": [s.dataset for s in only],
        **params,
    }
    return run_job("snapshot_heal", p, settings=settings, db=db, calendar=DbCalendar(db))


def test_the_history_load_is_off_by_default_and_previews_what_it_would_queue(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    new_series: Callable[[str], Series],
) -> None:
    a, b = new_series("TESTHLA"), new_series("TESTHLB")
    _heal(settings, db)
    base = _eod_final(world)
    out = _preview(settings, db, [a, b])
    shown = out.result["history"]
    assert shown["queued"] is False and "MDHUB_SNAPSHOT_HEAL_HISTORY is off" in shown["why"]
    assert shown["preview"] == [
        {
            "dataset": s.dataset,
            "job": s.job,
            "from": "2026-10-01",
            "to": "2026-10-09",
            "tasks": 1,
            "job_tasks": 1,
        }
        for s in (a, b)
    ]
    # nothing is queued, no row is written, no cut is touched
    for s in (a, b):
        assert _history_row(world, s.dataset) is None and _tasks(world, s.dataset) == []
    assert _eod_final(world) == base
    # ... but the owner sees the datasets, the ranges and the number of tasks in /v1/gaps
    notes = _gaps_by_key(world, heal.HISTORY_GAPS)
    for s in (a, b):
        status, note = notes[s.dataset]
        assert status == "open" and note.startswith(history_load.PREVIEW_PREFIX)
        assert "would load 2026-10-01..2026-10-09 in 1 backfill task(s)" in note
        assert "MDHUB_SNAPSHOT_HEAL_HISTORY is off" in note
    listed = world.execute(
        "SELECT key ->> 'dataset' AS d FROM ops.v_open_gaps WHERE dataset = %s ORDER BY 1",
        (heal.HISTORY_GAPS,),
    ).fetchall()
    assert [r["d"] for r in listed] == [a.dataset, b.dataset]
    # a second run writes nothing again
    seen = world.execute(
        "SELECT detected_at FROM ops.data_gap WHERE dataset = %s ORDER BY key::text",
        (heal.HISTORY_GAPS,),
    ).fetchall()
    _preview(settings, db, [a, b])
    assert (
        seen
        == world.execute(
            "SELECT detected_at FROM ops.data_gap WHERE dataset = %s ORDER BY key::text",
            (heal.HISTORY_GAPS,),
        ).fetchall()
    )
    # detect mode: the same preview, said so
    _preview(settings, db, [a, b], mode="detect")
    assert "detect mode" in _gaps_by_key(world, heal.HISTORY_GAPS)[a.dataset][1]
    assert _history_row(world, a.dataset) is None
    # the load switched on, but in the daytime: still only the preview
    on = settings.model_copy(update={"snapshot_heal_history": True})
    fixed_clock[0] = datetime(2026, 10, 20, 12, 0, tzinfo=MSK).astimezone(UTC)
    _preview(on, db, [a, b])
    assert "outside the night window" in _gaps_by_key(world, heal.HISTORY_GAPS)[a.dataset][1]
    assert _history_row(world, a.dataset) is None and _tasks(world, a.dataset) == []
    # a head that is no longer missing withdraws its preview
    world.execute(
        "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
        "VALUES (%s, %s, 'ecb', 1.5)",
        (b.code, HISTORY_START),
    )
    world.commit()
    _preview(on, db, [a, b])
    gaps = _gaps_by_key(world, heal.HISTORY_GAPS)
    assert gaps[b.dataset][0] == "not_expected" and "preview withdrawn" in gaps[b.dataset][1]
    assert gaps[a.dataset][0] == "open"
    # the night, the load on: now it is queued and the gap says it is loading
    fixed_clock[0] = NIGHT.astimezone(UTC)
    out = _preview(on, db, [a, b])
    assert out.result["history"]["new"] == [a.dataset]
    assert _history_row(world, a.dataset)["status"] == "loading"
    assert len(_tasks(world, a.dataset)) == 1
    status, note = _gaps_by_key(world, heal.HISTORY_GAPS)[a.dataset]
    assert status == "open" and not note.startswith(history_load.PREVIEW_PREFIX)
    assert "backfill task" in note


def test_history_tasks_run_only_in_the_night_window_up_to_the_nightly_cap(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    new_series: Callable[[str], Series],
) -> None:
    s = new_series("TESTHLA")
    spec = get_dataset(s.dataset)
    on = settings.model_copy(update={"snapshot_heal_history": True})
    capped = on.model_copy(update={"history_load_max_tasks_per_night": 3})
    queue_range(
        world, spec, date(2026, 1, 1), date(2026, 9, 30), source=history_load.HISTORY_SOURCE
    )  # nine tasks of up to 31 days
    world.commit()

    def pending() -> int:
        row = world.execute(
            "SELECT count(*) AS n FROM ops.backfill_task WHERE status = 'pending'"
        ).fetchone()
        assert row is not None
        return int(row["n"])

    def work(clock: datetime) -> int:
        fixed_clock[0] = clock.astimezone(UTC)
        done = backfill.process_tasks(settings=capped, db=db, calendar=DbCalendar(db)).processed
        # the database stamps started_at with its own clock; the test's night is another day
        world.execute(
            "UPDATE ops.backfill_task SET started_at = %s WHERE source = %s AND started_at > %s",
            (clock, history_load.HISTORY_SOURCE, datetime.now(UTC) - timedelta(minutes=30)),
        )
        world.commit()
        return done

    day = datetime(2026, 10, 20, 12, 0, tzinfo=MSK)
    assert backfill.history_slots(db, capped, day.astimezone(UTC)) == 0
    # switched off (the default): no slot even at night, a load that has started stops
    off = capped.model_copy(update={"snapshot_heal_history": False})
    night_utc = datetime(2026, 10, 20, 23, 30, tzinfo=MSK).astimezone(UTC)
    assert backfill.history_slots(db, off, night_utc) == 0
    detect = capped.model_copy(update={"snapshot_heal": "detect"})
    assert backfill.history_slots(db, detect, night_utc) == 0
    assert work(day) == 0 and pending() == 9  # never in the daytime
    assert work(day.replace(hour=20, minute=59)) == 0  # one minute before the window
    night = datetime(2026, 10, 20, 23, 30, tzinfo=MSK)
    assert backfill.history_slots(db, capped, night.astimezone(UTC)) == 3
    assert work(night) == 3 and pending() == 6  # the cap of the night
    assert work(night + timedelta(minutes=10)) == 0  # the same night: no slot left
    assert work(night.replace(day=21, hour=3)) == 0  # after midnight: still that night
    assert backfill.history_slots(db, capped, night.astimezone(UTC)) == 0
    assert work(night + timedelta(days=1)) == 3 and pending() == 3  # the next night
    # a default cap is a night's worth for the rest
    assert backfill.history_slots(db, on, night.astimezone(UTC) + timedelta(days=2)) == 30


def test_a_history_task_without_a_configured_source_waits_and_holds_nobody_back(
    world: Conn,
    settings: Settings,
    db: Database,
    fixed_clock: list[datetime],
    new_series: Callable[[str], Series],
) -> None:
    a, b = new_series("TESTHLA"), new_series("TESTHLB")
    a.unconfigured = True
    _heal(settings, db)
    _hl(settings, db, [a, b])
    # one more task of the same job, to see that the siblings wait with it
    queue_range(
        world,
        get_dataset(a.dataset),
        date(2026, 9, 1),
        date(2026, 9, 15),
        source=history_load.HISTORY_SOURCE,
    )
    world.commit()
    summary = _work(settings, db)
    # the skip is not a failed attempt: nothing failed, no attempt used, no night slot used
    assert summary.failed == 0 and summary.deferred == 1
    waiting = world.execute(
        "SELECT status, attempts, started_at, last_error, not_before > now() AS later "
        "FROM ops.backfill_task WHERE dataset = %s AND source = %s",
        (a.dataset, history_load.HISTORY_SOURCE),
    ).fetchall()
    assert len(waiting) == 2
    for r in waiting:
        assert r["status"] == "pending" and r["attempts"] == 0 and r["started_at"] is None
        assert r["last_error"].startswith(NOT_CONFIGURED) and r["later"]
    assert _tasks(world, b.dataset)[0]["status"] == "done"  # B's source is fine
    # A says it waits; it does not hold the reissue pass of B back
    out = _hl(settings, db, [a, b])
    row = _history_row(world, a.dataset)
    assert row["status"] == "loading" and "TEST_KEY not configured" in row["waiting_reason"]
    assert "waiting for the source" in row["note"] and row["load_round"] == 1
    status, note = _gaps_by_key(world, heal.HISTORY_GAPS)[a.dataset]
    assert status == "open" and "waiting for the source" in note
    assert out.result["history"]["pass_started"] == {"datasets": [b.dataset], "cuts": 3}
    assert _history_row(world, b.dataset)["status"] == "done"
    assert _cuts_with(world, b.code) == dict.fromkeys(QUOTED, True)
    assert _cuts_with(world, a.code) == dict.fromkeys(QUOTED, False)
    # the source is configured: the task is taken again after its wait; A gets a pass of its own
    a.unconfigured = False
    world.execute(
        "UPDATE ops.backfill_task SET not_before = now() - interval '1 minute' WHERE dataset = %s",
        (a.dataset,),
    )
    world.commit()
    assert _work(settings, db).done == 2
    out = _hl(settings, db, [a, b])
    assert out.result["history"]["pass_started"] == {"datasets": [a.dataset], "cuts": 3}
    row = _history_row(world, a.dataset)
    assert row["status"] == "done" and row["waiting_reason"] is None
    assert _cuts_with(world, a.code) == dict.fromkeys(QUOTED, True)
    assert all(len(v) == 3 for v in _eod_final(world).values())  # one reissue per pass


def test_a_skip_for_a_missing_source_is_told_apart_from_other_skips() -> None:
    assert str(SourceNotConfigured("X not configured")).startswith(NOT_CONFIGURED)
