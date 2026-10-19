# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

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
