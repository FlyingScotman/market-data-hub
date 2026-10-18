"""Test helpers for the admin console: a software WebAuthn authenticator (packed "none"
attestation, ES256) and a console client that tracks the CSRF token."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import struct
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

ORIGIN = "https://admin.test"
RP_ID = "admin.test"
CLIENT_IP = "203.0.113.5"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    """A platform authenticator with user verification and a sign counter."""

    def __init__(self, rp_id: str = RP_ID, origin: str = ORIGIN) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.sign_count = 0
        self.user_handle: bytes | None = None

    def _cose(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}
        )

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode()

    def create(self, options: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        self.user_handle = unb64u(options["user"]["id"])
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x45])  # UP | UV | AT
            + struct.pack(">I", self.sign_count)
            + bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose()
        )
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64u(client_data),
                "attestationObject": b64u(att),
                "transports": ["internal"],
            },
        }

    def get(
        self, options: dict[str, Any], *, origin: str | None = None, advance: int = 1
    ) -> dict[str, Any]:
        self.sign_count += advance
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x05])  # UP | UV
            + struct.pack(">I", self.sign_count)
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        assert self.user_handle is not None
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64u(client_data),
                "authenticatorData": b64u(auth_data),
                "signature": b64u(signature),
                "userHandle": b64u(self.user_handle),
            },
        }


_CSRF = re.compile(r'data-csrf="([^"]*)"')


class Console:
    """TestClient wrapper: remembers the CSRF token of the last page and sends same-origin
    headers with every unsafe request."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf = ""

    def get(self, path: str, **kw: Any) -> Any:
        r = self.client.get(path, **kw)
        m = _CSRF.search(r.text)
        if m and m.group(1):
            self.csrf = m.group(1)
        return r

    def post(self, path: str, data: dict[str, Any] | None = None, **kw: Any) -> Any:
        headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", **kw.pop("headers", {})}
        body = {"csrf_token": self.csrf, **(data or {})}
        r = self.client.post(path, data=body, headers=headers, **kw)
        m = _CSRF.search(r.text)
        if m and m.group(1):
            self.csrf = m.group(1)
        return r

    def post_json(self, path: str, body: dict[str, Any] | None = None, **kw: Any) -> Any:
        headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": self.csrf,
            **kw.pop("headers", {}),
        }
        return self.client.post(path, json=body or {}, headers=headers, **kw)

    @property
    def cookie(self) -> str | None:
        value = self.client.cookies.get("__Host-mdhub_admin")
        return str(value) if value is not None else None

    # ----------------------------------------------------------------- flows

    def enroll(self, link_path: str, password: str, auth: SoftAuthenticator) -> Any:
        r = self.get(link_path)
        assert r.status_code == 200 and "Step 1 of 2" in r.text, r.text
        r = self.post("/enroll/password", {"password": password, "password2": password})
        assert r.status_code == 200 and "Step 2 of 2" in r.text, r.text
        options = self.post_json("/webauthn/register/options").json()
        r = self.post_json(
            "/webauthn/register/verify", {"credential": auth.create(options), "name": "soft"}
        )
        assert r.status_code == 200, r.text
        return self.get(r.json()["redirect"])

    def password_step(self, login: str, password: str) -> Any:
        self.get("/login")
        return self.post("/login", {"login": login, "password": password})

    def passkey(self, auth: SoftAuthenticator, **kw: Any) -> Any:
        options = self.post_json("/webauthn/login/options").json()
        return self.post_json(
            "/webauthn/login/verify", {"credential": auth.get(options, **kw), "next": "/"}
        )

    def login(self, login: str, password: str, auth: SoftAuthenticator) -> None:
        r = self.password_step(login, password)
        assert r.status_code == 200 and "Use a passkey" in r.text, r.text
        r = self.passkey(auth)
        assert r.status_code == 200, r.text
        self.get(r.json()["redirect"])
