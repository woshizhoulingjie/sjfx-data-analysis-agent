# Bounded preview context retention

## Defect and scope

Actual HTTP/API and worker acceptance found that a completed, citation-verified
answer omitted a received quantity explicitly present in a short source file.
The stored preview had dropped the opening quantity sentence. A separate short
maintenance note and email could also lose the subject or requested date. This
is an evidence-retention defect, not proof that citation verification checks
answer completeness.

`record()` previously selected at most four sentences even when the entire
normalized source window fit its 1,800-character budget. `compact_text()` then
selected at most eight sentences even when all retained content fit its budget.
Repeated ranking unnecessarily discarded source context.

## Change

- Keep a normalized source window intact when it fits 1,800 characters. Preserve
  its location, source metadata, stable content ID and parser truncation flag.
- Assemble compact text in source order when all eligible retained blocks fit
  the requested character budget. Inventory-only attachment/member names remain
  excluded. At most 64 retained blocks qualify for this path.
- Keep the original greedy selector for over-budget content. No additional
  model call, original-file scan, vector load or source hashing is introduced.
- Keep byte/time/reservoir bounds and sensitive-file handling. All previews
  remain explicitly partial: complete retention of a sampled window is not
  complete parsing of the source file.
- Bump the reader marker to `naming-evidence/3`; preserve schema and ordinary
  preliminary-summary output format (结论—支撑证据).

Existing stored previews are not rewritten automatically. New previews use the
new reader. Existing packages require an explicit supported re-preview/refine
operation before their evidence benefits from this change. Do not directly
overwrite stored summaries or replay the entire production queue.

## Verification

Round directory: `.codex-backups/engineering-round5-real-api-flow/`. Baseline
commit: `b126ad4660910f918bd48593905d3942220a0e9e`.

- Seven new deterministic tests: four fail on the baseline, all pass with the
  fix. They use facts different from the model acceptance sample.
- Related suite: 287 tests and 355 subtests pass (87.28 seconds). Includes
  unchanged greedy-selection equivalence, evidence quality, bounded previews,
  sensitive files, sparse multi-GB preview, QA, catalog publication and handoff.
- Actual-model rerun uses a new isolated local-ext4 state directory and
  `evidence/context-v2/`, never overwriting the original incomplete answer.
- The first acceptance state was on NFS and correctly rejected for ordinary
  SQLite WAL operations. The guard remains enabled; test state moved to ext4.
  Test harness errors and the original results remain preserved.

Completion of a turn/job is not semantic acceptance. Inspect actual answers,
citations, parsed documents, file/node summaries and reports. Six synthetic
documents are not a tens-of-GB capacity or production deployment acceptance.

### Actual model outcome

New package: `lp-8e70e8fb71eb4e4fa5379de13e82bab2`. Published catalog and report
were current at 59.8 seconds; three real QA jobs finished in 65.5 seconds after
queueing. The isolated ordinary workers stopped when idle.

The focused preview acceptance PASSES: stored source evidence preserves
received/missing quantities, contract payment prerequisites, P-99 maintenance
identity and safety conditions, and the supplier request/confirmation boundary.
See `evidence/context-v2/preview-review.json` and its source hash.

The final QA acceptance still FAILS. Quantity facts now appear in the actual
citations and a real-model draft, but the numeric relation gate rejects some
valid multi-clause statements and the final answer omits requested quantities.
The raw draft and unchanged verifier output are preserved in
`numeric-audit-v2-verification-1.json`; `qa-review.json` explicitly says false.
No handoff/deep-summary acceptance is claimed or run past that failed gate.

Next work must address numeric matching with positive and adversarial negative
tests (units, identifiers, local subjects/dates, missing answer aspects). Do not
disable the verifier, treat generation as truth, hardcode the sample facts or
call a reduced answer fully verified/complete. The first diagnostic observer
only covered the parent worker; the corrected passive observer also records
spawned job subprocesses. Neither replaces the model or changes verification.

## Deployment and rollback

Changes are prepared and tested in the isolated staging copy. Before copying
approved files into the main checkout, save pre-change copies and verify the
expected clean baseline. Commit only the scoped source, tests and this document.
No Git push, production restart, production DB migration or bulk refresh is
included. Already-running processes may retain old modules; runtime activation
requires a separate controlled rollout. Roll back only these files, never
restore an old whole database over newly created business data.
