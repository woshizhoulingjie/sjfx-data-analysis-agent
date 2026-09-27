# Round15: lossless boundary review for ordinary smart catalogs

Date: 2026-09-27. Baseline: `088f4bcc16e4ecc016acbce7137568111c3f66d6`.
Backup: `.codex-backups/engineering-round15-catalog-boundaries/baseline.tar`.
SHA-256: `ec126a158bf7b915cbdb9b296c6725adda5607c7cb79fc596b06879a5ae35e84`.

## Observed root causes

Round14 actual worker execution grouped an unrelated maintenance document with
purchase/receipt correspondence. On the saved five-file synthetic corpus, the
centroid algorithm computed boundary samples but omitted `boundary_documents`
from its returned clusters. Naming therefore saw only the three central files.
The semantic tree had a second bug: it removed flagged paths from normal groups
without adding a review group, losing those files from the generated directory.

## Bounded changes

- Preserve boundary samples in centroid results and expose the same bounded
  peripheral sampling for the legacy clustering strategy. Fallback samples do
  not duplicate the three central representatives.
- Keep rejected documents in a stable-ID `needs_review` group with complete
  member paths and actual byte totals. No physical files are moved or deleted.
- Only sampled paths belonging to the cluster can be flagged. Malformed or
  wholly unknown model mismatch fields are unverified, not a successful review.
- Version naming cache keys and include the sampled boundary paths. Existing
  cached names do not bypass the new contract. This incurs a one-time cache miss
  when analyses run again; stored historic trees are not silently rewritten.
- Share the existing 2,550-character per-cluster profile budget across central
  and boundary samples. No extra naming call per cluster, no extra embedding
  pass. Character budget is not a token or latency guarantee; path/header
  overhead and small additional tree work remain.

No UI schema, summary format, production database schema or security policy is
changed. Ordinary conclusion/supporting-evidence formatting stays unchanged.

## Validation

17 new boundary tests cover both strategies, fallback, cached results, rejected
sample retention, uniqueness, deterministic identity, malformed/foreign flags,
and profile budget. Final selected regression: **96 passed, 1 failed**. The one
failure is pre-existing `test_candidate_batch_uses_short_budget`: current code
requests 960 tokens while this older assertion expects 180. Baseline with the
same new tests: **83 passed, 14 failed**; 13 failures belong to the new boundary
tests and one is the identical old budget failure. No new existing-test failure
was observed. This is not a full-repository run and must not be reported all-green.
The first test fixture assumed direct file children, ignoring existing subtopic
nesting; it was corrected to inspect descendant leaves. Original logs remain.

Two actual local embedding + naming + tree runs on the saved synthetic parsed
corpus produced three purchase/correspondence files, one finance file and one
maintenance file in the review node, with five unique members and no omissions.
The final run used the final budget/code and took 9.66 seconds once. This is a
single small-corpus observation, not a performance benchmark or p95 claim.

`tree-replay-baseline.json` and `tree-replay-patched-final.json` hold the same
fixed real naming output and source documents. Baseline loses the flagged
maintenance path; the patch retains it exactly once. Zero model calls and zero
product-data writes make this a deterministic tree-assembly comparison.

## Limits and rollout

Only bounded boundary samples are reviewed, not every document. Small clusters
whose members are all central representatives have no extra peripheral sample.
The model may still misclassify; `needs_review` is intentionally not a confident
new topic. This does not prove all ordinary catalog quality defects are solved.
Old stored/deep nodes, overlapping formal topics, missing structured conclusions,
empty preliminary surfaces and browser presentation need separate validation.
Round15 validates real model components and tree structure, not a repeated full
HTTP/worker import chain, tens-of-GB capacity or archive-member handoff.

No production restart, database migration or push. Back up before scoped install,
verify hashes and the initial baseline ancestry, test installed files, commit
only this service, test and document. Roll back only these scoped changes after
checking later work; never reset the whole repository or production database.
The overall engineering goal remains active.
