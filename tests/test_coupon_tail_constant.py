"""Coupon type from the constant tail of a schedule (market-data-hub#165 follow-up).

XS0114288789 paid 2.25 -> 2.5 -> 5 -> 7.5 % until 2007 and 7.5 % since; MOEX publishes 7.5 for
every future period but no coupon label, and ``schedule_constant`` wants one rate over the whole
schedule, so its future coupons were ``projected_by_moex``. The schedule alone cannot tell such a
bond from a floater whose rate MOEX carries forward, so the rule also needs the source's
statement: Cbonds ``floating_rate`` = false, and an active bond. The shapes below are synthetic.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.db.pool import Database
from mdhub.jobs import JobContext, JobRegistry, run_job
from mdhub.loaders import bonds_static as bs
from mdhub.sources import moex_static as ms

Conn = psycopg.Connection[DictRow]
TODAY = date(2026, 11, 17)
D = Decimal
# key-rate changes (any dates inside the tails below; the rule only needs to see them)
KEY_CHANGES = (date(2014, 12, 16), date(2022, 2, 28), date(2024, 7, 29))


def _pays(first: int, last: int) -> list[date]:
    return [date(y, m, d) for y in range(first, last + 1) for m, d in ((3, 31), (9, 30))]


def _schedule(values: list[str | None], pays: list[date]) -> tuple[ms.CouponRate, ...]:
    out, prev = [], date(pays[0].year - 1, 9, 30)
    for v, p in zip(values, pays, strict=True):
        out.append(ms.CouponRate(prev, p, None if v is None else D(v)))
        prev = p
    return tuple(out)


def _classify(
    rates: tuple[ms.CouponRate, ...], changes: tuple[date, ...] = KEY_CHANGES, **kw: Any
) -> tuple[str, str]:
    bond_class = kw.pop("bond_class", "corporate")
    inp = ms.ClassifyInput(
        **{
            "secid": "XS0114288789",
            "sec_type": "corporate_bond",
            "has_description": True,
            "bond_type": "Амортизируемая облигация",
            "coupon_values": tuple(r.value_prc is not None for r in rates),
            "coupon_rates": rates,
            "key_rate_changes": changes,
            "today": TODAY,
            "is_active": True,
            "cbonds_floating_rate": False,
            "cbonds_base_rate_present": False,
            **kw,
        }
    )
    return ms.classify_coupon_type(inp, bond_class)


def _tail(
    rates: tuple[ms.CouponRate, ...], changes: tuple[date, ...] = KEY_CHANGES, **kw: Any
) -> bool:
    """``schedule_tail_constant_fixed`` of an active, unlabelled bond that Cbonds calls fixed."""
    inp = ms.ClassifyInput(
        **{
            "secid": "XS0114288789",
            "has_description": True,
            "coupon_values": tuple(r.value_prc is not None for r in rates),
            "coupon_rates": rates,
            "key_rate_changes": changes,
            "today": TODAY,
            "is_active": True,
            "cbonds_floating_rate": False,
            "cbonds_base_rate_present": False,
            **kw,
        }
    )
    return ms.schedule_tail_constant_fixed(inp)


def _rus30_like(future: str = "7.5") -> tuple[ms.CouponRate, ...]:
    pays = _pays(2000, 2030)
    values = []
    for p in pays:
        if p <= date(2002, 9, 30):
            values.append("2.25")
        elif p <= date(2004, 9, 30):
            values.append("2.5")
        elif p <= date(2007, 9, 30):
            values.append("5")
        else:
            values.append(future if p > TODAY else "7.5")
    return _schedule(values, pays)


def test_step_up_long_ago_is_fixed_by_the_tail() -> None:
    rates = _rus30_like()
    # the whole schedule is not constant: the old rule rejects it
    assert not ms.schedule_constant_fixed(rates, TODAY, KEY_CHANGES)
    assert _tail(rates)
    assert _classify(rates) == ("fixed", "schedule_tail_constant")
    # its future coupons are then known, not projected
    row = ms.CouponRow(
        secid="XS0114288789", isin=None, name=None, pay_date=date(2027, 3, 31),
        start_date=date(2026, 9, 30), record_date=None, value=D("0.0026"), value_prc=D("7.5"),
        value_rub=None, face_value=D("0.035"), initial_face_value=D("1"), face_unit="USD",
        issue_value=None, capture_id=1,
    )  # fmt: skip
    kw = {"benchmark": None, "is_current": False, "face_indexed": False, "today": TODAY}
    assert ms.coupon_status(row, coupon_type="fixed", **kw) == "known"
    assert ms.coupon_status(row, coupon_type="unknown", **kw) == "projected_by_moex"


def test_a_constant_whole_schedule_keeps_its_old_basis() -> None:
    pays = _pays(2020, 2030)
    rates = _schedule(["6.25"] * len(pays), pays)
    assert _classify(rates, bond_type=None) == ("fixed", "schedule_constant")


def test_real_step_up_ahead_stays_unknown() -> None:
    # КалинОбл21-like: 9.5 % paid, 9.35 % published from 2028
    pays = _pays(2020, 2030)
    values = ["9.5" if p < date(2028, 1, 1) else "9.35" for p in pays]
    rates = _schedule(values, pays)
    assert not _tail(rates)
    assert _classify(rates, bond_type=None) == ("unknown", "schedule")
    # a future step-up in the last period only
    assert _classify(_rus30_like(future="8"), bond_type=None)[0] == "unknown"


def test_floater_with_equal_paid_rates_and_no_key_change_stays_unknown() -> None:
    pays = _pays(2024, 2030)
    values = ["16", "18", "21", "21", "21", "21", "21", "21", "21", "21", "21", "21", "21", "21"]
    rates = _schedule(values, pays)  # tail = the last 12 at 21, paid ones: 2024-09..2026-09
    no_change_in_tail = (date(2024, 3, 1),)  # before the tail starts (start of 2024-09-30 coupon)
    assert _classify(rates, no_change_in_tail, bond_type=None) == ("unknown", "schedule")
    assert _classify(rates, (), bond_type=None) == ("unknown", "schedule")
    # a change in the last paid period only does not count either (it must be on or before
    # the START of that period: a fixing lag could hide a floater's reaction)
    assert _classify(rates, (date(2026, 10, 1),), bond_type=None) == ("unknown", "schedule")
    # the same shape with a key-rate change between the first and the last paid tail period
    assert _classify(rates, (date(2025, 7, 28),), bond_type=None) == (
        "fixed",
        "schedule_tail_constant",
    )


def test_tail_needs_two_paid_coupons() -> None:
    pays = _pays(2025, 2030)
    # 12 % starts with the 2026-09 coupon: only one coupon of the tail is paid on 2026-11-17
    values = ["10"] * 3 + ["12"] * (len(pays) - 3)
    assert not _tail(_schedule(values, pays))
    # 12 % from the 2025-09 coupon: on 2026-03-31 two are paid; a key-rate change after the
    # start of the first and on/before the start of the last of them is then enough
    two_paid = _schedule(["10", *["12"] * (len(pays) - 1)], pays)
    assert _tail(two_paid, (date(2025, 6, 1),), today=date(2026, 3, 31))
    assert not _tail(two_paid, (), today=date(2026, 3, 31))


def test_history_that_does_not_reach_the_tail_is_conservative() -> None:
    rates = _rus30_like()
    # key-rate history loaded from 2013 holds no change inside a tail that lies before it
    assert not _tail(rates, ())
    # a change inside the covered part is enough: the uncovered start cannot weaken it
    assert _tail(rates, (date(2022, 2, 28),))


def test_other_conditions_of_the_rule_still_apply() -> None:
    rates = _rus30_like()
    assert _classify(rates, has_formula=True)[0] == "unknown"  # a rate formula from any source
    assert _classify(rates, bond_class="structured")[0] == "unknown"
    assert _classify(rates, coupon_benchmark="KEYRATE")[0] == "floating"  # labelled: earlier
    # an unvalued coupon anywhere: MOEX schedule not fully valued, never fixed by this rule
    gap = list(rates)
    gap[-1] = ms.CouponRate(gap[-1].start_date, gap[-1].pay_date, None)
    assert not _tail(tuple(gap))


def test_moex_floater_shape_is_unchanged() -> None:
    # current period valued, the rest of the future unpublished (None)
    pays = _pays(2024, 2030)
    values: list[str | None] = ["16", "18", "21", "21", "21", "21", "21", "21", *[None] * 6]
    rates = _schedule(values, pays)
    assert not _tail(rates)
    assert _classify(rates, bond_type=None) == ("variable_known", "schedule")


def test_schedule_tail_constant_is_an_allowed_basis(conn: Conn) -> None:
    conn.execute(
        "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status) "
        "VALUES ('TAILCHK00001', 'corporate', 'fixed', 'schedule_tail_constant', 'active')"
    )
    row = conn.execute(
        "SELECT col_description('ref.bond'::regclass, a.attnum) AS c FROM pg_attribute a "
        "WHERE a.attrelid = 'ref.bond'::regclass AND a.attname = 'coupon_type_basis'"
    ).fetchone()
    assert row is not None and "schedule_tail_constant" in row["c"]


# --------------------------------------------------------------- the source must say so


def test_cbonds_statement_is_required() -> None:
    rates = _rus30_like()
    assert _classify(rates, cbonds_floating_rate=False) == ("fixed", "schedule_tail_constant")
    # Cbonds says floating, or says nothing: the schedule alone proves nothing
    assert _classify(rates, cbonds_floating_rate=True) == ("unknown", "schedule")
    assert _classify(rates, cbonds_floating_rate=None) == ("unknown", "schedule")
    assert not _tail(rates, cbonds_floating_rate=None)


def test_a_stated_base_rate_contradicts_the_not_floating_flag() -> None:
    rates = _rus30_like()
    # Cbonds: floating_rate = false but a reference rate / margin is stated: not evidence
    assert _classify(rates, cbonds_base_rate_present=True) == ("unknown", "schedule")
    assert _classify(rates, cbonds_base_rate_present=None) == ("unknown", "schedule")
    assert _classify(rates, cbonds_base_rate_present=False)[1] == "schedule_tail_constant"


def test_delisted_bond_is_not_classified_by_its_tail() -> None:
    rates = _rus30_like()
    # a delisted floater whose old rate MOEX carries forward must not get known coupons
    assert _classify(rates, is_active=False) == ("unknown", "schedule")
    assert not _tail(rates, is_active=False)


def test_tail_rate_must_be_a_real_coupon() -> None:
    pays = _pays(2020, 2030)
    note = _schedule(["5"] * 10 + ["0.01"] * (len(pays) - 10), pays)  # investment-note placeholder
    assert not _tail(note)
    assert _tail(_rus30_like())


# ------------------------------------------------------------------------ loader level


def _universe(secid: str, rates: tuple[ms.CouponRate, ...]) -> bs.Universe:
    u = bs.Universe()
    u.directory[secid] = {
        "secid": secid, "isin": secid, "type": "corporate_bond", "name": "Test step-up bond",
        "shortname": "Test", "is_traded": True, "primary_boardid": "TQCB", "_raw": {"secid": secid},
    }  # fmt: skip
    u.descriptions[secid] = ms.Description(
        secid,
        {
            "BOND_TYPE": "Амортизируемая облигация",
            "MATDATE": date(2030, 3, 31),
            "FACEVALUE": D("0.035"),
        },
        [],
    )
    u.bondization.coupons[secid] = [
        ms.CouponRow(
            secid=secid, isin=secid, name=None, pay_date=r.pay_date, start_date=r.start_date,
            record_date=None, value=None, value_prc=r.value_prc, value_rub=None,
            face_value=D("0.035"), initial_face_value=D("1"), face_unit="USD",
            issue_value=None, capture_id=None,
        )
        for r in rates
    ]  # fmt: skip
    u.schedule_secids.add(secid)
    return u


def test_build_bond_row_uses_the_cbonds_flag() -> None:
    secid = "XS0114288789"
    u = _universe(secid, _rus30_like())
    kw: dict[str, Any] = {"key_rate_changes": KEY_CHANGES}
    for flags, expected in (
        ({secid: ms.CbondsFloating(False, False)}, ("fixed", "schedule_tail_constant")),
        ({secid: ms.CbondsFloating(False, True)}, ("unknown", "schedule")),  # base rate stated
        ({secid: ms.CbondsFloating(False, None)}, ("unknown", "schedule")),
        ({secid: ms.CbondsFloating(True, True)}, ("unknown", "schedule")),
        ({}, ("unknown", "schedule")),
    ):
        row, _ = bs.build_bond_row(
            secid, u, TODAY, bs._schedule_summary(u, secid), 1, cbonds_floating=flags, **kw
        )
        assert (row["coupon_type"], row["coupon_type_basis"]) == expected, flags
    # the same bond delisted (not traded, on no board): Cbonds's statement does not help
    u.directory[secid]["is_traded"] = False
    row, _ = bs.build_bond_row(
        secid,
        u,
        TODAY,
        bs._schedule_summary(u, secid),
        1,
        cbonds_floating={secid: ms.CbondsFloating(False, False)},
        **kw,
    )
    assert row["status"] == "delisted"
    assert (row["coupon_type"], row["coupon_type_basis"]) == ("unknown", "schedule")


def test_write_universe_makes_future_coupons_known(
    conn: Conn, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    secid = "XS0114288789"
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: TODAY)
    for d, v in zip(KEY_CHANGES, ("9.50", "20.00", "16.00"), strict=True):
        conn.execute(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
            "VALUES ('KEY_RATE', %s, 'cbr', %s), ('KEY_RATE', %s, 'cbr', 1)",
            (d, v, d - timedelta(days=1)),
        )
    u = _universe(secid, _rus30_like())
    registry = JobRegistry()

    def write(ctx: JobContext) -> None:
        with ctx.connection() as c:
            bs.write_universe(ctx, c, u, TODAY, scope={secid}, full=False)

    registry.register("write_universe_test", "test", write)

    def status(secid: str) -> tuple[str, str, set[str]]:
        row = conn.execute(
            "SELECT coupon_type, coupon_type_basis FROM ref.bond WHERE secid = %s", (secid,)
        ).fetchone()
        assert row is not None
        future = conn.execute(
            "SELECT DISTINCT status FROM ref.bond_coupon WHERE secid = %s AND pay_date > %s",
            (secid, TODAY),
        ).fetchall()
        return row["coupon_type"], row["coupon_type_basis"], {f["status"] for f in future}

    def run() -> None:
        out = run_job("write_universe_test", settings=settings, db=db, registry=registry)
        assert out.status == "succeeded"

    run()  # no statement from Cbonds yet
    assert status(secid) == ("unknown", "schedule", {"projected_by_moex"})
    conn.execute(
        "UPDATE ref.bond SET floating_rate = true, floating_rate_source = 'cbonds', "
        "floating_rate_base_present = true WHERE secid = %s",
        (secid,),
    )
    conn.commit()
    run()  # Cbonds says floating
    assert status(secid) == ("unknown", "schedule", {"projected_by_moex"})
    conn.execute(
        "UPDATE ref.bond SET floating_rate = false, floating_rate_base_present = false "
        "WHERE secid = %s",
        (secid,),
    )
    conn.commit()
    run()  # Cbonds says not floating
    assert status(secid) == ("fixed", "schedule_tail_constant", {"known"})


def test_floating_flag_columns_are_consistent(conn: Conn) -> None:
    ins = (
        "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status, "
        "floating_rate, floating_rate_source) VALUES (%s, 'corporate', 'fixed', 'moex_label', "
        "'active', %s, %s)"
    )
    conn.execute(ins, ("FLAGOK000001", False, "cbonds"))
    conn.execute(ins, ("FLAGOK000002", None, None))
    conn.commit()
    for flag, source in ((False, None), (True, None), (None, "cbonds"), (False, "other")):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(ins, ("FLAGBAD00001", flag, source))
        conn.rollback()
