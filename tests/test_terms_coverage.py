"""Bond terms coverage of historical cuts (market-data-hub#101)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from mdhub import cli
from mdhub.core.timeutil import utc_now
from mdhub.db.pool import Database
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.terms_coverage import (
    TERM_FIELDS,
    date_coverage,
    reference_terms,
    terms_coverage_report,
)
from test_snapshots import DAY, Conn, _cut_ts

FULL = {
    "benchmark_tenor_years": "3",
    "fixing_anchor": "accrual_start",
    "fixing_offset": 1,
    "fixing_offset_unit": "zcyc_publication_days",
    "rate_rounding_decimals": 2,
    "fixing_basis": "curated",
    "spread_schedule": [{"effective_from": "2025-01-10", "spread_bp": "150"}],
    "reproduction": {"accepted": True},
}


def _doc(
    formulas: list[dict[str, Any]],
    instruments: list[dict[str, Any]],
    version: str = "market_snapshot/1.6",
    **parameters: Any,
) -> dict[str, Any]:
    return {
        "schema_version": version,
        "parameters": parameters,
        "data": {"floater_formulas": formulas, "instruments": instruments},
    }


def _instr(secid: str, issued: str = "2025-01-10", matures: str | None = "2030-01-10") -> dict:
    return {"secid": secid, "issue_date": issued, "maturity_date": matures}


REFERENCE = _doc(
    [
        {"secid": "A", **FULL},
        {"secid": "B", **FULL, "spread_schedule": None, "reproduction": None},
        {"secid": "C", **FULL},  # placed after the date: not counted
        {"secid": "D", **FULL},  # matured before the date: not counted
        {"secid": "E", **FULL},
        {"secid": "F", **FULL},
        {"secid": "PLAIN", "benchmark": "KEY_RATE"},  # no 1.4 terms
    ],
    [
        _instr("A"),
        _instr("B"),
        _instr("C", issued="2026-06-01"),
        _instr("D", matures="2026-01-01"),
        _instr("E"),
        _instr("F"),
        _instr("PLAIN"),
    ],
)


def test_reference_terms_keeps_bonds_with_any_term() -> None:
    ref = reference_terms("ref", REFERENCE)
    assert set(ref.terms) == {"A", "B", "C", "D", "E", "F"}
    assert ref.terms["B"] == frozenset(TERM_FIELDS) - {"spread_schedule", "reproduction"}
    assert ref.alive_on("A", date(2026, 3, 16))
    assert not ref.alive_on("C", date(2026, 3, 16))
    assert not ref.alive_on("D", date(2026, 3, 16))


def test_date_coverage_reasons() -> None:
    ref = reference_terms("ref", REFERENCE)
    pre_1_4 = {k: v for k, v in FULL.items() if k not in TERM_FIELDS}  # 1.0: no 1.4 fields
    old = _doc(
        [
            {"secid": "A", **pre_1_4},
            {"secid": "B", **FULL, "spread_schedule": None, "reproduction": None},
            {"secid": "E", **FULL, "fixing_offset": None},
        ],
        [_instr("A"), _instr("B"), _instr("E")],  # F not in the static at all
        version="market_snapshot/1.0",
    )
    cov = date_coverage(date(2026, 3, 16), "m", "s", old, ref)
    assert (cov.bonds, cov.complete, cov.not_in_static, cov.no_formula) == (4, 1, 1, 0)
    assert cov.missing_by_field == {**dict.fromkeys(TERM_FIELDS, 1), "fixing_offset": 2}
    assert cov.missing_secids == ["A", "E", "F"]
    wire = cov.wire()
    assert wire["static_schema_version"] == "market_snapshot/1.0"
    assert wire["static_basis"] is None
    assert list(wire["missing_by_field"]) == list(TERM_FIELDS)

    no_formula = _doc([], [_instr(s) for s in "ABEF"], static_basis="x")
    cov = date_coverage(date(2026, 3, 16), "m", "s", no_formula, ref)
    assert (cov.bonds, cov.no_formula, cov.static_basis) == (4, 4, "x")


@pytest.fixture
def published(conn: Conn, db: Database) -> str:
    from test_api_seed import seed

    seed(conn)
    out = publish_market_snapshot(
        db, cut_type="eod_final", business_date=DAY, cut_ts=_cut_ts(), known_at=utc_now()
    )
    return out.static.id


def test_report_reads_current_eod_final_cuts(
    published: str, conn: Conn, db_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ref, rows = terms_coverage_report(conn, DAY, DAY)
    assert ref.static_id == published
    assert [(r.business_date, r.static_id) for r in rows] == [(DAY, published)]
    with pytest.raises(LookupError):
        terms_coverage_report(conn, DAY, DAY, reference_id="0" * 64)

    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    out = tmp_path / "terms.json"
    result = CliRunner().invoke(
        cli.app, ["ops", "static-terms", "--from", "2026-10-01", "--to", "2026-10-31",
                  "--json", str(out)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert f"reference static {published}" in result.output
    assert "2026-10-15" in result.output
    report = json.loads(out.read_text())
    assert report["reference_static_id"] == published
    assert [d["business_date"] for d in report["dates"]] == ["2026-10-15"]
    empty = CliRunner().invoke(
        cli.app, ["ops", "static-terms", "--from", "2026-01-01", "--to", "2026-01-31"]
    )
    assert "no current eod_final cut in the range" in empty.output
