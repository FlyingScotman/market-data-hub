"""Russian SPB bonds: one discovery path, report unit reference, chart-only daily prices."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.gaps import close_gap, record_no_data
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


def load_range(
    *,
    today: date,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[date, date]:
    """Completed Moscow calendar dates, inclusive, at most 31 days. No holiday filter."""
    if day is not None and (date_from is not None or date_to is not None):
        raise JobParamError("SPB: day and date range are mutually exclusive")
    if (date_from is None) != (date_to is None):
        raise JobParamError("SPB: both date_from and date_to are required")
    end = day or date_to or today - timedelta(days=1)
    start = day or date_from or end - timedelta(days=2)
    if start > end or end >= today or (end - start).days >= spb.MAX_LOAD_DAYS:
        raise JobParamError("SPB: require 1..31 completed calendar dates before today (MSK)")
    return start, end


@job("spb_bonds_daily", "SPB Russian bond chart daily OHLC (not official close)")
def spb_bonds_daily(
    ctx: JobContext,
    *,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    today = ctx.today()
    start, end = load_range(today=today, day=day, date_from=date_from, date_to=date_to)
    with ctx.http() as http:
        # Finished discovery is committed before prices; a later chart error retains evidence.
        # No instrument is admitted until all metadata joins/units have been validated.
        with ctx.connection() as conn:
            response = spb.fetch(http, spb.UNIVERSE_PATH)
            record_capture(
                conn, ctx.run.id, "spb", "russian_universe", None, response, spb.PARSER_VERSION
            )
            candidates = spb.parse_universe(response.content)
            if not candidates:
                raise FormatDriftError("SPB: Russian market returned no bond candidates")
            instruments = []
            for candidate in candidates:
                card = spb.fetch(http, spb.CARD_PATH.format(candidate.security_id))
                cap = record_capture(
                    conn, ctx.run.id, "spb", "russian_bond_card", None, card, spb.PARSER_VERSION
                )
                instruments.append({**spb.parse_card(card.content, candidate), "capture_id": cap})
            if len({b["isin"] for b in instruments}) != len(instruments):
                raise FormatDriftError("SPB: duplicate ISIN across discovered bonds")
            archive_rows = []
            for year in sorted({today.year, (today - timedelta(days=31)).year}):
                archive = spb.fetch(http, spb.ARCHIVE_PATH, {"year": year})
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
            report = spb.fetch(http, path)
            unit_cap = record_capture(
                conn, ctx.run.id, "spb", "bond_unit_report", None, report, spb.PARSER_VERSION
            )
            units = spb.parse_unit_report(
                report.content, report_date, {b["ticker"]: b["isin"] for b in instruments}
            )
            # Keep the original capture when its exact unit-reference payload is unchanged;
            # a new fetch alone is not a change to the instrument's reference facts.
            same_reference = {
                r["isin"]: r["unit_capture_id"]
                for r in conn.execute(
                    "SELECT b.isin, b.unit_capture_id FROM ref.spb_bond b "
                    "JOIN ops.raw_capture old ON old.id = b.unit_capture_id "
                    "JOIN ops.raw_capture new ON new.id = %s AND new.sha256 = old.sha256 "
                    "WHERE b.unit_reference_date = %s",
                    (unit_cap, report_date),
                ).fetchall()
            }
            for bond in instruments:
                unit, currency = units[bond["ticker"], bond["isin"]]
                if currency != bond["settlement_currency"]:
                    raise FormatDriftError("SPB: card/report settlement currency mismatch")
                bond.update(
                    price_unit=unit,
                    unit_reference_date=report_date,
                    unit_capture_id=same_reference.get(bond["isin"], unit_cap),
                    run_id=ctx.run.id,
                    unit_conflict=bond["card_price_unit"]
                    != ("% от номинальной стоимости" if unit == "PCT" else unit),
                )
            ctx.run.add_counts(
                upsert_rows(conn, "ref.spb_bond", instruments, ["venue", "isin"], run_id=ctx.run.id)
            )

        result: dict[str, Any] = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "unit_reference_date": report_date.isoformat(),
            "price_basis": spb.PRICE_BASIS,
            "instruments": {},
        }
        for bond in instruments:
            # Atomic per instrument/range. A malformed array never writes partial bars.
            with ctx.connection() as conn:
                response = spb.fetch(
                    http, spb.HISTORY_PATH, spb.history_params(bond["ticker"], start, end)
                )
                cap = record_capture(
                    conn, ctx.run.id, "spb", "bond_chart_daily", None, response, spb.PARSER_VERSION
                )
                bars = spb.parse_bars(response.content, start=start, end=end, today=today)
                rows = [
                    {
                        **bar,
                        **{
                            key: bond[key]
                            for key in (
                                "venue",
                                "isin",
                                "ticker",
                                "price_unit",
                                "unit_reference_date",
                                "card_price_unit",
                                "unit_conflict",
                            )
                        },
                        "source": "spb",
                        "price_basis": spb.PRICE_BASIS,
                        "capture_id": cap,
                        "run_id": ctx.run.id,
                    }
                    for bar in bars
                ]
                counts = upsert_rows(
                    conn,
                    "md.spb_bond_daily",
                    rows,
                    ["venue", "isin", "trade_date"],
                    run_id=ctx.run.id,
                )
                ctx.run.add_counts(counts)
                days = {bar["trade_date"] for bar in bars}
                current = start
                # No synthetic data or exchange-calendar inference. An empty source date is
                # recorded per ISIN, never declared complete because another bond has rows.
                while current <= end:
                    key = {"venue": "SPB", "isin": bond["isin"]}
                    if current in days:
                        close_gap(conn, DATASET.name, current, key=key)
                    else:
                        record_no_data(
                            conn,
                            DATASET.name,
                            current,
                            f"SPB chart returned no bar for {bond['ticker']}; "
                            "no session/trade completeness assertion",
                            today=today,
                            key=key,
                        )
                    current += timedelta(days=1)
                result["instruments"][bond["isin"]] = {
                    "ticker": bond["ticker"],
                    "rows": len(rows),
                    "inserted": counts.inserted,
                    "updated": counts.updated,
                    "unchanged": counts.unchanged,
                    "unit_conflict": bond["unit_conflict"],
                    "missing_dates": (end - start).days + 1 - len(days),
                }
    return result
