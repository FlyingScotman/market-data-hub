"""Russian SPB bonds: one discovery path, report unit reference, chart-only daily prices.

The loader requests completed dates only (the current day is never asked for; a chart saved
during the trading day had no bar for that day), so one run a day after the previous day closed
is all there is to read; see ``docs/SPB_BONDS.md``. A bond that fails validation is skipped with
a diagnostic (the run ends ``partial``); more than :func:`~mdhub.sources.spb.skip_limit` of
them is format drift and fails the run.

A price is never written with a unit that is not proven: when a bond's unit has changed (stored
bars carry a unit other than the one of the newest report) nothing is written for the dates up
to the change, whatever the chart returns now (the chart may or may not have restated its
history in the new unit), nor for the dates between the last bar of the old unit and the first
report of the new one. Stored bars of the old unit are not touched at all. See
:func:`_unit_freeze`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError, SourceNotConfigured
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.sources import spb

DATASET = register_dataset(
    DatasetSpec(
        name="spb_bonds_daily",
        table="md.spb_bond_daily",
        date_column="trade_date",
        calendar=None,
        frequency="event",
        series_column="isin",
        max_lag_days=7,
        backfill_job="spb_bonds_daily",
        max_task_days=spb.MAX_LOAD_DAYS,
        description="SPB Russian bond chart daily OHLC; observed bars, no assumed session calendar",
    )
)


DEFAULT_DAYS = 3
"""Completed calendar dates a run without a range re-reads (late corrections of a bar)."""


def load_range(
    *,
    today: date,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    last_stored: date | None = None,
) -> tuple[date, date]:
    """Completed Moscow calendar dates, inclusive, at most 31 days. No holiday filter.

    Without ``day`` / a range the run re-reads the last three completed dates, and starts earlier
    when the newest stored bar is older than that (``last_stored``: the worker was down, so the
    dates in between are read too), but never more than 31 dates back."""
    if day is not None and (date_from is not None or date_to is not None):
        raise JobParamError("SPB: day and date range are mutually exclusive")
    if (date_from is None) != (date_to is None):
        raise JobParamError("SPB: both date_from and date_to are required")
    end = day or date_to or today - timedelta(days=1)
    start = day or date_from
    if start is None:
        start = end - timedelta(days=DEFAULT_DAYS - 1)
        if last_stored is not None:
            start = max(min(start, last_stored + timedelta(days=1)), end - timedelta(days=30))
    if start > end or end >= today or (end - start).days >= spb.MAX_LOAD_DAYS:
        raise JobParamError("SPB: require 1..31 completed calendar dates before today (MSK)")
    return start, end


def _stored_units(
    conn: psycopg.Connection[Any], instruments: list[dict[str, Any]], report_date: date
) -> dict[str, tuple[str, int]] | None:
    """``isin -> (price unit, unit report capture)`` read from ``ref.spb_bond`` when every
    instrument was already validated against the report of ``report_date`` (same ticker, security
    id and settlement currency), else ``None``: the PDF is fetched. A report is dated and does not
    change afterwards, so a rerun on the same day (a retry, a backfill task, "Run now") need not
    download it again."""
    rows = {
        r["isin"]: r
        for r in conn.execute(
            "SELECT isin, ticker, security_id, settlement_currency, price_unit, "
            "unit_reference_date, unit_capture_id FROM ref.spb_bond "
            "WHERE venue = 'SPB' AND isin = ANY(%s)",
            ([b["isin"] for b in instruments],),
        ).fetchall()
    }
    stored: dict[str, tuple[str, int]] = {}
    for bond in instruments:
        row = rows.get(bond["isin"])
        if (
            row is None
            or row["unit_reference_date"] != report_date
            or row["unit_capture_id"] is None
            or row["ticker"] != bond["ticker"]
            or row["security_id"] != bond["security_id"]
            or row["settlement_currency"] != bond["settlement_currency"]
        ):
            return None
        stored[bond["isin"]] = row["price_unit"], row["unit_capture_id"]
    return stored


_UNIT_FIELDS = ("price_unit", "unit_reference_date", "card_price_unit", "unit_conflict")


@dataclass(frozen=True)
class UnitFreeze:
    """Where the price unit of a bond changed, derived from its *stored bars* only.

    ``stored_unit``: the older unit of the bars that must not be touched. ``frozen_through``:
    the newest date that may still be in that unit (the latest trade date or report date of a
    stored bar of another unit); no price is written for a date up to it, and stored bars of
    those dates are left as they are. ``new_unit_from``: the first date written with the current
    unit; the dates between ``frozen_through`` and it are the window in which the change
    happened and are not written either. ``pending``: no bar of the current unit is stored yet."""

    stored_unit: str
    frozen_through: date
    new_unit_from: date
    pending: bool


def _unit_freeze(conn: psycopg.Connection[Any], bond: dict[str, Any]) -> UnitFreeze | None:
    """The unit boundary of one bond, or ``None`` while every stored bar has the current unit.

    The boundary is read from the stored bars, never from ``ref.spb_bond`` (the first run after
    a change overwrites it, and a run that fails after that must not change the answer), so the
    result does not depend on the order in which runs happened. A stored bar proves that the
    newest report that said its unit was dated ``unit_reference_date`` and that its own date was
    assumed to carry that unit: both are "possibly the old unit". The latest of all such dates
    over the bars with another unit is the boundary ``B``. The current unit is proven only from
    the first report that said it (``unit_reference_date`` of the bond) or, once bars of the
    current unit have been stored after ``B``, from the first of those bars; the dates in
    between are a window in which the change happened and its exact date is unknown, and a
    single report cannot narrow it (the earlier reports are not read: that would cost up to 31
    more PDFs per run after an outage for a window that holds at most a few bars)."""
    foreign = conn.execute(
        "SELECT price_unit, max(trade_date) AS last_date, max(unit_reference_date) AS last_report "
        "FROM md.spb_bond_daily WHERE venue = 'SPB' AND isin = %s AND price_unit <> %s "
        "GROUP BY price_unit",
        (bond["isin"], bond["price_unit"]),
    ).fetchall()
    if not foreign:
        return None
    newest = max(foreign, key=lambda r: max(r["last_date"], r["last_report"]))
    boundary = max(newest["last_date"], newest["last_report"])
    first = conn.execute(
        "SELECT min(trade_date) AS d FROM md.spb_bond_daily "
        "WHERE venue = 'SPB' AND isin = %s AND price_unit = %s AND trade_date > %s",
        (bond["isin"], bond["price_unit"], boundary),
    ).fetchone()
    stored_from = first["d"] if first else None
    new_from = stored_from or max(bond["unit_reference_date"], boundary + timedelta(days=1))
    return UnitFreeze(newest["price_unit"], boundary, new_from, stored_from is None)


def _unit_change(ticker: str, new: str, freeze: UnitFreeze | None, old: str | None = None) -> str:
    """Diagnostic for the operator: what is not written and from which date the new unit applies."""
    if freeze is None:
        return f"price unit changed {old}->{new} for {ticker}; no stored bar of the old unit"
    return (
        f"price unit changed {freeze.stored_unit}->{new} for {ticker}: no price is written "
        f"for dates up to {freeze.new_unit_from - timedelta(days=1)} "
        f"(bars up to {freeze.frozen_through} keep {freeze.stored_unit}), "
        f"{new} from {freeze.new_unit_from}"
    )


@job("spb_bonds_daily", "SPB Russian bond chart daily OHLC (not official close)")
def spb_bonds_daily(
    ctx: JobContext,
    *,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    if not ctx.settings.enable_spb_bonds:
        raise SourceNotConfigured("SPB bonds disabled (MDHUB_ENABLE_SPB_BONDS=false)")
    today = ctx.today()
    last_stored: date | None = None
    if day is None and date_from is None and date_to is None:
        with ctx.connection() as conn:
            row = conn.execute("SELECT max(trade_date) AS d FROM md.spb_bond_daily").fetchone()
            last_stored = row["d"] if row else None
    start, end = load_range(
        today=today, day=day, date_from=date_from, date_to=date_to, last_stored=last_stored
    )
    requests = 0
    skipped: dict[str, str] = {}
    unit_changes: dict[str, str] = {}
    unit_seen: dict[str, str] = {}  # ticker -> older unit, when this run saw the report change
    unit_frozen: dict[str, dict[str, Any]] = {}
    unit_alarms: list[str] = []
    bars_seen = 0
    chart_failed: set[str] = set()
    total_rows = 0
    total = 0

    def skip(ticker: str, reason: str) -> None:
        """Leave one bond out of the run; too many is format drift, not an odd bond."""
        skipped[ticker] = reason
        limit = spb.skip_limit(total)
        ctx.logger.warning("SPB: skipping %s: %s", ticker, reason)
        if len(skipped) > limit:
            detail = "; ".join(f"{t}: {r}" for t, r in skipped.items())
            raise FormatDriftError(
                f"SPB: {len(skipped)} of {total} bonds failed validation (limit {limit}), "
                f"treated as format drift: {detail}"
            )

    with ctx.http() as http:

        def get(path: str, params: dict[str, Any] | None = None) -> Any:
            nonlocal requests
            requests += 1
            return spb.fetch(http, path, params)

        # Finished discovery is committed before prices; a later chart error retains evidence.
        # A bond is admitted only after its card and its unit report row have been validated.
        with ctx.connection() as conn:
            response = get(spb.UNIVERSE_PATH)
            record_capture(
                conn, ctx.run.id, "spb", "russian_universe", None, response, spb.PARSER_VERSION
            )
            candidates = spb.parse_universe(response.content)
            if not candidates:
                raise FormatDriftError("SPB: Russian market returned no bond candidates")
            total = len(candidates)
            instruments: list[dict[str, Any]] = []
            for candidate in candidates:
                card = get(spb.CARD_PATH.format(candidate.security_id))
                cap = record_capture(
                    conn, ctx.run.id, "spb", "russian_bond_card", None, card, spb.PARSER_VERSION
                )
                try:
                    instruments.append(
                        {**spb.parse_card(card.content, candidate), "capture_id": cap}
                    )
                except FormatDriftError as exc:
                    skip(candidate.ticker, f"card: {exc}")
            if len({b["isin"] for b in instruments}) != len(instruments):
                raise FormatDriftError("SPB: duplicate ISIN across discovered bonds")
            if not instruments:
                raise FormatDriftError("SPB: no bond passed card validation")
            archive_rows = []
            for year in sorted({today.year, (today - timedelta(days=31)).year}):
                archive = get(spb.ARCHIVE_PATH, {"year": year})
                record_capture(
                    conn, ctx.run.id, "spb", "report_archive", None, archive, spb.PARSER_VERSION
                )
                parsed_archive = spb.json_body(archive.content)
                if not isinstance(parsed_archive, list):
                    raise FormatDriftError("SPB: expected report archive array")
                archive_rows.extend(parsed_archive)
            report_date, path = spb.parse_report_archive(
                json.dumps(archive_rows).encode(), today=today
            )
            stored = _stored_units(conn, instruments, report_date)
            if stored is not None:
                # Every bond was validated against this very (dated) report already.
                units = {
                    (b["ticker"], b["isin"]): (stored[b["isin"]][0], b["settlement_currency"])
                    for b in instruments
                }
                unit_captures = {isin: cap for isin, (_, cap) in stored.items()}
            else:
                report = get(path)
                unit_cap = record_capture(
                    conn, ctx.run.id, "spb", "bond_unit_report", None, report, spb.PARSER_VERSION
                )
                units = spb.parse_unit_report(
                    report.content,
                    report_date,
                    {b["ticker"]: b["isin"] for b in instruments},
                    missing_ok=True,
                )
                # Keep the original capture when its exact unit-reference payload is unchanged;
                # a new fetch alone is not a change to the instrument's reference facts.
                unit_captures = {
                    r["isin"]: r["unit_capture_id"]
                    for r in conn.execute(
                        "SELECT b.isin, b.unit_capture_id FROM ref.spb_bond b "
                        "JOIN ops.raw_capture old ON old.id = b.unit_capture_id "
                        "JOIN ops.raw_capture new ON new.id = %s AND new.sha256 = old.sha256 "
                        "WHERE b.unit_reference_date = %s",
                        (unit_cap, report_date),
                    ).fetchall()
                }
                unit_captures = {
                    b["isin"]: unit_captures.get(b["isin"], unit_cap) for b in instruments
                }
            admitted = []
            for bond in instruments:
                found = units.get((bond["ticker"], bond["isin"]))
                if found is None:
                    skip(
                        bond["ticker"], f"absent from the unit report of {report_date} (new issue?)"
                    )
                    continue
                unit, currency = found
                if currency != bond["settlement_currency"]:
                    skip(bond["ticker"], "card/report settlement currency mismatch")
                    continue
                bond.update(
                    price_unit=unit,
                    unit_reference_date=report_date,
                    unit_capture_id=unit_captures[bond["isin"]],
                    run_id=ctx.run.id,
                    unit_conflict=bond["card_price_unit"]
                    != ("% от номинальной стоимости" if unit == "PCT" else unit),
                )
                admitted.append(bond)
            if not admitted:
                raise FormatDriftError("SPB: no bond passed unit validation")
            # The stored reference unit only tells that a change was *seen* in this run; where
            # it happened is read from the stored bars (see _unit_freeze).
            previous_units = {
                r["isin"]: r["price_unit"]
                for r in conn.execute(
                    "SELECT isin, price_unit FROM ref.spb_bond "
                    "WHERE venue = 'SPB' AND isin = ANY(%s)",
                    ([b["isin"] for b in admitted],),
                ).fetchall()
            }
            for bond in admitted:
                old = previous_units.get(bond["isin"])
                if old is not None and old != bond["price_unit"]:
                    unit_seen[bond["ticker"]] = old
                    unit_changes[bond["ticker"]] = _unit_change(
                        bond["ticker"], bond["price_unit"], None, old
                    )
            ctx.run.add_counts(
                upsert_rows(conn, "ref.spb_bond", admitted, ["venue", "isin"], run_id=ctx.run.id)
            )

        result: dict[str, Any] = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "unit_reference_date": report_date.isoformat(),
            "price_basis": spb.PRICE_BASIS,
            "report_downloaded": stored is None,
            "instruments": {},
        }
        for bond in admitted:
            # Atomic per instrument/range. A malformed array never writes partial bars; the
            # response stays captured as evidence and the bond is skipped.
            with ctx.connection() as conn:
                response = get(spb.HISTORY_PATH, spb.history_params(bond["ticker"], start, end))
                cap = record_capture(
                    conn, ctx.run.id, "spb", "bond_chart_daily", None, response, spb.PARSER_VERSION
                )
                try:
                    bars = spb.parse_bars(response.content, start=start, end=end, today=today)
                except FormatDriftError as exc:
                    skip(bond["ticker"], f"chart: {exc}")
                    chart_failed.add(bond["isin"])
                    result["instruments"][bond["isin"]] = {
                        "ticker": bond["ticker"],
                        "skipped": str(exc),
                    }
                    continue
                freeze = _unit_freeze(conn, bond)
                rows = []
                not_written = 0
                for bar in bars:
                    if freeze is not None and bar["trade_date"] < freeze.new_unit_from:
                        # The chart's number for this date may be in either unit (it is not
                        # known whether the chart restated its history): no price, no label.
                        not_written += 1
                        continue
                    rows.append(
                        {
                            **bar,
                            **{key: bond[key] for key in ("venue", "isin", "ticker")},
                            **{key: bond[key] for key in _UNIT_FIELDS},
                            "source": "spb",
                            "price_basis": spb.PRICE_BASIS,
                            "capture_id": cap,
                            "run_id": ctx.run.id,
                        }
                    )
                bars_seen += len(bars)
                if freeze is not None:
                    unit_changes[bond["ticker"]] = _unit_change(
                        bond["ticker"], bond["price_unit"], freeze
                    )
                    unit_frozen[bond["isin"]] = {
                        "ticker": bond["ticker"],
                        "stored_unit": freeze.stored_unit,
                        "current_unit": bond["price_unit"],
                        "frozen_through": freeze.frozen_through.isoformat(),
                        "new_unit_from": freeze.new_unit_from.isoformat(),
                        "dates_not_written": not_written,
                    }
                # Only the run that sees the change, or that still has no bar of the new unit,
                # ends partial; afterwards the fact stays in ``unit_frozen`` and the log.
                if bond["ticker"] in unit_seen or (freeze is not None and freeze.pending):
                    unit_alarms.append(unit_changes[bond["ticker"]])
                counts = upsert_rows(
                    conn,
                    "md.spb_bond_daily",
                    rows,
                    ["venue", "isin", "trade_date"],
                    run_id=ctx.run.id,
                )
                ctx.run.add_counts(counts)
                total_rows += len(rows)
                days = {bar["trade_date"] for bar in bars}
                # A calendar date without a bar is normal for this dataset (weekends, holidays, a
                # bond that did not trade): no gap, no backfill task. Missing dates are counted
                # in the result and are never filled.
                result["instruments"][bond["isin"]] = {
                    "ticker": bond["ticker"],
                    "rows": len(rows),
                    "inserted": counts.inserted,
                    "updated": counts.updated,
                    "unchanged": counts.unchanged,
                    "unit_conflict": bond["unit_conflict"],
                    "missing_dates": (end - start).days + 1 - len(days),
                }
    if not any(b["isin"] not in chart_failed for b in admitted):
        detail = "; ".join(f"{t}: {r}" for t, r in skipped.items())
        raise FormatDriftError(
            f"SPB: the charts of all {len(admitted)} admitted bonds failed validation: {detail}"
        )
    result["rows"] = total_rows
    result["requests"] = requests
    result["skipped"] = dict(skipped)
    result["unit_changes"] = dict(unit_changes)
    result["unit_frozen"] = unit_frozen
    for note in unit_changes.values():
        ctx.logger.warning("SPB: %s", note)
    if unit_alarms:
        ctx.run.mark_partial("SPB: " + "; ".join(unit_alarms))
    if bars_seen == 0 and any(
        (start + timedelta(days=i)).weekday() < 5 for i in range((end - start).days + 1)
    ):
        # Weekends and holidays have no bars, but a weekday range with none for any bond is
        # more likely a changed request or a silent feed than a market-wide closure.
        ctx.run.mark_partial(f"SPB: no bars for any bond in range {start}..{end}")
    if skipped:
        ctx.run.mark_partial(
            f"SPB: {len(skipped)} of {total} bonds skipped: "
            + "; ".join(f"{t}: {r}" for t, r in skipped.items())
        )
    return result
