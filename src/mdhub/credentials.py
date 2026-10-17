"""Source credentials store (ADMIN_DESIGN §9): the registry, sealed boxes and the worker side.

Write-only by construction. The worker holds an X25519 private key
(``MDHUB_SECRETS_PRIVATE_KEY``, worker ``.env`` only) and publishes the public key to
``admin.secret_key_public`` (:meth:`CredentialStore.publish`). The console seals a value to the
latest published key with :func:`seal` (ephemeral X25519, HKDF-SHA256, AES-256-GCM, the name and
key id as associated data) and stores only the ciphertext in ``admin.source_credential``; it
holds no private key, so it cannot open what it wrote.

Worker side: :func:`activate` installs a process-wide :class:`CredentialStore`, which caches the
ciphertexts (re-read on ``NOTIFY mdhub_credentials`` and every :data:`POLL_INTERVAL_S` seconds,
no restart). :func:`mdhub.jobs.run_job` calls :func:`resolve` at job start: every value sealed
to this worker's key is decrypted into the :class:`~mdhub.config.Settings` copy that job uses
(a database value wins over ``.env``) and registered for redaction
(:func:`mdhub.core.redact.register_secrets`). A row under another key id is never used; it is
logged here and shown as "re-enter" in the console.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from mdhub.config import ConfigError, Settings
from mdhub.core.redact import register_secrets
from mdhub.db.pool import Database

__all__ = [
    "CHANNEL",
    "POLL_INTERVAL_S",
    "REGISTRY",
    "Credential",
    "CredentialError",
    "CredentialStore",
    "activate",
    "env_names",
    "generate_private_key",
    "key_id",
    "open_sealed",
    "parse_private_key",
    "public_bytes",
    "resolve",
    "seal",
]

log = logging.getLogger(__name__)

CHANNEL: Final = "mdhub_credentials"
POLL_INTERVAL_S: Final = 60.0
_VERSION: Final = b"\x01"
_NONCE: Final = 12
_INFO: Final = b"mdhub source credential v1"


@dataclass(frozen=True, slots=True)
class Credential:
    """A known credential: its environment name, :class:`Settings` field and source."""

    name: str
    field: str
    source: str | None
    """``ops.source`` code whose ``source_probe`` checks it; ``None`` = every source (proxy)."""
    label: str


REGISTRY: Final[dict[str, Credential]] = {
    c.name: c
    for c in (
        Credential("MOEX_API_KEY", "moex_api_key", "moex_apim", "MOEX APIM / AlgoPack key"),
        Credential(
            "MOEX_PASSPORT_LOGIN", "moex_passport_login", "moex_passport", "MOEX Passport login"
        ),
        Credential(
            "MOEX_PASSPORT_PASSWORD",
            "moex_passport_password",
            "moex_passport",
            "MOEX Passport password",
        ),
        Credential("RATESCALC_USERNAME", "ratescalc_username", "ratescalcs", "Ratescalcs user"),
        Credential("RATESCALC_PASSWORD", "ratescalc_password", "ratescalcs", "Ratescalcs password"),
        Credential("CBONDS_LOGIN", "cbonds_login", "cbonds", "Cbonds login"),
        Credential("CBONDS_PASSWORD", "cbonds_password", "cbonds", "Cbonds password"),
        Credential("OTC_MONITOR_TOKEN", "otc_monitor_token", "otc_monitor", "OTC Monitor token"),
        Credential(
            "MDHUB_HTTP_PROXY", "http_proxy", None, "Egress proxy URL (may hold a password)"
        ),
    )
}
"""Every credential the console may set (the migration's CHECK lists the same names)."""


class CredentialError(Exception):
    """A key or sealed box is malformed, or a box does not open with this key."""


# ------------------------------------------------------------------------------ sealed boxes


def generate_private_key() -> str:
    """New private key as ``MDHUB_SECRETS_PRIVATE_KEY`` text (base64url, no padding)."""
    raw = X25519PrivateKey.generate().private_bytes_raw()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_private_key(text: str) -> X25519PrivateKey:
    """Key from base64 / base64url text (as :func:`generate_private_key` writes) or 64 hex."""
    value = text.strip()
    raw: bytes | None = None
    try:
        raw = bytes.fromhex(value) if len(value) == 64 else None
    except ValueError:
        raw = None
    if raw is None:
        try:
            raw = base64.urlsafe_b64decode(value.replace("+", "-").replace("/", "_") + "==")
        except (binascii.Error, ValueError):
            raw = None
    if raw is None or len(raw) != 32:
        raise CredentialError("MDHUB_SECRETS_PRIVATE_KEY must be 32 bytes (base64 or hex)")
    return X25519PrivateKey.from_private_bytes(raw)


def public_bytes(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes_raw()


def key_id(public: bytes) -> str:
    """First 8 bytes of the SHA-256 of the raw public key, hex."""
    return hashlib.sha256(public).hexdigest()[:16]


def _aad(name: str, kid: str) -> bytes:
    return f"mdhub-credential\x00{name}\x00{kid}".encode()


def _box_key(shared: bytes, ephemeral: bytes, recipient: bytes) -> bytes:
    return HKDF(SHA256(), 32, salt=ephemeral + recipient, info=_INFO).derive(shared)


def seal(public: bytes, name: str, value: str) -> bytes:
    """Encrypt ``value`` of credential ``name`` to the worker public key ``public``."""
    recipient = X25519PublicKey.from_public_bytes(public)
    ephemeral = X25519PrivateKey.generate()
    epub = public_bytes(ephemeral)
    key = _box_key(ephemeral.exchange(recipient), epub, public)
    nonce = os.urandom(_NONCE)
    sealed = AESGCM(key).encrypt(nonce, value.encode(), _aad(name, key_id(public)))
    return _VERSION + epub + nonce + sealed


def open_sealed(private: X25519PrivateKey, name: str, kid: str, blob: bytes) -> str:
    """Decrypt a box of :func:`seal` stored as ``name`` under key id ``kid``."""
    public = public_bytes(private)
    if kid != key_id(public):
        raise CredentialError(f"{name}: sealed to key {kid}, this key is {key_id(public)}")
    if len(blob) < 1 + 32 + _NONCE + 16 or blob[:1] != _VERSION:
        raise CredentialError(f"{name}: not a sealed credential")
    epub, nonce, sealed = blob[1:33], blob[33 : 33 + _NONCE], blob[33 + _NONCE :]
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(epub))
        plain = AESGCM(_box_key(shared, epub, public)).decrypt(nonce, sealed, _aad(name, kid))
        return plain.decode()
    except (InvalidTag, ValueError):
        raise CredentialError(f"{name}: does not decrypt (tampered or wrong name)") from None


def env_names(settings: Settings) -> list[str]:
    """Registry names set in ``settings`` (the process environment)."""
    return [c.name for c in REGISTRY.values() if getattr(settings, c.field) is not None]


# ------------------------------------------------------------------------------ worker store


class CredentialStore:
    """The worker's view of ``admin.source_credential`` (ciphertexts only; opened per job)."""

    def __init__(self, db: Database, private: X25519PrivateKey) -> None:
        self.db = db
        self._private = private
        self.key_id = key_id(public_bytes(private))
        self._rows: dict[str, tuple[str, bytes]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reported: frozenset[str] = frozenset()

    def publish(self, names: Iterable[str] = ()) -> None:
        """Make this key the one the console seals to; ``names``: registry names in ``.env``."""
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO admin.secret_key_public (key_id, public_key, env_names)
                VALUES (%s, %s, %s)
                ON CONFLICT (key_id) DO UPDATE
                SET env_names = EXCLUDED.env_names, published_at = now()
                """,
                (self.key_id, public_bytes(self._private), sorted(names)),
            )
        log.info("source credentials: published key %s", self.key_id)

    def refresh(self) -> None:
        """Re-read the ciphertexts; report rows under another key (never used)."""
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT name, key_id, ciphertext FROM admin.source_credential"
            ).fetchall()
        with self._lock:
            self._rows = {r["name"]: (r["key_id"], bytes(r["ciphertext"])) for r in rows}
        foreign = frozenset(r["name"] for r in rows if r["key_id"] != self.key_id)
        if foreign - self._reported:
            log.warning(
                "source credentials sealed to another key (re-enter them in the console): %s",
                ", ".join(sorted(foreign)),
            )
        self._reported = foreign

    def resolve(self, settings: Settings) -> Settings:
        """``settings`` with every usable database credential decrypted over it."""
        with self._lock:
            rows = dict(self._rows)
        update: dict[str, SecretStr] = {}
        for name, (kid, blob) in rows.items():
            spec = REGISTRY.get(name)
            if spec is None or kid != self.key_id:
                continue
            try:
                update[spec.field] = SecretStr(open_sealed(self._private, name, kid, blob))
            except CredentialError as exc:
                log.error("source credential not used: %s", exc)
        if not update:
            return settings
        register_secrets(v.get_secret_value() for v in update.values())
        return settings.model_copy(update=update)

    # ------------------------------------------------------------------ change listener

    def start(self) -> None:
        """Follow changes (LISTEN plus a :data:`POLL_INTERVAL_S` poll) in a daemon thread."""
        self._thread = threading.Thread(target=self._listen, name="mdhub-credentials", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _listen(self, poll_s: float = POLL_INTERVAL_S) -> None:
        while not self._stop.is_set():
            try:
                with self.db.connect(autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    self.refresh()  # changes made while not listening
                    waited = 0.0
                    while not self._stop.is_set():
                        notified = any(True for _ in conn.notifies(timeout=1.0, stop_after=1))
                        waited += 1.0
                        if notified or waited >= poll_s:
                            self.refresh()
                            waited = 0.0
            except Exception:
                log.exception("source credentials listener failed; retrying")
                self._stop.wait(5.0)


_STORE: CredentialStore | None = None


def resolve(settings: Settings) -> Settings:
    """``settings`` with the database credentials over ``.env`` (unchanged without a store)."""
    store = _STORE
    return store.resolve(settings) if store is not None else settings


def activate(settings: Settings, db: Database, *, worker: bool) -> CredentialStore | None:
    """Install the store when ``MDHUB_SECRETS_PRIVATE_KEY`` is set (else ``None``).

    ``worker`` (``mdhub scheduler``): also publish the key and follow changes; stop the
    returned store on shutdown. Otherwise (``mdhub run``) the credentials are read once.

    :raises ConfigError: the key is malformed.
    """
    global _STORE
    if settings.secrets_private_key is None:
        if worker:
            log.warning(
                "MDHUB_SECRETS_PRIVATE_KEY is not set: credentials set in the console are not "
                "used (mdhub secrets init-key)"
            )
        return None
    try:
        private = parse_private_key(settings.secrets_private_key.get_secret_value())
    except CredentialError as exc:
        raise ConfigError(str(exc)) from None
    store = CredentialStore(db, private)
    if worker:
        store.publish(env_names(settings))
    store.refresh()
    _STORE = store
    if worker:
        store.start()
    return store
