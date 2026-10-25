"""TDM contract-1.3 CSV export (mdhub.export.tdm) and ``mdhub export tdm``."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub.db.pool import Database
from mdhub.export.tdm import (
    HEADERS,
    KEYS,
    REQUIRED,
    TdmExport,
    TdmExportOptions,
    build_tdm_export,
    classify,
    dataset_of,
)
from mdhub.sources.moex_market import kbd_yield_pct
from test_api_seed import KBD_PARAMS, seed

# A local trading-desk-monitor checkout: $MDHUB_TDM_REPO, else a sibling of this repository.
TDM_REPO = Path(
    os.environ.get("MDHUB_TDM_REPO") or Path(__file__).resolve().parents[2] / "trading-desk-monitor"
)
TDM_COMMIT = "a6ba30b"  # contract 1.3


def _export(conn: psycopg.Connection[DictRow], **kw: Any) -> TdmExport:
    seed(conn)
    return build_tdm_export(conn, TdmExportOptions(date_to=date(2026, 10, 15), **kw))


def _read(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Parse like TDM's loader: BOM-tolerant UTF-8, separator from the header line."""
    text = data.decode("utf-8-sig")
    first = text.split("\n", 1)[0]
    assert first.count(";") > first.count(","), "TDM must detect ';' as separator"
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    return list(reader.fieldnames or []), list(reader)


def _files(export: TdmExport) -> dict[str, tuple[list[str], list[dict[str, str]]]]:
    return {n: _read(b) for n, b in export.rendered().items() if n.endswith(".csv")}


def _tdm_template(name: str) -> list[str] | None:
    if not (TDM_REPO / ".git").exists() or shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "-C", str(TDM_REPO), "show", f"{TDM_COMMIT}:contract/csv/{name}.csv"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8-sig").splitlines()[0].split(";")


@pytest.mark.parametrize("dataset", sorted(HEADERS))
def test_headers_match_tdm_contract_templates(dataset: str) -> None:
    tdm_name = {"md_fx": "md_fx"}.get(dataset, dataset)
    header = _tdm_template(tdm_name)
    if header is None:
        pytest.skip("TDM repo (contract 1.3 commit) not available")
    assert list(HEADERS[dataset]) == header


def test_file_names_select_the_right_tdm_dataset() -> None:
    assert dataset_of("md_fx_cbr_2026-10-15.csv") == "md_fx_cbr"
    assert dataset_of("md_fx_moex_indicative_2026-10-15.csv") == "md_fx"
    assert dataset_of("issuers_2026-10-15.csv") == "issuers"
    assert dataset_of("issuer_ratings_x.csv") == "issuer_ratings"
    assert dataset_of("md_fixings_2026-10-15.csv") == "md_fixings"
    assert dataset_of("manifest.json") is None


def test_export_files_and_rules(conn: psycopg.Connection[DictRow]) -> None:
    export = _export(conn, include_fx_candidate=True)
    files = _files(export)
    assert set(files) == {
        "instruments_2026-10-15.csv",
        "issuers_2026-10-15.csv",
        "bond_cashflows_2026-10-15.csv",
        "curves_2026-10-15.csv",
        "md_curves_2026-10-15.csv",
        "md_fx_cbr_2026-10-15.csv",
        "md_fixings_2026-10-15.csv",
        "md_bond_quotes_2026-10-15.csv",
        "md_fx_moex_indicative_2026-10-15.csv",
        "cbr_meetings_2026-10-15.csv",
        "calendar_2026-10-15.csv",
    }
    for name, (header, rows) in files.items():
        ds = dataset_of(name)
        assert ds is not None
        assert header == list(HEADERS[ds]), name
        keys = [tuple(r[k] for k in KEYS[ds]) for r in rows]
        assert len(keys) == len(set(keys)), f"duplicate key in {name}"
        for r in rows:
            for col in REQUIRED[ds]:
                assert r[col] != "", f"{name}: empty required {col}"
            for col in header:
                v = r[col]
                assert "E" not in v or not v[:1].isdigit(), f"exponent in {name}.{col}: {v}"
    raw = export.rendered()["md_fx_cbr_2026-10-15.csv"]
    assert raw.startswith(b"\xef\xbb\xbf")


def test_instruments_issuers_cashflows(conn: psycopg.Connection[DictRow]) -> None:
    files = _files(_export(conn))
    inst = {r["instrument_id"]: r for r in files["instruments_2026-10-15.csv"][1]}
    # perpetual (no maturity) and matured > 1y ago are left out; duplicate ISIN -> SECID
    assert set(inst) == {"SU26242RMFS0", "RU000A105X12", "DUPA", "DUPB"}
    ofz = inst["SU26242RMFS0"]
    assert ofz["asset_class"] == "ОФЗ-ПД"
    assert ofz["segment"] == "Гос"
    assert ofz["issuer_id"] == "7710168360"
    assert ofz["coupon_type"] == "fix"
    assert ofz["coupon_rate"] == "9.0"
    assert ofz["maturity"] == "2029-08-29"
    assert ofz["amortizing"] == "0"
    assert ofz["is_mbs"] == "0"
    assert ofz["collateral_eligible"] == ""
    flo = inst["RU000A105X12"]
    assert (flo["asset_class"], flo["coupon_type"], flo["index_name"]) == (
        "Корп RUB флоатер",
        "float",
        "КС",
    )
    assert Decimal(flo["margin"]) == Decimal("1.5")
    assert flo["coupon_rate"] == ""
    assert flo["offer_date"] == "2027-01-15"
    assert flo["call_date"] == "2027-07-15"
    assert flo["is_subord"] == "1"
    assert inst["DUPA"]["asset_class"] == "Корп валютные"
    assert inst["DUPA"]["ccy"] == "USD"
    assert inst["DUPA"]["issuer_id"] == "MOEX:3"

    issuers = {r["issuer_id"]: r for r in files["issuers_2026-10-15.csv"][1]}
    assert issuers["7710168360"]["is_gov"] == "1"
    assert issuers["7710168360"]["name"] == "Минфин РФ"
    assert issuers["MOEX:3"]["name"] == "АО Без ИНН"
    assert set(issuers) >= {r["issuer_id"] for r in inst.values()}

    flows = [
        (r["instrument_id"], r["date"], r["type"], r["rate"], r["amount"])
        for r in files["bond_cashflows_2026-10-15.csv"][1]
    ]
    assert ("SU26242RMFS0", "2027-03-03", "coupon", "9.0", "44.88") in flows
    assert ("SU26242RMFS0", "2029-08-29", "redemption", "", "1000") in flows
    assert ("RU000A105X12", "2027-01-15", "coupon", "", "") in flows  # unknown amount: empty
    assert ("RU000A105X12", "2027-01-15", "offer", "", "") in flows
    assert all(f[0] in inst for f in flows)


def test_market_files(conn: psycopg.Connection[DictRow]) -> None:
    export = _export(conn, include_fx_candidate=True)
    files = _files(export)
    quotes = {r["instrument_id"]: r for r in files["md_bond_quotes_2026-10-15.csv"][1]}
    assert set(quotes) == {"SU26242RMFS0", "RU000A105X12", "DUPA"}  # only the market board
    ofz = quotes["SU26242RMFS0"]
    assert ofz["clean_price"] == "97.80"  # default basis: WAP
    assert ofz["ytm"] == "15.45"  # MOEX effective yield at WAP (to its yield date)
    assert ofz["accrued"] == "0.123000"  # 1.23 RUB per 1000 face
    assert ofz["volume_rub"] == "1000000.50"
    assert ofz["source"] == "MOEX TQOB WAP"
    assert ofz["z_spread_bp"] == ofz["g_spread_bp"] == ""
    assert quotes["RU000A105X12"]["ytm"] == ""  # yields row priced another number
    assert quotes["DUPA"]["accrued"] == ""  # USD face, RUB settlement: no %
    close = _files(
        build_tdm_export(conn, TdmExportOptions(date(2026, 10, 15), price_basis="close"))
    )
    cq = {r["instrument_id"]: r for r in close["md_bond_quotes_2026-10-15.csv"][1]}
    assert (cq["SU26242RMFS0"]["clean_price"], cq["SU26242RMFS0"]["ytm"]) == ("97.85", "15.42")
    # YIELDCLOSE is a yield to maturity (also with an offer ahead) and a different convention:
    # never exported; the floater's MOEX effective yield is not a usable ytm either
    assert cq["RU000A105X12"]["ytm"] == ""

    curves = files["md_curves_2026-10-15.csv"][1]
    assert [r["tenor_years"] for r in curves] == [
        "0.0833", "0.25", "0.5", "1", "2", "3", "5", "7", "10", "15", "20"
    ]  # fmt: skip
    by_tenor = {r["tenor_years"]: r["rate"] for r in curves}
    assert by_tenor["0.25"] == "12.4175"  # reproduces MOEX's published standard yield
    assert by_tenor["2"] == "15.0208"
    assert by_tenor["10"] == f"{kbd_yield_pct(KBD_PARAMS, 10.0):.4f}"
    assert {r["curve_id"] for r in curves} == {"RUB OFZ"}
    assert files["curves_2026-10-15.csv"][1][0]["curve_id"] == "RUB OFZ"

    cbr = [
        (r["date"], r["ccy"], r["rate"], r["set_date"])
        for r in files["md_fx_cbr_2026-10-15.csv"][1]
    ]
    # effective on D, plus the rate set on D for D+1; nothing from before D
    assert cbr == [
        ("2026-10-15", "JPY", "0.543210", "2026-10-14"),
        ("2026-10-15", "USD", "81.1200", "2026-10-14"),
        ("2026-10-16", "USD", "81.5000", "2026-10-15"),
    ]

    fix = {(r["date"], r["index_name"]): r["value"] for r in files["md_fixings_2026-10-15.csv"][1]}
    assert fix == {
        ("2026-10-15", "КС"): "16.5",
        ("2026-10-14", "RUONIA"): "16.32",  # observation date, published on D
        ("2026-10-15", "RUSFAR O/N"): "16.10",
        ("2026-10-15", "RUSFAR 1W"): "16.20",
        ("2026-10-15", "RUSFAR CNY"): "7.50",
    }

    fx = files["md_fx_moex_indicative_2026-10-15.csv"][1]
    assert [(r["ccy"], r["rate"]) for r in fx] == [("CNY", "11.35"), ("USD", "81.45")]
    assert all("кандидат" in r["source"] for r in fx)
    meetings = files["cbr_meetings_2026-10-15.csv"][1]
    assert [(m["date"], m["effective_date"]) for m in meetings] == [
        ("2026-09-12", "2026-09-15"),
        ("2026-10-24", "2026-10-27"),
    ]
    cal = files["calendar_2026-10-15.csv"][1]
    assert [(c["date"], c["is_business_day"]) for c in cal] == [
        ("2026-10-15", "1"),
        ("2026-10-17", "1"),
    ]


def test_manifest_and_options(conn: psycopg.Connection[DictRow]) -> None:
    export = _export(conn, include_fx_candidate=False, calendar="RU_PRODUCTION")
    rendered = export.rendered()
    assert not any(n.startswith("md_fx_moex") for n in rendered)
    manifest = json.loads(rendered["manifest.json"])
    assert manifest["tdm_contract_version"] == "1.3"
    assert manifest["excluded"]["instruments: no maturity (perpetual)"] == 1
    assert "issuer_ratings" in manifest["absent"]
    assert "md_cds" in manifest["absent"]
    for f in manifest["files"]:
        assert f["rows"] > 0
        assert len(f["sha256"]) == 64
    cal = _read(rendered["calendar_2026-10-15.csv"])[1]
    assert [(c["date"], c["is_business_day"]) for c in cal] == [("2026-10-17", "0")]
    with pytest.raises(ValueError, match="price_basis"):
        TdmExportOptions(date(2026, 10, 15), price_basis="mid")
    with pytest.raises(ValueError, match="date_from"):
        TdmExportOptions(date(2026, 10, 15), date_from=date(2026, 10, 16))


def test_range_export_never_fills(conn: psycopg.Connection[DictRow]) -> None:
    export = _export(conn, date_from=date(2026, 10, 14))
    files = _files(export)
    assert "md_bond_quotes_2026-10-14_2026-10-15.csv" in files
    quotes = files["md_bond_quotes_2026-10-14_2026-10-15.csv"][1]
    days = sorted({(r["date"], r["instrument_id"]) for r in quotes})
    # 14 Oct has a quote only for the OFZ: other bonds are absent (no forward/back fill)
    assert [d for d in days if d[0] == "2026-10-14"] == [("2026-10-14", "SU26242RMFS0")]
    # no EOD ZCYC on 14 Oct -> no curve rows for that date
    curves = files["md_curves_2026-10-14_2026-10-15.csv"][1]
    assert {r["date"] for r in curves} == {"2026-10-15"}


def test_empty_database_exports_only_manifest(conn: psycopg.Connection[DictRow]) -> None:
    export = build_tdm_export(conn, TdmExportOptions(date(2026, 10, 15)))
    assert export.files == []
    names = zipfile.ZipFile(io.BytesIO(export.to_zip())).namelist()
    assert names == ["manifest.json"]


def test_classify() -> None:
    assert classify("ofz_pk", "floating", False) == ("ОФЗ-ПК", "Гос")
    assert classify("mbs", "fixed", False) == ("Секьюритизация", "Секьюритизация")
    assert classify("sovereign_fx", "fixed", True) == ("Суверен валютный", "Гос")
    assert classify("corporate", "fixed", False) == ("Корп RUB фикс", "Корп")
    assert classify("municipal", "fixed", False) == ("Субфед/муни", "Гос")


def test_cli_export(
    conn: psycopg.Connection[DictRow],
    db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mdhub import cli
    from mdhub.cli import app

    # The CLI installs a stderr handler; keep test logging untouched.
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    seed(conn)
    runner = CliRunner()
    env = {"MDHUB_DATABASE_URL": db_url}
    res = runner.invoke(
        app, ["export", "tdm", "--date", "2026-10-15", "--out", str(tmp_path)], env=env
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "md_fx_cbr_2026-10-15.csv").exists()
    assert (tmp_path / "manifest.json").exists()
    assert "md_bond_quotes_2026-10-15.csv\tmd_bond_quotes\t3" in res.output
    res = runner.invoke(
        app,
        ["export", "tdm", "--date", "2026-10-15", "--out", str(tmp_path / "z"), "--zip"],
        env=env,
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "z" / "tdm_2026-10-15.zip").exists()
    bad = runner.invoke(
        app, ["export", "tdm", "--date", "15.10.2026", "--out", str(tmp_path)], env=env
    )
    assert bad.exit_code == 2


def test_export_reads_through_database(db_url: str, conn: psycopg.Connection[DictRow]) -> None:
    seed(conn)
    with Database(db_url).connection() as c:
        export = build_tdm_export(c, TdmExportOptions(date(2026, 10, 15)))
    assert {f.dataset for f in export.files} >= {"instruments", "md_bond_quotes", "md_fx_cbr"}


_TDM_LOAD_SCRIPT = """
import json, sys
sys.path.insert(0, sys.argv[1])
from tdm.loader import load_paths
reports = load_paths(sys.argv[3], [sys.argv[2]])
print(json.dumps(reports, ensure_ascii=False))
"""


def test_tdm_loader_accepts_export(conn: psycopg.Connection[DictRow], tmp_path: Path) -> None:
    """Run TDM's own contract-1.3 loader (stdlib only) in a subprocess on the export."""
    if not (TDM_REPO / ".git").exists() or shutil.which("git") is None:
        pytest.skip("TDM repo not available")
    src = tmp_path / "tdm_src"
    src.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(TDM_REPO), "archive", "--format=tar", TDM_COMMIT, "tdm"],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        pytest.skip("TDM contract 1.3 commit not available")
    subprocess.run(["tar", "-x", "-C", str(src)], input=archive.stdout, check=True)
    out = tmp_path / "csv"
    export = _export(conn)
    export.write_dir(out)
    script = tmp_path / "scripts" / "load.py"
    script.parent.mkdir()
    script.write_text(_TDM_LOAD_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-I", str(script), str(src), str(out), str(tmp_path / "tdm.sqlite")],
        capture_output=True,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    reports = json.loads(proc.stdout)
    by_file = {Path(r["file"]).name: r for r in reports}
    assert set(by_file) == {f.name for f in export.files}
    for f in export.files:
        rep = by_file[f.name]
        assert rep["dataset"] == f.dataset
        assert rep["errors"] == [], (f.name, rep["errors"])
        assert rep["warnings"] == [], (f.name, rep["warnings"])
        assert rep["rows"] == len(f.rows)


def test_fx_candidate_is_opt_in(conn: psycopg.Connection[DictRow]) -> None:

    names = set(_files(_export(conn)))
    assert not any(n.startswith("md_fx_moex_indicative") for n in names)
