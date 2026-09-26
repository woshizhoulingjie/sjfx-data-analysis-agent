# Large-package statistics: consistency and read cost

## Scope

This change preserves the existing JSON fields, queue status semantics,
summary content, ordinary import contracts, and worker scheduling behavior.
It changes only `LargePackageStore` read transactions and two additive indexes.

- `counts()` opens one explicitly closed connection and one deferred read
  transaction for file totals, grouped file states and pending queue counts.
- `workflow_snapshot()` reads core package control, queue states, quality,
  selection/run metadata and published versions in one short read snapshot.
- There is no `BEGIN IMMEDIATE` on these read paths, no write, no result cache,
  and no model call inside the read transaction. Connection closure ends the
  transaction on success, missing-package return, or exception.
- The existing best-effort `catalog_degraded` diagnostic remains a separate
  observation after that transaction. It is not covered by the core snapshot
  consistency guarantee. Read results are not an authorization fence; mutation
  paths must still recheck control state and leases.

## Why separate reads were incorrect

With autocommit connections, a worker could atomically finish a file and its
queue entry between consecutive SELECTs. One response could therefore combine
old package/control state with new queue and quality state. Two connections in
`counts()` also could not share the same read snapshot. The regression tests
commit from a second real SQLite connection during the first read and assert
that the response stays coherent, then that a new request sees the commit.

## Indexes

- `idx_lp_files_quality(package_id, summary_quality)` covers summary quality
  aggregation without visiting rows that hold potentially large summaries.
- `idx_lp_queue_pending_counts(package_id, stage)` is partial: only
  `queued`, `running`, and `retry` rows participate. Finished history need
  not be scanned for pending counts. Unknown future stage names are retained.

Existing claim and stale-lease indexes are preserved. The measured stale-lease
query already used an efficient skip scan; adding another index for it was
not justified. Empty summary quality still means `no_summary`, not unknown.

Indexes are created only when missing in the existing initialization
transaction after column migration. Each new index is analyzed in that same
transaction: otherwise an existing database with statistics for the old indexes
can select the new, unanalyzed index for unrelated grouped queries and regress
whole-interface performance. Existing indexes are not reanalyzed on each start.
The initial unshipped implementation reproduced this regression; its evidence
is retained rather than hidden. Index creation does not modify file/queue rows. Index
construction has a one-time write/lock and space cost; status and quality
updates incur extra index maintenance. This is a measured read/write tradeoff,
not a claim that every operation is faster.

## Verification and limitations

Focused tests: `python -m pytest -q tests/test_large_package_statistics.py`.
The engineering-round13 backup includes baseline/patch regressions, original
production-snapshot and isolated-copy measurements, core-row hashes, migration
and interrupted-index-transaction recovery evidence, plus write cost results.

These are database and component checks. They do not establish tens-of-GB
end-to-end ingestion, model quality, concurrent production SLA, or activation
of the new source in existing long-lived processes. Production services are
not restarted and the production database is not migrated by these checks.

Before activation, use a verified consistent backup, check free disk and
active tasks, and arrange controlled initialization. Do not build indexes
blindly while production workers are busy. Recheck runtime versions and
actual API behavior afterwards. On rollback, restore only this round's exact
code changes after checking for later edits; additive indexes may remain
compatible with the old code. Never replace the live database with an older
snapshot merely to undo indexes, because newer user data would be lost.
