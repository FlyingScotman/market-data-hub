"""``snapshot_heal`` (market-data-hub#138): the hub finds the business days without an
``eod_final`` cut and the old cuts built on the static of their build day, repairs them on its
own (night window, bounded per run), reports them as ``ops.data_gap`` rows, resumes after an
interruption and does nothing when nothing is left."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

import mdhub.loaders.snapshot_heal as heal
from mdhub.config import Settings, in_clock_window, parse_clock_window
from mdhub.core.bizday import DbCalendar
from mdhub.core.timeutil import MSK
from mdhub.datasets import datasets_for_job
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, discover_jobs, run_job
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.static import STATIC_BASIS_AS_OF_DATE
from test_api_seed import seed
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
    monkeypatch.setattr(heal, "_sleep", lambda _s: None)
    return clock


def _heal(settings: Settings, db: Database, **params: Any) -> JobOutcome:
    params = {"date_from": FROM.isoformat(), "min_bond_rows": 1, **params}
    # the database calendar, as in production (the Saturday 2026-10-17 of the seed is a session)
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
    no_quotes = (date(2026, 10, 12), date(2026, 10, 16), date(2026, 10, 17))  # 17: Saturday session
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
    assert _heal(settings, db).status == "skipped"  # never tried again


def test_a_long_window_uses_the_calendar_for_every_day(
    world: Conn, settings: Settings, db: Database
) -> None:
    """Holidays and weekend sessions far from the window start are honoured (a year of days is
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
    assert date(2026, 10, 17) in gaps  # Saturday session, no quotes
    assert date(2026, 6, 6) not in gaps and date(2026, 6, 8) in gaps  # a weekend, a Monday


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
    assert settings.snapshot_heal_from == date(2025, 10, 29)
    assert (settings.snapshot_heal_max_per_run, settings.snapshot_heal_pause_s) == (10, 20)
    assert parse_clock_window(settings.snapshot_heal_window) == (time(21, 0), time(6, 0))


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
