# Round14: record-bound quantities and actual primary-chain acceptance

Date: 2026-09-27. Baseline: `519d76c8a9f41d4a0cb0b905e37392c1a35886ce`.
Backup: `.codex-backups/engineering-round14-primary-chain/baseline.tar`.
SHA-256: `2612598abf507d57594b017aa79c4f785f981374bb5744955166909ceff9a2c0`.

## Narrow product fix

Actual model output contained a dated, comma-separated receipt statement with
both received and missing counts. The numeric guard removed the supported
statement, so a completed answer omitted both requested quantities.
`grounded_clauses` now recognizes explicit record-context comma quantity atoms
and the role alias `实收到货`. Identity and date constraints must hold together
in one eligible source record. Values, units, conflicting observations, preview
exclusions and unknown-grammar behavior remain guarded. Context-free short
quantity labels retain the previous local-field gate. No model calls are added.
This is a bounded grammar, not a general semantic proof engine.

## Regression and causal replay

New regression file: 28 tests. Expanded selected suite: 325 passed and
365 subtests passed. Against baseline with the new tests: 8 failed, 317 passed
and 365 subtests passed; failures are confined to the new tests (missed valid
counts and invalid cross-record/conflicting count acceptance). Not a full-repo run.
The first unshipped version regressed the old short-label gate; its logs remain.
The final implementation restricts the new proof to identified or dated records.

`raw-replay-baseline.json` and `raw-replay-patched-v2.json` use the same saved
raw model answer and evidence, zero model calls, unchanged citations. Final
patch preserves the supported 8 received / 2 missing statement. Other complex
payment/count formulations still get rejected; universal completeness is not
claimed. Three fresh real-model QA jobs also completed. These are supplementary
acceptance, not controlled before/after comparisons: two deep summaries existed
in the source package by then. Request versus agreement remained distinguished,
and absent penalty figures were not invented. One answer still carries a partial
verification warning; successful jobs do not imply perfect semantic quality.

## Actual isolated HTTP / worker chain

Six synthetic known-answer physical files were processed via localhost HTTP,
real ordinary and large workers, and the configured local model services:

1. Large intake -> published smart catalog and matching-revision overview.
2. Three QA jobs -> selected deep summaries for two files.
3. Smart-topic node selection -> ordinary handoff of five files; repeated request
   key reuses the same scan and parsing job. Suspicious mail excluded from selection.
4. Ordinary parse -> preliminary file/node summaries -> overview -> automatic
   deep summaries -> `deep_update_available` -> explicit deep-results refresh.
5. Completed ordinary task; formal directory coverage 5/5, pending 0, failed 0,
   formal evidence count 7. Both isolated databases pass `quick_check`.

Package: `lp-73855fed3d0b40bf9cb994648f12e715`;
scan: `scan-677d548f0810`. A harness-only inventory field-name error was corrected
and the idempotent handoff resumed; the failure evidence is preserved. All
baseline and patched isolated service processes drained and stopped safely.

## Remaining quality and deployment boundaries

- Formal topics still mix an unrelated maintenance document into a purchase
  topic; some groups overlap and have no structured conclusions.
- Final preliminary-directory surface reports empty topics; file-summary-map
  fallback strings include zero-size folder labels. Inspect primary structured
  summary artifacts/UI before classifying or changing these surfaces.
- Six tiny synthetic files do NOT prove tens-of-GB capacity, browser UX, recovery
  at scale or archive-member handoff. `reports.zip` in this corpus is a directory.
- Preserve ordinary preliminary summary format: conclusion + supporting evidence.
- Production service restart, runtime activation and database migration are NOT
  performed here. Disk source and live long-running processes are distinct.
- No push. Installation preserves the initial uncommitted-work baseline commit
  `af534ad1a1493e741f3c09acb04f7a07624d0527` in ancestry.
- Revert only these scoped files after checking subsequent work; no whole-tree
  reset and no production database rollback.

Evidence resides under the round backup's `evidence/`, including original failed
tests, fixed-input replay, complete HTTP responses, worker logs and shutdown
receipts. The overall engineering goal remains active.
