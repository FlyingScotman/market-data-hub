"""Issuer reference (WEB-13 / TDM R9 HS1): MOEX emitter cards -> ref.issuer_card / ref.issuer.

Fixtures ``tests/fixtures/moex_emitters/`` are public ISS answers of 2026-10-20
(``/iss/emitters/1039.json``, ``/iss/emitters/712.json``, ``/iss/emitters.json`` first page).
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.loaders import issuers as _issuers  # noqa: F401  (registers the job)
from mdhub.sources import moex_emitters as me

FIX = Path(__file__).parent / "fixtures" / "moex_emitters"
Conn = psycopg.Connection[DictRow]


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


# ------------------------------------------------------------------------------ parser


def test_parse_card_gazprom_neft() -> None:
    card = me.parse_card(load("emitter_1039.json"), 1039)
    assert card is not None
    assert card.short_title == 'ПАО "Газпром нефть"'
    assert card.ogrn == "1025501701686"
    assert card.inn == "5504036333"
    assert card.oksm == 643 and card.country == "RU"


def test_parse_card_rzd() -> None:
    card = me.parse_card(load("emitter_712.json"), 712)
    assert card is not None and card.short_title == 'ОАО "РЖД"' and card.country == "RU"


def test_country_table() -> None:
    assert me.country_alpha2(643) == "RU"
    assert me.country_alpha2(112) == "BY"
    assert me.country_alpha2(398) == "KZ"
    assert me.country_alpha2(None) is None
    assert me.country_alpha2(999) is None  # unknown code: not guessed
    assert me.oksm_table_version()


def test_parse_card_empty_and_drift() -> None:
    empty = copy.deepcopy(load("emitter_712.json"))
    empty["emitter"]["data"] = []
    assert me.parse_card(empty, 712) is None
    other = load("emitter_712.json")
    with pytest.raises(FormatDriftError):
        me.parse_card(other, 713)  # card of another id
    drift = copy.deepcopy(load("emitter_712.json"))
    i = drift["emitter"]["columns"].index("OGRN")
    drift["emitter"]["columns"].pop(i)
    for r in drift["emitter"]["data"]:
        r.pop(i)
    with pytest.raises(FormatDriftError):
        me.parse_card(drift, 712)


def test_parse_list_rows() -> None:
    page = load("emitters_page0.json")
    cols = page["emitters"]["columns"]
    rows = [dict(zip(cols, r, strict=True)) for r in page["emitters"]["data"]]
    out = me.parse_list_rows(rows)
    assert out[196]["inn"] == "7744001497" and out[196]["short_title"] == "Банк ГПБ (АО)"


# ------------------------------------------------------------------------- integration


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def _list_payload(rows: list[list[Any]]) -> dict[str, Any]:
    page = load("emitters_page0.json")
    return {
        "emitters": {"columns": page["emitters"]["columns"], "data": rows},
        "emitters.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[0, len(rows), 100]],
        },
    }


LIST_ROWS = [
    [1039, 'Публичное акционерное общество "Газпром нефть"', "5504036333", 'ПАО "Газпром нефть"',
     "www.gazprom-neft.ru"],
    [712, 'открытое акционерное общество "Российские железные дороги"', "7708503727",
     'ОАО "РЖД"', "https://www.rzd.ru"],
]  # fmt: skip


def _seed(conn: Conn, today: date) -> None:
    conn.execute(
        """
        INSERT INTO ref.issuer (issuer_id, name, inn, okpo) VALUES
            (1039, 'Публичное акционерное общество "Газпром нефть"', '5504036333', '42045241'),
            (712, 'открытое акционерное общество "Российские железные дороги"', '7708503727',
             '00083262'),
            (5, 'old issuer', '0000000000', NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond (secid, issuer_id, bond_class, coupon_type, coupon_type_basis,
                              status, maturity_date, is_perpetual) VALUES
            ('RU000A0JWRE5', 1039, 'corporate', 'fixed', 'moex_label', 'delisted',
             %(far)s, false),
            ('RZD-1', 712, 'corporate', 'fixed', 'moex_label', 'active', %(far)s, false),
            ('OLD-1', 5, 'corporate', 'fixed', 'moex_label', 'matured', %(old)s, false);
        """,
        {"far": today + timedelta(days=7000), "old": today - timedelta(days=400)},
    )
    conn.commit()


def _mock(router: respx.Router, cards: dict[int, Any]) -> None:
    router.get(f"{ISS_BASE_URL}/emitters.json").mock(
        return_value=httpx.Response(200, json=_list_payload(LIST_ROWS))
    )
    for iid, body in cards.items():
        router.get(f"{ISS_BASE_URL}/emitters/{iid}.json").mock(
            return_value=body
            if isinstance(body, httpx.Response)
            else httpx.Response(200, json=body)
        )


def _run(settings: Settings, db: Database, **params: Any) -> JobOutcome:
    return run_job("issuers_static", params, settings=settings, db=db, http_factory=_http_factory)


def _today() -> date:
    from mdhub.core.timeutil import msk_today

    return msk_today()


def test_issuers_static_fills_issuer_and_caches(
    respx_mock: respx.Router, conn: Conn, settings: Settings, db: Database
) -> None:
    _seed(conn, _today())
    _mock(respx_mock, {1039: load("emitter_1039.json"), 712: load("emitter_712.json")})
    out = _run(settings, db)
    assert out.status == "succeeded", out
    rows = {
        r["issuer_id"]: r
        for r in conn.execute("SELECT * FROM ref.issuer ORDER BY issuer_id").fetchall()
    }
    gpn = rows[1039]
    assert (gpn["short_name"], gpn["ogrn"], gpn["country"], gpn["oksm"]) == (
        'ПАО "Газпром нефть"', "1025501701686", "RU", 643,
    )  # fmt: skip
    assert rows[712]["short_name"] == 'ОАО "РЖД"'
    # out of scope (matured long ago): no card fetched, nothing invented
    assert rows[5]["short_name"] is None and rows[5]["country"] is None
    assert gpn["name"].startswith("Публичное")  # directory-owned columns untouched
    caps = conn.execute(
        "SELECT endpoint, count(*) AS n FROM ops.raw_capture GROUP BY endpoint ORDER BY 1"
    ).fetchall()
    assert {r["endpoint"]: r["n"] for r in caps} == {"emitter": 2, "emitters_list": 1}

    # filling the existing issuers is a versioned change (old images: NULLs)
    hist_sql = (
        "SELECT count(*) AS n FROM ops.row_history WHERE table_name IN "
        "('ref.issuer', 'ref.issuer_card')"
    )
    assert conn.execute(hist_sql).fetchone() == {"n": 2}

    # second run: cards are fresh -> only the list is read, nothing changes, no history
    calls = respx_mock.calls.call_count
    out2 = _run(settings, db)
    assert out2.status == "succeeded"
    assert respx_mock.calls.call_count == calls + 1
    assert conn.execute(hist_sql).fetchone() == {"n": 2}

    # a stale cache refetches; identical cards are no-ops (no new history)
    conn.execute("UPDATE ref.issuer_card SET last_seen = now() - interval '8 days'")
    conn.commit()
    calls = respx_mock.calls.call_count
    assert _run(settings, db).status == "succeeded"
    assert respx_mock.calls.call_count == calls + 3
    assert conn.execute(hist_sql).fetchone() == {"n": 2}


def test_issuers_static_list_change_refreshes_and_versions(
    respx_mock: respx.Router, conn: Conn, settings: Settings, db: Database
) -> None:
    _seed(conn, _today())
    _mock(respx_mock, {1039: load("emitter_1039.json"), 712: load("emitter_712.json")})
    assert _run(settings, db).status == "succeeded"
    renamed = copy.deepcopy(load("emitter_712.json"))
    renamed["emitter"]["data"][0][2] = 'ОАО "РЖД" (новое)'
    list_rows_changed = copy.deepcopy(LIST_ROWS)
    list_rows_changed[1][3] = 'ОАО "РЖД" (новое)'
    respx_mock.get(f"{ISS_BASE_URL}/emitters.json").mock(
        return_value=httpx.Response(200, json=_list_payload(list_rows_changed))
    )
    respx_mock.get(f"{ISS_BASE_URL}/emitters/712.json").mock(
        return_value=httpx.Response(200, json=renamed)
    )
    assert _run(settings, db).status == "succeeded"
    row = conn.execute("SELECT short_name FROM ref.issuer WHERE issuer_id = 712").fetchone()
    assert row == {"short_name": 'ОАО "РЖД" (новое)'}
    hist = conn.execute(
        "SELECT old_row ->> 'short_name' AS old FROM ops.row_history "
        "WHERE table_name = 'ref.issuer' AND row_key ->> 'issuer_id' = '712' ORDER BY id"
    ).fetchall()
    assert [h["old"] for h in hist] == [None, 'ОАО "РЖД"']  # first fill, then the rename


def test_issuers_static_card_failure_is_partial(
    respx_mock: respx.Router, conn: Conn, settings: Settings, db: Database
) -> None:
    _seed(conn, _today())
    _mock(respx_mock, {1039: load("emitter_1039.json"), 712: httpx.Response(503)})
    out = _run(settings, db)
    assert out.status == "partial"
    row = conn.execute(
        "SELECT short_name, country FROM ref.issuer WHERE issuer_id = 712"
    ).fetchone()
    assert row == {"short_name": None, "country": None}
    gpn = conn.execute("SELECT country FROM ref.issuer WHERE issuer_id = 1039").fetchone()
    assert gpn == {"country": "RU"}
