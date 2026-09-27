# Logical archive snapshot safety — engineering round 17

Date: 2026-09-27. Baseline: `624b41384f3ae28a847988c18b95c81c1c5a3956`.

## Scope and compatibility

This change hardens the **existing ordinary-import logical archive snapshot reader**.
It does not add independent archive-member indexing to the large-package inventory
or remove the large-to-ordinary bridge's deliberate archive-member exclusion.
Whole physical archives retain the existing handoff behavior. No database migration,
model change, summary layout change, endpoint enforcement activation or service
restart is part of this change. Conclusions continue to use their supporting evidence.

Safe member names are looked up exactly, never normalized into a different record.
Traversal, absolute/drive-like paths, backslash ambiguity, NUL, empty segments,
embedded dot segments, reserved `::` logical delimiters and excessive depth are
rejected. A leading `./` commonly produced by TAR is preserved. Ordinary POSIX
colon filenames and Chinese names remain readable; Windows output-name restrictions
still apply on Windows. Snapshot output names must be a single safe filename.

TAR directory scanning uses `MAX_LOGICAL_UNITS_PER_CONTAINER`, not the smaller
whole-archive preview count. All encountered entries count against the scan budget,
including directories; this is an explicit work bound, not a silent partial import.
Member/content, total declared decompression and compression-ratio budgets remain
enforced, so an over-budget container is rejected even when its selected member is
small. Raising logical count does not disable these independent safety budgets.

## Implemented checks

- ZIP central-directory preflight uses the already verified open container handle.
  Replacing its original pathname does not redirect that preflight. Caller-owned
  handles retain position and ownership.
- ZIP rejects ambiguous duplicate selected names, selected links/special entries
  and encrypted selected members before opening the member stream.
- Selected member size must match the inventory. Declared size and compression
  ratio are checked before reading; actual streamed bytes are independently capped.
- TAR selected links, sparse and special entries are rejected. The complete bounded
  directory is checked to reject duplicate selected names. PAX and GNU-long-name
  metadata have allocation guards; GNU old-style sparse processing is refused.
- TAR directory scan drops accumulated `TarInfo` metadata, honors cancellation
  between entries, and enforces count/declared-total budgets before skipping bodies.
- Existing source identity checks, private temporary-file cleanup and file-local
  error recovery remain in place. A blocked member must not prevent a valid sibling.

## Reproducible acceptance

`tests/test_logical_archive_safety.py` adds 38 focused cases. The selected ten-suite
regression passes 264 tests and four subtests on the staged patch. This is not an
all-repository result. The duplicate-name fixture deliberately emits one warning.

The separate same-input acceptance corpus contains real ZIP, TAR, tar.gz, tar.bz2
and tar.xz files with Chinese source text. Actual process-isolated parsing preserves
all five texts, evidence and logical source paths. Input hashes remain unchanged;
temporary snapshots are removed. Duplicate ZIP rejection changes from false to
true while a healthy sibling continues to parse. No model service is called.

A 4,000-entry TAR extraction-only measurement reduces traced Python allocation
peak from approximately 5.97 MB to 4.22 MB. This is **not total RSS**. Two single
observations showed extraction time 0.5101→0.7062 s and 0.5208→0.5523 s respectively.
These noisy small measurements demonstrate a memory/safety tradeoff, not a speed
improvement or a capacity benchmark. Compressed TAR still needs directory scanning
and decompression to establish uniqueness.

## Remaining boundaries

- No tens-of-GB acceptance, full HTTP-chain rerun or browser visual acceptance.
- No new large-package archive-member discovery, persisted identity, catalog node
  ownership, selection contract or independent member handoff. Those must be built
  together; simply removing the bridge filter is incorrect.
- Standalone bz2, RAR and 7z branches are not comprehensively hardened by this round.
  RAR/7z optional dependencies are absent in the test environment and are not accepted.
- Ordinary archive inventory discovery itself is not fully brought under the new
  TAR allocation guards. Snapshot hardening is not complete ingestion hardening.
- An open handle prevents path substitution, not concurrent in-place mutation.
  Final source-stat checks cannot prove a fully immutable ZIP central directory
  throughout preflight and ZipFile construction. No exhaustive race-safety claim.
- Snapshot extraction occurs before the isolated document parser; parser timeouts
  do not by themselves provide a hard extraction deadline for every archive codec.
- Archive checks are not malware detection, phishing detection or local endpoint
  monitoring. Server paths cannot reveal arbitrary client-local paths.

## Backup and rollback

The immutable tracked baseline archive is stored under
`.codex-backups/engineering-round17-archive-handoff/baseline.tar`, SHA256
`72fe839bf5f957efbfb41e411d38d9e69a0784e65af7055ea156601552557286`.
Before installation, copy the three changed existing modules to `predeploy/`.
Keep test logs, same-source acceptance receipts and deployment receipt alongside
the archive. Verify the expected parent and a clean worktree before applying.
Commit only the three modules, focused tests and this document after installed
regression passes. Retain the initial user's-work commit in ancestry.

If rollback is needed later, first inspect newer commits and uncommitted work.
Use a reviewed inverse change, not a whole-worktree reset or blind archive overlay.
Do not restart production or claim live activation as a side effect of committing.
