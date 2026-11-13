# 0.9.0 — timestamp-aligned FX/futures inputs (contract 1.9)

This note covers the **0.8.0 → 0.9.0** changes. For the signed-release installation and
updater operation, use [README_DEPLOY.md](README_DEPLOY.md) §11 and §13. Release 0.9.0 is
the target of this note; do not treat it as installed until the update has completed.

## Database and rollback

From a 0.8.0 database with **53 applied migrations**, the release adds exactly:

- `0290_md_fx_input_pairs` — the NTPro public source and retained FX input groups, job
  completion observations and per-cut selections;
- `0671_fx_input_pair_source` — the job-to-source association.

After the update, `/health/ready` should report version `0.9.0`, `status: ok` and migrations
`applied: 55`, `known: 55`, `pending: 0`, `checksum_mismatch: 0`. If the starting database has
additional or missing migrations, investigate the difference before applying this expectation.

The updater takes a pre-update database dump. **Once the new migrations have run, rolling back
to 0.8.0 restores that dump:** database changes made after the backup are lost until reloaded,
including unrelated data and snapshots. This applies to an automatic rollback after a failed
check as well as a later rollback. Confirm that the backup exists and is usable; inspect the
rollback confirmation and request result. Do not describe this rollback as an image-only switch.

## Configuration and collection

The release adds **no new mandatory settings or environment variables**. Existing access to the
relevant feeds still determines whether observations are available. The public NTPro source
requires no new credentials.

The `fx_input_pairs` job is scheduled automatically every minute from **07:00 through 23:50
Moscow time** on `MOEX_FORTS` trading days, including configured weekend sessions. It polls NTPro
for indicative USD/EUR inputs and uses existing MOEX captures for CNY and futures. The NTPro
reachability probe runs at most hourly. Neither a successful job nor a successful probe proves
that prices are fresh or usable.

## Check after installation

1. In the admin console, **Updates → Running**, check version `0.9.0` and process heartbeats;
   under **Requests**, the install should finish `done`. Check `/health/ready` as above.
2. During the collection window, **Jobs → Schedule and health** should show `fx_input_pairs`;
   **Jobs → Run history**, field **Job**, allows filtering its runs. A `partial` run or missing
   inputs is a result to investigate, not proof of a working three-currency curve. **Sources**
   shows `ntpro_public` reachability after its probe; `not checked` is possible before that probe.
3. Inspect a naturally published market snapshot after installation using the existing snapshot
   API or consumer. It should identify contract `market_snapshot/1.9` and contain the
   `fx_input_pairs` dataset. Check the dataset's coverage and stale reasons, then each available
   currency's spot and four dated future nodes, their individual clocks, prices and reasons.
   An empty dataset or unavailable node must stay explicit; do not replace it with another source
   or maturity. A reused static snapshot may still carry an earlier contract version.

USD/EUR spot observations are public NTPro indicative bid/offer; CNY uses MOEX LAST, not a
two-sided spot quote. NTPro spot value date is unknown. Exchange `last_trade_date`,
`last_del_date` and underlying FX value date remain separate. The observed clock differences
do not prove physical simultaneity, executable liquidity or suitability for a rate calculation.
The hub publishes inputs only; the consumer decides whether and how to calculate a curve.
Existing snapshots remain immutable. See the [contract](../schemas/market_snapshot/v1/README.md#timestamp-aligned-fx-inputs-19)
for clock, provenance, selection and RUONIA limitations.
