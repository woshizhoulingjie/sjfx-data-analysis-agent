# Desktop protection runtime contract — 2026-10-14

This candidate is source and component evidence, not a completed installer or full
end-user acceptance. The existing 18765/18766 acceptance assistants are untouched.

## Owned lifecycle and independent installation

`source/sjfxctl.py setup` writes a new current-user-only instance configuration;
it never replaces an existing one. New desktop installations enable protection.
Subsequent explicit policy disable is preserved and recorded by the core policy
history. `run` owns helper, protection executor, Web and enabled business workers.
The helper and executor initialize before Web. All roles are stopped on an owned
child failure; Windows uses process Job ownership and POSIX children also watch
the controller identity and reap their private session if the controller dies.

Formal state is `DATA_DIR/desktop_protection`, with its own installation ID and OS
owner, credential file, helper database and stop request. Core registrations remain
in `DATA_DIR/file_protection`. No old helper database/remote device identity is
adopted. A changed OS owner, foreign device or business owner fails closed.
Windows credentials receive a protected current-user-only DACL; Linux uses
owner-only modes and rejects a token owned by a different user or group-readable.

The process interface is authenticated POST JSON at
`SJFX_LOCAL_AGENT_ENDPOINT` (default `http://127.0.0.1:18767`). Only IPv4 loopback
is accepted; any browser Origin is rejected. The token is never returned in health,
Web payloads, URLs or argv. The native helper's own `--listen` is configurable.
The launcher does not install/download models, change Defender, create an elevated
Web process, or alter existing scheduled tasks. Offline model environment is set.

Helper `healthy` / `directory_monitor_healthy` describes observation coverage;
`access_audit_healthy` separately describes native program-access evidence.
Application initialized readiness, executor heartbeat, engine/database health and
model services are separate fields. A running executor is not a virus-scan receipt.
ClamAV accepts the installed `SJFX_PROTECTION_CLAM_DATABASE` and the restricted
`SJFX_PROTECTION_SCAN_WORKDIR` configured for this installation.

## Complete registration transactions

All routes are POST below `/agent-local/v1/register/`. Each request is at most
1 MiB. The old `/agent-local/v1/register` remains compatible for at most 1,000
files and 8 GiB aggregate. Transactions accept at most 200,000 members and default
32 GiB aggregate stable hashing; `SJFX_LOCAL_AGENT_REGISTRATION_BYTES` may set an
explicit positive budget up to 1 TiB. Each individual file is limited to 10 GiB.
Core snapshot/inspection budgets are additional independent limits.

1. `begin`: `{registration:{device_id,owner_id,registration_id,manifest_revision,
   gap_epoch,local_root},expected_count,expected_bytes,manifest_digest}`. No files
   in this metadata. Reply: `transaction_id,next_offset:0,coverage_complete:false`.
2. `batch`: `{transaction_id,offset,files}`. At most 500 members, continuous offsets
   only. Reply includes `next_offset,coverage_complete:false`. Batch completion
   never activates bindings or claims complete coverage.
3. `commit`: `{transaction_id}`. Verify expected count, total bytes and digest,
   rehash every real member, revalidate each member again immediately before the
   DB commit. Formal bindings, frozen receipts and committed marker activate in
   one SQLite transaction. Reply includes `count,manifest_digest,gap_epoch,
   coverage_complete:true`. Any missing/changed/unauthorized member rejects all.
4. `result`: `{transaction_id,offset,limit}` with limit 1–500. Reply includes frozen
   `bindings,count,offset,next_offset,manifest_digest,coverage_complete:true`.
   The core verifies every returned instance/version field before associating.
5. `finish`: `{transaction_id}` after complete receipt verification and core
   persistence. Releases retained transaction rows without deleting formal
   bindings or histories. `abort` accepts only uncommitted transactions.

File fields are `binding_id,file_id,local_path,sha256,version_id,size,identity`.
Manifest digest is SHA256 of UTF-8 rows in submitted order:
`NUL.join(binding_id,file_id,local_path,sha256,version_id,decimal(size),identity)`
followed by newline for each row. Field strings must not contain NUL/CR/LF.
Result bindings use `local_identity`, plus device/owner/registration, manifest and
gap revisions. A committed receipt is frozen historical evidence; a restart gap
prevents idempotent `commit` from reporting that historical receipt as current.
Staging is durable but does not imply the files were accepted. At most eight
unfinished/retained transactions are allowed, and staging expires after 24 hours;
the caller can abort and restart. Batch retry after an uncertain response requires
abort/new begin, because duplicate offsets are deliberately rejected.

`/agent-local/v1/health` and sync health return summaries, not the full binding
list; large receipt lists use result pagination. Event/result queues are durable,
and the core acknowledges only after its own durable ingestion.

## Native audit maintenance

Windows uses selected-file SACL plans saved before apply and Security 4656/4663.
Events preserve program path, PID, user, time, access mask and native event/record
IDs. SACLs and file identities are read back before access health is reported.
`scripts/Install-DesktopProtection.ps1` defaults to read-only preflight. Explicit
maintenance actions `EnablePolicy`, `ConfigureFiles`, `Restore`, `CheckUninstall`
require an already authorized administrator terminal; the script never elevates.
Original global audit flags and file SACLs are retained. Restore compares current
settings with the saved applied settings before replacing them, preserves other
administrators' changes, and refuses uninstall if dispositions or SACL restores
remain unresolved. Ordinary runtime permission failures remain visible as access
coverage false and do not masquerade as directory-monitor failure.

Linux uses a Python observer with native dev/inode identity, stable hash polling,
new directory member discovery, persistent event queue and durable restart gaps.
Polling can miss intermediate writes. Linux observer lock/quarantine requests
return rejected/performed:false; they never manufacture successful side effects.
`scripts/desktop_audit_install.py preflight/plan/apply/restore --state <formal-root>`
supports auditd rules for the registered selected paths. Apply saves original
rules before changing only this installation's key and writes identity/boot/rule
receipt after native read-back. Restore removes only the saved newly introduced
matching rules. These are live kernel rules; distribution-specific boot persistence
is not installed, and a boot change invalidates the receipt.

Linux access evidence parses complete auditd SYSCALL/PATH/EOE groups, retains
program/PID/uid/syscall/permission evidence and native record ID. It requires a
readable `SJFX_LINUX_AUDIT_LOG`, current selected-file installation receipt and
successful actual `auditctl -l` query matching all rules. An ordinary account
without the required query/log permissions accurately reports coverage false;
the installer receipt alone is not treated as current proof. Missing auditd,
log rotation/truncation, event-loss records, incomplete groups, boot changes or
replaced instances all prevent healthy access coverage. No claim is made about
all Linux distributions or missing privileged kernel capabilities.

An access request/read-right event does not prove all bytes were consumed, uploaded
or sent to a destination. Historical Security/auditd events do not supply content
hashes; attached version IDs explicitly mean current binding at collection, not
historical byte proof. Full privileged real-application access acceptance and full
installer/ordinary-user cold start remain the user's final acceptance work.

## Component validation

Python: `source/tests/test_desktop_endpoint.py` includes 1,001 real small files,
partial transaction rejection, whole-commit changed-version rejection, paginated
receipts, queue/ack/restart persistence, owner isolation, authenticated ephemeral
loopback health, Linux unsupported side effects and native auditd record parsing.
Go: `agent/internal/endpoint/desktop_transaction_windows_test.go` adds equivalent
1,001-file atomic registration/receipt tests and separate audit-health failure.
Existing Go endpoint tests remain enabled; privileged audit smoke is explicitly
not run. `scripts/validate_desktop_lifecycle.py` starts only an isolated owned
helper/executor pair, verifies readiness, stops them, and records no models created
or global audit changes. All tests use isolated state and an independent port.
