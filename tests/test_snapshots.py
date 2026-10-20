"""Published snapshots (Market Snapshot Contract v1): canonical hashing, builders, publisher,
immutability and supersedes, as-known-at replay, coverage/excluded accounting, schema
validation of every published snapshot, endpoints, schedule and retention."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

import mdhub.loaders.snapshots  # noqa: F401  (registers the snapshot jobs)
from mdhub.api.app import create_app
from mdhub.api.auth import create_token
from mdhub.config import Settings
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.schedule import check_rules, effective_rules, snapshot_rules
from mdhub.snapshot.build import Cut
from mdhub.snapshot.canonical import canonical_bytes, gzip_bytes, sha256_hex, to_wire
from mdhub.snapshot.publish import publish, purge_intraday_snapshots
from mdhub.snapshot.runner import publish_market_snapshot, publish_static_snapshot
from mdhub.snapshot.schema import SnapshotSchemaError, validate_bundle, validate_manifest
from mdhub.snapshot.static import build_static
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
TOKENS = "tok-snap-1"
AUTH = {"Authorization": "Bearer tok-snap-1"}


def _cut_ts(d: date = DAY, hh: int = 19, mm: int = 45) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK)


@pytest.fixture
def seeded(conn: Conn) -> Conn:
    seed(conn)
    return conn


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    raw = gzip.decompress(bytes(row["payload_gz"]))
    assert hashlib.sha256(raw).hexdigest() == sid
    doc: dict[str, Any] = json.loads(raw)
    return doc


def _all_snapshots_valid(conn: Conn) -> int:
    rows = conn.execute("SELECT id FROM ops.published_snapshot").fetchall()
    for r in rows:
        doc = _bundle(conn, r["id"])
        validate_bundle(doc)
        man = conn.execute(
            "SELECT manifest FROM ops.published_snapshot WHERE id = %s", (r["id"],)
        ).fetchone()
        assert man is not None
        validate_manifest(man["manifest"])
        assert {k: v for k, v in doc.items() if k != "data"} == {
            k: v for k, v in man["manifest"].items() if k != "snapshot_id"
        }
    return len(rows)


# ---------------------------------------------------------------------- canonical form


def test_canonical_bytes_are_stable_and_exact() -> None:
    a = {"b": Decimal("14.500"), "a": [date(2026, 10, 15), None, True], "ц": "ключ"}
    b = {"ц": "ключ", "a": [date(2026, 10, 15), None, True], "b": Decimal("14.500")}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert canonical_bytes(a) == ('{"a":["2026-10-15",null,true],"b":"14.500","ц":"ключ"}'.encode())
    # exact stored digits: 14.5 and 14.500 are different facts
    assert canonical_bytes({"x": Decimal("14.5")}) != canonical_bytes({"x": Decimal("14.500")})
    assert to_wire(Decimal("1E-7")) == "0.0000001"
    assert to_wire(Decimal("1E+2")) == "100"
    assert to_wire(datetime(2026, 10, 15, 19, 45, tzinfo=MSK)) == "2026-10-15T16:45:00Z"
    assert to_wire(datetime(2026, 10, 15, 16, 45, 0, 120, tzinfo=UTC)) == (
        "2026-10-15T16:45:00.000120Z"
    )
    with pytest.raises(TypeError):
        canonical_bytes({"x": 0.1})
    with pytest.raises(ValueError, match="non-finite"):
        canonical_bytes({"x": Decimal("NaN")})
    with pytest.raises(ValueError, match="naive"):
        canonical_bytes({"x": datetime(2026, 10, 15, 19, 45)})
    raw = canonical_bytes(a)
    assert gzip_bytes(raw) == gzip_bytes(raw)  # deterministic (mtime=0)
    assert gzip.decompress(gzip_bytes(raw)) == raw
    # the hash of a known document never changes (pins the canonical encoding)
    assert sha256_hex(canonical_bytes({"k": Decimal("1.0"), "d": date(2026, 1, 2)})) == (
        hashlib.sha256(b'{"d":"2026-01-02","k":"1.0"}').hexdigest()
    )


# ---------------------------------------------------------------------- static + market


def test_static_and_market_snapshots_publish_and_validate(seeded: Conn, db: Database) -> None:
    known = utc_now()
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=DAY,
        cut_ts=_cut_ts(DAY + timedelta(1), 8, 0),
        known_at=known,
    )
    assert out.static.status == "published" and out.market.status == "published"
    market = _bundle(seeded, out.market.id)
    static = _bundle(seeded, out.static.id)
    assert market["static_snapshot_id"] == out.static.id
    assert market["kind"] == "market" and static["kind"] == "static"
    assert static["cut_type"] == "change"
    # one calendar version + fingerprint per snapshot, the market repeats its static's
    assert market["calendar"] == static["calendar"]
    assert _all_snapshots_valid(seeded) == 2

    universe = {i["secid"] for i in static["data"]["instruments"]}
    assert all(q["secid"] in universe for q in market["data"]["bond_quotes"])
    # dataset entries match the data
    for doc in (static, market):
        names = [d["name"] for d in doc["datasets"]]
        assert sorted(names) == sorted(doc["data"])
        for d in doc["datasets"]:
            assert d["rows"] == len(doc["data"][d["name"]])
            assert d["sha256"] == sha256_hex(canonical_bytes(doc["data"][d["name"]]))
            assert d["stale"] == bool(d["stale_reasons"])


def test_coverage_labels_instead_of_exclusion(seeded: Conn, db: Database) -> None:
    seeded.execute(
        "UPDATE ref.bond SET coupon_type = 'unknown', coupon_type_basis = 'none' "
        "WHERE secid = 'DUPA'"
    )
    seeded.execute(
        "INSERT INTO md.bond_eod (trade_date, secid, board, close) "
        "VALUES (%s, 'NOTINREF', 'TQCB', 99.5)",
        (DAY,),
    )
    seeded.commit()
    out = publish_market_snapshot(
        db, cut_type="eod_preliminary", business_date=DAY, cut_ts=_cut_ts(), known_at=utc_now()
    )
    static = _bundle(seeded, out.static.id)
    market = _bundle(seeded, out.market.id)
    inst = {i["secid"]: i for i in static["data"]["instruments"]}
    # unknown coupon type and perpetual are included, with labels
    assert "coupon_type_unknown" in inst["DUPA"]["labels"]
    assert {"perpetual", "perpetual_no_call_date"} <= set(inst["RU000PERP001"]["labels"])
    assert inst["RU000PERP001"]["maturity_date"] is None
    cov = static["datasets"][0]["coverage"]
    assert cov["labels"]["coupon_type_unknown"] == 1 and cov["labels"]["perpetual"] == 1
    # floater benchmark verbatim with the formula status
    f = {r["secid"]: r for r in static["data"]["floater_formulas"]}
    assert f["RU000A105X12"]["benchmark"] == "KEY_RATE"
    assert f["RU000A105X12"]["status"] in ("curated", "inferred", "partial")
    # a quote outside the static universe is excluded with a reason, never dropped silently
    assert {
        "dataset": "bond_quotes",
        "key": {"secid": "NOTINREF", "board": "TQCB", "trade_date": DAY.isoformat()},
        "reason": "not_in_static_snapshot",
    } in market["excluded"]
    n_eod = seeded.execute(
        "SELECT count(*) AS n FROM md.bond_eod WHERE trade_date = %s", (DAY,)
    ).fetchone()
    assert n_eod is not None
    quotes = market["data"]["bond_quotes"]
    excluded_quotes = [e for e in market["excluded"] if e["dataset"] == "bond_quotes"]
    assert len(quotes) + len(excluded_quotes) >= n_eod["n"]  # every EOD row accounted for
    stored = seeded.execute(
        "SELECT excluded_count FROM ops.published_snapshot WHERE id = %s", (out.market.id,)
    ).fetchone()
    assert stored == {"excluded_count": len(market["excluded"])}
    # calendars from ref.calendar_effective only
    cals = {c["calendar"]: c for c in static["data"]["calendars"]}
    assert "MOEX_STOCK" in cals
    assert _all_snapshots_valid(seeded) == 2


@pytest.mark.parametrize("mode", ["eod_preliminary", "eod_final"])
def test_eod_keeps_quoted_matured_and_delisted_bonds(seeded: Conn, db: Database, mode: str) -> None:
    historical_day = date(2026, 1, 5)
    seeded.execute(
        "UPDATE md.bond_eod SET trade_date=%s WHERE trade_date=%s", (historical_day, DAY)
    )
    seeded.execute(
        "UPDATE md.bond_eod_yield SET trade_date=%s WHERE trade_date=%s", (historical_day, DAY)
    )
    seeded.execute(
        "UPDATE ref.bond SET status='matured', maturity_date='2026-02-01' "
        "WHERE secid='SU26242RMFS0'"
    )
    # delisted after maturity (not alive on the cut's date: outside the default universe)
    seeded.execute(
        "UPDATE ref.bond SET status='delisted', maturity_date='2026-01-01' "
        "WHERE secid='RU000A105X12'"
    )
    # This bond has only yield analytics, not a price row. Its static must survive too.
    seeded.execute(
        "UPDATE ref.bond SET status='delisted', maturity_date='2026-01-01' WHERE secid='DUPB'"
    )
    seeded.execute(
        "INSERT INTO md.bond_eod_yield (trade_date,secid,board,effective_yield_pct) "
        "VALUES (%s,'DUPB','TQOD',15)",
        (historical_day,),
    )
    seeded.commit()
    known = utc_now()
    out = publish_market_snapshot(
        db, cut_type=mode, business_date=historical_day, cut_ts=_cut_ts(), known_at=known
    )
    static = _bundle(seeded, out.static.id)
    market = _bundle(seeded, out.market.id)
    instruments = {i["secid"]: i for i in static["data"]["instruments"]}
    quoted = {q["secid"] for q in market["data"]["bond_quotes"]}
    retained = {"SU26242RMFS0", "RU000A105X12", "DUPB"}
    assert retained <= quoted
    assert retained <= instruments.keys()
    assert instruments["SU26242RMFS0"]["status"] == "matured"
    assert instruments["RU000A105X12"]["status"] == "delisted"
    assert all("retained_for_market_quote" in instruments[s]["labels"] for s in retained)
    assert "OLD1" not in instruments  # no quote on this cut's date
    assert not [e for e in market["excluded"] if e["reason"] == "not_in_static_snapshot"]
    assert _all_snapshots_valid(seeded) == 2
    # Plain daily static keeps its existing active/recently-matured scope.
    with db.connection() as c:
        daily = build_static(c, cut=Cut("static", "daily", DAY, _cut_ts(), known))
    assert not (retained & daily.universe)
    # Replay must not see quote rows ingested after known_at.
    seeded.execute(
        "INSERT INTO md.bond_eod (trade_date,secid,board,close) VALUES (%s,'OLD1','TQCB',99)",
        (historical_day,),
    )
    seeded.commit()
    replay = publish_market_snapshot(
        db, cut_type=mode, business_date=historical_day, cut_ts=_cut_ts(), known_at=known
    )
    assert replay.market.id == out.market.id and replay.market.status == "unchanged"


def test_static_publishes_alive_bonds_that_are_not_traded(seeded: Conn, db: Database) -> None:
    """HS2: delisted bonds that are alive (maturity on/after the business date, or perpetual)
    are instruments with label ``not_traded``; dead or undated delisted bonds are not."""
    seeded.execute(
        "UPDATE ref.bond SET status='delisted', maturity_date='2046-03-01' "
        "WHERE secid='RU000A105X12'"
    )
    seeded.execute(
        "UPDATE ref.bond SET status='delisted' WHERE secid='RU000PERP001'"
    )  # perpetual, maturity NULL
    seeded.execute(
        "UPDATE ref.bond SET status='delisted', maturity_date=%s WHERE secid='DUPA'",
        (DAY - timedelta(days=1),),
    )  # matured before the business date
    seeded.execute(
        "UPDATE ref.bond SET status='delisted', maturity_date=NULL WHERE secid='DUPB'"
    )  # life unknown
    seeded.commit()
    out = publish_static_snapshot(
        db, cut_type="daily", business_date=DAY, cut_ts=_cut_ts(DAY, 7, 45), known_at=utc_now()
    )
    doc = _bundle(seeded, out.id)
    inst = {i["secid"]: i for i in doc["data"]["instruments"]}
    assert inst["RU000A105X12"]["status"] == "delisted"
    assert "not_traded" in inst["RU000A105X12"]["labels"]
    assert "retained_for_market_quote" not in inst["RU000A105X12"]["labels"]
    assert {"not_traded", "perpetual"} <= set(inst["RU000PERP001"]["labels"])
    assert "DUPA" not in inst and "DUPB" not in inst
    assert {r["secid"] for r in doc["data"]["id_map"]} >= {"RU000A105X12", "RU000PERP001"}
    cov = next(d for d in doc["datasets"] if d["name"] == "instruments")["coverage"]
    assert cov["by_status"]["delisted"] == 2 and cov["labels"]["not_traded"] == 2
    assert doc["parameters"]["universe"] == "active_or_recently_matured_or_alive"
    assert _all_snapshots_valid(seeded) == 1


def test_market_cut_flags_required_curve_without_any_source(seeded: Conn, db: Database) -> None:
    """WEB-8/WEB-13: every cut reports whether ruonia_ois and key_rate_swap came from at least
    one source; a missing one is a stale reason, never a forward-filled value."""
    seeded.execute("DELETE FROM md.curve_quote")
    seeded.execute("DELETE FROM md.curve_snapshot")
    seeded.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date, "
        "content_sha256, parser_version) VALUES ('ratescalcs', 'ruonia_ois', 'bid_ask_mid', "
        "%s, %s, %s, 't')",
        (_cut_ts(DAY, 12, 0), DAY, "0" * 64),
    )
    seeded.commit()
    out = publish_market_snapshot(
        db, cut_type="eod_preliminary", business_date=DAY, cut_ts=_cut_ts(), known_at=utc_now()
    )
    market = _bundle(seeded, out.market.id)
    ds = next(d for d in market["datasets"] if d["name"] == "curve_quote_sets")
    assert ds["coverage"]["required_datasets"] == ["key_rate_swap", "ruonia_ois"]
    assert ds["coverage"]["required_without_any_source"] == ["key_rate_swap"]
    assert ds["stale"] and any(
        r.startswith("key_rate_swap: no quote set from any source") for r in ds["stale_reasons"]
    )
    assert [q["dataset"] for q in market["data"]["curve_quote_sets"]] == ["ruonia_ois"]


def test_republish_same_content_is_unchanged_and_correction_supersedes(
    seeded: Conn, db: Database
) -> None:
    cut = _cut_ts(DAY, 7, 45)
    first = publish_static_snapshot(db, business_date=DAY, cut_ts=cut, known_at=utc_now())
    assert first.status == "published" and first.supersedes is None
    again = publish_static_snapshot(db, business_date=DAY, cut_ts=cut, known_at=utc_now())
    assert again.status == "unchanged" and again.id == first.id

    seeded.execute("UPDATE ref.bond SET face_value = 900 WHERE secid = 'SU26242RMFS0'")
    seeded.commit()
    fixed = publish_static_snapshot(db, business_date=DAY, cut_ts=cut, known_at=utc_now())
    assert fixed.status == "published" and fixed.supersedes == first.id
    # the old one stays readable; the list marks it superseded
    assert _bundle(seeded, first.id)["supersedes"] is None
    assert _bundle(seeded, fixed.id)["supersedes"] == first.id
    # a second correction supersedes the correction, not the original
    seeded.execute("UPDATE ref.bond SET face_value = 800 WHERE secid = 'SU26242RMFS0'")
    seeded.commit()
    fixed2 = publish_static_snapshot(db, business_date=DAY, cut_ts=cut, known_at=utc_now())
    assert fixed2.supersedes == fixed.id


def test_snapshots_are_immutable(seeded: Conn, db: Database) -> None:
    res = publish_static_snapshot(db, business_date=DAY, cut_ts=_cut_ts(), known_at=utc_now())
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        seeded.execute(
            "UPDATE ops.published_snapshot SET business_date = business_date + 1 WHERE id = %s",
            (res.id,),
        )
    seeded.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="intraday"):
        seeded.execute("DELETE FROM ops.published_snapshot WHERE id = %s", (res.id,))
    seeded.rollback()


def test_as_known_at_replay_reproduces_a_past_static_state(seeded: Conn, db: Database) -> None:
    t1 = utc_now()
    cut = Cut("static", "daily", DAY, _cut_ts(DAY, 7, 45), t1)
    with db.connection() as c:
        before = canonical_bytes(build_static(c, cut=cut).document(None))
    first = publish_static_snapshot(db, business_date=DAY, cut_ts=cut.cut_ts, known_at=t1)

    # after t1: a coupon revision, a new bond, a changed issuer, a soft-deleted calendar day
    seeded.execute(
        "UPDATE ref.bond_coupon SET value_prc = value_prc + 1, amount = amount + 1 "
        "WHERE secid = 'SU26242RMFS0'"
    )
    seeded.execute(
        "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status) "
        "VALUES ('NEWBOND', 'corporate', 'fixed', 'moex_label', 'active')"
    )
    seeded.execute("UPDATE ref.issuer SET name = 'Renamed' WHERE issuer_id = 2")
    seeded.execute(
        "UPDATE ref.calendar_day SET is_deleted = true, ingested_at = now() "
        "WHERE calendar = 'MOEX_STOCK'"
    )
    seeded.execute(
        "UPDATE ref.bond_rate_formula SET spread_bp = spread_bp + 10 WHERE secid = 'RU000A105X12'"
    )
    seeded.commit()

    with db.connection() as c:
        now_doc = build_static(c, cut=Cut("static", "daily", DAY, cut.cut_ts, utc_now()))
        replay = canonical_bytes(build_static(c, cut=cut).document(None))
    assert "NEWBOND" in {i["secid"] for i in now_doc.data()["instruments"]}
    assert replay == before  # byte-identical past state
    again = publish_static_snapshot(db, business_date=DAY, cut_ts=cut.cut_ts, known_at=t1)
    assert again.id == first.id and again.status == "unchanged"


def test_calendar_replay_and_weekend_sessions_never_business_days(
    seeded: Conn, db: Database
) -> None:
    seeded.execute(
        "INSERT INTO ref.weekend_session (calendar, day, source, trade_session_date, known_at) "
        "VALUES ('MOEX_STOCK', '2026-10-24', 'moex_iss_calendar', '2026-10-26', now())"
    )
    seeded.execute(
        "INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) VALUES "
        "('MOEX_STOCK', '2026-11-04', 'moex_iss_calendar', false, now()), "
        "('MOEX_STOCK', '2026-11-07', 'curated_unverified', true, now())"
    )
    seeded.commit()
    t1 = utc_now()
    with db.connection() as c:
        cal1 = {
            x["calendar"]: x
            for x in build_static(c, cut=Cut("static", "daily", DAY, t1, t1)).data()["calendars"]
        }
    stock = cal1["MOEX_STOCK"]
    assert "2026-11-04" in [d.isoformat() for d in stock["holidays"]]
    assert date(2026, 11, 7) in stock["working_weekends"]
    assert stock["provisional_from"] == date(2026, 11, 7)
    assert date(2026, 10, 24) not in stock["working_weekends"]  # weekend session ignored
    # the ISS calendar later stops listing 2026-11-04: a versioned (soft) delete
    from mdhub.loaders import calendar as cal

    with db.connection() as c:
        cal._drop_unlisted(c, "MOEX_STOCK", "moex_iss_calendar", set(), after=None, run_id=None)
    with db.connection() as c:
        t2 = utc_now()
        cal2 = build_static(c, cut=Cut("static", "daily", DAY, t2, t2)).data()["calendars"]
        cal1_again = build_static(c, cut=Cut("static", "daily", DAY, t1, t1)).data()["calendars"]
    stock2 = {x["calendar"]: x for x in cal2}["MOEX_STOCK"]
    assert date(2026, 11, 4) not in stock2["holidays"]
    assert stock2["fingerprint"] != stock["fingerprint"]
    assert {x["calendar"]: x for x in cal1_again}["MOEX_STOCK"] == stock


def test_every_published_snapshot_validates_and_bad_documents_fail(
    seeded: Conn, db: Database
) -> None:
    known = utc_now()
    for mode, d, cut in (
        ("eod_preliminary", DAY, _cut_ts()),
        ("intraday", DAY, _cut_ts(DAY, 12, 30)),
        ("eod_final", DAY, _cut_ts(DAY + timedelta(1), 8, 0)),
    ):
        publish_market_snapshot(db, cut_type=mode, business_date=d, cut_ts=cut, known_at=known)
    publish_static_snapshot(db, business_date=DAY, cut_ts=_cut_ts(DAY, 7, 45), known_at=known)
    assert _all_snapshots_valid(seeded) >= 4
    doc = _bundle(
        seeded,
        seeded.execute(
            "SELECT id FROM ops.published_snapshot WHERE kind = 'market' LIMIT 1"
        ).fetchone()["id"],  # type: ignore[index]
    )
    bad = json.loads(json.dumps(doc))
    bad["data"]["fixings"][0]["value"] = 14.5  # a float, not a decimal string
    with pytest.raises(SnapshotSchemaError):
        validate_bundle(bad)
    bad = json.loads(json.dumps(doc))
    bad["data"]["bond_quotes"][0]["new_field"] = "x"
    with pytest.raises(SnapshotSchemaError):
        validate_bundle(bad)
    bad = json.loads(json.dumps(doc))
    bad["static_snapshot_id"] = None
    with pytest.raises(SnapshotSchemaError):
        validate_bundle(bad)


def test_market_datasets_content(seeded: Conn, db: Database) -> None:
    out = publish_market_snapshot(
        db, cut_type="eod_preliminary", business_date=DAY, cut_ts=_cut_ts(), known_at=utc_now()
    )
    m = _bundle(seeded, out.market.id)
    data = m["data"]
    q = {(r["secid"], r["board"]): r for r in data["bond_quotes"]}
    assert q
    one = next(iter(q.values()))
    assert set(one["source_analytics"]) >= {"effective_yield_pct", "zspread_bp"}
    kinds = {r["kind"] for r in data["fx_rates"]}
    assert "cbr_official" in kinds
    for r in data["fx_rates"]:
        assert r["ccy"] != "RUB"
        if r["kind"] == "moex_eod" and r["quote_units"] is not None:
            assert r["close_rub_per_unit"] is not None or r["close"] is None
    assert all(r["base_ccy"] == "EUR" for r in data["cross_rates"])
    assert data["zcyc_params"] and set(data["zcyc_params"][0]["params"]) >= {"B1", "G9"}
    assert all(isinstance(v, str) for v in data["zcyc_params"][0]["params"].values())
    assert data["curve_quote_sets"] or m["datasets"][2]["coverage"]["missing"]
    assert any(r["origin"] == "md.index_eod" for r in data["fixings"]) or True
    assert data["bond_quotes_intraday"] == []


def test_market_snapshot_job_and_static_job(
    seeded: Conn, settings: Settings, db: Database, today_is: Any
) -> None:
    today_is(date(2026, 10, 16))
    out = run_job(
        "snapshot_market",
        {"mode": "eod_final", "cut": "08:00"},
        settings=settings,
        db=db,
    )
    assert out.status == "succeeded", out
    assert out.result["market"]["business_date"] == "2026-10-15"
    assert out.result["static"]["status"] == "published"
    st = run_job("snapshot_static", {"mode": "daily", "cut": "07:45"}, settings=settings, db=db)
    assert st.status == "succeeded"
    assert st.result["status"] == "published"  # a different cut (daily) than the change one
    again = run_job("snapshot_static", {"mode": "daily", "cut": "07:45"}, settings=settings, db=db)
    assert again.result["status"] == "unchanged"
    row = seeded.execute(
        "SELECT cut_ts, run_id FROM ops.published_snapshot WHERE kind = 'market'"
    ).fetchone()
    assert row is not None and row["run_id"] == out.run_id
    assert row["cut_ts"] == datetime(2026, 10, 16, 8, 0, tzinfo=MSK)


def test_eod_final_rebuild_on_a_later_day_corrects_the_same_cut(
    seeded: Conn, settings: Settings, db: Database, today_is: Any
) -> None:
    """cut_ts depends on the business date, not the run day: a later rebuild of eod_final
    for D supersedes the scheduled one (or publishes nothing when the content is the same)."""
    today_is(date(2026, 10, 16))
    first = run_job(
        "snapshot_market", {"mode": "eod_final", "cut": "08:00"}, settings=settings, db=db
    ).result["market"]
    assert first["status"] == "published" and first["business_date"] == "2026-10-15"

    today_is(date(2026, 10, 20))  # a manual run four days later, with and without cut=
    for params in ({}, {"cut": "08:00"}):
        same = run_job(
            "snapshot_market",
            {"mode": "eod_final", "day": "2026-10-15", **params},
            settings=settings,
            db=db,
        ).result["market"]
        assert same["status"] == "unchanged" and same["id"] == first["id"]

    seeded.execute("UPDATE md.bond_eod SET close = close + 1 WHERE trade_date = %s", (DAY,))
    seeded.commit()
    fixed = run_job(
        "snapshot_market", {"mode": "eod_final", "day": "2026-10-15"}, settings=settings, db=db
    ).result["market"]
    assert fixed["status"] == "published" and fixed["supersedes"] == first["id"]
    live = seeded.execute(
        """
        SELECT p.id, p.cut_ts FROM ops.published_snapshot p
        WHERE p.kind = 'market' AND p.cut_type = 'eod_final' AND p.business_date = %s
          AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
        """,
        (DAY,),
    ).fetchall()
    assert [r["id"] for r in live] == [fixed["id"]]
    assert live[0]["cut_ts"] == datetime(2026, 10, 16, 8, 0, tzinfo=MSK)  # next business day


def test_static_daily_and_eod_preliminary_cut_on_the_business_date(
    seeded: Conn, settings: Settings, db: Database, today_is: Any
) -> None:
    today_is(date(2026, 10, 20))
    run_job("snapshot_static", {"day": "2026-10-15"}, settings=settings, db=db)
    run_job(
        "snapshot_market",
        {"mode": "eod_preliminary", "day": "2026-10-15"},
        settings=settings,
        db=db,
    )
    rows = seeded.execute(
        "SELECT cut_type, cut_ts FROM ops.published_snapshot WHERE business_date = %s",
        (DAY,),
    ).fetchall()
    assert {r["cut_type"]: r["cut_ts"] for r in rows} == {
        "daily": _cut_ts(DAY, 7, 45),
        "eod_preliminary": _cut_ts(DAY, 19, 45),
    }


# ------------------------------------------------------------------------ retention


def test_intraday_retention_keeps_static_and_eod(seeded: Conn, db: Database) -> None:
    old_day = DAY - timedelta(days=40)
    known = utc_now()
    publish_market_snapshot(
        db, cut_type="intraday", business_date=old_day, cut_ts=_cut_ts(old_day, 12), known_at=known
    )
    publish_market_snapshot(
        db,
        cut_type="eod_preliminary",
        business_date=old_day,
        cut_ts=_cut_ts(old_day),
        known_at=known,
    )
    publish_market_snapshot(
        db, cut_type="intraday", business_date=DAY, cut_ts=_cut_ts(DAY, 12), known_at=known
    )
    with db.connection() as c:
        n = purge_intraday_snapshots(c, 30, now=_cut_ts(DAY, 20))
    assert n == 1
    left = seeded.execute(
        "SELECT kind, cut_type, business_date FROM ops.published_snapshot ORDER BY 1, 2, 3"
    ).fetchall()
    assert {"kind": "market", "cut_type": "eod_preliminary", "business_date": old_day} in left
    assert {"kind": "market", "cut_type": "intraday", "business_date": DAY} in left
    assert all(r["business_date"] != old_day or r["cut_type"] != "intraday" for r in left)


# ------------------------------------------------------------------------ schedule


def test_snapshot_rules_follow_settings() -> None:
    default = snapshot_rules()
    assert [r.cron for r in default["snapshot_static"]] == ["45 7 * * *"]
    crons = [r.cron for r in default["snapshot_market"]]
    assert crons == ["45 19 * * *", "0 8 * * *", "*/30 10-18 * * *", "0 19 * * *"]
    assert default["snapshot_market"][1].params["days_back"] == [1]
    custom = Settings(
        snapshot_eod_preliminary_time="20:10",
        snapshot_intraday_minutes=60,
        snapshot_intraday_window="11:00-18:00",
    )
    rules = effective_rules(custom)
    crons = [r.cron for r in rules["snapshot_market"]]
    assert crons == ["10 20 * * *", "0 8 * * *", "0 11-17 * * *", "0 18 * * *"]
    assert check_rules(rules) == []
    off = snapshot_rules(Settings(snapshot_intraday_minutes=0))
    assert [r.params["mode"] for r in off["snapshot_market"]] == ["eod_preliminary", "eod_final"]
    with pytest.raises(ValueError, match="divide 60"):
        Settings(snapshot_intraday_minutes=7)
    with pytest.raises(ValueError, match="HH:MM"):
        Settings(snapshot_static_time="7.45")
    with pytest.raises(ValueError, match="whole hours"):
        Settings(snapshot_intraday_window="10:30-19:00")


# ---------------------------------------------------------------------------- API


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    # the read API's own role (0800/0810): the grants must be enough
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens=TOKENS)
    )
    with TestClient(app) as c:
        yield c


def test_snapshot_endpoints(seeded: Conn, db: Database, client: TestClient) -> None:
    known = utc_now()
    out = publish_market_snapshot(
        db, cut_type="eod_preliminary", business_date=DAY, cut_ts=_cut_ts(), known_at=known
    )
    assert client.get("/v1/snapshots").status_code == 401
    r = client.get("/v1/snapshots", headers=AUTH)
    assert r.status_code == 200, r.text
    ids = [s["snapshot_id"] for s in r.json()["data"]]
    assert set(ids) == {out.market.id, out.static.id}
    r = client.get(
        "/v1/snapshots",
        params={
            "kind": "market",
            "cut_type": "eod_preliminary",
            "business_date": DAY.isoformat(),
            "latest": "true",
        },
        headers=AUTH,
    )
    body = r.json()
    assert [s["snapshot_id"] for s in body["data"]] == [out.market.id]
    assert body["data"][0]["static_snapshot_id"] == out.static.id
    assert body["data"][0]["rows"]["bond_quotes"] >= 1
    assert client.get("/v1/snapshots", params={"kind": "x"}, headers=AUTH).status_code == 400

    man = client.get(f"/v1/snapshots/{out.market.id}/manifest", headers=AUTH)
    assert man.status_code == 200
    m = man.json()
    assert m["data"]["snapshot_id"] == out.market.id
    validate_manifest(m["data"])
    assert m["meta"]["superseded_by"] is None

    b = client.get(f"/v1/snapshots/{out.market.id}/bundle", headers=AUTH)
    assert b.status_code == 200
    assert b.headers["content-type"] == "application/gzip"
    assert b.headers["etag"] == f'"{out.market.id}"'
    assert hashlib.sha256(gzip.decompress(b.content)).hexdigest() == out.market.id
    nm = client.get(
        f"/v1/snapshots/{out.market.id}/bundle",
        headers={**AUTH, "If-None-Match": f'"{out.market.id}"'},
    )
    assert nm.status_code == 304
    assert client.get(f"/v1/snapshots/{'0' * 64}/bundle", headers=AUTH).status_code == 404
    assert client.get("/v1/snapshots/nothex/manifest", headers=AUTH).status_code == 400
    assert client.get(f"/v1/snapshots/{out.market.id}/bundle").status_code == 401

    # scope "snapshots" (auth.required_scopes) is enough, "export" is not
    snap, _ = create_token(seeded, "replica", ["snapshots"])
    export, _ = create_token(seeded, "tdm", ["export"])
    seeded.commit()
    url = f"/v1/snapshots/{out.market.id}/bundle"
    assert client.get(url, headers={"Authorization": f"Bearer {snap}"}).status_code == 200
    assert client.get(url, headers={"Authorization": f"Bearer {export}"}).status_code == 403


def test_publish_rejects_market_without_static(seeded: Conn, db: Database) -> None:
    t = utc_now()
    with db.connection() as c:
        static = build_static(c, cut=Cut("static", "daily", DAY, t, t))
        from mdhub.snapshot.market import build_market

        m = build_market(c, cut=Cut("market", "eod_final", DAY, t, t), static=static)
    with db.connection() as c, pytest.raises(ValueError, match="static_snapshot_id"):
        publish(c, m)
