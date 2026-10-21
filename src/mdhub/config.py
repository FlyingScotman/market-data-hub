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
``MDHUB_SECRETS_PRIVATE_KEY``       worker only: X25519 private key opening the source
                                    credentials set in the console (``mdhub secrets init-key``)
``MDHUB_HOSTMON_MOUNTS``            host mounts sampled by ``mdhub hostmon`` (comma or space
                                    separated host paths), default ``/``
``MDHUB_HOSTMON_*``                 Machine page thresholds (ADMIN_DESIGN §10), see the
                                    ``hostmon_*`` fields
``MDHUB_NTP_SERVER``                SNTP server for the clock offset, default ``pool.ntp.org``;
                                    ``off`` = no clock check
``MOEX_API_KEY``                    MOEX APIM bearer token
``MOEX_PASSPORT_LOGIN`` / ``_PASSWORD`` MOEX Passport credentials
``RATESCALC_USERNAME`` / ``_PASSWORD``  Ratescalcs basic auth
``CBONDS_LOGIN`` / ``CBONDS_PASSWORD``  Cbonds JSON API
``OTC_MONITOR_TOKEN``               OTC Monitor bearer token (``swaps_otc_monitor``)
``MDHUB_SNAPSHOT_STATIC_TIME``      daily static snapshot, MSK ``HH:MM``, default 07:45
``MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME`` market ``eod_preliminary`` cut (day D), default 19:45
``MDHUB_SNAPSHOT_EOD_FINAL_TIME``   market ``eod_final`` cut (next morning, for D-1), default 08:00
``MDHUB_SNAPSHOT_INTRADAY_MINUTES`` intraday cut interval (divides 60, or 60), 0 = off; default 30
``MDHUB_SNAPSHOT_INTRADAY_WINDOW``  intraday cuts ``HH:00-HH:00`` (MSK), default 10:00-19:00
``MDHUB_SNAPSHOT_RETENTION_DAYS_INTRADAY`` intraday market snapshot retention, default 30
``MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES`` ``eod_final``: wait up to N minutes for the final
                                    ``bonds_eod`` / ``zcyc`` runs of D, then publish with
                                    stale reasons; 0 = no wait; default 30
``MDHUB_SNAPSHOT_AUTOCORRECT``      a backfill that changes inputs of a day with an
                                    ``eod_final`` queues its rebuild (published only when
                                    the content changes); default true
=================================== ====================================================

Snapshot cut times are an owner decision (system DEC04); the defaults above follow the
collection schedule (DESIGN §6, §11) and can be changed without a code change.
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ConfigError", "Settings", "get_settings", "parse_hhmm", "parse_window"]

_NAMED_TOKEN_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{0,63})=([^=].*)$")
"""``name=token`` entry of ``MDHUB_API_TOKENS``. The token part must not start with ``=``, so
a padded base64 token (``abc==``) is never split."""


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def parse_hhmm(value: str) -> tuple[int, int]:
    """``"19:45"`` -> ``(19, 45)``; raises ``ValueError`` for anything else."""
    hh, sep, mm = value.strip().partition(":")
    if not sep or not (hh.isdigit() and mm.isdigit()) or len(mm) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"expected HH:MM, got {value!r}")
    return h, m


def parse_window(value: str) -> tuple[int, int]:
    """``"10:00-19:00"`` -> ``(10, 19)`` (whole hours, start < end)."""
    start, sep, end = value.partition("-")
    if not sep:
        raise ValueError(f"expected HH:00-HH:00, got {value!r}")
    (h1, m1), (h2, m2) = parse_hhmm(start), parse_hhmm(end)
    if m1 or m2 or h1 >= h2:
        raise ValueError(f"expected whole hours HH:00-HH:00 with start < end, got {value!r}")
    return h1, h2


def _env(name: str) -> Any:
    # Accept both the env name and the python field name (for tests / programmatic use).
    return Field(default=None, validation_alias=AliasChoices(name, name.lower()))


def _threshold(name: str, default: float) -> Any:
    env = f"MDHUB_HOSTMON_{name}"
    return Field(default=default, gt=0, validation_alias=AliasChoices(env, env.lower()[6:]))


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
    skip_sources: str | None = Field(
        default=None, validation_alias=AliasChoices("MDHUB_SKIP_SOURCES", "skip_sources")
    )
    """Comma list of source codes the fallback chains of the reference-rate loaders do not try
    (e.g. ``bundesbank,fred`` when ``mdhub probe-sources`` shows them geo-blocked from the
    server): saves the timeouts of every run. The other loaders ignore it."""
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
    snapshot_static_time: str = Field(
        default="07:45",
        validation_alias=AliasChoices("MDHUB_SNAPSHOT_STATIC_TIME", "snapshot_static_time"),
    )
    snapshot_eod_preliminary_time: str = Field(
        default="19:45",
        validation_alias=AliasChoices(
            "MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME", "snapshot_eod_preliminary_time"
        ),
    )
    snapshot_eod_final_time: str = Field(
        default="08:00",
        validation_alias=AliasChoices("MDHUB_SNAPSHOT_EOD_FINAL_TIME", "snapshot_eod_final_time"),
    )
    snapshot_intraday_minutes: int = Field(
        default=30,
        ge=0,
        le=60,
        validation_alias=AliasChoices(
            "MDHUB_SNAPSHOT_INTRADAY_MINUTES", "snapshot_intraday_minutes"
        ),
    )
    snapshot_intraday_window: str = Field(
        default="10:00-19:00",
        validation_alias=AliasChoices("MDHUB_SNAPSHOT_INTRADAY_WINDOW", "snapshot_intraday_window"),
    )
    snapshot_retention_days_intraday: int = Field(
        default=30,
        ge=1,
        validation_alias=AliasChoices(
            "MDHUB_SNAPSHOT_RETENTION_DAYS_INTRADAY", "snapshot_retention_days_intraday"
        ),
    )
    snapshot_eod_final_wait_minutes: int = Field(
        default=30,
        ge=0,
        le=180,
        validation_alias=AliasChoices(
            "MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES", "snapshot_eod_final_wait_minutes"
        ),
    )
    snapshot_autocorrect: bool = Field(
        default=True,
        validation_alias=AliasChoices("MDHUB_SNAPSHOT_AUTOCORRECT", "snapshot_autocorrect"),
    )

    hostmon_mounts: str = Field(
        default="/", validation_alias=AliasChoices("MDHUB_HOSTMON_MOUNTS", "hostmon_mounts")
    )
    ntp_server: str | None = Field(
        default="pool.ntp.org", validation_alias=AliasChoices("MDHUB_NTP_SERVER", "ntp_server")
    )
    # Machine page thresholds (ADMIN_DESIGN §10); the states are in mdhub.hostmon.status.
    hostmon_disk_warn_pct: float = _threshold("DISK_WARN_PCT", 80)
    hostmon_disk_crit_pct: float = _threshold("DISK_CRIT_PCT", 90)
    hostmon_disk_full_days: float = _threshold("DISK_FULL_DAYS", 14)
    hostmon_mem_available_min_pct: float = _threshold("MEM_AVAILABLE_MIN_PCT", 10)
    hostmon_swap_max_pct: float = _threshold("SWAP_MAX_PCT", 50)
    hostmon_steal_max_pct: float = _threshold("STEAL_MAX_PCT", 10)
    hostmon_load_per_core_max: float = _threshold("LOAD_PER_CORE_MAX", 2)
    hostmon_clock_offset_max_s: float = _threshold("CLOCK_OFFSET_MAX_S", 1)
    hostmon_pg_connections_max_pct: float = _threshold("PG_CONNECTIONS_MAX_PCT", 80)
    hostmon_pg_xact_max_minutes: float = _threshold("PG_XACT_MAX_MINUTES", 30)

    admin_domain: str | None = _env("MDHUB_ADMIN_DOMAIN")
    admin_origin: str | None = _env("MDHUB_ADMIN_ORIGIN")
    admin_allow_cidrs: str | None = _env("MDHUB_ADMIN_ALLOW_CIDRS")
    admin_trusted_proxies: str | None = _env("MDHUB_ADMIN_TRUSTED_PROXIES")
    admin_secret_key: SecretStr | None = _env("MDHUB_ADMIN_SECRET_KEY")
    secrets_private_key: SecretStr | None = _env("MDHUB_SECRETS_PRIVATE_KEY")

    update_github_token: SecretStr | None = _env("MDHUB_UPDATE_GITHUB_TOKEN")
    update_github_repo: str = Field(
        default="FlyingScotman/market-data-hub",
        validation_alias=AliasChoices("MDHUB_UPDATE_GITHUB_REPO", "update_github_repo"),
    )
    update_root: str = Field(
        default="/opt/mdhub", validation_alias=AliasChoices("MDHUB_UPDATE_ROOT", "update_root")
    )
    update_compose_override: str | None = _env("MDHUB_UPDATE_COMPOSE_OVERRIDE")
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

    @field_validator(
        "snapshot_static_time", "snapshot_eod_preliminary_time", "snapshot_eod_final_time"
    )
    @classmethod
    def _valid_hhmm(cls, value: str) -> str:
        parse_hhmm(value)
        return value.strip()

    @field_validator("snapshot_intraday_window")
    @classmethod
    def _valid_window(cls, value: str) -> str:
        parse_window(value)
        return value.strip()

    @field_validator("snapshot_intraday_minutes")
    @classmethod
    def _valid_interval(cls, value: int) -> int:
        if value and 60 % value:
            raise ValueError(f"intraday interval must divide 60 (or be 0 = off), got {value}")
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
        "secrets_private_key",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ntp_server", mode="before")
    @classmethod
    def _ntp_off(cls, value: Any) -> Any:
        # An empty env value is ignored (env_ignore_empty), so "off" disables the clock check.
        if isinstance(value, str) and value.strip().lower() in {"", "off", "none"}:
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

    def skipped_sources(self) -> frozenset[str]:
        """:attr:`skip_sources` as a set of source codes."""
        raw = self.skip_sources or ""
        return frozenset(s.strip() for s in raw.split(",") if s.strip())

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
