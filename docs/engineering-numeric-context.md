# Numeric record context and real-flow acceptance

Date: 2026-09-27. Baseline: `288cc4685e0e5d9c4a2b05017d62b5075a9c65e2`.

## Change boundary

The verifier recognizes properly grouped amounts without splitting `182,000`
into unrelated values. Signs, decimal scale and explicit units remain binding.
Count and measurement units are no longer omitted from numeric tokens.
Only numeric/unit whitespace is normalized: separate numbers, identifiers and
dates must not concatenate. Explicit same-key unit binding remains unchanged.

Short explicit record headers may provide project, identifier and labeled
record-date context to adjacent quantity-only fields. No amount or predicate is
copied from another clause. New projects, line/field boundaries, unrelated or
hypothetical subjects, planning headers and bounded distance expire context.
Quantity roles and planning modality must agree. This is a conservative lexical
matching aid, not general semantic entailment or entity resolution. Original
citation text is preserved. Existing generated summary formats are unchanged.

## Verification

Backup/evidence: `.codex-backups/engineering-round7-numeric-context/`.

- Expanded targeted regression: **431 passed, 365 subtests passed**, 101.09s.
- Adversarial cases include wrong project/date, received versus missing counts,
  wrong unit/sign, malformed amount grouping, unrelated subjects, planned or
  conditional versus actual receipt, and numeric identifier/date boundaries.
- Three real model QA turns over six synthetic source files passed a reviewed
  minimum factual gate. Received 8/missing 2 now survive verification; requests
  remain distinct from agreement; unspecified penalties are not invented.
- Payment wording remains indirect: it reports incomplete acceptance, unpaid
  balance and unchanged contract conditions, without fully restating both
  delivery and signed-acceptance conditions. Do not claim perfect answers.

## Real handoff exposed a separate existing failure

Catalog selection created one ordinary import for three files (including a real
directory named `reports.zip`). Large-package deep summary and deep report
completed. The ordinary import failed with **`KeyError: 'total_size_human'`**
before preliminary file/node/root summaries were available. This round does not
claim the full handoff passed. Preserve the failure and repair its metadata
contract in the next separately backed-up round.

The initial handoff harness attempted a second isolated worker while the first
still held its lock. Single-worker exclusion correctly rejected it. A read-only
resume reused the original task and worker, exposing the actual metadata error.
No duplicate import was created and no lock protection was disabled.

## Release limitations

This is not tens-of-GB acceptance, production runtime activation, broad mail
quality evaluation, database capacity certification or endpoint protection
validation. Tests used isolated state, not production databases. No service
restart, database migration or remote push is part of this round.
