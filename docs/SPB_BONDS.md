# SPB Russian bond chart history

The hub discovers bonds explicitly classified as Russian securities by SPB Exchange. These
instruments do not need a MOEX listing or a `ref.bond` row. `ref.spb_bond` is keyed by venue and
validated Russian ISIN; `md.spb_bond_daily` by venue, ISIN and chart date. Existing MOEX tables
and published snapshots keep their existing semantics.

## One source path and explicit meaning

1. `/api/reader/marketdata/default/rus`: select `securityKind=Облигации`, exclude foreign-issuer
   categories. This is the website's observed current universe, not a complete historical
   listing register. Neither a RUB currency nor chart-search `type=EQR` identifies a bond.
2. `/api/listing/v1/security/cardBond/ru/{idSecurities}`: validate ticker, numeric security ID,
   Russian classification (`sgroupSec=Российские ценные бумаги`), category and ISIN check digit.
3. `/api/reader/marketdata/archive/cb/active-days?year=YYYY`: find the latest full report dated
   before the current Moscow date, at most 31 calendar days old. Fetch the advertised official
   `/static/reports/results/YYYY-MM-DD-full.pdf` once per run. A single pypdf parser validates
   title/date, column order and exact ticker+ISIN rows; every discovered instrument needs a
   matching unit reference. Reports supply **units only**. Their closing prices, retained
   prices and zero placeholders never become chart bars. There is no alternate price feed.
4. `/api/reader/marketdata/charts/chistory`: request each discovered ticker with
   `resolution=1440` and UNIX-second bounds. Persist the returned positive, internally
   consistent OHLC numbers as exact decimals. A valid empty response writes no price.

`price_basis=chart_close` is explicit: the chart's close is **not the official closing price**.
For MGKL1P1 on 2026-11-10 these values were 7.43 and 7.23 respectively.
`price_unit=PCT` means percent of nominal/par value; currency codes mean amounts in that
currency. `currPrice` in the current-price JSON is not used for this mapping.

On 2026-11-11, XMED1P1's instrument card declared RUB, while the 2026-11-09 and 2026-11-10
reports declared PCT. The one rule for all instruments uses the dated report unit, retaining
`card_price_unit` and `unit_conflict` so consumers can see the discrepancy. Unknown units,
conflicting identities, missing/ambiguous report rows or inconsistent settlement currency
fail validation before chart rows are admitted. There is no per-ticker exception.

The report is a reference **observed at ingestion**. `unit_reference_date` names its date;
this does not prove the unit metadata was historically available or unchanged throughout a
backfilled range. `known_at` reflects when the hub actually learned a row. Each price row's
capture and run IDs identify the chart payload and the run's captured discovery/cards/report;
`ref.spb_bond.unit_capture_id` points directly to its report. No source latency or complete
trade/session coverage is promised by the website feed.

## Dates, incremental loads and history

Source daily timestamps must be UNIX seconds at UTC midnight: they are chart bucket date
labels. Load only dates strictly before **today in Moscow**, including after Moscow midnight
while UTC is still on the previous date. This excludes the current-date unfinished bar; an
older chart bar remains revisable and is never described as official-final data.

The job re-reads the last three completed calendar dates at 07:40 Moscow every calendar day.
It does not borrow MOEX holidays, filter weekends, or infer a session calendar from whether
an instrument happened to trade. `spb_bonds_daily` is registered as an event dataset: quality
checks freshness but creates no synthetic session gaps, and automatic history-head loading
is deliberately disabled. Freshness of the overall dataset does not establish freshness of
every instrument.

In the admin console, open **Jobs → spb_bonds_daily**. **Run now** with empty range fields
re-reads the last three completed dates; supply both dates to load an inclusive historical
range of at most 31 calendar days. Use additional bounded ranges for deeper history. The
existing job-request worker, backfill queue and retry controls run the same idempotent job.
No separate process or owner command line is needed.

A successful requested range records source-confirmed missing bars per ISIN in `ops.data_gap`
and captures the empty response; it makes no assertion that a session or a trade should have
occurred. Generic no-data grace/retry bookkeeping uses weekdays with no MOEX calendar. A row
for one ISIN cannot close another ISIN's no-data record. Missing dates are not zeroes and are
not forward-filled. Parsing is atomic per instrument/range, metadata is validated before
price loading, and a later source error fails the run while retaining earlier committed
instruments and discovery evidence. Changed source bars create ordinary revision history.

Read through the authenticated, bounded [history API](HISTORY_API.md).
