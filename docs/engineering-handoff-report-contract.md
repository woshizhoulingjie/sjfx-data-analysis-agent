# Catalog handoff: ordinary report metadata contract

Local acceptance date: 2026-09-27 (Asia/Shanghai).
Baseline: `081c92ae8830101daeae41795266105612efe3e3`.
Backup and evidence: `.codex-backups/engineering-round8-handoff-metadata/`.

## Root cause and minimal repair

The catalog bridge created a valid selected-file scan and parsed documents, but
omitted `total_size_human`. The ordinary parsed overview DOCX writer requires
that field. A real worker failed with `KeyError: 'total_size_human'` before
preliminary summaries could be scheduled. Previous handoff tests replaced
`_write_local_overview`, inadvertently hiding this integration boundary.

The bridge now derives the field from its already-validated selected byte total
using the ordinary scanner's existing `human_size` function. Only two production
lines change. No new model call, parser, dependency, schema migration, fallback
zero or swallowed exception is introduced. Directory algorithms and summary
formats remain unchanged. This fixes newly created bridge scans; it does not
silently mutate old scans or requeue failed production tasks.

## Reproducible regression evidence

Six new tests cover empty/small/KB-boundary sizes, selected versus entire-package
scope and a real parsed overview with DOCX generation followed by summary queue
creation. The output location alone is redirected; the report builder/writer
are not replaced. All six fail with the missing field on the unchanged baseline
and all six pass on the patch.

Identical expanded suites were run in separate isolated source/state trees:

- Baseline: 201 passed, 8 failed, 7 subtests passed (95.52s).
- Patch: 207 passed, 2 failed, 7 subtests passed (93.80s).
- Exactly the six new failures are fixed; no new failure in the compared suite.
- The same two pre-existing `test_package_overview.py` English-single-name
  aggregation failures remain. Their source/test hashes are unchanged. They
  are recorded, not skipped, weakened, or represented as passing.

## Real isolated component acceptance

Actual APIs, parsers, local models and workers processed six synthetic files.
Catalog selection transferred three physical files (1599 bytes), including a
real directory named `reports.zip`. Large-package selected deep summary and
deep report were produced. Ordinary execution produced:

- Three preliminary file summaries, four preliminary node summaries, one
  preliminary overview.
- Three deep file summaries, four deep node summaries, one deep overview.
- The formal `refresh-deep-results` API returned 202 and the ordinary import
  reached `completed`. All its jobs completed and the entire isolated queue
  remained idle for 12.1 seconds before its identity-checked worker was stopped.

The user-visible preliminary conclusion/support structure remains
`file-claims/preview-1.0`; all 15 conclusion/fact entries have source-local
supports and resolving evidence IDs. These remain preview candidates, not
fully verified statements.

## Quality gates NOT passed

Fresh QA again omitted received/missing counts, despite the prior round's
single successful run. It was correctly marked partial. Therefore handoff
was explicitly tested as an independent component, **not** a passed full
end-to-end workflow. `qa-review.json` stays `passed: false`.

Semantic review additionally found: large deep summary turns unconfirmed repair
completion into an assertion of no repair; ordinary deep maintenance conclusions
use relative future wording for a June 2025 source; preliminary contract prose
strengthens no authorization into prohibition; preview polarity checking still
over-flags some source-backed claims. These require separate backed-up quality
work. `handoff-review.json` records operational success and semantic failure.

This is not tens-of-GB throughput, restart recovery, broad mail intention
acceptance, endpoint protection validation or proof of production activation.
No production service restart, database migration or remote push is performed.
