"""Market Snapshot Contract 1.6 (additive; TDM brief H17, market-data-hub#38): the calendars of
RUB swap and fixing conventions in the static snapshot - ``MOEX_SPFI`` (MOEX standardised
derivatives market trading days), ``CBR_RUONIA`` (days for which the Bank of Russia publishes
RUONIA) and ``MOEX_RUSFAR`` (calculation days of the RUB overnight RUSFAR), derived from the
effective base calendars by documented rules plus curated exceptions."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
import respx
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.bizday import CALENDARS, DbCalendar
from mdhub.core.errors import FormatDriftError
from mdhub.core.timeutil import MSK, date_range, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import calendar as cal
from mdhub.snapshot.runner import publish_static_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION
from test_api_seed import seed
from test_calendar import _http_factory, iss  # noqa: F401 - pytest fixture

Conn = psycopg.Connection[DictRow]
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_version() -> None:
    assert SCHEMA_VERSION == "market_snapshot/1.6"
    assert {"MOEX_SPFI", "CBR_RUONIA", "MOEX_RUSFAR"} <= set(CALENDARS)
    assert cal.DERIVED_CALENDARS == ("MOEX_SPFI", "CBR_RUONIA", "MOEX_RUSFAR")


# ---------------------------------------------------------------------- the data file


def test_derived_file_is_valid() -> None:
    rules = cal.load_derived()
    assert rules.known_at.tzinfo is not None and rules.version
    assert rules.ruonia_year_end_decided_through == 2025
    assert rules.rusfar_year_end_from == 2021
    for c in cal.DERIVED_CALENDARS:
        assert "http" in rules.references[c]  # every rule cites its primary source
    ruonia = {o.day: o.is_business_day for o in rules.overrides["CBR_RUONIA"]}
    assert ruonia == {
        **{date(2020, 3, 30) + timedelta(days=i): False for i in range(5)},
        date(2020, 6, 24): False,
    }
    rusfar = {o.day: o.is_business_day for o in rules.overrides["MOEX_RUSFAR"]}
    assert rusfar[date(2019, 4, 30)] is False and rusfar[date(2022, 3, 9)] is True
    assert sum(rusfar.values()) == 13  # 28.02-18.03.2022: money market open, indices closed
    assert rules.overrides["MOEX_SPFI"] == ()


def _file() -> dict[str, Any]:
    text = (cal.resources.files("mdhub.data") / cal.DERIVED_RESOURCE).read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format=2),
        lambda d: d.update(known_at="2026-10-23T00:00:00"),
        lambda d: d["calendars"].pop("MOEX_SPFI"),
        lambda d: d["calendars"]["CBR_RUONIA"].pop("year_end_decided_through"),
        lambda d: d["calendars"]["CBR_RUONIA"]["overrides"].append(
            d["calendars"]["CBR_RUONIA"]["overrides"][0]
        ),
        lambda d: d["calendars"]["MOEX_RUSFAR"]["overrides"][0].update(is_business_day="no"),
        lambda d: d["calendars"]["MOEX_RUSFAR"]["overrides"][0].update(reference=""),
        lambda d: d["calendars"]["MOEX_RUSFAR"]["overrides"][0].update(day="2019-02-30"),
    ],
)
def test_derived_file_validation(mutate: Any) -> None:
    data = _file()
    mutate(data)
    with pytest.raises(FormatDriftError):
        cal.load_derived(json.dumps(data))


# ------------------------------------------------------------------------- the rules


def _ru(*years: int) -> cal.Flags:
    out: cal.Flags = {}
    for y in cal.load_curated().years:
        if y.calendar == "RU_PRODUCTION" and y.year in years:
            out.update({d: (f, y.source == "curated") for d, f in y.days().items()})
    return out


def _moex_2024() -> cal.Flags:
    """MOEX trading days of 2024 (MOEX holiday schedules: closed on Jan 1, 2, 7, Feb 23, Mar 8,
    May 1, 9, Jun 12, Nov 4, Dec 31; trading on the other official non-working weekdays, e.g.
    Apr 29-30, May 10, Dec 30, and on the working Saturdays Apr 27, Nov 2, Dec 28)."""
    closed = {
        date(2024, m, d)
        for m, d in [(1, 1), (1, 2), (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4), (12, 31)]
    }
    sat = {date(2024, 4, 27), date(2024, 11, 2), date(2024, 12, 28)}
    return {
        d: ((d.weekday() < 5 and d not in closed) or d in sat, True)
        for d in date_range(date(2024, 1, 1), date(2024, 12, 31))
    }


def test_ruonia_publication_days() -> None:
    days = cal.ruonia_publication_days(_ru(2024, 2025, 2026, 2028), decided_through=2025)
    # the last business day of the year: decided (never published) through 2025
    assert days[date(2024, 12, 28)] == (False, True)  # a working Saturday
    assert days[date(2024, 12, 27)] == (True, True)
    assert days[date(2025, 12, 30)] == (False, True)
    assert days[date(2025, 11, 1)] == (True, True)  # working Saturday: RUONIA is published
    assert days[date(2025, 12, 31)] == (False, True)  # decree transfer: a holiday anyway
    # 2026: projected, provisional until the Bank of Russia decides
    assert days[date(2026, 12, 30)] == (False, False)
    assert days[date(2026, 12, 29)] == (True, True)
    assert days[date(2026, 5, 11)] == (False, True)  # non-working day although MOEX trades
    # an unverified RU_PRODUCTION year stays provisional
    assert days[date(2028, 3, 1)] == (True, False)
    assert min(days) == date(2024, 1, 1) and max(days) == date(2028, 12, 31)
    # a year the base does not cover in full is left out (its year-end is unknown)
    part = {d: v for d, v in _ru(2026).items() if d < date(2026, 12, 1)}
    assert cal.ruonia_publication_days(part, decided_through=2025) == {}


def test_rusfar_calculation_days() -> None:
    days = cal.rusfar_calculation_days(_ru(2024), _moex_2024(), year_end_from=2021)
    assert days[date(2024, 12, 27)] == (True, True)  # the last weekday before the year-end
    assert days[date(2024, 12, 30)] == (False, True)  # last trading day; non-working day too
    assert days[date(2024, 12, 28)] == (False, True)  # trading Saturday
    assert days[date(2024, 4, 27)] == (False, True)  # trading Saturday
    assert days[date(2024, 4, 29)] == (False, True)  # trading non-working day
    assert days[date(2024, 5, 1)] == (False, True)  # no trading
    assert days[date(2024, 10, 15)] == (True, True)
    # before year_end_from the last trading day is calculated (RUSFAR 30.12.2019, 30.12.2020)
    early = cal.rusfar_calculation_days(_ru(2024), _moex_2024(), year_end_from=2025)
    assert early[date(2024, 12, 30)] == (False, True)  # still a non-working day
    moex26 = next(
        y for y in cal.load_curated().years if (y.calendar, y.year) == ("MOEX_STOCK", 2026)
    )
    stock26 = {d: (f, True) for d, f in moex26.days().items()}
    days26 = cal.rusfar_calculation_days(_ru(2026), stock26, year_end_from=2021)
    assert days26[date(2026, 1, 5)] == (False, True)  # MOEX trades, a non-working day
    assert days26[date(2026, 12, 30)] == (False, True)  # the last trading day of 2026
    assert days26[date(2026, 12, 29)] == (True, True)
    assert days26[date(2026, 3, 6)] == (True, True)  # before the trading non-working Mar 9
    # a provisional MOEX year makes its days and its year-end provisional
    prov = {d: (f, d.month < 12) for d, (f, _ok) in stock26.items()}
    days_p = cal.rusfar_calculation_days(_ru(2026), prov, year_end_from=2021)
    assert days_p[date(2026, 6, 1)] == (True, True)
    assert days_p[date(2026, 12, 30)] == (False, False)


def test_spfi_days_follow_moex_forts() -> None:
    forts = {date(2026, 5, 11): (True, True), date(2027, 1, 4): (True, False)}
    assert cal.spfi_days(forts) == forts


# ------------------------------------------------------------------- refresh (database)


def _bases(conn: Conn) -> None:
    """Base calendars of 2025-2026: RU_PRODUCTION from the curated file, MOEX_STOCK /
    MOEX_FORTS = the curated MOEX 2026 year plus 2025 from the official RU days and the MOEX
    trading non-working days (2027 not stored: years outside the base are not derived)."""
    for y in cal.load_curated().years:
        if y.year in (2025, 2026) and y.calendar == "RU_PRODUCTION":
            cal.write_calendar_days(conn, y.calendar, "curated", y.days(), known_at=T0, run_id=None)
        if y.year == 2026 and y.calendar in ("MOEX_STOCK", "MOEX_FORTS"):
            days = y.days()
            src = "curated_unverified" if y.calendar == "MOEX_FORTS" else "curated"
            cal.write_calendar_days(conn, y.calendar, src, days, known_at=T0, run_id=None)
    ru25 = next(
        y for y in cal.load_curated().years if (y.calendar, y.year) == ("RU_PRODUCTION", 2025)
    )
    moex25 = {d: f or d in {date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 8)}
              for d, f in ru25.days().items()}  # fmt: skip
    for c in ("MOEX_STOCK", "MOEX_FORTS"):
        cal.write_calendar_days(conn, c, "observed_x", moex25, known_at=T0, run_id=None)
    conn.commit()


def _write_all(db: Database) -> cal.UpsertResult:
    total = cal.UpsertResult()
    rules = cal.load_derived()
    with db.connection() as c:
        for spec in cal.DERIVED_RULES:
            total += cal.write_derived_calendar(
                c, spec, rules, start=date(2019, 1, 1), end=date(2037, 12, 31), run_id=None
            )
    return total


def test_write_derived_calendars(conn: Conn, db: Database) -> None:
    _bases(conn)
    first = _write_all(db)
    assert first.inserted > 0
    rows = conn.execute(
        "SELECT calendar, source, min(day) AS lo, max(day) AS hi, count(*) AS n "
        "FROM ref.calendar_day WHERE calendar IN ('MOEX_SPFI', 'CBR_RUONIA', 'MOEX_RUSFAR') "
        "AND NOT is_deleted GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    assert rows == [
        {"calendar": "CBR_RUONIA", "source": "rule_cbr_ruonia", "lo": date(2025, 1, 1),
         "hi": date(2026, 12, 31), "n": 729},
        {"calendar": "CBR_RUONIA", "source": "rule_cbr_ruonia_unverified",
         "lo": date(2026, 12, 30), "hi": date(2026, 12, 30), "n": 1},
        {"calendar": "MOEX_RUSFAR", "source": "rule_moex_rusfar", "lo": date(2025, 1, 1),
         "hi": date(2026, 12, 31), "n": 730},
        {"calendar": "MOEX_SPFI", "source": "rule_moex_spfi", "lo": date(2025, 1, 1),
         "hi": date(2025, 12, 31), "n": 365},
        {"calendar": "MOEX_SPFI", "source": "rule_moex_spfi_unverified", "lo": date(2026, 1, 1),
         "hi": date(2026, 12, 31), "n": 365},
    ]  # fmt: skip
    # overrides are written only inside the derived range (2019-2022 days are outside here)
    cur = conn.execute(
        "SELECT count(*) AS n FROM ref.calendar_day WHERE source = 'curated' "
        "AND calendar IN ('CBR_RUONIA', 'MOEX_RUSFAR')"
    ).fetchone()
    assert cur == {"n": 0}
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2025, 12, 30), "CBR_RUONIA") is False
    assert bc(date(2025, 12, 30), "RU_PRODUCTION") is True
    assert bc(date(2025, 12, 30), "MOEX_RUSFAR") is False
    assert bc(date(2025, 12, 29), "MOEX_RUSFAR") is True
    assert bc(date(2025, 1, 3), "MOEX_SPFI") is True  # holiday trading day of MOEX
    assert bc(date(2025, 1, 3), "MOEX_RUSFAR") is False
    eff = conn.execute(
        "SELECT source FROM ref.calendar_effective WHERE calendar = 'CBR_RUONIA' "
        "AND day = '2026-12-30'"
    ).fetchone()
    assert eff == {"source": "rule_cbr_ruonia_unverified"}
    assert conn.execute(
        "SELECT ref.calendar_source_rank('rule_cbr_ruonia_unverified') AS r"
    ).fetchone() == {"r": 5}

    # idempotent
    again = _write_all(db)
    assert (again.inserted, again.updated) == (0, 0)

    # the base becomes verified: the provisional rows move to the verified source
    conn.execute(
        "UPDATE ref.calendar_day SET source = 'curated' "
        "WHERE calendar = 'MOEX_FORTS' AND source = 'curated_unverified'"
    )
    conn.commit()
    _write_all(db)
    spfi = conn.execute(
        "SELECT source, count(*) AS n FROM ref.calendar_day WHERE calendar = 'MOEX_SPFI' "
        "AND NOT is_deleted GROUP BY 1"
    ).fetchall()
    assert spfi == [{"source": "rule_moex_spfi", "n": 730}]


def test_refresh_derives_and_crosschecks_the_fixing_calendars(
    iss: respx.MockRouter,  # noqa: F811
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 1, 20))
    for d in date_range(date(2025, 12, 22), date(2025, 12, 30)):
        if d.weekday() < 5:  # RUONIA also on the year-end 30.12.2025: a calendar error
            conn.execute(
                "INSERT INTO md.fixing (index_code, fixing_date, source, value)"
                " VALUES ('RUONIA', %s, 'cbr', 16)",
                (d,),
            )
    conn.execute(
        "INSERT INTO md.index_eod (trade_date, secid, board, close, basis) "
        "VALUES ('2025-12-30', 'RUSFAR', 'RTSI', 16, 'history')"
    )
    conn.commit()
    out = run_job(
        "cbr_meetings",
        {"mode": "calendar_full"},
        settings=settings,
        db=db,
        http_factory=_http_factory,
    )
    assert out.status == "partial", out.result
    summary = out.result["calendar"]
    for spec in cal.DERIVED_RULES:
        assert summary[spec.source] != "failed"
    check = summary["fixing_crosscheck"]
    assert check["CBR_RUONIA"]["fixings_on_non_calculation_days"] == ["2025-12-30"]
    assert check["MOEX_RUSFAR"]["fixings_on_non_calculation_days"] == ["2025-12-30"]
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert err is not None and "CBR_RUONIA cross-check" in err["error"]
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2025, 12, 30), "CBR_RUONIA") is False  # the calendar is not changed
    assert bc(date(2025, 12, 29), "CBR_RUONIA") is True
    # the curated exceptions inside the derived range outrank the rule
    assert bc(date(2020, 6, 24), "RU_PRODUCTION") is True
    assert bc(date(2020, 6, 24), "CBR_RUONIA") is False
    assert bc(date(2022, 3, 9), "MOEX_RUSFAR") is True  # MOEX_STOCK (mock): no trading
    src = conn.execute(
        "SELECT calendar, source, count(*) AS n FROM ref.calendar_effective "
        "WHERE source = 'curated' AND calendar IN ('CBR_RUONIA', 'MOEX_RUSFAR') GROUP BY 1, 2 "
        "ORDER BY 1"
    ).fetchall()
    assert src == [
        {"calendar": "CBR_RUONIA", "source": "curated", "n": 6},
        {"calendar": "MOEX_RUSFAR", "source": "curated", "n": 15},
    ]
    lo = conn.execute(
        "SELECT calendar, min(day) AS lo, max(day) AS hi FROM ref.calendar_effective "
        "WHERE calendar IN ('MOEX_SPFI', 'CBR_RUONIA', 'MOEX_RUSFAR') GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert lo == [
        {"calendar": c, "lo": date(2019, 1, 1), "hi": date(2037, 12, 31)}
        for c in ("CBR_RUONIA", "MOEX_RUSFAR", "MOEX_SPFI")
    ]
    # an incremental refresh writes nothing new
    again = run_job(
        "cbr_meetings", {"mode": "calendar"}, settings=settings, db=db, http_factory=_http_factory
    )
    for spec in cal.DERIVED_RULES:
        assert again.result["calendar"][spec.source]["written"] == 0


# -------------------------------------------------------------------- static snapshot


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def test_static_snapshot_lists_the_fixing_calendars(conn: Conn, db: Database) -> None:
    seed(conn)
    _bases(conn)
    _write_all(db)
    out = publish_static_snapshot(
        db,
        cut_type="daily",
        business_date=date(2026, 10, 15),
        cut_ts=datetime(2026, 10, 15, 7, 45, tzinfo=MSK),
        known_at=utc_now(),
    )
    doc = _bundle(conn, out.id)  # the runner validates the bundle against the schema
    assert doc["schema_version"] == "market_snapshot/1.6"
    cals = {c["calendar"]: c for c in doc["data"]["calendars"]}
    ruonia = cals["CBR_RUONIA"]
    assert (ruonia["valid_from"], ruonia["valid_to"]) == ("2025-01-01", "2026-12-31")
    assert "2025-12-30" in ruonia["holidays"] and "2026-12-30" in ruonia["holidays"]
    assert ruonia["provisional_from"] == "2026-12-30"
    assert ruonia["sources"] == ["rule_cbr_ruonia", "rule_cbr_ruonia_unverified"]
    rusfar = cals["MOEX_RUSFAR"]
    assert {"2025-01-03", "2025-12-30", "2026-12-30"} <= set(rusfar["holidays"])
    assert rusfar["provisional_from"] is None
    assert cals["MOEX_SPFI"]["provisional_from"] == "2026-01-01"  # the MOEX_FORTS base
    missing = next(d for d in doc["datasets"] if d["name"] == "calendars")["coverage"]["missing"]
    assert not {"MOEX_SPFI", "CBR_RUONIA", "MOEX_RUSFAR"} & set(missing)
