"""Gap calendars of the fixing datasets (market-data-hub#79): ``cbr_ruonia`` expects RUONIA on
``CBR_RUONIA`` days, ``indices_rusfar`` / ``indices_rusfarrt`` expect RUSFAR on ``MOEX_RUSFAR``
days (contract 1.6 derived calendars), so the year-end and the MOEX trading non-working days are
no gaps any more; days the derived calendars do not cover (before 2019) take the base calendar
(``CALENDAR_FALLBACK``), never Monday-Friday."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

import mdhub.loaders.cbr  # registers cbr_ruonia
import mdhub.loaders.indices  # noqa: F401 - registers indices_rusfar*
from mdhub.cli_ext.report import EffectiveCalendar
from mdhub.core.bizday import CALENDAR_FALLBACK, CALENDARS, DbCalendar, calendar_chain
from mdhub.core.gaps import db_business_days
from mdhub.core.timeutil import utc_now
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.loaders import calendar as cal
from mdhub.quality.checks import scan_dataset, sync_gaps

Conn = psycopg.Connection[DictRow]
TODAY = date(2026, 1, 20)


def _days(lo: date, hi: date) -> list[date]:
    return [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]


def _flags(lo: date, hi: date, closed: Iterable[date] = ()) -> dict[date, bool]:
    shut = set(closed)
    return {d: d.weekday() < 5 and d not in shut for d in _days(lo, hi)}


def _write(conn: Conn, calendar: str, source: str, flags: dict[date, bool]) -> None:
    cal.write_calendar_days(conn, calendar, source, flags, known_at=utc_now(), run_id=None)


# 2018 (before the derived calendars): New-Year holidays Jan 1-8 (RU), MOEX closed Jan 1-2
OLD = (date(2017, 12, 25), date(2018, 1, 19))
RU_2018 = _flags(*OLD, _days(date(2018, 1, 1), date(2018, 1, 8)))
STOCK_2018 = _flags(*OLD, [date(2018, 1, 1), date(2018, 1, 2)])
# 2025/26: RU holidays Dec 31 - Jan 11; MOEX trades Jan 5-9 (trading non-working days)
NEW = (date(2025, 12, 1), date(2026, 1, 31))
RU_NEW = _flags(*NEW, _days(date(2025, 12, 31), date(2026, 1, 11)))
STOCK_NEW = _flags(*NEW, [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2)])
YEAR_END_2025 = date(2025, 12, 30)  # last RU business day and last MOEX trading day of 2025
TRADING_HOLIDAYS = _days(date(2026, 1, 5), date(2026, 1, 9))


def _calendars(conn: Conn) -> None:
    _write(conn, "RU_PRODUCTION", "curated", RU_2018 | RU_NEW)
    _write(conn, "MOEX_STOCK", "observed_moex_stock", STOCK_2018 | STOCK_NEW)
    ruonia = dict(RU_NEW)
    ruonia[YEAR_END_2025] = False
    rusfar = {d: RU_NEW[d] and STOCK_NEW[d] and d.weekday() < 5 for d in STOCK_NEW}
    rusfar[YEAR_END_2025] = False
    _write(conn, "CBR_RUONIA", "rule_cbr_ruonia", ruonia)
    _write(conn, "MOEX_RUSFAR", "rule_moex_rusfar", rusfar)


def test_fallback_chain_is_the_derived_calendars_base() -> None:
    assert set(CALENDAR_FALLBACK) == set(cal.DERIVED_CALENDARS)
    bases = {spec.calendar: spec.bases for spec in cal.DERIVED_RULES}
    for derived, base in CALENDAR_FALLBACK.items():
        assert base in bases[derived] and base in CALENDARS
    assert calendar_chain("CBR_RUONIA") == ("CBR_RUONIA", "RU_PRODUCTION")
    assert calendar_chain("MOEX_RUSFAR") == ("MOEX_RUSFAR", "MOEX_STOCK")
    assert calendar_chain("MOEX_STOCK") == ("MOEX_STOCK",)


def test_dataset_calendars() -> None:
    assert get_dataset("cbr_ruonia").calendar == "CBR_RUONIA"
    assert get_dataset("indices_rusfar").calendar == "MOEX_RUSFAR"
    assert get_dataset("indices_rusfarrt").calendar == "MOEX_RUSFAR"
    assert get_dataset("indices_bonds").calendar == "MOEX_STOCK"
    assert get_dataset("indices_all").calendar == "MOEX_STOCK"
    assert get_dataset("cbr_key_rate").calendar == "RU_PRODUCTION"


def test_predicates_fall_back_to_the_base_calendar(conn: Conn, db: Database) -> None:
    _calendars(conn)
    conn.commit()
    holiday_2018 = date(2018, 1, 3)  # a weekday: Monday-Friday would expect it
    dbcal = DbCalendar(db, ttl=0)
    report = EffectiveCalendar.load(conn)
    pred = db_business_days(conn, "CBR_RUONIA", date(2018, 1, 1), date(2018, 1, 31))
    for check in (lambda d: dbcal(d, "CBR_RUONIA"), lambda d: report(d, "CBR_RUONIA"), pred):
        assert check(holiday_2018) is False  # RU_PRODUCTION (no CBR_RUONIA row)
        assert check(date(2018, 1, 9)) is True
    new = db_business_days(conn, "CBR_RUONIA", date(2025, 12, 1), date(2026, 1, 31))
    for check in (lambda d: dbcal(d, "CBR_RUONIA"), lambda d: report(d, "CBR_RUONIA"), new):
        assert check(YEAR_END_2025) is False  # the derived row wins over RU_PRODUCTION
    assert dbcal(YEAR_END_2025, "RU_PRODUCTION") is True
    assert dbcal(date(2018, 1, 2), "MOEX_RUSFAR") is False  # MOEX_STOCK
    assert dbcal(date(2026, 1, 6), "MOEX_RUSFAR") is False
    assert dbcal(date(2026, 1, 6), "MOEX_STOCK") is True
    assert dbcal(date(2030, 1, 7), "MOEX_RUSFAR") is True  # no row anywhere: Monday-Friday


def _expected_gap(conn: Conn, dataset: str, key: dict[str, str], day: date, status: str) -> None:
    conn.execute(
        "INSERT INTO ops.data_gap (dataset, key, gap_date, status, note) "
        "VALUES (%s, %s, %s, %s, 'test')",
        (dataset, Jsonb(key), day, status),
    )


def _gap_status(conn: Conn, dataset: str, day: date) -> str | None:
    row = conn.execute(
        "SELECT status FROM ops.data_gap WHERE dataset = %s AND gap_date = %s", (dataset, day)
    ).fetchone()
    return None if row is None else str(row["status"])


def test_cbr_ruonia_gaps_follow_cbr_ruonia(conn: Conn, db: Database) -> None:
    _calendars(conn)
    missing = date(2026, 1, 14)  # a publication day without a fixing stays a gap
    published = [d for d, f in RU_2018.items() if f] + [
        d for d, f in RU_NEW.items() if f and d not in (YEAR_END_2025, missing) and d < TODAY
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
            "VALUES ('RUONIA', %s, 'cbr', 16)",
            [(d,) for d in published],
        )
    # production state: the year-end was an unfillable gap every year
    _expected_gap(conn, "cbr_ruonia", {}, YEAR_END_2025, "unfillable")
    conn.commit()
    spec = get_dataset("cbr_ruonia")
    scan = scan_dataset(conn, spec, today=TODAY, calendar=DbCalendar(db, ttl=0))
    gaps = {g.day for g in scan.gaps}
    assert YEAR_END_2025 not in gaps
    assert missing in gaps
    assert not gaps & set(_days(*OLD))  # 2018 holidays: RU_PRODUCTION, not Monday-Friday
    assert not gaps & set(_days(*NEW)) - {missing}
    sync_gaps(conn, spec, scan, run_id=None)
    assert _gap_status(conn, "cbr_ruonia", YEAR_END_2025) == "not_expected"
    assert _gap_status(conn, "cbr_ruonia", missing) == "open"


def test_indices_rusfar_gaps_follow_moex_rusfar(conn: Conn, db: Database) -> None:
    _calendars(conn)
    calc = {d for d in STOCK_NEW if dbcal_flag(conn, d)}
    traded_2018 = [d for d, f in STOCK_2018.items() if f]
    days = [*traded_2018, *(d for d in sorted(calc) if d < TODAY)]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO md.index_eod (trade_date, secid, close, basis) "
            "VALUES (%s, %s, 16, 'history')",
            [(d, s) for d in days for s in ("RUSFAR", "RUSFARRT")],
        )
    for d in (YEAR_END_2025, date(2026, 1, 6)):
        _expected_gap(conn, "indices_rusfar", {"secid": "RUSFAR"}, d, "unfillable")
    conn.commit()
    dbcal = DbCalendar(db, ttl=0)
    for name in ("indices_rusfar", "indices_rusfarrt"):
        spec = get_dataset(name)
        scan = scan_dataset(conn, spec, today=TODAY, calendar=dbcal)
        gaps = {g.day for g in scan.gaps}
        assert YEAR_END_2025 not in gaps
        assert not gaps & set(TRADING_HOLIDAYS)
        assert not gaps & set(_days(*OLD))  # 2018: MOEX_STOCK
        assert not gaps & set(_days(*NEW)), (name, sorted(gaps & set(_days(*NEW))))
        sync_gaps(conn, spec, scan, run_id=None)
    assert _gap_status(conn, "indices_rusfar", YEAR_END_2025) == "not_expected"
    assert _gap_status(conn, "indices_rusfar", date(2026, 1, 6)) == "not_expected"
    # the same days are still MOEX_STOCK trading days for the other index datasets
    bonds = get_dataset("indices_bonds")
    assert bonds.calendar is not None and dbcal(date(2026, 1, 6), bonds.calendar) is True


def dbcal_flag(conn: Conn, day: date) -> bool:
    row = conn.execute(
        "SELECT is_business_day FROM ref.calendar_effective "
        "WHERE calendar = 'MOEX_RUSFAR' AND day = %s",
        (day,),
    ).fetchone()
    assert row is not None
    return bool(row["is_business_day"])
