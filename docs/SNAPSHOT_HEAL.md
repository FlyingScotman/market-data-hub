# Self-healing of the published `eod_final` history

Job `snapshot_heal` (market-data-hub#138; owner decision of 2026-11-10: history repair must not
need manual runs on the server). Code: `src/mdhub/loaders/snapshot_heal.py`.

## What it repairs

| Defect | Detected as | Repair | Gap dataset (`/v1/gaps`) |
|---|---|---|---|
| **Missing cut**: a business day of `MOEX_STOCK` from `MDHUB_SNAPSHOT_HEAL_FROM` (default 2025-10-29) to the day before the previous business day has no `eod_final` market snapshot, although `md.bond_eod` / `md.bond_eod_yield` hold its bond quotes | no row of `ops.published_snapshot` with `kind = market`, `cut_type = eod_final`, that `business_date` | the cut is published as known now, bound to a static built for that date (`parameters.static_basis`, #102 / #104) - what `mdhub run snapshot_market -p mode=eod_final -p day=D` does | `snapshot_cut_missing` |
| **Cut on the static of its build day**: a current `eod_final` of a business date from 2026-01-05 to 2026-10-21 whose linked static has no `parameters.static_basis` (cuts published before contract 1.4; #101, #106) | the cut's static manifest has no `static_basis` | the cut is rebuilt with its own `cut_ts`: a correction that `supersedes` the old snapshot (which stays readable); what `snapshot_rebuild` does | `snapshot_cut_reissue` (one gap per cut: `key.cut_ts`) |

| **Outdated carried cut** (contract 1.7, #143): a current `eod_final` of a business date from `MDHUB_SNAPSHOT_HEAL_FROM` whose manifest lists `carried_curves` (a curve no source stored on its day, taken from an earlier day, flagged) while the store now holds a non-empty quote set of that curve for a day after the carried `source_day` up to the cut's business date (the real data, or a newer carry source, arrived late) | the cut is rebuilt with its own `cut_ts`: a correction that `supersedes` the old snapshot; the new cut holds the real curve (no marker) or the newer carry | `snapshot_cut_carried` (one gap per cut: `key.cut_ts`) |

The reissue range of the static-basis defect is fixed in the code (`REISSUE_FROM`, `REISSUE_TO`): the one described in #106.
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

### New datasets: history load and a one-off reissue (#138)

Owner decision of 2026-11-11: when a dataset is added to the hub, its history is backfilled **and**
the `eod_final` cuts of the last 12 months are reissued **once**, so that past cuts contain the new
series, with no operator action. Code: `src/mdhub/quality/history_load.py` (the dataset side) and
this job (the cut side); state: `ops.dataset_history_load` (one row per dataset, migration 0840).

1. **Detection.** In repair mode, inside the night window, every run looks at the registered
   datasets that have a backfill job and a `history_start` (daily, business-daily or monthly; not
   event, intraday or rolling-window ones). A dataset **without a row** in
   `ops.dataset_history_load` whose earliest stored day is later than its `history_start`, with at
   least one expected day of its calendar in between (an empty table: up to the expected latest
   day), is new. The scheduled gap scan cannot see this head of the history (it scans from the
   earliest stored day; an empty table is only `no rows`), which is why such a history used to wait
   for a manual run. A dataset that already has its history, or has a row, is never touched again.
2. **Load.** The head `[history_start, earliest stored day - 1]` is queued in `ops.backfill_task`
   with source `history_load`, in chunks of the dataset's `max_task_days`, **one set of tasks per
   backfill job** (datasets that share a job share its tasks). The ordinary `backfill_worker`
   runs them (every 10 minutes, with its time budget; it yields to scheduled runs as always) and
   takes them **after the tasks of every other source**, so repairs of live gaps are not delayed.
   "As far as each source allows": a day the source has nothing for stays empty. A range whose task
   failed (after the worker's own 5 attempts) is queued again once, no sooner than 6 hours later;
   what is still missing then stays missing, noted in the row and in `/v1/gaps`.
   The worker's autocorrection (a rebuild of the cuts of the days a backfill touched) is **not**
   applied to these tasks: it would reissue the same cuts once per task and dataset.
3. **One reissue pass.** When no dataset is still loading, the datasets whose load added rows (the
   earliest stored day moved back) and whose job feeds the cut (`SNAPSHOT_INPUT_JOBS`: fixings,
   FX, curves, CPI, ...) start **one** pass together: every current `eod_final` cut of the last
   365 days (up to the previous business day) gets a gap `snapshot_cut_new_series` and is rebuilt
   as known now (a correction that `supersedes` the old snapshot, which stays readable; a cut that
   already holds the data publishes nothing). The pass runs in the night window, at most
   `MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN` cuts per run with the pause between them, like every other
   repair, and one rebuild closes all gaps of a cut. A dataset detected while a pass runs waits
   for the next pass. A cut that fails is retried and given up like any other (see "Failures").
4. **Done.** When no cut of the pass is `open`, its datasets are `done`: the row stays, nothing is
   detected, queued or reissued for them again. A dataset whose load added nothing, or whose job
   does not feed the cut, is `done` without a pass.

Status: `GET /v1/gaps?dataset=snapshot_history_load` (one gap per dataset, `open` while it loads
or waits for the pass, with the progress in `note`; `backfilled` when done) and
`...=snapshot_cut_new_series` (the cuts of the pass still to reissue); `GET /v1/runs?job=snapshot_heal`
(the run result lists `history`: new datasets, loading / reissue counts, the pass started and
finished). `MDHUB_SNAPSHOT_HEAL_HISTORY=false` switches this part off; `mode=detect` and the
daytime do not start anything. Limits of this design: a dataset is judged as a whole (a new
*series* inside an existing dataset, e.g. one more index code in a table, is not detected), and the
cuts are reissued from the data stored at the time of the pass.

### Carried curves: when a cut is outdated, and why the job does not loop

A cut that carries a curve is **not** a defect by itself: the day really has no data, and the cut
says so (`carried_curves`, #143). It is outdated only when the store holds newer data of that curve
than the one it carries: a quote set of the same curve dated after the carried `source_day` and not
after the cut's business date. Then (and only then) the next night run rebuilds it, as of now, on
the static as of its date (`historical`): the correction replaces the carried quote set by the real
one, or by a newer carry source. After that the cut carries nothing outdated, so the detection is
silent: a run with nothing else to do ends `skipped` ("nothing to heal"), a second run publishes
nothing. A cut that legitimately stays carried (no newer data) is never touched, however many runs
pass. If a rebuild nevertheless left the cut carrying a curve with newer data (it cannot happen
with the same inputs; the guard is for a defect), the gap is closed `unfillable` with that reason
and not retried: no loop. A cut that is both on the static of its build day and carries an outdated
curve is rebuilt once and closes both gaps.

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

* `GET /v1/gaps?dataset=snapshot_cut_missing`, `...=snapshot_cut_reissue` and
  `...=snapshot_cut_carried`: the open items with the reason in `note`. Zero rows = nothing to repair. A repaired day is `backfilled`, a day that is
  no longer expected `not_expected` and a day that could not be repaired `unfillable` in
  `ops.data_gap`, all with a note, so no day is left without an explanation.
* `GET /v1/runs?job=snapshot_heal`: each run with its counts (`rows_inserted` = cuts published,
  `rows_unchanged` = rebuilds that changed nothing), `error` = reason for `skipped` / `partial`.
* `GET /v1/coverage`, dataset `snapshot_eod_final`: freshness of the published cuts.
* `GET /v1/carried-curves`: the curves carried in the *current* market snapshots (the cuts that
  still wait for the real data of their day). A cut reissued after the data arrived is superseded
  and drops out. The `quality` report lists them too (`summary.carried_curves`).

## Settings (`deploy/env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `MDHUB_SNAPSHOT_HEAL` | `repair` | `repair`: detect and build; `detect`: only record the gaps; `off`: the job does nothing |
| `MDHUB_SNAPSHOT_HEAL_FROM` | `2025-10-29` | first business date looked at (the start of the 12 months the desk recomputes) |
| `MDHUB_SNAPSHOT_HEAL_WINDOW` | `21:00-06:00` | MSK hours in which cuts are built (`HH:MM-HH:MM`, may wrap midnight) |
| `MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN` | `10` | cuts per run |
| `MDHUB_SNAPSHOT_HEAL_PAUSE_S` | `20` | pause between two cuts, seconds |
| `MDHUB_SNAPSHOT_HEAL_HISTORY` | `true` | the history load of new datasets and the one-off reissue after it (repair mode, night window) |

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
* **History of new datasets (the same job).** In the same nights the job finds the datasets that
  are new to the hub (their earliest stored day is later than their `history_start`: the ones added
  in 0.3.2 and any other registered dataset with such a gap), queues their history in the backfill
  queue (a one-off load; the backfill worker needs hours to days, depending on the sources) and,
  when the loads are finished, reissues the `eod_final` cuts of the last 12 months **once more**
  (a second wave of up to about 250 correction ids; a cut that is still waiting for its reissue
  above is rebuilt once for both reasons). Plan for that download as well. The list of what is
  loading is `/v1/gaps?dataset=snapshot_history_load`. `MDHUB_SNAPSHOT_HEAL_HISTORY=false` leaves
  this part out.
* Afterwards the runs end `skipped` ("nothing to heal") and publish nothing.
* **Switch.** `MDHUB_SNAPSHOT_HEAL=off` makes the job do nothing; `detect` only records the gaps and
  builds nothing (watch the scale first, then set `repair`). The server configuration is read at
  the worker start, so the change applies at the next restart.

## Not covered here

* A day without a curve in any source is not a gap of this job: the cut is built with the curve
  carried forward from the latest earlier day, flagged (contract 1.7, #143), and stays visible as a
  gap of `curves_ruonia_ois` / `curves_key_rate_swap` and as stale reasons of the cut. The carry is
  configured by `MDHUB_CURVE_CARRY_DATASETS` / `MDHUB_CURVE_CARRY_MAX_AGE_DAYS`.
* A new series inside a dataset that already has its history (one more index code in a table): the
  load is judged per dataset (see "New datasets"). The corrections of cuts after an ordinary
  backfill of live gaps are the existing `MDHUB_SNAPSHOT_AUTOCORRECT` mechanism, unchanged.
