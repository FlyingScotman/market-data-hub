"""CBR parsers against recorded cbr.ru pages (tests/fixtures/official, fetched 2026-10-16)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from mdhub.core.errors import FormatDriftError
from mdhub.sources import cbr

FIX = Path(__file__).parent / "fixtures" / "official"


def _text(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def test_key_rate_page() -> None:
    rows = cbr.parse_key_rate_page(_text("cbr_keyrate.html"))
    assert rows[0] == (date(2026, 9, 1), Decimal("14.00"))
    assert rows[-1] == (date(2026, 10, 16), Decimal("14.00"))
    assert all(d.weekday() < 5 for d, _ in rows)  # CBR business days only
    assert len(rows) == 34


def test_ruonia_page() -> None:
    rows = cbr.parse_ruonia_page(_text("cbr_ruonia.html"))
    last = rows[-1]
    assert last.fixing_date == date(2026, 10, 14)
    assert last.rate_pct == Decimal("13.91")
    assert last.published_on == date(2026, 10, 15)  # observation date != publication date
    assert last.volume_bn == Decimal("618.80")
    assert last.status == "Стандартный"
    assert [r.fixing_date for r in rows] == sorted(r.fixing_date for r in rows)


def test_ruonia_header_drift_fails_closed() -> None:
    page = _text("cbr_ruonia.html").replace("Дата публикации", "Дата обновления")
    with pytest.raises(FormatDriftError):
        cbr.parse_ruonia_page(page)


def test_ruonia_bad_row_fails_closed() -> None:
    page = _text("cbr_ruonia.html").replace('<td class="right">13,91</td>', "<td>abc</td>", 1)
    with pytest.raises(FormatDriftError):
        cbr.parse_ruonia_page(page)


def test_ruonia_index_xlsx() -> None:
    rows = cbr.parse_ruonia_index_xlsx((FIX / "cbr_ruonia_index.xlsx").read_bytes())
    assert rows[-1].fixing_date == date(2026, 10, 15)
    assert rows[-1].index == Decimal("4.54565125371164")
    assert rows[-1].term_3m_pct == Decimal("14.20879533495590")
    # calendar-day rows, weekends included
    assert {r.fixing_date for r in rows} >= {date(2026, 10, 10), date(2026, 10, 11)}


def test_xlsx_garbage_fails() -> None:
    with pytest.raises(FormatDriftError):
        cbr.parse_ruonia_index_xlsx(b"not a zip")


def test_standing_rates() -> None:
    rows = cbr.parse_standing_rates_page(_text("cbr_standing_rates.html"))
    last = rows[-1]
    assert last.rate_date == date(2026, 10, 16)
    assert last.deposit_standing_pct == Decimal("13.00")  # key rate - 1
    assert last.lending_standing_pct == Decimal("15.00")  # key rate + 1


def test_liquidity_page_and_identities() -> None:
    rows = cbr.parse_liquidity_page(_text("cbr_bliquidity.html"))
    last = rows[-1]
    assert last.observed_on == date(2026, 10, 16)
    assert last.values["deficit_bn"] == Decimal("3732.7")
    assert last.values["required_reserves_averaged_bn"] == Decimal("5463.8")
    broken = _text("cbr_bliquidity.html").replace("<td>3 732,7</td>", "<td>3 999,9</td>", 1)
    with pytest.raises(FormatDriftError, match="identity"):
        cbr.parse_liquidity_page(broken)


def test_meetings_page() -> None:
    meetings = cbr.parse_meetings_page(_text("cbr_cal_mp.html"))
    dates = [m.meeting_date for m in meetings]
    assert date(2026, 10, 23) in dates and date(2027, 12, 17) in dates
    assert date(2019, 2, 8) in dates  # year taken from the tab label
    extra = {m.meeting_date for m in meetings if m.is_extraordinary}
    assert extra == {date(2022, 2, 28), date(2022, 4, 8), date(2022, 5, 26), date(2023, 8, 15)}
    assert all("Пресс" not in m.title for m in meetings)


def test_xml_daily_nominal_normalization() -> None:
    rec = cbr.parse_xml_daily((FIX / "cbr_xml_daily_20261016.xml").read_bytes())
    assert rec.record_date == date(2026, 10, 16)
    by = {r.ccy: r for r in rec.rates}
    assert len(by) == 54
    dzd = by["DZD"]
    assert dzd.nominal == 100 and dzd.value == Decimal("62.3078")
    assert dzd.rate_per_unit == Decimal("0.623078") == dzd.vunit_rate
    assert by["USD"].rate_per_unit == Decimal("83.7600")


def test_xml_daily_returns_record_in_force() -> None:
    # requested Monday 12.10 -> Saturday record (set Friday 09.10, in force Sat-Mon)
    rec = cbr.parse_xml_daily((FIX / "cbr_xml_daily_20261012.xml").read_bytes())
    assert rec.record_date == date(2026, 10, 10)
    # requested 12.01.2026 -> record of 31.12.2025 (in force through the New Year holidays)
    rec = cbr.parse_xml_daily((FIX / "cbr_xml_daily_20260112.xml").read_bytes())
    assert rec.record_date == date(2025, 12, 31)


def test_xml_dynamic() -> None:
    recs = cbr.parse_xml_dynamic((FIX / "cbr_xml_dynamic_usd.xml").read_bytes(), "USD")
    dates = [r.record_date for r in recs]
    assert date(2026, 10, 3) in dates  # Saturday record
    assert date(2026, 10, 4) not in dates and date(2026, 10, 5) not in dates


def test_xml_rejects_dtd() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ValCurs Date="01.01.2026"/>'
    with pytest.raises(FormatDriftError):
        cbr.parse_xml_daily(evil)


def test_vunit_mismatch_fails() -> None:
    payload = (FIX / "cbr_xml_daily_20261016.xml").read_bytes()
    bad = payload.replace(b"<VunitRate>0,623078</VunitRate>", b"<VunitRate>0,7</VunitRate>")
    with pytest.raises(FormatDriftError, match="VunitRate"):
        cbr.parse_xml_daily(bad)


def test_cbr_number() -> None:
    assert cbr.parse_cbr_number("3 732,7") == Decimal("3732.7")
    assert cbr.parse_cbr_number("—") is None
    with pytest.raises(FormatDriftError):
        cbr.parse_cbr_number("1,2,3")


def test_hd_base_page_without_table_is_no_data() -> None:
    empty = "<html><form action='/hd_base/bliquidity/?UniDbQuery.Posted=True'></form></html>"
    assert cbr.parse_liquidity_page(empty) == []
    assert cbr.parse_key_rate_page(empty) == []
    with pytest.raises(FormatDriftError):  # not an hd_base page at all
        cbr.parse_liquidity_page("<html><body>maintenance</body></html>")


def test_parse_refinancing_page() -> None:
    periods = cbr.parse_refinancing_page(_text("cbr_refinancing_rates.html"))
    assert len(periods) == 84
    assert periods[0].date_from == date(1992, 1, 1) and periods[0].rate_pct == Decimal("20")
    last = periods[-1]
    assert (last.date_from, last.date_to) == (date(2012, 9, 14), date(2015, 12, 31))
    assert last.rate_pct == Decimal("8.25") and "2873-У" in last.document
    aug09 = next(p for p in periods if p.date_from == date(2009, 8, 10))
    assert aug09.rate_pct == Decimal("10.75") and aug09.date_to == date(2009, 9, 14)
    assert all(a.date_to + timedelta(days=1) == b.date_from for a, b in pairwise(periods))


def test_refinancing_page_drift() -> None:
    page = _text("cbr_refinancing_rates.html")
    row = "30&nbsp;апреля 2010&nbsp;г. &ndash;&nbsp;31&nbsp;мая&nbsp;2010&nbsp;г."
    merge = "1&nbsp;января 2016"
    assert page.count(row) == 1 and page.count(merge) == 1, "fixture layout changed"
    with pytest.raises(FormatDriftError, match="period starts"):  # a hole in the tiling
        cbr.parse_refinancing_page(page.replace(row, row.replace("31&nbsp;мая", "30&nbsp;мая")))
    with pytest.raises(FormatDriftError, match="merge row"):
        cbr.parse_refinancing_page(page.replace(merge, "1&nbsp;января 2017"))
