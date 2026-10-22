# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### R25 (2026-10-23): a failed layout fsync blocks every ACK

- **Fixed SF-R25-01 (P1, cross-family review R25)**: R14 only *recorded* a
  failed parent fsync as `layout_sync[parent] = "failed:<error>"`; the store
  opened anyway and `ingest()` -- including an idempotency replay -- acknowledged
  objects whose directory entries were never proven durable. A reopen synced
  nothing (`created == []`) and lost even the record. Now:
  - `ObjectStore.__init__` raises `LayoutNotDurable` (an `IngestError`,
    exported from `safeforge`) when any parent fsync raises; the exception
    carries the full `layout_sync` report, failed parents as `"failed:..."`.
    No store object exists, so no ACK path can run.
  - Every open re-syncs the parents of root/, objects/ and staging/, not only
    of directories it created, so a layout left unproven by a failed or
    crashed earlier open is proven again -- or refused -- after a restart.
  - `"unsupported:"` (Windows) and `"degraded:"` (macOS without F_FULLFSYNC)
    do not block, the same treatment the per-ingest directory fsync gets in
    `ingest()`, where only a raising `fsync_dir` prevents the ACK.
  - **Behaviour change:** a root whose parent cannot be opened for reading
    (execute-only) no longer opens with a `"failed:"` status; it is refused.
  - Remaining gap: ancestors *above* root's parent that an earlier open
    created are not re-synced on reopen (the store cannot tell them from
    pre-existing ones).
  Tests: `tests/test_r25_layout_ack_gate.py` (fault injection via
  `durable.fsync_dir`, each test asserts its fake was called; positive
  controls for ok/unsupported/degraded), R14 tests adapted to the new
  contract and no longer skipped on Windows. Red at 19d973f, green after.
  Not tested: a real power cut with a failing fsync; Linux/macOS runs of the
  new tests are left to CI.

### R14 (2026-10-22): power-cut tests, layout durability

Closes the open item "power cut untested" from the R13 handoff as far as
software simulation reaches; it is still not a proof (see README).

- **Added** `tests/test_powerloss_lazyfs.py`: crash at each of 9 failpoints,
  then LazyFS `clear-cache` (all unsynced bytes, SQLite WAL included, are
  dropped), then reconcile. Shipped writer: invariants hold everywhere, the
  acknowledged object is intact. Negative controls: without the staging fsync
  the acknowledged object comes back corrupt (`missing_or_corrupt`); with
  `synchronous=NORMAL` the acknowledged upload is lost. A canary file proves
  each cut dropped data. LazyFS pinned to commit `fa7d32e`
  (`experiments/lazyfs/build_lazyfs.sh`, spdlog/toml11 pinned too); new CI
  job `powerloss` with `SAFEFORGE_LAZYFS_REQUIRED=1` so a broken setup fails.
- **Added** `tests/test_powerloss_dirmodel.py`: LazyFS persists directory
  operations immediately (measured), so lost directory entries are covered by
  a model: every entry created or removed since the directory's last real
  fsync may independently survive or vanish (traced by
  `tests/powerloss_driver.py`). 17 states over 9 failpoints, all pass; a
  writer without the directory fsyncs loses the acknowledged object.
- **Fixed SF-R14-01 (P3)**: `ObjectStore.__init__` created root/, objects/
  and staging/ without fsyncing their parents, so a power cut soon after the
  first acknowledged upload could remove the directory holding it. Parents of
  every directory the store creates are now fsync'ed; the result is reported
  in `ObjectStore.layout_sync` (`"failed:<error>"` if a parent cannot be
  opened, e.g. execute-only) instead of being hidden. Red at 6e35d71, green
  after (`tests/test_r14_layout_durability.py`).
- LazyFS quirk documented in `tests/lazyfs_harness.py`: a `--config-path`
  containing "-o" (e.g. `/tmp/pytest-of-root/...`) is silently replaced by
  `config/default.toml`; its completion FIFO is opened once and must stay
  open on the reader side.

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
- Test-only: the portable R9 guard tests wrote a copy of `lock()`'s registry
  bookkeeping instead of calling `lock()`, so on Windows/macOS they stayed
  green when `lock()` registered under another key or never deregistered.
  They now run the real `coordination.lock()` with only flock and the POSIX
  ownership/marker checks stubbed, and add a nested-lock test.

### Packaging
- `MANIFEST.in`: the sdist now ships the test support files
  (`tests/__init__.py`, `conftest.py`, `crash_driver.py`, `invariants.py`,
  `tests/designs/`), `docs/`, `CHANGELOG.md` and `SECURITY.md`. Before, its
  tests did not collect (3 collection errors, exit code 2); now pytest inside
  the unpacked sdist runs 225 passed, 47 skipped (Windows, CPython 3.12), the
  same as the checkout. `experiments/` stays out (not needed by the tests).
  The wheel is unchanged. `tests/test_packaging_manifest.py` checks the rule.

### Fixed (R11, cross-family findings on fe123ab)
- **SF-P3-03, raised to P2: one store's lock unlocked another store's
  database.** `lock()` keyed its registry by `Path.absolute()`, `connect()` by
  `os.path.abspath()`. abspath removes `..` lexically; the kernel resolves it
  after symlinks. With `p/a` a symlink into `elsewhere/`, holding the lock of
  `p/source` let `connect("p/a/../source/safeforge.db", coordinated=True)` open
  `elsewhere/source/safeforge.db` without its lock, so an EX offline backup of
  that store could run beside the writer. The registry is now keyed by the
  directory identity `(st_dev, st_ino)` (`coordination.root_key`), connect no
  longer normalises the path, and the identity is re-checked after
  `sqlite3.connect`. Side effect: a legitimate root spelled with `..` or
  relative is no longer refused.
- **SF-R11-01 (P3):** three forged states failed closed but leaked a raw
  `OSError` instead of `CoordinationRejected`: a marker swapped for a symlink
  between `lstat` and `open` (ELOOP), and a symlink or directory in place of
  the lock file (ELOOP / EISDIR).
- **SF-P2-01 (P2):** `backup_offline`, `restore_offline` and `restore_atomic`
  ignored what `durable.fsync_dir` returned. Their reports now carry
  `directory_fsync` (`"ok"`, or every distinct `"unsupported:…"` status, e.g.
  on Windows) and `power_loss_verified: false`, next to the unchanged
  integrity verdict. `verify_backup` (read-only) does not claim either.

### Tests (R11)
- `tests/test_r11_coordination_forgers.py`: one forger per marker, lock-file
  and root check (socket, hard link, foreign owner via real `chown` when run as
  root plus an `lstat`/`stat` shim otherwise (marker and root), mode bits, content, symlink/regular file
  swapped in between `lstat` and `open`, marker or lock changed after `flock`,
  symlinked/non-directory root), each asserting the rejecting check's own
  message; registry identity across `..`, symlinked parents and relative paths.
- `experiments/dk_coordination_mutants.py`: deletes the 19 checks one at a
  time from a copy of the tree. Linux, CPython 3.13, root: **19/19 killed**,
  restore green; also 19/19 as an unprivileged user (shims instead of
  `chown`). The R9 tests alone killed **3/19** (link count, mode,
  content); owner, both `O_NOFOLLOW`s, the inode comparisons, the lock-file
  and root checks could each be deleted unnoticed.

### Fixed (R12, self-review of the R11 head c52a7b1)
- **SF-R12-01 (P2): backup/restore containment was lexical.** The same
  defect class as SF-P3-03, missed in R11: `backup_offline` accepted a
  destination inside the live store spelled `x/../store/bk` or through a
  symlink, and `restore_offline` / `restore_atomic` a target inside the
  backup through a symlink (the backup then no longer verifies). Containment
  now compares `os.path.realpath`. On Windows `commonpath` raised `ValueError`
  for a destination on another drive, i.e. for an off-volume backup; such a
  pair is now disjoint.
- **SF-R12-02 (P2): macOS durability.** `durable.fsync_fd` (new;
  `fsync_file` and `fsync_dir` use it) issues `F_FULLFSYNC` on macOS and falls
  back to fsync with a `degraded:` status, like SQLite. `db.connect` sets
  `PRAGMA fullfsync=ON` there for FULL/EXTRA and verifies it. Backup/restore
  reports add `file_fsync` next to `directory_fsync`; nested statuses are no
  longer duplicated. All `os.fsync` calls in `src/` go through `durable`.
- **SF-R12-03 (P3): coordination, asked for in R10 (SF-P2-02).** Refuse when
  `O_NOFOLLOW` is missing instead of opening with a weaker flag set; the lock
  file gets the marker's owner/mode rule; ownership is checked against the
  effective uid (files are created with it; `store.py` already used it).
- Test-only: the AF_UNIX marker forger binds a relative name (sun_path limit).
- Regression lock for a side effect of R11: an *uncoordinated* `connect()`
  through a `..` alias reached a coordinated database at fe123ab.

### Tests (R12)
- `tests/test_r12_hardening.py`: 19 of its cases are red against c52a7b1
  (two of them positive controls that fail only because `db.sys` did not
  exist there), all green on this head.
- `experiments/dk_guard_mutants.py` (renamed from `dk_coordination_mutants.py`)
  now deletes 31 guards across coordination, containment and the durability
  report: **31/31 killed**, as root and as an unprivileged user. A kill now
  counts only when the test fails by assertion (read from JUnit XML), as in
  Vero's `diskriminierung.py`; before, any red counted. That tool, run on
  9a8c7fe, found three tests that went red only by crashing
  (`CoordinationRejected`, `AttributeError`, `ValueError`); they now assert,
  and the harness reproduces its 28/31 verdict on 9a8c7fe exactly.
- Existence check (Survival-Item META-S14): the private Vero tool
  `diskriminierung.py` does the same job with a ledger; this repo keeps its own
  small harness because a public repo cannot depend on a private tool. It
  mutates a copy of the tree, so an aborted run cannot leave a mutant behind.

### Packaging (R13)
- The sdist now ships `experiments/` (I1020-10). README "Reproduce" runs
  `python experiments/exp_*.py`, which failed from an sdist, and
  `dk_guard_mutants.py` rebuilds the tree from `src/` and `tests/`. Checked by
  building the sdist with `uv build --sdist` and running inside the unpacked
  tree: pytest 321 passed / 7 skipped, `exp_sync_cost.py` ran,
  `dk_guard_mutants.py` 31/31. `test_packaging_manifest.py` now also requires
  every script the README names to be shipped. This reverses "experiments/
  stays out" above.

### Style (R13)
- `ruff format` applied once (11fac89, 24 files), proven semantics-free:
  `ast.dump` of all 44 tracked Python files identical before/after. The lint
  job now also runs `ruff format --check .`; `.git-blame-ignore-revs` lists
  the commit. Markdown is excluded from the formatter (I1020-10).

### Known limits
- The coordination forgers run on Linux only. macOS (also POSIX `flock`)
  runs none of them.
- macOS F_FULLFSYNC is exercised for real only in the macOS CI jobs: on
  `macos-latest` (CI run 39596375905) `test_real_macos_*` passed, i.e.
  F_FULLFSYNC succeeded on a file and on a directory fd, and SQLite reported
  `fullfsync=1`. Drives that ignore the flush cannot be detected. The same run
  failed two older tests that asserted `os.fsync` is called; on macOS that is
  now the fallback, so they assert the platform's sync primitive instead.
- Windows: no directory flush (no primary source that FlushFileBuffers on a
  directory handle persists a rename); still reported as `unsupported`.
- Power-cut behaviour is still untested on every platform.

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
