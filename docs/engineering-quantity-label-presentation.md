# Quantity labels: conservative presentation matching

Acceptance date: 2026-09-27 (Asia/Shanghai).
Baseline: `e08675cd23303d9511e8e7c431bcb70da1e8477d`.
Backup: `.codex-backups/engineering-round9-qa-completeness/`.

## Diagnosis and change

A passive, spawn-aware observer captured actual model drafts before and after
the unchanged verifier. Deterministic replay against those exact citations
showed that explicit count labels such as `缺少数量为2台` and bold quantity
headings could cause correct requested counts to be removed. The ordinary
comma-separated `实收8台，尚缺2台` variant survived the same evidence.

The matcher now canonicalizes only four explicit count labels immediately
followed by a number and quantity unit. `尚缺`, `还缺` and `缺少` share the
missing-count role; none is merged with `实收`. Optional label text, colon and
bold markers are presentation only. Unknown quantities and monetary labels
are not converted. Original answers, citation text and identifiers remain
unchanged; no answer is manufactured or filled with guessed counts.

Synonym expansion alone initially introduced false matches for negative
quantity statements. Adversarial tests caught these before deployment. A
separate local quantity-role denial check now prevents `不缺少`/`并非缺少`
from becoming a positive missing count. Unconfirmed counts cannot prove a
denied count. All prior project/date/unit/plan checks remain in force; no
threshold, prompt, summary format or schema is relaxed.

## Verification

- Final new test file: 38 parameterized cases. The identical baseline test file
  has 12 failures and 26 passes. All 38 pass as part of the final expanded suite.
- Expanded suite: **475 passed, 365 subtests passed**, pytest 115.26 seconds.
  It includes existing answer, evidence, conversation, catalog, report and
  handoff regressions. This is not the full repository suite; the two known
  person-aggregation failures from round8 remain outside this patch.
- Exact baseline-versus-patch replay preserves every original captured-draft
  verdict. Only the targeted label/bold variants gain supported counts. Wrong
  counts, unrelated projects and contradictory predicates remain rejected.
- A real local-model run keeps received8/missing2, source date, all10-delivered
  plus written-acceptance conditions, and unpaid182000. It also distinguishes
  an extension request from a mutually accepted contract change. The baseline
  live run was also factually adequate: neither run proves the patch caused a
  general semantic improvement or establishes stable QA completeness.
- The isolated worker finished its task, the queue stayed idle for12.2seconds,
  and identity was verified before stopping only that test worker.

An early test wrongly expected the guard never to normalize punctuation. It
was corrected before final baseline comparison to require retained counts,
citations and unchanged input. The initial and corrected test logs remain.

## Still unresolved / release boundaries

Compound quantity phrases joined with `、` or `且`, and counts joined to a
separately negated nonnumeric predicate, can still be conservatively rejected.
Their deterministic failures remain recorded, not hidden or relabeled as
passed. Broad semantic quality and full end-to-end acceptance remain false.

Round8 summary uncertainty/relative-time issues, production activation,
tens-of-GB capacity/recovery, broader mail intentions and authorized endpoint
enforcement require separate backed-up work. This patch does not restart
production, migrate a production database, or push to a remote. It preserves
the ordinary preliminary summary's conclusion/support structure.
