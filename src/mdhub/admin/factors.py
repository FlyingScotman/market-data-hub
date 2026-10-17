"""Second factors of console users: passkeys (WebAuthn, ``py_webauthn``), TOTP and recovery
codes (ADMIN_DESIGN §5).

Passkeys: RP ID = ``MDHUB_ADMIN_DOMAIN``, the origin is checked, user verification is
required and the credential must be discoverable (so it can also log in on its own when the
user allows that). The challenge lives in the session row and is used once. A signed assertion
whose sign counter did not increase marks the credential suspect (refused from then on); the
caller audits it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import psycopg
import webauthn
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from mdhub.admin import security
from mdhub.admin.store import Session, User, pop_challenge, set_challenge, user_by_id
from mdhub.db.pool import DictConnection

__all__ = [
    "FactorError",
    "Factors",
    "RelyingParty",
    "authentication_options",
    "factors",
    "register_passkey",
    "registration_options",
    "verify_passkey",
]

RP_NAME: Final = "market-data-hub admin"
_TRANSPORTS: Final = {t.value for t in AuthenticatorTransport}


class FactorError(ValueError):
    """A second-factor check failed (message is safe to show)."""


class SuspectCredential(FactorError):  # noqa: N818 - a FactorError with a reason
    """The assertion's sign counter did not increase (possible cloned authenticator)."""

    def __init__(self, credential_id: int, user_id: int) -> None:
        super().__init__("This passkey has been blocked. Use another factor.")
        self.credential_id = credential_id
        self.user_id = user_id


@dataclass(frozen=True, slots=True)
class RelyingParty:
    rp_id: str
    origin: str


@dataclass(frozen=True, slots=True)
class Factors:
    passkeys: int
    totp: bool
    recovery_left: int

    @property
    def any(self) -> bool:
        return bool(self.passkeys or self.totp or self.recovery_left)


def factors(conn: DictConnection, user_id: int) -> Factors:
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM admin.webauthn_credential
                 WHERE user_id = %(u)s AND suspect_at IS NULL) AS passkeys,
               EXISTS (SELECT 1 FROM admin.totp_secret
                        WHERE user_id = %(u)s AND confirmed_at IS NOT NULL) AS totp,
               (SELECT count(*) FROM admin.recovery_code
                 WHERE user_id = %(u)s AND used_at IS NULL) AS recovery_left
        """,
        {"u": user_id},
    ).fetchone()
    assert row is not None
    return Factors(int(row["passkeys"]), bool(row["totp"]), int(row["recovery_left"]))


# -------------------------------------------------------------------------------- passkeys


def registration_options(
    conn: DictConnection, session: Session, user: User, rp: RelyingParty
) -> str:
    """``PublicKeyCredentialCreationOptions`` JSON; stores the challenge in the session."""
    existing = conn.execute(
        "SELECT credential_id FROM admin.webauthn_credential WHERE user_id = %s", (user.id,)
    ).fetchall()
    options = webauthn.generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        user_name=user.login,
        user_id=user.webauthn_user_id,
        user_display_name=user.display_name or user.login,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(r["credential_id"])) for r in existing
        ],
    )
    set_challenge(conn, session, options.challenge)
    return webauthn.options_to_json(options)


def register_passkey(
    conn: DictConnection,
    session: Session,
    user: User,
    rp: RelyingParty,
    credential: dict[str, Any],
    name: str,
) -> int:
    """Verify an attestation and store the credential; returns its id."""
    challenge = pop_challenge(conn, session)
    if challenge is None:
        raise FactorError("The passkey request expired. Try again.")
    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure, ValueError, KeyError):
        raise FactorError("The passkey could not be verified.") from None
    transports = [
        t for t in (credential.get("response") or {}).get("transports") or [] if t in _TRANSPORTS
    ]
    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO admin.webauthn_credential
                    (user_id, credential_id, public_key, sign_count, transports, aaguid, name)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (
                    user.id,
                    verified.credential_id,
                    verified.credential_public_key,
                    verified.sign_count,
                    transports,
                    verified.aaguid,
                    (name.strip() or "passkey")[:64],
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise FactorError("This passkey is already registered.") from None
    assert row is not None
    return int(row["id"])


def authentication_options(
    conn: DictConnection, session: Session, user: User | None, rp: RelyingParty
) -> str:
    """``PublicKeyCredentialRequestOptions`` JSON for ``user``'s passkeys (``None``: any
    discoverable passkey, for passkey-only login); stores the challenge in the session."""
    allow: list[PublicKeyCredentialDescriptor] = []
    if user is not None:
        rows = conn.execute(
            "SELECT credential_id, transports FROM admin.webauthn_credential "
            "WHERE user_id = %s AND suspect_at IS NULL",
            (user.id,),
        ).fetchall()
        allow = [
            PublicKeyCredentialDescriptor(
                id=bytes(r["credential_id"]),
                transports=[AuthenticatorTransport(t) for t in r["transports"]] or None,
            )
            for r in rows
        ]
    options = webauthn.generate_authentication_options(
        rp_id=rp.rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    set_challenge(conn, session, options.challenge)
    return webauthn.options_to_json(options)


def verify_passkey(
    conn: DictConnection,
    session: Session,
    rp: RelyingParty,
    credential: dict[str, Any],
    *,
    user: User | None,
) -> User:
    """Verify an assertion; returns the credential's user (must be ``user`` when given).

    :raises SuspectCredential: signed assertion with a sign counter that did not increase.
    :raises FactorError: anything else that fails.
    """
    failed = FactorError("The passkey could not be verified.")
    challenge = pop_challenge(conn, session)
    if challenge is None:
        raise FactorError("The passkey request expired. Try again.")
    try:
        raw_id = base64url_to_bytes(str(credential["rawId"]))
    except (KeyError, ValueError, TypeError):
        raise failed from None
    row = conn.execute(
        """
        SELECT id, user_id, public_key, sign_count, suspect_at FROM admin.webauthn_credential
        WHERE credential_id = %s FOR UPDATE
        """,
        (raw_id,),
    ).fetchone()
    if row is None or row["suspect_at"] is not None:
        raise failed
    if user is not None and int(row["user_id"]) != user.id:
        raise failed
    owner = user or user_by_id(conn, int(row["user_id"]))
    if owner is None or owner.disabled_at is not None:
        raise failed
    try:
        response = credential.get("response") or {}
        handle = response.get("userHandle")
        if handle and base64url_to_bytes(str(handle)) != owner.webauthn_user_id:
            raise failed
        # Counter 0 here: the signature is checked first, the counter below (a cloned key
        # signs validly; only a signed assertion may mark a credential suspect).
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=0,
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, InvalidJSONStructure, ValueError, KeyError):
        raise failed from None
    stored = int(row["sign_count"])
    if (verified.new_sign_count > 0 or stored > 0) and verified.new_sign_count <= stored:
        conn.execute(
            "UPDATE admin.webauthn_credential SET suspect_at = now() WHERE id = %s", (row["id"],)
        )
        raise SuspectCredential(int(row["id"]), owner.id)
    conn.execute(
        "UPDATE admin.webauthn_credential SET sign_count = %s, last_used_at = now() WHERE id = %s",
        (verified.new_sign_count, row["id"]),
    )
    return owner


def credential_json(body: Any) -> dict[str, Any]:
    """The ``credential`` object of a JSON request body (``FactorError`` when malformed)."""
    cred = body.get("credential") if isinstance(body, dict) else None
    if not isinstance(cred, dict) or not isinstance(cred.get("rawId"), str):
        raise FactorError("Malformed passkey response.")
    return cred


# ------------------------------------------------------------------------------------ TOTP


def begin_totp(conn: DictConnection, user: User, key: bytes) -> bytes | None:
    """New unconfirmed TOTP secret (valid 15 minutes until confirmed); ``None`` while a
    confirmed secret exists (remove it first)."""
    secret = security.new_totp_secret()
    row = conn.execute(
        """
        INSERT INTO admin.totp_secret (user_id, secret_enc) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET secret_enc = EXCLUDED.secret_enc, created_at = now()
            WHERE admin.totp_secret.confirmed_at IS NULL
        RETURNING user_id
        """,
        (user.id, security.encrypt_secret(key, secret, aad=_aad(user))),
    ).fetchone()
    return secret if row else None


def _aad(user: User) -> bytes:
    return f"mdhub-totp:{user.id}".encode()


def pending_totp(conn: DictConnection, user: User, key: bytes) -> bytes | None:
    row = conn.execute(
        "SELECT secret_enc FROM admin.totp_secret WHERE user_id = %s AND confirmed_at IS NULL "
        "AND created_at > now() - interval '15 minutes'",
        (user.id,),
    ).fetchone()
    return security.decrypt_secret(key, bytes(row["secret_enc"]), aad=_aad(user)) if row else None


def confirm_totp(conn: DictConnection, user: User, key: bytes, code: str) -> bool:
    """Activate the pending secret when ``code`` matches it."""
    secret = pending_totp(conn, user, key)
    if secret is None:
        return False
    step = security.totp_match(secret, code, last_step=None)
    if step is None:
        return False
    conn.execute(
        "UPDATE admin.totp_secret SET confirmed_at = now(), last_step = %s WHERE user_id = %s",
        (step, user.id),
    )
    return True


def remove_totp(conn: DictConnection, user: User) -> bool:
    return conn.execute("DELETE FROM admin.totp_secret WHERE user_id = %s", (user.id,)).rowcount > 0


def check_totp(conn: DictConnection, user: User, key: bytes, code: str) -> bool:
    """Whether ``code`` is a fresh TOTP of the user's confirmed secret (records the step)."""
    row = conn.execute(
        "SELECT secret_enc, last_step FROM admin.totp_secret "
        "WHERE user_id = %s AND confirmed_at IS NOT NULL FOR UPDATE",
        (user.id,),
    ).fetchone()
    if row is None:
        return False
    secret = security.decrypt_secret(key, bytes(row["secret_enc"]), aad=_aad(user))
    step = security.totp_match(secret, code, last_step=row["last_step"])
    if step is None:
        return False
    conn.execute("UPDATE admin.totp_secret SET last_step = %s WHERE user_id = %s", (step, user.id))
    return True


def reencrypt_totp(conn: DictConnection, old: bytes, new: bytes) -> int:
    """Re-encrypt every TOTP secret from key ``old`` to ``new`` (``mdhub admin rotate-key``);
    unconfirmed ones are dropped. Returns how many secrets were re-encrypted."""
    conn.execute("DELETE FROM admin.totp_secret WHERE confirmed_at IS NULL")
    rows = conn.execute(
        "SELECT user_id, secret_enc FROM admin.totp_secret ORDER BY user_id FOR UPDATE"
    ).fetchall()
    for r in rows:
        aad = f"mdhub-totp:{r['user_id']}".encode()
        plain = security.decrypt_secret(old, bytes(r["secret_enc"]), aad=aad)
        conn.execute(
            "UPDATE admin.totp_secret SET secret_enc = %s WHERE user_id = %s",
            (security.encrypt_secret(new, plain, aad=aad), r["user_id"]),
        )
    return len(rows)


# -------------------------------------------------------------------------- recovery codes


def new_recovery_codes(conn: DictConnection, user: User) -> list[str]:
    """Replace the user's recovery codes with 10 new ones (returned once, stored hashed)."""
    codes = security.generate_recovery_codes()
    conn.execute("DELETE FROM admin.recovery_code WHERE user_id = %s", (user.id,))
    for code in codes:
        conn.execute(
            "INSERT INTO admin.recovery_code (user_id, code_hash) VALUES (%s, %s)",
            (user.id, security.hash_password(code)),
        )
    return codes


def use_recovery_code(conn: DictConnection, user: User, code: str) -> bool:
    """Consume a matching unused code."""
    normalized = security.normalize_recovery_code(code)
    rows = conn.execute(
        "SELECT id, code_hash FROM admin.recovery_code "
        "WHERE user_id = %s AND used_at IS NULL ORDER BY id FOR UPDATE",
        (user.id,),
    ).fetchall()
    for r in rows:
        if security.verify_password(r["code_hash"], normalized)[0]:
            conn.execute("UPDATE admin.recovery_code SET used_at = now() WHERE id = %s", (r["id"],))
            return True
    if not rows:
        security.verify_password(None, normalized)  # same cost without codes
    return False


def passkey_rows(conn: DictConnection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, created_at, last_used_at, suspect_at, transports
        FROM admin.webauthn_credential WHERE user_id = %s ORDER BY id
        """,
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]
