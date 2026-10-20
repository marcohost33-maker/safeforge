# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Integration of the cumulative R2–R9 hardening from an external cross-family
review. **Status: best-effort cooperative hardening of a reference
implementation. Production: NO GO.** The coordination lock is an advisory
`flock`: raw `sqlite3` clients, legacy versions and plain filesystem writers
are not fenced. Power-cut / power-loss behaviour was **not** tested; all crash
tests are process kills.

### Added
- `safeforge.coordination` (POSIX only, opt-in): `initialize`, `lock`,
  `event_writer`. Coordinated connections need a live lock in the calling
  process/thread (`require_held`); `Connection`/`Cursor` re-check it on every
  DB-API call; a SQLite authorizer denies SQL outside the lock (statement cache
  off); the marker is checked for type, owner, mode, link count, inode and
  content; a coordinated `ObjectStore` accepts only `root/safeforge.db` and
  `root/staging`; the lock registry is discarded after `fork`. On Windows every
  coordinated path refuses (`CoordinationRejected` / mode mismatch).
- `safeforge.offline_backup`: `backup_offline`, `verify_backup`,
  `restore_offline`, `backup_coordinated`. Requires externally quiesced writers
  (operator-attested, not verified by the library); no signature or encryption.
- `safeforge.atomic_restore.restore_atomic`: Linux only, publication via
  `renameat2(RENAME_NOREPLACE)`, no fallback on other platforms.
- `ObjectStore.open_verified()`, `ObjectStore(enforce_private=True)` (POSIX),
  `reconcile(verify_stored_digests=True)`.

### Changed
- POSIX `durable.publish` now uses `link` + `unlink` (no-clobber, two
  operations; a crash in between leaves both names for `reconcile`) instead
  of `rename`. Windows is unchanged (`MoveFileExW`).
- Idempotent replays hash and size-check the new request body instead of
  trusting the key, and re-verify the stored object before acknowledging.
- `extract_zip` removes a partially extracted destination on any failure and
  enforces the declared sizes while extracting; stricter `ZipPolicy` checks.
- Events reject NaN/Infinity in effect results and in stored replay rows.

### Fixed (during integration, Windows)
- `backup_offline` fsynced the backup database through a read-only handle,
  which fails with `EBADF` on Windows.
- Two test portability defects (`UPDATE … LIMIT`, an over-long parametrize id).

### Known limits
- Verified locally on Windows (Python 3.12) only. Linux results come from the
  reviewer's synthetic checkout; macOS was never run. The Linux-only R8/R9
  tests (flock, marker metadata, `renameat2`, `fork`) have not run on the
  integrated tree.

## [0.1.0] - 2026-10-19

### Added
- `safeforge.store.ObjectStore`: intent → stage → publish → directory fsync →
  conditional status commit, plus an idempotent `reconcile()` with a grace
  period and `verify()` for a full re-hash.
- `safeforge.durable`: `fsync_file`, `fsync_dir` (reports "unsupported" on
  Windows rather than skipping silently), `publish` (same volume, no clobber;
  `MoveFileExW(MOVEFILE_WRITE_THROUGH)` on Windows), `require_same_volume`.
- `safeforge.db`: WAL with verification, `synchronous=FULL` by default (OFF is
  refused), explicit `transaction()` that also rolls back when COMMIT fails.
- `safeforge.zipcheck`: `validate_zip` / `extract_zip` with 31 distinct rejection codes.
- `safeforge.events.process_event`: one effect per event id, in the same
  transaction as the event record.
- Fault-injection tests (crash at each of 8 steps, concurrent reconciler at
  each step), SQLite locking reproductions, kill-durability tests, and negative
  controls (naive design and the reconstructed V0.3 pattern).
- Experiments: contention matrix, sync cost, mmap.
