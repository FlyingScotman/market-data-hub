"""Market snapshot builder: bond quotes, curve quote sets, ZCYC, fixings, FX, meetings.

Rows are read **as known at** ``known_at`` (``md.*_known_at`` readers of migrations 0410/0500;
insert-only tables filter on ``ingested_at``). Facts are published as the hub stores them:
``dataset`` and tenor codes, price bases as separate labelled fields, units in field names,
MOEX analytics under ``source_analytics``. No forward fill: a series without a value on the
business date is reported in the dataset's coverage / stale flags, never copied from an earlier
date (dated datasets carry each row's own date inside an explicit window).

Bond quotes are published only for instruments of the referenced static snapshot; quotes of
other SECIDs are listed in ``excluded`` (reason ``not_in_static_snapshot``). RUB FX is RUB per
1 unit; MOEX FX rows that are swaps, non-RUB pairs or precious metals are excluded with a
reason. Non-RUB cross rates come from the ECB only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from mdhub.core.timeutil import MSK
from mdhub.db.pool import DictConnection
from mdhub.snapshot.build import (
    Build,
    Cut,
    Dataset,
    Excluded,
    Provenance,
    json_decimal,
    sorted_labels,
)

__all__ = [
    "EXPECTED_CURVES",
    "PRECIOUS_METALS",
    "MarketParams",
    "build_market",
]

EXPECTED_CURVES: Final = (
    ("otc_monitor", "cny_ruonia_xccy"),
    ("otc_monitor", "cny_rusfar_ois"),
    ("otc_monitor", "key_rate_swap"),
    ("otc_monitor", "ruonia_ois"),
    ("ratescalcs", "cny_futures_yields"),
    ("ratescalcs", "cny_ruonia_xccy"),
    ("ratescalcs", "key_rate_swap"),
    ("ratescalcs", "ruonia_ois"),
    ("ratescalcs", "usd_futures_yields"),
)
"""Live (source, dataset) quote-set series; a missing one is a coverage gap, not an error."""

PRECIOUS_METALS: Final = frozenset({"GLD", "SLV", "PLT", "PLD"})
_OIS_LIKE: Final = frozenset({"ruonia_ois", "key_rate_swap"})


@dataclass(frozen=True, slots=True)
class MarketParams:
    """Lookback windows (calendar days) of the dated datasets, per cut type."""

    fixing_lookback_days: int = 400
    """EOD cuts: fixings with fixing_date in [D - n, D + fx_forward_days] (floater history)."""
    intraday_fixing_lookback_days: int = 7
    fx_lookback_days: int = 0
    """CBR official rates: effective dates in [D - n, D + fx_forward_days] (one row per
    calendar day the rate is in force)."""
    fx_forward_days: int = 7
    """CBR rates set on D are in force from D+1 (over weekends / holidays for several days):
    the announced rates are part of the cut. Also the forward edge of the fixing window."""
    ecb_lookback_days: int = 7

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixing_lookback_days": self.fixing_lookback_days,
            "intraday_fixing_lookback_days": self.intraday_fixing_lookback_days,
            "fx_lookback_days": self.fx_lookback_days,
            "fx_forward_days": self.fx_forward_days,
            "ecb_lookback_days": self.ecb_lookback_days,
        }


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), tzinfo=MSK)
    return start, start + timedelta(days=1)


def _scale_exact(value: Decimal | None, units: Decimal | None) -> Decimal | None:
    """``value / units`` when ``units`` is a power of ten (exact); else ``None``."""
    if value is None or units is None or units <= 0:
        return None
    norm = units.normalize()
    sign, digits, exp = norm.as_tuple()
    if digits != (1,) or not isinstance(exp, int) or exp < 0 or sign:
        return None
    return value.scaleb(-exp)


# ------------------------------------------------------------------------ bond quotes


def _bond_quotes_eod(
    conn: DictConnection,
    t: datetime,
    d: date,
    universe: frozenset[str],
    prov: Provenance,
    excluded: Excluded,
) -> Dataset:
    rows = conn.execute(
        """
        SELECT COALESCE(e.trade_date, y.trade_date) AS trade_date,
               COALESCE(e.secid, y.secid) AS secid, COALESCE(e.board, y.board) AS board,
               e.open, e.low, e.high, e.close, e.legal_close, e.waprice, e.market_price2,
               e.market_price3, e.admitted_quote, e.accrued_int, e.face_value, e.face_unit,
               e.currency_id, e.volume, e.value, e.num_trades, e.trading_session,
               e.yield_close_pct, e.yield_at_waprice_pct,
               COALESCE(y.yield_to_offer_pct, e.yield_to_offer_pct) AS yield_to_offer_pct,
               COALESCE(y.duration_days, e.duration_days) AS duration_days,
               COALESCE(y.zspread_bp, e.zspread_bp) AS zspread_bp,
               y.effective_yield_pct, y.effective_yield_waprice_pct, y.yield_date,
               y.yield_date_type, y.gspread_bp,
               e.run_id AS e_run, e.ingested_at AS e_at, e.updated_at AS e_upd,
               y.run_id AS y_run, y.ingested_at AS y_at, y.updated_at AS y_upd
        FROM md.bond_eod_known_at(%(t)s, %(d)s, %(d)s) e
        FULL JOIN md.bond_eod_yield_known_at(%(t)s, %(d)s, %(d)s) y
          ON y.trade_date = e.trade_date AND y.secid = e.secid AND y.board = e.board
        ORDER BY 2, 3
        """,
        {"t": t, "d": d},
    ).fetchall()
    out = []
    for r in rows:
        if r["secid"] not in universe:
            excluded.add(
                "bond_quotes",
                {"secid": r["secid"], "board": r["board"], "trade_date": r["trade_date"]},
                "not_in_static_snapshot",
            )
            continue
        if r["e_at"] is not None:
            prov.add("bond_quotes", "moex_iss", r["e_run"], r["e_upd"] or r["e_at"])
        if r["y_at"] is not None:
            prov.add("bond_quotes", "moex_iss", r["y_run"], r["y_upd"] or r["y_at"])
        out.append(
            {
                "secid": r["secid"],
                "board": r["board"],
                "trade_date": r["trade_date"],
                "open_pct": r["open"],
                "low_pct": r["low"],
                "high_pct": r["high"],
                "close_pct": r["close"],
                "legal_close_pct": r["legal_close"],
                "waprice_pct": r["waprice"],
                "market_price2_pct": r["market_price2"],
                "market_price3_pct": r["market_price3"],
                "admitted_quote_pct": r["admitted_quote"],
                "accrued_per_bond": r["accrued_int"],
                "face_value": r["face_value"],
                "face_unit": r["face_unit"],
                "currency_id": r["currency_id"],
                "volume_bonds": r["volume"],
                "value_ccy": r["value"],
                "num_trades": r["num_trades"],
                "trading_session": r["trading_session"],
                "source_analytics": {
                    "yield_close_pct": r["yield_close_pct"],
                    "yield_at_waprice_pct": r["yield_at_waprice_pct"],
                    "yield_to_offer_pct": r["yield_to_offer_pct"],
                    "effective_yield_pct": r["effective_yield_pct"],
                    "effective_yield_waprice_pct": r["effective_yield_waprice_pct"],
                    "yield_date": r["yield_date"],
                    "yield_date_type": r["yield_date_type"],
                    "duration_days": r["duration_days"],
                    "zspread_bp": r["zspread_bp"],
                    "gspread_bp": r["gspread_bp"],
                },
            }
        )
    quoted = {r["secid"] for r in out}
    return Dataset(
        "bond_quotes",
        out,
        {
            "trade_date": d,
            "secids": len(quoted),
            "universe": len(universe),
            "universe_without_quote": len(universe - quoted),
        },
        [] if out else [f"no EOD bond rows for {d.isoformat()}"],
    )


def _bond_quotes_intraday(
    conn: DictConnection,
    t: datetime,
    d: date,
    universe: frozenset[str],
    prov: Provenance,
    excluded: Excluded,
) -> Dataset:
    start, end = _day_bounds(d)
    rows = conn.execute(
        """
        SELECT DISTINCT ON (q.secid, q.board) q.*
        FROM md.bond_quote_intraday q
        WHERE q.ts >= %(start)s AND q.ts < %(end)s AND q.ingested_at <= %(t)s
        ORDER BY q.secid, q.board, q.ts DESC
        """,
        {"start": start, "end": end, "t": t},
    ).fetchall()
    out = []
    for r in rows:
        if r["secid"] not in universe:
            excluded.add(
                "bond_quotes_intraday",
                {"secid": r["secid"], "board": r["board"], "ts": r["ts"]},
                "not_in_static_snapshot",
            )
            continue
        prov.add("bond_quotes_intraday", "moex_iss", r["run_id"], r["ingested_at"])
        out.append(
            {
                "secid": r["secid"],
                "board": r["board"],
                "ts": r["ts"],
                "source_ts": r["source_ts"],
                "latency": r["latency"],
                "bid_pct": r["bid"],
                "offer_pct": r["offer"],
                "last_pct": r["last"],
                "waprice_pct": r["waprice"],
                "market_price_pct": r["market_price"],
                "close_price_pct": r["close_price"],
                "num_trades": r["num_trades"],
                "vol_today": r["vol_today"],
                "val_today": r["val_today"],
                "trading_status": r["trading_status"],
                "trading_session": r["trading_session"],
                "source_analytics": {
                    "yield_pct": r["yield_pct"],
                    "yield_at_waprice_pct": r["yield_at_waprice_pct"],
                    "yield_to_offer_pct": r["yield_to_offer_pct"],
                    "effective_yield_pct": r["effective_yield_pct"],
                    "yield_date": r["yield_date"],
                    "yield_date_type": r["yield_date_type"],
                    "duration_days": r["duration_days"],
                    "zspread_bp": r["zspread_bp"],
                    "gspread_bp": r["gspread_bp"],
                },
            }
        )
    latest = max((r["source_ts"] for r in out), default=None)
    return Dataset(
        "bond_quotes_intraday",
        out,
        {"secids": len({r["secid"] for r in out}), "latest_source_ts": latest},
        [] if out else [f"no intraday bond quotes on {d.isoformat()}"],
    )


# ---------------------------------------------------------------------------- curves


def _curve_sets(conn: DictConnection, t: datetime, d: date, prov: Provenance) -> Dataset:
    snaps = conn.execute(
        """
        SELECT DISTINCT ON (s.source, s.dataset) s.*, s.meta::text AS meta_text
        FROM md.curve_snapshot s
        WHERE s.snapshot_date = %(d)s AND s.fetched_at <= %(t)s AND s.ingested_at <= %(t)s
        ORDER BY s.source, s.dataset, s.fetched_at DESC, s.id DESC
        """,
        {"d": d, "t": t},
    ).fetchall()
    quotes: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if snaps:
        for q in conn.execute(
            "SELECT q.*, q.extra::text AS extra_text FROM md.curve_quote q "
            "WHERE q.snapshot_id = ANY(%(ids)s) ORDER BY q.snapshot_id, q.position, q.tenor",
            {"ids": [s["id"] for s in snaps]},
        ).fetchall():
            quotes[q["snapshot_id"]].append(q)
    out = []
    stale: list[str] = []
    for s in snaps:
        clock_day = s["source_ts"].astimezone(MSK).date() if s["source_ts"] else None
        off_day = clock_day is not None and clock_day != d
        labels = sorted_labels(
            "mid_only" if s["quote_basis"] == "mid_only" else None,
            "market_closed" if s["market_closed"] else None,
            "source_clock_not_on_business_date" if off_day else None,
        )
        series = f"{s['source']}/{s['dataset']}"
        if off_day:
            stale.append(f"{series}: source clock on {clock_day}")
        if s["dataset"] in _OIS_LIKE and s["quote_basis"] == "mid_only":
            stale.append(f"{series}: mid-only quotes (off-hours mark)")
        prov.add("curve_quote_sets", s["source"], s["run_id"], s["ingested_at"])
        out.append(
            {
                "quote_set_id": s["id"],
                "dataset": s["dataset"],
                "source": s["source"],
                "quote_basis": s["quote_basis"],
                "fetched_at": s["fetched_at"],
                "source_ts": s["source_ts"],
                "snapshot_date": s["snapshot_date"],
                "market_closed": s["market_closed"],
                "label": s["label"],
                "content_sha256": s["content_sha256"],
                "labels": labels,
                "quotes": [
                    {
                        "tenor": q["tenor"],
                        "position": q["position"],
                        "tenor_months": q["tenor_months"],
                        "tenor_days": q["tenor_days"],
                        "pillar_date": q["pillar_date"],
                        "bid_pct": q["bid_pct"],
                        "mid_pct": q["mid_pct"],
                        "ask_pct": q["ask_pct"],
                        "extra": _extra(q["extra_text"]),
                    }
                    for q in quotes[s["id"]]
                ],
            }
        )
    present = {(s["source"], s["dataset"]) for s in snaps}
    return Dataset(
        "curve_quote_sets",
        out,
        {
            "expected": [f"{a}/{b}" for a, b in EXPECTED_CURVES],
            "present": sorted(f"{a}/{b}" for a, b in present),
            "missing": [f"{a}/{b}" for a, b in EXPECTED_CURVES if (a, b) not in present],
        },
        stale,
    )


def _extra(text: str | None) -> dict[str, Any] | None:
    """``curve_quote.extra`` with numbers as decimal strings, nested values as JSON text."""
    raw = json_decimal(text)
    if raw is None:
        return None
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if v is None or isinstance(v, bool | str):
            out[str(k)] = v
        elif isinstance(v, Decimal):
            out[str(k)] = format(v, "f")
        else:
            out[str(k)] = str(v)
    return out


# ------------------------------------------------------------------------------ ZCYC

_ZCYC_PARAMS: Final = ("B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10)))
_ZCYC_SELECT: Final = (
    "SELECT z.*, z.params::text AS params_text, z.standard_yields::text AS yields_text "
    "FROM md.zcyc_snapshot_known_at(%(t)s, %(d)s, %(d)s) z "
)
_ZCYC_EOD_SQL: Final = _ZCYC_SELECT + "ORDER BY z.is_eod DESC, z.trade_time DESC LIMIT 1"
"""EOD cuts: the end-of-day publication, else the latest one (flagged stale)."""
_ZCYC_LATEST_SQL: Final = _ZCYC_SELECT + "ORDER BY z.trade_time DESC LIMIT 1"


def _zcyc(conn: DictConnection, t: datetime, d: date, eod: bool, prov: Provenance) -> Dataset:
    query = _ZCYC_EOD_SQL if eod else _ZCYC_LATEST_SQL
    rows = conn.execute(query, {"t": t, "d": d}).fetchall()
    out = []
    stale: list[str] = []
    for z in rows:
        params = json_decimal(z["params_text"]) or {}
        yields = json_decimal(z["yields_text"]) or []
        prov.add("zcyc_params", "moex_iss", z["run_id"], z["updated_at"] or z["ingested_at"])
        out.append(
            {
                "trade_date": z["trade_date"],
                "trade_time": z["trade_time"],
                "source_ts": z["source_ts"],
                "is_eod": bool(z["is_eod"]),
                "params": {n: params.get(n) for n in _ZCYC_PARAMS},
                "standard_yields": [
                    {"tenor_years": y["tenor_years"], "yield_pct": y["yield_pct"]} for y in yields
                ],
                "check_max_error_bp": z["check_max_error_bp"],
                "check_tolerance_bp": z["check_tolerance_bp"],
            }
        )
        if eod and not z["is_eod"]:
            stale.append(f"no end-of-day ZCYC publication; latest intraday {z['trade_time']}")
    if not out:
        stale.append(f"no ZCYC publication on {d.isoformat()}")
    return Dataset("zcyc_params", out, {"trade_date": d}, stale)


# --------------------------------------------------------------------------- fixings


def _fixings(
    conn: DictConnection, t: datetime, d: date, lookback: int, forward: int, prov: Provenance
) -> Dataset:
    lo, hi = d - timedelta(days=lookback), d + timedelta(days=forward)
    fix = conn.execute(
        "SELECT f.*, f.details::text AS details_text "
        "FROM md.fixing_known_at(%(t)s, %(lo)s, %(hi)s) f "
        "ORDER BY f.index_code, f.source, f.fixing_date",
        {"t": t, "lo": lo, "hi": hi},
    ).fetchall()
    rusfar = conn.execute(
        "SELECT * FROM md.index_eod_known_at(%(t)s, %(lo)s, %(d)s) i "
        "WHERE i.secid LIKE 'RUSFAR%%' AND i.close IS NOT NULL ORDER BY i.secid, i.trade_date",
        {"t": t, "lo": lo, "d": d},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for f in fix:
        prov.add("fixings", f["source"], f["run_id"], f["ingested_at"])
        out.append(
            {
                "index_code": f["index_code"],
                "source": f["source"],
                "fixing_date": f["fixing_date"],
                "effective_date": f["effective_date"],
                "published_on": f["published_on"],
                "published_at": f["published_at"],
                "value": f["value"],
                "unit": f["unit"],
                "origin": "md.fixing",
                "labels": [],
            }
        )
    for i in rusfar:
        prov.add("fixings", "moex_iss", i["run_id"], i["updated_at"] or i["ingested_at"])
        out.append(
            {
                "index_code": i["secid"],
                "source": "moex_iss",
                "fixing_date": i["trade_date"],
                "effective_date": None,
                "published_on": None,
                "published_at": None,
                "value": i["close"],
                "unit": "pct",
                "origin": "md.index_eod",
                "labels": sorted_labels(
                    "moex_marketdata_preliminary" if i["basis"] == "marketdata" else None
                ),
            }
        )
    out.sort(key=lambda r: (r["index_code"], r["source"], r["fixing_date"]))
    last: dict[str, date] = {}
    for r in out:
        last[r["index_code"]] = max(last.get(r["index_code"], r["fixing_date"]), r["fixing_date"])
    stale = []
    if "KEY_RATE" in last and last["KEY_RATE"] < d:
        stale.append(f"KEY_RATE: latest fixing {last['KEY_RATE']} before {d}")
    elif "KEY_RATE" not in last:
        stale.append("KEY_RATE: no fixing in the window")
    return Dataset(
        "fixings",
        out,
        {"window_from": lo, "window_to": hi, "latest": dict(sorted(last.items()))},
        stale,
    )


# -------------------------------------------------------------------------------- FX


def _fx(
    conn: DictConnection,
    t: datetime,
    d: date,
    params: MarketParams,
    eod: bool,
    prov: Provenance,
    excluded: Excluded,
) -> Dataset:
    lo, hi = d - timedelta(days=params.fx_lookback_days), d + timedelta(days=params.fx_forward_days)
    out: list[dict[str, Any]] = []
    blank: dict[str, Any] = {
        "rate_rub_per_unit": None,
        "effective_date": None,
        "set_date": None,
        "nominal": None,
        "trade_date": None,
        "trade_time": None,
        "secid": None,
        "board": None,
        "tenor": None,
        "quote_units": None,
        "close": None,
        "waprice": None,
        "close_rub_per_unit": None,
        "waprice_rub_per_unit": None,
        "num_trades": None,
        "labels": [],
    }
    for r in conn.execute(
        "SELECT * FROM md.fx_official_known_at(%(t)s, %(lo)s, %(hi)s) ORDER BY ccy, effective_date",
        {"t": t, "lo": lo, "hi": hi},
    ).fetchall():
        prov.add("fx_rates", r["source"], r["run_id"], r["ingested_at"])
        out.append(
            {
                **blank,
                "kind": "cbr_official",
                "ccy": r["ccy"],
                "rate_rub_per_unit": r["rate_per_unit"],
                "effective_date": r["effective_date"],
                "set_date": r["set_date"],
                "nominal": r["nominal"],
            }
        )
    in_force = any(r["effective_date"] == d for r in out)
    if eod:
        for r in conn.execute(
            """
            SELECT e.*, i.kind AS inst_kind, i.base_ccy, i.quote_ccy, i.tenor, i.face_value
            FROM md.fx_eod_known_at(%(t)s, %(d)s, %(d)s) e
            LEFT JOIN ref.fx_instrument_known_at(%(t)s) i
              ON i.secid = e.secid AND i.board = e.board
            ORDER BY e.secid, e.board
            """,
            {"t": t, "d": d},
        ).fetchall():
            key = {"secid": r["secid"], "board": r["board"], "trade_date": r["trade_date"]}
            reason = None
            if r["inst_kind"] is None:
                reason = "instrument_unknown"
            elif r["inst_kind"] != "outright":
                reason = "fx_swap" if r["inst_kind"] == "swap" else "not_an_outright"
            elif r["quote_ccy"] != "RUB":
                reason = "non_rub_pair"
            elif r["base_ccy"] in PRECIOUS_METALS:
                reason = "precious_metal"
            if reason:
                excluded.add("fx_rates", key, reason)
                continue
            prov.add("fx_rates", "moex_iss", r["run_id"], r["ingested_at"])
            close_u = _scale_exact(r["close"], r["face_value"])
            wap_u = _scale_exact(r["waprice"], r["face_value"])
            out.append(
                {
                    **blank,
                    "kind": "moex_eod",
                    "ccy": r["base_ccy"],
                    "trade_date": r["trade_date"],
                    "secid": r["secid"],
                    "board": r["board"],
                    "tenor": r["tenor"],
                    "quote_units": r["face_value"],
                    "close": r["close"],
                    "waprice": r["waprice"],
                    "close_rub_per_unit": close_u,
                    "waprice_rub_per_unit": wap_u,
                    "num_trades": r["num_trades"],
                    "labels": sorted_labels(
                        "quote_units_not_power_of_ten"
                        if r["face_value"] is not None
                        and _scale_exact(Decimal(1), r["face_value"]) is None
                        else None,
                        "no_price" if r["close"] is None and r["waprice"] is None else None,
                    ),
                }
            )
    for r in conn.execute(
        "SELECT * FROM md.fx_indicative_known_at(%(t)s, %(d)s, %(d)s) ORDER BY clearing, ccy_pair",
        {"t": t, "d": d},
    ).fetchall():
        key = {"trade_date": r["trade_date"], "clearing": r["clearing"], "ccy_pair": r["ccy_pair"]}
        if r["quote_ccy"] != "RUB":
            excluded.add("fx_rates", key, "non_rub_pair")
            continue
        prov.add("fx_rates", r["source"], r["run_id"], r["ingested_at"])
        out.append(
            {
                **blank,
                "kind": "moex_indicative_14" if r["clearing"] == "tc" else "moex_indicative_19",
                "ccy": r["base_ccy"],
                "rate_rub_per_unit": r["rate"],
                "trade_date": r["trade_date"],
                "trade_time": r["trade_time"].isoformat() if r["trade_time"] else None,
            }
        )

    def sort_key(r: dict[str, Any]) -> tuple[str, str, str, str, str]:
        day = r["effective_date"] or r["trade_date"]
        return (r["kind"], r["ccy"], day.isoformat(), r["secid"] or "", r["board"] or "")

    out.sort(key=sort_key)
    kinds = Counter(r["kind"] for r in out)
    stale = [] if in_force else [f"cbr_official: no rate in force on {d.isoformat()}"]
    return Dataset(
        "fx_rates",
        out,
        {"by_kind": dict(sorted(kinds.items())), "cbr_window_from": lo, "cbr_window_to": hi},
        stale,
    )


def _cross(conn: DictConnection, t: datetime, d: date, lookback: int, prov: Provenance) -> Dataset:
    lo = d - timedelta(days=lookback)
    rows = conn.execute(
        "SELECT * FROM md.ecb_fx_known_at(%(t)s, %(lo)s, %(d)s) ORDER BY rate_date, ccy",
        {"t": t, "lo": lo, "d": d},
    ).fetchall()
    for r in rows:
        prov.add("cross_rates", "ecb", r["run_id"], r["ingested_at"])
    latest = max((r["rate_date"] for r in rows), default=None)
    return Dataset(
        "cross_rates",
        [
            {
                "kind": "ecb",
                "base_ccy": "EUR",
                "ccy": r["ccy"],
                "rate_per_base": r["rate_per_eur"],
                "rate_date": r["rate_date"],
            }
            for r in rows
        ],
        {"window_from": lo, "window_to": d, "latest_rate_date": latest},
    )


def _meetings(conn: DictConnection, t: datetime, prov: Provenance) -> Dataset:
    rows = conn.execute(
        "SELECT * FROM ref.cbr_meeting_known_at(%(t)s) ORDER BY meeting_date", {"t": t}
    ).fetchall()
    prov.add_rows("cbr_meetings", rows)
    return Dataset(
        "cbr_meetings",
        [
            {
                "meeting_date": r["meeting_date"],
                "effective_date": r["effective_date"],
                "is_estimated": bool(r["is_estimated"]),
                "is_extraordinary": bool(r["is_extraordinary"]),
                "decision": r["decision"],
                "decision_rate_pct": r["decision_rate_pct"],
                "prev_rate_pct": r["prev_rate_pct"],
            }
            for r in rows
        ],
    )


def build_market(
    conn: DictConnection,
    *,
    cut: Cut,
    static: Build,
    static_snapshot_id: str | None = None,
    params: MarketParams | None = None,
) -> Build:
    """Build the market snapshot of ``cut`` consistent with the static ``static`` build
    (its quotes are filtered by ``static.universe``). ``static_snapshot_id`` may be bound
    later, once the static build is published (:func:`mdhub.snapshot.runner`)."""
    if cut.kind != "market":
        raise ValueError("build_market needs a market cut")
    params = params or MarketParams()
    t, d = cut.known_at, cut.business_date
    eod = cut.cut_type != "intraday"
    prov = Provenance()
    excluded = Excluded()
    universe = static.universe
    if eod:
        quotes = _bond_quotes_eod(conn, t, d, universe, prov, excluded)
        quotes_intraday = Dataset("bond_quotes_intraday", [], {"not_in_cut": "eod"})
    else:
        quotes = Dataset("bond_quotes", [], {"not_in_cut": "intraday"})
        quotes_intraday = _bond_quotes_intraday(conn, t, d, universe, prov, excluded)
    lookback = params.fixing_lookback_days if eod else params.intraday_fixing_lookback_days
    datasets = [
        quotes,
        quotes_intraday,
        _curve_sets(conn, t, d, prov),
        _zcyc(conn, t, d, eod, prov),
        _fixings(conn, t, d, lookback, params.fx_forward_days, prov),
        _fx(conn, t, d, params, eod, prov, excluded),
        _cross(conn, t, d, params.ecb_lookback_days, prov),
        _meetings(conn, t, prov),
    ]
    return Build(
        cut=cut,
        datasets=datasets,
        calendar=dict(static.calendar),
        parameters={
            **params.as_dict(),
            "expected_curves": [f"{a}/{b}" for a, b in EXPECTED_CURVES],
        },
        provenance=prov,
        excluded=excluded,
        static_snapshot_id=static_snapshot_id,
    )
