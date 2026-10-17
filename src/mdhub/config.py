"""Runtime configuration from environment variables (DESIGN §7).

All settings come from the process environment; there is no config file. Secrets are held as
:class:`pydantic.SecretStr` so that ``repr()``/``str()``/logging never reveal them. Call
``.get_secret_value()`` only at the point of use (e.g. building an ``Authorization`` header)
and never put the value into a URL, a log message or ``ops.raw_capture.url_redacted``.

Environment variables (DESIGN §7 plus a few operational knobs):

=================================== ====================================================
``MDHUB_DATABASE_URL``              libpq URL/conninfo (required for anything touching DB)
``MDHUB_TZ``                        business time zone, default ``Europe/Moscow``
``MDHUB_HTTP_PROXY``                optional egress proxy URL (may contain credentials)
``MDHUB_ISS_RPS``                   MOEX ISS/APIM request rate per host, default 3
``MDHUB_RAW_RETENTION_DAYS_INTRADAY`` raw capture retention for intraday data, default 90
``MDHUB_RAW_RETENTION_DAYS_REREAD`` retention of ``intraday:reread:`` captures, default 7
``MDHUB_ALGOPACK_FULL_REREAD_MINUTES`` AlgoPack whole-day re-read interval, default 30
``MDHUB_ALGOPACK_RETENTION_DAYS``   AlgoPack rolling window in calendar days, default 90
``MDHUB_API_TOKENS``                comma-separated bearer tokens for the read API
                                    (``token`` -> name ``env:N``, or ``name=token``)
``MDHUB_OPS_LOG_RETENTION_DAYS``    retention of ``ops.api_request`` / ``source_probe`` /
                                    ``worker_heartbeat`` rows, default 90
``MDHUB_ENABLE_SDFI``               enable the MOEX SDFI curve loader, default false
``MDHUB_LOG_LEVEL``                 logging level, default ``INFO`` (extension)
``MDHUB_HTTP_TIMEOUT``              HTTP timeout in seconds, default 30 (extension)
``MDHUB_API_POOL_SIZE``             read-API connection pool size, default 8 (extension)
``MDHUB_BACKUP_DIR``                pg_dump target directory, default ``/var/backups/mdhub``
``MDHUB_ADMIN_DOMAIN``              admin console host name (WebAuthn RP ID), e.g.
                                    ``admin.example.com``; ``localhost`` for local use
``MDHUB_ADMIN_ORIGIN``              console origin, default ``https://<MDHUB_ADMIN_DOMAIN>``
``MDHUB_ADMIN_ALLOW_CIDRS``         client networks allowed to reach the console (comma or
                                    space separated); empty = every request refused
``MDHUB_ADMIN_TRUSTED_PROXIES``     peers (IPs/CIDRs) whose ``X-Forwarded-For`` is used
``MDHUB_ADMIN_SECRET_KEY``          32 random bytes (hex or base64) encrypting TOTP secrets
``MDHUB_UPDATE_GITHUB_TOKEN``       fine-grained GitHub token (contents:read on the repository);
                                    updater only (``deploy/updater.env``), never in ``.env``
``MDHUB_UPDATE_GITHUB_REPO``        ``owner/name`` of the release repository
``MDHUB_UPDATE_ROOT``               host directory with ``releases/``, ``current``, ``shared/``;
                                    mounted at the same path in the updater, default ``/opt/mdhub``
``MDHUB_UPDATE_UPLOAD_DIR``         upload volume shared by console and updater, default
                                    ``/var/lib/mdhub/uploads``
``MDHUB_UPDATE_API_URL``            read API as the updater reaches it, default ``http://api:8080``
``MOEX_API_KEY``                    MOEX APIM bearer token
``MOEX_PASSPORT_LOGIN`` / ``_PASSWORD`` MOEX Passport credentials
``RATESCALC_USERNAME`` / ``_PASSWORD``  Ratescalcs basic auth
``CBONDS_LOGIN`` / ``CBONDS_PASSWORD``  Cbonds JSON API
``OTC_MONITOR_TOKEN``               OTC Monitor bearer token (``swaps_otc_monitor``)
=================================== ====================================================
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ConfigError", "Settings", "get_settings"]

_NAMED_TOKEN_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{0,63})=([^=].*)$")
"""``name=token`` entry of ``MDHUB_API_TOKENS``. The token part must not start with ``=``, so
a padded base64 token (``abc==``) is never split."""


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _env(name: str) -> Any:
    # Accept both the env name and the python field name (for tests / programmatic use).
    return Field(default=None, validation_alias=AliasChoices(name, name.lower()))


class Settings(BaseSettings):
    """Process settings. Instantiate with ``Settings()`` to read the environment.

    Keyword arguments override the environment, e.g.
    ``Settings(database_url="postgresql://...")`` in tests.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
        frozen=True,
        env_ignore_empty=True,
    )

    database_url: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MDHUB_DATABASE_URL", "database_url")
    )
    tz: str = Field(default="Europe/Moscow", validation_alias=AliasChoices("MDHUB_TZ", "tz"))
    http_proxy: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MDHUB_HTTP_PROXY", "http_proxy")
    )
    iss_rps: float = Field(
        default=3.0, gt=0, validation_alias=AliasChoices("MDHUB_ISS_RPS", "iss_rps")
    )
    raw_retention_days_intraday: int = Field(
        default=90,
        ge=1,
        validation_alias=AliasChoices(
            "MDHUB_RAW_RETENTION_DAYS_INTRADAY", "raw_retention_days_intraday"
        ),
    )
    raw_retention_days_reread: int = Field(
        default=7,
        ge=1,
        validation_alias=AliasChoices(
            "MDHUB_RAW_RETENTION_DAYS_REREAD", "raw_retention_days_reread"
        ),
    )
    algopack_full_reread_minutes: int = Field(
        default=30,
        ge=5,
        validation_alias=AliasChoices(
            "MDHUB_ALGOPACK_FULL_REREAD_MINUTES", "algopack_full_reread_minutes"
        ),
    )
    algopack_retention_days: int = Field(
        default=90,
        ge=1,
        validation_alias=AliasChoices("MDHUB_ALGOPACK_RETENTION_DAYS", "algopack_retention_days"),
    )
    """Rolling window of the AlgoPack datasets (tradestats, FUTOI) in calendar days ending
    today (MSK): older rows and raw captures are deleted by ``maintenance mode=retention``,
    the gap scan and backfill ignore older days (:mod:`mdhub.datasets`)."""
    backup_dir: str = Field(
        default="/var/backups/mdhub",
        validation_alias=AliasChoices("MDHUB_BACKUP_DIR", "backup_dir"),
    )
    api_tokens: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MDHUB_API_TOKENS", "api_tokens")
    )
    ops_log_retention_days: int = Field(
        default=90,
        ge=1,
        validation_alias=AliasChoices("MDHUB_OPS_LOG_RETENTION_DAYS", "ops_log_retention_days"),
    )
    """Days of ``ops.api_request``, ``ops.source_probe`` and ``ops.worker_heartbeat`` kept by
    ``maintenance mode=retention`` (ADMIN_DESIGN §3.2)."""
    enable_sdfi: bool = Field(
        default=False, validation_alias=AliasChoices("MDHUB_ENABLE_SDFI", "enable_sdfi")
    )
    log_level: str = Field(
        default="INFO", validation_alias=AliasChoices("MDHUB_LOG_LEVEL", "log_level")
    )
    http_timeout: float = Field(
        default=30.0, gt=0, validation_alias=AliasChoices("MDHUB_HTTP_TIMEOUT", "http_timeout")
    )
    api_pool_size: int = Field(
        default=8,
        ge=1,
        le=100,
        validation_alias=AliasChoices("MDHUB_API_POOL_SIZE", "api_pool_size"),
    )

    admin_domain: str | None = _env("MDHUB_ADMIN_DOMAIN")
    admin_origin: str | None = _env("MDHUB_ADMIN_ORIGIN")
    admin_allow_cidrs: str | None = _env("MDHUB_ADMIN_ALLOW_CIDRS")
    admin_trusted_proxies: str | None = _env("MDHUB_ADMIN_TRUSTED_PROXIES")
    admin_secret_key: SecretStr | None = _env("MDHUB_ADMIN_SECRET_KEY")

    update_github_token: SecretStr | None = _env("MDHUB_UPDATE_GITHUB_TOKEN")
    update_github_repo: str = Field(
        default="FiveLimaGolf/market-data-hub",
        validation_alias=AliasChoices("MDHUB_UPDATE_GITHUB_REPO", "update_github_repo"),
    )
    update_root: str = Field(
        default="/opt/mdhub", validation_alias=AliasChoices("MDHUB_UPDATE_ROOT", "update_root")
    )
    update_upload_dir: str = Field(
        default="/var/lib/mdhub/uploads",
        validation_alias=AliasChoices("MDHUB_UPDATE_UPLOAD_DIR", "update_upload_dir"),
    )
    update_api_url: str = Field(
        default="http://api:8080",
        validation_alias=AliasChoices("MDHUB_UPDATE_API_URL", "update_api_url"),
    )

    moex_api_key: SecretStr | None = _env("MOEX_API_KEY")
    moex_passport_login: SecretStr | None = _env("MOEX_PASSPORT_LOGIN")
    moex_passport_password: SecretStr | None = _env("MOEX_PASSPORT_PASSWORD")
    ratescalc_username: SecretStr | None = _env("RATESCALC_USERNAME")
    ratescalc_password: SecretStr | None = _env("RATESCALC_PASSWORD")
    cbonds_login: SecretStr | None = _env("CBONDS_LOGIN")
    cbonds_password: SecretStr | None = _env("CBONDS_PASSWORD")
    otc_monitor_token: SecretStr | None = _env("OTC_MONITOR_TOKEN")

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level {value!r}")
        return level

    @field_validator(
        "database_url",
        "http_proxy",
        "api_tokens",
        "moex_api_key",
        "moex_passport_login",
        "moex_passport_password",
        "ratescalc_username",
        "ratescalc_password",
        "cbonds_login",
        "cbonds_password",
        "otc_monitor_token",
        "admin_domain",
        "admin_origin",
        "admin_allow_cidrs",
        "admin_trusted_proxies",
        "admin_secret_key",
        "update_github_token",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ------------------------------------------------------------------ helpers

    @property
    def zone(self) -> ZoneInfo:
        """Business time zone (``MDHUB_TZ``) as a :class:`~zoneinfo.ZoneInfo`."""
        return ZoneInfo(self.tz)

    def require_database_url(self) -> str:
        """Return the database conninfo or raise :class:`ConfigError` if unset."""
        if self.database_url is None:
            raise ConfigError("MDHUB_DATABASE_URL is not set")
        return self.database_url.get_secret_value()

    def proxy_url(self) -> str | None:
        """Egress proxy URL (secret: may contain credentials) or ``None``."""
        return self.http_proxy.get_secret_value() if self.http_proxy else None

    def api_token_entries(self) -> list[tuple[str, str]]:
        """``(name, token)`` per entry of the comma-separated ``MDHUB_API_TOKENS``.

        An entry is either a bare token, named ``env:N`` after its 1-based position, or
        ``name=token`` with a lower-case name (``[a-z0-9][a-z0-9._-]*``, at most 64 characters).
        """
        if self.api_tokens is None:
            return []
        entries = [t.strip() for t in self.api_tokens.get_secret_value().split(",")]
        out: list[tuple[str, str]] = []
        for i, entry in enumerate((e for e in entries if e), start=1):
            m = _NAMED_TOKEN_RE.match(entry)
            out.append((m.group(1), m.group(2)) if m else (f"env:{i}", entry))
        return out

    def api_token_list(self) -> list[str]:
        """Read-API bearer token values of ``MDHUB_API_TOKENS`` (names stripped)."""
        return [token for _, token in self.api_token_entries()]

    def secret_values(self) -> list[str]:
        """All configured secret values, for redaction of logs and error texts.

        Includes every :class:`SecretStr` field, each API token individually and the password
        component of ``MDHUB_DATABASE_URL``. Values shorter than 4 characters are skipped (too
        likely to cause false-positive redactions).
        """
        values: list[str] = []
        for name in type(self).model_fields:
            v = getattr(self, name)
            if isinstance(v, SecretStr):
                values.append(v.get_secret_value())
        values.extend(self.api_token_list())
        if self.database_url is not None:
            values.extend(_conninfo_password(self.database_url.get_secret_value()))
        return sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)


def _conninfo_password(conninfo: str) -> list[str]:
    try:
        from psycopg.conninfo import conninfo_to_dict

        pw = conninfo_to_dict(conninfo).get("password")
    except Exception:  # malformed conninfo: nothing to extract
        return []
    return [str(pw)] if pw else []


def get_settings() -> Settings:
    """Read settings from the environment (fresh instance on every call)."""
    return Settings()
