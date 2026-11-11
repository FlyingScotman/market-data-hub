"""The configured Ratescalcs address is in no table that the read-only role (or the read API) can
read (market-data-hub#144, third review of #142).

The read-only role ``mdhub_ro`` (``deploy/sql/grant_readonly.sql``) reads every schema but
``admin``, so ``ops.raw_capture.url_redacted``, ``ops.source_probe``, ``ops.backfill_task``,
``ops.job_request`` ... are as public as ``/v1/runs``. The tests run the loader, the probe, a
console request and a backfill task against an address that must stay secret, then read **every
column of every table and view** the role can read (a row cast to text) and look for the address:
a column added later is covered without a change here. The full text is only in the worker log.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

import mdhub.loaders.swaps
import mdhub.quality.source_probe  # noqa: F401 - registers the job
from mdhub.config import BASE_URL_PLACEHOLDER, SERVER_NAME_PLACEHOLDER, Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.capture import MAX_ERROR_CHARS, job_run
from mdhub.core.errors import MdhubError
from mdhub.core.http import (
    EntitlementError,
    HttpClient,
    HttpStatusError,
    RateLimiter,
    RedirectError,
    stored_message,
)
from mdhub.core.redact import hide_values, redact_url, stored_text, stored_url
from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.job_requests import claim_request, run_request
from mdhub.jobs import REGISTRY, JobContext, JobRegistry
from mdhub.quality.backfill import MAX_ATTEMPTS
from mdhub.quality.job import catch_up_missed
from mdhub.quality.report import QualityReport, store_report
from mdhub.quality.source_probe import _MAX_ERROR, PROBES, probe
from mdhub.schedule import Rule
from mdhub.sources import ratescalcs as rc
from test_backfill import Conn, Source, seed_gaps, source, work  # noqa: F401 - the fixture
from test_ratescalcs_address import _probe, _s
from test_ratescalcs_address import client as client
from test_swaps_ratescalcs import _run, _serve

HOST = "198.51.100.7"
PORT = "8443"
PREFIX = "zq-prefix-77"
BASE = f"https://{HOST}:{PORT}/{PREFIX}"
NAME = "rc-tls-name.example.invalid"
TARGET = "other-internal.example.invalid"  # where the server redirects to
FOOTER_HOST = "rc-footer-host.example.invalid"  # named by the error page of the server
CONFIGURED = (HOST, PORT, PREFIX, NAME)  # what the owner set
SECRET = (*CONFIGURED, TARGET, FOOTER_HOST)  # and what the server answers with
RO_ROLE = "mdhub_ro_scan"


# ------------------------------------------------------------- what the read-only role can read


def _grant_like_grant_readonly(conn: Conn) -> None:
    """The grants of ``deploy/sql/grant_readonly.sql`` to a role of this test."""
    conn.execute(
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name}) "
            "THEN CREATE ROLE {role} LOGIN; END IF; END $$"
        ).format(name=sql.Literal(RO_ROLE), role=sql.Identifier(RO_ROLE))
    )
    schemas = [
        r["nspname"]
        for r in conn.execute(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname NOT LIKE 'pg\\_%' AND nspname NOT IN ('information_schema', 'admin')"
        ).fetchall()
    ]
    for schema in schemas:
        for stmt in (
            "GRANT USAGE ON SCHEMA {s} TO {r}",
            "GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r}",
            "GRANT SELECT ON ALL SEQUENCES IN SCHEMA {s} TO {r}",
        ):
            conn.execute(sql.SQL(stmt).format(s=sql.Identifier(schema), r=sql.Identifier(RO_ROLE)))
    conn.commit()


def test_the_grants_of_this_test_mirror_the_deploy_script() -> None:
    script = (Path(__file__).parent.parent / "deploy" / "sql" / "grant_readonly.sql").read_text()
    assert "NOT IN ('information_schema', 'admin')" in script
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA" in script and ":'ro_user'" in script


def readable_text(db_url: str, conn: Conn) -> dict[str, str]:
    """``{schema.relation: all rows as text}`` of every table / view / materialized view the
    read-only role can select from (every column: a row cast to text, so jsonb, arrays and
    columns added later are covered)."""
    _grant_like_grant_readonly(conn)
    out: dict[str, str] = {}
    with psycopg.connect(make_conninfo(db_url, user=RO_ROLE)) as ro:
        relations = ro.execute(
            """
            SELECT n.nspname, c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm') AND n.nspname NOT LIKE 'pg\\_%%'
              AND n.nspname <> 'information_schema'
              AND has_table_privilege(c.oid, 'SELECT')
            ORDER BY 1, 2
            """
        ).fetchall()
        for schema, name in relations:
            rows = ro.execute(
                sql.SQL("SELECT t::text FROM {}.{} t").format(
                    sql.Identifier(schema), sql.Identifier(name)
                )
            ).fetchall()
            out[f"{schema}.{name}"] = "\n".join(r[0] for r in rows)
    return out


def leaks(db_url: str, conn: Conn, secret: tuple[str, ...] = SECRET) -> list[str]:
    """``relation: term`` for every term of ``secret`` found in a readable table."""
    return [
        f"{relation}: {term}"
        for relation, text in readable_text(db_url, conn).items()
        for term in secret
        if term.lower() in text.lower()
    ]


def test_the_scan_sees_every_schema_but_admin_and_finds_a_planted_value(
    db_url: str, conn: Conn
) -> None:
    text = readable_text(db_url, conn)
    assert {"ops.raw_capture", "ops.source_probe", "ops.backfill_task", "ops.job_request"} <= set(
        text
    )
    assert "ops.v_open_gaps" in text and "md.curve_snapshot" in text and "ref.bond" in text
    assert not any(name.startswith("admin.") for name in text)
    conn.execute(
        "INSERT INTO ops.job_run (job, status, error, finished_at) "
        "VALUES ('planted', 'failed', %s, now())",
        (f"cannot reach {BASE}/x",),
    )
    conn.commit()
    assert any(item.startswith("ops.job_run:") for item in leaks(db_url, conn))
    assert any(item.startswith("ops.job_run:") for item in leaks(db_url, conn, (PREFIX,)))


# ------------------------------------------------------------------------------ the scenarios


def _settings(db_url: str, **kw: Any) -> Settings:
    kw.setdefault("ratescalcs_base_url", BASE)
    kw.setdefault("ratescalcs_tls_server_name", NAME)
    return _s(db_url, **kw)


def _fail_loader(router: respx.MockRouter, response: httpx.Response) -> None:
    router.route(host=HOST).mock(return_value=response)


ERROR_PAGE = f"<address>Apache Server at {FOOTER_HOST} Port 443</address>"


def test_no_table_the_read_only_role_reads_holds_the_address(
    db_url: str, db: Database, conn: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    s = _settings(db_url)
    with caplog.at_level(logging.INFO):
        # the loader works: raw captures with their URLs
        with _serve(BASE) as served:
            assert _run(s, db).status == "succeeded"
        assert served.requested
        # the probe works and fails (status, redirect to a name of the server, error page)
        with respx.mock(assert_all_called=False) as router:
            router.route(host=HOST).mock(return_value=httpx.Response(200, json={}))
            _probe(db_url, db, conn, s)
        with respx.mock(assert_all_called=False) as router:
            _fail_loader(router, httpx.Response(503, text=ERROR_PAGE))
            _probe(db_url, db, conn, s)
            with pytest.raises(Exception, match="all pages failed"):
                _run(s, db)
        with respx.mock(assert_all_called=False) as router:
            _fail_loader(router, httpx.Response(302, headers={"location": f"https://{TARGET}/x"}))
            _probe(db_url, db, conn, s)
            with pytest.raises(Exception, match="all pages failed"):
                _run(s, db)
        with respx.mock(assert_all_called=False) as router:
            _fail_loader(router, httpx.Response(200, text=ERROR_PAGE))  # HTML, not the snapshot
            with pytest.raises(Exception, match="all pages failed"):
                _run(s, db)
        with respx.mock(assert_all_called=False) as router:
            router.route(host=HOST).mock(side_effect=httpx.ConnectError("boom"))
            _probe(db_url, db, conn, s)
            with pytest.raises(Exception, match="all pages failed"):
                _run(s, db)
    conn.rollback()

    rows = conn.execute("SELECT url_redacted FROM ops.raw_capture WHERE source = 'ratescalcs'")
    urls = {r["url_redacted"] for r in rows.fetchall()}
    assert urls == {
        BASE_URL_PLACEHOLDER + path for path in [rc.SNAPSHOT_PATH, *(p.path for p in rc.PAGES)]
    }
    probes = conn.execute(
        "SELECT endpoint, error FROM ops.source_probe WHERE source = 'ratescalcs'"
    )
    probe_rows = probes.fetchall()
    assert len(probe_rows) == 4
    assert {r["endpoint"] for r in probe_rows} == {BASE_URL_PLACEHOLDER + rc.SNAPSHOT_PATH}
    errors = [r["error"] for r in probe_rows if r["error"]]
    assert any("HTTP 503" in e for e in errors)
    assert any(e.startswith("RedirectError: refusing cross-host redirect from ") for e in errors)
    runs = conn.execute(
        "SELECT error FROM ops.job_run WHERE job = 'swaps_ratescalcs' AND error IS NOT NULL"
    ).fetchall()
    assert any("HTTP 503" in r["error"] for r in runs)
    assert any("refusing cross-host redirect from" in r["error"] for r in runs)

    assert leaks(db_url, conn) == []
    # the log has the full text for the owner
    assert HOST in caplog.text and TARGET in caplog.text and FOOTER_HOST in caplog.text


def test_a_console_request_does_not_store_the_address(
    db_url: str, db: Database, conn: Conn
) -> None:
    reg = JobRegistry()

    def boom(ctx: JobContext) -> None:
        raise RuntimeError(f"cannot reach {BASE}/snapshot, certificate for '{NAME}'")

    reg.register("boom_job", "fails", boom)
    request_id = conn.execute(
        "INSERT INTO ops.job_request (job) VALUES ('boom_job') RETURNING id"
    ).fetchone()
    assert request_id is not None
    conn.commit()
    req = claim_request(db, uuid.uuid4())
    assert req is not None
    state = run_request(
        req, settings=_settings(db_url), db=db, calendar=weekday_calendar, registry=reg
    )
    assert state == "failed"
    row = conn.execute(
        "SELECT error FROM ops.job_request WHERE id = %s", (request_id["id"],)
    ).fetchone()
    assert row is not None and "cannot reach" in row["error"]
    assert BASE_URL_PLACEHOLDER in row["error"] and SERVER_NAME_PLACEHOLDER in row["error"]
    assert leaks(db_url, conn) == []


def test_the_default_public_address_is_not_stored_either(
    db_url: str, db: Database, conn: Conn
) -> None:
    s = _s(db_url)
    with _serve(rc.DEFAULT_BASE_URL):
        assert _run(s, db).status == "succeeded"
    with respx.mock(assert_all_called=False) as router:
        router.route(host="ratescalcs.ru").mock(return_value=httpx.Response(503, text="down"))
        _probe(db_url, db, conn, s)
    conn.rollback()
    texts = readable_text(db_url, conn)
    for relation in ("ops.raw_capture", "ops.source_probe", "ops.job_run"):
        assert "cnyfuturesyields" not in texts[relation], relation
        # (ops.source lists the public host of the source by its seed row)
    assert "ratescalcs.ru" not in texts["ops.raw_capture"] + texts["ops.source_probe"]
    rows = conn.execute("SELECT endpoint FROM ops.source_probe WHERE source = 'ratescalcs'")
    assert {r["endpoint"] for r in rows.fetchall()} == {BASE_URL_PLACEHOLDER + rc.SNAPSHOT_PATH}


# ------------------------------------------------------------ backfill (market-data-hub#144 step 4)


def test_a_failed_backfill_task_does_not_store_or_serve_the_address(
    source: Source,  # noqa: F811
    db_url: str,
    db: Database,
    conn: Conn,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source.error = f"cannot reach {BASE}/snapshot: certificate for '{NAME}'"
    s = _settings(db_url)
    seed_gaps(conn)
    source.fail = True
    with caplog.at_level(logging.INFO):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            conn.execute("UPDATE ops.backfill_task SET not_before = now() - interval '1 second'")
            conn.commit()
            assert work(s, db).processed == 1, attempt
    conn.rollback()
    task = conn.execute("SELECT status, last_error FROM ops.backfill_task").fetchone()
    assert task is not None and task["status"] == "failed"
    assert "cannot reach" in task["last_error"] and BASE_URL_PLACEHOLDER in task["last_error"]
    notes = conn.execute("SELECT DISTINCT note FROM ops.data_gap WHERE status = 'open'").fetchall()
    assert len(notes) == 1 and "failed after" in notes[0]["note"]
    assert BASE_URL_PLACEHOLDER in notes[0]["note"]
    served = client.get("/v1/gaps", headers={"Authorization": "Bearer tok-alpha-123"}).text
    assert "failed after" in served and BASE_URL_PLACEHOLDER in served
    assert not [t for t in CONFIGURED if t.lower() in served.lower()]
    assert leaks(db_url, conn) == []
    assert BASE in caplog.text  # the run's own log line keeps the full text


# ----------------------------------------------------------------------------- spellings (step 2)


def _fragments(base: str) -> list[str]:
    """What must not survive in a text: host(s), the explicit port and the path prefix."""
    url = httpx.URL(base)
    parts = [url.host, url.netloc.decode().split(":")[0], url.path.strip("/")]
    if url.port is not None:
        parts.append(f":{url.port}")
    return [p for p in parts if p]


@pytest.mark.parametrize(
    "base",
    [
        "https://198.51.100.7:443/prefix",  # the default port: httpx drops it
        "https://198.51.100.7:8443/prefix",
        "https://198.51.100.7/prefix/deeper",
        "https://198.51.100.7",
        "HTTPS://RC.Example.COM:8443/Prefix",
        "https://rc.example.com:443/pfx",
        "http://rc.example.com:80/pfx",
        "https://[2001:DB8::1]:8443/pfx",
        "https://[2001:db8:0:0:0:0:0:1]/pfx",
        "https://[2001:db8::1]/pfx",
        "https://тест.example/pfx",  # an IDN host shows up as xn--e1aybc.example
        "https://xn--e1aybc.example:8443/pfx",
        "https://rc.example.com/p%20x",
        "https://rc.example.com/путь",
    ],
)
def test_every_spelling_of_the_address_is_hidden(base: str) -> None:
    s = Settings(ratescalcs_base_url=base)
    url = s.ratescalcs_base_url
    request_url = httpx.URL(url + rc.SNAPSHOT_PATH)  # what the client sends and reports
    spelled = [
        f"HTTP 503 for {redact_url(request_url)}: down",  # the request URL, httpx-normalized
        f"HTTP 503 for {url}{rc.SNAPSHOT_PATH}: down",  # as set
        f"HTTP 503 for {url}/{rc.SNAPSHOT_PATH}",
        f"unreachable: {request_url.host} ({request_url.netloc.decode()})",
        f"getaddrinfo failed for {urlparse_host(url)}",
        f"http://{request_url.netloc.decode()}{request_url.path}",  # another scheme
        f"https://{request_url.netloc.decode().upper()}{request_url.path.upper()}",
    ]
    for text in spelled:
        out = hide_values(text, s.hidden_addresses())
        low = out.lower()
        # the prefix may not stay next to the placeholder; the page path may
        for fragment in _fragments(url):
            assert fragment.lower() not in low, (text, out)
        assert BASE_URL_PLACEHOLDER in out, (text, out)
    # the stored URL of the page: the placeholder and the page, nothing else
    assert stored_url(request_url, s) == BASE_URL_PLACEHOLDER + rc.SNAPSHOT_PATH


def urlparse_host(url: str) -> str:
    return httpx.URL(url).host


def test_the_path_prefix_is_hidden_with_the_host_but_not_alone() -> None:
    s = Settings(ratescalcs_base_url="https://198.51.100.7:8443/zq-prefix")
    # a spelling of the base URL that no form lists (another scheme): host and prefix go together
    out = hide_values("fetch http://198.51.100.7:8443/zq-prefix/snapshot", s.hidden_addresses())
    assert out == f"fetch http://{BASE_URL_PLACEHOLDER}/snapshot"
    # the prefix alone is not secret by itself (it would hit unrelated words)
    assert hide_values("zq-prefix", s.hidden_addresses()) == "zq-prefix"
    # the empty and the root prefix add nothing but the host forms
    for base in ("https://198.51.100.7", "https://198.51.100.7/"):
        forms = Settings(ratescalcs_base_url=base).hidden_addresses()
        assert not any(v.endswith("/") for v, _ in forms)
        assert hide_values("GET https://198.51.100.7/snapshot", forms) == (
            f"GET {BASE_URL_PLACEHOLDER}/snapshot"
        )


def test_the_tls_server_name_is_hidden_in_unicode_and_punycode() -> None:
    s = Settings(
        ratescalcs_base_url="https://192.0.2.10", ratescalcs_tls_server_name="xn--e1aybc.example"
    )
    for spelling in ("xn--e1aybc.example", "XN--E1AYBC.EXAMPLE", "тест.example"):
        assert hide_values(f"valid for '{spelling}'", s.hidden_addresses()) == (
            f"valid for '{SERVER_NAME_PLACEHOLDER}'"
        )


SPELLED = "<X>"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("ratescalcs.example.invalid", "see ratescalc\u017f.example.invalid now"),  # long s
        ("ratescalcs.example.invalid", "see RATESCALC\u017f.EXAMPLE.INVALID now"),
        ("istanbul.example.invalid", "see \u0130STANBUL.example.invalid now"),  # dotted I
        ("\u0130stanbul.example.invalid", "see istanbul.example.invalid now"),
        ("stra\u00dfe.example.invalid", "see STRA\u1e9eE.example.invalid now"),  # sharp s
        ("STRA\u1e9eE.example.invalid", "see stra\u00dfe.example.invalid now"),
        ("kelvin.example.invalid", "see \u212aELVIN.example.invalid now"),  # Kelvin sign
        ("\u212aelvin.example.invalid", "see kelvin.example.invalid now"),
    ],
)
def test_hide_values_does_not_raise_for_letters_that_fold_differently_than_lower(
    value: str, text: str
) -> None:
    out = hide_values(text, [(value, SPELLED)])
    assert out == f"see {SPELLED} now"


def test_hide_values_never_raises_whatever_the_text_is() -> None:
    # every character in every position of the value: the text is hidden or left as it was
    value = "ratescalcs.example.invalid"
    codes = [*range(0x2200), *range(0xFB00, 0xFB10), *range(0xFF00, 0xFF60)]
    for code in codes:
        char = chr(code)
        for i in range(len(value)):
            text = value[:i] + char + value[i + 1 :]
            assert hide_values(text, [(value, SPELLED)]) in (text, SPELLED), (code, i)
        assert hide_values(char * 3, [(value, SPELLED)]) == char * 3


def test_hide_values_is_one_pass() -> None:
    # a placeholder is never searched again: a value inside a placeholder does not change it
    assert hide_values("a b", [("a", "<b>"), ("b", "<a>")]) == "<b> <a>"
    assert hide_values("abcd", [("ab", "X"), ("abcd", "Y")]) == "Y"  # longest first
    assert hide_values("keep", []) == "keep"
    assert stored_text("keep", None) == "keep"


# ------------------------------------------------------------------ what the server chose (step 3)


def test_stored_messages_have_no_error_page_and_no_redirect_target() -> None:
    status = HttpStatusError(
        503, url="https://h.example/x", body_snippet="Apache Server at in-ternal"
    )
    assert (
        "in-ternal" in str(status) and stored_message(status) == "HTTP 503 for https://h.example/x"
    )
    entitlement = EntitlementError(
        "HTML instead of JSON: secret page", stored_message="HTML instead of JSON"
    )
    assert stored_message(entitlement) == "HTML instead of JSON"
    plain = RedirectError("no Location")
    assert stored_message(plain) == "no Location"  # nothing the server chose: the message itself
    assert stored_message(ValueError("other")) == "other"


def _client_factory(**kw: Any) -> HttpClient:
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


def test_the_http_client_keeps_the_target_of_a_redirect_out_of_the_stored_message() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="192.0.2.10").mock(
            return_value=httpx.Response(302, headers={"location": f"https://{TARGET}/x"})
        )
        with _client_factory() as http, pytest.raises(RedirectError) as err:
            http.get("https://192.0.2.10/page")
    assert TARGET in str(err.value)  # the log
    assert TARGET not in err.value.stored_message
    assert err.value.stored_message == "refusing cross-host redirect from https://192.0.2.10/page"


def test_the_job_error_has_the_status_but_no_error_page_and_no_redirect_target(
    db_url: str, db: Database, conn: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    s = _settings(
        db_url, ratescalcs_base_url="https://192.0.2.10/p", ratescalcs_tls_server_name=None
    )
    with caplog.at_level(logging.INFO), respx.mock(assert_all_called=False) as router:
        router.route(host="192.0.2.10").mock(return_value=httpx.Response(503, text=ERROR_PAGE))
        with pytest.raises(Exception, match="all pages failed") as err:
            _run(s, db)
    assert "HTTP 503 for " in str(err.value) and FOOTER_HOST not in str(err.value)
    row = conn.execute(
        "SELECT error FROM ops.job_run WHERE job = 'swaps_ratescalcs' ORDER BY id DESC"
    ).fetchone()
    assert row is not None and "HTTP 503" in row["error"] and FOOTER_HOST not in row["error"]
    assert FOOTER_HOST in caplog.text  # the log keeps the page start


def test_the_message_of_a_status_error_still_has_the_page_for_the_log() -> None:
    err = HttpStatusError(404, url="https://iss.example/x", body_snippet="no such board")
    assert str(err) == "HTTP 404 for https://iss.example/x: no such board"


# ------------------------------------------------- the cut comes after the replacement (step 5)

FRAGMENT = HOST.rsplit(".", 2)[0]  # "198.51": the start of the host, what a cut inside it leaves
CUT_INSIDE = len("https://198.51.")  # characters of the address that are before the cut
CUT_PLACEHOLDER = BASE_URL_PLACEHOLDER[:CUT_INSIDE]  # what is left of the placeholder at the cut


def test_the_cut_of_a_job_error_does_not_leave_the_start_of_the_address(
    db_url: str, db: Database, conn: Conn
) -> None:
    s = _settings(db_url)
    pad = "x" * (MAX_ERROR_CHARS - CUT_INSIDE - len("RuntimeError: "))
    with pytest.raises(RuntimeError), job_run(db, "cut_failed", settings=s):
        raise RuntimeError(f"{pad}{BASE}/snapshot, then more text")
    with job_run(db, "cut_partial", settings=s) as run:
        run.mark_partial(pad + " ".join([BASE, "and more"]))
    rows = conn.execute(
        "SELECT job, status, error FROM ops.job_run WHERE job LIKE 'cut\\_%' ORDER BY job"
    ).fetchall()
    assert [(r["job"], r["status"]) for r in rows] == [
        ("cut_failed", "failed"),
        ("cut_partial", "partial"),
    ]
    for row in rows:
        assert "xxxxxxxx" in row["error"] and len(row["error"]) <= MAX_ERROR_CHARS
        assert FRAGMENT not in row["error"], row["job"]
        assert CUT_PLACEHOLDER in row["error"], row["job"]
    assert leaks(db_url, conn, (*SECRET, FRAGMENT)) == []


def test_the_cut_of_a_loader_failure_does_not_leave_the_start_of_the_address(
    db_url: str, db: Database, conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    pad = "x" * (400 - CUT_INSIDE)

    def fetch(*_a: Any, **_k: Any) -> Any:
        raise MdhubError(f"{pad}{BASE}/snapshot")

    monkeypatch.setattr(rc, "fetch", fetch)
    with pytest.raises(MdhubError, match="all pages failed"):
        _run(_settings(db_url), db)
    row = conn.execute(
        "SELECT error FROM ops.job_run WHERE job = 'swaps_ratescalcs' ORDER BY id DESC"
    ).fetchone()
    assert row is not None and "MdhubError: xxx" in row["error"]
    assert FRAGMENT not in row["error"] and CUT_PLACEHOLDER in row["error"]


def test_the_cut_of_a_probe_error_does_not_leave_the_start_of_the_address(
    db_url: str,
) -> None:
    class Down:
        def request(self, *_a: Any, **_k: Any) -> Any:
            raise OSError(pad + BASE + "/snapshot")

    pad = "x" * (_MAX_ERROR - CUT_INSIDE - len("OSError: "))
    spec = next(p for p in PROBES if p.source == "ratescalcs")
    result = probe(Down(), spec, _settings(db_url))  # type: ignore[arg-type]
    assert result.error is not None and len(result.error) <= _MAX_ERROR
    assert result.error.startswith("OSError: xxx")
    assert FRAGMENT not in result.error and CUT_PLACEHOLDER in result.error


# ------------------------------------------------------------------ the catch-up run (step 5)


def test_a_failed_catch_up_run_does_not_store_the_address_or_a_secret_in_the_report(
    db_url: str, db: Database, conn: Conn
) -> None:
    password = "pw-catch-up-4711"
    s = _settings(db_url, ratescalc_password=password)

    def loader(
        ctx: JobContext, *, mode: str = "final", date_from: Any = None, date_to: Any = None
    ) -> None:
        raise RuntimeError(f"cannot reach {BASE}/snapshot as u-test:{password}, valid for '{NAME}'")

    class Run:
        id = 0

        def mark_partial(self, _reason: str) -> None:
            pass

    class Health:
        job = "sa_catch_up"
        missed_by_lock = True
        last_expected_at = utc_now().astimezone(MSK) - timedelta(days=3)
        expected_rule = Rule(cron="0 3 * * *", params={"mode": "final"}, days="all")

    REGISTRY.register("sa_catch_up", "fails", loader)
    try:
        ctx = JobContext(
            settings=s,
            db=db,
            run=Run(),  # type: ignore[arg-type]
            job="quality",
            params={},
            logger=logging.getLogger("t"),
        )
        done = catch_up_missed(ctx, [Health()], lock_wait_s=1)  # type: ignore[list-item]
    finally:
        REGISTRY.unregister("sa_catch_up")
    assert [e["status"] for e in done] == ["failed"]
    assert "cannot reach" in done[0]["error"] and len(done[0]["error"]) <= 300
    run = conn.execute("INSERT INTO ops.job_run (job) VALUES ('quality') RETURNING id").fetchone()
    assert run is not None
    store_report(conn, QualityReport(as_of=utc_now().date(), run_id=run["id"], catch_up=done))
    conn.commit()
    stored = conn.execute("SELECT report::text AS t FROM ops.quality_report").fetchone()
    assert stored is not None and "cannot reach" in stored["t"]
    assert BASE_URL_PLACEHOLDER in stored["t"]
    assert leaks(db_url, conn, (*SECRET, password)) == []
