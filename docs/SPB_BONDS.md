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
   `/static/reports/results/YYYY-MM-DD-full.pdf` at most once per report date (a rerun for the same
   report date reads the units already stored). A single pypdf parser validates
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

The report is a reference **observed at ingestion**. `unit_reference_date` (on the bond and on
every bar) names its date; this does not prove the unit metadata was historically available or
unchanged throughout a backfilled range. `known_at` reflects when the hub actually learned a row. Each price row's
capture and run IDs identify the chart payload and the run's captured discovery/cards/report;
`ref.spb_bond.unit_capture_id` points directly to its report. No source latency or complete
trade/session coverage is promised by the website feed.

## A unit that changes, and the dates before the first report

A bar's unit is the bond's unit in the newest dated report; the bar stores that report's date as
`unit_reference_date`. Consumers read prices **only as percent of par**, so the rule is: **no
price is better than a price with a wrong or unproven unit.**

**How to read `trade_date` against `unit_reference_date`.** The unit is proven for a date only
when the report is dated that very day.

- `trade_date = unit_reference_date`: the unit is the one the report of that day says.
- `trade_date < unit_reference_date`: the unit was taken from a *later* report. This is the case
  for the first load of a bond's history (below) and for the dates a later report confirmed.
  It assumes that the unit did not change in between.
- `trade_date > unit_reference_date`: the report of that day was not published yet when the bar
  was stored, so the unit is that of an *earlier* report; the same assumption.

**The unit changes** (for example `PCT` to a currency). What the chart returns for old dates
after the change is not known: it may still hold the old numbers or it may have recomputed its
whole history in the new unit, and a number alone does not tell which. So the loader does not
guess, and it does not look at the report of the bond (`ref.spb_bond`, which the first run after
the change overwrites) either. It looks at the **bars it has stored**. If a bond has stored bars
whose unit differs from the newest report, then:

1. Those stored bars are **not touched at all**: not their price, not their unit, not their
   `unit_reference_date`, whatever date they have and whatever the chart returns now.
2. **No price is written** for the dates up to the *boundary*: the latest of the trade dates and
   report dates (`unit_reference_date`) of the bars with the other unit. That includes dates
   never stored before (a historical **Run now** over an old range writes nothing there) and
   corrections of old bars. The chart's answer stays in `ops.raw_capture`.
3. **No price is written for the window** between the boundary and the first date of the new
   unit. The new unit is proven from the first report that said it (or, once bars of the new
   unit are stored, from the first of those bars). Between the last date confirmed in the old
   unit and that report the change happened on an unknown day; the window is empty when
   consecutive reports changed, and up to 30 dates after an outage of the worker. Reading the
   reports in between (one PDF per date) would narrow it; the loader does not, to keep a run at
   one PDF.
4. Dates from the first date of the new unit on are written with the new unit and the date of
   the newest report.

The result does not depend on the order of runs: a daily run and a historical one, in either
order, or a run that failed after saving the bond's reference, give the same stored bars.
One consequence: if the first run after a change fails or skips the bond's chart, the new
unit starts from the report of the first successful run, and the earlier date is not written.

If the unit changes back, the bars of the middle period are the "other unit" bars, and every
date up to the boundary, including old bars of the matching unit, is left as it was.

**What the operator sees.** The run that finds the change, and every run until the first bar
of the new unit is stored, ends `partial` with `price unit changed X->Y for <ticker>: no price is
written for dates up to <date> (bars up to <date> keep X), Y from <date>`. After that the
fact stays visible but does not turn runs `partial`: `unit_changes` and
`unit_frozen` in the run result, and a log warning. `unit_frozen` holds per ISIN
`stored_unit`, `current_unit`, `frozen_through` (the boundary), `new_unit_from` and
`dates_not_written` (chart bars of the run's range that were not written). Nothing is migrated.
What to do about it is in the operator guide below.

**Dates before the first stored report** (first load of a bond's history): the unit of a bar
for a date earlier than its `unit_reference_date` is taken from the current report. This is an
**assumption**: the hub has no historical reports and does not fetch them. It is visible in the
data: such a bar has `trade_date < unit_reference_date`, and `unit_reference_date` is the date
of the report that defined the unit. No new column was needed (`md.spb_bond_daily` already
carries `unit_reference_date`), so there is no extra migration. The assumption is kept because
without it a new bond would have no history at all; it holds on every path that writes a bar:
a bar is always written with the unit and the date of the newest report, and the dates it
differs from tell the consumer how it was derived (see above).

## Dates, incremental loads and history

Source daily timestamps must be UNIX seconds at UTC midnight: they are chart bucket date
labels. Load only dates strictly before **today in Moscow**, including after Moscow midnight
while UTC is still on the previous date. This excludes the current-date unfinished bar; an
older chart bar remains revisable and is never described as official-final data.

The job re-reads the last three completed calendar dates at 07:40 Moscow every calendar day (one run a day, see below).
When the newest stored bar is older than that (the worker was down for more than three days), the
run starts the day after it instead, so the gap is read too, but never more than 31 dates back;
a longer outage needs bounded **Run now** ranges.
It does not borrow MOEX holidays, filter weekends, or infer a session calendar from whether
an instrument happened to trade. `spb_bonds_daily` is registered as an event dataset: quality
checks freshness but creates no synthetic session gaps, and automatic history-head loading
is deliberately disabled. Freshness of the overall dataset does not establish freshness of
every instrument.

In the admin console, open **Jobs → spb_bonds_daily**. **Run now** with empty range fields
re-reads the last three completed dates (or from the day after the newest stored bar, see above);
supply both dates to load an inclusive historical
range of at most 31 calendar days. Use additional bounded ranges for deeper history. The
existing job-request worker, backfill queue and retry controls run the same idempotent job.
No separate process or owner command line is needed.

A calendar date without a bar (weekend, holiday, a bond that did not trade) is normal for this
dataset: nothing is recorded in `ops.data_gap`, no backfill task is queued, and the chart
response stays captured as evidence. The run result counts `missing_dates` per ISIN; they are
not zeroes and are not forward-filled. Parsing is atomic per instrument/range and metadata is
validated before price loading. Changed source bars create ordinary revision history; a new
report date alone also updates the `unit_reference_date` of the re-read bars (they were
confirmed by a newer report), which is a revision without a price change.

Two run-level checks catch a changed request that makes every chart answer `[]`. A run with no
bar for any bond ends `partial` (`no bars for any bond in range <from>..<to>`) when the range
includes a weekday; a weekend-only range is `succeeded` with zero rows. The hub has no exchange
calendar for this source, so a range made only of weekday holidays (the New Year days, for
example) also ends `partial`: that is a false alarm, not a fault. If the charts of **all**
admitted bonds fail validation, the run fails as format drift, whatever the universe size.

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

**What the site gives inside a trading day.** In the saved snapshot (taken during the trading
day, in trading hours, 2026-11-11; `tests/fixtures/spb/chart-*.json`) the daily chart
(`chistory`, `resolution=1440`) had no bar for the current date although the request included
it. Whether the bar appears after the close has not been checked. The loader in any case takes
completed dates only and never asks for the current date. Another run during the day therefore finds no new bar, so the job stays
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
The `spb` probe sends one request every 8 hours (three a day, was 288) and none when switched
off. Together with the daily run that is the few reads a day the owner allowed.

**What the probe's rarity does to the console.** Nothing in the hub treats an old probe as a
problem: the Sources page shows only the probe's age (`7 h ago`), and the Overview counts a
source as a problem only when its last probe failed, its credentials were rejected, or it has
never been probed. So `spb` stays green between probes and no staleness threshold had to be
agreed for it (the hourly sources work the same way). The first probe goes out within about 5
minutes of the scheduler starting: the `source_probe` job runs every 5 minutes and decides per
source whether a probe is due.

Read through the authenticated, bounded [history API](HISTORY_API.md).

## For consumers of `/v1/history/spb-bonds`: percent of par only for `PCT`

The hub stores each price exactly as the source gave it, together with its unit
(`price_unit`); it converts nothing and relabels nothing.

- A row with `price_unit = PCT` is a price in **percent of nominal (par) value**.
- A row whose `price_unit` is a currency code (`RUB`, `USD`, ...) is an **absolute price** in
  that currency. It must **not** be read as a percent of par.
- A consumer that works only in percent of par takes the `PCT` rows and ignores the others, or
  converts the absolute price through the nominal itself (the hub does not do that).
- Filter on `price_unit` (and look at `unit_reference_date`, `unit_conflict`) of every row, not
  on the ISIN alone: a bond's unit can change over time, and its older rows keep the older unit.
  The three columns come with every response, also with `fields=close`.
- A `PCT` row with `trade_date = unit_reference_date` has the unit of that day's report. With
  `trade_date < unit_reference_date` the unit comes from a later report (history loaded for a
  bond, or dates a later report confirmed), with `trade_date >` from an earlier one: in both
  cases it assumes the unit did not change in between. A consumer that needs a proven unit
  takes only the rows where the two dates are equal.
- `unit_conflict = true`: the instrument card says another unit than the report; the report
  is used. Take such rows only if you accept that.
- After a change of the unit of a bond the hub leaves **gaps** instead of writing prices of
  unproven unit: no row for the dates up to the change and for the window around it. A missing
  date after a unit change is not a missing price in the source.

## Operator guide (new in 0.7.0)

**Nothing needs to be configured.** The job and the `spb` source are on by default. The server
needs outbound HTTPS (port 443) to `spbexchange.ru`, through the hub's usual egress setting
(`MDHUB_HTTP_PROXY`, if one is used). No account, key or credential is involved. Requests are
paced at 1 per second.

**What appears in the console after the update**

- **Jobs**: `spb_bonds_daily`, scheduled at 07:40 Moscow time every day. Its **Run now** has
  two optional date fields (both empty, or both filled; at most 31 completed dates).
- **Sources**: a row `spb` (SPB Exchange public website), probed every 8 hours.
- **Data**: the dataset `spb_bonds_daily` appears after the next run of the quality check
  (twice a day); it is an event dataset, so it never reports calendar gaps.
- **API**: `GET /v1/history/spb-bonds`, and the stored ISINs in `/v1/history/index`
  ([HISTORY_API.md](HISTORY_API.md)).

**On the day of the update the job is red, `missed`.** Job health expects the scheduled 07:40
run; until the scheduler has started one, the job is `missed`. **Run now does not clear it**:
only a run started by the schedule counts, a request from the console does not. This is not an
error; the status clears itself at the first 07:40 run.

**Checking the first load.** Open Jobs, press **Run now** on `spb_bonds_daily` with both date
fields empty, and open the run after a minute (about 45 requests at 1 per second). Expected:

- `succeeded`: all bonds were read. Weekend and holiday dates have no bars, which is normal.
- `partial`: it worked, with something to read in the reason: a bond skipped (a new issue that
  the report does not list yet, for instance), a changed price unit, or no bars for any bond on
  a weekday.
- `failed` with `N of M bonds failed validation`: more than a fifth of the bonds failed, which
  looks like a changed site format. The run stops there and lists the reasons. Report it.
- `failed` with `the charts of all N admitted bonds failed validation`: every bond's chart
  answer was rejected (one is within the skip limit, all of them are not), which also looks
  like a changed site format or request. The run stops and lists the reasons. Report it.

To load older history, use **Run now** with a first and a last date, in successive ranges of at
most 31 dates.

**If a bond's price unit changed** (a run ends `partial` with `price unit changed X->Y for
<ticker>`, and the run result has `unit_changes` and `unit_frozen` for it):

- What you see: the reason names the ticker, the two units, the date up to which no price is
  written and the date from which the new unit applies; `unit_frozen` in the result has the
  same dates and the number of chart dates left unwritten in that run.
- What it means: the data before the change stay as they are, in the old unit; new prices are
  written in the new unit; the dates of the window around the change are not written, and
  neither is anything older than the change if loaded later with **Run now**. Consumers that
  read percent of par get gaps for those dates, not wrong numbers, and no longer get prices
  for the bond after the change unless the new unit is `PCT`.
- What to do: **tell the owner** which bond and which dates (the ticker and the dates are in the
  run reason). Nothing needs to be repaired or reloaded; repeating **Run now** does not fill
  the gaps, by design. The run ends `partial` only until the first price of the new unit is
  stored; after that the same facts stay in the run result and the log.
- Open question, to settle once on the live site: whether the chart recomputes its history in
  the new unit after a change. If it does not, the unwritten old dates could be loaded
  safely; until it has been checked they are left empty.

**Switching it off.** There is no button for it in the console: the switch is a setting, and
changing it and restarting the worker is the fallback path until a button exists. Set
`MDHUB_ENABLE_SPB_BONDS=false` and restart the worker. After that the
job still starts at 07:40 but ends `skipped` ("SPB bonds disabled") without sending a request,
no `spb` probe is sent (its last row stays on the Sources page and grows old), and the stored
prices are still served by the API.
