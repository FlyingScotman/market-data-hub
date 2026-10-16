"""Phase 2 legacy mapping (mdhub.legacy.map_md) on a synthetic legacy_md."""

from __future__ import annotations

import importlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row, tuple_row
from typer.testing import CliRunner

from mdhub import cli
from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database
from mdhub.legacy import import_sqlite, verify_schema
from mdhub.legacy.import_sqlite import LegacyImportError
from mdhub.legacy.map_md import curve_content_hash, map_md
from mdhub.sources import ratescalcs as rc

LIQ = (
    "deficit_with_corr_accounts_bn, deficit_without_corr_accounts_bn, cbr_claims_total_bn, "
    "repo_fx_swap_auctions_bn, repo_fx_swap_standing_bn, secured_loans_auctions_bn, "
    "secured_loans_standing_bn, cbr_obligations_total_bn, deposit_auctions_bn, "
    "deposits_standing_bn, cbr_bonds_bn, nonstandard_net_claims_bn, bank_corr_accounts_bn, "
    "averaged_reserves_bn"
)
LIQ_DDL = ", ".join(f"{c.strip()} REAL NOT NULL" for c in LIQ.split(","))
# A real KBD publication (MOEX ISS 2026-08-28 18:49:59 MSK): passes the 0.01 bp check.
PARAMS = [1527.808981, -245.763992, -395.723137, 0.596835, -0.009991, -0.517047, -1.271739,
          -2.650931, 5.909998, 5.361478, -2.722557, 0.0, 0.0]  # fmt: skip
YIELDS = [[0.25, 13.4689], [0.5, 13.4737], [0.75, 13.5931], [1.0, 13.7688], [2.0, 14.5343],
          [3.0, 15.0922], [5.0, 15.6969], [7.0, 15.9673], [10.0, 16.1199], [15.0, 16.2076],
          [20.0, 16.2614]]  # fmt: skip
BAD_YIELDS = [[t, y + (0.01 if t == 5.0 else 0)] for t, y in YIELDS]
KRS13 = ["3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y"]

SCHEMA_SQL = f"""
CREATE TABLE sources (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE);
CREATE TABLE datasets (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, code TEXT NOT NULL);
CREATE TABLE curve_snapshots (
    id INTEGER PRIMARY KEY, dataset_id INTEGER NOT NULL, fetched_at_utc TEXT NOT NULL,
    source_observed_at_utc TEXT, raw_payload TEXT NOT NULL DEFAULT '',
    payload_sha256 TEXT NOT NULL, parser_version INTEGER NOT NULL, quote_basis TEXT NOT NULL,
    snapshot_uid TEXT, correction_state TEXT NOT NULL DEFAULT 'original');
CREATE TABLE curve_quotes (
    id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, position INTEGER NOT NULL,
    tenor TEXT NOT NULL, tenor_months INTEGER, bid_pct REAL NOT NULL, ask_pct REAL NOT NULL,
    mid_pct REAL NOT NULL, quote_basis TEXT NOT NULL);
CREATE TABLE index_fixings (index_code TEXT NOT NULL, fixing_date TEXT NOT NULL,
    rate_pct REAL NOT NULL, source_code TEXT NOT NULL, fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (index_code, fixing_date));
CREATE TABLE term_ruonia_fixings (fixing_date TEXT PRIMARY KEY, index_value REAL NOT NULL,
    one_month_pct REAL NOT NULL, three_month_pct REAL NOT NULL, six_month_pct REAL NOT NULL,
    source_code TEXT NOT NULL, fetched_at_utc TEXT NOT NULL);
CREATE TABLE policy_meetings (meeting_date TEXT PRIMARY KEY, effective_date TEXT NOT NULL,
    is_estimated INTEGER NOT NULL, source_code TEXT NOT NULL, fetched_at_utc TEXT NOT NULL);
CREATE TABLE cbr_liquidity_captures (id INTEGER PRIMARY KEY, fetched_at_utc TEXT NOT NULL);
CREATE TABLE cbr_liquidity_balance_revisions (id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL, observed_on TEXT NOT NULL, revision_number INTEGER NOT NULL,
    methodology_version TEXT NOT NULL, {LIQ_DDL});
CREATE TABLE cbr_liquidity_balances (observed_on TEXT PRIMARY KEY, {LIQ_DDL},
    revision_id INTEGER);
CREATE TABLE gcurve_snapshots (id INTEGER PRIMARY KEY, snapshot_uid TEXT NOT NULL UNIQUE,
    trade_date TEXT NOT NULL, source_observed_at_utc TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL, parameters_json TEXT NOT NULL,
    standard_yields_json TEXT NOT NULL, max_reconciliation_error_bp REAL NOT NULL) STRICT;
CREATE TABLE intraday_quote_captures (id INTEGER PRIMARY KEY, source_code TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL) STRICT;
CREATE TABLE intraday_quote_observations (id INTEGER PRIMARY KEY, capture_id INTEGER NOT NULL,
    instrument_code TEXT NOT NULL, instrument_type TEXT NOT NULL, source_variant TEXT,
    board TEXT, session_date TEXT NOT NULL, source_observed_at_utc TEXT NOT NULL,
    bid_text TEXT, offer_text TEXT, last_text TEXT, quote_unit TEXT NOT NULL,
    latency TEXT NOT NULL) STRICT;
"""


def _liq(base: float) -> list[float]:
    # columns in CBR table order; col 6 (legacy "repo_fx_swap_standing") = 1, col 7 = 2
    return [base, base - 100, 9000.0, 7000.0, 1.0, 2.0, 1997.0, 4000.0, 0.0, 4000.0, 0.0,
            250.5, 5000.0, 5400.0]  # fmt: skip


def _build(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_SQL)
    # fixings
    c.executemany(
        "INSERT INTO index_fixings VALUES (?, ?, ?, 'cbr', ?)",
        [
            ("ruonia", "2026-09-01", 15.15, "2026-09-02T04:15:06Z"),
            ("ruonia", "2026-09-02", 15.27, "2026-09-03T04:15:06Z"),
            ("ruonia", "2026-09-03", 15.31, "2026-09-04T04:15:06Z"),
            ("key_rate", "2026-09-01", 16.0, "2026-09-01T04:15:06Z"),
        ],
    )
    c.execute(
        "INSERT INTO term_ruonia_fixings VALUES ('2026-09-01', 4.54391958463158, "
        "14.0820292725795, 14.2145351596461, 14.5974327047964, 'cbr', '2026-09-02T04:15:06Z')"
    )
    c.executemany(
        "INSERT INTO policy_meetings VALUES (?, ?, ?, 'cbr', '2026-10-01T04:30:28Z')",
        [("2026-09-12", "2026-09-15", 0), ("2026-10-23", "2026-10-26", 1)],
    )
    # liquidity: 09-01 has two revisions, 09-02 one
    c.executemany(
        "INSERT INTO cbr_liquidity_captures VALUES (?, ?)",
        [(1, "2026-09-02T06:00:00Z"), (2, "2026-09-03T06:00:00Z")],
    )
    ph = ", ".join("?" * 14)
    c.executemany(
        f"INSERT INTO cbr_liquidity_balance_revisions VALUES (?, ?, ?, ?, ?, {ph})",
        [
            (1, 1, "2026-09-01", 1, "cbr_bliquidity_v2023_11", *_liq(3000.0)),
            (2, 2, "2026-09-01", 2, "cbr_bliquidity_v2023_11", *_liq(3001.5)),
            (3, 2, "2026-09-02", 1, "cbr_bliquidity_v2023_11", *_liq(3100.0)),
        ],
    )
    c.executemany(
        f"INSERT INTO cbr_liquidity_balances VALUES (?, {ph}, ?)",
        [("2026-09-01", *_liq(3001.5), 2), ("2026-09-02", *_liq(3100.0), 3)],
    )
    # gcurve: one valid publication polled twice, one failing the KBD check
    c.executemany(
        "INSERT INTO gcurve_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, 0.003)",
        [
            (1, "u1", "2026-08-28", "2026-08-28T15:49:59+00:00", "2026-08-28T15:50:10+00:00",
             json.dumps(PARAMS), json.dumps(YIELDS)),
            (2, "u2", "2026-08-28", "2026-08-28T15:49:59+00:00", "2026-08-28T15:55:10+00:00",
             json.dumps(PARAMS), json.dumps(YIELDS)),
            (3, "u3", "2026-08-28", "2026-08-28T12:00:00+00:00", "2026-08-28T12:01:00+00:00",
             json.dumps(PARAMS), json.dumps(BAD_YIELDS)),
        ],
    )  # fmt: skip
    # curves
    c.executemany(
        "INSERT INTO sources VALUES (?, ?)",
        [(1, "ratescalcs"), (2, "andromeda"), (3, "cfets_chinamoney"), (4, "otc_monitor")],
    )
    c.executemany(
        "INSERT INTO datasets VALUES (?, ?, ?)",
        [(1, 1, "key_rate_swap"), (2, 2, "key_rate_swap"), (3, 3, "cfets_fr007_irs_closing"),
         (4, 4, "cnyruonia_xccy")],
    )  # fmt: skip
    snaps = [
        # ratescalcs: 2-tenor partial, then the same full curve twice, then a change
        (1, 1, "2026-08-05T09:17:00Z", None, "bid_ask_mid"),
        (2, 1, "2026-08-10T09:00:00Z", None, "bid_ask_mid"),
        (3, 1, "2026-08-10T09:30:00Z", None, "bid_ask_mid"),
        (4, 1, "2026-08-11T09:00:00Z", None, "bid_ask_mid"),
        # andromeda archive batch: one fetch time, two curve dates, one partial
        (5, 2, "2026-08-06T09:19:48Z", "2022-10-24T00:00:00Z", "mid_only"),
        (6, 2, "2026-08-06T09:19:48Z", "2022-10-25T00:00:00Z", "mid_only"),
        (7, 3, "2026-08-29T14:00:04Z", None, "bid_ask_mid"),
        (8, 4, "2026-09-23T04:37:00Z", "2026-09-22T21:05:02Z", "mid_only"),
    ]
    c.executemany(
        "INSERT INTO curve_snapshots (id, dataset_id, fetched_at_utc, source_observed_at_utc, "
        "payload_sha256, parser_version, quote_basis, snapshot_uid) "
        "VALUES (?, ?, ?, ?, 'ab', 1, ?, NULL)",
        snaps,
    )
    quotes: list[tuple[Any, ...]] = []

    def add(sid: int, tenors: list[str], base: float, basis: str, spread: float = 0.1) -> None:
        for i, t in enumerate(tenors):
            mid = round(base + i * 0.01, 6)
            bid, ask = (
                (mid, mid)
                if basis == "mid_only"
                else (round(mid - spread, 6), round(mid + spread, 6))
            )
            months = rc.tenor_parts(t)[0]
            quotes.append((sid, i, t, months, bid, ask, (bid + ask) / 2, basis))

    add(1, ["3M", "6M"], 16.0, "bid_ask_mid")
    add(2, KRS13, 15.0, "bid_ask_mid")
    add(3, KRS13, 15.0, "bid_ask_mid")
    add(4, KRS13, 15.5, "bid_ask_mid")
    add(5, KRS13[:5], 7.0, "mid_only")
    add(6, KRS13, 7.1, "mid_only")
    add(7, ["1M", "3M"], 1.4, "bid_ask_mid")
    add(8, ["1W", "1M"], 1.9, "mid_only")
    quotes.append((8, 2, "3M", 3, 2.0, 2.2, 2.1, "bid_ask_mid"))  # per-quote basis differs
    c.executemany(
        "INSERT INTO curve_quotes (snapshot_id, position, tenor, tenor_months, bid_pct, "
        "ask_pct, mid_pct, quote_basis) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        quotes,
    )
    # CFETS publishes its own (rounded) mid
    c.execute("UPDATE curve_quotes SET mid_pct = 1.4238 WHERE snapshot_id = 7 AND tenor = '1M'")
    # intraday
    c.executemany(
        "INSERT INTO intraday_quote_captures VALUES (?, ?, ?)",
        [
            (1, "moex_apim", "2026-09-01T09:01:00Z"),
            (2, "moex_apim", "2026-09-01T09:02:00Z"),
            (3, "moex_passport_realtime", "2026-09-01T09:01:00Z"),
            (4, "ratescalcs", "2026-09-01T09:01:00Z"),
        ],
    )
    obs = [
        # AlgoPack obstats bar re-captured: the later capture wins
        (1, "SiZ6", "dated_future", "algopack_obstats_mid", "RFUD", "2026-09-01T09:00:00Z",
         None, None, "86.1"),
        (2, "SiZ6", "dated_future", "algopack_obstats_mid", "RFUD", "2026-09-01T09:00:00Z",
         None, None, "86.2"),
        (2, "SiZ6", "dated_future", "algopack_obstats_mid", "RFUD", "2026-09-01T08:55:00Z",
         None, None, "86.2"),
        (1, "SiZ6", "dated_future", "moex_apim", "RFUD", "2026-09-01T09:00:30Z",
         "86.10", "86.12", "86.11"),
        # passport realtime: unchanged consecutive row is dropped
        (3, "CNYRUB_TOM", "spot", "passport_cets_realtime", "CETS", "2026-09-01T09:00:01Z",
         "12.1", "12.2", "12.15"),
        (3, "CNYRUB_TOM", "spot", "passport_cets_realtime", "CETS", "2026-09-01T09:00:02Z",
         "12.1", "12.2", "12.15"),
        (3, "CNYRUB_TOM", "spot", "passport_cets_realtime", "CETS", "2026-09-01T09:00:03Z",
         "12.1", "12.3", "12.15"),
        (4, "USD/RUB_TOM", "spot", "ntpro_snapshot_clock", None, "2026-09-01T09:00:00Z",
         "85.46555000000001", "85.4656", None),
        (4, "USD/RUB_TOM", "spot", "usdrubf_fallback_snapshot_clock", None,
         "2026-09-01T09:00:05Z", "86.785", "86.785", None),
    ]  # fmt: skip
    c.executemany(
        "INSERT INTO intraday_quote_observations (capture_id, instrument_code, instrument_type, "
        "source_variant, board, session_date, source_observed_at_utc, bid_text, offer_text, "
        "last_text, quote_unit, latency) "
        "VALUES (?, ?, ?, ?, ?, '2026-09-01', ?, ?, ?, ?, 'rub_per_base_unit', ?)",
        [(*o, "realtime" if o[3] in ("moex_apim", "passport_cets_realtime") else "derived")
         for o in obs],
    )  # fmt: skip
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
    path = tmp_path / "market-data.sqlite3"
    _build(path)
    import_sqlite(database, path, "legacy_md")
    assert verify_schema(database, "legacy_md").ok
    return database


def _live_run(conn: psycopg.Connection[Any], job: str) -> int:
    row = conn.execute("INSERT INTO ops.job_run (job) VALUES (%s) RETURNING id", (job,)).fetchone()
    assert row is not None
    return int(row[0])


def _seed_live(db_url: str) -> None:
    """Rows as live loaders would have written them, overlapping the legacy keys."""
    with psycopg.connect(db_url) as conn:
        run = _live_run(conn, "cbr_daily")
        conn.execute(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value, unit, run_id) VALUES "
            "('RUONIA', '2026-09-01', 'cbr', 15.15, 'pct', %(r)s), "
            "('RUONIA', '2026-09-02', 'cbr', 15.27, 'pct', %(r)s), "
            "('RUONIA', '2026-09-03', 'cbr', 15.32, 'pct', %(r)s)",
            {"r": run},
        )
        cols = (
            "deficit_bn, deficit_ex_corr_accounts_bn, cbr_claims_total_bn, "
            "repo_fxswap_auction_bn, secured_loans_auction_bn, repo_fxswap_standing_bn, "
            "secured_loans_standing_bn, cbr_obligations_total_bn, deposits_auction_bn, "
            "deposits_standing_bn, cobr_bn, nonstandard_net_claims_bn, corr_accounts_bn, "
            "required_reserves_averaged_bn"
        )
        conn.execute(
            f"INSERT INTO md.cbr_liquidity (observed_on, {cols}, methodology_version, run_id) "
            f"VALUES ('2026-09-02', {', '.join(str(v) for v in _liq(3100.0))}, "
            "'cbr_bliquidity_v2023_11', %s)",
            (run,),
        )
        run = _live_run(conn, "futures_intraday")
        conn.execute(
            "INSERT INTO md.futures_intraday (ts, secid, source, source_variant, "
            "last_rub_per_unit, latency, run_id) VALUES ('2026-09-01T09:00:30Z', 'SiZ6', "
            "'moex_apim', 'apim_marketdata', 99, 'realtime', %s)",
            (run,),
        )


def _q(db_url: str, query: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()


def test_curve_content_hash_matches_live_loader() -> None:
    quotes = [
        {"tenor": "3M", "position": 0, "bid_pct": Decimal("15.10"), "ask_pct": Decimal("15.2"),
         "mid_pct": Decimal("15.15")},
    ]  # fmt: skip
    live = rc.ParsedCurve(
        dataset="key_rate_swap",
        label="RUB KRS run",
        quotes=(rc.CurveQuote("3M", 0, Decimal("15.1"), Decimal("15.20")),),
    )
    assert curve_content_hash("bid_ask_mid", quotes) == rc.content_hash(live)
    mid_only = [{**quotes[0], "bid_pct": None, "ask_pct": None}]
    other = [{**mid_only[0], "mid_pct": Decimal("15.16")}]
    assert curve_content_hash("mid_only", mid_only) != curve_content_hash("mid_only", other)


def test_map_md_full(legacy: Database, db_url: str) -> None:
    _seed_live(db_url)
    res = map_md(legacy)
    assert res.ok, [t.error for t in res.targets]
    rep = {t.target: t for t in res.targets}

    # ---- fixings: live wins, mismatch reported, legacy-only keys inserted exactly
    fx = rep["fixings"]
    assert (fx.live_overlap, fx.live_match, fx.live_mismatch) == (3, 2, 1)
    assert fx.notes["ruonia_date_semantics"]["equal_on_same_date"] == 2
    rows = {
        (r["index_code"], r["fixing_date"]): r
        for r in _q(db_url, "SELECT * FROM md.fixing ORDER BY 1, 2")
    }
    assert rows[("RUONIA", date(2026, 9, 3))]["value"] == Decimal("15.32")  # live kept
    key = rows[("KEY_RATE", date(2026, 9, 1))]
    assert key["value"] == Decimal("16.0") and key["capture_id"] is None
    assert key["details"]["legacy_table"] == "legacy_md.index_fixings"
    assert key["effective_date"] == date(2026, 9, 1) and key["unit"] == "pct"
    idx = rows[("RUONIA_INDEX", date(2026, 9, 1))]
    assert idx["value"] == Decimal("4.54391958463158") and idx["unit"] == "index"
    assert rows[("TERM_RUONIA_6M", date(2026, 9, 1))]["value"] == Decimal("14.5974327047964")

    # ---- meetings
    m = {r["meeting_date"]: r for r in _q(db_url, "SELECT * FROM ref.cbr_meeting")}
    assert m[date(2026, 9, 12)]["is_estimated"] is False
    assert m[date(2026, 10, 23)]["is_estimated"] is True
    assert m[date(2026, 10, 23)]["effective_date"] == date(2026, 10, 26)

    # ---- liquidity: positional mapping, live row kept and matching, revisions in history
    liq = rep["liquidity"]
    assert (liq.live_overlap, liq.live_match) == (1, 1)
    row = _q(db_url, "SELECT * FROM md.cbr_liquidity WHERE observed_on = '2026-09-01'")[0]
    assert row["deficit_bn"] == Decimal("3001.5") and row["revision_no"] == 2
    assert row["secured_loans_auction_bn"] == Decimal("1.0")  # CBR column 6
    assert row["repo_fxswap_standing_bn"] == Decimal("2.0")  # CBR column 7
    hist = _q(db_url, "SELECT * FROM ops.row_history WHERE table_name = 'md.cbr_liquidity'")
    assert len(hist) == 1
    assert hist[0]["row_key"] == {"observed_on": "2026-09-01"}
    assert hist[0]["old_row"]["deficit_bn"] == 3000.0 and hist[0]["old_row"]["revision_no"] == 1
    assert hist[0]["replaced_at"] == datetime(2026, 9, 3, 6, 0, tzinfo=UTC)

    # ---- zcyc: duplicate publication collapsed, failing check rejected with capture
    z = _q(db_url, "SELECT * FROM md.zcyc_snapshot")
    assert len(z) == 1 and z[0]["trade_time"] == time(18, 49, 59) and z[0]["is_eod"] is False
    assert z[0]["params"]["B1"] == 1527.808981 and z[0]["params"]["G9"] == 0.0
    assert z[0]["standard_yields"][0] == {"tenor_years": 0.25, "yield_pct": 13.4689}
    assert z[0]["check_max_error_bp"] < Decimal("0.01")
    rej = _q(db_url, "SELECT r.*, c.endpoint FROM md.zcyc_reject r JOIN ops.raw_capture c "
             "ON c.id = r.capture_id")  # fmt: skip
    assert len(rej) == 1 and rej[0]["reason"].startswith("legacy: standard yields")
    assert rej[0]["endpoint"] == "legacy_gcurve_snapshot"
    assert rep["zcyc"].notes["duplicate_publication_identical"] == 1

    # ---- curves
    snaps = _q(
        db_url,
        "SELECT s.*, (SELECT count(*) FROM md.curve_quote q WHERE q.snapshot_id = s.id) AS n "
        "FROM md.curve_snapshot s ORDER BY source, dataset, fetched_at",
    )
    by_legacy = {int(s["legacy_id"]): s for s in snaps}
    assert set(by_legacy) == {1, 2, 4, 5, 6, 7, 8}  # 3 = unchanged repeat of 2
    assert by_legacy[1]["meta"]["quality_flags"] == ["ratescalcs_krs_partial_2_tenors"]
    assert "quality_flags" not in by_legacy[2]["meta"]
    assert by_legacy[2]["parser_version"] == "legacy_v1" and by_legacy[2]["capture_id"] is None
    a5, a6 = by_legacy[5], by_legacy[6]
    assert a5["fetched_at"] == datetime(2022, 10, 24, 23, 59, 59, tzinfo=MSK)
    assert a5["snapshot_date"] == date(2022, 10, 24)
    assert a5["meta"]["quality_flags"] == ["fetched_at_synthetic", "andromeda_partial_curve"]
    assert a6["meta"]["quality_flags"] == ["fetched_at_synthetic"]
    assert a6["meta"]["legacy"]["fetched_at_utc"] == "2026-08-06T09:19:48Z"
    assert by_legacy[7]["source"] == "cfets" and by_legacy[7]["dataset"] == "cfets_fr007_irs"
    assert by_legacy[8]["dataset"] == "cny_ruonia_xccy"
    quotes = {
        (int(r["legacy_id"]), r["tenor"]): r
        for r in _q(
            db_url,
            "SELECT s.legacy_id, q.* FROM md.curve_quote q "
            "JOIN md.curve_snapshot s ON s.id = q.snapshot_id",
        )
    }
    q6 = quotes[(6, "10Y")]
    assert q6["bid_pct"] is None and q6["ask_pct"] is None and q6["mid_pct"] == Decimal("7.22")
    q2 = quotes[(2, "1Y")]
    assert (q2["bid_pct"], q2["ask_pct"], q2["mid_pct"]) == (
        Decimal("14.93"), Decimal("15.13"), Decimal("15.03"),
    )  # fmt: skip
    assert q2["tenor_months"] == 12 and q2["position"] == 3
    assert quotes[(7, "1M")]["extra"] == {"legacy_mid_pct": "1.4238"}
    assert quotes[(8, "3M")]["extra"] == {"quote_basis": "bid_ask_mid"}
    assert quotes[(8, "1W")]["tenor_days"] == 7 and quotes[(8, "1W")]["bid_pct"] is None

    # ---- intraday
    fut = {
        (r["secid"], r["source"], r["source_variant"], r["ts"]): r
        for r in _q(db_url, "SELECT * FROM md.futures_intraday")
    }
    t0 = datetime(2026, 9, 1, 12, 0, tzinfo=MSK)
    assert fut[("SiZ6", "moex_apim", "algopack_obstats_mid", t0)]["last_rub_per_unit"] == Decimal(
        "86.2"
    )
    live_ts = datetime(2026, 9, 1, 12, 0, 30, tzinfo=MSK)
    assert fut[("SiZ6", "moex_apim", "apim_marketdata", live_ts)]["last_rub_per_unit"] == 99
    usdrubf = [r for k, r in fut.items() if k[0] == "USDRUBF"]
    assert len(usdrubf) == 1 and usdrubf[0]["source"] == "ratescalcs"
    assert usdrubf[0]["source_variant"] == "usdrubf_fallback_snapshot_clock"
    assert usdrubf[0]["bid_rub_per_unit"] == Decimal("86.785")
    spot = _q(db_url, "SELECT * FROM md.fx_intraday ORDER BY source, ts")
    passport = [r for r in spot if r["source"] == "moex_passport"]
    assert [r["offer"] for r in passport] == [Decimal("12.2"), Decimal("12.3")]
    assert passport[0]["board"] == "CETS" and passport[0]["latency"] == "realtime"
    ntpro = [r for r in spot if r["source"] == "ratescalcs"]
    assert len(ntpro) == 1 and ntpro[0]["board"] == "NTPRO"
    assert ntpro[0]["bid"] == Decimal("85.46555000000001") and ntpro[0]["secid"] == "USD/RUB_TOM"
    it = rep["intraday"]
    assert it.live_overlap == 1 and it.live_mismatch == 1
    assert it.notes["collapsed_duplicate_keys"] == 1
    assert it.notes["collapsed_with_different_values"] == 1
    assert it.notes["dropped_unchanged_consecutive"] == 1

    # ---- idempotent re-run: nothing written, no extra history / rejects
    again = map_md(legacy)
    assert again.ok
    for t in again.targets:
        assert t.counts.inserted == 0 and t.counts.updated == 0, t.target
    assert (
        len(_q(db_url, "SELECT 1 FROM ops.row_history WHERE table_name = 'md.cbr_liquidity'")) == 1
    )
    assert len(_q(db_url, "SELECT 1 FROM md.zcyc_reject")) == 1
    runs = _q(db_url, "SELECT status FROM ops.job_run WHERE job = 'legacy_map_md'")
    assert [r["status"] for r in runs] == ["succeeded", "succeeded"]


def test_live_loader_overwrites_legacy_row_later(legacy: Database, db_url: str) -> None:
    assert map_md(legacy, targets=["fixings"]).ok
    with psycopg.connect(db_url) as conn:
        run = _live_run(conn, "cbr_daily")
        conn.execute(
            "UPDATE md.fixing SET value = 16.5, run_id = %s "
            "WHERE index_code = 'KEY_RATE' AND fixing_date = '2026-09-01'",
            (run,),
        )
    res = map_md(legacy, targets=["fixings"])
    t = res.targets[0]
    assert t.live_overlap == 1 and t.live_mismatch == 1 and t.counts.updated == 0
    value = _q(db_url, "SELECT value FROM md.fixing WHERE index_code = 'KEY_RATE'")[0]["value"]
    assert value == Decimal("16.5")


def test_map_requires_verified_import(legacy: Database, db_url: str) -> None:
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE ops.legacy_import SET verified = false")
    with pytest.raises(LegacyImportError, match="not verified"):
        map_md(legacy)
    assert map_md(legacy, targets=["meetings"], allow_unverified=True).ok
    with pytest.raises(LegacyImportError, match="unknown target"):
        map_md(legacy, targets=["bonds"])


def test_target_failure_is_isolated(
    legacy: Database, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = importlib.import_module("mdhub.legacy.map_md")

    def boom(*_a: Any) -> None:
        raise ValueError("boom")

    monkeypatch.setitem(mod._MAPPERS, "zcyc", boom)
    res = map_md(legacy, targets=["meetings", "zcyc"])
    assert not res.ok
    assert res.targets[1].error == "ValueError: boom"
    assert len(_q(db_url, "SELECT 1 FROM ref.cbr_meeting")) == 2
    with psycopg.connect(db_url, row_factory=tuple_row) as conn:
        status = conn.execute(
            "SELECT status FROM ops.job_run WHERE job = 'legacy_map_md'"
        ).fetchone()
    assert status == ("partial",)


def test_cli_map_md(legacy: Database, db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    runner = CliRunner()
    res = runner.invoke(cli.app, ["legacy", "map-md", "--only", "meetings,fixings", "--json"])
    assert res.exit_code == 0, res.output
    assert '"meetings"' in res.output and "OK" in res.output
    res = runner.invoke(cli.app, ["legacy", "map-md", "--only", "nope"])
    assert res.exit_code == 1 and "unknown target" in res.output
