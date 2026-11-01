"""Historical terms: verified document provenance, idempotency and fail-closed writes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import IssFormatError
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import futures_rules

BODY = b"synthetic approved specification document"


def http_factory(**kw: Any) -> HttpClient:
    return HttpClient(limiter=RateLimiter(default_rps=None), max_retries=0, **kw)


def seed(conn: psycopg.Connection[DictRow], *, secid: str, asset: str, expiry: str,
         listed: str | None = None, min_step: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO ref.futures_contract
            (secid, asset_code, contract_type, spec_source, first_seen, last_seen,
             first_trade_date, last_trade_date, lot_volume, min_step)
        VALUES (%s, %s, 'dated', 'iss_description', '2023-01-01', %s,
                '2023-01-01', %s, 1, %s)
        """,
        (secid, asset, listed, expiry, min_step),
    )
    conn.commit()


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter) -> None:
    rules = tuple(replace(r, sha256=hashlib.sha256(BODY).hexdigest()) for r in futures_rules.RULES)
    monkeypatch.setattr(futures_rules, "RULES", rules)
    for rule in rules:
        respx_mock.get(rule.url).mock(return_value=httpx.Response(200, content=BODY))


def test_rules_repair_only_historical_terms_and_capture_documents(
    verified: None, conn: psycopg.Connection[DictRow], settings: Settings, db: Database,
    respx_mock: respx.MockRouter, today_is: Any,
) -> None:
    today_is(date(2026, 11, 1))
    seed(conn, secid="RBH4", asset="RGBI", expiry="2024-03-01")
    seed(conn, secid="RBM6", asset="RGBI", expiry="2026-06-01")
    seed(conn, secid="RFU6", asset="RUONIA", expiry="2026-09-01")
    seed(conn, secid="RBZ6", asset="RGBI", expiry="2026-12-01", listed="2026-10-30")
    outcome = run_job("futures_contract_rules", settings=settings, db=db, http_factory=http_factory)
    assert outcome.status == "succeeded"
    conn.commit()
    rows = conn.execute(
        "SELECT secid, min_step, step_price, spec_source, last_seen, capture_id "
        "FROM ref.futures_contract ORDER BY secid"
    ).fetchall()
    known = {r["secid"]: r for r in rows}
    for secid in ("RBH4", "RBM6", "RFU6"):
        r = known[secid]
        assert r["min_step"] == (Decimal("0.0001") if secid == "RFU6" else Decimal(1))
        assert r["step_price"] == 1 and r["spec_source"] == "moex_rules"
        assert r["last_seen"] is None and r["capture_id"] is not None
    assert known["RBZ6"]["min_step"] is None
    assert known["RBZ6"]["last_seen"] == date(2026, 10, 30)
    assert known["RBM6"]["capture_id"] == known["RFU6"]["capture_id"]
    assert len(respx_mock.calls) == 2
    assert conn.execute("SELECT count(*) n FROM ops.raw_capture").fetchone()["n"] == 2
    conn.commit()
    again = run_job("futures_contract_rules", settings=settings, db=db, http_factory=http_factory)
    assert again.status == "succeeded" and len(respx_mock.calls) == 2


def test_changed_document_rolls_back_all_repairs(
    verified: None, conn: psycopg.Connection[DictRow], settings: Settings, db: Database,
    respx_mock: respx.MockRouter, today_is: Any,
) -> None:
    today_is(date(2026, 11, 1))
    seed(conn, secid="RBH4", asset="RGBI", expiry="2024-03-01")
    seed(conn, secid="RBM6", asset="RGBI", expiry="2026-06-01")
    respx_mock.get(futures_rules.RULES[3].url).mock(
        return_value=httpx.Response(200, content=b"changed specification")
    )
    with pytest.raises(IssFormatError, match="document changed"):
        run_job("futures_contract_rules", settings=settings, db=db, http_factory=http_factory)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) n FROM ref.futures_contract WHERE min_step IS NOT NULL"
    ).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) n FROM ops.raw_capture").fetchone()["n"] == 0


def test_unknown_edition_remains_missing_without_requests(
    conn: psycopg.Connection[DictRow], settings: Settings, db: Database,
    respx_mock: respx.MockRouter, today_is: Any,
) -> None:
    today_is(date(2028, 1, 1))
    seed(conn, secid="RBZ7", asset="RGBI", expiry="2027-12-01")
    outcome = run_job("futures_contract_rules", settings=settings, db=db, http_factory=http_factory)
    assert outcome.status == "partial" and not respx_mock.calls
    conn.commit()
    assert conn.execute("SELECT min_step FROM ref.futures_contract").fetchone()["min_step"] is None


def test_conflicting_stored_term_is_not_overwritten(
    verified: None, conn: psycopg.Connection[DictRow], settings: Settings, db: Database,
    respx_mock: respx.MockRouter, today_is: Any,
) -> None:
    today_is(date(2026, 11, 1))
    seed(conn, secid="RBH4", asset="RGBI", expiry="2024-03-01", min_step="2")
    with pytest.raises(IssFormatError, match="conflict"):
        run_job("futures_contract_rules", settings=settings, db=db, http_factory=http_factory)
    assert not respx_mock.calls
    conn.commit()
    assert conn.execute("SELECT min_step FROM ref.futures_contract").fetchone()["min_step"] == 2
