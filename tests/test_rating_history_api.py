"""The opt-in action API must never mistake event dates for observation times."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

from mdhub.api.app import create_app
from mdhub.config import Settings

AUTH = {"Authorization": "Bearer test-rating-token"}
QUERY = {
    "object_kind": "issue",
    "object_key": "ZZ0000000001",
    "from": "2026-01-01",
    "to": "2026-12-31",
    "known_at": "2026-10-15T12:00:00Z",
}


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            database_url=make_conninfo(db_url, user="mdhub_api"),
            api_tokens="test-rating-token",
            rating_history_enabled=True,
        )
    )
    with TestClient(app) as c:
        yield c


def test_disabled_and_authentication() -> None:
    with TestClient(create_app(Settings(api_tokens="test-rating-token"))) as client:
        assert client.get("/v1/history/rating-actions", params=QUERY).status_code == 401
        assert (
            client.get("/v1/history/rating-actions", params=QUERY, headers=AUTH).status_code == 404
        )


def test_observed_history_and_limits(client: TestClient, conn: Connection[DictRow]) -> None:
    conn.execute("""
        INSERT INTO ref.rating_action
            (agency, object_kind, object_key, rating_type, scale, action_date, action_ref,
             rating, action, ingested_at, updated_at)
        VALUES
            ('nra', 'issue', 'ZZ0000000001', 'credit', 'national', '2026-06-01', 'a',
             'AAA', 'assigned', '2026-10-01Z', '2026-10-01Z'),
            ('nra', 'issue', 'ZZ0000000001', 'credit', 'national', '2026-10-14', 'b',
             NULL, 'withdrawn', '2026-10-16Z', '2026-10-16Z'),
            ('nra', 'issuer', '1234567890', 'credit', 'national', '2026-06-01', 'c',
             'AA', 'assigned', '2026-10-01Z', '2026-10-01Z');
        UPDATE ref.rating_action SET rating = 'AA', updated_at = '2026-10-17Z'
        WHERE object_key = 'ZZ0000000001' AND action_ref = 'a';
        UPDATE ops.row_history SET replaced_at = '2026-10-17Z'
        WHERE table_name = 'ref.rating_action';
    """)
    conn.commit()
    response = client.get("/v1/history/rating-actions", params=QUERY, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["complete"] is False and body["coverage"] == "unverified"
    assert len(body["actions"]) == 1 and body["actions"][0]["rating"] == "AAA"
    later = client.get(
        "/v1/history/rating-actions",
        params={**QUERY, "known_at": "2026-10-18T12:00:00Z", "limit": 1},
        headers=AUTH,
    ).json()
    assert later["truncated"] is True and later["actions"][0]["rating"] == "AA"
    all_actions = client.get(
        "/v1/history/rating-actions",
        params={**QUERY, "known_at": "2026-10-18T12:00:00Z"},
        headers=AUTH,
    ).json()
    assert all_actions["actions"][1]["action"] == "withdrawn"
    issuer = client.get(
        "/v1/history/rating-actions",
        params={**QUERY, "object_kind": "issuer", "object_key": "1234567890"},
        headers=AUTH,
    ).json()
    assert issuer["actions"][0]["rating"] == "AA"
    assert (
        client.get(
            "/v1/history/rating-actions",
            params={**QUERY, "known_at": "2026-10-15T12:00:00"},
            headers=AUTH,
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/v1/history/rating-actions", params={**QUERY, "from": "2027-01-01"}, headers=AUTH
        ).status_code
        == 400
    )
