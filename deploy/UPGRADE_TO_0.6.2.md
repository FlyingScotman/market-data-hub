# 0.6.2 — verified historical rates-futures tick terms

0.6.1 repairs expired contract dates from ISS descriptions. Those descriptions do not
publish minimum price increments or their RUB value. The manual `futures_contract_rules`
job fills these terms from the MOEX specification edition in force at contract expiry.
There is no new schedule. Run this job from the admin Jobs page after the dated contracts
have been loaded by the ordinary futures EOD job.

The scope is unlisted, expired, dated RGBI/RUONIA contracts with incomplete tick terms.
The job retains their dates and listing state. It never copies terms from a live successor.
For the verified editions, RGBI has a one-point increment worth RUB 1; RUONIA has an
increment of 0.0001 quoted points worth RUB 1. These are quoted-contract units.

The edition periods and source files are listed on the
[MOEX specification archive](https://www.moex.com/ru/documents/fyuchersnye-kontrakty-na-indeksy-dolgovogo-i-denezhnogo-rynka).
The curated terms and exact document SHA-256 hashes are in `loaders/futures_rules.py`:

| Effective period | Contents |
| --- | --- |
| 2022-02-28…2025-04-27 | RGBI minimum-increment and RUB-value clauses |
| 2025-04-28…2026-03-22 | RGBI minimum-increment and RUB-value clauses |
| 2026-03-23…2026-05-18 | RGBI minimum-increment and RUB-value clauses |
| 2026-05-19…2026-09-13 | Separate RGBI and RUONIA parameter tables |

Each required original DOCX is downloaded, checked against its pinned hash and stored as a
raw capture under provider `moex_rules`. A changed document or a conflicting existing term
fails the run and rolls back all its writes. An unknown edition remains missing and is
reported as partial. Repeating a completed repair makes no HTTP requests or row changes.

Migration `0830` registers the provider/job relation and permits `spec_source=moex_rules`.
That value means retained ISS contract dates plus verified document tick terms;
`capture_id` points to the actual specification document. The existing read endpoint and
its field types are unchanged. Consumers should retain this provenance string rather than
assume that every historical contract term came from an ISS description.

The repair does not establish market-data usage rights or change collection cadence.
Trading-session metadata and weekend valuation are tracked separately in issues #115
and FlyingScotman/rgbi-futures-monitor#4; settlement calendar flags are not changed here.
