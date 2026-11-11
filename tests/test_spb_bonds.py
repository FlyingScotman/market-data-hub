"""SPB public source: identity/units, bounded chart history and isolated persistence."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, JobParamError, run_job
from mdhub.loaders.spb_bonds import DATASET, load_range
from mdhub.quality.checks import queue_range, reconcile_gaps, scan_dataset
from mdhub.quality.history_load import eligible
from mdhub.schedule import RULES, rule_applies
from mdhub.sources import spb

FIX = Path(__file__).parent / "fixtures" / "spb"
TODAY = date(2026, 11, 11)
DAY = date(2026, 11, 10)
MGKL = "RU000A10DTC8"
XMED = "RU000A10G0R1"
Conn = psycopg.Connection[DictRow]


def _bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _json(name: str) -> Any:
    return json.loads(_bytes(name))


def _encode(data: Any) -> bytes:
    return json.dumps(data).encode()


def _candidates() -> list[spb.Candidate]:
    return spb.parse_universe(_bytes("russian-market.json"))


def _identities() -> dict[str, str]:
    return {
        c.ticker: spb.parse_card(_bytes(f"card-{c.ticker}.json"), c)["isin"] for c in _candidates()
    }


def test_discovery_all_russian_bonds_not_hardcoded_or_inferred_from_rub() -> None:
    rows = _json("russian-market.json")
    discovered = _candidates()
    assert {c.ticker for c in discovered} == {
        r["srtsCode"] for r in rows if r["securityKind"] == "Облигации"
    }
    assert len(discovered) == 21
    # Foreign classification never enters discovery, even if the table says RUB.
    foreign = copy.deepcopy(next(r for r in rows if r["securityKind"] == "Облигации"))
    foreign.update(securityCategory="Облигации иностранного эмитента", srtsCode="FOREIGN")
    assert spb.parse_universe(_encode([*rows, foreign])) == discovered
    # Another dynamically supplied, valid candidate is accepted, with no fixed universe.
    added = copy.deepcopy(foreign)
    added.update(securityCategory="Облигации биржевые", idSecurities=99999, srtsCode="NEWBOND")
    assert len(spb.parse_universe(_encode([*rows, added]))) == 22
    assert spb.parse_universe(b"[]") == []
    with pytest.raises(FormatDriftError, match="duplicate"):
        spb.parse_universe(_encode([*rows, rows[0]]))
    with pytest.raises(FormatDriftError):
        spb.parse_universe(b'{"error": "failed"}')


def test_cards_identity_and_explicit_unit_conflict_evidence() -> None:
    identities = _identities()
    assert identities["MGKL1P1"] == MGKL and identities["XMED1P1"] == XMED
    assert len(identities) == len(set(identities.values()))
    card = spb.parse_card(
        _bytes("card-XMED1P1.json"), next(c for c in _candidates() if c.ticker == "XMED1P1")
    )
    assert card["card_price_unit"] == "RUB"  # Never infer PCT from magnitude/issuer.
    assert spb.valid_isin(MGKL) and not spb.valid_isin(MGKL[:-1] + "9")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sisinCode", "RU000A10DTC9"),
        ("sisinCode", "US0378331005"),
        ("srtsCode", "OTHER"),
        ("idSecurities", True),
        ("idSecurities", 11155),
        ("sgroupSec", "Иностранные ценные бумаги"),
        ("securityKind", "акция"),
        ("securityCategory", "Облигации иностранного эмитента"),
        ("scurrencyKotir", None),
    ],
)
def test_card_contradictions_fail_closed(field: str, value: Any) -> None:
    card = _json("card-MGKL1P1.json")
    card[field] = value
    c = next(c for c in _candidates() if c.ticker == "MGKL1P1")
    with pytest.raises(FormatDriftError):
        spb.parse_card(_encode(card), c)


@pytest.mark.parametrize("day", [9, 10])
def test_official_report_unit_reference_exact_join_all_bonds(day: int) -> None:
    units = spb.parse_unit_report(
        _bytes(f"unit-reference-2026-11-{day:02}.pdf"), date(2026, 11, day), _identities()
    )
    assert len(units) == 21
    assert set(units.values()) == {("PCT", "RUB")}
    assert units["XMED1P1", XMED] == ("PCT", "RUB")
    assert units["MGKL1P1", MGKL] == ("PCT", "RUB")
    # Ticker itself can be the ISIN; only the dedicated ISIN column supplies identity.
    assert units["RU000A100FE5", "RU000A100FE5"] == ("PCT", "RUB")


def test_report_date_identity_and_missing_rows_fail_closed() -> None:
    payload = _bytes("unit-reference-2026-11-10.pdf")
    with pytest.raises(FormatDriftError, match="date/title"):
        spb.parse_unit_report(payload, date(2026, 11, 9), _identities())
    with pytest.raises(FormatDriftError, match="identity"):
        spb.parse_unit_report(payload, DAY, {"MGKL1P1": XMED})
    with pytest.raises(FormatDriftError, match="lacks"):
        spb.parse_unit_report(payload, DAY, {"MISSING": MGKL})
    with pytest.raises(FormatDriftError, match="unreadable"):
        spb.parse_unit_report(b"not PDF", DAY, _identities())


@pytest.mark.parametrize("change", ["unit", "currency", "column", "duplicate", "header"])
def test_report_drift_ambiguous_and_unknown_units_rejected(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    header = "Биржевая информация об итогах торгов ценными бумагами за 2026-11-10\n"
    columns = "ISIN Валюта цены Валюта расчетов\n"
    row = f"MGKL1P1 name Биржевые облигации Out of list {MGKL} PCT RUB " + "0 " * 11
    text = header + columns + row
    if change == "unit":
        text = text.replace("PCT RUB", "UNKNOWN RUB")
    elif change == "currency":
        text = text.replace("PCT RUB", "PCT UNKNOWN")
    elif change == "column":
        text += "0"
    elif change == "duplicate":
        text += "\n" + row
    else:
        text = text.replace("Валюта цены", "Missing column")
    monkeypatch.setattr(
        spb,
        "PdfReader",
        lambda *_a, **_kw: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)]),
    )
    with pytest.raises(FormatDriftError):
        spb.parse_unit_report(b"PDF", DAY, {"MGKL1P1": MGKL})


def test_report_archive_completed_dates_stale_and_bad_paths() -> None:
    report_day, path = spb.parse_report_archive(_bytes("archive.json"), today=TODAY)
    assert report_day == DAY and path.endswith("2026-11-10-full.pdf")
    rows = [
        {"date": "2026-11-11", "eventTypes": [{"fullPath": "/anything"}]},
        {"date": "2026-11-10", "eventTypes": [{"fullPath": path}]},
    ]
    assert spb.parse_report_archive(_encode(rows), today=TODAY)[0] == DAY
    for raw in (b"[]", b"{}", _encode([rows[-1], rows[-1]])):
        with pytest.raises(FormatDriftError):
            spb.parse_report_archive(raw, today=TODAY)
    rows[-1]["eventTypes"][0]["fullPath"] = "https://other.test/report.pdf"
    with pytest.raises(FormatDriftError, match="path"):
        spb.parse_report_archive(_encode(rows), today=TODAY)
    with pytest.raises(FormatDriftError, match="no recent"):
        spb.parse_report_archive(_bytes("archive.json"), today=date(2027, 1, 1))


@pytest.mark.parametrize(
    ("ticker", "count", "close"),
    [
        ("MGKL1P1", 217, "7.43"),
        ("XMED1P1", 45, "101.21"),
    ],
)
def test_exact_chart_values_dates_and_not_official_close(
    ticker: str, count: int, close: str
) -> None:
    bars = spb.parse_bars(
        _bytes(f"chart-{ticker}.json"), start=date(2026, 1, 1), end=DAY, today=TODAY
    )
    assert len(bars) == count
    assert bars[-1]["trade_date"] == DAY and bars[-1]["close"] == Decimal(close)
    assert isinstance(bars[-1]["close"], Decimal)
    if ticker == "MGKL1P1":
        assert bars[-1]["close"] != Decimal("7.23")
    assert spb.parse_bars(b"[]", start=DAY, end=DAY, today=TODAY) == []


@pytest.mark.parametrize(
    "change",
    [
        "zero",
        "negative",
        "nan",
        "bool",
        "missing",
        "bounds",
        "timestamp",
        "timestamp_bool",
        "duplicate",
        "range",
        "current_day",
        "shape",
    ],
)
def test_malformed_or_unfinished_chart_is_rejected(change: str) -> None:
    row = _json("chart-MGKL1P1.json")[-1]
    rows = [row]
    if change in {"zero", "negative", "nan", "bool", "missing"}:
        row["close"] = {"zero": 0, "negative": -1, "nan": "NaN", "bool": True, "missing": None}[
            change
        ]
    elif change == "bounds":
        row["high"] = 1
    elif change == "timestamp":
        row["bar_unixtime"] += 1
    elif change == "timestamp_bool":
        row["bar_unixtime"] = True
    elif change == "duplicate":
        rows.append(row)
    elif change == "range":
        row["bar_unixtime"] -= 86400
    elif change == "current_day":
        row["bar_unixtime"] += 86400
    elif change == "shape":
        rows = {"error": "failure"}  # type: ignore[assignment]
    with pytest.raises(FormatDriftError):
        spb.parse_bars(_encode(rows), start=DAY, end=DAY, today=TODAY)


def test_calendar_bounds_current_moscow_date_and_utc_midnight() -> None:
    assert load_range(today=TODAY) == (date(2026, 11, 8), DAY)
    assert load_range(today=TODAY, day=date(2026, 11, 7)) == (date(2026, 11, 7),) * 2
    assert load_range(today=TODAY, date_from=date(2026, 10, 11), date_to=DAY) == (
        date(2026, 10, 11),
        DAY,
    )
    for args in (
        {"day": TODAY},
        {"date_from": DAY},
        {"date_to": DAY},
        {"date_from": DAY, "date_to": date(2026, 11, 9)},
        {"date_from": date(2026, 10, 10), "date_to": DAY},
        {"day": DAY, "date_from": DAY, "date_to": DAY},
    ):
        with pytest.raises(JobParamError):
            load_range(today=TODAY, **args)
    # At 00:30 Moscow, UTC is still yesterday: that UTC day's bar remains unfinished.
    local_today = (
        datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
        .astimezone(__import__("zoneinfo").ZoneInfo("Europe/Moscow"))
        .date()
    )
    assert local_today == TODAY
    with pytest.raises(FormatDriftError):
        spb.parse_bars(_bytes("chart-MGKL1P1.json"), start=date(2026, 1, 1), end=TODAY, today=DAY)
    params = spb.history_params("MGKL1P1", DAY, DAY)
    assert params == {"symbol": "MGKL1P1", "resolution": 1440, "from": 1794268800, "to": 1794355199}


class MockSpb:
    """Normal HttpClient with recorded responses; request bounds really control fixture rows."""

    def __init__(self) -> None:
        self.chart_payload: dict[str, Any] = {}
        self.card_override: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.report_payload = _bytes("unit-reference-2026-11-10.pdf")

    def __call__(self, **kwargs: Any) -> HttpClient:
        return HttpClient(
            transport=httpx.MockTransport(self.handle),
            limiter=RateLimiter(default_rps=None),
            rate_limits={"spbexchange.ru": 10000.0},
            max_retries=0,
            **kwargs,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == spb.UNIVERSE_PATH:
            data = _bytes("russian-market.json")
        elif path == spb.ARCHIVE_PATH:
            data = _bytes("archive.json")
        elif path.startswith("/api/listing/"):
            candidate = next(c for c in _candidates() if str(c.security_id) == path.split("/")[-1])
            data = (
                _encode(self.card_override[candidate.ticker])
                if candidate.ticker in (self.card_override)
                else _bytes(f"card-{candidate.ticker}.json")
            )
        elif path.endswith(".pdf"):
            return httpx.Response(
                200, content=self.report_payload, headers={"content-type": "application/pdf"}
            )
        elif path == spb.HISTORY_PATH:
            ticker = request.url.params["symbol"]
            if ticker in self.chart_payload:
                data = _encode(self.chart_payload[ticker])
            else:
                raw = _json(f"chart-{ticker}.json") if ticker in ("MGKL1P1", "XMED1P1") else []
                lo, hi = (int(request.url.params[k]) for k in ("from", "to"))
                data = _encode([r for r in raw if lo <= r["bar_unixtime"] <= hi])
        else:
            raise AssertionError(f"Unexpected request {request.url}")
        return httpx.Response(200, content=data, headers={"content-type": "application/json"})


@pytest.fixture
def source() -> MockSpb:
    return MockSpb()


def _run(settings: Settings, db: Database, source: MockSpb, **params: Any) -> JobOutcome:
    return run_job("spb_bonds_daily", params, settings=settings, db=db, http_factory=source)


def test_discovery_load_idempotence_corrections_and_provenance(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    first = _run(settings, db, source, day=DAY)
    assert first.status == "succeeded"
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 21}
    assert conn.execute("SELECT count(*) AS n FROM ref.bond").fetchone() == {"n": 0}
    assert conn.execute("SELECT count(*) AS n FROM md.bond_eod").fetchone() == {"n": 0}
    rows = conn.execute("SELECT * FROM md.spb_bond_daily ORDER BY isin").fetchall()
    assert len(rows) == 2
    assert {r["price_unit"] for r in rows} == {"PCT"}
    assert all(r["venue"] == "SPB" and r["price_basis"] == "chart_close" for r in rows)
    xmed = next(r for r in rows if r["isin"] == XMED)
    assert xmed["unit_conflict"] and xmed["card_price_unit"] == "RUB"
    assert xmed["close"] == Decimal("101.21")
    ref = conn.execute(
        "SELECT b.*, c.endpoint FROM ref.spb_bond b "
        "JOIN ops.raw_capture c ON c.id=b.unit_capture_id WHERE b.isin=%s",
        (XMED,),
    ).fetchone()
    assert ref and ref["endpoint"] == "bond_unit_report" and ref["unit_reference_date"] == DAY
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE run_id=%s AND endpoint='bond_unit_report'",
        (first.run_id,),
    ).fetchone() == {"n": 1}
    known = conn.execute("SELECT clock_timestamp() AS ts").fetchone()["ts"]
    conn.commit()
    again = _run(settings, db, source, day=DAY)
    assert conn.execute(
        "SELECT rows_inserted, rows_updated, rows_unchanged FROM ops.job_run WHERE id=%s",
        (again.run_id,),
    ).fetchone() == {"rows_inserted": 0, "rows_updated": 0, "rows_unchanged": 23}
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE "
        "table_name IN ('ref.spb_bond','md.spb_bond_daily')"
    ).fetchone() == {"n": 0}
    conn.commit()
    changed = _json("chart-MGKL1P1.json")[-1]
    changed["close"] = 7.44
    source.chart_payload["MGKL1P1"] = [changed]
    correction = _run(settings, db, source, day=DAY)
    assert conn.execute(
        "SELECT rows_updated FROM ops.job_run WHERE id=%s", (correction.run_id,)
    ).fetchone() == {"rows_updated": 1}
    assert conn.execute(
        "SELECT close FROM md.spb_bond_daily WHERE isin=%s", (MGKL,)
    ).fetchone() == {"close": Decimal("7.44")}
    assert conn.execute(
        "SELECT close FROM md.spb_bond_daily_known_at(%s,%s,%s) WHERE isin=%s",
        (known, DAY, DAY, MGKL),
    ).fetchone() == {"close": Decimal("7.43")}
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE table_name='md.spb_bond_daily'"
    ).fetchone() == {"n": 1}


def test_empty_per_isin_evidence_and_event_calendar_no_synthetic_gaps(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    source.chart_payload["XMED1P1"] = []
    outcome = _run(settings, db, source, date_from=date(2026, 11, 7), date_to=DAY)
    assert outcome.status == "succeeded"
    gaps = conn.execute(
        "SELECT * FROM ops.data_gap WHERE dataset='spb_bonds_daily' AND key->>'isin'=%s", (XMED,)
    ).fetchall()
    assert len(gaps) == 4 and all("no session/trade completeness" in g["note"] for g in gaps)
    assert all(g["status"] != "backfilled" for g in gaps)
    # Another ISIN's rows cannot close XMED's no-data evidence in the generic backfill path.
    reconcile_gaps(
        conn,
        DATASET,
        date(2026, 11, 7),
        DAY,
        run_id=outcome.run_id,
        mark_unfillable=True,
        today=TODAY,
    )
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.data_gap WHERE dataset='spb_bonds_daily' "
        "AND key->>'isin'=%s AND status='backfilled'",
        (XMED,),
    ).fetchone() == {"n": 0}
    check = scan_dataset(conn, DATASET, today=TODAY, calendar=lambda *_: False)
    assert check.gaps == [] and DATASET.calendar is None and not eligible(DATASET)
    (rule,) = RULES[DATASET.name]
    assert rule.days == "all" and rule_applies(rule, date(2026, 11, 7), lambda *_: False)
    task = queue_range(conn, DATASET, date(2026, 9, 1), date(2026, 11, 10))
    assert len(task) == 3 and all((b - a).days < 31 for _, a, b in task)


def test_malformed_metadata_prevents_all_chart_writes(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    card = _json("card-XMED1P1.json")
    card["sgroupSec"] = "Иностранные ценные бумаги"
    source.card_override["XMED1P1"] = card
    with pytest.raises(FormatDriftError):
        _run(settings, db, source, day=DAY)
    assert not any(r.url.path == spb.HISTORY_PATH for r in source.requests)
    assert conn.execute("SELECT count(*) AS n FROM md.spb_bond_daily").fetchone() == {"n": 0}
    assert conn.execute("SELECT status FROM ops.job_run ORDER BY id DESC LIMIT 1").fetchone() == {
        "status": "failed"
    }


def test_malformed_chart_atomic_per_instrument(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    valid = _json("chart-MGKL1P1.json")[-1]
    invalid = {**valid, "close": 0}
    source.chart_payload["MGKL1P1"] = [valid, invalid]
    with pytest.raises(FormatDriftError):
        _run(settings, db, source, day=DAY)
    assert conn.execute("SELECT count(*) AS n FROM md.spb_bond_daily").fetchone() == {"n": 0}
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 21}
