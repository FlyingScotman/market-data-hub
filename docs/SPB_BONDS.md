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

The job re-reads the last three completed calendar dates at 07:40 Moscow every calendar day (one run a day, see below).
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

A calendar date without a bar (weekend, holiday, a bond that did not trade) is normal for this
dataset: nothing is recorded in `ops.data_gap`, no backfill task is queued, and the chart
response stays captured as evidence. The run result counts `missing_dates` per ISIN; they are
not zeroes and are not forward-filled. Parsing is atomic per instrument/range and metadata is
validated before price loading. Changed source bars create ordinary revision history.

## A bond that fails validation, and format drift

One awkward bond does not stop the load. A bond is skipped, with a diagnostic in the run result
(`skipped`) and in the log, and the run ends `partial` (the reason lists the tickers), when

- its card contradicts the Russian-bond identity or classification, or its ISIN is invalid;
- it is missing from the dated unit report (typically a new issue the previous day's report does
  not list yet) or the card and the report disagree on the settlement currency;
- its chart answer is malformed (a bar outside the requested dates, a duplicate, non-positive or
  inconsistent prices). The response itself is kept in `ops.raw_capture`; no row is written.

The other bonds are loaded. A changed format breaks every card, report row or chart alike, so
more than one fifth of the discovered bonds (at least one; 4 of today's 21) skipped in one run is
treated as format drift: the run fails with the list of reasons. A malformed or unreadable
report, a changed column layout or an ambiguous row still fail the run at once.

## Reading the site: how often, and what costs a request

The owner allowed reading the public SPB Exchange website on 2026-11-11, up to several times
a day. Setting `MDHUB_ENABLE_SPB_BONDS` (default `true`) switches the reads off: the job then
ends `skipped` and the `spb` source probe sends nothing; stored prices stay and are served.

**What the site gives inside a trading day.** The daily chart (`chistory`, `resolution=1440`)
returns completed dates only: a request that included the current date (2026-11-11, saved in
`tests/fixtures/spb/chart-*.json`) answered up to the previous date, and the loader never asks
for the current date. Another run during the day therefore finds no new bar, so the job stays
**one run a day at 07:40 Moscow time**, which re-reads the last three completed dates (late
corrections of a bar are picked up there). The instrument list (`default/rus`) also carries
session aggregates of the current day (first, lowest, highest and last price, volumes) in
text form, but with no date and from a different endpoint than the chart; they are captured
with the list and are not stored as prices. Storing them as a separate, explicitly preliminary
series needs a check of their meaning against the live site (when they restart, which session
they cover) and an owner decision; see the open question in the pull request.

**Requests.** One run reads the list (1), every discovered bond's card (N), the report archive
(1, 2 around New Year) and the dated unit report (1, a PDF), then one chart per bond (N):
45 requests for the 21 bonds seen on 2026-11-11, paced at 1 request per second. The unit report
is dated and does not change: a rerun for the same report date (a retry, "Run now", a backfill
task) reads the units already stored and skips the PDF (44 requests) unless a card changed.
The `spb` probe sends one request an hour (24 a day, was 288) and none when switched off.

Read through the authenticated, bounded [history API](HISTORY_API.md).
