"""Phase 2: map the verbatim ``legacy_md`` copy into the normalized ``md`` / ``ref`` tables.

``mdhub legacy map-md`` runs :func:`map_md`: one ``ops.job_run`` (job ``legacy_map_md``) and
one transaction per target (a failing target is reported and the run ends ``partial``; the
others commit). Every target is idempotent: a re-run writes nothing new.

Targets (see ``MAPPING.md``):

=========  ==============================================  ====================================
target     legacy tables                                   hub table
=========  ==============================================  ====================================
fixings    index_fixings, term_ruonia_fixings              md.fixing
meetings   policy_meetings                                 ref.cbr_meeting
liquidity  cbr_liquidity_balance_revisions (+ captures)    md.cbr_liquidity (+ ops.row_history)
zcyc       gcurve_snapshots                                md.zcyc_snapshot / md.zcyc_reject
curves     curve_snapshots, curve_quotes                   md.curve_snapshot / md.curve_quote
intraday   intraday_quote_observations (+ captures)        md.fx_intraday / md.futures_intraday
=========  ==============================================  ====================================

Rules shared by all targets:

* **Live rows win.** A key that already exists in the hub table and was not written by a
  ``legacy_map_md`` run (``run_id`` of another job) is never touched; the legacy value is
  compared with it and the outcome counted (``live_match`` / ``live_mismatch``).
* **Provenance.** ``capture_id`` NULL (the export blanked the raw payloads), ``run_id`` = the
  mapping run, ``legacy_id`` / ``meta`` / ``details`` name the legacy table and row where the
  hub table has such a column. ``ingested_at`` (known-at) = the legacy ``fetched_at_utc``.
* **Numbers.** ``REAL`` -> ``Decimal(repr(x))``; Decimal text columns -> ``Decimal(text)``.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import logging
import time as _time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.capture import job_run, record_capture
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.db.pool import Database
from mdhub.legacy.import_sqlite import LegacyImportError
from mdhub.sources import ratescalcs as rc
from mdhub.sources.moex_market import (
    ZCYC_PARAM_NAMES,
    ZCYC_TOLERANCE_BP,
    ZcycSnapshot,
    check_zcyc,
)

__all__ = [
    "JOB",
    "TARGETS",
    "MapResult",
    "TargetReport",
    "map_md",
]

log = logging.getLogger(__name__)

JOB: Final = "legacy_map_md"
SCHEMA: Final = "legacy_md"
TARGETS: Final = ("fixings", "meetings", "liquidity", "zcyc", "curves", "intraday")
CHUNK: Final = 20_000
MAX_EXAMPLES: Final = 10

FIXING_CODES: Final = {"ruonia": "RUONIA", "key_rate": "KEY_RATE"}
TERM_COLUMNS: Final = (
    ("RUONIA_INDEX", "index_value", "index"),
    ("TERM_RUONIA_1M", "one_month_pct", "pct"),
    ("TERM_RUONIA_3M", "three_month_pct", "pct"),
    ("TERM_RUONIA_6M", "six_month_pct", "pct"),
)
LIQUIDITY_COLUMNS: Final = (
    # legacy column -> hub column, by POSITION in the CBR table (columns 2..15). The legacy
    # schema lists its columns in table order but labels columns 6 and 7 "repo/FX swap
    # standing" and "secured loans auctions"; the hub parser labels them "secured loans
    # auctions" and "repo/FX swap standing". Values agree with the live CBR load only by
    # position (checked on the 2026-09/10 overlap), so the mapping is positional.
    ("deficit_with_corr_accounts_bn", "deficit_bn"),
    ("deficit_without_corr_accounts_bn", "deficit_ex_corr_accounts_bn"),
    ("cbr_claims_total_bn", "cbr_claims_total_bn"),
    ("repo_fx_swap_auctions_bn", "repo_fxswap_auction_bn"),
    ("repo_fx_swap_standing_bn", "secured_loans_auction_bn"),
    ("secured_loans_auctions_bn", "repo_fxswap_standing_bn"),
    ("secured_loans_standing_bn", "secured_loans_standing_bn"),
    ("cbr_obligations_total_bn", "cbr_obligations_total_bn"),
    ("deposit_auctions_bn", "deposits_auction_bn"),
    ("deposits_standing_bn", "deposits_standing_bn"),
    ("cbr_bonds_bn", "cobr_bn"),
    ("nonstandard_net_claims_bn", "nonstandard_net_claims_bn"),
    ("bank_corr_accounts_bn", "corr_accounts_bn"),
    ("averaged_reserves_bn", "required_reserves_averaged_bn"),
)
CURVE_SOURCES: Final = {
    "ratescalcs": "ratescalcs",
    "andromeda": "andromeda",
    "otc_monitor": "otc_monitor",
    "cfets_chinamoney": "cfets",
}
CURVE_DATASETS: Final = {
    "cnyruonia_xccy": "cny_ruonia_xccy",
    "cfets_fr007_irs_closing": "cfets_fr007_irs",
    "cfets_fdr007_irs_closing": "cfets_fdr007_irs",
}
FULL_TENOR_SETS: Final = {"ruonia_ois": 17, "key_rate_swap": 13}
ANDROMEDA_PARTIAL_BEFORE: Final = date(2023, 7, 1)
INTRADAY_SOURCES: Final = {
    "moex_apim": "moex_apim",
    "moex_passport_realtime": "moex_passport",
    "ratescalcs": "ratescalcs",
}
INTRADAY_VARIANTS: Final = {"moex_apim": "apim_marketdata"}  # legacy -> hub; others kept
OBSTATS_VARIANT: Final = "algopack_obstats_mid"
USDRUBF_FALLBACK: Final = "usdrubf_fallback_snapshot_clock"
NTPRO_BOARD: Final = "NTPRO"


# ----------------------------------------------------------------------------- reports


@dataclass(slots=True)
class TargetReport:
    """Outcome of one target."""

    target: str
    source_rows: int = 0
    candidates: int = 0
    """Rows offered to the hub table after transformation / dedup."""
    counts: UpsertResult = field(default_factory=UpsertResult)
    live_overlap: int = 0
    live_match: int = 0
    live_mismatch: int = 0
    mismatch_examples: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_s: float = 0.0

    def note(self, key: str, n: int = 1) -> None:
        self.notes[key] = int(self.notes.get(key, 0)) + n

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "candidates": self.candidates,
            "inserted": self.counts.inserted,
            "updated": self.counts.updated,
            "unchanged": self.counts.unchanged,
            "live_overlap": self.live_overlap,
            "live_match": self.live_match,
            "live_mismatch": self.live_mismatch,
            "mismatch_examples": self.mismatch_examples,
            "notes": self.notes,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass(slots=True)
class MapResult:
    """Outcome of :func:`map_md`."""

    run_id: int
    targets: list[TargetReport]

    @property
    def ok(self) -> bool:
        return all(t.error is None for t in self.targets)


# ----------------------------------------------------------------------------- helpers


def dec(value: float | int | None) -> Decimal | None:
    """Exact decimal of a legacy REAL/INTEGER (shortest round-trip repr of the double)."""
    if value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(repr(float(value)))


def dec_text(value: str | None) -> Decimal | None:
    """Decimal of a legacy Decimal-text column (``bid_text`` ...)."""
    if value is None or value == "":
        return None
    return Decimal(value)


def parse_ts(value: str) -> datetime:
    """Legacy UTC timestamp (``Z``, ``.ffffffZ`` or ``+00:00``) -> aware datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive legacy timestamp {value!r}")
    return dt


def _legacy_runs(conn: psycopg.Connection[Any]) -> list[int]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute("SELECT id FROM ops.job_run WHERE job = %s", (JOB,)).fetchall()
    return [int(r[0]) for r in rows]


def _existing(
    conn: psycopg.Connection[Any],
    table: str,
    key_cols: Sequence[str],
    cmp_cols: Sequence[str],
    legacy_runs: Sequence[int],
    where: str = "true",
    params: Sequence[Any] = (),
) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    """``{key: compared values}`` of rows *not* written by a legacy mapping run."""
    schema, name = table.split(".")
    query = sql.SQL(
        "SELECT {keys}, {cmp} FROM {tbl} WHERE ({where}) "
        "AND (run_id IS NULL OR NOT (run_id = ANY(%s)))"
    ).format(
        keys=sql.SQL(", ").join(sql.Identifier(c) for c in key_cols),
        cmp=sql.SQL(", ").join(sql.Identifier(c) for c in cmp_cols),
        tbl=sql.Identifier(schema, name),
        where=sql.SQL(where),  # constant fragments only, values via params
    )
    n = len(key_cols)
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(query, [*params, list(legacy_runs)]).fetchall()
    return {tuple(r[:n]): tuple(r[n:]) for r in rows}


def _same(a: Any, b: Any) -> bool:
    if isinstance(b, Jsonb):
        b = b.obj
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b
    return bool(a == b)


def _split_live(
    rows: Iterable[dict[str, Any]],
    key_cols: Sequence[str],
    cmp_cols: Sequence[str],
    live: Mapping[tuple[Any, ...], tuple[Any, ...]],
    rep: TargetReport,
    *,
    group: Callable[[dict[str, Any]], str] | None = None,
) -> list[dict[str, Any]]:
    """Drop rows whose key is owned by a live loader; compare and count them."""
    out: list[dict[str, Any]] = []
    by_group: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        key = tuple(r[c] for c in key_cols)
        cur = live.get(key)
        if cur is None:
            out.append(r)
            continue
        rep.live_overlap += 1
        diff = [c for a, c in zip(cur, cmp_cols, strict=True) if not _same(a, r[c])]
        ok = not diff
        by_col = rep.notes.setdefault("live_mismatch_by_column", {})
        for c in diff:
            by_col[c] = by_col.get(c, 0) + 1
        g = by_group[group(r)] if group else None
        if ok:
            rep.live_match += 1
            if g is not None:
                g[0] += 1
        else:
            rep.live_mismatch += 1
            if g is not None:
                g[1] += 1
            if len(rep.mismatch_examples) < MAX_EXAMPLES:
                legacy = tuple(r[c].obj if isinstance(r[c], Jsonb) else r[c] for c in cmp_cols)
                rep.mismatch_examples.append(
                    _short(f"{_fmt(key)}: live {_fmt(cur)} vs legacy {_fmt(legacy)}")
                )
    if group:
        rep.notes.setdefault("live_overlap_by_series", {}).update(
            {k: {"match": v[0], "mismatch": v[1]} for k, v in sorted(by_group.items())}
        )
    return out


def _fmt(values: Iterable[Any]) -> str:
    out = []
    for v in values:
        if isinstance(v, datetime | date | time):
            out.append(v.isoformat())
        else:
            out.append(str(v))
    return "(" + ", ".join(out) + ")"


def _short(text: str, n: int = 300) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def _upsert(
    conn: psycopg.Connection[Any],
    table: str,
    rows: Sequence[dict[str, Any]],
    key: Sequence[str],
    run_id: int,
) -> UpsertResult:
    total = UpsertResult()
    for i in range(0, len(rows), CHUNK):
        total += upsert_rows(conn, table, rows[i : i + CHUNK], key, run_id=run_id)
    return total


# ----------------------------------------------------------------------------- fixings


def _fixing_row(
    code: str,
    d: date,
    value: Decimal,
    unit: str,
    fetched: str,
    legacy_table: str,
    legacy_key: str,
    run_id: int,
) -> dict[str, Any]:
    return {
        "index_code": code,
        "fixing_date": d,
        "source": "cbr",
        "value": value,
        "unit": unit,
        "effective_date": d,
        "published_on": None,
        "published_at": None,
        "details": {
            "legacy_table": f"{SCHEMA}.{legacy_table}",
            "legacy_key": legacy_key,
            "legacy_fetched_at": fetched,
        },
        "capture_id": None,
        "run_id": run_id,
        "ingested_at": parse_ts(fetched),
    }


def _map_fixings(
    conn: psycopg.Connection[Any], run_id: int, legacy_runs: Sequence[int], rep: TargetReport
) -> None:
    rows: list[dict[str, Any]] = []
    with conn.cursor(row_factory=tuple_row) as cur:
        for code, d, v, source, fetched in cur.execute(
            "SELECT index_code, fixing_date, rate_pct, source_code, fetched_at_utc "
            "FROM legacy_md.index_fixings ORDER BY index_code, fixing_date"
        ):
            rep.source_rows += 1
            hub = FIXING_CODES.get(code)
            if hub is None or source != "cbr":
                rep.note(f"skipped_unknown_series:{code}/{source}")
                continue
            value = dec(v)
            assert value is not None
            rows.append(
                _fixing_row(
                    hub,
                    date.fromisoformat(d),
                    value,
                    "pct",
                    fetched,
                    "index_fixings",
                    f"{code}|{d}",
                    run_id,
                )
            )
        for rec in cur.execute(
            "SELECT fixing_date, index_value, one_month_pct, three_month_pct, six_month_pct, "
            "source_code, fetched_at_utc FROM legacy_md.term_ruonia_fixings ORDER BY fixing_date"
        ).fetchall():
            rep.source_rows += 1
            d, values, source, fetched = rec[0], rec[1:5], rec[5], rec[6]
            if source != "cbr":
                rep.note(f"skipped_unknown_series:term_ruonia/{source}")
                continue
            for (code, _col, unit), v in zip(TERM_COLUMNS, values, strict=True):
                value = dec(v)
                assert value is not None
                rows.append(
                    _fixing_row(
                        code,
                        date.fromisoformat(d),
                        value,
                        unit,
                        fetched,
                        "term_ruonia_fixings",
                        d,
                        run_id,
                    )
                )
    codes = sorted({r["index_code"] for r in rows})
    key = ["index_code", "fixing_date", "source"]
    live = _existing(
        conn,
        "md.fixing",
        key,
        ["value", "unit"],
        legacy_runs,
        "index_code = ANY(%s) AND source = 'cbr'",
        [codes],
    )
    _ruonia_date_check(rows, live, rep)
    write = _split_live(rows, key, ["value", "unit"], live, rep, group=lambda r: r["index_code"])
    rep.candidates = len(rows)
    rep.counts = _upsert(conn, "md.fixing", write, key, run_id)


def _ruonia_date_check(
    rows: Sequence[dict[str, Any]],
    live: Mapping[tuple[Any, ...], tuple[Any, ...]],
    rep: TargetReport,
) -> None:
    """Evidence for the RUONIA date semantics: does a legacy value equal the live value of
    the same date (observation date) or of the next live date (publication date)?"""
    live_ruonia = sorted((k[1], v[0]) for k, v in live.items() if k[0] == "RUONIA")
    if not live_ruonia:
        return
    by_date = dict(live_ruonia)
    dates = [d for d, _ in live_ruonia]
    same = shifted = n = 0
    for r in rows:
        if r["index_code"] != "RUONIA" or not dates[0] <= r["fixing_date"] < dates[-1]:
            continue
        nxt = next((d for d in dates if d > r["fixing_date"]), None)
        if r["fixing_date"] not in by_date or nxt is None:
            continue
        n += 1
        same += by_date[r["fixing_date"]] == r["value"]
        shifted += by_date[nxt] == r["value"]
    rep.notes["ruonia_date_semantics"] = {
        "compared": n,
        "equal_on_same_date": same,
        "equal_on_next_live_date": shifted,
    }


# ----------------------------------------------------------------------------- meetings


def _map_meetings(
    conn: psycopg.Connection[Any], run_id: int, legacy_runs: Sequence[int], rep: TargetReport
) -> None:
    rows: list[dict[str, Any]] = []
    with conn.cursor(row_factory=tuple_row) as cur:
        for meeting, effective, estimated, source, fetched in cur.execute(
            "SELECT meeting_date, effective_date, is_estimated, source_code, fetched_at_utc "
            "FROM legacy_md.policy_meetings ORDER BY meeting_date"
        ):
            rep.source_rows += 1
            if source != "cbr":
                rep.note(f"skipped_source:{source}")
                continue
            rows.append(
                {
                    "meeting_date": date.fromisoformat(meeting),
                    "effective_date": date.fromisoformat(effective),
                    "is_estimated": bool(estimated),
                    "is_extraordinary": False,
                    "decision": None,
                    "decision_rate_pct": None,
                    "prev_rate_pct": None,
                    "title": None,
                    "source": "cbr",
                    "capture_id": None,
                    "run_id": run_id,
                    "ingested_at": parse_ts(fetched),
                }
            )
    key = ["meeting_date"]
    cmp = ["effective_date", "is_estimated"]
    live = _existing(conn, "ref.cbr_meeting", key, cmp, legacy_runs)
    write = _split_live(rows, key, cmp, live, rep)
    rep.candidates = len(rows)
    rep.counts = _upsert(conn, "ref.cbr_meeting", write, key, run_id)


# ---------------------------------------------------------------------------- liquidity


def _liq_values(rec: Mapping[str, Any]) -> dict[str, Decimal | None]:
    return {hub: dec(rec[old]) for old, hub in LIQUIDITY_COLUMNS}


def _map_liquidity(
    conn: psycopg.Connection[Any], run_id: int, legacy_runs: Sequence[int], rep: TargetReport
) -> None:
    cols = ", ".join(f"r.{old}" for old, _ in LIQUIDITY_COLUMNS)
    names = ["id", "observed_on", "revision_number", "methodology_version", "fetched_at"]
    names += [old for old, _ in LIQUIDITY_COLUMNS]
    with conn.cursor(row_factory=tuple_row) as cur:
        recs = [
            dict(zip(names, r, strict=True))
            for r in cur.execute(
                f"""
                SELECT r.id, r.observed_on, r.revision_number, r.methodology_version,
                       c.fetched_at_utc, {cols}
                FROM legacy_md.cbr_liquidity_balance_revisions r
                JOIN legacy_md.cbr_liquidity_captures c ON c.id = r.capture_id
                ORDER BY r.observed_on, r.revision_number
                """
            )
        ]
        balances = {
            r[0]: (r[1], r[2:])
            for r in cur.execute(
                f"SELECT observed_on, revision_id, {', '.join(o for o, _ in LIQUIDITY_COLUMNS)} "
                "FROM legacy_md.cbr_liquidity_balances"
            )
        }
    rep.source_rows = len(recs)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        by_day[r["observed_on"]].append(r)

    def hub_row(r: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "observed_on": date.fromisoformat(r["observed_on"]),
            **_liq_values(r),
            "methodology_version": r["methodology_version"],
            "revision_no": int(r["revision_number"]),
            "capture_id": None,
            "run_id": run_id,
            "ingested_at": parse_ts(r["fetched_at"]),
        }

    rows: list[dict[str, Any]] = []
    history: dict[date, list[tuple[dict[str, Any], datetime]]] = {}
    for day, revs in by_day.items():
        latest = revs[-1]
        rows.append(hub_row(latest))
        bal = balances.get(day)
        if (
            bal is None
            or bal[0] != latest["id"]
            or any(
                dec(v) != dec(latest[old])
                for v, (old, _) in zip(bal[1], LIQUIDITY_COLUMNS, strict=True)
            )
        ):
            rep.note("balances_differ_from_latest_revision")
        if len(revs) > 1:
            history[date.fromisoformat(day)] = [
                (hub_row(old), parse_ts(new["fetched_at"])) for old, new in itertools.pairwise(revs)
            ]
    rep.note("superseded_revisions", sum(len(v) for v in history.values()))
    key = ["observed_on"]
    cmp = [hub for _, hub in LIQUIDITY_COLUMNS]
    live = _existing(conn, "md.cbr_liquidity", key, cmp, legacy_runs)
    write = _split_live(rows, key, cmp, live, rep)
    rep.candidates = len(rows)
    rep.counts = _upsert(conn, "md.cbr_liquidity", write, key, run_id)
    written = {r["observed_on"] for r in write}
    added = 0
    for obs_day, images in history.items():
        if obs_day not in written:
            continue
        for old, replaced_at in images:
            image = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in old.items()}
            image["observed_on"] = obs_day.isoformat()
            image["ingested_at"] = old["ingested_at"].isoformat()
            cur = conn.execute(
                """
                INSERT INTO ops.row_history (table_name, row_key, old_row, replaced_at,
                    replaced_by_run)
                SELECT 'md.cbr_liquidity', jsonb_build_object('observed_on', %(d)s::date),
                       to_jsonb(jsonb_populate_record(NULL::md.cbr_liquidity, %(img)s::jsonb)),
                       %(at)s, %(run)s
                WHERE NOT EXISTS (
                    SELECT 1 FROM ops.row_history
                    WHERE table_name = 'md.cbr_liquidity'
                      AND row_key = jsonb_build_object('observed_on', %(d)s::date)
                      AND replaced_at = %(at)s)
                """,
                {"d": obs_day, "img": Jsonb(image), "at": replaced_at, "run": run_id},
            )
            added += cur.rowcount
    rep.notes["row_history_added"] = added


# --------------------------------------------------------------------------------- zcyc


def _map_zcyc(
    conn: psycopg.Connection[Any], run_id: int, legacy_runs: Sequence[int], rep: TargetReport
) -> None:
    with conn.cursor(row_factory=tuple_row) as cur:
        recs = cur.execute(
            "SELECT id, snapshot_uid, trade_date, source_observed_at_utc, fetched_at_utc, "
            "parameters_json, standard_yields_json, max_reconciliation_error_bp "
            "FROM legacy_md.gcurve_snapshots ORDER BY fetched_at_utc, id"
        ).fetchall()
    rep.source_rows = len(recs)
    rows: dict[tuple[date, time], dict[str, Any]] = {}
    rejects = 0
    for lid, uid, td, observed, fetched, params_text, yields_text, legacy_err in recs:
        trade_date = date.fromisoformat(td)
        src_ts = parse_ts(observed)
        local = src_ts.astimezone(MSK)
        reason: str | None = None
        snap: ZcycSnapshot | None = None
        try:
            params = json.loads(params_text)
            yields = json.loads(yields_text)
            if not isinstance(params, list) or len(params) != len(ZCYC_PARAM_NAMES):
                raise ValueError(f"expected {len(ZCYC_PARAM_NAMES)} parameters")
            snap = ZcycSnapshot(
                trade_date=trade_date,
                trade_time=local.time(),
                params={n: float(v) for n, v in zip(ZCYC_PARAM_NAMES, params, strict=True)},
                standard_yields=[(float(t), float(y)) for t, y in yields],
            )
        except (ValueError, TypeError) as exc:
            reason = f"legacy gcurve snapshot not parseable: {exc}"
        if snap is not None and local.date() != trade_date:
            reason = f"trade_date {trade_date} != Moscow date of source time {local.isoformat()}"
        check = check_zcyc(snap) if snap is not None and reason is None else None
        if check is not None and not check.passed:
            reason = (
                f"standard yields not reproduced from params: max error {check.max_error_bp} bp "
                f">= {ZCYC_TOLERANCE_BP} bp (legacy check {legacy_err} bp)"
            )
        if reason is not None or snap is None or check is None:
            rejects += 1
            _zcyc_reject(
                conn,
                run_id,
                recs_row=(lid, uid, td, observed, fetched, params_text, yields_text, legacy_err),
                snap=snap,
                src_ts=src_ts,
                err=check.max_error_bp if check else None,
                reason=reason or "?",
            )
            continue
        row = {
            "trade_date": snap.trade_date,
            "trade_time": snap.trade_time,
            "source_ts": snap.source_ts,
            "params": snap.params_json(),
            "standard_yields": snap.yields_json(),
            "check_max_error_bp": check.max_error_bp,
            "check_tolerance_bp": ZCYC_TOLERANCE_BP,
            "is_eod": False,
            "capture_id": None,
            "run_id": run_id,
            "ingested_at": parse_ts(fetched),
        }
        key = (snap.trade_date, snap.trade_time)
        prev = rows.get(key)
        if prev is None:
            rows[key] = row
        elif prev["params"] == row["params"] and prev["standard_yields"] == row["standard_yields"]:
            rep.note("duplicate_publication_identical")
        else:
            rep.note("duplicate_publication_conflicting_kept_first")
    rep.notes["rejected"] = rejects
    key_cols = ["trade_date", "trade_time"]
    cmp = ["params", "standard_yields"]
    live = _existing(conn, "md.zcyc_snapshot", key_cols, cmp, legacy_runs)
    write = _split_live(list(rows.values()), key_cols, cmp, live, rep)
    for r in write:
        r["standard_yields"] = Jsonb(r["standard_yields"])
    rep.candidates = len(rows)
    rep.counts = _upsert(conn, "md.zcyc_snapshot", write, key_cols, run_id)


def _zcyc_reject(
    conn: psycopg.Connection[Any],
    run_id: int,
    *,
    recs_row: tuple[Any, ...],
    snap: ZcycSnapshot | None,
    src_ts: datetime,
    err: Decimal | None,
    reason: str,
) -> None:
    """``md.zcyc_reject`` needs a capture: store the legacy record itself (deterministic JSON,
    legacy fetch time) as a synthetic capture - re-runs hit the same capture."""
    lid, uid, td, observed, fetched, params_text, yields_text, legacy_err = recs_row
    payload = json.dumps(
        {
            "legacy_table": f"{SCHEMA}.gcurve_snapshots",
            "id": lid,
            "snapshot_uid": uid,
            "trade_date": td,
            "source_observed_at_utc": observed,
            "fetched_at_utc": fetched,
            "parameters_json": params_text,
            "standard_yields_json": yields_text,
            "max_reconciliation_error_bp": legacy_err,
        },
        sort_keys=True,
    ).encode()
    cap = record_capture(
        conn,
        run_id,
        "moex_iss",
        "legacy_gcurve_snapshot",
        f"legacy://{SCHEMA}/gcurve_snapshots/{lid}",
        payload,
        "legacy",
        source_ts=src_ts,
        content_type="application/json",
        fetched_at=parse_ts(fetched),
    )
    conn.execute(
        """
        INSERT INTO md.zcyc_reject (capture_id, trade_date, trade_time, max_error_bp, reason,
            run_id)
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (capture_id) DO NOTHING
        """,
        (
            cap,
            snap.trade_date if snap else date.fromisoformat(td),
            snap.trade_time if snap else None,
            err,
            "legacy: " + reason,
            run_id,
        ),
    )


# ------------------------------------------------------------------------------- curves


def curve_content_hash(basis: str, quotes: Sequence[Mapping[str, Any]]) -> str:
    """Content hash as the live Ratescalcs loader computes it
    (:func:`mdhub.sources.ratescalcs.content_hash`) when every quote has bid/ask; quotes
    without bid/ask (mid-only) add their mid, so mid-only curves do not all hash alike."""
    if all(q["bid_pct"] is not None and q["ask_pct"] is not None for q in quotes):
        curve = rc.ParsedCurve(
            dataset="",
            label="",
            quote_basis=basis,
            quotes=tuple(
                rc.CurveQuote(q["tenor"], q["position"], q["bid_pct"], q["ask_pct"]) for q in quotes
            ),
        )
        return rc.content_hash(curve)
    payload = [
        basis,
        [
            [
                q["tenor"],
                None if q["bid_pct"] is None else str(q["bid_pct"].normalize()),
                None if q["ask_pct"] is None else str(q["ask_pct"].normalize()),
                None,
                None if q["mid_pct"] is None else str(q["mid_pct"].normalize()),
            ]
            for q in quotes
        ],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _curve_quotes(
    qrows: Sequence[tuple[Any, ...]], snapshot_basis: str, rep: TargetReport
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos, (_qid, _position, tenor, months, bid, ask, mid, basis) in enumerate(qrows):
        t = str(tenor).upper()
        try:
            hub_months, hub_days = rc.tenor_parts(t)
        except ValueError:
            hub_months, hub_days = months, None
            rep.note("tenor_unparsed")
        if months is not None and months != hub_months:
            rep.note("tenor_months_differs")
        extra: dict[str, str] = {}
        if basis == "mid_only":
            b = a = None
            m = dec(mid)
        else:
            b, a = dec(bid), dec(ask)
            assert b is not None
            assert a is not None
            m = (b + a) / 2
            legacy_mid = dec(mid)
            if legacy_mid is not None and abs(legacy_mid - m) > Decimal("1e-9"):
                extra["legacy_mid_pct"] = str(legacy_mid)
                rep.note("quote_mid_differs_from_bid_ask_average")
        if basis != snapshot_basis:
            extra["quote_basis"] = str(basis)
        out.append(
            {
                "tenor": t,
                "position": pos,
                "tenor_months": hub_months,
                "tenor_days": hub_days,
                "pillar_date": None,
                "bid_pct": b,
                "ask_pct": a,
                "mid_pct": m,
                "extra": extra or None,
            }
        )
    return out


def _map_curves(
    conn: psycopg.Connection[Any], run_id: int, legacy_runs: Sequence[int], rep: TargetReport
) -> None:
    with conn.cursor(row_factory=tuple_row) as cur:
        snaps = cur.execute(
            """
            SELECT s.id, so.code, d.code, s.fetched_at_utc, s.source_observed_at_utc,
                   s.payload_sha256, s.parser_version, s.quote_basis, s.snapshot_uid,
                   s.correction_state
            FROM legacy_md.curve_snapshots s
            JOIN legacy_md.datasets d ON d.id = s.dataset_id
            JOIN legacy_md.sources so ON so.id = d.source_id
            ORDER BY s.id
            """
        ).fetchall()
        quotes: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
        n_quotes = 0
        for q in cur.execute(
            "SELECT snapshot_id, id, position, tenor, tenor_months, bid_pct, ask_pct, mid_pct, "
            "quote_basis FROM legacy_md.curve_quotes ORDER BY snapshot_id, position, id"
        ):
            quotes[int(q[0])].append(tuple(q[1:]))
            n_quotes += 1
    rep.source_rows = len(snaps)
    rep.notes["source_quotes"] = n_quotes

    batch: dict[tuple[str, str, str], int] = defaultdict(int)
    for s in snaps:
        batch[(s[1], s[2], s[3])] += 1

    prepared: list[dict[str, Any]] = []
    used: set[tuple[str, str, datetime]] = set()
    for lid, lsource, ldataset, fetched, observed, psha, pver, basis, uid, corr in sorted(
        snaps, key=lambda s: (s[1], s[2], s[4] or s[3], s[3], s[0])
    ):
        source = CURVE_SOURCES.get(lsource)
        if source is None:
            rep.note(f"skipped_unknown_source:{lsource}")
            continue
        dataset = str(CURVE_DATASETS.get(ldataset, ldataset))
        src_ts = parse_ts(observed) if observed else None
        fetched_at = parse_ts(fetched)
        flags: list[str] = []
        if batch[(lsource, ldataset, fetched)] > 1:
            # Archive batch (andromeda): one fetch time for hundreds of historical curves.
            # Use the end of the curve's Moscow date so as-of reads see it after the close.
            if src_ts is not None:
                fetched_at = datetime.combine(src_ts.astimezone(MSK).date(), time(23, 59, 59), MSK)
            flags.append("fetched_at_synthetic")
        while (source, dataset, fetched_at) in used:
            fetched_at += timedelta(microseconds=1)
        used.add((source, dataset, fetched_at))
        qlist = _curve_quotes(quotes.get(int(lid), []), basis, rep)
        if not qlist:
            rep.note("skipped_without_quotes")
            continue
        if source == "ratescalcs" and dataset == "key_rate_swap" and len(qlist) == 2:
            flags.append("ratescalcs_krs_partial_2_tenors")
        full = FULL_TENOR_SETS.get(dataset)
        if source == "andromeda" and full is not None and len(qlist) < full:
            flags.append("andromeda_partial_curve")
            if src_ts is None or src_ts.date() >= ANDROMEDA_PARTIAL_BEFORE:
                rep.note("andromeda_partial_after_2023_07")
        meta: dict[str, Any] = {
            "legacy": {
                "table": f"{SCHEMA}.curve_snapshots",
                "id": lid,
                "source": lsource,
                "dataset": ldataset,
                "fetched_at_utc": fetched,
                "source_observed_at_utc": observed,
                "payload_sha256": psha,
                "snapshot_uid": uid,
                "correction_state": corr,
            }
        }
        if flags:
            meta["quality_flags"] = flags
        prepared.append(
            {
                "snapshot": {
                    "source": source,
                    "dataset": dataset,
                    "quote_basis": basis,
                    "fetched_at": fetched_at,
                    "snapshot_date": fetched_at.astimezone(MSK).date(),
                    "source_ts": src_ts,
                    "market_closed": None,
                    "content_sha256": curve_content_hash(basis, qlist),
                    "parser_version": f"legacy_v{pver}",
                    "label": None,
                    "meta": meta,
                    "capture_id": None,
                    "run_id": run_id,
                    "legacy_id": str(lid),
                },
                "quotes": qlist,
                "flags": flags,
            }
        )

    # Content dedup per (source, dataset) in fetched_at order, as the live loader does.
    prepared.sort(
        key=lambda p: (
            p["snapshot"]["source"],
            p["snapshot"]["dataset"],
            p["snapshot"]["fetched_at"],
            int(p["snapshot"]["legacy_id"]),
        )
    )
    kept: list[dict[str, Any]] = []
    prev: dict[tuple[str, str], str | None] = {}
    for p in prepared:
        s = p["snapshot"]
        series = (s["source"], s["dataset"])
        if series not in prev:
            prev[series] = _previous_hash(conn, s["source"], s["dataset"], s["fetched_at"])
        if s["content_sha256"] == prev[series]:
            rep.note("deduplicated_unchanged_content")
            continue
        prev[series] = s["content_sha256"]
        kept.append(p)
        for f in p["flags"]:
            rep.note(f"flagged:{f}")

    key = ["source", "dataset", "fetched_at"]
    live = _existing(
        conn, "md.curve_snapshot", key, ["content_sha256"], legacy_runs, "legacy_id IS NULL"
    )
    snap_rows = _split_live(
        [p["snapshot"] for p in kept],
        key,
        ["content_sha256"],
        live,
        rep,
        group=lambda r: f"{r['source']}/{r['dataset']}",
    )
    rep.candidates = len(kept)
    keep_ids = {(r["source"], r["legacy_id"]): r["fetched_at"] for r in snap_rows}
    # Prune legacy-owned snapshots this mapping no longer produces (quotes cascade).
    pruned = 0
    with conn.cursor(row_factory=tuple_row) as cur:
        for sid, source, legacy_id, fetched_at in cur.execute(
            "SELECT id, source, legacy_id, fetched_at FROM md.curve_snapshot "
            "WHERE legacy_id IS NOT NULL AND run_id = ANY(%s)",
            (list(legacy_runs),),
        ).fetchall():
            if keep_ids.get((source, legacy_id)) != fetched_at:
                conn.execute("DELETE FROM md.curve_snapshot WHERE id = %s", (sid,))
                pruned += 1
    rep.notes["pruned_stale_legacy_snapshots"] = pruned
    for r in snap_rows:
        r["meta"] = Jsonb(r["meta"])
    rep.counts = _upsert(conn, "md.curve_snapshot", snap_rows, key, run_id)
    with conn.cursor(row_factory=tuple_row) as cur:
        ids = {
            (str(s), str(lid)): int(i)
            for i, s, lid in cur.execute(
                "SELECT id, source, legacy_id FROM md.curve_snapshot WHERE legacy_id IS NOT NULL"
            )
        }
    written = {(r["source"], r["legacy_id"]) for r in snap_rows}
    quote_rows: list[dict[str, Any]] = []
    for p in kept:
        s = p["snapshot"]
        k = (s["source"], s["legacy_id"])
        if k not in written:
            continue
        for q in p["quotes"]:
            quote_rows.append({"snapshot_id": ids[k], **q, "extra": q["extra"]})
    qres = _upsert(conn, "md.curve_quote", quote_rows, ["snapshot_id", "tenor"], run_id)
    rep.notes["quotes"] = {
        "inserted": qres.inserted,
        "updated": qres.updated,
        "unchanged": qres.unchanged,
    }


def _previous_hash(
    conn: psycopg.Connection[Any], source: str, dataset: str, before: datetime
) -> str | None:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """SELECT content_sha256 FROM md.curve_snapshot
               WHERE source = %s AND dataset = %s AND fetched_at < %s AND legacy_id IS NULL
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (source, dataset, before),
        ).fetchone()
    return str(row[0]) if row else None


# ----------------------------------------------------------------------------- intraday


def _map_intraday(
    conn: psycopg.Connection[Any], run_id: int, legacy_runs: Sequence[int], rep: TargetReport
) -> None:
    fx: dict[tuple[Any, ...], tuple[tuple[Any, ...], dict[str, Any]]] = {}
    fut: dict[tuple[Any, ...], tuple[tuple[Any, ...], dict[str, Any]]] = {}
    collapsed = conflicting = 0
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            """
            SELECT o.id, o.capture_id, o.instrument_code, o.instrument_type, o.source_variant,
                   o.board, o.session_date, o.source_observed_at_utc, o.bid_text, o.offer_text,
                   o.last_text, o.quote_unit, o.latency, c.source_code, c.fetched_at_utc
            FROM legacy_md.intraday_quote_observations o
            JOIN legacy_md.intraday_quote_captures c ON c.id = o.capture_id
            """
        )
        for rec in cur:
            rep.source_rows += 1
            (
                oid,
                cid,
                instr,
                itype,
                lvariant,
                board,
                session,
                observed,
                bid,
                offer,
                last,
                unit,
                latency,
                lsource,
                fetched,
            ) = rec
            source = INTRADAY_SOURCES.get(lsource)
            if source is None:
                rep.note(f"skipped_unknown_source:{lsource}")
                continue
            if unit != "rub_per_base_unit":
                rep.note(f"skipped_quote_unit:{unit}")
                continue
            variant = INTRADAY_VARIANTS.get(lvariant, lvariant)
            ts = parse_ts(observed)
            fetched_at = parse_ts(fetched)
            order = (fetched_at, cid, oid)
            values = (dec_text(bid), dec_text(offer), dec_text(last))
            if lvariant == USDRUBF_FALLBACK:
                # Mislabelled in legacy as USD/RUB_TOM spot: it is the USDRUBF futures price
                # Ratescalcs used as a fallback. Kept as a futures row of its own variant.
                rep.note("flagged:usdrubf_fallback_as_futures")
                itype, instr = "perpetual_future", "USDRUBF"
            if itype == "spot":
                row = {
                    "ts": ts,
                    "secid": instr,
                    "board": board or (NTPRO_BOARD if source == "ratescalcs" else None),
                    "source": source,
                    "source_variant": variant,
                    "bid": values[0],
                    "offer": values[1],
                    "last": values[2],
                    "latency": latency,
                    "capture_id": None,
                    "run_id": run_id,
                    "ingested_at": fetched_at,
                }
                if row["board"] is None:
                    rep.note("skipped_without_board")
                    continue
                key: tuple[Any, ...] = (row["secid"], row["board"], source, variant, ts)
                target = fx
            elif itype in ("dated_future", "perpetual_future"):
                row = {
                    "ts": ts,
                    "secid": instr,
                    "source": source,
                    "source_variant": variant,
                    "trade_date": date.fromisoformat(session) if session else None,
                    "bid_rub_per_unit": values[0],
                    "offer_rub_per_unit": values[1],
                    "last_rub_per_unit": values[2],
                    "latency": latency,
                    "capture_id": None,
                    "run_id": run_id,
                    "ingested_at": fetched_at,
                }
                key = (instr, source, variant, ts)
                target = fut
            else:
                rep.note(f"skipped_instrument_type:{itype}")
                continue
            prev = target.get(key)
            if prev is not None:
                collapsed += 1
                if _vals(prev[1]) != values:
                    conflicting += 1
                if prev[0] >= order:
                    continue
            target[key] = (order, row)
    rep.notes["collapsed_duplicate_keys"] = collapsed
    rep.notes["collapsed_with_different_values"] = conflicting

    fx_rows = _change_only(
        [r for _, r in fx.values()],
        ["secid", "board", "source", "source_variant"],
        ["bid", "offer", "last"],
        rep,
    )
    fut_rows = _change_only(
        [r for _, r in fut.values()],
        ["secid", "source", "source_variant"],
        ["bid_rub_per_unit", "offer_rub_per_unit", "last_rub_per_unit"],
        rep,
    )
    rep.candidates = len(fx_rows) + len(fut_rows)
    total = UpsertResult()
    by_table: dict[str, Any] = {}
    for table, rows, key_cols, cmp in (
        (
            "md.fx_intraday",
            fx_rows,
            ["secid", "board", "source", "source_variant", "ts"],
            ["bid", "offer", "last"],
        ),
        (
            "md.futures_intraday",
            fut_rows,
            ["secid", "source", "source_variant", "ts"],
            ["bid_rub_per_unit", "offer_rub_per_unit", "last_rub_per_unit"],
        ),
    ):
        if not rows:
            continue
        lo = min(r["ts"] for r in rows)
        hi = max(r["ts"] for r in rows)
        sources = sorted({r["source"] for r in rows})
        live = _existing(
            conn,
            table,
            key_cols,
            cmp,
            legacy_runs,
            "ts BETWEEN %s AND %s AND source = ANY(%s)",
            [lo, hi, sources],
        )
        write = _split_live(
            rows,
            key_cols,
            cmp,
            live,
            rep,
            group=functools.partial(_series_label, table),
        )
        res = _upsert(conn, table, write, key_cols, run_id)
        total += res
        series: dict[str, int] = defaultdict(int)
        for r in rows:
            series[f"{r['source']}/{r['source_variant']}/{r['secid']}"] += 1
        by_table[table] = {
            "rows": len(rows),
            "inserted": res.inserted,
            "updated": res.updated,
            "unchanged": res.unchanged,
            "series": dict(sorted(series.items())),
        }
    rep.counts = total
    rep.notes["tables"] = by_table


def _series_label(table: str, row: Mapping[str, Any]) -> str:
    return f"{table}:{row['source']}/{row['source_variant']}"


def _vals(row: Mapping[str, Any]) -> tuple[Any, ...]:
    if "bid" in row:
        return (row["bid"], row["offer"], row["last"])
    return (row["bid_rub_per_unit"], row["offer_rub_per_unit"], row["last_rub_per_unit"])


def _change_only(
    rows: list[dict[str, Any]],
    series_cols: Sequence[str],
    value_cols: Sequence[str],
    rep: TargetReport,
) -> list[dict[str, Any]]:
    """Store-on-change like the live intraday loaders: drop a row whose values equal the
    previous row of its series. AlgoPack obstats bars are kept one per interval."""
    rows.sort(key=lambda r: (*(str(r[c]) for c in series_cols), r["ts"]))
    out: list[dict[str, Any]] = []
    last: dict[tuple[str, ...], tuple[Any, ...]] = {}
    dropped = 0
    for r in rows:
        s = tuple(str(r[c]) for c in series_cols)
        v = tuple(r[c] for c in value_cols)
        if r["source_variant"] != OBSTATS_VARIANT and last.get(s) == v:
            dropped += 1
            continue
        last[s] = v
        out.append(r)
    rep.note("dropped_unchanged_consecutive", dropped)
    return out


# ------------------------------------------------------------------------------- runner

_MAPPERS: Final[
    dict[
        str,
        Callable[[psycopg.Connection[Any], int, Sequence[int], TargetReport], None],
    ]
] = {
    "fixings": _map_fixings,
    "meetings": _map_meetings,
    "liquidity": _map_liquidity,
    "zcyc": _map_zcyc,
    "curves": _map_curves,
    "intraday": _map_intraday,
}


def _check_import(db: Database, allow_unverified: bool) -> int:
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT id, verified FROM ops.legacy_import WHERE schema_name = %s "
            "ORDER BY id DESC LIMIT 1",
            (SCHEMA,),
        ).fetchone()
    if row is None:
        raise LegacyImportError(f"no import recorded for {SCHEMA}; run `mdhub legacy import`")
    if not row[1] and not allow_unverified:
        raise LegacyImportError(
            f"{SCHEMA} import #{row[0]} is not verified; run `mdhub legacy verify` first"
        )
    return int(row[0])


def map_md(
    db: Database,
    *,
    targets: Sequence[str] | None = None,
    allow_unverified: bool = False,
    progress: Callable[[str], None] | None = None,
) -> MapResult:
    """Map ``legacy_md`` into ``md`` / ``ref`` (see module docstring).

    :raises LegacyImportError: no (verified) import of ``legacy_md``; unknown target.
    """
    say = progress or (lambda msg: log.info("%s", msg))
    chosen = list(targets or TARGETS)
    unknown = [t for t in chosen if t not in _MAPPERS]
    if unknown:
        raise LegacyImportError(f"unknown target(s) {unknown}; choose from {list(TARGETS)}")
    import_id = _check_import(db, allow_unverified)
    reports: list[TargetReport] = []
    with job_run(db, JOB, {"targets": chosen, "legacy_import_id": import_id}) as run:
        for name in chosen:
            rep = TargetReport(target=name)
            t0 = _time.monotonic()
            try:
                with db.connection() as conn:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (JOB,))
                    legacy_runs = sorted({*_legacy_runs(conn), run.id})
                    _MAPPERS[name](conn, run.id, legacy_runs, rep)
            except (psycopg.Error, ValueError, KeyError, TypeError, AssertionError) as exc:
                rep.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                run.mark_partial(f"{name}: {rep.error}")
                log.exception("legacy map %s failed", name)
            rep.duration_s = _time.monotonic() - t0
            run.add_counts(rep.counts)
            reports.append(rep)
            say(
                f"{name}: {rep.source_rows} legacy rows -> {rep.candidates} candidates, "
                f"inserted {rep.counts.inserted}, updated {rep.counts.updated}, "
                f"unchanged {rep.counts.unchanged}; live overlap {rep.live_overlap} "
                f"(match {rep.live_match}, mismatch {rep.live_mismatch}) "
                f"in {rep.duration_s:.1f}s" + (f" ERROR {rep.error}" if rep.error else "")
            )
    return MapResult(run_id=run.id, targets=reports)
