# Bond history query benchmark

Issue: https://github.com/FlyingScotman/market-data-hub/issues/9

`benchmark_history.py` runs against the standard disposable PostgreSQL fixture, using
synthetic data only. Its name excludes it from ordinary test collection. It calls the real
HTTP route, records version/page statement durations separately, and prints complete
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` plans. Each explain executes the statement again:
its execution time is separate from the preceding route measurement.

Dataset: 4,000 bonds, 500 business dates from November 2024, 2,000,000 price rows and
1,200,000 yield rows (60% of bonds). All have a primary board. The January–October 2026
query selects 784,000 prices and 470,400 yields. Narrow selection: one bond in September
2026 (23 rows from each table). The regular correctness tests separately cover missing
prices/yields, overlapping keys, multiple boards, mixed-case SECIDs and `known_at`.

## Reproduce

On the feature branch, from the repository root:

```sh
uv --cache-dir /tmp/mdhub-history-uv run pytest -q -s --tb=short tests/benchmark_history.py
```

`uv` manages the project environment; `--cache-dir /tmp/mdhub-history-uv` places its cache
in a temporary directory; `run` runs the following program in the project environment;
`pytest` is the test runner; `-q` reduces progress output; `-s` displays benchmark output;
`--tb=short` shortens failure tracebacks; the final path selects only this benchmark.
The local PostgreSQL fixture must be allowed to start and write its normal lock directory.

For the baseline, create an isolated checkout of v0.3.2 and copy only the benchmark into it:

```sh
git worktree add /tmp/mdhub-history-baseline d71d984
cp tests/benchmark_history.py /tmp/mdhub-history-baseline/tests/benchmark_history.py
cd /tmp/mdhub-history-baseline
uv --cache-dir /tmp/mdhub-history-uv run pytest -q -s --tb=short tests/benchmark_history.py
```

`git worktree add` creates an additional checkout at the first path, using commit `d71d984`
as its starting point (detached HEAD); it does not change the feature checkout. `cp` copies
the first file to the second path, supplying the same workload to the older implementation.
`cd` changes the shell's working directory. The final command has the same meaning as
above. Use an unused baseline directory; these commands do not delete existing work.

## Measurements (2026-10-22)

Local pgserver PostgreSQL 16 on macOS/Apple Silicon, default database settings, no server
data copied. One priming broad request precedes the measurements, and table statistics are
analyzed after seeding. These are individual reproducible development measurements, not
production latency guarantees or an assertion that all data fit in shared buffers.

| HTTP request, limit 2 | v0.3.2 | Optimized |
| --- | ---: | ---: |
| Broad first page | 2,139 ms | 92 ms |
| Broad next page | 2,099 ms | 95 ms |
| One bond / September | 4.93 ms | 2.91 ms |

| SQL within broad first HTTP request | v0.3.2 | Optimized |
| --- | ---: | ---: |
| Whole-selection version | 279 ms | 90 ms |
| Page | 1,839 ms | 0.85 ms |

The baseline page plan materializes 784,000 price and 470,400 yield rows and spills to
temporary storage despite returning only three keys (two response rows plus the next-page
sentinel). Its root reports 49,931 temporary blocks read and 65,752 written. The optimized
page materializes at most three rows per source, using existing SECID/date indexes and
incremental sorting; it has no temporary reads/writes. No new index is needed.

The baseline version scans both materialized inputs twice, writing/reading 12,404 temporary
blocks. A single count/max aggregate per source and removal of the unused union CTE allow
inlining and parallel aggregation without temporary storage. Counts, timestamp values and
their order in the version tuple remain identical; the version still covers every page.

Correctness argument: if a key is among the first N distinct keys of a union, its rank in
either input containing it is at most N. Thus retaining the first N ordered rows of each
input preserves all source values for the first N union keys, including overlaps and rows
present in only one table. Both inputs use the same filters, cursor and ordering as before.

## Remaining limits

The exact whole-selection version remains O(selected rows); this change introduces no
cache, triggers or migrations. Large pages and as-known-at reconstruction still cost more
than tiny current-state pages. Existing 50,000-row pagination tests pass.

The history catalogue query is unchanged. On the same synthetic data, its cold HTTP read
took 1.28 seconds and a read from the existing per-process 300-second cache took 1.08 ms.
That remains a separate performance concern; no shared cache or freshness change is hidden
in this fix. Validate the optimized route on production after a separately approved deploy.
