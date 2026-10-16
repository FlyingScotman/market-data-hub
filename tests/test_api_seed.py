"""Small, deterministic data set for the read-API and TDM-export tests (no tests here).

Export / query date: 2026-10-15 (Thursday).
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import DictRow

DAY = "2026-10-15"

KBD_PARAMS = {
    "B1": 1506.334188,
    "B2": -389.027515,
    "B3": 526.946929,
    "T1": 1.970464,
    "G1": 2.773419,
    "G2": -2.589492,
    "G3": -2.023503,
    "G4": 2.190385,
    "G5": -3.402547,
    "G6": -3.217731,
    "G7": 0.560305,
    "G8": 0.0,
    "G9": 0.0,
}
KBD_YIELDS = [
    {"tenor_years": 0.25, "yield_pct": 12.4175},
    {"tenor_years": 0.5, "yield_pct": 12.9366},
    {"tenor_years": 1.0, "yield_pct": 13.8055},
    {"tenor_years": 2.0, "yield_pct": 15.0208},
]

_SQL = """
INSERT INTO ref.issuer (issuer_id, name, short_name, inn, is_sovereign) VALUES
  (1, 'Министерство финансов Российской Федерации', 'Минфин РФ', '7710168360', true),
  (2, 'ПАО Тест', 'Тест', '7700000002', false),
  (3, 'АО Без ИНН', NULL, NULL, false);

INSERT INTO ref.bond (secid, isin, issuer_id, name, short_name, bond_class, coupon_type,
    coupon_type_basis, coupon_benchmark, coupon_benchmark_spread_pct, face_value,
    initial_face_value, face_unit, currency_id, issue_date, maturity_date, is_perpetual,
    coupon_frequency, is_amortizing, is_subordinated, is_fx_face, primary_board,
    marketprice_board, status) VALUES
  ('SU26242RMFS0', 'SU26242RMFS0', 1, 'ОФЗ-ПД 26242', 'ОФЗ 26242', 'ofz_pd', 'fixed',
   'ofz_series', NULL, NULL, 1000, 1000, 'RUB', 'RUB', '2019-05-15', '2029-08-29', false,
   2, false, false, false, 'TQOB', 'TQOB', 'active'),
  ('RU000A105X12', 'RU000A105X12', 2, 'Тест БО-01', 'Тест БО-01', 'corporate', 'floating',
   'benchmark', 'RREFKEYR', 1.5, 1000, 1000, 'RUB', 'RUB', '2023-01-15', '2028-01-15', false,
   4, false, true, false, 'TQCB', 'TQCB', 'active'),
  ('RU000PERP001', 'RU000PERP001', 2, 'Тест бессрочный', 'Тест perp', 'corporate', 'fixed',
   'moex_label', NULL, NULL, 1000, 1000, 'RUB', 'RUB', '2023-01-15', NULL, true,
   2, false, true, false, 'TQCB', 'TQCB', 'active'),
  ('DUPA', 'RU000DUP0000', 3, 'Дубль A', 'Дубль A', 'corporate', 'fixed',
   'moex_label', NULL, NULL, 1000, 1000, 'USD', 'RUB', '2023-01-15', '2027-06-01', false,
   2, false, false, true, 'TQCB', 'TQCB', 'active'),
  ('DUPB', 'RU000DUP0000', 3, 'Дубль B', 'Дубль B', 'corporate', 'fixed',
   'moex_label', NULL, NULL, 1000, 1000, 'USD', 'RUB', '2023-01-15', '2027-06-01', false,
   2, false, false, true, 'TQOD', 'TQOD', 'active'),
  ('OLD1', 'RU000OLD0001', 2, 'Старый', 'Старый', 'corporate', 'fixed',
   'schedule', NULL, NULL, 1000, 1000, 'RUB', 'RUB', '2015-01-15', '2020-01-15', false,
   2, false, false, false, 'TQCB', 'TQCB', 'matured');

INSERT INTO ref.bond_board (secid, board, is_primary, listed) VALUES
  ('SU26242RMFS0', 'TQOB', true, true), ('SU26242RMFS0', 'PSOB', false, true),
  ('RU000A105X12', 'TQCB', true, true), ('DUPA', 'TQCB', true, true),
  ('DUPB', 'TQOD', true, false);

INSERT INTO ref.bond_coupon (secid, coupon_no, start_date, pay_date, amount, value_prc,
    face_value, face_unit, status) VALUES
  ('SU26242RMFS0', 14, '2026-03-04', '2026-09-02', 44.88, 9.0, 1000, 'RUB', 'known'),
  ('SU26242RMFS0', 15, '2026-09-02', '2027-03-03', 44.88, 9.0, 1000, 'RUB', 'known'),
  ('RU000A105X12', 8, '2026-07-15', '2026-10-15', 40.00, 16.0, 1000, 'RUB', 'known'),
  ('RU000A105X12', 9, '2026-10-15', '2027-01-15', NULL, NULL, 1000, 'RUB', 'unknown');

INSERT INTO ref.bond_amortization (secid, amort_date, data_source, value_prc, value,
    face_value, initial_face_value, face_unit) VALUES
  ('SU26242RMFS0', '2029-08-29', 'maturity', 100, 1000, 1000, 1000, 'RUB');

INSERT INTO ref.bond_offer (secid, source, offer_key, offer_date, offer_type, status, price) VALUES
  ('RU000A105X12', 'moex_iss', '2027-01-15', '2027-01-15', 'put', 'scheduled', 100),
  ('RU000A105X12', 'moex_iss', '2027-07-15', '2027-07-15', 'call', 'scheduled', 100);

INSERT INTO ref.bond_rate_formula (secid, source, benchmark, benchmark_raw, spread_bp, status)
VALUES ('RU000A105X12', 'moex_iss', 'KEY_RATE', 'RREFKEYR', 150, 'inferred');

INSERT INTO md.bond_eod (trade_date, secid, board, close, waprice, legal_close, accrued_int,
    face_value, face_unit, currency_id, value, yield_close_pct, num_trades, ingested_at) VALUES
  ('2026-10-15', 'SU26242RMFS0', 'TQOB', 97.85, 97.80, 97.82, 1.23, 1000, 'SUR', 'SUR',
   1000000.50, 15.40, 120, '2026-10-15 16:00Z'),
  ('2026-10-15', 'SU26242RMFS0', 'PSOB', NULL, NULL, NULL, 1.23, 1000, 'SUR', 'SUR',
   NULL, NULL, 0, '2026-10-15 16:00Z'),
  ('2026-10-15', 'RU000A105X12', 'TQCB', 101.5, 101.40, 101.45, 5.10, 1000, 'SUR', 'SUR',
   2500000, 17.10, 30, '2026-10-15 16:00Z'),
  ('2026-10-15', 'DUPA', 'TQCB', 99.0, 99.1, NULL, 3.0, 1000, 'USD', 'SUR',
   10000, NULL, 2, '2026-10-15 16:00Z'),
  ('2026-10-14', 'SU26242RMFS0', 'TQOB', 97.70, 97.65, 97.66, 1.0, 1000, 'SUR', 'SUR',
   900000, 15.45, 100, '2026-10-14 16:00Z');

INSERT INTO md.bond_eod_yield (trade_date, secid, board, yield_date, yield_date_type, price,
    effective_yield_pct, waprice, effective_yield_waprice_pct, gspread_bp, ingested_at) VALUES
  ('2026-10-15', 'SU26242RMFS0', 'TQOB', '2029-08-29', 'MATDATE', 97.85, 15.42, 97.80, 15.45,
   12, '2026-10-15 16:00Z'),
  ('2026-10-15', 'RU000A105X12', 'TQCB', '2027-01-15', 'OFFERDATE', 101.5, 17.0, 101.30, 17.2,
   NULL, '2026-10-15 16:00Z');

INSERT INTO md.fixing (index_code, fixing_date, source, value, effective_date, published_on,
    ingested_at) VALUES
  ('KEY_RATE', '2026-10-15', 'cbr', 16.5, '2026-10-15', NULL, '2026-10-14 09:00Z'),
  ('KEY_RATE', '2026-10-14', 'cbr', 16.5, '2026-10-14', NULL, '2026-10-13 09:00Z'),
  ('RUONIA', '2026-10-14', 'cbr', 16.32, NULL, '2026-10-15', '2026-10-15 12:00Z'),
  ('RUONIA', '2026-10-13', 'cbr', 16.30, NULL, '2026-10-14', '2026-10-14 12:00Z'),
  ('TERM_RUONIA_1M', '2026-10-15', 'cbr', 16.1, NULL, NULL, '2026-10-15 12:00Z');

INSERT INTO md.index_eod (trade_date, secid, close, basis) VALUES
  ('2026-10-15', 'RUSFAR', 16.10, 'history'),
  ('2026-10-15', 'RUSFAR1W', 16.20, 'history'),
  ('2026-10-15', 'RUSFARCNY', 7.50, 'marketdata'),
  ('2026-10-15', 'RGBI', 115.3, 'history');

INSERT INTO md.fx_official (effective_date, ccy, nominal, value, rate_per_unit, record_date,
    set_date, cbr_id) VALUES
  ('2026-10-15', 'USD', 1, 81.1200, 81.1200, '2026-10-15', '2026-10-14', 'R01235'),
  ('2026-10-16', 'USD', 1, 81.5000, 81.5000, '2026-10-16', '2026-10-15', 'R01235'),
  ('2026-10-14', 'USD', 1, 81.0000, 81.0000, '2026-10-14', '2026-10-13', 'R01235'),
  ('2026-10-15', 'JPY', 100, 54.3210, 0.543210, '2026-10-15', '2026-10-14', 'R01820');

INSERT INTO md.fx_indicative (trade_date, clearing, ccy_pair, base_ccy, quote_ccy, trade_time,
    rate) VALUES
  ('2026-10-15', 'mc', 'USD/RUB', 'USD', 'RUB', '19:00', 81.45),
  ('2026-10-15', 'tc', 'USD/RUB', 'USD', 'RUB', '14:00', 81.30),
  ('2026-10-15', 'mc', 'CNY/RUB', 'CNY', 'RUB', '19:00', 11.35),
  ('2026-10-15', 'mc', 'EUR/USD', 'EUR', 'USD', '19:00', 1.0850);

INSERT INTO md.ecb_fx (rate_date, ccy, rate_per_eur) VALUES
  ('2026-10-15', 'USD', 1.0850), ('2026-10-15', 'CHF', 0.9400);

INSERT INTO ref.fx_instrument (secid, board, base_ccy, quote_ccy, kind, tenor, first_seen) VALUES
  ('CNYRUB_TOM', 'CETS', 'CNY', 'RUB', 'outright', 'TOM', '2026-01-01');
INSERT INTO md.fx_eod (trade_date, secid, board, close, waprice) VALUES
  ('2026-10-15', 'CNYRUB_TOM', 'CETS', 11.3400, 11.3350);

INSERT INTO ref.futures_contract (secid, asset_code, is_fx, base_ccy, quote_ccy, contract_type,
    last_trade_date, min_step, step_price, lot_volume, spec_source, first_seen) VALUES
  ('SiZ6', 'Si', true, 'USD', 'RUB', 'dated', '2026-12-17', 1, 1, 1000, 'rfud', '2026-01-01'),
  ('USDRUBF', 'USDRUBF', true, 'USD', 'RUB', 'perpetual', NULL, 0.001, 1, 1, 'rfud',
   '2026-01-01'),
  ('BRZ6', 'BR', false, NULL, NULL, 'dated', '2026-12-01', 0.01, 7.5, 10, 'rfud', '2026-01-01');
INSERT INTO md.futures_eod (trade_date, secid, board, asset_code, close, settle_price,
    settle_rub_per_unit, close_rub_per_unit) VALUES
  ('2026-10-15', 'SiZ6', 'RFUD', 'Si', 82500, 82450, 82.45, 82.5),
  ('2026-10-15', 'USDRUBF', 'RFUD', 'USDRUBF', 81.6, 81.55, 81.55, 81.6),
  ('2026-10-15', 'BRZ6', 'RFUD', 'BR', 70.1, 70.0, NULL, NULL);

INSERT INTO ref.cbr_meeting (meeting_date, effective_date, is_estimated) VALUES
  ('2026-10-24', '2026-10-27', true), ('2026-09-12', '2026-09-15', false);

INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) VALUES
  ('MOEX_STOCK', '2026-10-15', 'curated', true, '2026-01-01Z'),
  ('MOEX_STOCK', '2026-10-17', 'curated', false, '2026-01-01Z'),
  ('MOEX_STOCK', '2026-10-17', 'moex_iss_calendar', true, '2026-06-01Z'),
  ('RU_PRODUCTION', '2026-10-17', 'curated', false, '2026-01-01Z');

INSERT INTO ops.job_run (job, status, started_at, finished_at, rows_inserted) VALUES
  ('bonds_eod', 'succeeded', '2026-10-15 16:20Z', '2026-10-15 16:25Z', 10),
  ('cbr_daily', 'failed', '2026-10-15 06:00Z', '2026-10-15 06:01Z', 0);

INSERT INTO ops.dataset_status (dataset, table_name, frequency, first_date, last_date,
    row_count, backfill_job) VALUES
  ('bonds_eod', 'md.bond_eod', 'daily', '2026-10-14', '2026-10-15', 5, 'bonds_eod');
"""


def seed(conn: psycopg.Connection[DictRow]) -> None:
    """Insert the data set and commit."""
    conn.execute(_SQL)  # type: ignore[arg-type]
    conn.execute(
        "INSERT INTO md.zcyc_snapshot (trade_date, trade_time, source_ts, params, standard_yields,"
        " check_max_error_bp, check_tolerance_bp, is_eod) VALUES"
        " ('2026-10-15', '18:54:59', '2026-10-15 15:54:59Z', %s, %s, 0.001, 0.01, true),"
        " ('2026-10-15', '12:00:00', '2026-10-15 09:00:00Z', %s, %s, 0.001, 0.01, false)",
        (json.dumps(KBD_PARAMS), json.dumps(KBD_YIELDS), json.dumps(KBD_PARAMS), "[]"),
    )
    snaps: list[tuple[str, list[tuple[str, int, Any, Any]]]] = [
        ("2026-10-15 10:00Z", [("1Y", 0, 15.10, 15.30), ("2Y", 1, 14.60, 14.80)]),
        ("2026-10-15 12:00Z", [("1Y", 0, 15.20, 15.40), ("2Y", 1, 14.70, 14.90)]),
    ]
    for i, (fetched, quotes) in enumerate(snaps):
        sid = conn.execute(
            "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at,"
            " snapshot_date, content_sha256, parser_version) VALUES"
            " ('ratescalcs', 'ruonia_ois', 'bid_ask_mid', %s, '2026-10-15', %s, '1') RETURNING id",
            (fetched, f"{i:064x}"),
        ).fetchone()
        assert sid is not None
        for tenor, pos, bid, ask in quotes:
            conn.execute(
                "INSERT INTO md.curve_quote (snapshot_id, tenor, position, tenor_months, bid_pct,"
                " ask_pct, mid_pct) VALUES (%s, %s, %s, %s, %s, %s, (%s::numeric + %s) / 2)",
                (sid["id"], tenor, pos, 12 * int(tenor[0]), bid, ask, bid, ask),
            )
    conn.commit()
