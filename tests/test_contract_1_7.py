"""Market Snapshot Contract 1.7 (additive; market-data-hub#143): a curve of the carried set that no
source stored on the business date of a market cut is taken, flagged, from the latest earlier day
that has it. Covers the one-day gap, the multi-day gap (growing age, same source day, the age
limit that only changes the marker), the start of history (nothing to carry), the carried set
setting, the byte-for-byte unchanged snapshot of a day without a gap, the reissue of a past cut,
and the status outputs (``/v1/carried-curves``, the quality report). The interaction with
``snapshot_heal`` is in ``test_snapshot_heal.py``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

import mdhub.loaders.swaps
import mdhub.quality.job  # noqa: F401  (registers the `quality` job)
from mdhub.api.app import create_app
from mdhub.config import Settings, parse_name_list
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders.snapshots import market_params
from mdhub.snapshot.carry import (
    DEFAULT_CARRY_DATASETS,
    DEFAULT_CARRY_MAX_AGE,
    REASON_NO_QUOTE_SET,
    carried_curves,
    pending_carried_cuts,
)
from mdhub.snapshot.market import MarketParams
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION
from test_api_seed import seed
from test_snapshots import Conn, _all_snapshots_valid, _bundle, _cut_ts


@pytest.fixture
def seeded(conn: Conn) -> Conn:
    seed(conn)
    return conn


MON, TUE, WED, THU, FRI = (date(2026, 10, n) for n in (12, 13, 14, 15, 16))
NEXT_MON = date(2026, 10, 19)
ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "schemas" / "market_snapshot" / "v1" / "fixtures" / "market_eod_final.json"

# the shape of a snapshot before 1.7: nothing of 1.7 may appear on a day without a gap
ENVELOPE_1_6 = {
    "schema_version",
    "kind",
    "cut_type",
    "business_date",
    "cut_ts",
    "known_at",
    "static_snapshot_id",
    "supersedes",
    "producer",
    "parameters",
    "calendar",
    "sources",
    "datasets",
    "excluded",
    "data",
}
CURVE_ROW_1_6 = {
    "quote_set_id",
    "dataset",
    "source",
    "quote_basis",
    "fetched_at",
    "source_ts",
    "snapshot_date",
    "market_closed",
    "label",
    "content_sha256",
    "labels",
    "quotes",
}


def curve(
    conn: Conn, source: str, dataset: str, day: date, n: int, basis: str = "bid_ask_mid"
) -> int:
    """A stored quote set of ``dataset`` from ``source`` for ``day`` (fetched at 12:00 MSK)."""
    fetched = datetime.combine(day, time(12, 0), tzinfo=MSK)
    row = conn.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,"
        " content_sha256, parser_version) VALUES (%s, %s, %s, %s, %s, %s, '1') RETURNING id",
        (source, dataset, basis, fetched, day, f"{n:064x}"),
    ).fetchone()
    assert row is not None
    for pos, (tenor, bid, ask) in enumerate((("1Y", 15.1, 15.3), ("2Y", 14.6, 14.8))):
        conn.execute(
            "INSERT INTO md.curve_quote (snapshot_id, tenor, position, tenor_months, bid_pct,"
            " ask_pct, mid_pct) VALUES (%s, %s, %s, %s, %s, %s, (%s::numeric + %s) / 2)",
            (row["id"], tenor, pos, 12 * (pos + 1), bid, ask, bid, ask),
        )
    return int(row["id"])


def cut(
    db: Database,
    day: date,
    *,
    cut_type: str = "eod_preliminary",
    params: MarketParams | None = None,
    historical: bool = False,
) -> str:
    """Publish a market cut of ``day`` as known now; returns the market snapshot id."""
    ts = _cut_ts(day) if cut_type != "eod_final" else _cut_ts(day + timedelta(days=1), 8, 0)
    out = publish_market_snapshot(
        db,
        cut_type=cut_type,
        business_date=day,
        cut_ts=ts,
        known_at=utc_now(),
        params=params,
        historical=historical,
    )
    return out.market.id


def sets(doc: dict[str, Any], dataset: str) -> list[dict[str, Any]]:
    return [q for q in doc["data"]["curve_quote_sets"] if q["dataset"] == dataset]


def dataset_entry(doc: dict[str, Any]) -> dict[str, Any]:
    return next(d for d in doc["datasets"] if d["name"] == "curve_quote_sets")


def test_version_and_defaults() -> None:
    assert SCHEMA_VERSION.startswith("market_snapshot/1.")  # the 1.7 rules hold in later minors
    assert int(SCHEMA_VERSION.rsplit(".", 1)[1]) >= 7
    assert DEFAULT_CARRY_DATASETS == ("key_rate_swap", "ruonia_ois")
    assert DEFAULT_CARRY_MAX_AGE == 5
    assert MarketParams().curve_carry_datasets == DEFAULT_CARRY_DATASETS


# ------------------------------------------------------------------------------ the carry


def test_one_day_gap_carries_the_curve_flagged(seeded: Conn, db: Database) -> None:
    """The seed has ruonia_ois on Thursday only; key_rate_swap exists for Wednesday."""
    wed_id = curve(seeded, "ratescalcs", "key_rate_swap", WED, 1)
    curve(seeded, "ratescalcs", "ruonia_ois", WED, 2)
    seeded.commit()
    sid = cut(db, THU)
    doc = _bundle(seeded, sid)

    (krs,) = sets(doc, "key_rate_swap")
    assert krs["quote_set_id"] == wed_id
    assert krs["snapshot_date"] == WED.isoformat()  # the day of the data, not the business date
    assert krs["carried_forward"] == {
        "source_day": WED.isoformat(),
        "age_working_days": 1,
        "age_limit_working_days": DEFAULT_CARRY_MAX_AGE,
        "age_limit_exceeded": False,
        "reason": REASON_NO_QUOTE_SET,
    }
    assert "carried_forward" in krs["labels"]
    assert [q["tenor"] for q in krs["quotes"]] == ["1Y", "2Y"]
    # the curve of the day itself is fresh: no marker
    assert all("carried_forward" not in q for q in sets(doc, "ruonia_ois"))
    assert all("carried_forward" not in q["labels"] for q in sets(doc, "ruonia_ois"))

    assert doc["carried_curves"] == [
        {
            "dataset": "key_rate_swap",
            "source": "ratescalcs",
            "quote_set_id": wed_id,
            "source_day": WED.isoformat(),
            "age_working_days": 1,
            "age_limit_working_days": DEFAULT_CARRY_MAX_AGE,
            "age_limit_exceeded": False,
            "reason": REASON_NO_QUOTE_SET,
        }
    ]
    assert doc["parameters"]["curve_carry_datasets"] == ["key_rate_swap", "ruonia_ois"]
    assert doc["parameters"]["curve_carry_max_age_working_days"] == DEFAULT_CARRY_MAX_AGE
    entry = dataset_entry(doc)
    assert entry["coverage"]["carried_forward"] == ["key_rate_swap"]
    # as before: no source delivered it on the day; the dataset is stale and says what was done
    assert entry["coverage"]["required_without_any_source"] == ["key_rate_swap"]
    assert entry["stale"]
    reasons = entry["stale_reasons"]
    assert "key_rate_swap: no quote set from any source on 2026-10-15" in reasons
    assert "key_rate_swap: carried forward from 2026-10-14 (age 1 working day(s))" in reasons
    # the manifest (what /v1/snapshots/{id}/manifest serves) lists it too; schema-valid
    man = seeded.execute(
        "SELECT manifest FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert man is not None and man["manifest"]["carried_curves"] == doc["carried_curves"]
    assert _all_snapshots_valid(seeded) >= 2


def test_gap_of_several_days_keeps_the_source_day_and_grows_the_age(
    seeded: Conn, db: Database
) -> None:
    curve(seeded, "otc_monitor", "key_rate_swap", MON, 1)
    curve(seeded, "otc_monitor", "ruonia_ois", MON, 2)
    seeded.commit()
    # Tue..Fri have no key_rate_swap; ruonia_ois exists on Thursday only (the seed)
    ages = {}
    for day in (TUE, THU, FRI, NEXT_MON):
        doc = _bundle(seeded, cut(db, day, params=MarketParams(curve_carry_max_age=3)))
        (krs,) = sets(doc, "key_rate_swap")
        m = krs["carried_forward"]
        assert m["source_day"] == MON.isoformat() and krs["snapshot_date"] == MON.isoformat()
        ages[day] = (m["age_working_days"], m["age_limit_exceeded"])
        assert m["age_limit_working_days"] == 3
        assert krs["quotes"], "a curve over the age limit is still served"
    # working days of the calendar: the seed makes Saturday 17th a working day, Sunday none
    assert ages == {TUE: (1, False), THU: (3, False), FRI: (4, True), NEXT_MON: (6, True)}

    # Friday also lacks ruonia_ois: carried from Thursday (its own, newer source day)
    fri = _bundle(seeded, cut(db, FRI, params=MarketParams(curve_carry_max_age=3)))
    (ruonia,) = [q for q in sets(fri, "ruonia_ois") if q["quote_set_id"]]
    assert ruonia["carried_forward"]["source_day"] == THU.isoformat()
    assert ruonia["carried_forward"]["age_working_days"] == 1
    assert {c["dataset"] for c in fri["carried_curves"]} == {"key_rate_swap", "ruonia_ois"}
    exceeded = {c["dataset"]: c["age_limit_exceeded"] for c in fri["carried_curves"]}
    assert exceeded == {"key_rate_swap": True, "ruonia_ois": False}
    reasons = dataset_entry(fri)["stale_reasons"]
    assert any("(age 4 working day(s); age limit of 3 exceeded)" in r for r in reasons)


def test_every_source_of_the_curve_is_carried_with_its_own_set(seeded: Conn, db: Database) -> None:
    a = curve(seeded, "ratescalcs", "key_rate_swap", WED, 1)
    b = curve(seeded, "otc_monitor", "key_rate_swap", WED, 2)
    seeded.commit()
    doc = _bundle(seeded, cut(db, THU))
    got = {q["source"]: q["quote_set_id"] for q in sets(doc, "key_rate_swap")}
    assert got == {"ratescalcs": a, "otc_monitor": b}
    assert len(doc["carried_curves"]) == 2


def test_a_day_whose_selected_set_is_empty_is_not_the_source_day(
    seeded: Conn, db: Database
) -> None:
    """Wednesday has a non-empty set, but the per-source EOD rule selects a later EMPTY bid/ask
    set: nothing could be carried from it, so it is not the source day. The carry goes on to the
    latest day whose selected set has quotes (Tuesday) and says nothing about Wednesday."""
    tue = curve(seeded, "otc_monitor", "key_rate_swap", TUE, 1)
    curve(seeded, "otc_monitor", "key_rate_swap", WED, 2, basis="mid_only")
    seeded.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,"
        " content_sha256, parser_version) VALUES ('otc_monitor', 'key_rate_swap', 'bid_ask_mid',"
        " %s, %s, %s, '1')",
        (datetime.combine(WED, time(13, 0), tzinfo=MSK), WED, f"{3:064x}"),
    )
    seeded.commit()
    doc = _bundle(seeded, cut(db, THU))
    (krs,) = sets(doc, "key_rate_swap")
    assert krs["quote_set_id"] == tue and krs["quotes"]
    assert krs["carried_forward"]["source_day"] == TUE.isoformat()
    assert "key_rate_swap" in [c["dataset"] for c in doc["carried_curves"]]
    reasons = dataset_entry(doc)["stale_reasons"]
    assert any(f"key_rate_swap: carried forward from {TUE.isoformat()}" in r for r in reasons)
    assert not any(WED.isoformat() in r for r in reasons)
    # without the good day nothing is carried at all: no marker, no "carried forward" reason
    seeded.execute("DELETE FROM md.curve_quote WHERE snapshot_id = %s", (tue,))
    seeded.execute("DELETE FROM md.curve_snapshot WHERE id = %s", (tue,))
    seeded.commit()
    bare = _bundle(seeded, cut(db, FRI))
    assert sets(bare, "key_rate_swap") == []
    # (the seed's Thursday ruonia_ois is carried to Friday, as it should)
    assert [c["dataset"] for c in bare["carried_curves"]] == ["ruonia_ois"]
    entry = dataset_entry(bare)
    assert not any("key_rate_swap: carried forward" in r for r in entry["stale_reasons"])
    assert entry["coverage"]["carried_forward"] == ["ruonia_ois"]
    assert "key_rate_swap" in entry["coverage"]["required_without_any_source"]


def test_the_look_back_for_a_source_day_is_bounded(
    seeded: Conn,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Many stored days whose selected set is empty (a non-empty mid-only set and a later empty
    bid/ask one): only the newest ``MAX_CANDIDATE_DAYS`` are looked at, one pair of queries each,
    then the curve stays absent and the log says why. Within the bound the older good day is
    used."""
    import logging

    import mdhub.snapshot.carry as carry_mod
    import mdhub.snapshot.market as market_mod

    good = curve(seeded, "otc_monitor", "key_rate_swap", MON, 1)
    for n, day in enumerate((TUE, WED), start=2):
        curve(seeded, "otc_monitor", "key_rate_swap", day, n, basis="mid_only")
        seeded.execute(
            "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at,"
            " snapshot_date, content_sha256, parser_version) VALUES ('otc_monitor',"
            " 'key_rate_swap', 'bid_ask_mid', %s, %s, %s, '1')",
            (datetime.combine(day, time(13, 0), tzinfo=MSK), day, f"{n + 10:064x}"),
        )
    seeded.commit()
    looked: list[date] = []
    real = market_mod._day_snapshots

    def counting(conn: Any, t: datetime, d: date, eod_cutoff: str | None) -> Any:
        looked.append(d)
        return real(conn, t, d, eod_cutoff)

    monkeypatch.setattr(market_mod, "_day_snapshots", counting)

    # the bound is smaller than the distance to the good day: absent, two days looked at, a log line
    monkeypatch.setattr(carry_mod, "MAX_CANDIDATE_DAYS", 2)
    with caplog.at_level(logging.WARNING, logger="mdhub.snapshot.market"):
        doc = _bundle(seeded, cut(db, THU))
    assert sets(doc, "key_rate_swap") == []
    assert sorted(set(looked) - {THU}) == [TUE, WED] and len(looked) == 3  # THU itself + 2
    assert any("key_rate_swap" in r.getMessage() and "latest 2" in r.getMessage()
               for r in caplog.records)  # fmt: skip

    # the default bound reaches the good day, with one lookup per candidate day, no more
    monkeypatch.undo()
    monkeypatch.setattr(market_mod, "_day_snapshots", counting)
    looked.clear()
    doc = _bundle(seeded, cut(db, THU))
    (krs,) = sets(doc, "key_rate_swap")
    assert krs["quote_set_id"] == good and krs["carried_forward"]["source_day"] == MON.isoformat()
    assert sorted(set(looked) - {THU}) == [MON, TUE, WED]


def test_an_unknown_carry_dataset_stops_the_start_and_names_the_valid_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from mdhub.config import CURVE_DATASETS
    from mdhub.snapshot.market import EXPECTED_CURVES

    assert set(CURVE_DATASETS) == {ds for _src, ds in EXPECTED_CURVES}  # kept in step
    assert set(DEFAULT_CARRY_DATASETS) <= set(CURVE_DATASETS)
    monkeypatch.setenv("MDHUB_CURVE_CARRY_DATASETS", "key_rate_swap, ruonia_oiss")
    with pytest.raises(ValidationError) as exc:
        Settings()
    text = str(exc.value)
    assert "ruonia_oiss" in text and "MDHUB_CURVE_CARRY_DATASETS" in text
    assert all(name in text for name in CURVE_DATASETS)  # the valid names are listed
    monkeypatch.setenv("MDHUB_CURVE_CARRY_DATASETS", "usd_futures_yields, key_rate_swap")
    assert Settings().curve_carry_datasets == "key_rate_swap,usd_futures_yields"
    monkeypatch.setenv("MDHUB_CURVE_CARRY_DATASETS", "")
    assert parse_name_list(Settings().curve_carry_datasets) == DEFAULT_CARRY_DATASETS  # unset


def test_gap_at_the_start_of_history_carries_nothing(seeded: Conn, db: Database) -> None:
    """key_rate_swap exists on no earlier day: the curve stays absent, as before 1.7."""
    doc = _bundle(seeded, cut(db, THU))
    assert sets(doc, "key_rate_swap") == []
    assert "carried_curves" not in doc
    assert "curve_carry_datasets" not in doc["parameters"]
    entry = dataset_entry(doc)
    assert "carried_forward" not in entry["coverage"]
    assert entry["coverage"]["required_without_any_source"] == ["key_rate_swap"]
    assert entry["stale"]
    # an empty earlier quote set (no quotes) is not a curve to carry either
    row = seeded.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,"
        " content_sha256, parser_version) VALUES ('ratescalcs', 'key_rate_swap', 'mid_only',"
        " %s, %s, %s, '1') RETURNING id",
        (datetime.combine(WED, time(12, 0), tzinfo=MSK), WED, f"{9:064x}"),
    ).fetchone()
    assert row is not None
    seeded.commit()
    doc = _bundle(seeded, cut(db, THU))
    assert sets(doc, "key_rate_swap") == [] and "carried_curves" not in doc


def test_the_carried_set_and_the_age_limit_are_settings(
    seeded: Conn, db: Database, db_url: str
) -> None:
    curve(seeded, "ratescalcs", "key_rate_swap", WED, 1)
    curve(seeded, "ratescalcs", "cny_ruonia_xccy", WED, 2)
    seeded.commit()
    # a curve outside the carried set is never carried (cny_ruonia_xccy is not required)
    doc = _bundle(seeded, cut(db, THU))
    assert sets(doc, "cny_ruonia_xccy") == []
    assert [c["dataset"] for c in doc["carried_curves"]] == ["key_rate_swap"]
    # a smaller set
    only_ruonia = MarketParams(curve_carry_datasets=("ruonia_ois",))
    doc = _bundle(seeded, cut(db, THU, params=only_ruonia))
    assert sets(doc, "key_rate_swap") == [] and "carried_curves" not in doc
    # a larger one
    wider = MarketParams(curve_carry_datasets=("cny_ruonia_xccy", "key_rate_swap"))
    doc = _bundle(seeded, cut(db, THU, params=wider))
    assert {c["dataset"] for c in doc["carried_curves"]} == {"cny_ruonia_xccy", "key_rate_swap"}
    # empty = off
    doc = _bundle(seeded, cut(db, THU, params=MarketParams(curve_carry_datasets=())))
    assert "carried_curves" not in doc and sets(doc, "key_rate_swap") == []

    # the settings reach the builder parameters
    s = Settings(
        database_url=db_url,
        curve_carry_datasets=" ruonia_ois, key_rate_swap,ruonia_ois",
        curve_carry_max_age_days=2,
    )
    assert s.curve_carry_datasets == "key_rate_swap,ruonia_ois"
    assert parse_name_list(s.curve_carry_datasets) == ("key_rate_swap", "ruonia_ois")
    p = market_params(SimpleNamespace(settings=s))  # type: ignore[arg-type]
    assert p.curve_carry_datasets == ("key_rate_swap", "ruonia_ois") and p.curve_carry_max_age == 2
    assert Settings(database_url=db_url, curve_carry_datasets="").curve_carry_datasets == ""
    with pytest.raises(ValueError, match="invalid curve dataset"):
        Settings(database_url=db_url, curve_carry_datasets="Bad Name")
    assert Settings(database_url=db_url).curve_carry_max_age_days == 5


# ------------------------------------------------------------- days without a gap are unchanged


def test_a_day_without_a_gap_is_what_a_1_6_hub_published(seeded: Conn, db: Database) -> None:
    curve(seeded, "ratescalcs", "key_rate_swap", THU, 1)
    seeded.commit()
    first = cut(db, THU)
    doc = _bundle(seeded, first)
    assert set(doc) == ENVELOPE_1_6
    assert all(set(q) == CURVE_ROW_1_6 for q in doc["data"]["curve_quote_sets"])
    assert "carried_forward" not in json.dumps(doc)
    assert "curve_carry" not in json.dumps(doc["parameters"])
    entry = dataset_entry(doc)
    assert set(entry["coverage"]) == {
        "expected",
        "present",
        "missing",
        "required_datasets",
        "required_without_any_source",
        "eod_rule",
        "eod_cutoff_msk",
    }
    assert not entry["stale"]
    # with the carry switched off the same cut has the same content: nothing new to publish
    again = publish_market_snapshot(
        db,
        cut_type="eod_preliminary",
        business_date=THU,
        cut_ts=_cut_ts(THU),
        known_at=utc_now(),
        params=MarketParams(curve_carry_datasets=()),
    )
    assert again.market.status == "unchanged" and again.market.id == first


def test_golden_fixtures_carry_nothing() -> None:
    """The golden bundle (all required curves present) differs from the 1.6 one in
    ``schema_version`` (and the ids that hash it) only: no 1.7 field, no curve over the carry."""
    doc = json.loads(GOLDEN.read_text())
    assert doc["schema_version"] == SCHEMA_VERSION
    assert "carried_curves" not in doc and "carried_forward" not in GOLDEN.read_text()
    assert set(doc) == ENVELOPE_1_6


# ------------------------------------------------------------------------------ reissue


def test_a_past_cut_is_reissued_when_the_real_data_arrives(
    seeded: Conn, db: Database, settings: Settings, today_is: Any
) -> None:
    today_is(date(2026, 10, 20))
    curve(seeded, "ratescalcs", "key_rate_swap", WED, 1)
    seeded.commit()
    old_id = cut(db, THU, cut_type="eod_final", historical=True)
    old = _bundle(seeded, old_id)
    assert [c["dataset"] for c in old["carried_curves"]] == ["key_rate_swap"]
    assert carried_curves(seeded, since=THU) != []
    assert pending_carried_cuts(seeded, MON, FRI) == []  # nothing newer is stored: not outdated

    # the same inputs: a rebuild publishes nothing (a legitimately carried cut stays as it is)
    out = run_job("snapshot_rebuild", {"day": THU.isoformat()}, settings=settings, db=db)
    (res,) = out.result.values()
    assert res["market"]["status"] == "unchanged" and res["market"]["id"] == old_id

    # the real curve of Thursday is imported later
    real = curve(seeded, "ratescalcs", "key_rate_swap", THU, 2)
    seeded.commit()
    assert pending_carried_cuts(seeded, MON, FRI) != []  # now the cut is outdated
    out = run_job("snapshot_rebuild", {"day": THU.isoformat()}, settings=settings, db=db)
    (res,) = out.result.values()
    assert res["market"]["status"] == "published"
    new = _bundle(seeded, res["market"]["id"])
    assert new["supersedes"] == old_id  # a correction of the same cut; the old one stays readable
    assert new["cut_ts"] == old["cut_ts"]
    (krs,) = sets(new, "key_rate_swap")
    assert krs["quote_set_id"] == real and "carried_forward" not in krs
    assert "carried_curves" not in new and "curve_carry_datasets" not in new["parameters"]
    assert not dataset_entry(new)["coverage"].get("carried_forward")
    assert _bundle(seeded, old_id)["carried_curves"]  # unchanged history
    # the reissued cut is current: nothing is carried any more, nothing is pending
    assert carried_curves(seeded, since=THU) == [] and pending_carried_cuts(seeded, MON, FRI) == []
    assert _all_snapshots_valid(seeded) >= 3


def test_a_newer_source_day_arriving_late_also_outdates_the_cut(seeded: Conn, db: Database) -> None:
    curve(seeded, "ratescalcs", "key_rate_swap", MON, 1)
    seeded.commit()
    cut(db, THU, cut_type="eod_final", historical=True)  # carried from Monday, age 3
    curve(seeded, "ratescalcs", "key_rate_swap", WED, 2)  # Wednesday is imported afterwards
    seeded.commit()
    assert pending_carried_cuts(seeded, MON, FRI) == [(THU, _cut_ts(FRI, 8, 0))]
    # data after the business date does not concern the cut
    other = curve(seeded, "ratescalcs", "ruonia_ois", NEXT_MON, 3)
    assert other
    assert pending_carried_cuts(seeded, MON, FRI) == [(THU, _cut_ts(FRI, 8, 0))]


# ------------------------------------------------------------------------------ status


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="tok-carry")
    )
    with TestClient(app) as c:
        yield c


def test_status_lists_the_curves_carried_now(
    seeded: Conn, db: Database, settings: Settings, client: TestClient, today_is: Any
) -> None:
    today_is(date(2026, 10, 16))
    curve(seeded, "ratescalcs", "key_rate_swap", MON, 1)
    seeded.commit()
    sid = cut(db, THU)  # eod_preliminary, key_rate_swap carried, age 3
    auth = {"Authorization": "Bearer tok-carry"}
    r = client.get("/v1/carried-curves?days=400", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    (row,) = body["data"]
    assert row["snapshot_id"] == sid and row["cut_type"] == "eod_preliminary"
    assert row["business_date"] == THU.isoformat() and row["dataset"] == "key_rate_swap"
    assert row["source"] == "ratescalcs" and row["source_day"] == MON.isoformat()
    assert row["age_working_days"] == 3 and row["age_limit_exceeded"] is False
    assert row["reason"] == REASON_NO_QUOTE_SET
    assert client.get("/v1/carried-curves?cut_type=eod_final", headers=auth).json()["data"] == []
    assert client.get("/v1/carried-curves?days=0", headers=auth).json()["data"] == []

    out = run_job(
        "quality",
        {"as_of": FRI.isoformat(), "datasets": ["curves_ruonia_ois"], "check_jobs": False},
        settings=settings,
        db=db,
    )
    assert out.status == "succeeded", out
    assert out.result["summary"]["carried_curves"] == [f"key_rate_swap from {MON}"]
    assert [c["dataset"] for c in out.result["carried_curves"]] == ["key_rate_swap"]
    stored = seeded.execute("SELECT report FROM ops.quality_report").fetchone()
    assert stored is not None and stored["report"]["summary"]["carried_curves"]

    # the real data arrives and the cut is corrected: nothing is carried any more
    curve(seeded, "ratescalcs", "key_rate_swap", THU, 2)
    seeded.commit()
    cut(db, THU)
    assert client.get("/v1/carried-curves?days=400", headers=auth).json()["data"] == []
