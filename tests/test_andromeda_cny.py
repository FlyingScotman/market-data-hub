"""Andromeda CNY swap history import (synthetic workbook/CSV built here; the owner's export is
not part of the repository)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub import cli
from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database
from mdhub.legacy.andromeda_cny import (
    AndromedaImportError,
    import_andromeda_cny,
    parse_csv,
    parse_xlsx,
    xlsx_modified,
)
from mdhub.legacy.map_md import curve_content_hash

Conn = psycopg.Connection[DictRow]
EPOCH = date(1899, 12, 30)
TENORS = ["1W", "2W", "1M", "3M", "6M", "1Y", "2Y"]
EXPORTED = "2026-10-16T15:05:15Z"  # = 18:05:15 MSK
EXPORTED_AT = datetime(2026, 10, 16, 18, 5, 15, tzinfo=MSK)

# date -> values per TENORS (None = missing / '-')
RUSFAR: dict[date, list[str | None]] = {
    date(2024, 9, 17): ["20.58", "20.27", "18.72", "20", "16.9", "17.72", "16.83"],
    date(2024, 9, 18): ["43.15", "39.14", "23.06", "22.56", "22.56", "20.94", "21.89"],
    date(2025, 12, 15): ["-1", "-0.5", "0.25", "1.5", "2.5", "3.75", "5"],
    date(2025, 12, 16): [None, None, "0.3", "1.6", "2.5", "3.8", None],  # partial day
    date(2025, 12, 17): [None] * 7,  # nothing at all: not stored
    date(2026, 3, 13): ["5", "5", "5", "5", "6", "7", "7"],
    date(2026, 3, 18): ["17", "16", "14", "13", "12", "10", "10"],  # placeholder-like
    date(2026, 3, 25): ["10"] * 7,  # flat
    date(2026, 10, 15): ["0.8", "1", "2.4", "4.6", "6.1", "7.85", "8"],
    date(2026, 10, 16): ["0.8", "1", "2.4", "4.6", "6.1", "7.65", "7.75"],  # export day
}
XCCY: dict[date, list[str | None]] = {
    date(2026, 10, 15): [None, "1.7", "2.9", "5.48", "7.13", "8.829999999999999", "8.6"],
    date(2026, 10, 16): ["1.2", "1.4", "2.85", "5", "6.75", "8.5", "8.6"],
}


def _cell(ref: str, value: str | None) -> str:
    if value is None:
        return f'<c r="{ref}" t="inlineStr" />'
    return f'<c r="{ref}" t="n"><v>{value}</v></c>'


def _sheet_xml(rows: dict[date, list[str | None]]) -> str:
    cols = "ABCDEFGHIJKLM"
    out = ['<row r="1"><c r="A1" t="inlineStr"><is><t>Дата</t></is></c>']
    out += [
        f'<c r="{cols[i + 1]}1" t="inlineStr"><is><t>{t}</t></is></c>' for i, t in enumerate(TENORS)
    ]
    out.append("</row>")
    for n, (day, vals) in enumerate(sorted(rows.items()), start=2):
        serial = (day - EPOCH).days
        cells = [f'<c r="A{n}" s="1" t="n"><v>{serial}</v></c>']
        cells += [_cell(f"{cols[i + 1]}{n}", v) for i, v in enumerate(vals)]
        out.append(f'<row r="{n}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return f'<worksheet xmlns="{ns}"><sheetData>{"".join(out)}</sheetData></worksheet>'


def make_xlsx(
    rusfar: dict[date, list[str | None]] = RUSFAR,
    xccy: dict[date, list[str | None]] = XCCY,
    modified: str = EXPORTED,
) -> bytes:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pns = "http://schemas.openxmlformats.org/package/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns:r="{rns}" xmlns="{ns}"><sheets>'
            '<sheet name="CNY_IRS_RUSFAR_OIS" sheetId="1" r:id="rId1" />'
            '<sheet name="XCCY" sheetId="2" r:id="rId2" /></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{pns}">'
            '<Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml" Type="ws" />'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="ws" /></Relationships>',
        )
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rusfar))
        zf.writestr("xl/worksheets/sheet2.xml", _sheet_xml(xccy))
        zf.writestr(
            "docProps/core.xml",
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dcterms="http://purl.org/dc/terms/">'
            f"<dcterms:modified>{modified}</dcterms:modified></cp:coreProperties>",
        )
    return buf.getvalue()


def make_csv(rows: dict[date, list[str | None]]) -> bytes:
    lines = ["Дата," + ",".join(t.lower() for t in TENORS)]
    for day, vals in sorted(rows.items(), reverse=True):
        cells = ["-" if v is None else f'"{Decimal(v):.2f}%"'.replace(".", ",") for v in vals]
        lines.append(day.isoformat() + "," + ",".join(cells))
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    out = {
        "xlsx": tmp_path / "andromeda_cny_swaps.xlsx",
        "rusfar_csv": tmp_path / "andromeda_rusfar_cny_ois.csv",
        "xccy_csv": tmp_path / "andromeda_xccy.csv",
    }
    out["xlsx"].write_bytes(make_xlsx())
    out["rusfar_csv"].write_bytes(make_csv(RUSFAR))
    out["xccy_csv"].write_bytes(make_csv(XCCY))
    return out


# ------------------------------------------------------------------------------ parsing


def test_parse_csv_dash_negatives_and_percent() -> None:
    sheet = parse_csv(make_csv(RUSFAR), "cny_rusfar_ois")
    assert sheet.tenors == tuple(TENORS)  # upper-cased from "1w"
    rows = dict(sheet.rows)
    assert rows[date(2025, 12, 15)][:2] == (Decimal("-1"), Decimal("-0.5"))
    assert rows[date(2025, 12, 16)][0] is None and rows[date(2025, 12, 16)][2] == Decimal("0.3")
    assert rows[date(2026, 10, 15)][0] == Decimal("0.8")  # "0,80%"
    assert all(v is None for v in rows[date(2025, 12, 17)])


def test_parse_xlsx_serial_dates_and_float_noise() -> None:
    payload = make_xlsx()
    sheets = {s.dataset: s for s in parse_xlsx(payload)}
    assert set(sheets) == {"cny_rusfar_ois", "cny_ruonia_xccy"}
    xccy = dict(sheets["cny_ruonia_xccy"].rows)
    assert xccy[date(2026, 10, 15)][5] == Decimal("8.83")  # 8.829999999999999 -> 8.83
    assert xccy[date(2026, 10, 15)][0] is None
    assert xlsx_modified(payload) == EXPORTED_AT
    # the CSV of the same data parses to identical values
    csv_rows = dict(parse_csv(make_csv(XCCY), "cny_ruonia_xccy").rows)
    assert csv_rows == xccy


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda r: r.__setitem__(date(2026, 10, 15), ["400", *r[date(2026, 10, 15)][1:]]),
            "outside",
        ),
        (
            lambda r: r.__setitem__(date(2026, 10, 15), ["abc", *r[date(2026, 10, 15)][1:]]),
            "not a number",
        ),
        (lambda r: r.__setitem__(date(2026, 10, 17), ["1"] * 7), "after the export date"),
    ],
)
def test_bad_input_fails_closed(
    mutate: Any, match: str, tmp_path: Path, db: Database, conn: Conn
) -> None:
    rows = {k: list(v) for k, v in RUSFAR.items()}
    mutate(rows)
    path = tmp_path / "a.xlsx"
    path.write_bytes(make_xlsx(rusfar=rows))
    with pytest.raises(AndromedaImportError, match=match):
        import_andromeda_cny(db, [path])
    assert conn.execute("SELECT count(*) AS n FROM md.curve_snapshot").fetchone() == {"n": 0}


def test_duplicate_date_and_header_rejected() -> None:
    dup = make_csv({date(2026, 1, 5): ["1"] * 7}) + b"2026-01-05,1,1,1,1,1,1,1\r\n"
    with pytest.raises(AndromedaImportError, match="duplicate date"):
        parse_csv(dup, "cny_rusfar_ois")
    with pytest.raises(AndromedaImportError, match="first column"):
        parse_csv(b"Date?,1W\r\n2026-01-05,1\r\n", "cny_rusfar_ois")
    with pytest.raises(AndromedaImportError, match="tenor header"):
        parse_csv("Дата,1W,foo\r\n".encode(), "cny_rusfar_ois")


def test_csv_needs_export_time(files: dict[str, Path], db: Database) -> None:
    with pytest.raises(AndromedaImportError, match="no export time"):
        import_andromeda_cny(db, [files["rusfar_csv"]])


def test_disagreeing_inputs_fail(files: dict[str, Path], db: Database, tmp_path: Path) -> None:
    other = dict(RUSFAR) | {date(2026, 10, 15): ["0.9", "1", "2.4", "4.6", "6.1", "7.85", "8"]}
    csv = tmp_path / "rusfar_changed.csv"
    csv.write_bytes(make_csv(other))
    with pytest.raises(AndromedaImportError, match="disagrees"):
        import_andromeda_cny(db, [files["xlsx"], csv])


# ------------------------------------------------------------------------------- import


def _snaps(conn: Conn, dataset: str) -> dict[date, dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, snapshot_date, fetched_at, quote_basis, content_sha256, meta, label,
                  capture_id, parser_version
           FROM md.curve_snapshot WHERE source = 'andromeda' AND dataset = %s
           ORDER BY fetched_at""",
        (dataset,),
    ).fetchall()
    return {r["snapshot_date"]: r for r in rows}


def _quotes(conn: Conn, snapshot_id: int) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT tenor, position, tenor_months, tenor_days, bid_pct, ask_pct, mid_pct
           FROM md.curve_quote WHERE snapshot_id = %s ORDER BY position""",
        (snapshot_id,),
    ).fetchall()


def test_import_snapshots(files: dict[str, Path], db: Database, conn: Conn) -> None:
    rep = import_andromeda_cny(db, [files["xlsx"]])
    assert rep.exported_at == EXPORTED_AT
    assert rep.exported_at_basis.startswith("xlsx docProps")
    st = rep.datasets["cny_rusfar_ois"]
    assert st.rows == 10 and st.stored_days == 9 and st.empty_days == ["2025-12-17"]
    assert st.partial_days == 1 and st.negative_values == 2
    assert rep.snapshots.inserted == 9 + 2
    snaps = _snaps(conn, "cny_rusfar_ois")
    assert date(2025, 12, 17) not in snaps  # no values at all -> no snapshot
    eod = snaps[date(2026, 10, 15)]
    assert eod["fetched_at"] == datetime(2026, 10, 15, 23, 59, 59, tzinfo=MSK)
    assert eod["meta"]["quality_flags"][0] == "fetched_at_synthetic"
    assert eod["quote_basis"] == "mid_only" and eod["label"] == "CNY_IRS_RUSFAR_OIS"
    intraday = snaps[date(2026, 10, 16)]
    assert intraday["fetched_at"] == EXPORTED_AT  # real export time, not 23:59:59
    assert intraday["meta"] == {"export": "intraday", "quality_flags": []}
    q = _quotes(conn, eod["id"])
    assert [r["tenor"] for r in q] == TENORS and [r["position"] for r in q] == list(range(7))
    assert q[0]["bid_pct"] is None and q[0]["ask_pct"] is None and q[0]["mid_pct"] == Decimal("0.8")
    assert q[0]["tenor_days"] == 7 and q[-1]["tenor_months"] == 24
    neg = _quotes(conn, snaps[date(2025, 12, 15)]["id"])
    assert neg[0]["mid_pct"] == Decimal("-1")
    partial = snaps[date(2025, 12, 16)]
    assert partial["meta"]["missing_tenors"] == ["1W", "2W", "2Y"]
    assert "andromeda_partial_curve" in partial["meta"]["quality_flags"]
    pq = _quotes(conn, partial["id"])
    assert [(r["tenor"], r["position"]) for r in pq] == [("1M", 0), ("3M", 1), ("6M", 2), ("1Y", 3)]
    qlist = [
        {"tenor": r["tenor"], "position": r["position"], "bid_pct": None, "ask_pct": None,
         "mid_pct": r["mid_pct"]}
        for r in pq
    ]  # fmt: skip
    assert partial["content_sha256"] == curve_content_hash("mid_only", qlist)
    cap = conn.execute(
        "SELECT endpoint, url_redacted, fetched_at, source FROM ops.raw_capture"
    ).fetchall()
    assert cap == [
        {
            "endpoint": "file:andromeda_cny.xlsx",
            "url_redacted": "file:andromeda_cny_swaps.xlsx",
            "fetched_at": EXPORTED_AT,
            "source": "andromeda",
        }
    ]
    assert len(_snaps(conn, "cny_ruonia_xccy")) == 2


def test_quality_flags_and_notes(files: dict[str, Path], db: Database, conn: Conn) -> None:
    import_andromeda_cny(db, [files["xlsx"]])
    snaps = _snaps(conn, "cny_rusfar_ois")
    squeeze = snaps[date(2024, 9, 18)]["meta"]
    assert "jump_gt_500bp" in squeeze["quality_flags"]
    assert squeeze["jumps_bp"]["1W"] == 2257 and squeeze["jump_prev_date"] == "2024-09-17"
    assert "judged real" in squeeze["quality_note"]
    placeholder = snaps[date(2026, 3, 18)]["meta"]
    assert {"jump_gt_500bp", "integer_grid_curve"} <= set(placeholder["quality_flags"])
    assert "placeholder" in placeholder["quality_note"]
    flat = snaps[date(2026, 3, 25)]["meta"]["quality_flags"]
    assert "flat_curve" in flat and "integer_grid_curve" in flat
    assert snaps[date(2026, 10, 16)]["meta"]["quality_flags"] == []  # -20 bp: no flag
    assert "integer_grid_curve" not in snaps[date(2025, 12, 15)]["meta"]["quality_flags"]


def test_reimport_is_idempotent(files: dict[str, Path], db: Database, conn: Conn) -> None:
    import_andromeda_cny(db, [files["xlsx"]])
    again = import_andromeda_cny(db, [files["xlsx"]])
    assert again.snapshots.inserted == 0 and again.snapshots.updated == 0
    assert again.quotes.inserted == 0 and again.quotes.updated == 0
    # the same data as CSV (same export time): nothing changes either
    csv = import_andromeda_cny(
        db, [files["rusfar_csv"], files["xccy_csv"]], exported_at=EXPORTED_AT
    )
    assert csv.snapshots.changed == 0 and csv.quotes.changed == 0
    assert conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone() == {"n": 0}
    assert conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone() == {"n": 3}
    n = conn.execute("SELECT count(*) AS n FROM md.curve_snapshot").fetchone()
    assert n == {"n": 11}


def test_later_export_revises_and_adds_eod(
    files: dict[str, Path], db: Database, conn: Conn, tmp_path: Path
) -> None:
    import_andromeda_cny(db, [files["xlsx"]])
    before = _snaps(conn, "cny_rusfar_ois")
    # next export (2026-10-19): 2026-10-15 revised (1W changed, 2Y removed), 2026-10-16 now a
    # past day, 2026-10-19 the new export day; it starts later than the first export.
    later = {
        date(2026, 10, 15): ["0.85", "1", "2.4", "4.6", "6.1", "7.85", None],
        date(2026, 10, 16): ["0.8", "1", "2.4", "4.6", "6.1", "7.65", "7.75"],
        date(2026, 10, 19): ["7", "1", "2.4", "4.6", "6.1", "7.65", "7.75"],
    }
    path = tmp_path / "andromeda_cny_swaps_2.xlsx"
    path.write_bytes(make_xlsx(rusfar=later, xccy=XCCY, modified="2026-10-19T09:00:00Z"))
    rep = import_andromeda_cny(db, [path])
    # rusfar 2026-10-16 EOD + 2026-10-19 intraday, xccy 2026-10-16 EOD; xccy 10-15 unchanged
    assert rep.snapshots.inserted == 3 and rep.snapshots.updated == 1
    assert rep.quotes_deleted == 1
    after = _snaps(conn, "cny_rusfar_ois")
    revised = after[date(2026, 10, 15)]
    assert revised["id"] == before[date(2026, 10, 15)]["id"]
    assert revised["content_sha256"] != before[date(2026, 10, 15)]["content_sha256"]
    assert "andromeda_partial_curve" in revised["meta"]["quality_flags"]
    q = {r["tenor"]: r["mid_pct"] for r in _quotes(conn, revised["id"])}
    assert q["1W"] == Decimal("0.85") and "2Y" not in q
    hist = conn.execute(
        "SELECT table_name, row_key, old_row FROM ops.row_history ORDER BY id"
    ).fetchall()
    by_table: dict[str, list[dict[str, Any]]] = {}
    for h in hist:
        by_table.setdefault(h["table_name"], []).append(h)
    assert len(by_table["md.curve_snapshot"]) == 1
    old_quotes = {
        h["row_key"]["tenor"]: h["old_row"]["mid_pct"] for h in by_table["md.curve_quote"]
    }
    assert old_quotes == {"1W": 0.8, "2Y": 8}
    # 2026-10-16: intraday (first export) and EOD (second export) both kept
    rows_1016 = conn.execute(
        """SELECT fetched_at, meta FROM md.curve_snapshot
           WHERE dataset = 'cny_rusfar_ois' AND snapshot_date = '2026-10-16' ORDER BY fetched_at"""
    ).fetchall()
    assert [r["fetched_at"] for r in rows_1016] == [
        EXPORTED_AT,
        datetime(2026, 10, 16, 23, 59, 59, tzinfo=MSK),
    ]
    # the jump check of the export's first day uses the stored previous day (2026-10-14
    # absent: 2026-03-25 flat 10 -> 2026-10-15 is the first row of this export)
    first = after[date(2026, 10, 15)]["meta"]
    assert first["jump_prev_date"] == "2026-03-25" and first["jumps_bp"]["1W"] == -915
    new_day = after[date(2026, 10, 19)]
    assert new_day["fetched_at"] == datetime(2026, 10, 19, 12, 0, tzinfo=MSK)
    assert new_day["meta"]["jumps_bp"] == {"1W": 620}


def test_history_enabled_on_curve_tables(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
           WHERE t.tgname = 'mdhub_row_history' AND c.relname LIKE 'curve_%'
           ORDER BY 1"""
    ).fetchall()
    assert [r["relname"] for r in rows] == ["curve_quote", "curve_snapshot"]


def test_cli(
    files: dict[str, Path], db_url: str, monkeypatch: pytest.MonkeyPatch, conn: Conn
) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    runner = CliRunner()
    dry = runner.invoke(cli.app, ["legacy", "andromeda-cny", str(files["xlsx"]), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert conn.execute("SELECT count(*) AS n FROM md.curve_snapshot").fetchone() == {"n": 0}
    res = runner.invoke(cli.app, ["legacy", "andromeda-cny", str(files["xlsx"]), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output[res.output.index("{") : res.output.rindex("}") + 1])
    assert data["snapshots"]["inserted"] == 11
    assert data["exported_at_msk"] == "2026-10-16T18:05:15+03:00"
    bad = runner.invoke(cli.app, ["legacy", "andromeda-cny", str(files["rusfar_csv"])])
    assert bad.exit_code == 1 and "--exported-at" in bad.output
    ok = runner.invoke(
        cli.app,
        [
            "legacy",
            "andromeda-cny",
            str(files["rusfar_csv"]),
            "--exported-at",
            EXPORTED_AT.isoformat(),
        ],
    )
    assert ok.exit_code == 0, ok.output
    run = conn.execute(
        "SELECT job, status, trigger FROM ops.job_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run == {"job": "import_andromeda_cny", "status": "succeeded", "trigger": "cli"}
