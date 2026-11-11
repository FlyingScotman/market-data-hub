"""The configured Ratescalcs address (market-data-hub#144, review of #142): plain http with
credentials is refused unless accepted explicitly, a TLS server name that nothing would verify
(plain http, an egress proxy) is refused, and the address never reaches a text that the read API
serves (``/v1/runs``, ``/v1/runs/health``): the full text goes to the log only."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

import mdhub.cli as cli
import mdhub.loaders.swaps  # noqa: F401 - registers the job
from mdhub.api.app import create_app
from mdhub.config import (
    BASE_URL_PLACEHOLDER,
    PLAINTEXT_OPT_IN,
    SERVER_NAME_PLACEHOLDER,
    ConfigError,
    Settings,
)
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.redact import hide_values
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.sources import ratescalcs as rc
from test_ratescalcs_tls import NAME, Site, tls_site  # noqa: F401 - the fixture
from test_swaps_ratescalcs import Conn, _run, _serve

HTTP_BASE = "http://192.0.2.10/cnyfuturesyields"
SECRET_BASE = "https://198.51.100.7:8443/prefix"
SECRET_NAME = "rc-internal.example.invalid"
TOKENS = "tok-alpha-123"
AUTH = {"Authorization": "Bearer tok-alpha-123"}


def _s(db_url: str | None = None, **kw: Any) -> Settings:
    kw.setdefault("ratescalc_username", "u-test")
    kw.setdefault("ratescalc_password", "p-test")
    return Settings(database_url=db_url, **kw)


# -------------------------------------------------------------------- plain http, credentials


def test_plain_http_with_credentials_is_refused_unless_accepted() -> None:
    errors = _s(ratescalcs_base_url=HTTP_BASE).ratescalcs_config_errors()
    assert len(errors) == 1 and "unencrypted" in errors[0] and PLAINTEXT_OPT_IN in errors[0]
    assert "192.0.2.10" not in errors[0]  # names settings, never values
    with pytest.raises(ConfigError, match=PLAINTEXT_OPT_IN):
        _s(ratescalcs_base_url=HTTP_BASE).require_ratescalcs_config()
    accepted = _s(ratescalcs_base_url=HTTP_BASE, ratescalcs_allow_plaintext_credentials=True)
    assert accepted.ratescalcs_config_errors() == []
    warning = accepted.ratescalcs_plaintext_warning()
    assert warning is not None and "credentials are sent unencrypted" in warning
    # no warning, no error: https, or http without credentials
    assert _s().ratescalcs_config_errors() == [] and _s().ratescalcs_plaintext_warning() is None
    bare = Settings(ratescalcs_base_url=HTTP_BASE)
    assert bare.ratescalcs_config_errors() == [] and bare.ratescalcs_plaintext_warning() is None


def test_the_opt_in_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_RATESCALCS_BASE_URL", HTTP_BASE)
    monkeypatch.setenv("RATESCALC_USERNAME", "u-test")
    monkeypatch.setenv("RATESCALC_PASSWORD", "p-test")
    assert Settings().ratescalcs_config_errors() != []
    monkeypatch.setenv(PLAINTEXT_OPT_IN, "true")
    assert Settings().ratescalcs_config_errors() == []


def test_the_worker_refuses_to_start_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_RATESCALCS_BASE_URL", HTTP_BASE)
    monkeypatch.setenv("RATESCALC_USERNAME", "u-test")
    monkeypatch.setenv("RATESCALC_PASSWORD", "p-test")
    result = CliRunner().invoke(cli.app, ["scheduler"])
    assert result.exit_code == 2
    assert PLAINTEXT_OPT_IN in result.output and "unencrypted" in result.output
    assert "192.0.2.10" not in result.output and "p-test" not in result.output


def test_the_worker_start_warns_loudly_when_the_opt_in_is_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    s = _s(ratescalcs_base_url=HTTP_BASE, ratescalcs_allow_plaintext_credentials=True)
    with caplog.at_level(logging.WARNING, logger="mdhub.cli"):
        cli._source_settings(s)
    assert any(
        r.levelno == logging.WARNING and "credentials are sent unencrypted" in r.getMessage()
        for r in caplog.records
    )


def test_the_job_sends_nothing_over_plain_http_without_the_opt_in(
    db_url: str, db: Database, conn: Conn
) -> None:
    # credentials may come from the console after the start: the job checks again
    with _serve(HTTP_BASE) as served, pytest.raises(ConfigError, match=PLAINTEXT_OPT_IN):
        _run(_s(db_url, ratescalcs_base_url=HTTP_BASE), db)
    assert served.requested == []
    row = conn.execute(
        "SELECT status, error FROM ops.job_run WHERE job = 'swaps_ratescalcs'"
    ).fetchone()
    assert row is not None and row["status"] == "failed" and "unencrypted" in row["error"]


def test_the_job_over_plain_http_with_the_opt_in_works_and_warns(
    db_url: str, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    s = _s(db_url, ratescalcs_base_url=HTTP_BASE, ratescalcs_allow_plaintext_credentials=True)
    with caplog.at_level(logging.WARNING), _serve(HTTP_BASE) as served:
        out = _run(s, db)
    assert out.status == "succeeded" and len(served.requested) == 4
    assert any("credentials are sent unencrypted" in r.getMessage() for r in caplog.records)


def _probe_factory(**kw: Any) -> HttpClient:
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


def _probe(db_url: str, db: Database, conn: Conn, settings: Settings) -> Any:
    run_job(
        "source_probe",
        {"sources": ["ratescalcs"], "force": True},
        settings=settings,
        db=db,
        http_factory=_probe_factory,
    )
    return conn.execute(
        "SELECT ok, error, http_status FROM ops.source_probe WHERE source = 'ratescalcs' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def test_the_probe_does_not_send_credentials_over_plain_http_and_says_when_it_does(
    db_url: str, db: Database, conn: Conn
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(HTTP_BASE + rc.SNAPSHOT_PATH).mock(return_value=httpx.Response(200))
        row = _probe(db_url, db, conn, _s(db_url, ratescalcs_base_url=HTTP_BASE))
        assert row["ok"] is False and PLAINTEXT_OPT_IN in row["error"]
        assert not route.called  # nothing was sent
        accepted = _s(db_url, ratescalcs_base_url=HTTP_BASE)
        accepted = accepted.model_copy(update={"ratescalcs_allow_plaintext_credentials": True})
        row = _probe(db_url, db, conn, accepted)
        assert route.called
        assert row["ok"] is True and "credentials are sent unencrypted" in row["error"]


# ------------------------------------------------------------------- TLS server name checks


def test_a_server_name_needs_https() -> None:
    s = _s(ratescalcs_base_url="http://192.0.2.10", ratescalcs_tls_server_name=NAME)
    [error] = s.ratescalcs_config_errors()[:1]
    assert "MDHUB_RATESCALCS_TLS_SERVER_NAME" in error and "plain http" in error
    # also with the opt-in: nothing would be verified
    s = s.model_copy(update={"ratescalcs_allow_plaintext_credentials": True})
    assert any("plain http" in e for e in s.ratescalcs_config_errors())
    ok = _s(ratescalcs_base_url="https://192.0.2.10", ratescalcs_tls_server_name=NAME)
    assert ok.ratescalcs_config_errors() == []


def test_a_server_name_is_not_combined_with_the_egress_proxy() -> None:
    s = _s(
        ratescalcs_base_url="https://192.0.2.10",
        ratescalcs_tls_server_name=NAME,
        http_proxy="http://proxy.example.invalid:3128",
    )
    [error] = s.ratescalcs_config_errors()
    assert "MDHUB_HTTP_PROXY" in error and "MDHUB_RATESCALCS_TLS_SERVER_NAME" in error
    assert "proxy.example.invalid" not in error
    with pytest.raises(ConfigError):
        s.require_ratescalcs_config()
    # the proxy alone, or the name alone, is fine
    proxy_only = _s(
        ratescalcs_base_url="https://192.0.2.10", http_proxy="http://proxy.example.invalid:3128"
    )
    assert proxy_only.ratescalcs_config_errors() == []


def test_the_worker_refuses_a_server_name_with_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_RATESCALCS_BASE_URL", "https://192.0.2.10")
    monkeypatch.setenv("MDHUB_RATESCALCS_TLS_SERVER_NAME", NAME)
    monkeypatch.setenv("MDHUB_HTTP_PROXY", "http://proxy.example.invalid:3128")
    result = CliRunner().invoke(cli.app, ["scheduler"])
    assert result.exit_code == 2 and "MDHUB_HTTP_PROXY" in result.output
    assert "proxy.example.invalid" not in result.output


def test_the_job_and_the_probe_refuse_a_server_name_with_a_proxy(
    db_url: str, db: Database, conn: Conn
) -> None:
    s = _s(
        db_url,
        ratescalcs_base_url="https://192.0.2.10",
        ratescalcs_tls_server_name=NAME,
        http_proxy="http://proxy.example.invalid:3128",
    )
    with pytest.raises(ConfigError, match="MDHUB_HTTP_PROXY"):
        _run(s, db)
    row = _probe(db_url, db, conn, s)
    assert row["ok"] is False and "MDHUB_HTTP_PROXY" in row["error"]


# ---------------------------------------------------------- the address is not served by the API


def _client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens=TOKENS)
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    yield from _client(db_url)


def _served(client: TestClient) -> str:
    return (
        client.get("/v1/runs", headers=AUTH).text + client.get("/v1/runs/health", headers=AUTH).text
    )


def test_hide_values_replaces_every_form_of_the_address() -> None:
    pairs = Settings(
        ratescalcs_base_url=SECRET_BASE, ratescalcs_tls_server_name=SECRET_NAME
    ).hidden_addresses()
    text = (
        f"HTTP 503 for {SECRET_BASE}/snapshot; host 198.51.100.7:8443 and 198.51.100.7, "
        f"valid for '{SECRET_NAME.upper()}'"
    )
    out = hide_values(text, pairs)
    for leaked in ("198.51.100.7", "8443", SECRET_NAME, "prefix"):
        assert leaked.lower() not in out.lower()
    assert out.count(BASE_URL_PLACEHOLDER) == 3 and SERVER_NAME_PLACEHOLDER in out


def test_a_failing_job_does_not_publish_the_address(
    db_url: str, db: Database, conn: Conn, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    s = _s(db_url, ratescalcs_base_url=SECRET_BASE, ratescalcs_tls_server_name=SECRET_NAME)
    with caplog.at_level(logging.INFO), respx.mock(assert_all_called=False) as router:
        router.route(host="198.51.100.7").mock(return_value=httpx.Response(503, text="down"))
        with pytest.raises(Exception, match="all pages failed"):
            _run(s, db)
    row = conn.execute(
        "SELECT status, error FROM ops.job_run WHERE job = 'swaps_ratescalcs'"
    ).fetchone()
    assert row is not None and row["status"] == "failed"
    assert "HTTP 503" in row["error"] and BASE_URL_PLACEHOLDER in row["error"]
    for text in (row["error"], _served(client)):
        assert "198.51.100.7" not in text and SECRET_NAME not in text and "prefix" not in text
    # the log keeps the full text for the owner
    assert "198.51.100.7" in caplog.text


def test_a_partial_job_does_not_publish_the_address(
    db_url: str, db: Database, conn: Conn, client: TestClient
) -> None:
    s = _s(db_url, ratescalcs_base_url=SECRET_BASE, ratescalcs_tls_server_name=SECRET_NAME)
    with _serve(SECRET_BASE) as served:
        served.snapshot = b"not json"  # one page of five fails, the others load
        out = _run(s, db)
    assert out.status == "partial", out.result
    row = conn.execute(
        "SELECT error FROM ops.job_run WHERE job = 'swaps_ratescalcs' ORDER BY id DESC"
    ).fetchone()
    assert row is not None and row["error"] and "snapshot" in row["error"]
    for text in (row["error"], _served(client)):
        assert "198.51.100.7" not in text and SECRET_NAME not in text


def test_a_certificate_error_does_not_publish_the_address_or_the_name(
    tls_site: Site,  # noqa: F811
    db_url: str,
    db: Database,
    conn: Conn,
    client: TestClient,
) -> None:
    s = _s(db_url, ratescalcs_base_url=tls_site.base, ratescalcs_tls_server_name=SECRET_NAME)
    with pytest.raises(Exception, match="TLS certificate check failed"):
        run_job("swaps_ratescalcs", {}, settings=s, db=db, http_factory=_http_factory_tls)
    row = conn.execute(
        "SELECT error FROM ops.job_run WHERE job = 'swaps_ratescalcs' ORDER BY id DESC"
    ).fetchone()
    assert row is not None and "TLS certificate check failed" in row["error"]
    for text in (row["error"], _served(client)):
        assert (
            "127.0.0.1" not in text and SECRET_NAME not in text and str(tls_site.port) not in text
        )
    assert tls_site.paths == []


def _http_factory_tls(**kw: Any) -> Any:
    from test_ratescalcs_tls import _factory

    return _factory(**kw)


def test_the_default_public_address_is_hidden_too(
    db_url: str, db: Database, conn: Conn, client: TestClient
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="ratescalcs.ru").mock(return_value=httpx.Response(503, text="down"))
        with pytest.raises(Exception, match="all pages failed"):
            _run(_s(db_url), db)
    served = _served(client)
    assert "ratescalcs.ru" not in served and "HTTP 503" in served
