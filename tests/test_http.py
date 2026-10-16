from __future__ import annotations

import ssl
from collections.abc import Iterator

import httpx
import pytest
import respx

from mdhub import USER_AGENT
from mdhub.config import Settings
from mdhub.core.http import (
    EntitlementError,
    HttpClient,
    HttpStatusError,
    RateLimiter,
    RedirectError,
    ResponseFormatError,
    TokenBucket,
    TransportFailure,
    build_ssl_context,
    redact_url,
)
from mdhub.core.redact import SecretRedactingFilter, sanitize_text

ISS = "https://iss.moex.com/iss"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> Iterator[HttpClient]:
    with HttpClient(
        settings=Settings(),
        base_url=ISS,
        limiter=RateLimiter(default_rps=None),
        sleep=sleeps.append,
        rate_limits={"iss.moex.com": 1e9},
    ) as c:
        yield c


@respx.mock
def test_retries_on_5xx_then_succeeds(client: HttpClient, sleeps: list[float]) -> None:
    route = respx.get(f"{ISS}/x.json").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    res = client.get_json("/x.json", params={"a": 1, "skip": None})
    assert res.data == {"ok": 1}
    assert route.call_count == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] * 1.2  # exponential backoff
    assert route.calls[0].request.url.params.get("skip") is None


@respx.mock
def test_gives_up_after_max_retries(client: HttpClient, sleeps: list[float]) -> None:
    route = respx.get(f"{ISS}/x.json").mock(return_value=httpx.Response(500, text="oops"))
    with pytest.raises(HttpStatusError) as ei:
        client.get("/x.json")
    assert ei.value.status_code == 500
    assert route.call_count == 5  # 1 + 4 retries
    assert len(sleeps) == 4


@respx.mock
def test_no_retry_on_4xx(client: HttpClient, sleeps: list[float]) -> None:
    route = respx.get(f"{ISS}/x.json").mock(return_value=httpx.Response(404))
    with pytest.raises(HttpStatusError):
        client.get("/x.json")
    assert route.call_count == 1 and sleeps == []
    resp = client.get("/x.json", raise_for_status=False)
    assert resp.status_code == 404


@respx.mock
def test_429_honours_retry_after(client: HttpClient, sleeps: list[float]) -> None:
    respx.get(f"{ISS}/x.json").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    assert client.get("/x.json").status_code == 200
    assert sleeps == [7.0]


@respx.mock
def test_transport_errors_retried_then_raise(client: HttpClient, sleeps: list[float]) -> None:
    route = respx.get(f"{ISS}/x.json").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TransportFailure, match="ConnectError"):
        client.get("/x.json", params={"token": "s3cret"})
    assert route.call_count == 5
    respx.get(f"{ISS}/y.json").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[])]
    )
    assert client.get_json("/y.json").data == []


@respx.mock
def test_post_not_retried_by_default(client: HttpClient) -> None:
    route = respx.post(f"{ISS}/p").mock(return_value=httpx.Response(503))
    with pytest.raises(HttpStatusError):
        client.request("POST", "/p", json={"a": 1})
    assert route.call_count == 1
    with pytest.raises(HttpStatusError):
        client.request("POST", "/p", json={"a": 1}, retry=True)
    assert route.call_count == 6


@respx.mock
def test_entitlement_error_on_html(client: HttpClient) -> None:
    respx.get(f"{ISS}/orderbook.json").mock(
        return_value=httpx.Response(
            200,
            text="<html><body>Информация доступна только подписчикам</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    with pytest.raises(EntitlementError, match="подписчикам"):
        client.get_json("/orderbook.json")
    respx.get(f"{ISS}/plain.json").mock(
        return_value=httpx.Response(200, text="x", headers={"content-type": "text/plain"})
    )
    with pytest.raises(ResponseFormatError):
        client.get_json("/plain.json")
    respx.get(f"{ISS}/bad.json").mock(
        return_value=httpx.Response(
            200, text="{not json", headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(ResponseFormatError, match="invalid JSON"):
        client.get_json("/bad.json")


@respx.mock
def test_user_agent_and_redirects(client: HttpClient) -> None:
    route = respx.get(f"{ISS}/a").mock(
        return_value=httpx.Response(302, headers={"Location": "/iss/b"})
    )
    respx.get(f"{ISS}/b").mock(return_value=httpx.Response(200, text="ok"))
    assert client.get("/a").text == "ok"
    assert route.calls[0].request.headers["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("market-data-hub/")

    respx.get(f"{ISS}/evil").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/x"})
    )
    with pytest.raises(RedirectError, match="cross-host"):
        client.get("/evil")
    respx.get(f"{ISS}/down").mock(
        return_value=httpx.Response(301, headers={"Location": "http://iss.moex.com/iss/b"})
    )
    with pytest.raises(RedirectError):
        client.get("/down")
    respx.get(f"{ISS}/loop").mock(
        return_value=httpx.Response(302, headers={"Location": "/iss/loop"})
    )
    with pytest.raises(RedirectError, match="too many"):
        client.get("/loop")


def test_token_bucket_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2.0, clock=clock, sleep=clock.sleep)
    waits = [bucket.acquire() for _ in range(6)]
    assert waits[:2] == [0.0, 0.0]  # burst = capacity 2
    assert waits[2:] == pytest.approx([0.5, 0.5, 0.5, 0.5])
    assert clock.now == pytest.approx(2.0)
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(0)


@respx.mock
def test_client_rate_limits_per_host() -> None:
    clock = FakeClock()
    limiter = RateLimiter(default_rps=None, clock=clock, sleep=clock.sleep)
    respx.get(url__regex=r"https://(iss\.moex\.com|www\.ecb\.europa\.eu)/.*").mock(
        return_value=httpx.Response(200)
    )
    with HttpClient(settings=Settings(iss_rps=3), limiter=limiter, sleep=clock.sleep) as c:
        for _ in range(6):
            c.get(f"{ISS}/x")
        assert clock.now == pytest.approx(1.0)  # 3 burst + 3 more at 3 rps
        before = clock.now
        c.get("https://www.ecb.europa.eu/x")
        assert clock.now == before  # separate bucket per host
    assert limiter.rate_for("iss.moex.com") == 3
    assert limiter.rate_for("unknown.example") is None


def test_ssl_context_contains_russian_root_and_certifi() -> None:
    ctx = build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
    names = {s.get("commonName") for s in subjects}
    assert "Russian Trusted Root CA" in names
    assert len(names) > 50  # certifi bundle loaded too


def test_proxy_setting_is_secret() -> None:
    s = Settings(http_proxy="http://user:hunter22@proxy:3128", moex_api_key="abcdef123")
    assert "hunter22" not in repr(s) and "abcdef123" not in repr(s)
    assert s.proxy_url() == "http://user:hunter22@proxy:3128"
    with HttpClient(settings=s) as c:  # builds a proxied client without error
        assert c.settings is s


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:pass@iss.moex.com/iss/x.json?date=2026-10-15&apikey=k#frag",
            "https://iss.moex.com/iss/x.json?date=2026-10-15",
        ),
        (
            "https://ws2.cbonds.info/x?login=a&password=b&Token=c&auth_code=d&q=1",
            "https://ws2.cbonds.info/x?q=1",
        ),
        (
            "https://iss.moex.com/iss/a.json?iss.only=history,history.cursor&start=100",
            "https://iss.moex.com/iss/a.json?iss.only=history,history.cursor&start=100",
        ),
        ("http://h:8080/p?client_secret=z", "http://h:8080/p"),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected
    assert redact_url(httpx.URL(url)) == expected


def test_sanitize_and_log_filter(caplog: pytest.LogCaptureFixture) -> None:
    text = "failed https://u:p@h/x?token=t&a=1 with key ABCD1234"
    assert sanitize_text(text, ["ABCD1234"]) == "failed https://h/x?a=1 with key ***"
    import logging

    logger = logging.getLogger("mdhub.test.redact")
    caplog.handler.addFilter(SecretRedactingFilter(["ABCD1234"]))
    logger.warning("secret %s at %s", "ABCD1234", "https://h/x?apikey=zzz")
    assert "ABCD1234" not in caplog.text and "zzz" not in caplog.text
