"""OTC Monitor parser (recorded /api/bond/ payload) and swaps_otc_monitor in PostgreSQL."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from apscheduler.triggers.cron import CronTrigger
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

import mdhub.loaders.swaps  # noqa: F401 - registers the job
from mdhub.api.app import create_app
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.legacy.map_md import curve_content_hash
from mdhub.schedule import RULES, rule_applies
from mdhub.sources import otc_monitor as otc
from mdhub.sources import ratescalcs as rc

FIX = Path(__file__).parent / "fixtures" / "otc_monitor" / "bond.json"
URL = otc.BASE_URL + otc.BOND_PATH
TOKEN = "otc-test-token-0123456789abcdef"
Conn = psycopg.Connection[DictRow]


def _rows() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIX.read_text("utf-8"))
    return data


def _row(rows: list[dict[str, Any]], series: str) -> dict[str, Any]:
    return next(r for r in rows if r["series"] == series)


def _curves(rows: list[dict[str, Any]]) -> dict[str, rc.ParsedCurve]:
    parsed = otc.parse_payload(rows)
    return {c.dataset: c for c in parsed.curves}


# ---------------------------------------------------------------------------- parser


def test_parse_all_four_curves() -> None:
    parsed = otc.parse_payload(_rows())
    assert parsed.failures == {}
    curves = {c.dataset: c for c in parsed.curves}
    assert [c.dataset for c in parsed.curves] == [
        "ruonia_ois",
        "key_rate_swap",
        "cny_ruonia_xccy",
        "cny_rusfar_ois",
    ]
    assert {k: len(c.quotes) for k, c in curves.items()} == {
        "ruonia_ois": 17,
        "key_rate_swap": 13,
        "cny_ruonia_xccy": 14,
        "cny_rusfar_ois": 8,
    }
    ois = curves["ruonia_ois"]
    assert [q.tenor for q in ois.quotes][:4] == ["1W", "2W", "1M", "2M"]
    assert [q.position for q in ois.quotes] == list(range(17))
    assert ois.quote_basis == "bid_ask_mid" and ois.label == "OIS RUONIA"
    q6m = {q.tenor: q for q in ois.quotes}["6M"]  # raw "6m OIS RUONIA", provider mid 14.22
    assert (q6m.bid_pct, q6m.ask_pct, q6m.mid_pct) == (
        Decimal("14.15"),
        Decimal("14.35"),
        Decimal("14.25"),
    )
    assert q6m.tenor_months == 6 and q6m.extra is None
    assert {q.tenor: q for q in ois.quotes}["1W"].tenor_days == 7
    assert ois.source_ts == datetime(2026, 10, 16, 16, 49, 55, tzinfo=MSK)  # max date_update
    krs = curves["key_rate_swap"]
    assert krs.quotes[0].tenor == "3M" and krs.quotes[-1].tenor_months == 120
    assert krs.quote_basis == "bid_ask_mid"


def test_parse_mixed_and_mid_only_quotes() -> None:
    curves = _curves(_rows())
    xccy = {q.tenor: q for q in curves["cny_ruonia_xccy"].quotes}
    assert curves["cny_ruonia_xccy"].quote_basis == "mid_only"
    assert xccy["1W"].bid_pct == Decimal("0.6") and xccy["1W"].mid_pct == Decimal("1.0")
    assert xccy["1W"].extra == {"quote_basis": "bid_ask_mid"}
    assert xccy["4M"].bid_pct is None and xccy["4M"].ask_pct is None
    assert xccy["4M"].mid_pct == Decimal("6.25") and xccy["4M"].extra is None
    rusfar = curves["cny_rusfar_ois"]
    assert rusfar.quote_basis == "mid_only" and rusfar.label == "OIS RUSFARCNY"
    assert [q.tenor for q in rusfar.quotes] == ["1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y"]
    two_sided = [q.tenor for q in rusfar.quotes if q.bid_pct is not None]
    assert two_sided == ["6M", "9M", "1Y"]
    q = {x.tenor: x for x in rusfar.quotes}
    assert q["1Y"].mid_pct == Decimal("7.375")  # "1Y OIS RUSFARCNY " (trailing space)
    assert q["1W"].mid_pct == Decimal("0.8") and q["1W"].bid_pct is None
    assert rusfar.source_ts == datetime(2026, 10, 16, 16, 49, 52, tzinfo=MSK)


@pytest.mark.parametrize(
    ("series", "expected"),
    [
        ("6m OIS RUONIA", ("6M", "OIS RUONIA")),
        ("1W  XCCY CNYRUONIA", ("1W", "XCCY CNYRUONIA")),
        ("1Y OIS RUSFARCNY ", ("1Y", "OIS RUSFARCNY")),
        ("26224 vs 4Y KRS", None),
        ("1Y KRS vs 4Y KRS", ("1Y", "KRS vs 4Y KRS")),
        ("РЖД 1Р-27R vs 3Y OIS", None),
        ("ОФЗ 26238", None),
        ("", None),
    ],
)
def test_normalize_series(series: str, expected: tuple[str, str] | None) -> None:
    assert otc.normalize_series(series) == expected


def test_spread_rows_are_not_tenors() -> None:
    rows = _rows()
    spreads = [r for r in rows if " vs " in r["series"]]
    assert {"26224 vs 4Y KRS", "1Y KRS vs 4Y KRS", "РЖД 1Р-27R vs 3Y OIS"} <= {
        r["series"] for r in spreads
    }
    curves = _curves(rows)
    assert sum(len(c.quotes) for c in curves.values()) == 17 + 13 + 14 + 8
    # spread rows, bonds and FX alone yield no curve at all
    only_noise = [
        r for r in rows if otc.normalize_series(r["series"]) is None or " vs " in r["series"]
    ]
    assert set(otc.parse_payload(only_noise).failures) == {s.dataset for s in otc.CURVES}


def _failures(rows: list[dict[str, Any]]) -> dict[str, str]:
    parsed = otc.parse_payload(rows)
    assert len(parsed.curves) + len(parsed.failures) == 4
    return dict(parsed.failures)


def test_missing_tenor_fails_that_curve_only() -> None:
    rows = [r for r in _rows() if r["series"] != "3m OIS RUONIA"]
    fails = _failures(rows)
    assert set(fails) == {"ruonia_ois"} and "missing ['3M']" in fails["ruonia_ois"]


def test_unexpected_or_duplicate_tenor_fails() -> None:
    rows = _rows()
    extra = dict(_row(rows, "9m OIS RUONIA"), series="18M OIS RUONIA")
    assert "unexpected ['18M']" in _failures([*rows, extra])["ruonia_ois"]
    dup = dict(_row(rows, "4Y IRS KeyRate"), series="4y  IRS KeyRate")
    assert "duplicate tenor 4Y" in _failures([*rows, dup])["key_rate_swap"]


@pytest.mark.parametrize(
    ("series", "patch", "match"),
    [
        ("4Y IRS KeyRate", {"bid": 15.3, "offer": 15.19}, "invalid bid/offer"),
        ("4Y IRS KeyRate", {"bid": -0.1}, "invalid bid/offer"),
        ("4Y IRS KeyRate", {"offer": 100.5}, "invalid bid/offer"),
        ("4Y IRS KeyRate", {"offer": None}, "one-sided"),
        ("4Y IRS KeyRate", {"bid": None, "offer": None, "mid": None}, "no bid, offer or mid"),
        ("4Y IRS KeyRate", {"bid": "abc"}, "not a number"),
        ("4Y IRS KeyRate", {"date_update": "2026-10-16 16:49:48"}, "bad date_update"),
    ],
)
def test_bad_quote_fails_closed(series: str, patch: dict[str, Any], match: str) -> None:
    rows = copy.deepcopy(_rows())
    _row(rows, series).update(patch)
    fails = _failures(rows)
    assert set(fails) == {"key_rate_swap"}
    assert match in fails["key_rate_swap"]


def test_cny_curves_accept_negative_and_squeeze_rates() -> None:
    """CNY rates went negative and RUSFARCNY spiked above 200% in 2024: real quotes."""
    rows = copy.deepcopy(_rows())
    _row(rows, "1W  XCCY CNYRUONIA").update(bid=-1.5, offer=-0.9)
    _row(rows, "1W OIS RUSFARCNY").update(mid=-1.2)
    _row(rows, "2W OIS RUSFARCNY").update(mid=212.5)
    curves = _curves(rows)
    assert curves["cny_ruonia_xccy"].quotes[0].mid_pct == Decimal("-1.2")
    assert [q.mid_pct for q in curves["cny_rusfar_ois"].quotes[:2]] == [
        Decimal("-1.2"),
        Decimal("212.5"),
    ]
    _row(rows, "2W OIS RUSFARCNY").update(mid=300.5)
    _row(rows, "1W  XCCY CNYRUONIA").update(bid=-50.5)
    fails = _failures(rows)
    assert "invalid mid for 2W" in fails["cny_rusfar_ois"]
    assert "invalid bid/offer for 1W" in fails["cny_ruonia_xccy"]


def test_case_drift_of_suffix_fails_closed() -> None:
    rows = copy.deepcopy(_rows())
    _row(rows, "4Y IRS KeyRate")["series"] = "4Y IRS KEYRATE"
    assert "missing ['4Y']" in _failures(rows)["key_rate_swap"]


@pytest.mark.parametrize("payload", [{"rows": []}, [1, 2], [{"isin": "x"}]])
def test_malformed_payload_raises(payload: Any) -> None:
    with pytest.raises(FormatDriftError):
        otc.parse_payload(payload)


def test_content_hash_matches_legacy_mapping() -> None:
    """Live hashes equal the legacy ``curve_content_hash`` of the same quotes, so a live
    snapshot identical to the last legacy-mapped one is deduplicated."""
    for curve in _curves(_rows()).values():
        qlist = [
            {
                "tenor": q.tenor,
                "position": q.position,
                "bid_pct": q.bid_pct,
                "ask_pct": q.ask_pct,
                "mid_pct": q.mid_pct,
            }
            for q in curve.quotes
        ]
        assert otc.content_hash(curve) == curve_content_hash(curve.quote_basis, qlist)
    ois = _curves(_rows())["ruonia_ois"]
    assert otc.content_hash(ois) == rc.content_hash(ois)  # two-sided: Ratescalcs hash


def test_content_hash_ignores_clock_but_sees_mid_only_values() -> None:
    rows = copy.deepcopy(_rows())
    a = _curves(rows)
    for r in rows:
        r["date_update"] = "16.10.2026 18:00:00"
    _row(rows, "6m OIS RUONIA")["mid"] = 99  # provider mid of a two-sided quote: ignored
    b = _curves(rows)
    assert all(otc.content_hash(a[k]) == otc.content_hash(b[k]) for k in a)
    _row(rows, "4M  XCCY CNYRUONIA")["mid"] = 6.3  # mid-only quote: part of the content
    c = _curves(rows)
    assert otc.content_hash(c["cny_ruonia_xccy"]) != otc.content_hash(a["cny_ruonia_xccy"])


# ------------------------------------------------------------------------------- job


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits={"otc-monitor.ru": 1e6},
        sleep=lambda _s: None,
        max_retries=0,
        **kw,
    )


def _run(settings: Settings, db: Database) -> JobOutcome:
    return run_job("swaps_otc_monitor", {}, settings=settings, db=db, http_factory=_http_factory)


class Site:
    def __init__(self) -> None:
        self.rows = _rows()
        self.status = 200
        self.auth_headers: list[str] = []
        self.calls = 0


@pytest.fixture
def site() -> Iterator[Site]:
    s = Site()
    with respx.mock(assert_all_called=False) as router:

        def handler(request: httpx.Request) -> httpx.Response:
            s.calls += 1
            s.auth_headers.append(request.headers.get("authorization", ""))
            assert request.method == "GET"
            if s.status != 200:
                return httpx.Response(
                    s.status,
                    json={"detail": "Учетные данные не были предоставлены."},
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                content=json.dumps(s.rows, ensure_ascii=False).encode(),
                headers={"content-type": "application/json; charset=UTF-8"},
            )

        router.get(URL).mock(side_effect=handler)
        yield s


@pytest.fixture
def otc_settings(db_url: str) -> Settings:
    return Settings(database_url=db_url, otc_monitor_token=TOKEN)


def _count(conn: Conn, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row["n"])


def test_job_skipped_without_token(site: Site, settings: Settings, db: Database) -> None:
    out = _run(settings, db)
    assert out.status == "skipped"
    assert site.calls == 0


def test_job_401_fails_without_leaking_token(
    site: Site, otc_settings: Settings, db: Database, conn: Conn
) -> None:
    site.status = 401
    with pytest.raises(Exception, match="HTTP 401"):
        _run(otc_settings, db)
    row = conn.execute("SELECT status, error FROM ops.job_run").fetchone()
    assert row is not None and row["status"] == "failed"
    assert "401" in row["error"] and TOKEN not in row["error"]
    assert _count(conn, "SELECT count(*) AS n FROM md.curve_snapshot") == 0


def test_job_stores_and_dedups(
    site: Site, otc_settings: Settings, db: Database, conn: Conn
) -> None:
    first = _run(otc_settings, db)
    assert first.status == "succeeded", first.result
    assert site.auth_headers == [f"Bearer {TOKEN}"]
    assert first.result == {
        "ruonia_ois": "stored (17 quotes)",
        "key_rate_swap": "stored (13 quotes)",
        "cny_ruonia_xccy": "stored (14 quotes)",
        "cny_rusfar_ois": "stored (8 quotes)",
    }
    caps = conn.execute(
        "SELECT id, source, endpoint, url_redacted, source_ts, parser_version FROM ops.raw_capture"
    ).fetchall()
    assert len(caps) == 1  # one capture per fetch, shared by the four curves
    cap = caps[0]
    assert (cap["source"], cap["endpoint"]) == ("otc_monitor", "bond")
    assert cap["url_redacted"] == URL and TOKEN not in cap["url_redacted"]
    assert cap["source_ts"] == datetime(2026, 10, 16, 16, 49, 55, tzinfo=MSK)
    snaps = {
        r["dataset"]: r
        for r in conn.execute(
            "SELECT dataset, source, quote_basis, source_ts, capture_id, parser_version, label, "
            "snapshot_date FROM md.curve_snapshot"
        ).fetchall()
    }
    assert set(snaps) == {"ruonia_ois", "key_rate_swap", "cny_ruonia_xccy", "cny_rusfar_ois"}
    assert {s["source"] for s in snaps.values()} == {"otc_monitor"}
    assert {s["capture_id"] for s in snaps.values()} == {cap["id"]}
    assert snaps["cny_rusfar_ois"]["quote_basis"] == "mid_only"
    assert snaps["ruonia_ois"]["quote_basis"] == "bid_ask_mid"
    assert snaps["ruonia_ois"]["label"] == "OIS RUONIA"
    rusfar = {
        r["tenor"]: r
        for r in conn.execute(
            """SELECT q.tenor, q.position, q.bid_pct, q.ask_pct, q.mid_pct, q.extra
               FROM md.curve_quote q JOIN md.curve_snapshot s ON s.id = q.snapshot_id
               WHERE s.dataset = 'cny_rusfar_ois'"""
        ).fetchall()
    }
    assert rusfar["2W"]["bid_pct"] is None and rusfar["2W"]["mid_pct"] == Decimal("1")
    assert rusfar["9M"]["mid_pct"] == Decimal("6.5") and rusfar["9M"]["position"] == 6
    assert rusfar["9M"]["extra"] == {"quote_basis": "bid_ask_mid"}

    # unchanged content (clocks moved, provider mid of a two-sided quote changed): no writes
    for r in site.rows:
        r["date_update"] = "16.10.2026 17:05:00"
    _row(site.rows, "6m OIS RUONIA")["mid"] = 14.3
    second = _run(otc_settings, db)
    assert second.status == "succeeded"
    assert set(second.result.values()) == {"unchanged"}
    assert _count(conn, "SELECT count(*) AS n FROM ops.raw_capture") == 1
    assert _count(conn, "SELECT count(*) AS n FROM md.curve_snapshot") == 4
    run = conn.execute(
        "SELECT rows_unchanged FROM ops.job_run WHERE id = %s", (second.run_id,)
    ).fetchone()
    assert run is not None and run["rows_unchanged"] == 4

    # one changed mid-only XCCY quote -> one new XCCY snapshot (and one new capture)
    _row(site.rows, "5M  XCCY CNYRUONIA")["mid"] = 6.75
    third = _run(otc_settings, db)
    assert third.result["cny_ruonia_xccy"] == "stored (14 quotes)"
    assert third.result["ruonia_ois"] == "unchanged"
    assert _count(conn, "SELECT count(*) AS n FROM ops.raw_capture") == 2
    assert _count(conn, "SELECT count(*) AS n FROM md.curve_snapshot") == 5


def test_job_continues_legacy_history(
    site: Site, otc_settings: Settings, db: Database, conn: Conn
) -> None:
    """A legacy-mapped snapshot with the same quotes (hash as the mapper computes it) makes
    the first live poll ``unchanged`` for that dataset."""
    curve = _curves(site.rows)["key_rate_swap"]
    qlist = [
        {"tenor": q.tenor, "position": q.position, "bid_pct": q.bid_pct, "ask_pct": q.ask_pct}
        for q in curve.quotes
    ]
    digest = curve_content_hash("bid_ask_mid", [{**q, "mid_pct": None} for q in qlist])
    conn.execute(
        """INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,
               content_sha256, parser_version, legacy_id)
           VALUES ('otc_monitor', 'key_rate_swap', 'bid_ask_mid', %s, %s, %s, 'legacy_v5',
                   '4209')""",
        (utc_now() - timedelta(minutes=1), utc_now().astimezone(MSK).date(), digest),
    )
    conn.commit()
    out = _run(otc_settings, db)
    assert out.result["key_rate_swap"] == "unchanged"
    assert out.result["ruonia_ois"] == "stored (17 quotes)"


@pytest.mark.parametrize(
    ("dataset", "quotes"),
    [("key_rate_swap", 13), ("cny_rusfar_ois", 8), ("cny_ruonia_xccy", 14)],
)
def test_unchanged_curve_is_stored_once_on_a_new_business_date(
    site: Site, otc_settings: Settings, db: Database, conn: Conn, dataset: str, quotes: int
) -> None:
    """WEB-8/WEB-13: content dedup must not hide a curve from the next day's cut. A curve
    whose quotes did not change since yesterday is stored once more on the first poll of
    today (snapshot_date = today), so the market snapshot of today has the quote set; later
    identical polls of the same day stay ``unchanged``.

    Review §2.5 item 5: OTC Monitor updates CNY OIS vs RUSFAR CNY rarely (source clock 16.10,
    then 20.10 18:50), and before this rule (0.2.0) an unchanged curve was never stored again:
    2 snapshots in a week on production. The CNY curves get their daily copy like the RUB ones."""
    curve = _curves(site.rows)[dataset]
    yesterday = utc_now() - timedelta(days=1)
    conn.execute(
        """INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,
               content_sha256, parser_version)
           VALUES ('otc_monitor', %s, %s, %s, %s, %s, 'test')""",
        (
            dataset,
            curve.quote_basis,
            yesterday,
            yesterday.astimezone(MSK).date(),
            otc.content_hash(curve),
        ),
    )
    conn.commit()
    out = _run(otc_settings, db)
    assert out.result[dataset] == f"stored ({quotes} quotes, unchanged content, new day)"
    today = utc_now().astimezone(MSK).date()
    n = _count(
        conn,
        f"SELECT count(*) AS n FROM md.curve_snapshot WHERE dataset = '{dataset}' "
        f"AND snapshot_date = '{today.isoformat()}'",
    )
    assert n == 1
    again = _run(otc_settings, db)
    assert set(again.result.values()) == {"unchanged"}


def test_job_partial_when_a_curve_fails(
    site: Site, otc_settings: Settings, db: Database, conn: Conn
) -> None:
    site.rows = [r for r in site.rows if r["series"] != "2M OIS RUSFARCNY "]
    _row(site.rows, "7Y IRS KeyRate").update(bid=15.9, offer=15.57)  # bid > offer
    out = _run(otc_settings, db)
    assert out.status == "partial"
    assert out.result["cny_rusfar_ois"] == "failed"
    assert out.result["key_rate_swap"] == "failed"
    assert out.result["ruonia_ois"].startswith("stored")
    stored = {
        r["dataset"] for r in conn.execute("SELECT dataset FROM md.curve_snapshot").fetchall()
    }
    assert stored == {"ruonia_ois", "cny_ruonia_xccy"}
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert err is not None and "missing ['2M']" in err["error"]


def test_job_fails_when_every_curve_fails(
    site: Site, otc_settings: Settings, db: Database, conn: Conn
) -> None:
    site.rows = [r for r in site.rows if otc.normalize_series(r["series"]) is None]
    with pytest.raises(Exception, match="all curves failed"):
        _run(otc_settings, db)
    assert _count(conn, "SELECT count(*) AS n FROM ops.raw_capture") == 0


def test_api_serves_otc_curves(
    site: Site, otc_settings: Settings, db: Database, db_url: str
) -> None:
    assert _run(otc_settings, db).status == "succeeded"
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="tok-otc-123")
    )
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer tok-otc-123"}
        lst = client.get("/v1/curves", headers=auth).json()["data"]
        assert {(r["dataset"], r["source"]) for r in lst} >= {
            ("cny_rusfar_ois", "otc_monitor"),
            ("ruonia_ois", "otc_monitor"),
        }
        r = client.get("/v1/curves/cny_rusfar_ois", params={"source": "otc_monitor"}, headers=auth)
        assert r.status_code == 200, r.text
        data = json.loads(r.content, parse_float=Decimal)["data"]
        assert len(data) == 1 and data[0]["quote_basis"] == "mid_only"
        quotes = {q["tenor"]: q for q in data[0]["quotes"]}
        assert quotes["1W"]["bid_pct"] is None and quotes["1W"]["mid_pct"] == Decimal("0.8")


# -------------------------------------------------------------------------- schedule


def test_schedule_rule() -> None:
    rules = RULES["swaps_otc_monitor"]
    assert all(r.lock_wait_s == 0 and not r.catch_up for r in rules)  # polls
    assert all(r.days == "business" and r.calendar == "MOEX_STOCK" for r in rules)
    assert not rule_applies(rules[0], date(2026, 10, 17), weekday_calendar)  # Saturday
    assert rule_applies(rules[0], date(2026, 10, 16), weekday_calendar)
    fires: list[datetime] = []
    for rule in rules:
        trig = CronTrigger.from_crontab(rule.cron, timezone=MSK)
        t = trig.get_next_fire_time(None, datetime(2026, 10, 16, 0, 0, tzinfo=MSK))
        while t is not None and t.date() == date(2026, 10, 16):
            fires.append(t)
            t = trig.get_next_fire_time(t, t.replace(second=1))
    times = sorted(f.strftime("%H:%M") for f in fires)
    assert times[0] == "09:00" and times[-1] == "20:00" and len(times) == 45
    assert "12:15" in times and "19:45" in times
