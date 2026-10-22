# Fixtures of the reference-rate, cross-rate and proxy-curve sources (WEB-16)

**Recorded** (real responses, fetched 2026-10-21 from the development environment, bytes as
received):

| file | request |
|---|---|
| `ecb_est_2026-09-28_2026-10-20.csv` | `data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?format=csvdata&detail=dataonly&startPeriod=2026-09-28&endPeriod=2026-10-20` |
| `ecb_exr_2026-10-16_2026-10-20.csv` | `.../EXR/D..EUR.SP00.A?format=csvdata&detail=dataonly&startPeriod=2026-10-16&endPeriod=2026-10-20` |
| `ecb_yc_py_2026-10-15_2026-10-20.csv` | `.../YC/B.U2.EUR.4F.G_N_A.SV_C_YM.PY_3M+...+PY_30Y?format=csvdata&detail=dataonly&...` |
| `nyfed_sofr_2026-09-28_2026-10-20.json` | `markets.newyorkfed.org/api/rates/secured/sofr/search.json?startDate=2026-09-28&endDate=2026-10-20` |
| `nyfed_sofrai_2026-09-28_2026-10-20.json` | `.../sofrai/search.json?startDate=2026-09-28&endDate=2026-10-20` |
| `nyfed_sofr_2018-04_first.json` | `.../sofr/search.json?startDate=2018-03-26&endDate=2018-04-06` (first SOFR publications) |
| `ust_par_202610.xml` | `home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=202610` |

**Format-derived, not recorded** - `api.statistiken.bundesbank.de`, `api.db.nomics.world` and
`fred.stlouisfed.org` do not answer from the development environment (egress policy). These
files follow the documented export formats (Bundesbank CSV `lang=en`: BOM, CRLF, metadata lines,
every calendar day, `.` = no value; DBnomics `/v22/series` JSON with parallel arrays and `"NA"`;
FRED `fredgraph.csv` with `observation_date` and empty missing values) and carry the **real
values** of the recorded ECB / NY Fed files above (FRED €STR: 2026-10-02 left empty to exercise a
missing value). `bundesbank_bbsis_par_synthetic.csv` has synthetic round test values. Verify the
live formats on the server with `mdhub probe-sources` before relying on these fallbacks.

| file | stands for |
|---|---|
| `bundesbank_bbmmb_estr_2026-09-28_2026-10-20.csv` | `api.statistiken.bundesbank.de/rest/data/BBMMB/D.EU000A2X2A25.WT?format=csv&lang=en&...` |
| `bundesbank_bbsis_par_synthetic.csv` | `.../BBSIS/D.I.ZAR.ZI.EUR.S1311.B.A604.R01XX+...+R30XX.R.A.A._Z._Z.A?format=csv&lang=en&...` |
| `dbnomics_ecb_est.json` | `api.db.nomics.world/v22/series/ECB/EST/B.EU000A2X2A25.WT?observations=1` |
| `fred_estr_2026-09-28_2026-10-20.csv` | `fred.stlouisfed.org/graph/fredgraph.csv?id=ECBESTRVOLWGTTRMDMNRT&cosd=...&coed=...` |
| `fred_sofr_2026-09-28_2026-10-20.csv` | `...?id=SOFR,SOFR30DAYAVG,SOFR90DAYAVG,SOFR180DAYAVG,SOFRINDEX&...` |

`fred_h10_outside_range_synthetic.csv` is a **synthetic regression fixture**, not a recorded
response or historical market values. It reproduces the oversized-response behavior confirmed
in issue #23: a bounded H.10 request returned observations dating back to 1971. It includes
dates before, on both bounds of, within, and after the requested 2026-10-16..2026-10-21 interval,
with both empty and `.` missing values. The loader must keep the full raw response while
writing only eligible observations. Tests for the other FRED callers use the existing ESTR
and SOFR CSVs with a narrower requested interval to exercise the same provider behavior.

CBR inflation (`../official/cbr_infl_2024-01_2026-09.html`, recorded:
`www.cbr.ru/hd_base/infl/?UniDbQuery.Posted=True&UniDbQuery.From=01.01.2024&UniDbQuery.To=30.09.2026`)
and the Rosstat price page excerpt (`../rosstat/price_page_excerpt.html`, three document items of
`rosstat.gov.ru/statistics/price` verbatim) are recorded too.
