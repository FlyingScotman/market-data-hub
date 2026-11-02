# MOEX stock trading calendar: proposed data contract

Status: proposal for review; this document does not describe an implemented API.

Consumers that count trading days need an authoritative session calendar. The
existing `MOEX_STOCK` calendar represents business/settlement days. Historical
days are inferred from observed market data, which can hide a data gap, and an
additional weekend session can belong to a later trading date. Changing the
existing business-day flags would change settlement semantics for current users.

## Source and ingestion

Use the [MOEX machine-readable stock calendar](https://fs.moex.com/files/26408)
through ISS, subject to verifying the hub's entitlement, permitted redistribution,
available historical range, request filters and pagination. The service is
[available through Algopack PROMO or separately](https://www.moex.com/a2836).
Do not assume that a successful HTTP response establishes entitlement or that
it covers every requested day.

Load the complete requested period, and periodically re-fetch it to capture MOEX
corrections. Retain source, capture and revision metadata and the time the hub
learned each value. Preserve physical session dates and the corresponding
exchange trading dates separately; retain market and other session attributes
only when supported by the source. A missing row, null flag, incomplete page,
failed request, or unavailable period is **unknown**, not a closed market.
Historical records and revisions must remain auditable. Do not overwrite a
previously complete period with an incomplete response.

The existing `/v1/calendar`, business-day priority and settlement calculations
remain unchanged. The existing `ref.weekend_session` and its provenance can be
reused where appropriate, but neither its current contents nor observed index
history alone establish complete physical-session coverage.

## Proposed consumer contract

Add a separate versioned endpoint, provisionally `GET /v1/trading-calendar`,
with market and date-range parameters. Its exact schema is subject to review.
It should return:

- Each calendar date, a trading-session state (`open`, `closed`, or `unknown`),
  and any physical sessions with their assigned exchange trading dates.
- Source, capture/revision identifier and time known to the hub, plus the
  timestamp of the last successful complete refresh.
- Per-month coverage and freshness, including an explicit incomplete reason;
  completeness requires every date and every response page to be accounted for.
  A failed or partial refresh must not silently certify cached data as current.
- The market and the calendar's supported scope. Do not imply that a market-level
  calendar proves that a particular instrument or trading mode was available.

Date-range and month-boundary handling must retain sessions whose physical date
and assigned trading date fall in different months. Before consumers count days,
verify how the hub's bond trade-history dates relate to physical sessions and to
assigned trading dates. The applicable counting convention must be agreed with
the consumer; the hub must expose both rather than silently deciding for it.

## Validation and operational behavior

Test a complete month, ordinary holidays, leap years, year boundaries, additional
weekend sessions and several sessions assigned to the same trading date. Test
pagination, a missing or null day, authentication errors, a partial response,
late exchange revisions and failed refreshes following a successful refresh.
Check that existing settlement-calendar and API behavior remains unchanged.

On a licensed test connection, verify the actual response schema and historical
coverage before declaring a period complete. Expose load failures and coverage
to operators without logging credentials. The proposal does not establish
coverage of bond trades across non-REPO trading modes; that requires a separate
source-coverage contract and validation.

Refs: [physical sessions issue](https://github.com/FlyingScotman/market-data-hub/issues/115),
[calendar gap issue](https://github.com/FlyingScotman/market-data-hub/issues/55).
