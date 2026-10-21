"""Market Snapshot Contract 1.3 (additive, WEB-16): agency ratings with issuer scope, grade,
rating type, press release and mapping basis; replacement bonds (P1-6); ``issue_size_moex``
(P1-10); documented floater conventions / calls / offers with their evidence (HS8-HS10); issuer
``sector_source = agency``.

``tests/fixtures/moex_names/`` are MOEX ISS answers of 2026-10-21 to
``/iss/securities.json?q=ЗО&engine=stock&market=bonds`` (both pages): every bond whose name
contains ``ЗО``, the population the replacement name rule is checked against.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.snapshot.runner import publish_static_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION
from mdhub.snapshot.static import issue_size_moex, replacement_by_name
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)
FIX = Path(__file__).parent / "fixtures" / "moex_names"


def test_version() -> None:
    assert SCHEMA_VERSION == "market_snapshot/1.3"


def _names() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in ("securities_q_zo_p0.json", "securities_q_zo_p1.json"):
        block = json.loads((FIX / f).read_text("utf-8"))["securities"]
        i_secid, i_name = block["columns"].index("secid"), block["columns"].index("name")
        out.update({r[i_secid]: r[i_name] for r in block["data"]})
    return out


def test_replacement_name_rule_on_moex_names() -> None:
    names = _names()
    assert len(names) == 187
    flagged = {s for s, n in names.items() if replacement_by_name(n)}
    assert len(flagged) == 89
    # replacement series of the main issuers, perpetual and subordinated variants, sovereign
    for secid in ("RU000A1056U0", "RU000A105QW3", "RU000A108NQ6", "RU000A1082Q4",
                  "RU000A108DH6", "RU000A107B84", "RU000A10A8E8", "RU000A1059N9"):  # fmt: skip
        assert secid in flagged, (secid, names[secid])
    # names that merely contain the letters: "Защита онлайн" (short name ЗО-001P-01), ЗООПТ,
    # Союзобщемашбанк, Новомосковский Азот
    for secid in ("RU000A10G080", "RU000A10DRX8", "RU0005880987", "RU0003877472"):
        assert secid not in flagged, (secid, names[secid])
    assert not replacement_by_name(None)


def test_issue_size_moex_basis() -> None:
    assert issue_size_moex({"issue_size": Decimal(10), "issue_size_placed": Decimal(9)}) == (
        Decimal(10), "ISSUESIZE",
    )  # fmt: skip
    assert issue_size_moex({"issue_size": None, "issue_size_placed": Decimal(9)}) == (
        Decimal(9), "ISSUESIZEPLACED",
    )  # fmt: skip
    assert issue_size_moex({"issue_size": None, "issue_size_placed": None}) == (None, None)


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


EVIDENCE = {
    "file": "bond_terms_curated.json",
    "version": "test.1",
    "document_url": "https://example.invalid/decision.pdf",  # test data, not a real document
    "document_title": "TEST decision",
    "page": "7",
    "extracted_text": "TEST clause",
    "retrieved_on": "2026-10-14",
}


def test_static_snapshot_1_3_fields(conn: Conn, db: Database) -> None:
    seed(conn)
    conn.execute(
        """
        UPDATE ref.bond SET name = 'Газпром капитал ООО ЗО27-1-Д', issue_size = NULL,
               issue_size_placed = 1000000
         WHERE secid = 'SU26242RMFS0';
        UPDATE ref.bond SET issue_size = 5000000 WHERE secid = 'RU000A105X12';
        UPDATE ref.issuer SET sector = 'banks', sector_source = 'agency' WHERE issuer_id = 2;
        INSERT INTO ref.bond_rating (secid, agency, scale, source, issuer_id, rating, grade,
                                     outlook, rating_date, action, rating_type, evidence)
        VALUES ('RU000A105X12', 'НКР', 'national_ru', 'nkr', 2, 'A+.ru', 'A+', NULL,
                '2026-10-01', NULL, 'Рейтинги выпусков',
                '{"mapping_basis": "isin", "press_release": "https://ratings.ru/x/"}'),
               ('RU000PERP001', 'НРА', 'national_ru', 'nra', 2, NULL, NULL, NULL,
                '2026-09-01', 'withdrawn', 'Кредитный рейтинг отдельных выпусков облигаций',
                '{"mapping_basis": "isin"}');
        INSERT INTO ref.issuer_rating (issuer_id, agency, scale, source, rating_type, rating,
                                       grade, outlook, watch, action, rating_date, evidence)
        VALUES (2, 'Эксперт РА', 'national_ru', 'raexpert',
                'Рейтинги кредитоспособности банков', 'ruA', 'A', 'Стабильный',
                'Рейтинг под наблюдением', NULL, '2026-10-02',
                '{"mapping_basis": "inn", "press_release": "https://raexpert.ru/releases/x/"}');
        """
    )
    conn.execute(
        "INSERT INTO ref.bond_rate_formula (secid, source, benchmark, spread_bp, lag_days, "
        "averaging, status, evidence) VALUES ('RU000A105X12', 'curated', 'KEY_RATE', 150, 1, "
        "'none', 'curated', %s)",
        (Jsonb(EVIDENCE),),
    )
    conn.execute(
        "INSERT INTO ref.bond_offer (secid, source, offer_key, offer_date, offer_type, "
        "offer_type_basis, status, price) VALUES ('RU000PERP001', 'curated', '2028-01-15', "
        "'2028-01-15', 'call', 'document', 'scheduled', 100)"
    )
    conn.execute(
        "INSERT INTO ref.bond_curated_term (secid, term, term_key, value, evidence, "
        "file_version) VALUES "
        "('RU000PERP001', 'call', '2028-01-15', %s, %s, 'test.1'), "
        "('RU000PERP001', 'replacement_bond', '', %s, %s, 'test.1')",
        (Jsonb({"date": "2028-01-15", "price_pct": "100"}), Jsonb(EVIDENCE),
         Jsonb({"replacement_bond": False}), Jsonb(EVIDENCE)),
    )  # fmt: skip
    conn.commit()
    out = publish_static_snapshot(
        db,
        cut_type="daily",
        business_date=DAY,
        cut_ts=datetime(2026, 10, 15, 7, 45, tzinfo=MSK).astimezone(UTC),
        known_at=utc_now(),
    )
    doc = _bundle(conn, out.id)  # validated against the 1.3 schema when published
    assert doc["schema_version"] == "market_snapshot/1.3"
    inst = {i["secid"]: i for i in doc["data"]["instruments"]}
    assert (inst["SU26242RMFS0"]["replacement_bond"], inst["SU26242RMFS0"]["replacement_basis"]) == (
        True, "name_rule",
    )  # fmt: skip
    assert (inst["RU000PERP001"]["replacement_bond"], inst["RU000PERP001"]["replacement_basis"]) == (
        False, "document",
    )  # fmt: skip
    assert inst["RU000A105X12"]["replacement_bond"] is None
    assert inst["RU000A105X12"]["replacement_basis"] is None
    assert (inst["SU26242RMFS0"]["issue_size_moex"], inst["SU26242RMFS0"]["issue_size_moex_field"]) == (
        "1000000", "ISSUESIZEPLACED",
    )  # fmt: skip
    assert inst["RU000A105X12"]["issue_size_moex_field"] == "ISSUESIZE"

    ratings = doc["data"]["ratings"]
    assert [(r["scope"], r["secid"], r["source"]) for r in ratings] == [
        ("issue", "RU000A105X12", "nkr"),
        ("issue", "RU000PERP001", "nra"),
        ("issuer", None, "raexpert"),
    ]
    nkr_row, nra_row, rae_row = ratings
    assert (nkr_row["grade"], nkr_row["mapping_basis"], nkr_row["press_release"]) == (
        "A+", "isin", "https://ratings.ru/x/",
    )  # fmt: skip
    assert (nra_row["rating"], nra_row["action"]) == (None, "withdrawn")
    assert (rae_row["issuer_id"], rae_row["watch"], rae_row["mapping_basis"]) == (
        2, "Рейтинг под наблюдением", "inn",
    )  # fmt: skip
    ds = {d["name"]: d for d in doc["datasets"]}
    cov = ds["ratings"]["coverage"]
    assert cov["by_scope"] == {"issue": 2, "issuer": 1} and cov["withdrawn"] == 1
    assert cov["active_rub_corporate_issuers_rated"] == 1
    assert cov["active_non_structured_bonds_issue_rated"] == 1
    assert cov["active_non_structured_bonds_issuer_rated"] >= 2  # both bonds of issuer 2

    (formula,) = [f for f in doc["data"]["floater_formulas"] if f["secid"] == "RU000A105X12"]
    assert (formula["status"], formula["lag_days"], formula["evidence_document_url"]) == (
        "curated", 1, "https://example.invalid/decision.pdf",
    )  # fmt: skip
    assert formula["evidence_page"] == "7"
    fcov = ds["floater_formulas"]["coverage"]
    assert (fcov["key_rate_ruonia_floaters"], fcov["key_rate_ruonia_floaters_curated"]) == (1, 1)
    assert fcov["active_perpetuals_with_scheduled_call"] == 1
    (call,) = [o for o in doc["data"]["offers"] if o["source"] == "curated"]
    assert call["evidence_document_url"] == "https://example.invalid/decision.pdf"
    assert all(
        o["evidence_document_url"] is None
        for o in doc["data"]["offers"]
        if o["source"] != "curated"
    )
    (issuer,) = [i for i in doc["data"]["issuers"] if i["issuer_id"] == 2]
    assert issuer["sector_source"] == "agency"
