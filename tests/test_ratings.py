"""Credit ratings from the agencies' public disclosure (WEB-18; review P1-1, TDM brief HS5).

Fixtures ``tests/fixtures/ratings/`` are public responses recorded on 2026-10-21:

* ``nkr/issues.xlsx``, ``nkr/issuers.xlsx`` - www.ratings.ru ``/issues.php``, ``/issuers.php``;
* ``nra/ratings_page.html.gz`` (www.ra-national.ru ``/ratings/``) and ``nra/export.xlsx``
  (the export it links);
* ``raexpert/`` - raexpert.ru ``/sitemap.xml``, ``/sitemap0.xml`` (gzip), the list pages
  ``/ratings/debt_inst/`` and ``/ratings/credits/``, the press releases
  ``/releases/2026/oct20d`` and ``oct21a``-``oct21e``, and the object pages they link
  (companies, the Amur region, two Amur bonds), and ``company_1000042599`` (a non-resident
  bank with Belarus and international-scale blocks) from the live crawl of the same day.

Large HTML is stored gzipped, byte for byte as received. No test uses the network.
"""

from __future__ import annotations

import gzip
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.errors import FormatDriftError
from mdhub.core.htmldom import parse_html
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.loaders import _issuer_rules as rules
from mdhub.loaders import ratings as _ratings  # noqa: F401  (registers the job)
from mdhub.sources import nkr, nra, raexpert
from mdhub.sources.ratings_common import (
    is_watch,
    normalize_action,
    normalize_grade,
    parse_ru_date,
)

FIX = Path(__file__).parent / "fixtures" / "ratings"
Conn = psycopg.Connection[DictRow]
TODAY = date(2026, 10, 21)


def raw(name: str) -> bytes:
    data = (FIX / name).read_bytes()
    return gzip.decompress(data) if name.endswith(".gz") else data


def text(name: str) -> str:
    return raw(name).decode("utf-8")


# ------------------------------------------------------------------------- normalization


@pytest.mark.parametrize(
    ("rating", "grade"),
    [
        ("ruAAA", "AAA"),
        ("ruAАА", "AAA"),  # Cyrillic А as published in a release title
        ("AA-.ru", "AA-"),
        ("AA–|ru|", "AA-"),  # NRA: en dash
        ("BBB+|ru|", "BBB+"),
        ("ruBB-", "BB-"),
        ("ruC", "C"),
        ("CC.ru", "CC"),
        ("D", "D"),
        ("a+.ru", None),  # NKR standalone credit assessment (lower case)
        ("ВВВ.esg", None),  # ESG
        ("PreАА-|ru|", None),  # NRA expected rating
        ("ruA(EXP)", None),
        ("рейтинг отозван", None),
        ("—", None),
        (None, None),
    ],
)
def test_normalize_grade(rating: str | None, grade: str | None) -> None:
    assert normalize_grade(rating) == grade


@pytest.mark.parametrize(
    ("text_", "action"),
    [
        ("Подтверждён", "affirmed"),
        ("Присвоен", "assigned"),
        ("Повышен", "upgraded"),
        ("Понижен", "downgraded"),
        ("Отозван", "withdrawn"),
        ("НКР снизило кредитный рейтинг ПАО «ГК «Самолет» с A.ru до BBB.ru", "downgraded"),
        ("«Эксперт РА» отозвал без подтверждения кредитный рейтинг", "withdrawn"),
        ("Соответствует", None),
        (None, None),
    ],
)
def test_normalize_action(text_: str | None, action: str | None) -> None:
    assert normalize_action(text_) == action


def test_watch_and_dates() -> None:
    assert is_watch("рейтинг на пересмотре с возможностью понижения")
    assert is_watch("Под наблюдением") and not is_watch("Снято с наблюдения")
    assert not is_watch("стабильный") and not is_watch("-")
    assert parse_ru_date("19.10.2026") == date(2026, 10, 19)
    assert parse_ru_date("—") is None
    with pytest.raises(FormatDriftError):
        parse_ru_date("19/10/2026")


def test_htmldom_tolerates_stray_tags() -> None:
    root = parse_html('<div class="a b"><span>x</a></span><p>y<br>z</p><script>no</script></div>')
    (div,) = root.find_all("div", "b")
    assert div.text() == "x y z"
    assert div.find("p") is not None and root.find("script") is None


# ----------------------------------------------------------------------------------- NKR


def test_nkr_issues() -> None:
    recs = {r.object_key: r for r in nkr.parse_issues(raw("nkr/issues.xlsx"))}
    assert len(recs) == 172
    x5 = recs["RU000A10F9F8"]
    assert (x5.rating, x5.grade, x5.inn, x5.rating_date, x5.is_withdrawn) == (
        "AAA.ru", "AAA", "9722079341", date(2026, 10, 19), False,
    )  # fmt: skip
    assert x5.press_release == "https://ratings.ru/ratings/press-releases/X5-RA-bonds-191026/"
    mts = recs["RU000A104T04"]  # matured, withdrawn: kept with rating None
    assert (mts.rating, mts.action, mts.action_raw, mts.is_withdrawn) == (
        None, "withdrawn", "рейтинг отозван", True,
    )  # fmt: skip
    assert all(r.object_kind == "issue" and r.scale == "national_ru" for r in recs.values())


def test_nkr_issuers() -> None:
    recs = {r.object_key: r for r in nkr.parse_issuers(raw("nkr/issuers.xlsx"))}
    assert len(recs) == 260  # two foreign issuers without INN are keyed by the NKR id
    assert recs["nkr:1224"].object_name == "«Танжент Памп Кампани Холдингс Лтд.»"
    sam = recs["9731004688"]
    assert (sam.rating, sam.outlook, sam.ogrn, sam.industry) == (
        "BBB.ru", "негативный", "1187746590283", "Строительство и девелопмент",
    )  # fmt: skip
    idel = recs["0261012138"]
    assert idel.watch == idel.outlook == "рейтинг на пересмотре с возможностью понижения"
    assert recs["7718122921"].is_withdrawn and recs["7718122921"].outlook is None


def test_nkr_drift() -> None:
    with pytest.raises(FormatDriftError):
        nkr.parse_issues(raw("nkr/issuers.xlsx"))  # other columns
    with pytest.raises(FormatDriftError):
        nkr.parse_issuers(b"<html>maintenance</html>")


# ----------------------------------------------------------------------------------- NRA


def test_nra_export_link_and_actions() -> None:
    url = nra.parse_export_url(text("nra/ratings_page.html.gz"))
    assert url.startswith("https://www.ra-national.ru/wp-load.php?") and "export_id=20" in url
    with pytest.raises(FormatDriftError):
        nra.parse_export_url("<html></html>")
    e = nra.parse_actions(raw("nra/export.xlsx"))
    assert (e.rows, e.skipped_non_credit, len(e.actions), len(e.states)) == (983, 356, 627, 214)
    states = {(s.object_kind, s.object_key): s for s in e.states}
    kamaz = states[("issue", "RU000A0ZZ7G1")]
    assert (kamaz.rating, kamaz.grade, kamaz.action, kamaz.inn) == (
        "A+|ru|", "A+", "assigned", "1650032058",
    )  # fmt: skip
    zetta = states[("issuer", "7702073683")]
    assert (zetta.rating, zetta.grade, zetta.action, zetta.outlook, zetta.rating_date) == (
        "AA–|ru|", "AA-", "affirmed", "Стабильный", date(2026, 10, 16),
    )  # fmt: skip
    assert zetta.industry == "Cтраховые организации / Финансовая деятельность"
    pz = states[("issuer", "5203000478")]  # withdrawn on 21.10.2026
    assert (pz.rating, pz.is_withdrawn, pz.action) == (None, True, "withdrawn")
    smlt = states[("issuer", "7705003797")]
    assert smlt.watch == "Под наблюдением"
    # history: every credit action of an object, one row per release
    pz_actions = [a for a in e.actions if a.object_key == "5203000478"]
    assert len(pz_actions) > 1 and pz_actions[-1].action == "withdrawn"


# ----------------------------------------------------------------------------- RA Expert


def test_raexpert_sitemap_and_lists() -> None:
    assert raexpert.parse_sitemap_index(raw("raexpert/sitemap.xml")) == [
        "https://raexpert.ru/sitemap0.xml"
    ]
    rel = raexpert.parse_sitemap_releases(raw("raexpert/sitemap0.xml.gz"))
    assert len(rel) == 20934
    assert ("https://raexpert.ru/releases/2003/Aug05/", date(2003, 8, 5)) in rel
    assert raexpert.release_date("https://raexpert.ru/releases/2026/oct20d/") == date(2026, 10, 20)
    assert raexpert.page_kind("https://raexpert.ru/database/companies/aeroflot/") == "company"
    assert raexpert.canonical_url("http://raexpert.ru/releases/2026/oct20d?pdf") == (
        "https://raexpert.ru/releases/2026/oct20d/"
    )
    assert raexpert.canonical_url("https://example.com/database/companies/x/") is None
    found = raexpert.parse_list(text("raexpert/list_credits.html.gz"), "https://raexpert.ru/")
    kinds = [k for k, _ in found]
    assert kinds.count("company") == 19 and kinds.count("release") == 19
    with pytest.raises(FormatDriftError):
        raexpert.parse_list("<html><table><thead></thead></table></html>", "https://raexpert.ru/")


def test_raexpert_release() -> None:
    r = raexpert.parse_release(
        text("raexpert/release_2026_oct20d.html.gz"), "https://raexpert.ru/releases/2026/oct20d/"
    )
    assert r.day == date(2026, 10, 20) and r.category == "Рейтинговые действия"
    assert r.title.startswith("«Эксперт РА» подтвердил кредитные рейтинги облигаций Амурской")
    assert r.linked == [
        ("bond", "https://raexpert.ru/database/securities/bonds/1000061013/"),
        ("bond", "https://raexpert.ru/database/securities/bonds/1000067374/"),
        ("region", "https://raexpert.ru/database/regions/amur/"),
    ]


def test_raexpert_company_watch_and_withdrawal() -> None:
    url = "https://raexpert.ru/database/companies/1000040168/"
    e = raexpert.parse_entity(text("raexpert/company_1000040168.html.gz"), url)
    assert (e.inn, e.ogrn) == ("5829060635", "1055801700239")
    states, actions = raexpert.entity_records(e)
    (s,) = states
    assert (s.rating, s.grade, s.outlook, s.watch, s.rating_date) == (
        "ruC", "C", "Развивающийся", "Рейтинг под наблюдением", date(2026, 10, 21),
    )  # fmt: skip
    assert s.press_release == "https://raexpert.ru/releases/2026/oct21b/"
    assert len(actions) == 8
    withdrawn = [a for a in actions if a.action == "withdrawn"]
    assert [a.action_date for a in withdrawn] == [date(2026, 4, 28)]
    nkk = raexpert.parse_entity(
        text("raexpert/company_1000030255.html.gz"),
        "https://raexpert.ru/database/companies/1000030255/",
    )
    (s,) = raexpert.entity_records(nkk)[0]
    assert (s.rating, s.is_withdrawn, s.action, s.action_raw) == (
        None, True, "withdrawn", "отозван",
    )  # fmt: skip


def test_raexpert_region_and_bond() -> None:
    region = raexpert.parse_entity(
        text("raexpert/region_amur.html.gz"), "https://raexpert.ru/database/regions/amur/"
    )
    (s,) = raexpert.entity_records(region)[0]
    assert (s.object_key, s.rating, s.grade, s.inn) == ("raexpert:region:amur", "ruA+", "A+", None)
    bond = raexpert.parse_entity(
        text("raexpert/bond_1000067374.html.gz"),
        "https://raexpert.ru/database/securities/bonds/1000067374/",
    )
    assert bond.region_url == "https://raexpert.ru/database/regions/amur/"
    (s,), actions = raexpert.entity_records(bond)
    assert (s.object_kind, s.object_key, s.rating, s.outlook) == (
        "issue", "RU000A10D4D6", "ruA+", None,
    )  # fmt: skip
    assert s.extra["region_page"] == "https://raexpert.ru/database/regions/amur/"
    assert [a.rating for a in actions] == ["ruA", "ruA", "ruA+", "ruA+"]
    nlmk = raexpert.parse_entity(
        text("raexpert/company_nlmk.html.gz"),
        "https://raexpert.ru/database/companies/novolipetskii_metallur-_gicheskii_kombinat/",
    )
    assert nlmk.industry == "черная металлургия"


def test_raexpert_only_russian_national_scale() -> None:
    """A non-resident bank (recorded during the live crawl of 2026-10-21) with three blocks:
    RF national scale (kept), Belarus national scale ``by…`` under the same header and an
    international-scale table (both skipped, the page does not fail)."""
    e = raexpert.parse_entity(
        text("raexpert/company_1000042599.html.gz"),
        "https://raexpert.ru/database/companies/1000042599/",
    )
    assert [(b.rating_type, b.rows[0].rating) for b in e.blocks] == [
        ("Рейтинг кредитоспособности банка-нерезидента по нац. рейтинговой шкале РФ", "ruBBB-")
    ]
    (s,) = raexpert.entity_records(e)[0]
    assert (s.object_key, s.grade) == ("raexpert:company:1000042599", "BBB-")  # no INN


def test_raexpert_drift() -> None:
    page = text("raexpert/bond_1000067374.html.gz")
    with pytest.raises(FormatDriftError, match="no ISIN"):
        raexpert.parse_entity(
            page.replace("RU000A10D4D6", "n/a"),
            "https://raexpert.ru/database/securities/bonds/1000067374/",
        )
    with pytest.raises(FormatDriftError, match="header"):
        raexpert.parse_entity(
            page.replace("<th>Национальная шкала</th>", "<th>Шкала</th>"),
            "https://raexpert.ru/database/securities/bonds/1000067374/",
        )


# --------------------------------------------------------------------- sector by agency


def test_agency_sector_rules() -> None:
    m = rules.sector_map()
    assert rules.agency_sector([("nkr", "Банки")], m) == ("banks", "agency:nkr:Банки")
    nra_bank = ("nra", "Банки / Финансовая деятельность")
    assert rules.agency_sector([("nkr", "Банки"), nra_bank], m) is not None
    assert rules.agency_sector([("nkr", "Банки"), ("raexpert", "черная металлургия")], m) is None
    assert rules.agency_sector([("nkr", "ИТ и телекоммуникации")], m) is None  # ambiguous
    assert rules.agency_sector([("nkr", "Торговля")], m) is None  # distributors of every kind
    res = rules.assign_sector(
        inn="1", name="ООО Ромашка", short_name=None, bond_classes=["corporate"],
        agency_industries=[("raexpert", "черная металлургия")],
    )  # fmt: skip
    assert res is not None and (res.sector, res.source) == ("metals_mining", "agency")
    # gap filler only: a name rule or a class rule keeps its sector
    res = rules.assign_sector(
        inn="1", name="ПАО Транснефть", short_name=None, bond_classes=["corporate"],
        agency_industries=[("nkr", "Транспорт и логистика")],
    )  # fmt: skip
    assert res is not None and (res.sector, res.source) == ("oil_gas", "rule")
    res = rules.assign_sector(
        inn="1", name="Амурская область", short_name=None, bond_classes=["subfederal"],
        agency_industries=[("nkr", "Банки")],
    )  # fmt: skip
    assert res is not None and res.source == "rule"


# ---------------------------------------------------------------------------- the job


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def _run(settings: Settings, db: Database, **params: Any) -> JobOutcome:
    return run_job("ratings_agencies", params, settings=settings, db=db, http_factory=_http_factory)


RAEX_PAGES = {
    "/sitemap.xml": "raexpert/sitemap.xml",
    "/sitemap0.xml": "raexpert/sitemap0.xml.gz",
    "/ratings/credits/": "raexpert/list_credits.html.gz",
    "/releases/2026/oct21b/": "raexpert/release_2026_oct21b.html.gz",
    "/releases/2026/oct21c/": "raexpert/release_2026_oct21c.html.gz",
    "/database/companies/1000040168/": "raexpert/company_1000040168.html.gz",
    "/database/companies/novolipetskii_metallur-_gicheskii_kombinat/": (
        "raexpert/company_nlmk.html.gz"
    ),
}


def _mock_agencies(router: respx.Router) -> None:
    router.get(nkr.BASE_URL + nkr.ISSUES_PATH).respond(200, content=raw("nkr/issues.xlsx"))
    router.get(nkr.BASE_URL + nkr.ISSUERS_PATH).respond(200, content=raw("nkr/issuers.xlsx"))
    router.get(nra.BASE_URL + nra.RATINGS_PATH).respond(
        200, content=raw("nra/ratings_page.html.gz"), headers={"content-type": "text/html"}
    )
    router.get(url__startswith=nra.BASE_URL + "/wp-load.php").respond(
        200, content=raw("nra/export.xlsx")
    )

    def raex(request: httpx.Request) -> httpx.Response:
        name = RAEX_PAGES.get(request.url.path)
        if name is None:
            return httpx.Response(404, text="not recorded")
        return httpx.Response(200, content=raw(name), headers={"content-type": "text/html"})

    router.get(url__regex=r"^https://raexpert\.ru/").mock(side_effect=raex)


def _seed(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO ref.issuer (issuer_id, name, inn) VALUES
            (100, 'ПАО "НЛМК"', '4823006703'),
            (101, 'АО "РЕГИОН-ПРОДУКТ"', '5829060635'),
            (102, 'ПАО "Совкомбанк"', '4401116480'),
            (103, 'ПАО "Корпоративный центр ИКС 5"', '9722079341'),
            (104, 'ПАО "КАМАЗ"', '1650032058'),
            (105, 'ООО "Ромашка"', '0000000001')
        """
    )
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, issuer_id, bond_class, coupon_type, coupon_type_basis,
                              status, maturity_date, is_perpetual, is_structured, face_unit)
        VALUES
            ('RU000A10F9F8', 'RU000A10F9F8', 103, 'corporate', 'fixed', 'moex_label', 'active',
             '2036-09-26', false, false, 'RUB'),
            ('RU000A0ZZ7G1', 'RU000A0ZZ7G1', 104, 'corporate', 'fixed', 'moex_label', 'active',
             '2027-01-01', false, false, 'RUB'),
            ('NLMK-1', 'RU000XNLMK01', 100, 'corporate', 'fixed', 'moex_label', 'active',
             '2028-01-01', false, false, 'RUB'),
            ('ROM-1', 'RU000XROM001', 105, 'corporate', 'fixed', 'moex_label', 'active',
             '2028-01-01', false, false, 'RUB')
        """
    )
    conn.commit()


def test_ratings_agencies_loads_maps_and_reports_coverage(
    respx_mock: respx.Router,
    conn: Conn,
    settings: Settings,
    db: Database,
    today_is: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today_is(TODAY)
    monkeypatch.setattr(raexpert, "LIST_PATHS", ("/ratings/credits/",))
    _seed(conn)
    _mock_agencies(respx_mock)
    # budget: 1 list + 2 sitemaps + 2 releases of 2026-10-21 (oct21b, oct21c in the list)
    # + the 2 companies they link
    out = _run(settings, db, since_days=0, max_requests=7)
    assert out.status == "succeeded", out
    res = out.result
    assert res["raexpert"]["releases_fetched"] == 2 and res["raexpert"]["objects_fetched"] == 2
    assert res["raexpert"]["backlog"] == {"releases": 0, "objects": 17}

    counts = {
        r["agency"]: (r["issue"], r["issuer"])
        for r in conn.execute(
            "SELECT agency, count(*) FILTER (WHERE object_kind = 'issue') AS issue, "
            "count(*) FILTER (WHERE object_kind = 'issuer') AS issuer "
            "FROM ref.agency_rating GROUP BY agency"
        ).fetchall()
    }
    assert counts == {"nkr": (172, 260), "nra": (47, 167), "raexpert": (0, 2)}

    bonds = {
        (r["secid"], r["source"]): r
        for r in conn.execute("SELECT * FROM ref.bond_rating ORDER BY secid").fetchall()
    }
    x5 = bonds[("RU000A10F9F8", "nkr")]
    assert (x5["agency"], x5["scale"], x5["rating"], x5["grade"], x5["issuer_id"]) == (
        "НКР", "national_ru", "AAA.ru", "AAA", 103,
    )  # fmt: skip
    assert x5["evidence"]["mapping_basis"] == "isin"
    kamaz = bonds[("RU000A0ZZ7G1", "nra")]
    assert (kamaz["agency"], kamaz["rating"], kamaz["action"]) == ("НРА", "A+|ru|", "assigned")

    issuers = {
        (r["issuer_id"], r["source"]): r
        for r in conn.execute("SELECT * FROM ref.issuer_rating").fetchall()
    }
    nlmk = issuers[(100, "raexpert")]
    assert (nlmk["agency"], nlmk["rating"], nlmk["outlook"], nlmk["rating_date"]) == (
        "Эксперт РА", "ruAAA", "Стабильный", TODAY,
    )  # fmt: skip
    assert issuers[(101, "raexpert")]["watch"] == "Рейтинг под наблюдением"
    assert issuers[(102, "nkr")]["rating"] == "AA.ru"
    assert (104, "nra") in issuers and (105, "nkr") not in issuers

    cov = res["coverage"]["active_non_structured"]
    assert cov == {
        "bonds": 4, "issue_rated": 2, "issuer_rated": 3, "any_rated": 3,
        "rub_corporate": 4, "rub_corporate_rated": 3,
    }  # fmt: skip

    # raw first: every response captured, the NRA export key never stored
    eps = {
        (r["source"], r["endpoint"]): r["n"]
        for r in conn.execute(
            "SELECT source, endpoint, count(*) AS n FROM ops.raw_capture GROUP BY 1, 2"
        ).fetchall()
    }
    assert eps == {
        ("nkr", "issues_xlsx"): 1, ("nkr", "issuers_xlsx"): 1,
        ("nra", "ratings_page"): 1, ("nra", "export_xlsx"): 1,
        ("raexpert", "list"): 1, ("raexpert", "sitemap"): 2, ("raexpert", "release"): 2,
        ("raexpert", "company"): 2,
    }  # fmt: skip
    urls = [r["url_redacted"] for r in conn.execute("SELECT url_redacted FROM ops.raw_capture")]
    assert not any("security_key" in u for u in urls)
    assert not any(re.search(r"security_key|100c906f", str(r)) for r in bonds.values())

    # identical reload: no data change, no history
    hist = "SELECT count(*) AS n FROM ops.row_history WHERE table_name LIKE 'ref.%%rating%%'"
    before = conn.execute(hist).fetchone()
    out2 = _run(settings, db, since_days=0, max_requests=3)
    assert out2.status == "succeeded", out2
    assert conn.execute(hist).fetchone() == before


def test_ratings_agencies_one_agency_failing_is_partial(
    respx_mock: respx.Router, conn: Conn, settings: Settings, db: Database
) -> None:
    _seed(conn)
    _mock_agencies(respx_mock)
    respx_mock.get(nkr.BASE_URL + nkr.ISSUERS_PATH).respond(
        200, text="<html>maintenance</html>", headers={"content-type": "text/html"}
    )
    out = _run(settings, db, agencies=["nkr", "nra"])
    assert out.status == "partial", out
    run = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert run is not None and "nkr: FormatDriftError" in run["error"]
    n = conn.execute("SELECT count(*) AS n FROM ref.agency_rating WHERE agency = 'nra'").fetchone()
    assert n is not None and n["n"] > 0


def test_rating_datasets_registered_for_freshness() -> None:
    from mdhub.datasets import get_dataset

    for agency in ("nkr", "nra", "raexpert"):
        spec = get_dataset(f"ratings_{agency}")
        assert (spec.table, spec.frequency, spec.where) == (
            "ref.rating_action", "event", f"agency = '{agency}'",
        )  # fmt: skip


def test_ratings_agencies_rejects_unknown_agency(settings: Settings, db: Database) -> None:
    with pytest.raises(ValueError, match="unknown agencies"):
        _run(settings, db, agencies=["acra"])


def test_region_rating_maps_through_its_bonds(
    conn: Conn, settings: Settings, db: Database, respx_mock: respx.Router, today_is: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """An RA Expert region (no INN) maps to the MOEX issuer of its rated bonds."""
    today_is(date(2026, 10, 20))
    monkeypatch.setattr(raexpert, "LIST_PATHS", ())
    conn.execute(
        "INSERT INTO ref.issuer (issuer_id, name, inn) VALUES (200, 'Амурская область', NULL)"
    )
    conn.execute(
        """
        INSERT INTO ref.bond (secid, isin, issuer_id, bond_class, coupon_type, coupon_type_basis,
                              status, maturity_date, is_perpetual, is_structured, face_unit)
        VALUES ('RU000A10D4D6', 'RU000A10D4D6', 200, 'subfederal', 'fixed', 'moex_label',
                'active', '2027-10-17', false, false, 'RUB')
        """
    )
    conn.commit()
    pages = {
        "/sitemap.xml": "raexpert/sitemap.xml",
        "/sitemap0.xml": "raexpert/sitemap0.xml.gz",
        "/releases/2026/oct20d/": "raexpert/release_2026_oct20d.html.gz",
        "/database/securities/bonds/1000067374/": "raexpert/bond_1000067374.html.gz",
        "/database/securities/bonds/1000061013/": "raexpert/bond_1000061013.html.gz",
        "/database/regions/amur/": "raexpert/region_amur.html.gz",
    }
    respx_mock.get(url__regex=r"^https://raexpert\.ru/").mock(
        side_effect=lambda req: (
            httpx.Response(200, content=raw(pages[req.url.path]))
            if req.url.path in pages
            else httpx.Response(404)
        )
    )
    # oct20d is not in the (lagging) sitemap: it reaches the crawl through a list page in
    # production; here it is queued as a list page would queue it
    conn.execute(
        "INSERT INTO ref.agency_page (agency, url, kind, page_date) VALUES "
        "('raexpert', 'https://raexpert.ru/releases/2026/oct20d/', 'release', '2026-10-20')"
    )
    conn.commit()
    out = _run(settings, db, agencies=["raexpert"], since_days=0, max_requests=6)
    assert out.status == "succeeded", out
    row = conn.execute("SELECT * FROM ref.issuer_rating WHERE issuer_id = 200").fetchone()
    assert row is not None
    assert (row["rating"], row["evidence"]["mapping_basis"]) == ("ruA+", "region_bond_isin")
    b = conn.execute("SELECT * FROM ref.bond_rating WHERE secid = 'RU000A10D4D6'").fetchone()
    assert b is not None and (b["agency"], b["rating"]) == ("Эксперт РА", "ruA+")


# ------------------------------------------------- RA Expert request budget (issue #22)

RAEX_LISTS = ("/ratings/a/", "/ratings/b/", "/ratings/c/")
RAEX_CHILD_SITEMAPS = ("/sitemap1.xml", "/sitemap2.xml", "/sitemap3.xml")
RAEX_DISCOVERY = len(RAEX_LISTS) + 1 + len(RAEX_CHILD_SITEMAPS)  # lists + index + children


def _sitemap_index(children: tuple[str, ...]) -> bytes:
    locs = "".join(f"<sitemap><loc>https://raexpert.ru{c}</loc></sitemap>" for c in children)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</sitemapindex>'
    ).encode()


# a child sitemap with one (old, out-of-window) release: parses, queues nothing
_CHILD_SITEMAP = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b"<url><loc>https://raexpert.ru/releases/2004/mar04/</loc></url></urlset>"
)


def _mock_raexpert_counting(router: respx.Router) -> list[str]:
    """Synthetic site: 3 list pages (the recorded credits list), a sitemap index linking 3
    children; releases and companies from the fixtures. Returns the requested paths."""
    calls: list[str] = []
    pages: dict[str, bytes] = {
        "/sitemap.xml": _sitemap_index(RAEX_CHILD_SITEMAPS),
        **dict.fromkeys(RAEX_CHILD_SITEMAPS, _CHILD_SITEMAP),
        **dict.fromkeys(RAEX_LISTS, raw("raexpert/list_credits.html.gz")),
        **{p: raw(n) for p, n in RAEX_PAGES.items() if p.startswith(("/releases", "/database"))},
    }

    def raex(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not recorded")
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    router.get(url__regex=r"^https://raexpert\.ru/").mock(side_effect=raex)
    return calls


@pytest.mark.parametrize(
    ("budget", "phase", "skipped"),
    [
        (0, "lists", {"lists": 3, "sitemap_index": True, "sitemaps": 0}),
        (1, "lists", {"lists": 2, "sitemap_index": True, "sitemaps": 0}),
        (3, "sitemaps", {"lists": 0, "sitemap_index": True, "sitemaps": 0}),
        (4, "sitemaps", {"lists": 0, "sitemap_index": False, "sitemaps": 3}),
        (6, "sitemaps", {"lists": 0, "sitemap_index": False, "sitemaps": 1}),
        (RAEX_DISCOVERY, "releases", None),  # oct21b, oct21c of the list are due
        (RAEX_DISCOVERY + 1, "releases", None),
        (RAEX_DISCOVERY + 2, "objects", None),  # the 2 companies they link (+ 17 listed)
        (RAEX_DISCOVERY + 3, "objects", None),
    ],
)
def test_raexpert_budget_caps_every_phase(
    respx_mock: respx.Router,
    conn: Conn,
    settings: Settings,
    db: Database,
    today_is: Any,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
    phase: str,
    skipped: dict[str, Any] | None,
) -> None:
    today_is(TODAY)
    monkeypatch.setattr(raexpert, "LIST_PATHS", RAEX_LISTS)
    calls = _mock_raexpert_counting(respx_mock)
    out = _run(settings, db, agencies=["raexpert"], since_days=0, max_requests=budget)
    res = out.result["raexpert"]
    assert len(calls) == budget, calls  # never one page more than the budget
    assert (res["requests"], res["budget_exhausted"]) == (budget, phase)
    assert res["discovery_complete"] is (skipped is None)
    assert res.get("discovery_skipped") == skipped
    caps = conn.execute(
        "SELECT count(*) AS n FROM ops.raw_capture WHERE source = 'raexpert'"
    ).fetchone()
    assert caps is not None and caps["n"] == budget  # every fetched page captured
    if skipped is None:
        # a backlog left for later runs is reported, not a failure
        assert out.status == "succeeded", out
        assert res["backlog"]["releases"] + res["backlog"]["objects"] > 0
    else:
        assert out.status == "partial", out
        run = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
        assert run is not None and "before discovery finished" in run["error"]


def test_raexpert_budget_rejects_negative_before_network(
    respx_mock: respx.Router, settings: Settings, db: Database
) -> None:
    calls = _mock_raexpert_counting(respx_mock)
    with pytest.raises(ValueError, match="max_requests"):
        _run(settings, db, agencies=["raexpert"], max_requests=-1)
    assert calls == []


def test_raexpert_budget_continues_across_runs(
    respx_mock: respx.Router,
    conn: Conn,
    settings: Settings,
    db: Database,
    today_is: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small bounded runs make progress on the stored backlog and never duplicate actions."""
    today_is(TODAY)
    monkeypatch.setattr(raexpert, "LIST_PATHS", RAEX_LISTS)
    calls = _mock_raexpert_counting(respx_mock)

    def run(extra: int) -> dict[str, Any]:
        calls.clear()
        budget = RAEX_DISCOVERY + extra
        out = _run(settings, db, agencies=["raexpert"], since_days=0, max_requests=budget)
        assert out.status == "succeeded", out
        assert len(calls) == budget
        return dict(out.result["raexpert"])

    def actions() -> tuple[int, int]:
        r = conn.execute(
            "SELECT count(*) AS n, count(DISTINCT (object_key, rating_type, scale, action_date, "
            "action_ref)) AS d FROM ref.rating_action WHERE agency = 'raexpert'"
        ).fetchone()
        assert r is not None
        return r["n"], r["d"]

    first = run(3)  # both releases + one linked company
    assert (first["releases_fetched"], first["objects_fetched"]) == (2, 1)
    assert first["budget_exhausted"] == "objects"
    second = run(1)  # releases done: the other linked company
    assert (second["releases_fetched"], second["objects_fetched"]) == (0, 1)
    assert second["backlog"]["releases"] == 0
    stored = conn.execute(
        "SELECT count(DISTINCT object_key) AS n FROM ref.agency_rating WHERE agency = 'raexpert'"
    ).fetchone()
    assert stored is not None and stored["n"] == 2
    n_actions = actions()
    assert n_actions[0] == n_actions[1] > 0

    # a newer release links both companies again: re-read, nothing duplicated
    conn.execute(
        "UPDATE ref.agency_page SET linked_at = now() WHERE agency = 'raexpert' "
        "AND kind = 'company' AND fetched_at IS NOT NULL"
    )
    conn.commit()
    third = run(2)
    assert third["objects_fetched"] == 2
    assert actions() == n_actions
