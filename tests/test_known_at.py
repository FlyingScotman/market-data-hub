"""As-known-at readers over ops.row_history (migration 0500) and versioned calendar deletes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import DictRow

from mdhub.core.bizday import DbCalendar
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.db.pool import Database
from mdhub.loaders import calendar as cal
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]


def _at(conn: Conn, query: Any, t: datetime) -> list[dict[str, Any]]:
    return conn.execute(query, {"t": t}).fetchall()


def test_ref_readers_return_the_version_known_at(conn: Conn) -> None:
    seed(conn)
    conn.commit()
    t0 = utc_now()
    # revisions after t0 (upsert_rows: history images via the trigger)
    upsert_rows(
        conn,
        "ref.bond_board",
        [{"secid": "SU26242RMFS0", "board": "TQOB", "lot_size": 10, "listed": False}],
        ["secid", "board"],
    )
    conn.execute("UPDATE ref.bond_amortization SET value_prc = value_prc + 1")
    conn.execute("UPDATE ref.bond_offer SET status = 'cancelled'")
    conn.execute(
        "UPDATE ref.bond_coupon SET is_deleted = true "
        "WHERE coupon_no = (SELECT min(coupon_no) FROM ref.bond_coupon)"
    )
    conn.execute("UPDATE ref.issuer SET inn = '0000000000' WHERE issuer_id = 2")
    conn.execute("UPDATE ref.bond_rate_formula SET benchmark = 'RUONIA', benchmark_raw = 'X'")
    conn.execute(
        "INSERT INTO ref.issuer (issuer_id, name) VALUES (99, 'Later')"
    )  # inserted after t0
    conn.commit()
    t1 = utc_now()

    def rows(table: str, t: datetime) -> list[dict[str, Any]]:
        return conn.execute(
            f"SELECT * FROM ref.{table}_known_at(%(t)s) ORDER BY 1, 2",
            {"t": t},
        ).fetchall()

    def current(table: str) -> list[dict[str, Any]]:
        return conn.execute(f"SELECT * FROM ref.{table} ORDER BY 1, 2").fetchall()

    for table in ("bond_board", "bond_amortization", "bond_offer", "bond_coupon", "issuer"):
        now_rows = rows(table, t1)
        assert now_rows == current(table), table  # known at "now" = the table
        assert rows(table, t0) != now_rows, table  # the past differs ...
    assert {r["issuer_id"] for r in rows("issuer", t0)} == {1, 2, 3}  # ... 99 unknown then
    old_board = next(r for r in rows("bond_board", t0) if r["board"] == "TQOB")
    assert old_board["listed"] is True
    assert {r["status"] for r in rows("bond_offer", t0)} != {"cancelled"}
    assert not any(r["is_deleted"] for r in rows("bond_coupon", t0))
    # resolved formula as known at
    f0 = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved_known_at(%(t)s)", {"t": t0}
    ).fetchall()
    f1 = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved_known_at(%(t)s)", {"t": t1}
    ).fetchall()
    assert [r["benchmark"] for r in f0] == ["KEY_RATE"]
    assert [(r["benchmark"], r["benchmark_raw"], r["benchmark_source"]) for r in f1] == [
        ("RUONIA", "X", "moex_iss")
    ]
    view = conn.execute(
        "SELECT secid, benchmark, spread_bp, status, sources FROM ref.bond_rate_formula_resolved"
    ).fetchall()
    assert view == [
        {k: r[k] for k in ("secid", "benchmark", "spread_bp", "status", "sources")} for r in f1
    ]
    # nothing is known before the first insert
    assert rows("bond", datetime(2000, 1, 1, tzinfo=t0.tzinfo)) == []


def test_calendar_soft_delete_replays_and_relists(conn: Conn, db: Database) -> None:
    known = utc_now()
    with db.connection() as c:
        cal.write_calendar_days(
            c,
            "MOEX_STOCK",
            "moex_iss_calendar",
            {date(2027, 1, 4): False},
            known_at=known,
            run_id=None,
        )
        cal.write_weekend_sessions(
            c,
            "MOEX_STOCK",
            {date(2027, 1, 9): date(2027, 1, 11)},
            known_at=known,
            run_id=None,
            capture_id=None,
            drop_after=None,
        )
    t_listed = utc_now()
    with db.connection() as c:
        res = cal._drop_unlisted(
            c, "MOEX_STOCK", "moex_iss_calendar", set(), after=None, run_id=None
        )
        cal.write_weekend_sessions(
            c,
            "MOEX_STOCK",
            {},
            known_at=utc_now(),
            run_id=None,
            capture_id=None,
            drop_after=date(2026, 12, 31),
        )
    assert res.updated == 1
    t_gone = utc_now()
    # no DELETE: rows stay, flagged
    flags = conn.execute(
        "SELECT (SELECT is_deleted FROM ref.calendar_day WHERE day = '2027-01-04') AS d, "
        "(SELECT is_deleted FROM ref.weekend_session WHERE day = '2027-01-09') AS w"
    ).fetchone()
    assert flags == {"d": True, "w": True}
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2027, 1, 4), "MOEX_STOCK") is True  # weekday fallback again
    assert not bc.is_trading_day(date(2027, 1, 9), "MOEX_STOCK")

    def eff(t: datetime) -> list[Any]:
        return [
            (r["day"], r["is_business_day"])
            for r in conn.execute(
                "SELECT * FROM ref.calendar_effective_known_at(%(t)s) "
                "WHERE calendar = 'MOEX_STOCK'",
                {"t": t},
            ).fetchall()
        ]

    assert eff(t_listed) == [(date(2027, 1, 4), False)]
    assert eff(t_gone) == []
    assert conn.execute(
        "SELECT count(*) AS n FROM ref.calendar_effective WHERE day = '2027-01-04'"
    ).fetchone() == {"n": 0}
    ws = conn.execute(
        "SELECT day, is_deleted FROM ref.weekend_session_known_at(%(t)s)", {"t": t_listed}
    ).fetchall()
    assert ws == [{"day": date(2027, 1, 9), "is_deleted": False}]

    # listed again: re-activated (a new version), history keeps all three states
    with db.connection() as c:
        cal.write_calendar_days(
            c,
            "MOEX_STOCK",
            "moex_iss_calendar",
            {date(2027, 1, 4): False},
            known_at=utc_now(),
            run_id=None,
        )
    assert eff(utc_now()) == [(date(2027, 1, 4), False)]
    assert eff(t_gone) == []
    assert eff(t_listed) == [(date(2027, 1, 4), False)]


def test_pre_0500_hard_delete_images_replay(conn: Conn) -> None:
    """Rows hard-deleted before 0500 left an image with '_deleted' in ops.row_history: they
    are known until the deletion and absent afterwards."""
    conn.execute(
        """
        INSERT INTO ops.row_history (table_name, row_key, old_row, replaced_at)
        VALUES ('ref.calendar_day',
                '{"calendar": "MOEX_FX", "day": "2026-12-31", "source": "moex_iss_calendar"}',
                '{"calendar": "MOEX_FX", "day": "2026-12-31", "source": "moex_iss_calendar",
                  "is_business_day": false, "known_at": "2026-06-01T00:00:00+00:00",
                  "_deleted": true}',
                '2026-09-01T00:00:00+00:00')
        """
    )
    conn.commit()
    before = conn.execute(
        "SELECT day, is_business_day FROM ref.calendar_effective_known_at('2026-08-01Z')"
    ).fetchall()
    after = conn.execute(
        "SELECT day FROM ref.calendar_effective_known_at('2026-09-02Z')"
    ).fetchall()
    early = conn.execute(
        "SELECT day FROM ref.calendar_effective_known_at('2026-05-01Z')"
    ).fetchall()
    assert before == [{"day": date(2026, 12, 31), "is_business_day": False}]
    assert after == [] and early == []
