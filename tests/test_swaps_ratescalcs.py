"""Ratescalcs parsers (sanitized recorded pages) and swaps_ratescalcs dedup in PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.swaps  # noqa: F401 - registers the job
from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import ratescalcs as rc

FIX = Path(__file__).parent / "fixtures" / "official"
Conn = psycopg.Connection[DictRow]
FILES = {
    "ruonia_ois": "ratescalcs_ois.html",
    "key_rate_swap": "ratescalcs_krs.html",
    "cny_ruonia_xccy": "ratescalcs_cny_xccy.html",
}


def _page(dataset: str) -> str:
    return (FIX / FILES[dataset]).read_text("utf-8")


def test_parse_ois() -> None:
    c = rc.parse_textarea_page(_page("ruonia_ois"), "ruonia_ois")
    assert c.label.startswith("RUB RUONIA OIS run")
    q = {x.tenor: x for x in c.quotes}
    assert q["1W"].bid_pct == Decimal("13.70") and q["1W"].tenor_days == 7
    assert q["10Y"].tenor_months == 120 and q["10Y"].mid_pct == Decimal("15.68")
    assert [x.tenor for x in c.quotes][:3] == ["1W", "2W", "1M"]


def test_parse_krs_and_xccy() -> None:
    krs = rc.parse_textarea_page(_page("key_rate_swap"), "key_rate_swap")
    assert krs.quotes[0].tenor == "3M" and len(krs.quotes) == 13
    xccy = rc.parse_textarea_page(_page("cny_ruonia_xccy"), "cny_ruonia_xccy")
    q = {x.tenor: x for x in xccy.quotes}
    assert q["ON"].tenor_days == 1 and q["ON"].bid_pct == Decimal("0.1")
    assert q["12M"].tenor_months == 12 and "CNY XCCY" in xccy.label


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("1W   13.70    13.90", "1W   13.95    13.90", "bid/ask"),
        ("2W   13.75    13.95", "1W   13.75    13.95", "duplicate"),
        ("1M   13.83    14.03", "1M   13.83", "invalid quote row"),
        ("RUB RUONIA OIS run", "RUB OIS", "label"),
    ],
)
def test_strict_validation(old: str, new: str, match: str) -> None:
    page = _page("ruonia_ois")
    assert old in page
    with pytest.raises(FormatDriftError, match=match):
        rc.parse_textarea_page(page.replace(old, new), "ruonia_ois")


def test_parse_snapshot() -> None:
    curves = {
        c.dataset: c for c in rc.parse_snapshot((FIX / "ratescalcs_snapshot.json").read_bytes())
    }
    cny = curves["cny_futures_yields"]
    assert cny.source_ts is not None and cny.market_closed is False
    first = cny.quotes[0]
    assert first.tenor == "CRZ6" and first.pillar_date == date(2026, 12, 18)
    assert first.bid_pct is not None and first.ask_pct is not None
    assert first.bid_pct <= first.ask_pct
    assert first.extra is not None and "future_bid" in first.extra
    assert curves["usd_futures_yields"].quotes[0].tenor.startswith("Si")


def test_snapshot_missing_field_fails() -> None:
    data = json.loads((FIX / "ratescalcs_snapshot.json").read_text("utf-8"))
    del data["market_closed"]
    with pytest.raises(FormatDriftError):
        rc.parse_snapshot(json.dumps(data))


def test_content_hash_ignores_clock() -> None:
    a = rc.parse_snapshot((FIX / "ratescalcs_snapshot.json").read_bytes())[0]
    data = json.loads((FIX / "ratescalcs_snapshot.json").read_text("utf-8"))
    data["timestamp"] = "2026-10-16T11:00:00+03:00"
    data["active_users"] = 99
    b = rc.parse_snapshot(json.dumps(data))[0]
    assert rc.content_hash(a) == rc.content_hash(b)


def test_fetch_allow_list() -> None:
    with pytest.raises(ValueError, match="allow-list"):
        rc.fetch(None, "/cnyfuturesyields/admin/reload")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ job


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits={"ratescalcs.ru": 1e6},
        sleep=lambda _s: None,
        max_retries=0,
        **kw,
    )


def _run(settings: Settings, db: Database) -> JobOutcome:
    return run_job("swaps_ratescalcs", {}, settings=settings, db=db, http_factory=_http_factory)


class Site:
    def __init__(self) -> None:
        self.pages = {spec.path: _page(spec.dataset).encode() for spec in rc.PAGES}
        self.snapshot = (FIX / "ratescalcs_snapshot.json").read_bytes()
        self.auth_headers: list[str] = []
        self.requested: list[str] = []


@contextmanager
def _serve(base_url: str) -> Iterator[Site]:
    """The source served at ``base_url`` (pages and snapshot relative to it)."""
    s = Site()
    with respx.mock(assert_all_called=False) as router:
        for path in s.pages:

            def handler(request: httpx.Request, path: str = path) -> httpx.Response:
                s.auth_headers.append(request.headers.get("authorization", ""))
                s.requested.append(str(request.url))
                assert request.method == "GET"
                return httpx.Response(
                    200, content=s.pages[path], headers={"content-type": "text/html"}
                )

            router.get(base_url + path).mock(side_effect=handler)

        def snapshot(request: httpx.Request) -> httpx.Response:
            s.requested.append(str(request.url))
            return httpx.Response(
                200, content=s.snapshot, headers={"content-type": "application/json"}
            )

        router.get(base_url + rc.SNAPSHOT_PATH).mock(side_effect=snapshot)
        yield s


@pytest.fixture
def site() -> Iterator[Site]:
    with _serve(rc.DEFAULT_BASE_URL) as s:
        yield s


@pytest.fixture
def rc_settings(db_url: str) -> Settings:
    return Settings(database_url=db_url, ratescalc_username="u-test", ratescalc_password="p-test")


def test_job_skipped_without_credentials(settings: Settings, db: Database) -> None:
    assert _run(settings, db).status == "skipped"


def test_job_stores_and_dedups(site: Site, rc_settings: Settings, db: Database, conn: Conn) -> None:
    first = _run(rc_settings, db)
    assert first.status == "succeeded", first.result
    assert all(h.startswith("Basic ") for h in site.auth_headers)
    snaps = conn.execute(
        "SELECT dataset, count(*) AS n FROM md.curve_snapshot GROUP BY 1"
    ).fetchall()
    assert {r["dataset"]: r["n"] for r in snaps} == {
        "ruonia_ois": 1,
        "key_rate_swap": 1,
        "cny_ruonia_xccy": 1,
        "cny_futures_yields": 1,
        "usd_futures_yields": 1,
    }
    caps_before = conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone()
    # identical content (snapshot clock changed only): nothing new stored
    data = json.loads(site.snapshot)
    data["timestamp"] = "2026-10-16T12:00:00+03:00"
    site.snapshot = json.dumps(data).encode()
    second = _run(rc_settings, db)
    assert set(second.result.values()) == {"unchanged"}
    assert conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone() == caps_before
    n = conn.execute("SELECT count(*) AS n FROM md.curve_snapshot").fetchone()
    assert n is not None and n["n"] == 5
    run = conn.execute(
        "SELECT rows_unchanged FROM ops.job_run WHERE id = %s", (second.run_id,)
    ).fetchone()
    assert run is not None and run["rows_unchanged"] == 5
    # a changed OIS quote -> one new OIS snapshot only
    path = rc.PAGES[0].path
    site.pages[path] = site.pages[path].replace(b"1W   13.70    13.90", b"1W   13.72    13.90")
    third = _run(rc_settings, db)
    assert third.result["ruonia_ois"].startswith("stored")
    q = conn.execute(
        """SELECT q.bid_pct, q.mid_pct FROM md.curve_quote q
           JOIN md.curve_snapshot s ON s.id = q.snapshot_id
           WHERE s.dataset = 'ruonia_ois' AND q.tenor = '1W' ORDER BY s.fetched_at DESC LIMIT 1"""
    ).fetchone()
    assert q is not None and q["bid_pct"] == Decimal("13.72") and q["mid_pct"] == Decimal("13.81")


def test_bad_page_partial(site: Site, rc_settings: Settings, db: Database, conn: Conn) -> None:
    path = rc.PAGES[1].path
    site.pages[path] = b"<html><body>maintenance</body></html>"
    out = _run(rc_settings, db)
    assert out.status == "partial" and out.result["key_rate_swap"] == "failed"
    assert out.result["ruonia_ois"].startswith("stored")


# ------------------------------------------------------------ the address is a setting (#144)

DEFAULT_URLS = [
    "https://ratescalcs.ru/cnyfuturesyields/cny-xccy-curve",
    "https://ratescalcs.ru/cnyfuturesyields/key-rate-editor/krs",
    "https://ratescalcs.ru/cnyfuturesyields/key-rate-editor/ois",
    "https://ratescalcs.ru/cnyfuturesyields/snapshot",
]


def test_the_default_urls_are_the_ones_of_before(
    site: Site, rc_settings: Settings, db: Database
) -> None:
    """Setting unset: every request goes to the byte-identical address of the former constants."""
    from mdhub.quality import source_probe

    assert rc_settings.ratescalcs_base_url == "https://ratescalcs.ru/cnyfuturesyields"
    assert _run(rc_settings, db).status == "succeeded"
    assert sorted(site.requested) == DEFAULT_URLS
    probe = source_probe._ratescalcs(rc_settings)
    assert probe.url == "https://ratescalcs.ru/cnyfuturesyields/snapshot" and probe.configured


@pytest.mark.parametrize(
    "base",
    [
        "https://ratescalcs.example.invalid",
        "https://ratescalcs.example.invalid/",  # a trailing slash does not double
        "http://203.0.113.7:8080",  # plain http and a bare IP work when the owner sets them
        "http://source.example.invalid:8080/some/prefix",
    ],
)
def test_a_configured_base_is_used_by_every_page_the_snapshot_and_the_probe(
    base: str, db_url: str, db: Database
) -> None:
    from mdhub.core.http import default_rate_limits
    from mdhub.quality import source_probe

    settings = Settings(
        database_url=db_url,
        ratescalc_username="u-test",
        ratescalc_password="p-test",
        ratescalcs_base_url=base,
        ratescalcs_allow_plaintext_credentials=True,  # some of the bases are plain http
    )
    clean = base.rstrip("/")
    assert settings.ratescalcs_base_url == clean
    with _serve(clean) as served:
        out = _run(settings, db)
        assert out.status == "succeeded", out.result
        assert sorted(served.requested) == sorted(
            clean + p for p in (*(spec.path for spec in rc.PAGES), rc.SNAPSHOT_PATH)
        )
        assert all(h.startswith("Basic ") for h in served.auth_headers)
    assert source_probe._ratescalcs(settings).url == clean + "/snapshot"
    host = httpx.URL(clean).host
    assert default_rate_limits(settings)[host] == 1.0  # as polite as the default host


@pytest.mark.parametrize(
    "bad",
    [
        "ratescalcs.example.invalid",  # no scheme
        "ftp://ratescalcs.example.invalid",
        "https://",
        "https://user:secret-pw@ratescalcs.example.invalid/x",
        "https://ratescalcs.example.invalid/x?a=1",
        "https://ratescalcs.example.invalid/x#frag",
        "https://ratescalcs.example.invalid:99999",
        "https://ratescalcs.example.invalid:0",  # port 0
        "https://ratescalcs.example.invalid:/x",  # an empty port
        "https://ratescalcs.example.invalid:",
        "https://[2001:db8::1]:/x",
        "https://ratescalcs .example.invalid",
    ],
)
def test_an_invalid_base_stops_the_start_without_echoing_the_value(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("MDHUB_RATESCALCS_BASE_URL", bad)
    with pytest.raises(ValidationError) as exc:
        Settings()
    text = str(exc.value)
    assert "MDHUB_RATESCALCS_BASE_URL" in text and "secret-pw" not in text
    assert "input_value" not in text  # pydantic's echo of the rejected value is off


def test_an_empty_base_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_RATESCALCS_BASE_URL", "")
    assert Settings().ratescalcs_base_url == "https://ratescalcs.ru/cnyfuturesyields"
