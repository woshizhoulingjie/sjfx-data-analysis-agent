# Numeric answer verification and completeness

Date: 2026-09-27. Base: `e00d542339fefed9ce2d19a7513a46f48c6eafe1`.

## Scoped changes

- Match whole numeric tokens in each claim clause, not substring matches such as 2 inside 12 or a year. Check every occurrence of a repeated value so one valid field cannot authorize another field or entity.
- Keep sentence-final periods distinct from a continuing decimal number.
- Bind a value to an explicitly specified unit in its own key, including parenthesized units. Do not borrow currency, scale or units from another field.
- Preserve explicit signs in the verifier's numeric token set.
- Recognize received and missing quantity questions as separate requested aspects. Require role, number and count unit; a heading or acceptance status alone is insufficient.
- Compute missing aspects from the text that survives the existing final guard, not from rejected draft statements. This makes the missing fields visible to the existing repair loop before final filtering.
- Keep existing entity, attribute, date-order, polarity, scope, citation and preview restrictions. No thresholds were lowered. No new model calls, database schema changes or external dependencies were introduced.

The ordinary preliminary summary format remains unchanged: conclusion followed by supporting evidence.

## Regression evidence

The initial valid added-test baseline had 9 failures and 6 passes, plus 4 subtests. An earlier run with an ineligible citation fixture was a test-harness error, not product evidence.

The first broad patched run caught a sentence-final period regression (181 passed, one failed); this was repaired with positive and adversarial decimal-boundary assertions.

Final regression: **418 tests and 362 subtests passed**, 101.18 seconds reported by pytest, 102.91 seconds including harness setup. This covers the 18 new tests and existing answer quality, natural answers, analysis contracts/turns, conversation, evidence quality/selection, preview processing, catalog updates and large-to-ordinary import identity. Evidence is preserved in `evidence/feedback-expanded-regression-tests.{json,log}` below the round backup.

## Real-model acceptance: NOT PASSED

Six synthetic documents were imported through the actual API into an isolated local-filesystem database, using the real local model and actual workers. Package: `lp-8dd270e7197e4c688ebacf447dd75954`; catalog: `cat-e0614b15ad02b35b0996`. Catalog and report met the revision/stability checks at 64.6 seconds; three initial QA jobs finished 79.8 seconds after queueing. These measurements are not large-data benchmarks.

The final numeric answer still omits received/missing quantities. It now correctly reports missing aspects and partial fulfillment instead of treating the remaining payment clauses as a complete answer. Passive diagnostics preserve the actual draft and unchanged verification output. The updated repair loop receives the missing quantities, but prompt guidance alone did not solve the factual-binding problem.

Open defects include conservative cross-sentence subject/date binding and formatted thousands separators such as `182,000`. Additional quantity-unit and identifier/date tokenization cases need a separate tested parser improvement. Do not weaken validation, hardcode acceptance facts or treat task completion/citation verification as proof of answer completeness.

The QA gate remains closed. Deep-summary and catalog-to-ordinary full semantic acceptance did not run past this gate. No tens-of-GB end-to-end acceptance, production runtime activation or full-system certification is claimed.

## Backup and operational boundaries

Round directory: `.codex-backups/engineering-round6-numeric-answer-quality/`.

- `baseline.tar`: source baseline with SHA256 `81360c412b879613e66f0fa6e5824d4b03b7273cfa956b57b0f2e21a28cee7c3`.
- `pre-feedback/`: tested intermediate implementation retained before feedback changes.
- `predeploy/`: exact production files before the scoped commit.
- `evidence/`: passing and failing tests, actual QA and passive verifier diagnostics.
- Test database: `/home/zhoulingjie/.local/state/sjfx-engineering-round6-numeric-answer-quality`, initially copied through SQLite's backup API from prior isolated state. Production database and NFS protection remain unchanged.

No production restart, Git push, production migration or client monitoring agent installation is part of this round. Existing processes may still be running earlier loaded modules; source commit does not establish runtime activation.

Remaining goal areas include robust numeric binding, full deep/ordinary handoff, tens-of-GB capacity/recovery, catalog semantic quality, email intent analysis, database efficiency, and server/authorized-client protection. Client-local paths cannot be inferred reliably from server paths alone; local monitoring/enforcement requires authorized endpoint software and trustworthy identity mapping.
