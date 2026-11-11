Public SPB Exchange responses fetched on 2026-11-11 (no account or credentials).

- `russian-market.json`: 21 explicit bonds plus two non-bond rows selected from
  `/api/reader/marketdata/default/rus`. This observed universe is not a complete-listing claim.
- `card-*.json`: `/api/listing/v1/security/cardBond/ru/{idSecurities}` for those bonds.
  XMED1P1 declares RUB, while the dated official report says PCT: this conflict is intentional.
- `archive.json`: November subset of `/api/reader/marketdata/archive/cb/active-days?year=2026`.
- `unit-reference-2026-11-{09,10}.pdf`: pages 1, 36, 47, 49, 53, 57, 61 from each official
  `/static/reports/results/YYYY-MM-DD-full.pdf`, copied unchanged with pypdf; all 21 bond rows
  and the title/column headers are retained. Used only as unit-reference fixtures.
- `chart-{MGKL1P1,XMED1P1}.json`: daily `chistory` responses (`resolution=1440`), requested
  2026-01-01 through 2026-11-11; actual returned dates end on 2026-11-10.

The official closing price for MGKL1P1 on 2026-11-10 is 7.23, whereas its chart close is
7.43. Tests must preserve the chart-close meaning rather than treating the report as prices.
