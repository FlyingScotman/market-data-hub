"""A bond whose price unit changes: no price is written with an unproven unit.

The chart answers in whatever unit it likes after a change (it is not known whether it restates
its history), so for the dates that may still be in the old unit nothing is written and stored
bars are not touched. The scenarios come from the review of the first version of this rule."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from mdhub.config import Settings
from mdhub.core.http import HttpStatusError
from mdhub.db.pool import Database
from mdhub.sources import spb
from test_spb_bonds import MGKL, XMED, Conn, MockSpb, _bytes, _encode, _error, _run

TICK = "MGKL1P1"
REAL_REPORT_DATE = date(2026, 11, 10)  # the date inside the unit-report fixture


def nov(n: int) -> date:
    return date(2026, 11, n)


def _bar(day: date, close: float) -> dict[str, Any]:
    stamp = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    return {"open": close, "high": close, "low": close, "close": close, "bar_unixtime": stamp}


class Charts(MockSpb):
    """Chart answers cut to the requested bounds, with an optional failure for one ticker."""

    def __init__(self) -> None:
        super().__init__()
        self.series: dict[str, list[dict[str, Any]]] = {}
        self.failing: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == spb.HISTORY_PATH:
            ticker = request.url.params["symbol"]
            if ticker in self.failing:
                self.requests.append(request)
                return httpx.Response(500, content=b"boom")
            if ticker in self.series:
                self.requests.append(request)
                lo, hi = (int(request.url.params[k]) for k in ("from", "to"))
                rows = [r for r in self.series[ticker] if lo <= r["bar_unixtime"] <= hi]
                return httpx.Response(
                    200, content=_encode(rows), headers={"content-type": "application/json"}
                )
        return super().handle(request)


@pytest.fixture
def source() -> Charts:
    src = Charts()
    src.report_payload = _bytes("unit-reference-2026-11-10.pdf")
    return src


Day = Callable[[date, date, str | None], None]


@pytest.fixture
def day(monkeypatch: pytest.MonkeyPatch, today_is: Callable[[date], None]) -> Day:
    """``day(today, newest report date, unit of MGKL1P1 in it)``; ``None``: the fixture's unit."""
    state: dict[str, Any] = {"report": nov(10), "unit": None}
    real_archive, real_units = spb.parse_report_archive, spb.parse_unit_report

    def archive(payload: bytes, *, today: date) -> tuple[date, str]:
        _, path = real_archive(payload, today=nov(11))
        return state["report"], path

    def units(
        payload: bytes, report_date: date, identities: dict[str, str], **kwargs: Any
    ) -> dict[tuple[str, str], tuple[str, str]]:
        out = real_units(payload, REAL_REPORT_DATE, identities, **kwargs)
        if state["unit"] is not None:
            key = next(k for k in out if k[0] == TICK)
            out[key] = (state["unit"], out[key][1])
        return out

    monkeypatch.setattr(spb, "parse_report_archive", archive)
    monkeypatch.setattr(spb, "parse_unit_report", units)

    def set_day(today: date, report: date, unit: str | None) -> None:
        today_is(today)
        state["report"], state["unit"] = report, unit

    return set_day


def _stored(conn: Conn, isin: str = MGKL) -> dict[date, tuple[str, str, date]]:
    """``trade_date -> (price unit, close, unit reference date)`` of every stored bar."""
    conn.rollback()
    return {
        r["trade_date"]: (r["price_unit"], f"{float(r['close']):g}", r["unit_reference_date"])
        for r in conn.execute(
            "SELECT trade_date, price_unit, close, unit_reference_date FROM md.spb_bond_daily "
            "WHERE isin=%s ORDER BY trade_date",
            (isin,),
        ).fetchall()
    }


def _units(conn: Conn) -> dict[int, str]:
    return {d.day: v[0] for d, v in _stored(conn).items()}


def test_s1_the_change_is_seen_the_next_morning(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    source.series[TICK] = [_bar(nov(n), 90.0 + n) for n in (5, 6, 9, 10)]
    day(nov(10), nov(9), None)
    first = _run(settings, db, source, date_from=nov(5), date_to=nov(9))
    assert first.status == "succeeded"
    # first load: every date takes the unit of the report, dated later than most of the bars
    assert {v[2] for v in _stored(conn).values()} == {nov(9)}
    day(nov(11), nov(10), "RUB")
    second = _run(settings, db, source)
    assert second.status == "partial"
    assert "price unit changed PCT->RUB for MGKL1P1" in _error(conn, second)
    assert _stored(conn) == {
        nov(5): ("PCT", "95", nov(9)),
        nov(6): ("PCT", "96", nov(9)),
        nov(9): ("PCT", "99", nov(9)),
        nov(10): ("RUB", "100", nov(10)),
    }


def test_s2_a_chart_restated_in_the_new_unit_never_overwrites_a_stored_bar(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    source.series[TICK] = [_bar(nov(5), 98.0), _bar(nov(6), 98.5), _bar(nov(9), 99.0)]
    day(nov(10), nov(9), None)
    assert _run(settings, db, source, date_from=nov(5), date_to=nov(9)).status == "succeeded"
    source.series[TICK] = [
        _bar(nov(5), 980.0),
        _bar(nov(6), 985.0),
        _bar(nov(9), 990.0),
        _bar(nov(10), 995.0),
    ]
    day(nov(11), nov(10), "RUB")
    _run(settings, db, source)
    again = _run(settings, db, source, date_from=nov(5), date_to=nov(10))  # and once more, widely
    assert again.status == "succeeded"
    assert _stored(conn) == {
        nov(5): ("PCT", "98", nov(9)),
        nov(6): ("PCT", "98.5", nov(9)),
        nov(9): ("PCT", "99", nov(9)),
        nov(10): ("RUB", "995", nov(10)),
    }


@pytest.mark.parametrize("hist_first", [True, False], ids=["s3b-hist-first", "s3-daily-first"])
def test_s3_the_outcome_does_not_depend_on_the_order_of_the_runs(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: Charts,
    day: Day,
    hist_first: bool,
) -> None:
    source.series[TICK] = [
        _bar(nov(2), 97.0),
        _bar(nov(3), 97.5),
        _bar(nov(5), 98.0),
        _bar(nov(9), 99.0),
        _bar(nov(10), 990.0),
    ]
    day(nov(10), nov(9), None)
    assert _run(settings, db, source, date_from=nov(5), date_to=nov(9)).status == "succeeded"
    day(nov(11), nov(10), "RUB")
    if hist_first:
        _run(settings, db, source, date_from=nov(2), date_to=nov(3))
        _run(settings, db, source)
    else:
        _run(settings, db, source)  # rewrites ref.spb_bond with the new unit
        hist = _run(settings, db, source, date_from=nov(2), date_to=nov(3))
        assert hist.status == "succeeded"  # nothing is written, and that is not a failure
    # 2 and 3 November (older than the stored bars, possibly in either unit): not written
    assert _stored(conn) == {
        nov(5): ("PCT", "98", nov(9)),
        nov(9): ("PCT", "99", nov(9)),
        nov(10): ("RUB", "990", nov(10)),
    }


def test_s4_back_and_forth_keeps_every_stored_bar_with_its_unit(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    source.series[TICK] = [_bar(nov(n), 90.0 + n) for n in (2, 3, 5, 6, 9, 10, 11, 12)]
    day(nov(10), nov(9), None)
    _run(settings, db, source, date_from=nov(2), date_to=nov(9))
    day(nov(11), nov(10), "RUB")
    assert _run(settings, db, source).status == "partial"
    day(nov(12), nov(11), None)  # back to the percent of par
    back = _run(settings, db, source)
    assert back.status == "partial"  # the run that saw the second change
    day(nov(13), nov(12), None)
    assert _run(settings, db, source).status == "succeeded"
    assert _units(conn) == {2: "PCT", 3: "PCT", 5: "PCT", 6: "PCT", 9: "PCT"} | {
        10: "RUB",
        11: "PCT",
        12: "PCT",
    }
    assert _stored(conn)[nov(10)][1] == "100"  # the RUB bar is the one read under RUB


def test_s5_a_failure_between_the_reference_and_the_bars_changes_nothing(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    source.series[TICK] = [_bar(nov(n), 90.0 + n) for n in (2, 3, 5, 6, 9, 10)]
    day(nov(10), nov(9), None)
    _run(settings, db, source, date_from=nov(5), date_to=nov(9))
    day(nov(11), nov(10), "RUB")
    source.failing = {TICK}
    with pytest.raises(HttpStatusError):  # ref.spb_bond is already RUB, no bar written
        _run(settings, db, source)
    conn.rollback()
    ref = conn.execute("SELECT price_unit FROM ref.spb_bond WHERE isin=%s", (MGKL,)).fetchone()
    assert ref == {"price_unit": "RUB"}
    source.failing = set()
    retry = _run(settings, db, source)
    assert retry.status == "partial"  # still no bar of the new unit: still reported
    hist = _run(settings, db, source, date_from=nov(2), date_to=nov(3))
    assert hist.status == "succeeded"
    assert _stored(conn) == {
        nov(5): ("PCT", "95", nov(9)),
        nov(6): ("PCT", "96", nov(9)),
        nov(9): ("PCT", "99", nov(9)),
        nov(10): ("RUB", "100", nov(10)),
    }


def test_s6_the_window_after_an_outage_is_not_written(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    """Last bar of the old unit on 9 November, new unit confirmed on 19 November: when exactly it
    changed is unknown, so 10..18 November get no price."""
    source.series[TICK] = [_bar(nov(n), 90.0 + n) for n in (5, 9, 10, 11, 12, 18, 19)]
    day(nov(10), nov(9), None)
    _run(settings, db, source, date_from=nov(5), date_to=nov(9))
    day(nov(20), nov(19), "RUB")
    outage = _run(settings, db, source)  # reads from the day after the newest stored bar
    assert (outage.result["from"], outage.result["to"]) == ("2026-11-10", "2026-11-19")
    assert outage.status == "partial"
    assert outage.result["unit_frozen"][MGKL] == {
        "ticker": TICK,
        "stored_unit": "PCT",
        "current_unit": "RUB",
        "frozen_through": "2026-11-09",
        "new_unit_from": "2026-11-19",
        "dates_not_written": 4,  # 10, 11, 12 and 18 November
    }
    assert _units(conn) == {5: "PCT", 9: "PCT", 19: "RUB"}
    # later runs, narrow or wide, keep the same boundary and stay quiet
    day(nov(21), nov(20), "RUB")
    wide = _run(settings, db, source, date_from=nov(8), date_to=nov(20))
    assert wide.status == "succeeded"
    assert _units(conn) == {5: "PCT", 9: "PCT", 19: "RUB"}


def test_s7_a_bar_dated_after_its_report_is_not_relabelled(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    """On 11 November the newest report is the one of 9 November; the bar of 10 November is stored
    under it. The report of 11 November says RUB: the stored bar stays as it was."""
    source.series[TICK] = [_bar(nov(9), 99.0), _bar(nov(10), 99.5)]
    day(nov(11), nov(9), None)
    _run(settings, db, source)
    before = _stored(conn)
    assert before[nov(10)] == ("PCT", "99.5", nov(9))
    source.series[TICK] = [_bar(nov(9), 990.0), _bar(nov(10), 995.0), _bar(nov(11), 1000.0)]
    day(nov(12), nov(11), "RUB")
    _run(settings, db, source)
    assert _stored(conn) == {**before, nov(11): ("RUB", "1000", nov(11))}


def test_s8_a_card_that_disagrees_with_the_report_is_a_flag_not_a_unit(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    day(nov(11), nov(10), None)
    _run(settings, db, source, day=nov(10))
    conn.rollback()
    row = conn.execute(
        "SELECT price_unit, card_price_unit, unit_conflict FROM md.spb_bond_daily WHERE isin=%s",
        (XMED,),
    ).fetchone()
    assert row == {"price_unit": "PCT", "card_price_unit": "RUB", "unit_conflict": True}


def test_s9_a_later_historical_run_is_quiet_and_changes_nothing(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    source.series[TICK] = [_bar(nov(n), 90.0 + n) for n in (5, 6, 9, 10, 11)]
    day(nov(10), nov(9), None)
    _run(settings, db, source, date_from=nov(5), date_to=nov(9))
    day(nov(11), nov(10), None)
    _run(settings, db, source)
    day(nov(12), nov(11), "RUB")
    changed = _run(settings, db, source)
    assert changed.status == "partial"
    after = _stored(conn)
    assert [after[nov(n)][0] for n in (9, 10, 11)] == ["PCT", "PCT", "RUB"]
    day(nov(20), nov(19), "RUB")
    for _ in range(2):  # ten days later, twice
        hist = _run(settings, db, source, date_from=nov(5), date_to=nov(10))
        assert hist.status == "succeeded"
        assert hist.result["unit_frozen"][MGKL]["dates_not_written"] == 4  # 5, 6, 9, 10
        assert hist.result["unit_changes"][TICK].startswith("price unit changed PCT->RUB")
    assert _stored(conn) == after


def test_s10_a_skipped_chart_on_the_day_of_the_change_is_still_reported_next_day(
    settings: Settings, db: Database, conn: Conn, source: Charts, day: Day
) -> None:
    good = [_bar(nov(n), 90.0 + n) for n in (2, 3, 5, 6, 9, 10, 11)]
    source.series[TICK] = good
    day(nov(10), nov(9), None)
    _run(settings, db, source, date_from=nov(5), date_to=nov(9))
    day(nov(11), nov(10), "RUB")
    source.series.pop(TICK)
    source.chart_payload[TICK] = {"error": "format changed"}  # this bond's chart is skipped
    skipped = _run(settings, db, source)
    assert skipped.status == "partial" and TICK in skipped.result["skipped"]
    source.chart_payload.pop(TICK)
    source.series[TICK] = good
    day(nov(12), nov(11), "RUB")
    next_day = _run(settings, db, source)
    assert next_day.status == "partial"  # no bar of the new unit stored yet
    assert "price unit changed PCT->RUB for MGKL1P1" in _error(conn, next_day)
    # the new unit is proven from the report of the first successful run: 10 November, which
    # only the failed run had seen, is not written (safe, if lossy)
    assert _units(conn) == {5: "PCT", 6: "PCT", 9: "PCT", 11: "RUB"}
    day(nov(13), nov(12), "RUB")
    assert _run(settings, db, source).status == "succeeded"
    assert _stored(conn)[nov(11)][1] == "101"
