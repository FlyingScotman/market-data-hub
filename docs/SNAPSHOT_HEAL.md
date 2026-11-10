# Self-healing of the published `eod_final` history

Job `snapshot_heal` (market-data-hub#138; owner decision of 2026-11-10: history repair must not
need manual runs on the server). Code: `src/mdhub/loaders/snapshot_heal.py`.

## What it repairs

| Defect | Detected as | Repair | Gap dataset (`/v1/gaps`) |
|---|---|---|---|
| **Missing cut**: a business day of `MOEX_STOCK` from `MDHUB_SNAPSHOT_HEAL_FROM` (default 2025-10-29) to the day before the previous business day has no `eod_final` market snapshot, although `md.bond_eod` / `md.bond_eod_yield` hold its bond quotes | no row of `ops.published_snapshot` with `kind = market`, `cut_type = eod_final`, that `business_date` | the cut is published as known now, bound to a static built for that date (`parameters.static_basis`, #102 / #104) - what `mdhub run snapshot_market -p mode=eod_final -p day=D` does | `snapshot_cut_missing` |
| **Cut on the static of its build day**: a current `eod_final` of a business date from 2026-01-05 to 2026-10-21 whose linked static has no `parameters.static_basis` (cuts published before contract 1.4; #101, #106) | the cut's static manifest has no `static_basis` | the cut is rebuilt with its own `cut_ts`: a correction that `supersedes` the old snapshot (which stays readable); what `snapshot_rebuild` does | `snapshot_cut_reissue` (one gap per cut: `key.cut_ts`) |

The reissue range is fixed in the code (`REISSUE_FROM`, `REISSUE_TO`): the one described in #106.
Cuts of other dates are never reissued by this job. The day before the previous business day is the
newest day it looks at: the previous business day belongs to the scheduled 08:00 cut (it waits for
its inputs; the `quality` job catches it up).

A business day can have more than one current `eod_final` cut. Each has its own reissue gap
(`key.cut_ts`) and its own check after the rebuild, so a run limit or a failure between two cuts of
one day loses nothing.

A missing day without bond quotes is **not** cut (a cut of nothing): its gap stays `open` with the
reason "no bond quotes in the store for the day yet" until the quotes are loaded (the ordinary
`bonds_eod` gap backfill); the next run then builds it.

A missing day whose **input data is still being filled** is not cut either. That is an `open` gap on
that day, or a `pending` / `running` backfill task whose range contains it, of a dataset the cut
reads (the datasets of `SNAPSHOT_INPUT_JOBS`: bond quotes, zero-coupon curve parameters, indices, FX,
futures, fixings, CPI, ...). The gap note names the dataset ("waiting for the input data of the day:
zcyc_params (open gap), ..."). Otherwise the cut would be built from incomplete inputs, the
autocorrection would publish a correction as soon as that backfill ends, and the consumer would
download the day twice. When the gaps and tasks are done, the next run builds the day.

## How it runs

* Polling rule, every 30 minutes, all days (`schedule.py`). The job lock keeps one run at a time.
  There is no start-up hook: after a deploy the first run is at the next :00 / :30, and the first
  cuts are built in the next night window.
* **Detection** (read-only SQL, cheap) runs at every fire and writes the gaps. **Repair** runs
  only inside `MDHUB_SNAPSHOT_HEAL_WINDOW` (default 21:00-06:00 MSK: after the evening loads and the
  20:30 quality run, before the 06:30 loads), builds at most `MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN` (10)
  cuts per run with `MDHUB_SNAPSHOT_HEAL_PAUSE_S` (20) seconds between two, and stops when the window
  closes. A build is one read-only transaction (about a minute) plus a short publish under the
  publish lock, so live jobs and cuts are not blocked. Missing cuts go first, oldest day first.
  At the defaults the window holds 18 runs of 10 cuts (up to 180 cuts a night), so about 249 cuts
  (45 missing, 204 reissues) take about a night and a half: the history is done in the second night.
* **Resumable, idempotent.** The state is the database: published cuts and gaps. An interrupted
  run (restart, crash, window end) loses nothing; the next run detects what is left and goes on.
  When nothing is left the run ends `skipped` ("nothing to heal") and publishes nothing.
* **Failures.** A cut that fails is noted in its gap (`heal failed (n): ...`), the run ends
  `partial`, the other days are still built; the day is tried again after 6 hours and given up
  (`unfillable`, reason and hub version in the note) after 5 attempts.
* **No manual retry.** A day given up because of failures is a candidate again after 7 days and at
  once after an upgrade of the hub (another version than the one that gave up): one attempt each
  time, so a persistent error costs one failed build a week, and a fix is picked up by itself. A
  successful build closes the gap. A reissue that leaves the cut on a static without
  `static_basis` is a different case (same input, same result): it is given up at once and never
  retried.
* **Gap rows** are written only when something changed (a new gap, a reopened one, another reason).
  A gap that is no longer detected is closed with the real reason: `backfilled` when the cut exists
  (or the reissue is done), `not_expected` when the day left the checked range
  (`MDHUB_SNAPSHOT_HEAL_FROM` moved) or the calendar no longer counts it as a business day.

## Progress and what is left (existing status API)

* `GET /v1/gaps?dataset=snapshot_cut_missing` and `...=snapshot_cut_reissue`: the open items with
  the reason in `note`. Zero rows = nothing to repair. A repaired day is `backfilled`, a day that is
  no longer expected `not_expected` and a day that could not be repaired `unfillable` in
  `ops.data_gap`, all with a note, so no day is left without an explanation.
* `GET /v1/runs?job=snapshot_heal`: each run with its counts (`rows_inserted` = cuts published,
  `rows_unchanged` = rebuilds that changed nothing), `error` = reason for `skipped` / `partial`.
* `GET /v1/coverage`, dataset `snapshot_eod_final`: freshness of the published cuts.

## Settings (`deploy/env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `MDHUB_SNAPSHOT_HEAL` | `repair` | `repair`: detect and build; `detect`: only record the gaps; `off`: the job does nothing |
| `MDHUB_SNAPSHOT_HEAL_FROM` | `2025-10-29` | first business date looked at (the start of the 12 months the desk recomputes) |
| `MDHUB_SNAPSHOT_HEAL_WINDOW` | `21:00-06:00` | MSK hours in which cuts are built (`HH:MM-HH:MM`, may wrap midnight) |
| `MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN` | `10` | cuts per run |
| `MDHUB_SNAPSHOT_HEAL_PAUSE_S` | `20` | pause between two cuts, seconds |

**Why `repair` is the default.** The owner decision is that a normal deploy repairs the history with
no manual step, and a setting only the operator can change would be that step. The risk is bounded:
a missing cut is a new snapshot (nothing published changes); a reissue is a correction under the
contract's rules (immutable ids, the old snapshot stays readable, `supersedes`), limited to the range
of #106, at most 10 per night run. The switch to stop it is `MDHUB_SNAPSHOT_HEAL=off` (or `detect`)
in the server configuration, effective at the next worker start.

## What happens after the upgrade

For the release notes. The job is on by default (`MDHUB_SNAPSHOT_HEAL=repair`), so the first
deploy that contains it repairs the history without anyone running anything:

* From the next :00 / :30 the job records the defects as gaps (`snapshot_cut_missing`,
  `snapshot_cut_reissue` in `/v1/gaps`). Nothing is published in the daytime.
* In the night window (21:00-06:00 MSK), within one or two nights, the hub publishes about **45 new
  `eod_final` cuts** (business days that had none) and about **204 corrections** of existing cuts
  of business dates **2026-01-05 .. 2026-10-21**, which are rebuilt on the static as of their date.
  The old snapshots stay readable; a correction `supersedes` its cut and keeps its `cut_ts`.
* **Consumers download these snapshots again.** Any client that follows `eod_final` sees about 249
  new snapshot ids over those nights: new days and replacements of existing days. Expect that
  download (and whatever the client recomputes from the history).
* Afterwards the runs end `skipped` ("nothing to heal") and publish nothing.
* **Switch.** `MDHUB_SNAPSHOT_HEAL=off` makes the job do nothing; `detect` only records the gaps and
  builds nothing (watch the scale first, then set `repair`). The server configuration is read at
  the worker start, so the change applies at the next restart.

## Not covered here

* Days without a curve in any source are not filled (see the open question in #138: a carried-forward
  curve needs a contract decision). They stay visible as gaps of `curves_ruonia_ois` /
  `curves_key_rate_swap` and as stale reasons of the cuts.
* Loading the history of datasets added later (v0.3.2: €STR, SOFR, FX crosses, proxy curves, ...).
* Corrections of cuts after such a history load: that is the existing `MDHUB_SNAPSHOT_AUTOCORRECT`
  mechanism, unchanged.
