"""``mdhub report seed``: read-only integrity report."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub import cli
from mdhub.cli_ext.report import EffectiveCalendar, build_seed_report, dataset_coverage
from mdhub.config import Settings
from mdhub.datasets import DatasetSpec, register_dataset, unregister_dataset

Conn = psycopg.Connection[DictRow]
runner = CliRunner()

TODAY = date(2026, 1, 12)  # Monday
GENERATED = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

PLAIN = DatasetSpec(
    name="zz_report_plain",
    table="md.report_test",
    date_column="trade_date",
    calendar="MOEX_STOCK",
    frequency="business_daily",
    max_lag_days=0,
    description="report test, whole table",
    history_start=date(2026, 1, 5),
    min_rows_per_day=2,
)
SERIES = DatasetSpec(
    name="zz_report_series",
    table="md.report_test",
    date_column="trade_date",
    calendar="MOEX_STOCK",
    frequency="business_daily",
    max_lag_days=0,
    description="report test, per series",
    history_start=date(2026, 1, 5),
    series_column="secid",
    series=("A", "B", "C"),
)

# 2026-01-07 (Wed) is a holiday, 2026-01-10 (Sat) a working day (observed beats curated).
# Expected days 01-05..01-12: 5, 6, 8, 9, 10, 12.
ROWS = {
    date(2026, 1, 5): ["A", "B"],
    date(2026, 1, 6): ["A", "B"],
    date(2026, 1, 7): ["A"],  # off-calendar
    date(2026, 1, 8): ["A"],  # below min (plain)
    date(2026, 1, 10): ["A", "B"],
    date(2026, 1, 12): ["A", "B"],
}


@pytest.fixture
def seeded(conn: Conn) -> Iterator[Conn]:
    conn.execute("CREATE TABLE md.report_test (trade_date date NOT NULL, secid text NOT NULL)")
    for day, secids in ROWS.items():
        for s in secids:
            conn.execute("INSERT INTO md.report_test VALUES (%s, %s)", (day, s))
    conn.execute(
        """
        INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) VALUES
          ('MOEX_STOCK', '2026-01-07', 'curated', false, '2025-12-01'),
          ('MOEX_STOCK', '2026-01-10', 'observed_moex_stock', true, '2025-12-01'),
          ('MOEX_STOCK', '2026-01-10', 'curated', false, '2026-01-11')
        """
    )
    conn.commit()
    register_dataset(PLAIN)
    register_dataset(SERIES)
    try:
        yield conn
    finally:
        unregister_dataset(PLAIN.name)
        unregister_dataset(SERIES.name)


def _seed_ops_and_bonds(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO ops.job_run (job, started_at, finished_at, status, rows_inserted, error)
        VALUES ('bonds_eod', '2026-01-11 10:00+00', '2026-01-11 10:05+00', 'succeeded', 5, NULL),
               ('bonds_eod', '2026-01-12 08:00+00', '2026-01-12 08:01+00', 'failed', 0,
                'HTTP 503 | upstream
second line')
        """
    )
    conn.execute(
        """
        INSERT INTO ops.data_gap (dataset, gap_date, status) VALUES
          ('zz_report_plain', '2026-01-09', 'open'),
          ('zz_report_plain', '2025-12-31', 'unfillable')
        """
    )
    conn.execute(
        """
        INSERT INTO ops.backfill_task (dataset, params, status) VALUES
          ('zz_report_plain', '{"date_from": "2026-01-09", "date_to": "2026-01-09"}', 'pending')
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status) VALUES
          ('FIX1', 'corporate', 'fixed', 'moex_label', 'active'),
          ('FLT1', 'corporate', 'floating', 'moex_label', 'active'),
          ('FLT2', 'corporate', 'floating', 'moex_label', 'matured')
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_coupon (secid, coupon_no, pay_date, status, is_deleted) VALUES
          ('FIX1', 1, '2025-06-01', 'known', false),
          ('FIX1', 2, '2026-06-01', 'known', false),
          ('FLT1', 1, '2026-03-01', 'current_published', false),
          ('FLT1', 2, '2026-06-01', 'projected_by_moex', true)
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_offer (secid, offer_key, offer_date, offer_type, status)
        VALUES ('FLT1', '2027-01-01', '2027-01-01', 'put', 'scheduled')
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_rate_formula (secid, source, benchmark, spread_bp, status)
        VALUES ('FLT1', 'moex_iss', 'KEY_RATE', 150, 'inferred')
        """
    )
    conn.execute(
        """
        INSERT INTO ops.legacy_import (schema_name, source_path, source_sha256,
            source_size_bytes, table_counts, verified, verify_report)
        VALUES ('legacy_md', '/somewhere/private/market-data.sqlite3', repeat('a', 64), 1,
                '{"t1": 2, "t2": 3}', true, '{"ok": true, "problems": [], "warnings": ["w"]}')
        """
    )
    conn.commit()


def test_effective_calendar_precedence(seeded: Conn) -> None:
    cal = EffectiveCalendar.load(seeded)
    seeded.rollback()
    assert cal(date(2026, 1, 7), "MOEX_STOCK") is False
    assert cal(date(2026, 1, 10), "MOEX_STOCK") is True  # observed beats newer curated
    assert cal(date(2026, 1, 11), "MOEX_STOCK") is False  # no row: weekday rule
    assert cal(date(2026, 1, 7), "MOEX_FX") is True


def test_dataset_coverage_plain_and_series(seeded: Conn) -> None:
    cal = EffectiveCalendar.load(seeded)
    plain = dataset_coverage(seeded, PLAIN, today=TODAY, calendar=cal)
    assert plain.problem is None and plain.expected_latest == TODAY and not plain.is_stale
    t = plain.total
    assert t is not None
    assert (t.first_date, t.last_date, t.rows, t.distinct_days) == (
        date(2026, 1, 5),
        TODAY,
        10,
        6,
    )
    assert t.window == (date(2026, 1, 5), TODAY) and t.expected == 6
    assert t.missing == [date(2026, 1, 9)]
    assert t.thin == [date(2026, 1, 8)]
    assert t.off_calendar == 1

    series = dataset_coverage(seeded, SERIES, today=TODAY, calendar=cal)
    assert series.total is not None and series.total.thin == []  # min rows apply per series
    by_name = {s.series: s for s in series.series}
    assert list(by_name) == ["A", "B", "C"]
    assert by_name["A"].missing == [date(2026, 1, 9)] and by_name["A"].off_calendar == 1
    assert by_name["B"].missing == [date(2026, 1, 8), date(2026, 1, 9)]
    assert by_name["C"].rows == 0 and by_name["C"].expected is None
    seeded.rollback()


def test_dataset_coverage_history_start_before_first_row(seeded: Conn) -> None:
    spec = DatasetSpec(
        name="zz_report_early",
        table="md.report_test",
        date_column="trade_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=0,
        description="history_start before the data",
        history_start=date(2026, 1, 1),
    )
    cov = dataset_coverage(seeded, spec, today=TODAY, calendar=EffectiveCalendar.load(seeded))
    assert cov.total is not None
    assert cov.total.missing == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 9)]
    seeded.rollback()


def test_dataset_coverage_rolling_window(seeded: Conn) -> None:
    """Days before a rolling window (deleted by retention) are not reported missing."""
    spec = DatasetSpec(
        name="zz_report_window",
        table="md.report_test",
        date_column="trade_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=0,
        description="rolling window",
        history_start=date(2026, 1, 1),
        rolling_window_setting="algopack_retention_days",
    )
    settings = Settings(algopack_retention_days=4)  # type: ignore[call-arg]  # from 01-09
    cov = dataset_coverage(
        seeded, spec, today=TODAY, calendar=EffectiveCalendar.load(seeded), settings=settings
    )
    assert cov.total is not None
    assert cov.total.missing == [date(2026, 1, 9)]
    seeded.rollback()


def test_missing_table_and_no_rows(conn: Conn) -> None:
    cal = EffectiveCalendar({})
    gone = DatasetSpec(
        name="zz_gone",
        table="md.does_not_exist",
        date_column="d",
        calendar=None,
        frequency="daily",
        max_lag_days=0,
        description="x",
    )
    cov = dataset_coverage(conn, gone, today=TODAY, calendar=cal)
    assert cov.problem == "table md.does_not_exist does not exist" and cov.is_stale
    conn.execute("CREATE TABLE md.empty_t (d date)")
    empty = DatasetSpec(
        name="zz_empty",
        table="md.empty_t",
        date_column="d",
        calendar=None,
        frequency="daily",
        max_lag_days=0,
        description="x",
        history_start=date(2026, 1, 10),
    )
    cov = dataset_coverage(conn, empty, today=TODAY, calendar=cal)
    assert cov.problem == "no rows" and cov.total is not None
    assert cov.total.missing == [date(2026, 1, 10), date(2026, 1, 11), TODAY]
    conn.rollback()


def test_build_seed_report(seeded: Conn) -> None:
    _seed_ops_and_bonds(seeded)
    runs_before = seeded.execute("SELECT count(*) AS n FROM ops.job_run").fetchone()
    seeded.commit()
    text = build_seed_report(seeded, today=TODAY, generated_at=GENERATED)
    again = build_seed_report(seeded, today=TODAY, generated_at=GENERATED)
    assert text == again  # deterministic
    assert seeded.read_only is None and not seeded.autocommit  # settings restored

    assert text.startswith("# market-data-hub seed report\n")
    assert "- generated at: 2026-01-12 09:00:00 UTC" in text
    assert "- server: PostgreSQL 16" in text
    for heading in (
        "## Summary",
        "## Tables",
        "### md",
        "### legacy_bc",
        "## Datasets",
        "### Series",
        "## Operations",
        "## Bond static coverage",
        "## Legacy import (ops.legacy_import)",
    ):
        assert f"\n{heading}\n" in text, heading
    assert "| report_test | 10 |" in text
    assert "| zz_report_plain | md.report_test | business_daily/MOEX_STOCK | 2026-01-05 |" in text
    assert "- `zz_report_plain` missing 1: 2026-01-09" in text
    assert "- `zz_report_plain` below 2 rows 1: 2026-01-08" in text
    assert "- `zz_report_series / B` missing 2: 2026-01-08, 2026-01-09" in text
    assert "| zz_report_plain | open | 1 | 1 | 2026-01-09 | 2026-01-09 |" in text
    assert "| pending | zz_report_plain | 1 | 0 |" in text
    # last run per job, error on one line with escaped pipe
    assert "| bonds_eod |" in text and "| failed | 2026-01-12 08:00:00 |" in text
    assert "HTTP 503 \\| upstream |" in text and "second line" not in text
    assert "- jobs whose last run failed/partial: bonds_eod" in text
    assert "| bonds | 3 | 2 |" in text
    assert "| with coupon schedule | 2 | 2 |" in text
    assert "| with offers | 1 | 1 |" in text
    assert "| floating | 2 | 1 |" in text
    assert "| known | 2 | 1 | 1 | 1 |" in text
    assert "| current_published | 1 | 1 | 1 | 1 |" in text
    assert "projected_by_moex" not in text.split("### ref.bond_coupon status")[1].split("###")[0]
    assert "- floaters: 2 (active 1)" in text
    assert "- with resolved benchmark: 1 (active 1), with benchmark and spread: 1" in text
    assert "| KEY_RATE | inferred | 1 | 1 |" in text
    assert "| market-data.sqlite3 | aaaaaaaaaaaa |" in text and "/somewhere" not in text
    assert "| 2 | 5 | yes |" in text  # tables, rows, verified
    assert "Latest import per schema verified: yes" in text

    runs_after = seeded.execute("SELECT count(*) AS n FROM ops.job_run").fetchone()
    assert runs_before == runs_after
    seeded.rollback()


def test_report_works_on_read_only_session(seeded: Conn, db_url: str) -> None:
    with psycopg.connect(db_url, options="-c default_transaction_read_only=on") as ro:
        text = build_seed_report(ro, today=TODAY, generated_at=GENERATED)
    assert "| report_test | 10 |" in text


def test_report_reads_exported_snapshot(seeded: Conn, db_url: str) -> None:
    with psycopg.connect(db_url) as holder, psycopg.connect(db_url, autocommit=True) as writer:
        holder.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        row: Any = holder.execute("SELECT pg_export_snapshot()").fetchone()
        snapshot = str(row[0])
        writer.execute("INSERT INTO md.report_test VALUES ('2026-01-09', 'A')")
        with psycopg.connect(db_url) as reader:
            old = build_seed_report(reader, today=TODAY, generated_at=GENERATED, snapshot=snapshot)
            new = build_seed_report(reader, today=TODAY, generated_at=GENERATED)
        holder.rollback()
    assert "| report_test | 10 |" in old and f"- snapshot: `{snapshot}`" in old
    assert "| report_test | 11 |" in new
    with pytest.raises(ValueError, match="invalid snapshot"):
        build_seed_report(seeded, snapshot="x'; DROP")


def test_cli_report_seed(
    seeded: Conn, db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    out = tmp_path / "sub" / "SEED_REPORT.md"
    result = runner.invoke(cli.app, ["report", "seed", "--out", str(out), "--as-of", "2026-01-12"])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert text.startswith("# market-data-hub seed report\n")
    assert "- as of (business date, MSK): 2026-01-12" in text
    assert "zz_report_series" in text

    bad = runner.invoke(cli.app, ["report", "seed", "--as-of", "12.01.2026"])
    assert bad.exit_code == 2
    bad = runner.invoke(cli.app, ["report", "seed", "--snapshot", "nope"])
    assert bad.exit_code == 2
    monkeypatch.delenv("MDHUB_DATABASE_URL")
    missing = runner.invoke(cli.app, ["report", "seed"])
    assert missing.exit_code == 2 and "MDHUB_DATABASE_URL" in missing.output
