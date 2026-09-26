# Verified answer retention

## Failure and scope

A real model run on the isolated six-file acceptance package answered received /
missing counts in its first draft. A subsequent repair rewrote the supported
paragraph into wording rejected by the final verifier, leaving payment terms
only. Prompt instructions to preserve facts did not prevent this regression.

This change selects a whole verified answer from the existing bounded generation
attempts. It does not change the model, retrieval budget, claim-verification
thresholds, preliminary-summary format, mail analysis, databases or file controls.

## Safety and selection

- Enabled by the existing conversation-quality switch; no extra generation or
  retrieval calls. The runtime still permits at most two repairs.
- Snapshot answer, its own citation set, plan and session together. Never splice
  drafts or transplant citation numbers.
- Track raw observed evidence before result merging. A changed body under an
  existing evidence ID cannot disappear behind merge deduplication.
- Earlier candidates are ineligible when observed evidence is unknown or changes,
  citation content/provenance changes, scope changes, or package digest changes.
  Empty successful repair searches do not erase the already observed corpus.
- Fingerprints include evidence identity/path, original content, translation,
  location, preview/coverage/role and revision metadata. Ranking and citation
  numbering do not change evidence identity.
- Revalidate each eligible candidate under final tools/batch and the same answer
  contract. Apply the existing unsupported-claim guard and revalidate again.
  Earlier candidates must have no failed factual claims, invalid generated
  citation indexes, out-of-scope citations or numeric failures.
- Prefer fewer missing explicit answer aspects, then more distinct supported
  claims. Equal scores prefer the latest attempt. Raw length is not a score.
- Preserve cancellation checkpoints and the final runtime guard. The selected
  result and its verification ledger use existing persistence; a diagnostic
  `candidate_selection` records the selection and ineligibility reasons.

This is intentionally conservative: changing citation subsets or adding evidence
may make an otherwise useful previous answer ineligible. It does not fix every
free-form entailment false negative or promise complete answers for every prompt.
Local reverification adds bounded CPU and snapshots add memory; no production
latency, throughput or capacity improvement is claimed without measurements.

## Evidence: engineering-round11-answer-retention

- Backup parent: `3817f1cc25b07116f96935b90b39f314c7841412`; source archive and
  independent synthetic-state database backups retained before implementation.
- Final 30-case suite on the original runtime: 5 failures, 15 passes, 10 skips
  (new helper absent). The four answer-loss failures are reproduced; the fifth
  asserts new persisted diagnostics. All 30 pass after the patch.
- Expanded regression: 565 passed, 365 subtests; consumers: 51 passed, including
  the same 30 new cases. Total: **586 distinct tests**, not the full repository.
- Fixed original and repair drafts from the previous real model run, driven
  through the actual runtime/storage/verifier: original loses both counts; patch
  keeps 8 received / 2 missing and conditional payment terms, preserving the
  chosen draft's citations and saved conversation. No new model call in replay.
- Fresh isolated API/worker/model run: received/missing counts and payment
  conditions answered, seven supported claims, no missing aspects/failed claims.
  Both attempts score equally, so the latest wins; this is not evidence of
  retention being needed in that fresh run. The fixed-draft replay proves that.
- The isolated worker is stopped only after identity checks and 12 seconds of
  stable queue idleness. Production processes/databases are not restarted or
  migrated by the validation.

## Release and remaining work

Install the new module before its runtime consumer, preserve the exact parent and
clean-tree guards, take predeployment backups, and commit only this round's four
files. Do not force-reset or discard concurrent work. No push or service restart
is part of this round. Long-lived production activation remains unverified.

One small synthetic package and deterministic tests do not establish tens-of-GB
end-to-end capacity, recovery, catalog/mail quality, semantic quality of all
summaries, database performance or authorized client-side OS enforcement. The
broader engineering goal remains active. Server paths alone cannot prove arbitrary
client-local paths. Preserve the preliminary “结论—支撑证据” format throughout.
