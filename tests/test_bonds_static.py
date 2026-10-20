"""Bond static (MOEX ISS): parsers on recorded fixtures, classification, schedules, paging.

Fixtures in ``tests/fixtures/bonds_static`` are real ISS responses recorded on 2026-10-16,
reduced to these SECIDs:

==============  ==========================================================================
SU26238RMFS4    OFZ-PD
SU29010RMFS4    OFZ-PK (RUONIA; MOEX forward-fills the current coupon into future periods)
SU29025RMFS2    OFZ-PK
SU52002RMFS1    OFZ-IN (indexed face)
RU000A10A356    key-rate floater, current label 'Облигация с плавающим купоном'
RU000A0JR4U9    amortizing bond
RU000A0JS4Z7    put offer (2027-08-26) and a '0000-00-00' offer; labelled fixed with known
                coupons, but 9 of 11 future coupons unpublished (-> variable_known)
RU000A0JXTS9    RUS-27: USD face, RUB settlement (FACEUNIT != CURRENCYID)
RU000A0JWMZ1    perpetual (Бессрочные)
RU000A0NNH09    EQRP structured note, qualified investors only (not in the collection)
RU000A0JVY04    fixed coupon with unknown future coupons
RU000A10DQB6    CNY OFZ (sovereign FX)
==============  ==========================================================================
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL, IssFormatError, IssPagingError
from mdhub.loaders import bonds_static as bs
from mdhub.sources import moex_static as ms

FIX = Path(__file__).parent / "fixtures" / "bonds_static"
TODAY = date(2026, 10, 16)
SAMPLES = sorted(p.stem for p in (FIX / "bondization").glob("*.json"))
CURSOR_COLS = ["INDEX", "TOTAL", "PAGESIZE"]


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def http_client() -> HttpClient:
    return HttpClient(
        base_url=ISS_BASE_URL,
        limiter=RateLimiter(default_rps=None),
        rate_limits={"iss.moex.com": 100_000.0},
        sleep=lambda _s: None,
        max_retries=0,
    )


# ------------------------------------------------------------------ fixture handlers


def bulk_bondization_payloads(
    secids: list[str] | None = None, mutate: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, dict[str, Any]]:
    """Concatenate per-SECID bondization fixtures into bulk blocks (MOEX order: by date)."""
    blocks: dict[str, dict[str, Any]] = {}
    for secid in secids or SAMPLES:
        payload = load(f"bondization/{secid}.json")
        if mutate is not None:
            mutate(payload)
        for name in ms.BONDIZATION_BLOCKS:
            b = payload[name]
            target = blocks.setdefault(name, {"columns": b["columns"], "data": []})
            assert target["columns"] == b["columns"]
            target["data"].extend(b["data"])
    date_col = {"coupons": "coupondate", "amortizations": "amortdate", "offers": "offerdate"}
    for name, b in blocks.items():
        i = b["columns"].index(date_col[name])
        s = b["columns"].index("secid")
        b["data"].sort(key=lambda r, i=i, s=s: (str(r[i]), str(r[s])))
    return blocks


DATE_COL = {"coupons": "coupondate", "amortizations": "amortdate", "offers": "offerdate"}


def bulk_handler(
    blocks: dict[str, dict[str, Any]],
    *,
    pagesize: int = 100,
    total_shift_at: int | None = None,
    grand_shift: int = 0,
) -> Callable[[httpx.Request], httpx.Response]:
    """ISS bulk bondization: ``iss.only`` blocks, ``from``/``till`` (inclusive) windows,
    ``start`` paging. ``total_shift_at``: windowed coupon TOTAL grows by one from that start
    on; ``grand_shift``: the unfiltered coupon TOTAL is off by that many rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = int(params.get("start", "0"))
        only = [b for b in ms.BONDIZATION_BLOCKS if b in params.get("iss.only", "").split(",")]
        lo, hi = params.get("from"), params.get("till")
        body: dict[str, Any] = {}
        for name in only or ms.BONDIZATION_BLOCKS:
            cols = blocks[name]["columns"]
            i = cols.index(DATE_COL[name])
            rows = [r for r in blocks[name]["data"] if lo is None or (lo <= str(r[i]) <= str(hi))]
            total = len(rows)
            shift = total_shift_at is not None and start >= total_shift_at
            if name == "coupons" and lo is not None and shift:
                total += 1
            if name == "coupons" and lo is None:
                total += grand_shift
            body[name] = {"columns": cols, "data": rows[start : start + pagesize]}
            body[f"{name}.cursor"] = {"columns": CURSOR_COLS, "data": [[start, total, pagesize]]}
        return httpx.Response(200, json=body)

    return handler


def paged_until_empty(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        if request.url.params.get("q"):
            q = request.url.params["q"]
            sec = payload["securities"]
            rows = [r for r in sec["data"] if r[0] == q]
            return httpx.Response(
                200, json={"securities": {"columns": sec["columns"], "data": rows}}
            )
        sec = payload["securities"]
        data = sec["data"] if start == 0 else []
        return httpx.Response(200, json={"securities": {"columns": sec["columns"], "data": data}})

    return handler


def collection_handler(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        sec = payload["securities"]
        rows = sec["data"][start : start + 1000]
        return httpx.Response(
            200,
            json={
                "securities": {"columns": sec["columns"], "data": rows},
                "securities.cursor": {
                    "columns": CURSOR_COLS,
                    "data": [[start, len(sec["data"]), 1000]],
                },
            },
        )

    return handler


def description_handler(request: httpx.Request) -> httpx.Response:
    secid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
    path = FIX / "description" / f"{secid}.json"
    if not path.exists():
        return httpx.Response(404, text="not found")
    return httpx.Response(200, json=json.loads(path.read_text("utf-8")))


def bondization_one_handler(request: httpx.Request) -> httpx.Response:
    secid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
    return httpx.Response(200, json=load(f"bondization/{secid}.json"))


def mock_iss(
    router: respx.Router,
    *,
    directory: dict[str, Any] | None = None,
    collection: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    blocks: dict[str, dict[str, Any]] | None = None,
) -> None:
    base = ISS_BASE_URL
    router.get(f"{base}/securities.json").mock(
        side_effect=paged_until_empty(directory or load("directory.json"))
    )
    router.get(
        f"{base}/securitygroups/stock_bonds/collections/stock_bonds_all/securities.json"
    ).mock(side_effect=collection_handler(collection or load("collection.json")))
    router.get(f"{base}/engines/stock/markets/bonds/securities.json").mock(
        return_value=httpx.Response(200, json=market or load("market.json"))
    )
    router.get(url__regex=rf"^{base}/securities/[^/]+\.json").mock(side_effect=description_handler)
    router.get(f"{base}/statistics/engines/stock/markets/bonds/bondization.json").mock(
        side_effect=bulk_handler(blocks or bulk_bondization_payloads())
    )
    router.get(url__regex=rf"^{base}/statistics/engines/stock/markets/bonds/bondization/").mock(
        side_effect=bondization_one_handler
    )


# ------------------------------------------------------------------------- universe


def fixture_universe() -> bs.Universe:
    """Universe as the full job builds it from the fixtures (no HTTP, no DB)."""
    u = bs.Universe()
    for row in ms.parse_directory_payload(load("directory.json")):
        u.directory[row["secid"]] = row
    coll = load("collection.json")["securities"]
    for row in ms.parse_collection_rows(
        dict(zip(coll["columns"], r, strict=True)) for r in coll["data"]
    ):
        u.collection[row["SECID"]] = row
    market = ms.parse_market(load("market.json"))
    u.market_trade_date = market.trade_date
    for row in market.rows:
        u.market.setdefault(row["SECID"], []).append(row)
        u.market_boards.add(row["BOARDID"])
    for secid in ("RU000A0NNH09",):
        u.descriptions[secid] = ms.parse_description(load(f"description/{secid}.json"), secid)
        u.description_fresh[secid] = (1, "0" * 64)
    for secid in SAMPLES:
        u.bondization.extend(
            ms.parse_bondization_one(load(f"bondization/{secid}.json"), secid, capture_id=None)
        )
        u.schedule_secids.add(secid)
    return u


def bond(u: bs.Universe, secid: str) -> tuple[dict[str, Any], bs.BondFacts]:
    return bs.build_bond_row(secid, u, TODAY, bs._schedule_summary(u, secid), run_id=1)


# ------------------------------------------------------------------------- parsers


def test_parse_directory_collection_market() -> None:
    d = ms.parse_directory_payload(load("directory.json"))
    assert {r["secid"] for r in d} == set(SAMPLES)
    ofz = next(r for r in d if r["secid"] == "SU26238RMFS4")
    assert ofz["emitent_inn"] == ms.MINFIN_INN and ofz["is_traded"] is True
    assert ofz["primary_boardid"] == "TQOB"

    coll = load("collection.json")["securities"]
    rows = ms.parse_collection_rows(
        dict(zip(coll["columns"], r, strict=True)) for r in coll["data"]
    )
    by = {r["SECID"]: r for r in rows}
    assert "RU000A0NNH09" not in by  # EQRP notes are not in the collection
    fl = by["RU000A10A356"]
    assert fl["BOND_TYPE"] == "Облигация с плавающим купоном"
    assert fl["COUPON_BENCHMARK"] == "RREFKEYR"
    assert fl["COUPON_BENCHMARK_SPREAD"] == Decimal("1.95")
    assert fl["STARTDATEMOEX"] is not None and isinstance(fl["MATDATE"], date)

    market = ms.parse_market(load("market.json"))
    assert market.trade_date == TODAY
    rus = [r for r in market.rows if r["SECID"] == "RU000A0JXTS9"]
    assert rus and rus[0]["FACEUNIT"] == "USD" and rus[0]["CURRENCYID"] == "RUB"
    pk = next(r for r in market.rows if r["SECID"] == "SU29010RMFS4")
    assert pk["LOTSIZE"] == 1 and pk["MINSTEP"] == Decimal("0.001") and pk["DECIMALS"] == 4


def test_parse_market_format_drift() -> None:
    payload = load("market.json")
    payload["securities"]["columns"] = [
        c if c != "LOTSIZE" else "LOT_SIZE" for c in payload["securities"]["columns"]
    ]
    with pytest.raises(IssFormatError, match="LOTSIZE"):
        ms.parse_market(payload)


def test_parse_description_eqrp_note() -> None:
    desc = ms.parse_description(load("description/RU000A0NNH09.json"), "RU000A0NNH09")
    assert desc.fields["STRUCTBOND"] is True
    assert desc.fields["ISQUALIFIEDINVESTORS"] is True
    assert desc.fields["MATDATE"] == date(2028, 7, 11)
    assert desc.fields["EMITTER_ID"] == 33
    primary = [b for b in desc.boards if b["is_primary"]]
    assert [b["board"] for b in primary] == ["EQRP"]
    assert {b["market"] for b in desc.boards} >= {"ccp", "ndm"}
    with pytest.raises(IssFormatError):
        ms.parse_description(load("description/RU000A0NNH09.json"), "SU26238RMFS4")


def test_parse_bondization_one_checks_totals() -> None:
    payload = load("bondization/SU29010RMFS4.json")
    data = ms.parse_bondization_one(payload, "SU29010RMFS4")
    assert len(data.coupons["SU29010RMFS4"]) == 40
    broken = copy.deepcopy(payload)
    broken["coupons"]["data"].pop()
    with pytest.raises(IssPagingError):
        ms.parse_bondization_one(broken, "SU29010RMFS4")


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("secid", "bond_class", "coupon_type"),
    [
        ("SU26238RMFS4", "ofz_pd", "fixed"),
        ("SU29010RMFS4", "ofz_pk", "floating"),
        ("SU29025RMFS2", "ofz_pk", "floating"),
        ("SU52002RMFS1", "ofz_in", "indexed_face"),
        ("RU000A10A356", "corporate", "floating"),
        ("RU000A0JS4Z7", "corporate", "variable_known"),
        ("RU000A0JXTS9", "sovereign_fx", "fixed"),
        ("RU000A10DQB6", "sovereign_fx", "fixed"),
        ("RU000A0JWMZ1", "corporate", "fixed"),
        ("RU000A0NNH09", "structured", "fixed"),
        ("RU000A0JVY04", "corporate", "variable_known"),
    ],
)
def test_classification_current_vocabulary(secid: str, bond_class: str, coupon_type: str) -> None:
    row, _ = bond(fixture_universe(), secid)
    assert (row["bond_class"], row["coupon_type"]) == (bond_class, coupon_type)


def test_bond_fields_typed() -> None:
    u = fixture_universe()
    rus, _ = bond(u, "RU000A0JXTS9")
    assert rus["face_unit"] == "USD" and rus["currency_id"] == "RUB" and rus["is_fx_face"] is True
    perp, _ = bond(u, "RU000A0JWMZ1")
    assert perp["is_perpetual"] is True and perp["maturity_date"] is None
    assert perp["status"] == "active"
    note, _ = bond(u, "RU000A0NNH09")
    assert note["qualified_only"] is True and note["is_structured"] is True
    assert note["primary_board"] == "EQRP" and note["description_source"] == "security"
    assert note["issuer_id"] == 33 and note["maturity_date"] == date(2028, 7, 11)
    am, _ = bond(u, "RU000A0JR4U9")
    assert am["is_amortizing"] is True and am["initial_face_value"] == Decimal(1000)
    linker, facts = bond(u, "SU52002RMFS1")
    assert linker["is_face_indexed"] is True and facts.face_indexed
    fl, facts = bond(u, "RU000A10A356")
    assert facts.benchmark == "KEY_RATE" and fl["coupon_benchmark_spread_pct"] == Decimal("1.95")
    assert fl["start_date_moex"] is not None and fl["listing_level"] is not None


def test_classify_legacy_floater_label_and_unlabelled() -> None:
    legacy = ms.ClassifyInput(secid="RU000X", sec_type="corporate_bond", bond_type="Флоатер")
    assert ms.classify_coupon_type(legacy, "corporate") == ("floating", "moex_label")
    # an active bond without any MOEX label is never classified from its (forward-filled) schedule
    bare = ms.ClassifyInput(secid="RU000Y", sec_type="corporate_bond", coupon_values=(True, True))
    assert ms.classify_coupon_type(bare, "corporate") == ("unknown", "none")
    matured = ms.ClassifyInput(
        secid="RU000Z", sec_type="corporate_bond", is_matured=True, coupon_values=(True, True)
    )
    assert ms.classify_coupon_type(matured, "corporate") == ("fixed", "schedule")


PAID = (date(2026, 4, 15), Decimal("85.48"), Decimal("17"))
"""(pay_date, amount, rate) of a paid coupon (before TODAY)."""


def fixed_label_bond(
    coupons: list[tuple[date, Decimal | None, Decimal | None]], **kw: Any
) -> ms.ClassifyInput:
    """Synthetic active corporate labelled fixed with known coupons; coupons in schedule order."""
    base: dict[str, Any] = {
        "secid": "RU000TEST01",
        "sec_type": "corporate_bond",
        "coupon_details": ms.DETAILS_FIXED_KNOWN,
        "has_description": True,
        "is_active": True,
        "today": TODAY,
    }
    base.update(kw)
    return ms.ClassifyInput(
        coupon_values=tuple(any(v is not None and v != 0 for v in (a, r)) for _, a, r in coupons),
        coupon_rates=tuple(ms.CouponRate(None, pay, r) for pay, _, r in coupons),
        **base,
    )


def test_fixed_label_with_unknown_future_coupons_is_variable_known() -> None:
    unknown = (date(2027, 4, 15), None, None)
    inp = fixed_label_bond([PAID, unknown])
    assert ms.unknown_future_coupons(inp) == (2,)
    assert ms.classify_coupon_type(inp, "corporate") == ("variable_known", "schedule")
    # the same through BOND_TYPE alone; MOEX's 0 counts as missing
    by_type = fixed_label_bond(
        [PAID, (date(2027, 4, 15), Decimal(0), Decimal(0))],
        coupon_details=None,
        bond_type="Облигация с фиксированным (известным) купоном",
    )
    assert ms.fixed_known_label(by_type) == "Облигация с фиксированным (известным) купоном"
    assert ms.classify_coupon_type(by_type, "corporate") == ("variable_known", "schedule")
    # a structured note labelled fixed is no exception
    note = fixed_label_bond([PAID, unknown], struct_flag=True)
    assert ms.classify_coupon_type(note, "structured") == ("variable_known", "schedule")


@pytest.mark.parametrize(
    ("case", "inp"),
    [
        # fixed-rate pool: rate published, amount depends on the face outstanding
        ("rate_without_amount", fixed_label_bond([PAID, (date(2027, 4, 15), None, Decimal(9))])),
        (
            "all_future_known",
            fixed_label_bond([PAID, (date(2027, 4, 15), Decimal("85.48"), None)]),
        ),
        ("only_paid_unknown", fixed_label_bond([(date(2026, 4, 15), None, None)])),
        ("pays_today", fixed_label_bond([PAID, (TODAY, None, None)])),
        ("delisted", fixed_label_bond([PAID, (date(2027, 4, 15), None, None)], is_active=False)),
        ("no_today", fixed_label_bond([PAID, (date(2027, 4, 15), None, None)], today=None)),
    ],
)
def test_fixed_label_kept(case: str, inp: ms.ClassifyInput) -> None:
    assert ms.classify_coupon_type(inp, "corporate") == ("fixed", "moex_label"), case


def amortizer(rates: list[Decimal | None], **kw: Any) -> ms.ClassifyInput:
    """Synthetic new active amortizer (BOND_TYPE only, no coupon labels), monthly coupons."""
    pays = [date(2026, 11, 15) + timedelta(days=30 * i) for i in range(len(rates))]
    base: dict[str, Any] = {
        "secid": "RU000A10GBM8",
        "sec_type": "corporate_bond",
        "bond_type": "Амортизируемая облигация",
        "has_description": True,
        "is_active": True,
        "today": TODAY,
        "coupon_values": tuple(r is not None for r in rates),
        "coupon_rates": tuple(ms.CouponRate(None, p, r) for p, r in zip(pays, rates, strict=True)),
    }
    base.update(kw)
    return ms.ClassifyInput(**base)


def test_hs6_new_amortizer_with_constant_rate_is_fixed() -> None:
    """HS6: a new amortizer (no paid coupon yet, so schedule_constant cannot apply) with one
    positive rate over its whole schedule is fixed, basis amortizer_constant_rate."""
    inp = amortizer([Decimal("16.4")] * 12)
    assert ms.classify_coupon_type(inp, "corporate") == ("fixed", "amortizer_constant_rate")
    plural = amortizer([Decimal("16.4")] * 3, bond_type="Амортизируемые облигации")
    assert ms.classify_coupon_type(plural, "corporate") == ("fixed", "amortizer_constant_rate")


@pytest.mark.parametrize(
    ("case", "inp", "expected"),
    [
        # any benchmark evidence wins: never a floater classed fixed
        ("benchmark", amortizer([Decimal(17)] * 3, coupon_benchmark="RREFKEYR"), "floating"),
        ("details", amortizer([Decimal(17)] * 3, coupon_details="Ключевая ставка"), "floating"),
        ("formula", amortizer([Decimal(17)] * 3, has_formula=True), "unknown"),
        ("rates_differ", amortizer([Decimal(17), Decimal(16)]), "unknown"),
        ("blank_future", amortizer([Decimal(17), None]), "variable_known"),
        ("zero_rate", amortizer([Decimal(0)] * 2), "unknown"),
        ("not_amortizer", amortizer([Decimal(17)] * 3, bond_type=None), "unknown"),
    ],
)
def test_hs6_amortizer_rule_guards(case: str, inp: ms.ClassifyInput, expected: str) -> None:
    assert ms.classify_coupon_type(inp, "corporate")[0] == expected, case
    note = amortizer([Decimal(17)] * 3)
    assert ms.classify_coupon_type(note, "structured") == ("unknown", "schedule")


def test_unknown_future_coupons_needs_aligned_schedule() -> None:
    inp = fixed_label_bond([PAID, (date(2027, 4, 15), None, None)])
    misaligned = ms.ClassifyInput(
        secid=inp.secid, today=TODAY, coupon_values=(True,), coupon_rates=inp.coupon_rates
    )
    assert ms.unknown_future_coupons(misaligned) == ()


def test_fixed_label_conflict_row() -> None:
    row, facts = bond(fixture_universe(), "RU000A0JS4Z7")
    assert (row["coupon_type"], row["coupon_type_basis"]) == ("variable_known", "schedule")
    assert row["status"] == "active"
    assert facts.coupon_type_conflict == {
        "secid": "RU000A0JS4Z7",
        "field": "coupon_type",
        "source_a": "moex_schedule",
        "source_b": "moex_label",
        "value_a": "variable_known",
        "value_b": "fixed (Фиксированный с известными купонами)",
        "selected_source": "moex_schedule",
        "reason": "active bond: 9 of 11 future coupons without amount or rate"
        " (first #32 paying 2028-02-22)",
        "resolved": False,
        "run_id": 1,
        "capture_id": row["capture_id"],
    }
    # labels agreeing with the schedule raise no conflict
    _, ofz = bond(fixture_universe(), "SU26238RMFS4")
    assert ofz.coupon_type_conflict is None


def test_fixed_label_kept_for_delisted_bond() -> None:
    u = fixture_universe()
    u.market.pop("RU000A0JS4Z7")
    u.directory["RU000A0JS4Z7"]["is_traded"] = False
    u.collection["RU000A0JS4Z7"]["IS_TRADED"] = 0
    row, facts = bond(u, "RU000A0JS4Z7")
    assert row["status"] == "delisted"
    assert (row["coupon_type"], row["coupon_type_basis"]) == ("fixed", "moex_label")
    assert facts.coupon_type_conflict is None


# ---------------------------------------------------------------------- schedules


def coupon_rows(u: bs.Universe, secid: str) -> list[dict[str, Any]]:
    _, facts = bond(u, secid)
    coupons, _, _, _ = bs._schedule_rows(secid, u, facts, TODAY, run_id=1)
    return coupons


def test_floater_future_coupons_not_known() -> None:
    u = fixture_universe()
    rows = coupon_rows(u, "SU29010RMFS4")
    assert [r["coupon_no"] for r in rows] == list(range(1, len(rows) + 1))
    past = [r for r in rows if r["pay_date"] <= TODAY]
    future = [r for r in rows if r["pay_date"] > TODAY]
    assert past and all(r["status"] == "known" for r in past)
    # MOEX copies the current rate into every future period: never "known"
    assert future and all(r["status"] == "projected_by_moex" for r in future)
    assert {r["amount"] for r in future} == {Decimal("82.72")}

    key_rate = coupon_rows(u, "RU000A10A356")
    assert all(
        r["status"] == "unknown" and r["amount"] is None for r in key_rate if r["pay_date"] > TODAY
    )
    assert all(r["status"] == "known" for r in key_rate if r["pay_date"] <= TODAY)


def test_fixed_and_linker_coupon_status() -> None:
    u = fixture_universe()
    pd = coupon_rows(u, "SU26238RMFS4")
    assert all(r["status"] == "known" for r in pd)
    linker = coupon_rows(u, "SU52002RMFS1")
    assert all(r["status"] == "projected_by_moex" for r in linker if r["pay_date"] > TODAY)
    unknown_fix = coupon_rows(u, "RU000A0JVY04")
    statuses = {r["status"] for r in unknown_fix if r["pay_date"] > TODAY}
    assert statuses == {"known", "unknown"}


def test_fx_future_value_rub_dropped() -> None:
    u = fixture_universe()
    rows = coupon_rows(u, "RU000A0JXTS9")
    assert all(r["value_rub"] is None for r in rows if r["pay_date"] > TODAY)
    assert all(r["value_rub"] is not None for r in rows if r["pay_date"] <= TODAY)


def test_coupon_numbering_stable_under_record_date_change() -> None:
    rows = ms.parse_bondization_one(load("bondization/SU26238RMFS4.json"), "SU26238RMFS4")
    coupons = rows.coupons["SU26238RMFS4"]
    first = ms.number_coupons(coupons)
    moved = [
        ms.CouponRow(**{**{f: getattr(c, f) for f in c.__slots__}, "record_date": date(2000, 1, 1)})
        if i == 5
        else c
        for i, c in enumerate(coupons)
    ]
    second = ms.number_coupons(list(reversed(moved)))
    assert [(n, r.pay_date) for n, r in first] == [(n, r.pay_date) for n, r in second]


def test_offers_normalized() -> None:
    u = fixture_universe()
    _, facts = bond(u, "RU000A0JS4Z7")
    _, _, offers, _ = bs._schedule_rows("RU000A0JS4Z7", u, facts, TODAY, run_id=1)
    by_key = {o["offer_key"]: o for o in offers}
    undated = by_key["undated-1"]
    assert undated["offer_date"] is None and undated["date_missing"] is True
    assert undated["status"] == "undated" and undated["is_offer_or_redemption"] is True
    nxt = by_key["2027-08-26"]
    assert (nxt["offer_type"], nxt["offer_type_basis"], nxt["status"]) == (
        "put",
        "put_option_date",
        "scheduled",
    )
    done = by_key["2015-09-10"]
    assert done["status"] == "completed" and done["offer_type"] == "put"
    assert done["offer_type_basis"] == "exercise_window"
    assert by_key["2015-03-12"]["offer_type_basis"] == "bond_subtype"


def test_settle_lag_and_formula() -> None:
    u = fixture_universe()
    m, _, obs = bs._board_rows("SU29010RMFS4", u, "TQOB", _Ctx(), run_id=1)
    assert m[0]["settle_lag_days"] == 1 and m[0]["settle_code"] == "T1"
    assert obs[0][2] == date(2026, 10, 19)
    row, facts = bond(u, "RU000A10A356")
    f = bs._formula_row(row, facts, run_id=1)
    assert f is not None
    assert (f["benchmark"], f["spread_bp"], f["status"]) == (
        "KEY_RATE",
        Decimal("195.00"),
        "inferred",
    )
    assert f["lag_days"] is None and f["averaging"] is None and f["floor_pct"] is None
    pd_row, pd_facts = bond(u, "SU26238RMFS4")
    assert bs._formula_row(pd_row, pd_facts, run_id=1) is None


class _Ctx:
    """Minimal stand-in for JobContext in pure builder tests."""

    def is_business_day(self, day: date, calendar: str = "MOEX_STOCK") -> bool:
        return day.weekday() < 5


# ---------------------------------------------------------------------- bulk paging


@respx.mock
def test_bulk_bondization_paging_complete() -> None:
    blocks = bulk_bondization_payloads()
    respx.get(f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization.json").mock(
        side_effect=bulk_handler(blocks, pagesize=20)
    )
    with http_client() as http:
        pages = list(ms.iter_bondization_pages(http, split_rows=10))
    for name in ms.BONDIZATION_BLOCKS:
        got = sorted(json.dumps(r, sort_keys=True) for p in pages for r in p.rows.get(name, []))
        cols = blocks[name]["columns"]
        want = sorted(
            json.dumps(dict(zip(cols, r, strict=True)), sort_keys=True)
            for r in blocks[name]["data"]
        )
        assert got == want, name
    # undated offers are covered by the unfiltered offers paging
    assert any(r["offerdate"] == "0000-00-00" for p in pages for r in p.rows.get("offers", []))
    # windows were split below split_rows; discovery pages carry no rows
    assert any(not p.rows for p in pages[1:])


@respx.mock
def test_bulk_bondization_total_change_fails_closed() -> None:
    blocks = bulk_bondization_payloads()
    respx.get(f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization.json").mock(
        side_effect=bulk_handler(blocks, pagesize=20, total_shift_at=20)
    )
    with http_client() as http, pytest.raises(IssPagingError, match="TOTAL changed"):
        list(ms.iter_bondization_pages(http))
    respx.get(f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization.json").mock(
        side_effect=bulk_handler(blocks, grand_shift=1)
    )
    with http_client() as http, pytest.raises(IssPagingError, match="windows hold"):
        list(ms.iter_bondization_pages(http))


@respx.mock
def test_bulk_bondization_short_page_retried_then_fails() -> None:
    blocks = bulk_bondization_payloads()
    good = bulk_handler(blocks, pagesize=20)
    shortened: set[str] = set()

    def flaky(request: httpx.Request) -> httpx.Response:
        resp = good(request)
        key = str(request.url)
        windowed = "from" in request.url.params
        if request.url.params.get("start") == "20" and windowed and key not in shortened:
            shortened.add(key)
            body = json.loads(resp.content)
            body["coupons"]["data"] = body["coupons"]["data"][:-1]
            return httpx.Response(200, json=body)
        return resp

    route = respx.get(f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization.json")
    route.mock(side_effect=flaky)
    with http_client() as http:
        pages = list(ms.iter_bondization_pages(http))
    assert shortened
    assert sum(len(p.rows.get("coupons", [])) for p in pages) == len(blocks["coupons"]["data"])

    def always_short(request: httpx.Request) -> httpx.Response:
        body = json.loads(good(request).content)
        if request.url.params.get("start") == "20" and "from" in request.url.params:
            body["coupons"]["data"] = body["coupons"]["data"][:-1]
        return httpx.Response(200, json=body)

    route.mock(side_effect=always_short)
    with http_client() as http, pytest.raises(IssPagingError):
        list(ms.iter_bondization_pages(http))


# ======================================================================== DB tests

import psycopg  # noqa: E402
from psycopg.rows import DictRow  # noqa: E402

from mdhub.config import Settings  # noqa: E402
from mdhub.db.pool import Database  # noqa: E402
from mdhub.jobs import JobOutcome, run_job  # noqa: E402

Conn = psycopg.Connection[DictRow]


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


@pytest.fixture
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: TODAY)


def run_full(settings: Settings, db: Database, **params: Any) -> JobOutcome:
    return run_job(
        "bonds_static",
        {"mode": "full", **params},
        settings=settings,
        db=db,
        http_factory=_http_factory,
    )


def run_row(conn: Conn, run_id: int | None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ops.job_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    return dict(row)


def history_count(conn: Conn, table: str | None = None) -> int:
    if table is None:
        row = conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone()
    else:
        row = conn.execute(
            "SELECT count(*) AS n FROM ops.row_history WHERE table_name = %s", (table,)
        ).fetchone()
    assert row is not None
    return int(row["n"])


def without(
    payload: dict[str, Any], secid: str, block: str = "securities", col: str = ""
) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    b = out[block]
    i = b["columns"].index(col) if col else 0
    b["data"] = [r for r in b["data"] if r[i] != secid]
    return out


@respx.mock
def test_full_load_and_idempotent(
    settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    mock_iss(respx)  # type: ignore[arg-type]
    first = run_full(settings, db)
    r1 = run_row(conn, first.run_id)
    assert first.status == "succeeded", r1["error"]
    assert r1["rows_inserted"] > 500
    bonds = {
        r["secid"]: r for r in conn.execute("SELECT * FROM ref.bond ORDER BY secid").fetchall()
    }
    assert set(bonds) == set(SAMPLES)
    assert bonds["SU29010RMFS4"]["bond_class"] == "ofz_pk"
    assert bonds["RU000A0NNH09"]["bond_class"] == "structured"
    assert bonds["RU000A0NNH09"]["primary_board"] == "EQRP"
    assert all(b["status"] == "active" for b in bonds.values())
    issuer = conn.execute("SELECT * FROM ref.issuer WHERE inn = %s", (ms.MINFIN_INN,)).fetchone()
    assert issuer is not None and issuer["is_sovereign"] is True
    boards = conn.execute(
        "SELECT * FROM ref.bond_board WHERE secid = 'RU000A0NNH09' ORDER BY board"
    ).fetchall()
    assert {b["board"] for b in boards} >= {"EQRP", "OCBR"}
    assert all(b["listing_source"] == "description" for b in boards)
    tqob = conn.execute(
        "SELECT * FROM ref.bond_board WHERE secid = 'SU26238RMFS4' AND board = 'TQOB'"
    ).fetchone()
    assert tqob is not None and tqob["lot_size"] == 1 and tqob["settle_code"] == "T1"
    assert tqob["settle_date"] == date(2026, 10, 19) and tqob["is_primary"] is True
    n_coupons = conn.execute("SELECT count(*) AS n FROM ref.bond_coupon").fetchone()
    expected = sum(len(load(f"bondization/{s}.json")["coupons"]["data"]) for s in SAMPLES)
    assert n_coupons is not None and n_coupons["n"] == expected
    undated = conn.execute(
        "SELECT * FROM ref.bond_offer WHERE secid = 'RU000A0JS4Z7' AND date_missing"
    ).fetchall()
    assert len(undated) == 1 and undated[0]["offer_date"] is None
    formula = conn.execute(
        "SELECT * FROM ref.bond_rate_formula WHERE secid = 'RU000A10A356'"
    ).fetchone()
    assert formula is not None and formula["benchmark"] == "KEY_RATE"
    assert formula["spread_bp"] == Decimal("195") and formula["status"] == "inferred"
    resolved = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = 'RU000A10A356'"
    ).fetchone()
    assert resolved is not None and resolved["sources"] == ["moex_iss"]
    snaps = conn.execute(
        "SELECT endpoint, count(*) AS n FROM ref.static_snapshot GROUP BY endpoint"
    ).fetchall()
    assert {s["endpoint"] for s in snaps} == {
        "directory",
        "collection",
        "market",
        "description",
        "bondization",
    }
    captures = conn.execute(
        "SELECT endpoint, count(*) AS n FROM ops.raw_capture WHERE run_id = %s GROUP BY endpoint",
        (first.run_id,),
    ).fetchall()
    assert {c["endpoint"] for c in captures} >= {
        bs.EP_DIRECTORY,
        bs.EP_COLLECTION,
        bs.EP_MARKET,
        bs.EP_DESCRIPTION,
        bs.EP_BONDIZATION,
    }
    assert history_count(conn) == 0

    # second identical run: nothing changes, no history
    second = run_full(settings, db)
    r2 = run_row(conn, second.run_id)
    assert second.status == "succeeded", r2["error"]
    assert (r2["rows_inserted"], r2["rows_updated"]) == (0, 0)
    assert r2["rows_unchanged"] > 400
    assert history_count(conn) == 0
    # the EQRP description was not re-fetched but re-parsed from the stored capture
    desc_caps = conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE endpoint = %s", (bs.EP_DESCRIPTION,)
    ).fetchone()
    assert desc_caps is not None and desc_caps["n"] == 1


@respx.mock
def test_revisions_tracked(settings: Settings, db: Database, conn: Conn, fixed_today: None) -> None:
    mock_iss(respx)  # type: ignore[arg-type]
    assert run_full(settings, db).status == "succeeded"
    before = conn.execute(
        "SELECT * FROM ref.bond_coupon WHERE secid = 'SU26238RMFS4' AND pay_date > %s "
        "ORDER BY coupon_no LIMIT 1",
        (TODAY,),
    ).fetchone()
    assert before is not None and before["status"] == "known"

    def move_record_date(payload: dict[str, Any]) -> None:
        c = payload["coupons"]
        i_pay, i_rec = c["columns"].index("coupondate"), c["columns"].index("recorddate")
        i_sec = c["columns"].index("secid")
        for r in c["data"]:
            if r[i_sec] == "SU26238RMFS4" and r[i_pay] == before["pay_date"].isoformat():
                r[i_rec] = "2027-01-01"
        o = payload["offers"]
        i_osec, i_odate = o["columns"].index("secid"), o["columns"].index("offerdate")
        o["data"] = [
            r for r in o["data"] if not (r[i_osec] == "RU000A0JS4Z7" and r[i_odate] == "2027-08-26")
        ]

    market = without(load("market.json"), "RU000A0JXTS9")
    mock_iss(respx, blocks=bulk_bondization_payloads(mutate=move_record_date), market=market)  # type: ignore[arg-type]
    out = run_full(settings, db)
    assert out.status == "succeeded", run_row(conn, out.run_id)["error"]
    after = conn.execute(
        "SELECT * FROM ref.bond_coupon WHERE secid = 'SU26238RMFS4' AND coupon_no = %s",
        (before["coupon_no"],),
    ).fetchone()
    assert after is not None and after["record_date"] == date(2027, 1, 1)
    assert after["pay_date"] == before["pay_date"]
    hist = conn.execute(
        "SELECT * FROM ops.row_history WHERE table_name = 'ref.bond_coupon'"
    ).fetchall()
    assert len(hist) == 1
    assert hist[0]["row_key"] == {"secid": "SU26238RMFS4", "coupon_no": before["coupon_no"]}
    assert hist[0]["old_row"]["record_date"] == before["record_date"].isoformat()
    assert hist[0]["replaced_by_run"] == out.run_id
    offer = conn.execute(
        "SELECT * FROM ref.bond_offer WHERE secid = 'RU000A0JS4Z7' AND offer_key = '2027-08-26'"
    ).fetchone()
    assert offer is not None and offer["is_deleted"] is True
    board = conn.execute(
        "SELECT * FROM ref.bond_board WHERE secid = 'RU000A0JXTS9' AND board = 'TQCB'"
    ).fetchone()
    assert board is not None and board["listed"] is False
    # still traded per directory -> active, settlement currency now unknown
    rus = conn.execute(
        "SELECT status, currency_id FROM ref.bond WHERE secid = 'RU000A0JXTS9'"
    ).fetchone()
    assert rus is not None and rus["status"] == "active"


@respx.mock
def test_fixed_label_conflict_written_and_resolved(
    settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    mock_iss(respx)  # type: ignore[arg-type]
    assert run_full(settings, db).status == "succeeded"
    sql = "SELECT * FROM ref.static_conflict WHERE secid = 'RU000A0JS4Z7'"
    rows = conn.execute(sql).fetchall()
    assert len(rows) == 1
    assert (rows[0]["field"], rows[0]["value_a"], rows[0]["resolved"]) == (
        "coupon_type",
        "variable_known",
        False,
    )
    assert conn.execute("SELECT count(*) AS n FROM ref.static_conflict").fetchone() == {"n": 1}

    def publish_coupons(payload: dict[str, Any]) -> None:
        c = payload["coupons"]
        i_val, i_prc = c["columns"].index("value"), c["columns"].index("valueprc")
        for r in c["data"]:
            if r[i_val] is None and r[i_prc] is None:
                r[i_val], r[i_prc] = 95.49, 19.15

    blocks = bulk_bondization_payloads(["RU000A0JS4Z7"], mutate=publish_coupons)
    other = bulk_bondization_payloads([s for s in SAMPLES if s != "RU000A0JS4Z7"])
    for name, b in other.items():
        blocks[name]["data"] += b["data"]
    mock_iss(respx, blocks=blocks)  # type: ignore[arg-type]
    out = run_full(settings, db)
    assert out.status == "succeeded", run_row(conn, out.run_id)["error"]
    after = conn.execute(sql).fetchone()
    assert after is not None and after["resolved"] is True and after["run_id"] == out.run_id
    labels = conn.execute(
        "SELECT coupon_type, coupon_type_basis FROM ref.bond WHERE secid = 'RU000A0JS4Z7'"
    ).fetchone()
    assert labels == {"coupon_type": "fixed", "coupon_type_basis": "moex_label"}
    assert history_count(conn, "ref.static_conflict") == 1


@respx.mock
def test_bondization_failure_is_partial(
    settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    mock_iss(respx)  # type: ignore[arg-type]
    blocks = bulk_bondization_payloads()
    respx.get(f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization.json").mock(
        side_effect=bulk_handler(blocks, grand_shift=-1)
    )
    out = run_full(settings, db)
    row = run_row(conn, out.run_id)
    assert out.status == "partial" and "bondization" in row["error"]
    n = conn.execute("SELECT count(*) AS n FROM ref.bond_coupon").fetchone()
    assert n is not None and n["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM ref.bond").fetchone() == {"n": len(SAMPLES)}


@respx.mock
def test_probe_loads_new_listing(
    settings: Settings, db: Database, conn: Conn, fixed_today: None
) -> None:
    new = "RU000A10A356"
    directory = without(load("directory.json"), new)
    collection = without(load("collection.json"), new)
    market = without(load("market.json"), new)
    blocks = bulk_bondization_payloads(secids=[s for s in SAMPLES if s != new])
    mock_iss(respx, directory=directory, collection=collection, market=market, blocks=blocks)  # type: ignore[arg-type]
    assert run_full(settings, db).status == "succeeded"
    assert conn.execute(
        "SELECT count(*) AS n FROM ref.bond WHERE secid = %s", (new,)
    ).fetchone() == {"n": 0}

    mock_iss(respx)  # type: ignore[arg-type]
    out = run_job(
        "bonds_static_probe",
        {"mode": "probe"},
        settings=settings,
        db=db,
        http_factory=_http_factory,
    )
    assert out.status == "succeeded", run_row(conn, out.run_id)["error"]
    row = conn.execute("SELECT * FROM ref.bond WHERE secid = %s", (new,)).fetchone()
    assert row is not None and row["coupon_type"] == "floating" and row["status"] == "active"
    n = conn.execute(
        "SELECT count(*) AS n FROM ref.bond_coupon WHERE secid = %s", (new,)
    ).fetchone()
    assert n is not None and n["n"] == len(load(f"bondization/{new}.json")["coupons"]["data"])
    # the probe's result equals what the next full run computes: no revision
    hist_before = history_count(conn, "ref.bond")
    full = run_full(settings, db)
    assert full.status == "succeeded"
    assert history_count(conn, "ref.bond") == hist_before
    assert history_count(conn, "ref.bond_coupon") == 0


def test_ordered_map_keeps_order_and_raises() -> None:
    import time

    def slow(i: int) -> int:
        time.sleep(0.001 * (5 - i % 5))
        if i == 7:
            raise ValueError("boom")
        return i * 2

    assert list(ms.ordered_map(slow, range(7), workers=3)) == [i * 2 for i in range(7)]
    with pytest.raises(ValueError, match="boom"):
        list(ms.ordered_map(slow, range(20), workers=3))


@respx.mock
def test_bulk_bondization_concurrent_same_result() -> None:
    blocks = bulk_bondization_payloads()
    respx.get(f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization.json").mock(
        side_effect=bulk_handler(blocks, pagesize=20)
    )
    with http_client() as http:
        seq = [p.rows for p in ms.iter_bondization_pages(http, split_rows=40)]
        par = [p.rows for p in ms.iter_bondization_pages(http, workers=4, split_rows=40)]
    assert seq == par and len(seq) > 5


def test_board_rows_merge_fresh_description_into_market_row() -> None:
    u = fixture_universe()
    m, d_only, _ = bs._board_rows("SU52002RMFS1", u, "TQOB", _Ctx(), run_id=1)  # type: ignore[arg-type]
    assert "board_is_traded" not in m[0]  # no fresh description: description columns untouched
    u.descriptions["SU52002RMFS1"] = ms.parse_description(
        load("description/SU52002RMFS1.json"), "SU52002RMFS1"
    )
    u.description_fresh["SU52002RMFS1"] = (1, "0" * 64)
    m, d_only, _ = bs._board_rows("SU52002RMFS1", u, "TQOB", _Ctx(), run_id=1)  # type: ignore[arg-type]
    tqob = next(r for r in m if r["board"] == "TQOB")
    assert tqob["board_is_traded"] is True and tqob["listing_source"] == "market"
    assert all(r["board"] != "TQOB" for r in d_only)


def test_at1_without_maturity_is_perpetual() -> None:
    u = fixture_universe()
    coll = u.collection["RU000A0JWMZ1"]
    coll["BOND_SUBTYPE"] = "До оферты (call)"
    row, _ = bond(u, "RU000A0JWMZ1")
    assert row["is_perpetual"] is True and row["maturity_date"] is None


def test_foreign_sovereign_not_ofz() -> None:
    kz = ms.ClassifyInput(
        secid="RU000A101RV2",
        name="Республика Казахстан 07",
        sec_type="ofz_bond",
        issuer_inn="0010038193",
        face_unit="RUB",
    )
    assert ms.classify_bond(kz) == "foreign_sovereign"
    stub = ms.ClassifyInput(secid="SU26238RMFS4", name="ОФЗ-ПД 26238", sec_type="ofz_bond")
    assert ms.classify_bond(stub) == "ofz_pd"
