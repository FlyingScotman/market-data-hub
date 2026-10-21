"""Curated issue terms with evidence (WEB-16; TDM brief HS8 / HS9 / HS10, review P1-6 / P1-9).

The packaged ``mdhub/data/bond_terms_curated.json`` is validated as shipped. The job tests use
a synthetic document in the curated format (values and evidence below are test data, not
read from any issue document).
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import bond_terms

Conn = psycopg.Connection[DictRow]

EVIDENCE = {
    "document_url": "https://example.invalid/decision.pdf",
    "document_title": "TEST decision on issue",
    "page": "7",
    "extracted_text": "TEST clause",
    "retrieved_on": "2026-10-21",
}
DOC: dict[str, Any] = {
    "version": "test.1",
    "entries": [
        {
            "secid": "FLT-1",
            "isin": "RU000XFLT001",
            "term": "floater_formula",
            "value": {"benchmark": "KEY_RATE", "spread_bp": "250", "lag_days": 1,
                      "averaging": "none", "floor_pct": "0"},
            "evidence": EVIDENCE,
        },
        {
            "secid": "PERP-1",
            "term": "call",
            "value": {"date": "2030-03-01", "price_pct": "100"},
            "evidence": EVIDENCE,
        },
        {
            "secid": "FLT-1",
            "term": "offer",
            "value": {"date": "2027-06-01", "type": "put", "price_pct": "100",
                      "window_start": "2027-05-25", "window_end": "2027-05-31"},
            "evidence": EVIDENCE,
        },
        {
            "secid": "PERP-1",
            "term": "replacement_bond",
            "value": {"replacement_bond": False},
            "evidence": EVIDENCE,
        },
    ],
}  # fmt: skip


def test_packaged_file_is_valid() -> None:
    version, terms = bond_terms.packaged_terms()
    assert version and all(t.evidence.get("extracted_text") for t in terms)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"evidence": {**EVIDENCE, "extracted_text": ""}}, "evidence needs"),
        ({"term": "coupon"}, "unknown term"),
        ({"value": {"benchmark": "KEY_RATE", "spread_bp": 2.5}}, "decimals are strings"),
        ({"value": {"spread_bp": "250"}}, "without benchmark"),
        ({"value": {"benchmark": "KEY_RATE", "lag": 1}}, "unknown formula fields"),
    ],
)
def test_validation_fails_closed(patch: dict[str, Any], match: str) -> None:
    doc = copy.deepcopy(DOC)
    doc["entries"][0].update(patch)
    with pytest.raises(ValueError, match=match):
        bond_terms.load_terms(doc)


def test_validation_duplicates_and_offers() -> None:
    doc = copy.deepcopy(DOC)
    doc["entries"].append(copy.deepcopy(doc["entries"][1]))
    with pytest.raises(ValueError, match="duplicate"):
        bond_terms.load_terms(doc)
    doc = copy.deepcopy(DOC)
    doc["entries"][2]["value"]["type"] = "maybe"
    with pytest.raises(ValueError, match="bad offer type"):
        bond_terms.load_terms(doc)


def _seed(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, bond_class, coupon_type, coupon_type_basis, status,
                              maturity_date, is_perpetual)
        VALUES ('FLT-1', 'RU000XFLT001', 'corporate', 'floating', 'benchmark', 'active',
                '2029-01-01', false),
               ('PERP-1', 'RU000XPERP01', 'corporate', 'fixed', 'moex_label', 'active',
                NULL, true)
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond_rate_formula (secid, source, benchmark, benchmark_raw, spread_bp,
                                           status)
        VALUES ('FLT-1', 'moex_iss', 'KEY_RATE', 'RUCBCRKR', 250, 'inferred')
        """
    )
    conn.commit()


def _use(monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any]) -> None:
    monkeypatch.setattr(bond_terms, "packaged_terms", lambda: bond_terms.load_terms(doc))


def test_job_applies_terms_and_soft_deletes(
    conn: Conn, settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(conn)
    _use(monkeypatch, DOC)
    out = run_job("bond_terms_curated", {}, settings=settings, db=db)
    assert out.status == "succeeded", out
    assert out.result["applied"] == 4 and out.result["formulas"] == 1
    f = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = 'FLT-1'"
    ).fetchone()
    assert f is not None
    assert (f["status"], f["lag_days"], f["averaging"], f["floor_pct"]) == (
        "curated", 1, "none", 0,
    )  # fmt: skip
    assert f["sources"] == ["curated", "moex_iss"]
    cur = conn.execute(
        "SELECT * FROM ref.bond_rate_formula WHERE secid = 'FLT-1' AND source = 'curated'"
    ).fetchone()
    assert cur is not None and cur["evidence"]["file"] == "bond_terms_curated.json"
    assert cur["evidence"]["extracted_text"] == "TEST clause"
    offers = {
        (r["secid"], r["offer_key"]): r
        for r in conn.execute(
            "SELECT * FROM ref.bond_offer WHERE source = 'curated' AND NOT is_deleted"
        ).fetchall()
    }
    call = offers[("PERP-1", "2030-03-01")]
    assert (call["offer_type"], call["offer_type_basis"], call["status"], call["price"]) == (
        "call", "document", "scheduled", 100,
    )  # fmt: skip
    put = offers[("FLT-1", "2027-06-01")]
    assert (put["offer_type"], put["offer_date_start"]) == ("put", date(2027, 5, 25))

    # identical rerun: no change
    hist = "SELECT count(*) AS n FROM ops.row_history WHERE table_name LIKE 'ref.bond_%%'"
    before = conn.execute(hist).fetchone()
    assert run_job("bond_terms_curated", {}, settings=settings, db=db).status == "succeeded"
    assert conn.execute(hist).fetchone() == before

    # an entry removed from the file is soft-deleted, the MOEX formula is untouched
    doc = copy.deepcopy(DOC)
    doc["entries"] = doc["entries"][1:]
    _use(monkeypatch, doc)
    out = run_job("bond_terms_curated", {}, settings=settings, db=db)
    assert out.status == "succeeded" and out.result["soft_deleted"] == 2
    f = conn.execute(
        "SELECT status FROM ref.bond_rate_formula_resolved WHERE secid = 'FLT-1'"
    ).fetchone()
    assert f == {"status": "inferred"}


def test_job_unknown_secid_partial_isin_mismatch_fails(
    conn: Conn, settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(conn)
    doc = copy.deepcopy(DOC)
    doc["entries"][1]["secid"] = "NOPE-1"
    _use(monkeypatch, doc)
    out = run_job("bond_terms_curated", {}, settings=settings, db=db)
    assert out.status == "partial" and out.result["skipped_unknown_secid"] == 1
    doc["entries"][0]["isin"] = "RU000XOTHER1"
    _use(monkeypatch, doc)
    with pytest.raises(ValueError, match="ISIN does not match"):
        run_job("bond_terms_curated", {}, settings=settings, db=db)
