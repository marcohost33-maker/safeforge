# SafeForge R3: replay-integrity decisions

Status: reference implementation, not production deployment. Source baseline:
`9ee0ddc76c14aef2b62038faabe1e1ef5749ec2b` + cumulative R2/R3 local patch.

A reused idempotency key **does not** authenticate the incoming bytes. The previous
implementation silently returned a success for a different request body if the
caller did not supply `expected_sha256`. R3 consumes and hashes each repeated
stream, enforces the configured size bound and compares it with the stored SHA-256.
It also rehashes the existing stored file before acknowledging the replay.
This changes repeated-upload stream-consumption semantics intentionally.

`reconcile(verify_stored_digests=True)` enables optional full content re-hashing.
The default is faster and checks existence + size only. Do not claim that the
non-hashing default detects same-size corruption. Reconciliation reports a
problem but never repairs a corrupted stored object automatically.

**Known limits**

- Hash verification adds O(upload size + stored size) work for repeated uploads.
- On hostile shared volumes, filename checks do not prevent inode-swap or
  path replacement races. Use private, permission-restricted storage; a
  multi-tenant attacker-safe read/publish API needs dirfd-based operations.
- The R2 hardlink + unlink fallback is two operations; process-crash tests do
  not prove physical power-loss durability.
- Explicit `renameat2(RENAME_NOREPLACE)` could be an optimization on Linux
  where supported. No implicit unsafe `rename` fallback is allowed.
- SQLite WAL and database-journal files belong together; copying only the `.db`
  file during a live operation is unsafe. Use SQLite's backup API.
- No malware/content scanning, network API, tenant isolation, attestation,
  credential handling, or public marketplace deployment is implemented.

**Release gate**: Linux post-patch CI, Windows tests, volume/fsync/power-fail
matrix, authorization and quota model, threat review, rollback/recovery plan.
