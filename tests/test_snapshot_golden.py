"""Golden fixtures of the Market Snapshot Contract v1 (``schemas/market_snapshot/v1/fixtures``).

* every fixture validates against the schema and its file name / index carries the sha256 of
  its canonical form (the snapshot id);
* the fixtures are reproduced exactly by the hub's builders from the public source rows in
  ``tests/fixtures/snapshot/source_rows.json.gz`` (``MDHUB_REGEN_GOLDEN=1`` rewrites them).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

import pytest

from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database
from mdhub.snapshot.build import Cut
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex
from mdhub.snapshot.market import MarketParams, build_market
from mdhub.snapshot.schema import validate_bundle, validate_manifest
from mdhub.snapshot.static import build_static
from snapshot_golden import (
    DAY,
    GOLDEN_DIR,
    GOLDEN_FIXING_LOOKBACK_DAYS,
    GOLDEN_SECIDS,
    known_at_of,
    load,
    load_source,
)

INDEX = GOLDEN_DIR / "index.json"


def _index() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(INDEX.read_text(encoding="utf-8"))
    return data


def _fixtures() -> dict[str, dict[str, Any]]:
    return {
        name: json.loads((GOLDEN_DIR / entry["file"]).read_text(encoding="utf-8"))
        for name, entry in _index()["fixtures"].items()
    }


def _build(db: Database) -> dict[str, dict[str, Any]]:
    source = load_source()
    with db.connection() as conn:
        load(conn, source)
    known_at = known_at_of(source)
    next_day = DAY + timedelta(days=1)
    final_cut = datetime(next_day.year, next_day.month, next_day.day, 8, 0, tzinfo=MSK)
    static_cut = Cut("static", "daily", DAY, datetime(2026, 10, 15, 7, 45, tzinfo=MSK), known_at)
    with db.connection() as conn:
        static = build_static(conn, cut=static_cut)
        static_doc = static.document(None)
        static_id = sha256_hex(canonical_bytes(static_doc))
        market = build_market(
            conn,
            cut=Cut("market", "eod_final", DAY, final_cut, known_at),
            static=static,
            static_snapshot_id=static_id,
            params=MarketParams(fixing_lookback_days=GOLDEN_FIXING_LOOKBACK_DAYS),
        )
        market_doc = market.document(None)
    return {"static_daily": static_doc, "market_eod_final": market_doc}


def test_golden_fixtures_validate_and_ids_match() -> None:
    index = _index()
    fixtures = _fixtures()
    assert set(fixtures) == {"static_daily", "market_eod_final"}
    for name, doc in fixtures.items():
        validate_bundle(doc)
        sid = sha256_hex(canonical_bytes(doc))
        assert index["fixtures"][name]["snapshot_id"] == sid, name
        manifest = {k: v for k, v in doc.items() if k != "data"} | {"snapshot_id": sid}
        validate_manifest(manifest)
    static, market = fixtures["static_daily"], fixtures["market_eod_final"]
    assert market["static_snapshot_id"] == index["fixtures"]["static_daily"]["snapshot_id"]
    assert {i["secid"] for i in static["data"]["instruments"]} == set(GOLDEN_SECIDS)
    # the coverage the fixtures must demonstrate
    inst = {i["secid"]: i for i in static["data"]["instruments"]}
    types = {i["coupon_type"] for i in inst.values()}
    assert {"fixed", "floating", "indexed_face", "unknown"} <= types
    assert any(i["is_perpetual"] and "perpetual" in i["labels"] for i in inst.values())
    assert any(i["is_amortizing"] for i in inst.values())
    assert any(o["type"] == "put" and o["status"] == "scheduled" for o in static["data"]["offers"])
    assert {f["benchmark"] for f in static["data"]["floater_formulas"]} >= {"KEY_RATE", "RUONIA"}
    assert {r["kind"] for r in static["data"]["redemptions"]} == {"amortization", "maturity"}
    assert market["data"]["bond_quotes"] and market["data"]["curve_quote_sets"]
    assert market["data"]["zcyc_params"][0]["is_eod"] is True


GOLDEN_PRODUCER_VERSION = "0.1.0"


def test_golden_fixtures_are_reproduced_by_the_builders(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # producer.version is in the bundle (so in the id) and now comes from the git tags
    # (hatch-vcs): pin the version the fixtures were built with, keeping their ids.
    monkeypatch.setattr("mdhub.snapshot.build.__version__", GOLDEN_PRODUCER_VERSION)
    built = _build(db)
    if os.environ.get("MDHUB_REGEN_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        entries = {}
        for name, doc in built.items():
            validate_bundle(doc)
            file = f"{name}.json"
            text = json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
            (GOLDEN_DIR / file).write_text(text, encoding="utf-8")
            entries[name] = {
                "file": file,
                "snapshot_id": sha256_hex(canonical_bytes(doc)),
                "kind": doc["kind"],
                "cut_type": doc["cut_type"],
                "business_date": doc["business_date"],
            }
        INDEX.write_text(
            json.dumps(
                {"schema_version": built["static_daily"]["schema_version"],
                 "instruments": GOLDEN_SECIDS, "fixtures": entries},
                indent=1, ensure_ascii=False, sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )  # fmt: skip
        pytest.skip("golden fixtures regenerated")
    fixtures = _fixtures()
    for name, doc in built.items():
        assert canonical_bytes(doc) == canonical_bytes(fixtures[name]), (
            f"{name} differs from the golden fixture: regenerate it in a reviewed change "
            "(MDHUB_REGEN_GOLDEN=1) and bump the schema version if the contract changed"
        )
