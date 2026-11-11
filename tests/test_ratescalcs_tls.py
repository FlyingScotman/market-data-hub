"""Ratescalcs behind a bare IP address (market-data-hub#144): the base URL is an IP, the certificate
is issued for a name, and ``MDHUB_RATESCALCS_TLS_SERVER_NAME`` makes the hub send that name as SNI /
``Host`` and verify the certificate against it. Certificate verification is never switched off.

A real local TLS server (a throw-away CA and a certificate for a name) is reached by 127.0.0.1:
with the server name it works, without it the check fails with a plain reason (also in the job
error and in the source probe row), and with a wrong name it fails again."""

from __future__ import annotations

import datetime as dt
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

import mdhub.core.http as http_mod
import mdhub.loaders.swaps  # noqa: F401 - registers the job
import mdhub.quality.source_probe as sp
from mdhub.config import BASE_URL_PLACEHOLDER, SERVER_NAME_PLACEHOLDER, Settings
from mdhub.core.errors import MdhubError
from mdhub.core.http import HttpClient, RateLimiter, TransportFailure
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.sources import ratescalcs as rc
from test_swaps_ratescalcs import FIX, Conn, _page

NAME = "ratescalcs.example.invalid"


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _make_pki(tmp: Path) -> tuple[Path, Path, bytes]:
    """A CA and a server certificate (DNS name :data:`NAME` only, no IP) -> cert, key, CA pem."""
    now = dt.datetime.now(dt.UTC)
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mdhub test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = _key()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAME)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(NAME)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "server.pem", tmp / "server.key"
    cert_path.write_bytes(_pem(leaf))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, _pem(ca)


class Site:
    """What the local server saw."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base = f"https://127.0.0.1:{port}"
        self.sni: list[str | None] = []
        self.hosts: list[str] = []
        self.paths: list[str] = []


@pytest.fixture
def tls_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    cert, key, ca_pem = _make_pki(tmp_path)
    # the hub's own trust store knows the test CA (verification stays ON, hostname checked)
    ctx_client = ssl.create_default_context(cadata=ca_pem.decode())
    monkeypatch.setattr(http_mod, "build_ssl_context", lambda: ctx_client)
    pages = {spec.path: _page(spec.dataset).encode() for spec in rc.PAGES}
    snapshot = (FIX / "ratescalcs_snapshot.json").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            site.hosts.append(self.headers.get("Host", ""))
            site.paths.append(self.path)
            if self.path == rc.SNAPSHOT_PATH:
                body, ctype = snapshot, "application/json"
            elif self.path in pages:
                body, ctype = pages[self.path], "text/html"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a: Any) -> None:
            pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, *_a: Any) -> None:  # handshakes the client aborts
            pass

    server = Server(("127.0.0.1", 0), Handler)
    ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx_server.load_cert_chain(cert, key)
    ctx_server.sni_callback = lambda _sock, name, _ctx: site.sni.append(name)  # type: ignore[assignment,return-value]
    server.socket = ctx_server.wrap_socket(server.socket, server_side=True)
    site = Site(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _client(**kw: Any) -> HttpClient:
    kw.setdefault("max_retries", 0)
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


def test_the_server_name_is_sent_as_sni_and_host_and_the_certificate_is_checked_against_it(
    tls_site: Site,
) -> None:
    with _client() as http:
        resp = rc.fetch(http, rc.SNAPSHOT_PATH, tls_site.base, NAME)
        page = rc.fetch(http, rc.PAGES[0].path, tls_site.base, NAME)
    assert resp.status_code == 200 and b"timestamp" in resp.content and page.status_code == 200
    assert set(tls_site.sni) == {NAME}  # SNI: the name, not the IP
    assert set(tls_site.hosts) == {f"{NAME}:{tls_site.port}"}  # Host: the name (and the port)
    assert tls_site.paths == [rc.SNAPSHOT_PATH, rc.PAGES[0].path]


def test_without_the_server_name_the_check_fails_with_a_plain_reason(tls_site: Site) -> None:
    with _client() as http, pytest.raises(TransportFailure) as exc:
        rc.fetch(http, rc.SNAPSHOT_PATH, tls_site.base)
    text = str(exc.value)
    assert exc.value.tls_certificate and "TLS certificate check failed" in text
    assert "127.0.0.1" in text and "not valid for" in text  # the certificate lists no IP
    assert "MDHUB_RATESCALCS_TLS_SERVER_NAME" in text  # what the owner can set
    assert text.count("not valid for") == 1 and "must be valid for" not in text  # said once
    assert tls_site.paths == []  # nothing was sent: no unverified request ever left


def test_a_wrong_server_name_fails_the_check(tls_site: Site) -> None:
    with _client() as http, pytest.raises(TransportFailure) as exc:
        rc.fetch(http, rc.SNAPSHOT_PATH, tls_site.base, "other.example.invalid")
    text = str(exc.value)
    assert exc.value.tls_certificate and "other.example.invalid" in text
    assert "MDHUB_RATESCALCS_TLS_SERVER_NAME" not in text  # a name is set; the hint is for none
    assert tls_site.paths == []


def test_a_certificate_failure_is_not_retried(tls_site: Site) -> None:
    sleeps: list[float] = []
    http = HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=sleeps.append, max_retries=3, backoff_base=1
    )
    with http, pytest.raises(TransportFailure):
        rc.fetch(http, rc.SNAPSHOT_PATH, tls_site.base)
    assert sleeps == []  # no backoff: the same answer every time


def _settings(db_url: str, site: Site, name: str | None) -> Settings:
    return Settings(
        database_url=db_url,
        ratescalc_username="u-test",
        ratescalc_password="p-test",
        ratescalcs_base_url=site.base,
        ratescalcs_tls_server_name=name,
    )


def _factory(**kw: Any) -> HttpClient:
    kw.setdefault("max_retries", 0)
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


def test_the_job_loads_through_the_ip_with_the_server_name(
    tls_site: Site, db_url: str, db: Database, conn: Conn
) -> None:
    out = run_job(
        "swaps_ratescalcs",
        {},
        settings=_settings(db_url, tls_site, NAME),
        db=db,
        http_factory=_factory,
    )
    assert out.status == "succeeded", out.result
    n = conn.execute("SELECT count(*) AS n FROM md.curve_snapshot").fetchone()
    assert n is not None and n["n"] == 5
    assert set(tls_site.hosts) == {f"{NAME}:{tls_site.port}"} and len(tls_site.paths) == 4


def test_the_job_error_says_why_when_the_certificate_does_not_match(
    tls_site: Site, db_url: str, db: Database, conn: Conn
) -> None:
    with pytest.raises(MdhubError, match="TLS certificate check failed"):
        run_job(
            "swaps_ratescalcs",
            {},
            settings=_settings(db_url, tls_site, None),
            db=db,
            http_factory=_factory,
        )
    row = conn.execute(
        "SELECT status, error FROM ops.job_run WHERE job = 'swaps_ratescalcs' ORDER BY id DESC"
    ).fetchone()
    assert row is not None and row["status"] == "failed"
    assert "not valid for" in row["error"] and "MDHUB_RATESCALCS_TLS_SERVER_NAME" in row["error"]
    assert tls_site.paths == []


def _probe_row(conn: Conn) -> Any:
    return conn.execute(
        "SELECT ok, http_status, auth, error, tls_expires_at, endpoint FROM ops.source_probe "
        "WHERE source = 'ratescalcs' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def test_the_source_probe_uses_the_name_and_shows_a_mismatch_in_plain_words(
    tls_site: Site, db_url: str, db: Database, conn: Conn
) -> None:
    params = {"sources": ["ratescalcs"], "force": True}
    run_job(
        "source_probe",
        params,
        settings=_settings(db_url, tls_site, NAME),
        db=db,
        http_factory=_factory,
    )
    row = _probe_row(conn)
    assert row["ok"] is True and row["http_status"] == 200 and row["error"] is None
    # the address is not stored (the read-only role reads ops.source_probe): the page stays
    assert row["endpoint"] == BASE_URL_PLACEHOLDER + rc.SNAPSHOT_PATH
    assert set(tls_site.sni) == {NAME}

    run_job(
        "source_probe",
        params,
        settings=_settings(db_url, tls_site, "other.example.invalid"),
        db=db,
        http_factory=_factory,
    )
    row = _probe_row(conn)
    assert row["ok"] is False and row["http_status"] is None
    assert "TLS certificate check failed" in row["error"]
    assert SERVER_NAME_PLACEHOLDER in row["error"] and "other.example.invalid" not in row["error"]

    run_job(
        "source_probe",
        params,
        settings=_settings(db_url, tls_site, None),
        db=db,
        http_factory=_factory,
    )
    row = _probe_row(conn)
    assert row["ok"] is False and "MDHUB_RATESCALCS_TLS_SERVER_NAME" in row["error"]
    assert "p-test" not in row["error"]


def test_the_probe_request_carries_the_server_name(db_url: str) -> None:
    s = Settings(
        database_url=db_url,
        ratescalc_username="u",
        ratescalc_password="p",
        ratescalcs_base_url="https://192.0.2.10",
        ratescalcs_tls_server_name=NAME,
    )
    req = sp._ratescalcs(s)
    assert req.url == "https://192.0.2.10/snapshot" and req.tls_server_name == NAME


@pytest.mark.parametrize(
    "bad",
    [
        "192.0.2.10",  # an IP cannot be an SNI name
        "https://ratescalcs.example.invalid",
        "ratescalcs.example.invalid:8443",
        "ratescalcs.example.invalid/x",
        "bad name",
        "-x.example.invalid",
    ],
)
def test_an_invalid_server_name_stops_the_start(bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_RATESCALCS_TLS_SERVER_NAME", bad)
    with pytest.raises(ValidationError, match="MDHUB_RATESCALCS_TLS_SERVER_NAME"):
        Settings()


def test_the_server_name_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().ratescalcs_tls_server_name is None  # unset: checked against the base host
    monkeypatch.setenv("MDHUB_RATESCALCS_TLS_SERVER_NAME", "")
    assert Settings().ratescalcs_tls_server_name is None
    monkeypatch.setenv("MDHUB_RATESCALCS_TLS_SERVER_NAME", " Ratescalcs.Example.invalid. ")
    assert Settings().ratescalcs_tls_server_name == "Ratescalcs.Example.invalid"


def test_there_is_no_switch_to_turn_certificate_verification_off() -> None:
    names = {f.lower() for f in Settings.model_fields}
    assert not any(("insecure" in n or "verify" in n or "tls_skip" in n) for n in names)
    ctx = http_mod.build_ssl_context.__wrapped__()  # the real context of the hub
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
