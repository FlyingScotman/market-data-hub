"""Phase 2 bond-catalog mapping (mdhub.legacy.map_bc) on a synthetic legacy_bc."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from mdhub.db.pool import Database
from mdhub.legacy import import_sqlite, verify_schema
from mdhub.legacy.import_sqlite import LegacyImportError
from mdhub.legacy.map_bc import map_bc

TODAY = date(2026, 10, 16)
ISSUE_COLS = (
    "secid, isin, regnumber, name, shortname, is_traded, emitent_id, emitent_title, emitent_inn, "
    "type, bond_type, bond_subtype, facevalue, initial_facevalue, faceunit, currency, issue_size, "
    "issue_size_placed, issue_date, maturity_date, coupon_period_days, coupon_frequency, "
    "list_level, qualified_investors, coupon_benchmark, coupon_benchmark_spread, "
    "primary_boardid, marketprice_boardid, description_available, put_option_date, "
    "call_option_date, first_seen_run, issuer_ogrn, issuer_country, issuer_short_name_cbr, "
    "instrument_class"
)
SCHEMA_SQL = """
CREATE TABLE issuers (emitent_id TEXT PRIMARY KEY, title TEXT, inn TEXT, okpo TEXT,
    first_seen_run TEXT NOT NULL, raw_json TEXT NOT NULL DEFAULT '');
CREATE TABLE issues (secid TEXT PRIMARY KEY, isin TEXT, regnumber TEXT, name TEXT,
    shortname TEXT, is_traded INTEGER, emitent_id TEXT, emitent_title TEXT, emitent_inn TEXT,
    type TEXT, bond_type TEXT, bond_subtype TEXT, facevalue REAL, initial_facevalue REAL,
    faceunit TEXT, currency TEXT, issue_size REAL, issue_size_placed REAL, issue_date TEXT,
    maturity_date TEXT, coupon_period_days INTEGER, coupon_frequency INTEGER,
    list_level INTEGER, qualified_investors INTEGER, coupon_benchmark TEXT,
    coupon_benchmark_spread REAL, primary_boardid TEXT, marketprice_boardid TEXT,
    description_available INTEGER NOT NULL, put_option_date TEXT, call_option_date TEXT,
    first_seen_run TEXT NOT NULL, issuer_ogrn TEXT, issuer_country TEXT,
    issuer_short_name_cbr TEXT, instrument_class TEXT, raw_json TEXT NOT NULL DEFAULT '');
CREATE TABLE boards (secid TEXT NOT NULL, boardid TEXT NOT NULL, is_primary INTEGER NOT NULL,
    board_json TEXT NOT NULL DEFAULT '', first_seen_run TEXT NOT NULL,
    last_seen_run TEXT NOT NULL, PRIMARY KEY (secid, boardid));
CREATE TABLE events (event_key TEXT PRIMARY KEY, secid TEXT NOT NULL, event_type TEXT NOT NULL,
    event_date TEXT NOT NULL, ordinal INTEGER NOT NULL, value REAL, rate_pct REAL,
    currency TEXT, raw_json TEXT NOT NULL DEFAULT '');
CREATE TABLE formula_models (secid TEXT PRIMARY KEY, model_type TEXT NOT NULL,
    status TEXT NOT NULL, annual_rate_pct REAL, benchmark TEXT, spread_pp REAL,
    missing_terms_json TEXT NOT NULL, source TEXT NOT NULL, llm_used INTEGER NOT NULL,
    model_json TEXT NOT NULL);
"""
RUN1, RUN2 = "20260801T171519Z", "20261012T033027Z"


def _issue(secid: str, **kw: Any) -> tuple[Any, ...]:
    base: dict[str, Any] = {
        "secid": secid, "isin": None, "regnumber": None, "name": f"Облигация {secid}",
        "shortname": secid, "is_traded": 0, "emitent_id": "100", "emitent_title": "ПАО Тест",
        "emitent_inn": "7700000000", "type": "exchange_bond", "bond_type": None,
        "bond_subtype": None, "facevalue": 1000.0, "initial_facevalue": 1000.0,
        "faceunit": "SUR", "currency": "SUR", "issue_size": 1000000.0,
        "issue_size_placed": None, "issue_date": "2020-01-15", "maturity_date": "2023-01-15",
        "coupon_period_days": None, "coupon_frequency": 4, "list_level": 3,
        "qualified_investors": 0, "coupon_benchmark": None, "coupon_benchmark_spread": None,
        "primary_boardid": "TQCB", "marketprice_boardid": None, "description_available": 1,
        "put_option_date": None, "call_option_date": None, "first_seen_run": RUN1,
        "issuer_ogrn": None, "issuer_country": None, "issuer_short_name_cbr": None,
        "instrument_class": "ordinary_or_unclassified",
    }  # fmt: skip
    base.update(kw)
    return tuple(base[c.strip()] for c in ISSUE_COLS.split(","))


def _build(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_SQL)
    c.executemany(
        "INSERT INTO issuers (emitent_id, title, inn, okpo, first_seen_run) VALUES (?, ?, ?, ?, ?)",
        [
            ("100", "ПАО Тест", "7700000000", "123", RUN1),
            ("200", "ПАО Живой", "7711111111", None, RUN1),
        ],
    )
    ph = ", ".join("?" * len(ISSUE_COLS.split(",")))
    c.executemany(
        f"INSERT INTO issues ({ISSUE_COLS}) VALUES ({ph})",
        [
            _issue("LIVE1", emitent_id="200", maturity_date="2030-01-01"),
            _issue(
                "OLDFRN", isin="RU000OLDFRN1", coupon_benchmark="RREFKEYR",
                coupon_benchmark_spread=1.5, bond_type="Облигация с плавающим купоном",
                issuer_ogrn="1027700000000", issuer_country="RU",
            ),
            _issue(
                "PERP1", maturity_date="0000-00-00", bond_subtype="Бессрочные",
                faceunit="USD", currency="RUB",
            ),
            _issue("RU22TRMK2009", isin="RU000A0D05B7", maturity_date="2010-01-01"),
            _issue("RU000A0D05B7", isin="RU000A0D05B7", maturity_date="2010-01-01"),
        ],
    )  # fmt: skip
    c.executemany(
        "INSERT INTO boards VALUES (?, ?, ?, '', ?, ?)",
        [("OLDFRN", "TQCB", 1, RUN1, RUN2), ("OLDFRN", "PSBB", 0, RUN1, RUN1)],
    )
    ev = [
        ("OLDFRN", "coupon", "2022-04-15", 0, 40.0, 16.0, "SUR"),
        ("OLDFRN", "coupon", "2022-07-15", 0, 41.5, 16.6, "SUR"),
        ("OLDFRN", "coupon", "2023-01-15", 0, None, None, "SUR"),
        ("OLDFRN", "amortization", "2022-07-15", 0, 0.0, 0.0, "SUR"),  # zero row
        ("OLDFRN", "amortization", "2022-10-15", 0, 500.0, 50.0, "SUR"),
        ("OLDFRN", "amortization", "2023-01-15", 0, 500.0, 50.0, "SUR"),
        ("OLDFRN", "offer", "0000-00-00", 0, None, None, "SUR"),
        ("OLDFRN", "offer", "2022-10-15", 0, 1000.0, None, "SUR"),
        ("PERP1", "coupon", "2027-01-01", 0, 30.0, 6.0, "USD"),
    ]
    c.executemany(
        "INSERT INTO events (event_key, secid, event_type, event_date, ordinal, value, rate_pct, "
        "currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(f"{e[0]}|{e[1]}|{e[2]}|{i}", *e) for i, e in enumerate(ev)],
    )
    fm = [
        ("LIVE1", "arithmetic_ruonia", "exact_curated", None, "RUONIA", 0.0,
         {"benchmark": "RUONIA", "lag_calendar_days": 7, "spread_pp": "0"}),
        ("OLDFRN", "fixed", "exact_curated", 7.5, None, None,
         {"annual_rate_pct": "7.50", "type": "fixed"}),
        ("NOTHERE", "gcurve_5y", "exact_curated", None, "MOEX G-curve 5Y", None,
         {"fixing_calendar_days_before_start": 6}),
        ("PERP1", "ruonia_indexed_principal_fixed_coupon", "partial_public_formula", 1.85,
         "RUONIA compounded index", None, {"index_lag_calendar_days": 10}),
        ("RU000A0D05B7", "fixed_rate", "preliminary_bondization", 5.0, None, None, {}),
    ]  # fmt: skip
    c.executemany(
        "INSERT INTO formula_models VALUES (?, ?, ?, ?, ?, ?, '[]', 'manifest', 1, ?)",
        [(*f[:6], json.dumps({"formula": f[6]})) for f in fm],
    )
    c.commit()
    c.close()


@pytest.fixture
def database(db_url: str) -> Iterator[Database]:
    d = Database(db_url)
    try:
        yield d
    finally:
        d.close()


@pytest.fixture
def legacy(tmp_path: Path, database: Database) -> Database:
    path = tmp_path / "bond_catalog.sqlite3"
    _build(path)
    import_sqlite(database, path, "legacy_bc")
    assert verify_schema(database, "legacy_bc").ok
    return database


def _seed_live(db_url: str) -> None:
    with psycopg.connect(db_url) as conn:
        row = conn.execute("INSERT INTO ops.job_run (job) VALUES ('bonds_static') RETURNING id")
        run = row.fetchone()
        assert run is not None
        conn.execute(
            "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status, "
            "run_id) VALUES ('LIVE1', 'corporate', 'floating', 'benchmark', 'active', %s)",
            (run[0],),
        )
        conn.execute(
            "INSERT INTO ref.issuer (issuer_id, name, run_id) VALUES (200, 'live', %s)", (run[0],)
        )


def _q(db_url: str, query: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()


def test_map_bc_requires_live_load(legacy: Database) -> None:
    res = map_bc(legacy, targets=["bonds"], today=TODAY)
    assert not res.ok and "bonds_static" in (res.targets[0].error or "")


def test_map_bc(legacy: Database, db_url: str) -> None:
    _seed_live(db_url)
    res = map_bc(legacy, today=TODAY)
    assert res.ok, [t.error for t in res.targets]
    bonds_rep, cur_rep = res.targets
    assert bonds_rep.candidates == 4 and bonds_rep.notes["legacy_secids_in_live_load"] == 1

    bonds = {r["secid"]: r for r in _q(db_url, "SELECT * FROM ref.bond")}
    assert set(bonds) == {"LIVE1", "OLDFRN", "PERP1", "RU22TRMK2009", "RU000A0D05B7"}
    assert bonds["LIVE1"]["source"] == "moex_iss"  # untouched live row
    frn = bonds["OLDFRN"]
    assert frn["source"] == "legacy_bc" and frn["status"] == "matured"
    assert (frn["coupon_type"], frn["coupon_type_basis"]) == ("floating", "moex_label")
    assert frn["bond_class"] == "corporate" and frn["face_unit"] == "RUB"
    assert frn["coupon_benchmark_spread_pct"] == Decimal("1.5") and frn["is_amortizing"] is True
    assert frn["in_directory"] is False and frn["capture_id"] is None
    assert frn["maturity_date"] == date(2023, 1, 15) and frn["issuer_id"] == 100
    perp = bonds["PERP1"]
    assert perp["is_perpetual"] is True and perp["maturity_date"] is None
    assert perp["status"] == "delisted" and perp["is_fx_face"] is True
    assert bonds["RU22TRMK2009"]["isin"] is None
    assert bonds["RU000A0D05B7"]["isin"] == "RU000A0D05B7"

    issuers = {r["issuer_id"]: r for r in _q(db_url, "SELECT * FROM ref.issuer")}
    assert issuers[200]["name"] == "live"  # live issuer untouched
    assert issuers[100]["source"] == "legacy_bc" and issuers[100]["okpo"] == "123"
    assert issuers[100]["ogrn"] == "1027700000000"

    coupons = _q(db_url, "SELECT * FROM ref.bond_coupon WHERE secid = 'OLDFRN' ORDER BY 2")
    assert [c["coupon_no"] for c in coupons] == [1, 2, 3]
    assert [c["status"] for c in coupons] == ["known", "known", "unknown"]
    assert coupons[1]["amount"] == Decimal("41.5") and coupons[1]["value_rub"] == Decimal("41.5")
    assert coupons[0]["start_date"] is None and coupons[2]["amount"] is None
    perp_c = _q(db_url, "SELECT * FROM ref.bond_coupon WHERE secid = 'PERP1'")[0]
    assert perp_c["face_unit"] == "USD" and perp_c["value_rub"] is None

    amorts = _q(db_url, "SELECT * FROM ref.bond_amortization ORDER BY amort_date")
    assert [(a["amort_date"], a["data_source"]) for a in amorts] == [
        (date(2022, 10, 15), "amortization"),
        (date(2023, 1, 15), "maturity"),
    ]
    assert bonds_rep.notes["zero_amortization_skipped"] == 1

    offers = _q(db_url, "SELECT * FROM ref.bond_offer ORDER BY offer_key")
    assert [(o["offer_key"], o["status"], o["offer_type"]) for o in offers] == [
        ("2022-10-15", "past", "unknown"),
        ("undated-1", "undated", "unknown"),
    ]
    assert offers[1]["date_missing"] is True and offers[0]["source"] == "legacy_bc"

    boards = _q(db_url, "SELECT * FROM ref.bond_board ORDER BY board")
    assert [(b["board"], b["is_primary"], b["listed"]) for b in boards] == [
        ("PSBB", False, False),
        ("TQCB", True, False),
    ]
    assert boards[1]["last_seen"] == date(2026, 10, 12)

    formulas = {
        (r["secid"], r["source"]): r for r in _q(db_url, "SELECT * FROM ref.bond_rate_formula")
    }
    moex = formulas[("OLDFRN", "legacy_bc")]
    assert moex["benchmark"] == "KEY_RATE" and moex["spread_bp"] == Decimal("150.0")
    assert moex["status"] == "inferred"
    live_cur = formulas[("LIVE1", "curated")]
    assert (live_cur["benchmark"], live_cur["spread_bp"], live_cur["lag_days"]) == (
        "RUONIA", Decimal("0"), 7,
    )  # fmt: skip
    assert live_cur["averaging"] == "arithmetic" and live_cur["status"] == "curated"
    assert live_cur["evidence"]["model_type"] == "arithmetic_ruonia"
    perp_cur = formulas[("PERP1", "curated")]
    assert perp_cur["benchmark"] == "RUONIA_INDEX" and perp_cur["status"] == "partial"
    assert perp_cur["lag_days"] == 10
    assert ("OLDFRN", "curated") not in formulas  # fixed model has no benchmark
    assert "skipped_not_in_ref_bond:NOTHERE" in cur_rep.notes

    again = map_bc(legacy, today=TODAY)
    assert again.ok
    for t in again.targets:
        assert t.counts.inserted == 0 and t.counts.updated == 0, t.target


def test_map_bc_unknown_target(legacy: Database) -> None:
    with pytest.raises(LegacyImportError):
        map_bc(legacy, targets=["nope"])
