from __future__ import annotations

from datetime import date, time

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import ValidationError

from mdhub.config import ConfigError, Settings
from mdhub.core.timeutil import (
    add_business_days,
    business_days_back,
    msk_datetime,
    previous_business_day,
    to_msk,
)
from mdhub.db.pool import Database, redact_conninfo


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_DATABASE_URL", "postgresql://mdhub:dbpass99@db:5432/mdhub")
    monkeypatch.setenv("MDHUB_ISS_RPS", "2.5")
    monkeypatch.setenv("MDHUB_RAW_RETENTION_DAYS_INTRADAY", "30")
    monkeypatch.setenv("MDHUB_API_TOKENS", " tok-aaaa , tok-bbbb ,")
    monkeypatch.setenv("MDHUB_ENABLE_SDFI", "true")
    monkeypatch.setenv("MOEX_API_KEY", "apim-key-123")
    monkeypatch.setenv("RATESCALC_PASSWORD", "")
    s = Settings()
    assert s.iss_rps == 2.5 and s.raw_retention_days_intraday == 30 and s.enable_sdfi
    assert s.api_token_list() == ["tok-aaaa", "tok-bbbb"]
    assert s.ratescalc_password is None  # blank -> unset
    assert s.moex_api_key is not None and s.moex_api_key.get_secret_value() == "apim-key-123"
    assert s.require_database_url().startswith("postgresql://")
    secrets = s.secret_values()
    assert {"dbpass99", "apim-key-123", "tok-aaaa", "tok-bbbb"} <= set(secrets)
    text = repr(s) + str(s) + s.model_dump_json()
    for secret in ("dbpass99", "apim-key-123", "tok-aaaa"):
        assert secret not in text
    assert str(s.zone) == "Europe/Moscow"


def test_settings_validation() -> None:
    with pytest.raises(ConfigError):
        Settings().require_database_url()
    with pytest.raises(ValidationError):
        Settings(tz="Mars/Olympus")
    with pytest.raises(ValidationError):
        Settings(iss_rps=0)


def test_database_transactions(db_url: str) -> None:
    db = Database(db_url)
    with db.connection() as conn:
        conn.execute("CREATE TABLE md.p (k int)")
        tz = conn.execute("SHOW TimeZone").fetchone()
        assert tz == {"TimeZone": "UTC"}

    def insert_then_fail() -> None:
        with db.connection() as conn:
            conn.execute("INSERT INTO md.p VALUES (1)")
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError):
        insert_then_fail()
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM md.p").fetchone() == {"n": 0}
    assert "password" not in db.redacted


def test_database_pool(db_url: str) -> None:
    with Database(db_url, pool_size=2) as db:
        with db.connection() as conn:
            conn.execute("CREATE TABLE md.pp (k int)")
            conn.execute("INSERT INTO md.pp VALUES (1)")
        with db.connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM md.pp").fetchone() == {"n": 1}
            assert conn.execute("SHOW application_name").fetchone() == {"application_name": "mdhub"}
    with pytest.raises(ValueError, match="pool_size"):
        Database(db_url, pool_size=0)


def test_redact_conninfo() -> None:
    red = redact_conninfo("postgresql://u:secretpw@h:5432/db?sslmode=require")
    assert "secretpw" not in red and "sslmode=require" in red and "user=u" in red
    assert conninfo_to_dict(redact_conninfo("host=h password=x dbname=d")) == {
        "host": "h",
        "dbname": "d",
    }
    with pytest.raises(psycopg.ProgrammingError):
        Database("not a valid = = conninfo")


def test_business_day_helpers() -> None:
    fri, sat, mon = date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 19)
    assert previous_business_day(mon) == fri
    assert add_business_days(fri, 1) == mon
    assert add_business_days(fri, 0) == fri
    assert business_days_back(mon, [0, 1, 2]) == [mon, fri, date(2026, 10, 15)]
    assert business_days_back(sat, [0, 1]) == [fri]
    with pytest.raises(ValueError, match=">= 0"):
        business_days_back(mon, [-1])
    dt = msk_datetime(fri, time(19))
    assert dt.utcoffset() is not None and dt.hour == 19
    assert to_msk(dt) == dt
    with pytest.raises(ValueError, match="naive"):
        to_msk(dt.replace(tzinfo=None))
