"""Credential primitives of the admin console (ADMIN_DESIGN §5); no database access here.

* Passwords: argon2id via ``argon2-cffi`` (m=64 MiB, t=3, p=1; :data:`HASHER`), 12-128
  characters, refused when on the bundled common-password list; no composition rules.
  :func:`verify_password` reports when a stored hash needs rehashing (parameters changed).
* TOTP: RFC 6238 (HMAC-SHA1, 6 digits, 30 s steps, +-1 step) with replay protection by the
  last accepted step (:func:`totp_match`).
* Secrets at rest: AES-256-GCM with ``MDHUB_ADMIN_SECRET_KEY`` (32 bytes, hex or base64):
  ``nonce(12) || ciphertext+tag``; the associated data binds a ciphertext to its user.
* Random values: 256-bit session ids, CSRF tokens and link tokens (only their SHA-256 is
  stored, except the CSRF token), recovery codes ``xxxx-xxxx-xxxx`` (60 bits, base32).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from functools import cache
from importlib.resources import files
from typing import Final
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "HASHER",
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "SecretKeyError",
    "decrypt_secret",
    "encrypt_secret",
    "generate_recovery_codes",
    "hash_password",
    "new_token",
    "new_totp_secret",
    "normalize_recovery_code",
    "parse_secret_key",
    "password_problem",
    "sha256",
    "totp_code",
    "totp_match",
    "totp_uri",
    "verify_password",
]

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
"""argon2id, m=64 MiB, t=3, p=1 (tests swap in cheaper parameters)."""
PASSWORD_MIN: Final = 12
PASSWORD_MAX: Final = 128
TOTP_STEP_S: Final = 30
TOTP_DIGITS: Final = 6
_NONCE: Final = 12
_RECOVERY_ALPHABET: Final = "abcdefghjkmnpqrstuvwxyz23456789"


class SecretKeyError(ValueError):
    """``MDHUB_ADMIN_SECRET_KEY`` is missing or not 32 bytes, or a ciphertext does not decrypt."""


# ------------------------------------------------------------------------- random values


def new_token() -> str:
    """256 random bits, URL-safe (session ids, CSRF and link tokens)."""
    return secrets.token_urlsafe(32)


def sha256(value: str | bytes) -> bytes:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).digest()


# ----------------------------------------------------------------------------- passwords


@cache
def _common_passwords() -> frozenset[str]:
    text = files("mdhub.admin").joinpath("data/common_passwords.txt").read_text("utf-8")
    return frozenset(line for line in text.splitlines() if line and not line.startswith("#"))


def password_problem(password: str, *, login: str = "") -> str | None:
    """Why ``password`` is not acceptable, or ``None`` (NIST SP 800-63B: length and a
    blocklist, no composition rules)."""
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters."
    lowered = password.lower()
    if lowered in _common_passwords() or (login and login.lower() in lowered):
        return "This password is too common or contains the login. Choose another one."
    return None


def hash_password(password: str) -> str:
    return HASHER.hash(password)


def verify_password(stored: str | None, password: str) -> tuple[bool, bool]:
    """``(matches, needs_rehash)``. A missing or malformed hash still costs one argon2
    verification, so timing does not tell whether the account exists."""
    target = stored or _dummy_hash()
    try:
        HASHER.verify(target, password)
    except (VerificationError, InvalidHashError):
        return False, False
    if stored is None:
        return False, False
    return True, HASHER.check_needs_rehash(stored)


@cache
def _dummy_hash() -> str:
    return HASHER.hash(secrets.token_urlsafe(16))


# ------------------------------------------------------------------------ recovery codes


def generate_recovery_codes(n: int = 10) -> list[str]:
    """``n`` codes like ``k7pq-3mzx-a9rt`` (12 symbols of a 31-letter alphabet, ~59 bits)."""
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        out.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return out


def normalize_recovery_code(code: str) -> str:
    raw = "".join(ch for ch in code.lower() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}" if len(raw) == 12 else raw


# ---------------------------------------------------------------------------------- TOTP


def new_totp_secret() -> bytes:
    return secrets.token_bytes(20)


def totp_code(secret: bytes, step: int) -> str:
    """RFC 4226 HOTP value of ``step`` (RFC 6238 counter)."""
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_match(
    secret: bytes, code: str, *, last_step: int | None, now: float | None = None
) -> int | None:
    """The time step ``code`` matches (current +-1), or ``None``. A step at or before
    ``last_step`` is never accepted again (replay guard)."""
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // TOTP_STEP_S)
    found: int | None = None
    for step in (current - 1, current, current + 1):  # no early exit
        if hmac.compare_digest(totp_code(secret, step), code) and found is None:
            found = step
    if found is None or (last_step is not None and found <= last_step):
        return None
    return found


def totp_uri(secret: bytes, *, login: str, issuer: str) -> str:
    """``otpauth://`` URI for authenticator apps."""
    b32 = base64.b32encode(secret).decode().rstrip("=")
    label = quote(f"{issuer}:{login}")
    return f"otpauth://totp/{label}?secret={b32}&issuer={quote(issuer)}&digits=6&period=30"


# ----------------------------------------------------------------------- secrets at rest


def parse_secret_key(value: str | None) -> bytes:
    """32-byte key from hex (64 digits) or base64 / base64url text."""
    if not value:
        raise SecretKeyError("MDHUB_ADMIN_SECRET_KEY is not set")
    text = value.strip()
    key: bytes | None = None
    if len(text) == 64:
        try:
            key = bytes.fromhex(text)
        except ValueError:
            key = None
    if key is None:
        try:
            key = base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "==")
        except (binascii.Error, ValueError):
            key = None
    if key is None or len(key) != 32:
        raise SecretKeyError("MDHUB_ADMIN_SECRET_KEY must be 32 bytes (hex or base64)")
    return key


def encrypt_secret(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt_secret(key: bytes, blob: bytes, *, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:_NONCE], blob[_NONCE:], aad)
    except (InvalidTag, ValueError):
        raise SecretKeyError("secret does not decrypt with MDHUB_ADMIN_SECRET_KEY") from None
