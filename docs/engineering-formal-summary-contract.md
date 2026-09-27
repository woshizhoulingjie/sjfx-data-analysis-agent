# Formal node summary presentation contract (2026-09-27)

## Observed failure

A saved real-model acceptance corpus produced normalized question/answer/evidence
node summaries with `claims` and ID-bound source evidence. The formal-directory
route read only `conclusions` / `conclusion_evidence`, so those claims disappeared.
It also labelled topics supported merely because some evidence records existed.
The finance node actually had insufficient evidence and must not be repaired by
inventing conclusions. Completion of deep processing is not semantic verification.

## Change

`services/formal_summary.py` supplies a read-only deterministic projection, used
by all four formal-directory topic sources. It preserves the existing
conclusion—supporting-evidence card shape and original quotes, source paths and
locations. It introduces no model requests, embedding passes, database migration,
stored-summary rewrite, template or CSS changes.

- Only explicitly referenced evidence IDs bind to a claim; no nearest-evidence
  fallback. Conflicting duplicate IDs are rejected.
- Preview, candidate, rejected, missing-source, missing-quote, out-of-scope and
  node-coverage records cannot become claim-specific supports.
- Partial claims or partial inherited supports remain partial. Losing any
  referenced support also prevents a fully verified card.
- Explicitly emptied top-level claims override older nested claims, including
  after numeric grounding rejected a model answer.
- Insufficient/candidate aggregate contracts cannot be revived by presentation.
- Unsupported claims remain separate review items; they are not formal conclusions.
- Legacy payloads without a QAE contract are not rewritten.
- Publication gates, membership and processing coverage stay unchanged. Topic
  evidence and verification status now describe claim support rather than merely
  the existence of evidence. Aggregate evidence counts follow projected topics.

## Acceptance

Round backup: `.codex-backups/engineering-round16-summary-contract/`. Baseline
commit: `fd89467f98ad5661d53612b4e6faf44634d4a935`. The archive, test results, HTTP
responses, source hashes and installation receipt are retained under that backup.

New deterministic tests cover exact binding/locations, partial status, preview
boundaries, malformed optional fields, conflicting IDs, explicit empty claims,
legacy compatibility, all four route branches and unpublished deep-result gating.
Existing selected summary, import, catalog and evidence regressions run against
both baseline and patch with independent state. See the retained receipts for
exact counts, rather than treating these selected suites as a full-project run.

Real HTTP replay uses separate local-disk SQLite copies of the round14 synthetic
acceptance database, not production. Baseline and patch read the same completed
model artifacts. Two topics change from zero visible conclusions to four and
three claim cards, all explicitly partially supported. Every displayed
statement/support pair is compared against its saved original. The finance node
remains without conclusions and is explicitly insufficient. Both databases pass
quick_check and temporary services are stopped. Model endpoints are disabled.

The first harness attempt used the NFS backup directory for state; the existing
SQLite network-filesystem guard correctly refused it. The harness was corrected
to local disk without weakening that product guard. Initial patch matching also
found a fourth route branch; all four branches are covered in final tests.

## Boundaries and rollback

Seven displayed cards are not seven independent discoveries: the saved directory
contains overlapping historical topics. This round does not change those topics,
regenerate initial summaries, solve the finance evidence gap, benchmark tens of
gigabytes, or establish browser visual acceptance. Legacy non-QAE payload
verification semantics are unchanged. The historical short-budget test mismatch
recorded in round15 is not claimed repaired by this change.

Installation does not restart existing services or establish that long-lived
processes loaded new code. Runtime activation requires a controlled rollout. No
production migration or Git push is performed. For rollback, retain subsequent
work and revert this round's commit rather than resetting the worktree; an exact
pre-install copy of the previous app file is also retained in `predeploy/`.
