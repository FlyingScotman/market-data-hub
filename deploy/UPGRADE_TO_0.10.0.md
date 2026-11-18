# 0.10.0 — rounded MOEX redemption schedules (contract 1.10)

This note covers the **0.9.0 → 0.10.0** changes (market-data-hub#165). For the signed-release
installation and updater operation, use [README_DEPLOY.md](README_DEPLOY.md) §11 and §13. Release
0.10.0 is the target of this note; do not treat it as installed until the update has completed.

## What changes

MOEX ISS publishes the principal schedule of a bond rounded (`value` in steps of 0.01). For a bond
with a low face this breaks the schedule: XS0114288789 (face 0.035 USD) has seven future
redemptions of 0.005 that MOEX shows as 0.01, and its `EFFECTIVEYIELD` (53.567 %) and durations
are computed from that schedule. The release:

- labels such schedules (`redemptions_rounded_by_source`, `future_redemptions_differ_from_face`);
- lets a **reviewed file** of confirmed amounts replace the rounded future rows
  (`src/mdhub/data/bond_amortization_overrides.json`, first entry: XS0114288789, confirmed by the
  owner on 2026-11-17), while it agrees with what MOEX publishes;
- marks the MOEX yields, durations and spreads derived from an inconsistent schedule as
  unreliable in the snapshots (`source_analytics_unreliable`) and in the TDM export manifest
  (`unreliable_values`); the values themselves are unchanged.

The contract is `market_snapshot/1.10`, additive; see the
[contract](../schemas/market_snapshot/v1/README.md#rounded-redemption-schedules-110).

## Database and rollback

From a 0.9.0 database with **55 applied migrations**, the release adds exactly:

- `0122_bond_amortization_override` — table `ref.bond_amortization_override` with row history;
- `0542_amortization_override_known_at` — its as-known-at reader;
- `0436_coupon_basis_schedule_tail` — the new `ref.bond.coupon_type_basis` value `schedule_tail_constant`;
- `0123_bond_floating_rate_flag` — columns `ref.bond.floating_rate`, `floating_rate_source`, `floating_rate_updated_on`, `floating_rate_base_present`: Cbonds's `floating_rate` as true / false / no statement, and whether it states a base rate or margin.

After the update, `/health/ready` should report version `0.10.0`, `status: ok` and migrations
`applied: 59`, `known: 59`, `pending: 0`, `checksum_mismatch: 0`. If the starting database has
additional or missing migrations, investigate the difference before applying this expectation.

The updater takes a pre-update database dump. **Once the new migrations have run, rolling back
to 0.9.0 restores that dump:** database changes made after the backup are lost until reloaded,
including unrelated data and snapshots. This applies to an automatic rollback after a failed
check as well as a later rollback.

## Coupon type of bonds whose rate stepped up long ago

MOEX has no coupon-type field. An active bond without a coupon label was `fixed` only if one rate
ran over its **whole** schedule, so a bond that paid 2.25 → 2.5 → 5 → 7.5 % until 2007 and 7.5 %
since (XS0114288789) stayed `unknown` and its future coupons `projected_by_moex`, although MOEX
publishes the rate of every future period. The schedule alone cannot tell such a bond from a
floater whose rate MOEX carries forward (a capped floater, a stale fixing), so the rule needs the
source's statement as well. `cbonds_enrich` now keeps Cbonds's `floating_rate` of every active
bond it reads as true / false / no statement (`ref.bond.floating_rate`, with its source and Cbonds's
update date); before, only "true" was used. A bond becomes `fixed` with
`coupon_type_basis = schedule_tail_constant` only when **all** of these hold: it is active; Cbonds
states `floating_rate` = false and no base rate or margin for it (true, no statement, or a stated base rate leaves it `unknown`); it has no
COUPON_DETAILS, benchmark or rate formula; and the **tail** of its schedule (the paid coupons at the
rate of the last coupon, plus all future ones) has at least two paid coupons, a rate above 0.1 %,
every future coupon at that rate, and a CBR key-rate change inside it. Its future coupons then
become `known`.

Effect, from the static snapshot of 2026-11-17: 59 bonds matched the schedule-only tail test (28
active, 31 delisted). Delisted bonds are excluded; at most the 28 active ones can change, and only
those for which Cbonds states `floating_rate` = false, which the snapshot does not show. The flag
is filled by the first `cbonds_enrich` run after the update (daily 07:30); `bonds_static` (06:30)
applies it from the next day, so the reclassification appears one day after the update.

## Configuration and jobs

No new settings, environment variables, jobs or schedule rules. The existing job
`bond_terms_curated` (daily 06:55 Moscow time, after `bonds_static`) also loads the overrides file.
Its run is `partial` when an override no longer agrees with the MOEX schedule (the builders then
publish MOEX's rows and label the bond `redemptions_override_stale`) or when an entry has a
mistyped SECID. An entry for a SECID the database does not hold (a development seed, a fresh
install) is a warning in the log only.

## Check after installation

1. Check version `0.10.0` and `/health/ready` as above.
2. After the next `bond_terms_curated` run: in the admin console open **Jobs**, in the card
   **Run history** type `bond_terms_curated` into the field **Job** and press **Filter**. The
   latest run should have **Status** `succeeded` and an empty **Error**. If an override no longer
   agrees with MOEX, or an entry has a mistyped SECID, the **Status** is `partial` and **Error**
   names the bond and the reason. The console shows nothing else of this job: no result details,
   and not whether the override was applied.
3. Whether the override was applied is visible only in the published snapshot, which the console
   does not show. Read it through the API with a token of scope `snapshots` (or `read`):
   `GET /v1/snapshots` lists the snapshots, `GET /v1/snapshots/{id}/bundle` returns a static one
   (gzipped JSON). In it, `data.redemptions` of `XS0114288789` has seven future rows with
   `source: manual_override` and the evidence in `override`; the instrument has the label
   `redemptions_manual_override` (or `redemptions_override_stale` if it was not applied) and
   `source_analytics_unreliable_reasons` still lists the MOEX reasons. The coverage of
   `data.redemptions` lists `override_applied` and `override_stale`. Market snapshots carry
   `source_analytics_unreliable` on the quotes of the bond. A reused static snapshot may still
   carry an earlier contract version.
4. The consumer reads `source_analytics_unreliable` and must not use the listed MOEX fields for
   pricing or risk.

The past redemptions of such a bond stay as MOEX publishes them (rounded) until they are read
from the `FACEVALUE` history: FlyingScotman/market-data-hub#166.

Changing or removing an override is a reviewed change of the JSON file and a new release.
