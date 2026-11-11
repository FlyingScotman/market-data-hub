"""SPB public source: identity/units, bounded chart history and isolated persistence."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.migrate import discover_migrations, migrate
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, JobParamError, run_job
from mdhub.loaders.spb_bonds import DATASET, load_range
from mdhub.quality.checks import queue_backfill, queue_range, scan_dataset
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


def test_default_range_reaches_back_to_the_last_stored_bar_but_not_past_31_dates() -> None:
    """After a worker outage longer than the three re-read dates the gap is read too."""
    assert load_range(today=TODAY, last_stored=DAY) == (date(2026, 11, 8), DAY)
    assert load_range(today=TODAY, last_stored=date(2026, 11, 9)) == (date(2026, 11, 8), DAY)
    assert load_range(today=TODAY, last_stored=date(2026, 11, 2)) == (date(2026, 11, 3), DAY)
    far = load_range(today=TODAY, last_stored=date(2026, 9, 1))
    assert far == (date(2026, 10, 11), DAY) and (far[1] - far[0]).days + 1 == 31
    assert load_range(today=TODAY, last_stored=None) == (date(2026, 11, 8), DAY)
    # an explicit day or range is taken as given
    assert (
        load_range(today=TODAY, day=date(2026, 11, 7), last_stored=date(2026, 9, 1))
        == (date(2026, 11, 7),) * 2
    )
    assert load_range(
        today=TODAY, date_from=date(2026, 11, 9), date_to=DAY, last_stored=date(2026, 9, 1)
    ) == (date(2026, 11, 9), DAY)


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
        self.extra_rows: list[dict[str, Any]] = []
        self.extra_cards: dict[str, dict[str, Any]] = {}

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
            data = _encode([*_json("russian-market.json"), *self.extra_rows])
        elif path == spb.ARCHIVE_PATH:
            data = _bytes("archive.json")
        elif path.startswith("/api/listing/"):
            security_id = path.split("/")[-1]
            extra = next(
                (r for r in self.extra_rows if str(r["idSecurities"]) == security_id), None
            )
            if extra is not None:
                data = _encode(self.extra_cards[extra["srtsCode"]])
            else:
                candidate = next(c for c in _candidates() if str(c.security_id) == security_id)
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


def _error(conn: Conn, outcome: JobOutcome) -> str:
    conn.rollback()
    row = conn.execute("SELECT error FROM ops.job_run WHERE id=%s", (outcome.run_id,)).fetchone()
    return str(row["error"]) if row and row["error"] else ""


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


def test_day_without_a_bar_is_normal_no_gap_no_backfill_task(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    """Weekends, holidays and a bond that did not trade have no bar: that is not a gap, so the
    quality check queues no backfill task that would rerun the whole load."""
    today_is(TODAY)
    source.chart_payload["XMED1P1"] = []
    outcome = _run(settings, db, source, date_from=date(2026, 11, 7), date_to=DAY)
    assert outcome.status == "succeeded"
    assert outcome.result["instruments"][XMED]["missing_dates"] == 4
    assert outcome.result["instruments"][XMED]["rows"] == 0
    # the empty answer itself is kept as raw evidence, but nothing is recorded as missing
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE run_id=%s AND endpoint='bond_chart_daily'",
        (outcome.run_id,),
    ).fetchone() == {"n": 21}
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.data_gap WHERE dataset='spb_bonds_daily'"
    ).fetchone() == {"n": 0}
    never = lambda *_: False  # noqa: E731 - no exchange calendar for this dataset
    assert queue_backfill(conn, DATASET, never, today=TODAY) == []
    assert conn.execute("SELECT count(*) AS n FROM ops.backfill_task").fetchone() == {"n": 0}
    check = scan_dataset(conn, DATASET, today=TODAY, calendar=never)
    assert check.gaps == [] and DATASET.calendar is None and not eligible(DATASET)
    (rule,) = RULES[DATASET.name]
    # the chart has no bar for the current day, so one run a day (07:40, last three dates) reads
    # everything there is; more runs would only repeat the requests
    assert rule.cron == "40 7 * * *" and rule.days == "all"
    assert rule_applies(rule, date(2026, 11, 7), never)
    task = queue_range(conn, DATASET, date(2026, 9, 1), date(2026, 11, 10))
    assert len(task) == 3 and all((b - a).days < 31 for _, a, b in task)


def _new_issue(source: MockSpb, ticker: str = "NEWISS1") -> str:
    """A valid, well-formed bond that yesterday's full report does not list yet."""
    row = copy.deepcopy(next(r for r in _json("russian-market.json") if r["srtsCode"] == "MGKL1P1"))
    card = _json("card-MGKL1P1.json")
    serial = len(source.extra_rows) + 1
    isin = next(
        f"RU000A10{serial}QQ{k}" for k in range(10) if spb.valid_isin(f"RU000A10{serial}QQ{k}")
    )
    row.update(idSecurities=90000 + serial, srtsCode=ticker)
    card.update(idSecurities=90000 + serial, srtsCode=ticker, sisinCode=isin)
    source.extra_rows.append(row)
    source.extra_cards[ticker] = card
    return isin


def test_new_issue_missing_from_report_is_skipped_others_load(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    new_isin = _new_issue(source)
    outcome = _run(settings, db, source, day=DAY)
    assert outcome.status == "partial"
    assert list(outcome.result["skipped"]) == ["NEWISS1"]
    assert "unit report" in outcome.result["skipped"]["NEWISS1"]
    assert "NEWISS1" in _error(conn, outcome)
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 21}
    assert conn.execute(
        "SELECT count(*) AS n FROM ref.spb_bond WHERE isin=%s", (new_isin,)
    ).fetchone() == {"n": 0}
    assert conn.execute("SELECT count(*) AS n FROM md.spb_bond_daily").fetchone() == {"n": 2}
    assert not any(r.url.params.get("symbol") == "NEWISS1" for r in source.requests)


def test_odd_card_is_skipped_others_load(
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
    outcome = _run(settings, db, source, day=DAY)
    assert outcome.status == "partial" and list(outcome.result["skipped"]) == ["XMED1P1"]
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 20}
    assert conn.execute("SELECT isin FROM md.spb_bond_daily").fetchall() == [{"isin": MGKL}]
    assert not any(r.url.params.get("symbol") == "XMED1P1" for r in source.requests)


def test_malformed_chart_skips_that_bond_only_and_keeps_evidence(
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
    outcome = _run(settings, db, source, day=DAY)
    assert outcome.status == "partial" and list(outcome.result["skipped"]) == ["MGKL1P1"]
    # atomic per instrument: nothing of the malformed array, the other bond is stored
    assert conn.execute("SELECT isin FROM md.spb_bond_daily").fetchall() == [{"isin": XMED}]
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 21}
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE run_id=%s AND endpoint='bond_chart_daily'",
        (outcome.run_id,),
    ).fetchone() == {"n": 21}


def test_bar_outside_requested_dates_skips_the_bond(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    stray = {**_json("chart-MGKL1P1.json")[-1]}
    stray["bar_unixtime"] -= 5 * 86400
    source.chart_payload["MGKL1P1"] = [stray]
    outcome = _run(settings, db, source, day=DAY)
    assert outcome.status == "partial" and "outside" in outcome.result["skipped"]["MGKL1P1"]


def test_many_failures_are_format_drift_and_stop_the_run(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    """21 bonds: 4 skipped is an odd bond or two, 5 is a changed format."""
    today_is(TODAY)
    assert spb.skip_limit(21) == 4 and spb.skip_limit(2) == 1 and spb.skip_limit(5) == 1
    tickers = [c.ticker for c in _candidates()]
    for ticker in tickers[:4]:
        card = _json(f"card-{ticker}.json")
        card["sgroupSec"] = "Иностранные ценные бумаги"
        source.card_override[ticker] = card
    assert _run(settings, db, source, day=DAY).status == "partial"
    source.requests.clear()
    card = _json(f"card-{tickers[4]}.json")
    card["sgroupSec"] = "Иностранные ценные бумаги"
    source.card_override[tickers[4]] = card
    with pytest.raises(FormatDriftError, match="5 of 21 bonds failed validation"):
        _run(settings, db, source, day=DAY)
    assert not any(r.url.path == spb.HISTORY_PATH for r in source.requests)
    assert conn.execute("SELECT status FROM ops.job_run ORDER BY id DESC LIMIT 1").fetchone() == {
        "status": "failed"
    }


def test_chart_drift_in_most_bonds_fails_the_run(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    for candidate in _candidates()[:6]:
        source.chart_payload[candidate.ticker] = {"error": "format changed"}
    with pytest.raises(FormatDriftError, match="failed validation"):
        _run(settings, db, source, day=DAY)
    assert conn.execute("SELECT status FROM ops.job_run ORDER BY id DESC LIMIT 1").fetchone() == {
        "status": "failed"
    }


def test_report_unit_row_drift_still_stops_the_run(
    settings: Settings,
    db: Database,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    source.report_payload = b"not a PDF"
    with pytest.raises(FormatDriftError, match="unreadable"):
        _run(settings, db, source, day=DAY)


def test_report_without_the_discovered_bonds_is_drift(
    settings: Settings,
    db: Database,
    source: MockSpb,
    monkeypatch: pytest.MonkeyPatch,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    monkeypatch.setattr(spb, "parse_unit_report", lambda *_a, **_kw: {})
    with pytest.raises(FormatDriftError, match=r"5 of 21 bonds failed validation \(limit 4\)"):
        _run(settings, db, source, day=DAY)
    assert not any(r.url.path == spb.HISTORY_PATH for r in source.requests)


def test_report_is_downloaded_once_per_report_date(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    """A rerun on the same day (retry, backfill task, Run now) reads the units it already
    validated against the dated report; the request count is the cheap part of the run."""
    today_is(TODAY)

    def pdfs() -> int:
        return sum(r.url.path.endswith(".pdf") for r in source.requests)

    first = _run(settings, db, source, day=DAY)
    assert first.result["report_downloaded"] is True and pdfs() == 1
    # universe + 21 cards + archive (one year) + report + 21 charts
    assert first.result["requests"] == len(source.requests) == 1 + 21 + 1 + 1 + 21
    unit_capture = conn.execute(
        "SELECT unit_capture_id FROM ref.spb_bond WHERE isin=%s", (MGKL,)
    ).fetchone()
    conn.commit()
    source.requests.clear()
    again = _run(settings, db, source, day=DAY)
    assert again.status == "succeeded" and pdfs() == 0
    assert again.result["report_downloaded"] is False
    assert again.result["requests"] == len(source.requests) == 1 + 21 + 1 + 21
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 21}
    assert (
        conn.execute("SELECT unit_capture_id FROM ref.spb_bond WHERE isin=%s", (MGKL,)).fetchone()
        == unit_capture
    )
    assert conn.execute(
        "SELECT rows_inserted, rows_updated, rows_unchanged FROM ops.job_run WHERE id=%s",
        (again.run_id,),
    ).fetchone() == {"rows_inserted": 0, "rows_updated": 0, "rows_unchanged": 23}
    assert conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE "
        "table_name IN ('ref.spb_bond','md.spb_bond_daily')"
    ).fetchone() == {"n": 0}
    conn.commit()
    # a changed card (settlement currency) is not trusted: the report is read again, and the
    # bond whose card now contradicts the report is skipped
    card = _json("card-MGKL1P1.json")
    card["scurrencyCalc"] = "USD"
    source.card_override["MGKL1P1"] = card
    source.requests.clear()
    third = _run(settings, db, source, day=DAY)
    assert pdfs() == 1 and third.status == "partial"
    assert "settlement currency" in third.result["skipped"]["MGKL1P1"]


def test_switched_off_job_and_no_request(
    db_url: str,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    off = Settings(database_url=db_url, enable_spb_bonds=False)
    outcome = _run(off, db, source, day=DAY)
    assert outcome.status == "skipped" and "MDHUB_ENABLE_SPB_BONDS" in _error(conn, outcome)
    assert source.requests == []
    assert conn.execute("SELECT count(*) AS n FROM ref.spb_bond").fetchone() == {"n": 0}
    assert Settings(database_url=db_url).enable_spb_bonds is True


SPB_MIGRATIONS = {
    "0130_spb_bond_reference",
    "0290_spb_bond_daily",
    "0570_spb_bond_known_at",
    "0680_spb_job_source",
    "0690_spb_licence_note",
}


def test_upgrade_from_main_applies_only_the_spb_migrations_with_role_grants(
    template_db: str, empty_db_url: str, tmp_path: Path
) -> None:
    """A database at ``main`` (no SPB migration) takes exactly the SPB files, and the read API
    role (it reads md and ref schema-wide, migration 0800) sees the new tables. ``template_db``
    creates the service roles of the test cluster. The read-only role gets its SELECT from
    ``deploy/sql/grant_readonly.sql`` for every table, so the new ones need nothing of their own."""
    main_dir = tmp_path / "main"
    main_dir.mkdir()
    for m in discover_migrations():
        if m.version not in SPB_MIGRATIONS:
            shutil.copy(m.path, main_dir / m.path.name)
    assert len(list(main_dir.glob("*.sql"))) == len(discover_migrations()) - len(SPB_MIGRATIONS)
    migrate(empty_db_url, directory=main_dir)
    report = migrate(empty_db_url)
    assert report.applied == sorted(SPB_MIGRATIONS)
    with psycopg.connect(make_conninfo(empty_db_url, user="mdhub_api")) as c:
        for table in ("ref.spb_bond", "md.spb_bond_daily"):
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert c.execute(
            "SELECT count(*) FROM md.spb_bond_daily_known_at(now(), %s, %s)", (DAY, DAY)
        ).fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM md.spb_bond_daily")
    with psycopg.connect(empty_db_url) as owner:
        note = owner.execute("SELECT licence_note FROM ops.source WHERE code = 'spb'").fetchone()
    assert note and "2026-11-11" in note[0] and "Chart close is not official" in note[0]


def _bar(day: date, close: float) -> dict[str, Any]:
    stamp = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    return {
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "bar_unixtime": stamp,
    }


def test_default_run_reads_from_the_day_after_the_last_stored_bar(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    today_is(TODAY)
    assert _run(settings, db, source, day=date(2026, 11, 5)).status == "succeeded"
    conn.commit()
    source.requests.clear()
    outcome = _run(settings, db, source)  # the worker was down since 5 November
    assert (outcome.result["from"], outcome.result["to"]) == ("2026-11-06", "2026-11-10")
    sent = next(r.url.params for r in source.requests if r.url.params.get("symbol") == "MGKL1P1")
    assert int(sent["from"]) == spb.history_params("MGKL1P1", date(2026, 11, 6), DAY)["from"]
    assert conn.execute(
        "SELECT array_agg(trade_date ORDER BY trade_date) AS d FROM md.spb_bond_daily "
        "WHERE isin=%s",
        (MGKL,),
    ).fetchone() == {"d": [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), DAY]}


def test_no_bar_for_any_bond_on_a_weekday_is_partial_but_a_weekend_is_not(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    today_is: Callable[[date], None],
) -> None:
    """A changed request parameter would answer ``[]`` for every bond, every day."""
    today_is(TODAY)
    for candidate in _candidates():
        source.chart_payload[candidate.ticker] = []
    weekday = _run(settings, db, source, day=DAY)
    assert weekday.status == "partial" and weekday.result["rows"] == 0
    assert "no bars for any bond in range 2026-11-10..2026-11-10" in _error(conn, weekday)
    weekend = _run(settings, db, source, date_from=date(2026, 11, 7), date_to=date(2026, 11, 8))
    assert weekend.status == "succeeded" and weekend.result["rows"] == 0
    # a weekday inside a longer range counts
    mixed = _run(settings, db, source, date_from=date(2026, 11, 7), date_to=date(2026, 11, 9))
    assert mixed.status == "partial"


def test_every_admitted_bond_failing_its_chart_is_format_drift(
    settings: Settings,
    db: Database,
    source: MockSpb,
    monkeypatch: pytest.MonkeyPatch,
    today_is: Callable[[date], None],
) -> None:
    """With a tiny universe one failing chart stays within the skip limit; all of them do not."""
    today_is(TODAY)
    original = spb.parse_universe
    monkeypatch.setattr(spb, "parse_universe", lambda payload: original(payload)[:1])
    ticker = _candidates()[0].ticker
    source.chart_payload[ticker] = {"error": "format changed"}
    with pytest.raises(FormatDriftError, match="charts of all 1 admitted bonds failed validation"):
        _run(settings, db, source, day=DAY)


def _changed_unit(monkeypatch: pytest.MonkeyPatch, ticker: str, unit: str) -> None:
    original = spb.parse_unit_report

    def patched(*args: Any, **kwargs: Any) -> dict[tuple[str, str], tuple[str, str]]:
        out = original(*args, **kwargs)
        key = next(k for k in out if k[0] == ticker)
        out[key] = (unit, out[key][1])
        return out

    monkeypatch.setattr(spb, "parse_unit_report", patched)


def test_a_changed_unit_leaves_stored_bars_alone_and_writes_no_unproven_price(
    settings: Settings,
    db: Database,
    conn: Conn,
    source: MockSpb,
    monkeypatch: pytest.MonkeyPatch,
    today_is: Callable[[date], None],
) -> None:
    """MGKL1P1 is in percent of par in the report of 9 November and in RUB in the next one."""
    today_is(date(2026, 11, 10))
    source.report_payload = _bytes("unit-reference-2026-11-09.pdf")
    first = _run(settings, db, source, date_from=date(2026, 11, 2), date_to=date(2026, 11, 9))
    assert first.status == "succeeded"
    conn.commit()

    def bars() -> dict[date, Any]:
        return {
            r["trade_date"]: r
            for r in conn.execute(
                "SELECT trade_date, price_unit, unit_reference_date, close FROM md.spb_bond_daily "
                "WHERE isin=%s",
                (MGKL,),
            ).fetchall()
        }

    before = bars()
    assert {b["price_unit"] for b in before.values()} == {"PCT"}
    assert {b["unit_reference_date"] for b in before.values()} == {date(2026, 11, 9)}

    # the next morning: the report of 10 November says RUB; the chart also has a bar for a
    # date never stored (8 November), a different number for 9 November (it may be a restated
    # RUB price) and the new date (10 November)
    today_is(TODAY)
    source.report_payload = _bytes("unit-reference-2026-11-10.pdf")
    _changed_unit(monkeypatch, "MGKL1P1", "RUB")
    source.chart_payload["MGKL1P1"] = [
        _bar(date(2026, 11, 8), 7.0),
        _bar(date(2026, 11, 9), 748.0),
        _bar(DAY, 7.43),
    ]
    second = _run(settings, db, source)  # default range: 8..10 November
    assert (second.result["from"], second.result["to"]) == ("2026-11-08", "2026-11-10")
    assert second.status == "partial"  # the run that saw the change
    note = second.result["unit_changes"]["MGKL1P1"]
    assert note.startswith("price unit changed PCT->RUB for MGKL1P1")
    assert note in _error(conn, second)
    assert second.result["unit_frozen"][MGKL] == {
        "ticker": "MGKL1P1",
        "stored_unit": "PCT",
        "current_unit": "RUB",
        "frozen_through": "2026-11-09",
        "new_unit_from": "2026-11-10",
        "dates_not_written": 2,  # 8 and 9 November
    }
    after = bars()
    for day in before:  # nothing stored before was touched: not the price, not the label
        assert after[day] == before[day]
    assert date(2026, 11, 8) not in after  # a date of the old unit never stored: not written now
    assert after[DAY]["price_unit"] == "RUB" and after[DAY]["unit_reference_date"] == DAY
    assert after[DAY]["close"] == Decimal("7.43")
    ref = conn.execute(
        "SELECT price_unit, unit_reference_date FROM ref.spb_bond WHERE isin=%s", (MGKL,)
    ).fetchone()
    assert ref == {"price_unit": "RUB", "unit_reference_date": DAY}
    conn.commit()

    # a historical run over the old range (with a corrected close) writes nothing for old dates
    source.chart_payload["MGKL1P1"] = [_bar(date(2026, 11, 6), 9.5), _bar(date(2026, 11, 9), 7.5)]
    third = _run(settings, db, source, date_from=date(2026, 11, 2), date_to=date(2026, 11, 9))
    assert third.status == "succeeded"  # the change was already acted on: no partial every time
    assert third.result["unit_changes"]["MGKL1P1"] == note
    assert third.result["unit_frozen"][MGKL]["dates_not_written"] == 2
    assert bars() == after
