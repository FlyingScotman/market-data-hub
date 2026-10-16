from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.core.upsert import UpsertResult, touch_last_seen, upsert_rows

Conn = psycopg.Connection[DictRow]
KEY = ["trade_date", "secid", "board"]


@pytest.fixture
def table(conn: Conn) -> str:
    conn.execute(
        """
        CREATE TABLE md.t_eod (
            trade_date    date NOT NULL,
            secid         text NOT NULL,
            board         text NOT NULL,
            close         numeric,
            volume        bigint,
            extra         jsonb,
            run_id        bigint,
            capture_id    bigint,
            ingested_at   timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz,
            last_seen_run bigint,
            PRIMARY KEY (trade_date, secid, board)
        )
        """
    )
    conn.execute("CALL ops.enable_history('md.t_eod', ARRAY['trade_date','secid','board'])")
    conn.commit()
    return "md.t_eod"


def _rows(n: int = 3, close: str = "100.5", run_id: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": date(2026, 10, 15),
            "secid": f"SU{i:05d}",
            "board": "TQOB",
            "close": Decimal(close),
            "volume": i,
            "extra": {"i": i},
            "run_id": run_id,
            "capture_id": run_id * 10,
        }
        for i in range(n)
    ]


def _history(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM ops.row_history ORDER BY id").fetchall()


def test_insert_then_identical_reload_is_noop(conn: Conn, table: str) -> None:
    assert upsert_rows(conn, table, _rows(), KEY) == UpsertResult(3, 0, 0)
    conn.commit()
    before = conn.execute("SELECT xmin::text AS x FROM md.t_eod ORDER BY secid").fetchall()
    # Same data, different meta columns: unchanged, no write, no history.
    assert upsert_rows(conn, table, _rows(run_id=2), KEY, run_id=2) == UpsertResult(0, 0, 3)
    conn.commit()
    after = conn.execute("SELECT xmin::text AS x FROM md.t_eod ORDER BY secid").fetchall()
    assert before == after
    assert _history(conn) == []
    assert conn.execute("SELECT DISTINCT run_id FROM md.t_eod").fetchall() == [{"run_id": 1}]


def test_changed_row_updates_and_writes_history(conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(), KEY)
    rows = _rows(run_id=7)
    rows[1]["close"] = Decimal("101.25")
    result = upsert_rows(conn, table, rows, KEY, run_id=7)
    assert result == UpsertResult(inserted=0, updated=1, unchanged=2)
    assert result.total == 3 and result.changed == 1
    conn.commit()

    hist = _history(conn)
    assert len(hist) == 1
    h = hist[0]
    assert h["table_name"] == "md.t_eod"
    assert h["row_key"] == {"trade_date": "2026-10-15", "secid": "SU00001", "board": "TQOB"}
    assert h["old_row"]["close"] == 100.5
    assert h["old_row"]["run_id"] == 1
    assert h["replaced_by_run"] == 7

    row = conn.execute("SELECT * FROM md.t_eod WHERE secid = 'SU00001'").fetchone()
    assert row is not None
    assert row["close"] == Decimal("101.25")
    assert row["run_id"] == 7 and row["capture_id"] == 70  # meta refreshed with the data
    assert row["updated_at"] is not None  # set automatically on change
    other = conn.execute("SELECT * FROM md.t_eod WHERE secid = 'SU00000'").fetchone()
    assert other is not None and other["run_id"] == 1 and other["updated_at"] is None


def test_null_transitions_count_as_change(conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(1), KEY)
    rows = _rows(1)
    rows[0]["close"] = None
    assert upsert_rows(conn, table, rows, KEY) == UpsertResult(0, 1, 0)
    assert upsert_rows(conn, table, rows, KEY) == UpsertResult(0, 0, 1)
    rows[0]["close"] = Decimal("100.5")
    assert upsert_rows(conn, table, rows, KEY) == UpsertResult(0, 1, 0)
    assert len(_history(conn)) == 2


def test_numeric_scale_is_not_a_change(conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(1, close="100.5"), KEY)
    assert upsert_rows(conn, table, _rows(1, close="100.50"), KEY) == UpsertResult(0, 0, 1)


def test_update_cols_restricts_updates(conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(1), KEY)
    rows = _rows(1)
    rows[0]["close"] = Decimal("1")
    rows[0]["volume"] = 999
    assert upsert_rows(conn, table, rows, KEY, update_cols=["volume"]) == UpsertResult(0, 1, 0)
    row = conn.execute("SELECT close, volume FROM md.t_eod").fetchone()
    assert row == {"close": Decimal("100.5"), "volume": 999}


def test_key_only_rows_insert_or_ignore(conn: Conn) -> None:
    conn.execute("CREATE TABLE md.t_keys (k text PRIMARY KEY, run_id bigint)")
    rows = [{"k": "a", "run_id": 1}, {"k": "b", "run_id": 1}]
    assert upsert_rows(conn, "md.t_keys", rows, ["k"]) == UpsertResult(2, 0, 0)
    assert upsert_rows(conn, "md.t_keys", rows, ["k"]) == UpsertResult(0, 0, 2)


def test_validation_errors(conn: Conn, table: str) -> None:
    assert upsert_rows(conn, table, [], KEY) == UpsertResult()
    with pytest.raises(ValueError, match="schema-qualified"):
        upsert_rows(conn, "t_eod", _rows(), KEY)
    with pytest.raises(ValueError, match="does not exist"):
        upsert_rows(conn, "md.nope", _rows(), KEY)
    bad = _rows(1)
    bad[0]['x"; DROP TABLE md.t_eod; --'] = 1
    with pytest.raises(ValueError, match="unknown column"):
        upsert_rows(conn, table, bad, KEY)
    dup = _rows(2)
    dup[1]["secid"] = dup[0]["secid"]
    with pytest.raises(ValueError, match="duplicate key"):
        upsert_rows(conn, table, dup, KEY)
    ragged = _rows(2)
    del ragged[1]["volume"]
    with pytest.raises(ValueError, match="different columns"):
        upsert_rows(conn, table, ragged, KEY)
    with pytest.raises(ValueError, match="key column"):
        upsert_rows(conn, table, [{"secid": "x"}], KEY)
    # Connection still usable, table intact.
    assert conn.execute("SELECT count(*) AS n FROM md.t_eod").fetchone() == {"n": 0}


def test_failure_does_not_poison_outer_transaction(conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(1), KEY)
    bad = _rows(1)
    bad[0]["volume"] = "not-a-number"
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        upsert_rows(conn, table, bad, KEY)
    assert conn.execute("SELECT count(*) AS n FROM md.t_eod").fetchone() == {"n": 1}
    conn.commit()


def test_upsert_does_not_commit(db_url: str, conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(2), KEY)
    with psycopg.connect(db_url) as other:
        assert other.execute("SELECT count(*) FROM md.t_eod").fetchone() == (0,)
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM md.t_eod").fetchone() == {"n": 0}


def test_ten_thousand_rows_fast(conn: Conn, table: str) -> None:
    rows = _rows(10_000)
    t0 = time.perf_counter()
    assert upsert_rows(conn, table, rows, KEY) == UpsertResult(10_000, 0, 0)
    t1 = time.perf_counter()
    for r in rows[:500]:
        r["close"] = Decimal("99")
    assert upsert_rows(conn, table, rows, KEY) == UpsertResult(0, 500, 9_500)
    t2 = time.perf_counter()
    assert t1 - t0 < 5 and t2 - t1 < 5
    assert len(_history(conn)) == 500


def test_touch_last_seen_meta_only(conn: Conn, table: str) -> None:
    upsert_rows(conn, table, _rows(3), KEY)
    keys = [(date(2026, 10, 15), "SU00000", "TQOB"), (date(2026, 10, 15), "SU00002", "TQOB")]
    assert touch_last_seen(conn, table, KEY, keys, run_id=42) == 2
    assert touch_last_seen(conn, table, KEY, keys, run_id=42) == 0  # already marked
    seen = conn.execute("SELECT secid, last_seen_run FROM md.t_eod ORDER BY secid").fetchall()
    assert [r["last_seen_run"] for r in seen] == [42, None, 42]
    assert _history(conn) == []
    as_dicts = [{"trade_date": date(2026, 10, 15), "secid": "SU00001", "board": "TQOB"}]
    assert touch_last_seen(conn, table, KEY, as_dicts, run_id=43) == 1


def test_touch_last_seen_date_column(conn: Conn) -> None:
    conn.execute("CREATE TABLE ref.t_board (secid text PRIMARY KEY, last_seen date)")
    conn.execute("INSERT INTO ref.t_board VALUES ('A', NULL)")
    assert touch_last_seen(conn, "ref.t_board", ["secid"], [("A",)], run_id=1) == 1
    row = conn.execute("SELECT last_seen FROM ref.t_board").fetchone()
    assert row is not None and isinstance(row["last_seen"], date)
    conn.execute("CREATE TABLE ref.t_plain (secid text PRIMARY KEY)")
    with pytest.raises(ValueError, match="neither"):
        touch_last_seen(conn, "ref.t_plain", ["secid"], [("A",)], run_id=1)
