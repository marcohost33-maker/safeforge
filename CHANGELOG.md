# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### R40 (2026-10-30): the fixed-library path runs for real; review of R39

Critical review of R39 against primary data (CI logs of run 40984016099).

- **Blind spot closed (P1 evidence gap)**: no CI runner links a SQLite with the
  WAL-reset fix -- ubuntu 3.45.1, windows/macos 3.49.1 (py3.12.10) and 3.50.4
  (py3.13.16). The True path of `durability_prerequisites_ok` and the release
  gate's no-waiver pass had therefore only run against the conftest pin, never
  against a real library. New CI job `sqlite-fixed`:
  `experiments/sqlite/build_sqlite.sh` builds 3.54.1 from the official GitHub
  mirror pinned by commit (`57520227`, tag version-3.54.1; the commit id is the
  integrity check), loads it through `LD_LIBRARY_PATH`, runs the gate in the
  release profile WITHOUT a waiver and the full suite with
  `SAFEFORGE_EXPECT_FIXED_SQLITE=3.54.1`, which makes
  `tests/test_r40_sqlite_evidence.py::test_required_*` mandatory. Negative
  control: with the variable set on the affected 3.45.1 all three fail by
  assertion. Not run in the authoring sandbox (building third-party code there
  was refused); the CI job is the first execution.
- **Added `source_canonical`** to the SQLite status: False when
  `sqlite_source_id()` is not "date time <64 hex>". sqlite.org says an edited
  tree may change the hash's last four digits; ubuntu-latest reports
  `...82257ccalt1`. Reported only -- the version decision is unchanged.
- **Waiver expiry is announced**: `waiver.days_left` in the verdict; from
  `WAIVER_WARN_DAYS` = 21 days before expiry the CLI warns on stderr, and under
  GitHub Actions it appends the verdict to `GITHUB_STEP_SUMMARY`. Stdout stays
  one JSON line (GitHub reads `::warning::` from stdout, so no workflow
  commands). A summary that cannot be written never changes the exit code.
- **Test isolation**: the CLI tests drop `GITHUB_ACTIONS` and
  `GITHUB_STEP_SUMMARY`, so the suite never writes into CI's real job summary.
- **Workflow lint widened**: a waiver must sit in a step's own `env` next to
  `--profile ci`; a release-profile gate step must carry none; a job- or
  workflow-level waiver fails the test (3 mutated workflows, all caught).
- **Measured in CI (job sqlite-fixed, run 40987503949, first execution)**:
  py3.13.17 loaded SQLite 3.54.1; the gate without `--profile` and without a
  waiver gave `pass`, exit 0, `source_canonical: true`; 588 passed, 24 skipped,
  none of the `test_required_*` skipped.
- **Second integrity anchor**: the tree's `manifest.uuid` is SQLite's Fossil
  check-in id (`555e07ef…ca76` for 3.54.1); the build script checks it against
  the tree and the generated header, and CI pins the full
  `sqlite_source_id()` in `SAFEFORGE_EXPECT_FIXED_SQLITE_SOURCE_ID`, so the
  tests prove that this build was loaded, not just some 3.54.1. Not confirmed
  from a source outside the tree (sqlite.org unreachable from the sandbox).
- **`profile_source: "default"`** when no profile was given (was "requested",
  found in the job log); `release_gate(profile=None)` and the CLI default are
  still the release profile.
- Mutation probe: R40 guards 8/8, R39 guards re-run 11/11 (default mutant
  retargeted), workflow lint 3/3 -- all killed by assertion.
- Corrections to the R39 report: the `main` run for 8ebfbab was cancelled by
  `cancel-in-progress` when b2cba6f was pushed (superseded, green);
  Codex reviewed #4 after the merge with no findings.

### R39 (2026-10-30): no waiver in a release; the ACK names its evidence

- **Fixed SF-R39-01 (P1, GPT-6 cross-family A2 on 89a0381)**: the release gate
  turned an affected SQLite into verdict `"waived"`, exit 0, for ANY non-blank
  value of `SAFEFORGE_ALLOW_UNFIXED_SQLITE` -- an inherited runner variable,
  an organisation secret or a copied job was enough to pass a release, with no
  end date. Reproduced at 484f8d8 (SQLite 3.45.1): `="inherited"` gave exit 0,
  also with `GITHUB_REF_TYPE=tag`. Now:
  - `release_gate(..., profile="release")` and `python -m
    safeforge.sqlite_gate` default to `release`, which accepts no waiver:
    offered anyway -> `"waiver-forbidden"`, exit 3.
  - `--profile ci` accepts only `expires=YYYY-MM-DD; reason=<text>` (each key
    once, reason non-blank, expiry day inclusive, at most `MAX_WAIVER_DAYS` =
    120 ahead); anything else -> `"invalid-waiver"`, exit 2 with the error in
    `waiver_error`. An expired waiver turns CI red again.
  - A GitHub run with `GITHUB_REF_TYPE=tag` or `GITHUB_EVENT_NAME=release` is
    gated as `release` whatever profile was asked for.
  - CI runs `--profile ci` with `expires=2027-01-31`; a test checks that the
    workflow's waiver parses and sits next to `--profile ci` only.
  - Not done: a signed owner approval. The authority for a CI waiver is the
    review of the workflow file; an attacker who controls the release job's
    command line can skip the gate anyway (module docstring).
  Tests: `tests/test_r39_release_waiver.py`; 11 mutants of the new guards
  (release accepts a waiver, no/exclusive expiry, no horizon, no tag/release
  override, blank reason, duplicate/unknown key, default `ci`, truthy `fixed`)
  each killed by an assertion.
- **Added SF-R39-02 (question 3, second round 2026-10-29)**: is "keep walking
  without mount proof" (macOS/BSD, `st_dev-only`) fail-safe? Yes for
  durability: a test on one tree with a simulated same-device bind mount
  shows the `st_dev-only` walk syncs a strict superset of the mount-aware
  walk and reaches the same flag; the cost is availability (an unreadable
  outer ancestor refuses the open), never a missing sync. So the flag keeps
  its meaning and the ACK reports the basis instead: new `IngestResult`
  fields `layout_mount_basis`, `sqlite_version`, `sqlite_wal_reset_fixed`
  (the 2026-10-27 review recommendation); empty/False on a deduplicated
  result. Not added to the backup report: a backup needs quiescent writers,
  so the WAL-reset race does not apply while it runs.
  Tests: `tests/test_r39_ack_evidence.py`.

### R38 (2026-10-29): external staging chain, linear mount table (Codex on ac933f3)

- **Fixed SF-R38-01 (P2)**: a `staging_dir` outside `root` has its own ancestor
  chain, which the SF-R36-01 walk never covered. A failed open that created
  `/external/new/spool/staging` with `/external` unsynced was forgotten on
  retry (nothing "created"), and the retry ACKed -- with an explicit `db_path`
  in `/external/new/spool`. Every open now also walks the staging chain, with
  the same mount probe (no second mountinfo parse). Cost: one stat walk and
  one directory fsync per ancestor level that root's chain does not share.
- **Fixed SF-R38-02 (P2)**: `parse_mountinfo` scanned the whole table for each
  entry to drop covered mounts (quadratic; 6,000 sibling mounts took 36 s
  here). A prefix index plus path counters over the mount parent forest give
  the same result in linear work: a differential test against the previous
  algorithm (3,000 random tables incl. cycles, self-parents and trailing
  slashes) and a line-count budget under `sys.settrace`, not a wall clock.

### R37 (2026-10-29): the ancestor walk stops on mount identity (GPT-6 R15)

- **Fixed R15 (cross-family on 89a0381)**: the SF-R36-01 chain stopped where the
  parent's `st_dev` changed. `st_dev` names a filesystem, not a mount, so a bind
  mount of the same filesystem was walked through and its outer directories
  synced; one that could not be synced refused the open. New
  `safeforge.mounts`: the walk stops only on proof -- another `st_dev`, the
  statx mount id (`os.statx`, Python >= 3.15; unique id from Linux 6.8),
  `/proc/self/mountinfo` (strict; covered mounts dropped; `major:minor` must
  match) or `GetVolumePathNameW` (Windows). Without a source (macOS, BSD) it
  keeps walking. `ObjectStore.layout_mount_basis` names the source used.
- **Still open (R15)**: what a directory fsync is worth on OverlayFS or through
  a bind mount after a power cut. A real bind-mount and power-cut matrix stays
  an integration gate; this change only decides which entries are synced.
- **Checked, unchanged (SF-R35-01)**: the WAL-reset version gate against
  sqlite.org on 2026-10-29: wal.html section 11 (3.7.0-3.51.2 affected, fixed
  3.51.3, backports 3.44.6/3.50.7), changes.html (3.52.0 withdrawn), and the
  branch timelines: on branch-3.44 and branch-3.50 the fix precedes the
  version bump, so the version number is a valid discriminator.

### R36 (2026-10-28): three unearned durability reports (Codex on 4f7404d)

- **Fixed SF-R36-01 (P2)**: a retry after a failed open never re-synced an
  ancestor that the failed open had created (`/existing/new/store` with
  `/existing` unsynced: on retry nothing is "created"). Every open now
  re-syncs the parents of root's whole same-filesystem ancestor chain, up to
  the mount point.
- **Fixed SF-R36-02 (P2)**: `_file_copy_verified` returned the fsync status
  before re-checking the source, so a backup source changed after its bytes
  were read was restored as `offline_restore_copy_pass`. The check runs again
  before the return (regression from R35).
- **Fixed SF-R36-03 (P2)**: the database directory was chosen with
  `os.path.abspath`, which collapses `link/..` lexically; SQLite follows the
  link first. The directory is now resolved as the platform's `open()` resolves
  it (POSIX: symlinks before `..`; Windows: lexical, measured). The same
  resolution now guards the coordinated-mode and `enforce_private` database
  location checks.

### R35 (2026-10-27): the durability flag needs the WAL-reset fix; renamed

- **Fixed SF-R35-01 (P0, cross-family review of 6a39c38)**: the flag checked
  syncs, layout and the commit policy but not the linked library; SQLite
  3.45.1 gave True. It now also requires
  `ObjectStore.sqlite_runtime["fixed"] is True` (exact True: a truthy
  forgery or a missing key gives False). Open and ingest are unchanged.
- **Renamed** `IngestResult.powercut_durable` -> `durability_prerequisites_ok`
  before the first release: it reports six software prerequisites, not a
  power-cut guarantee. `powercut_durable` stays a read-only property alias
  for one minor release; the constructor no longer takes the old keyword.
  This supersedes R28's "the name stays".
- **Fixed SF-R35-02 (P2)**: the backup summary of an empty status part
  (`""`, `"ok;"`, `";ok"`) was `""` -- falsy; it is now
  `"missing:empty-status"`.
- **Decision E2 (2026-10-27)**: `require_wal_reset_fix` stays `False` by
  default; releases are held by `python -m safeforge.sqlite_gate` until a
  build with SQLite >= 3.51.3 (or a backport) is used; PR CI keeps its
  visible waiver.
- Tests: `tests/conftest.py` pins the library as fixed for tests without the
  `real_sqlite` marker, so the True path stays covered on affected runners;
  `tests/test_r35_wal_library_prerequisite.py`.

### SF-XF-WAL (2026-10-27): gate on the SQLite library actually loaded

- **Cross-family finding (GPT-6)**: the README named the upstream WAL-reset
  bug but nothing checked it, and the SQLite bundled with python.org 3.13/3.14
  (3.50.4) and 3.12 (3.49.1) predates the fix. sqlite.org/wal.html section 11:
  affected 3.7.0 through 3.51.2 when two or more connections on one WAL file
  write or checkpoint at the same instant; fixed in 3.51.3, backports 3.44.6
  and 3.50.7; 3.52.0 withdrawn (sqlite.org/changes.html). SafeForge meets the
  precondition by design (one `ObjectStore` per thread, coordinated stores
  across processes, `event_writer` beside a store). Now:
  - `safeforge.sqlite_runtime.sqlite_wal_reset_status(version_info, source_id)`
    (pure, exact types) and `loaded_sqlite_status(conn=None)`, which reads
    `sqlite_version()` / `sqlite_source_id()` from a live connection instead of
    `sqlite3.sqlite_version_info`.
  - `ObjectStore.sqlite_runtime` reports the library behind the store's
    connection.
  - `connect` / `ObjectStore` take `require_wal_reset_fix` (exact bool).
    `True` refuses an affected library with `WalResetBugUnfixed` (a
    `ConfigurationError`, exported) before any file or directory is touched.
    **Default `False` -- owner decision E2 open**; nothing blocks by default.
  - Release gate `python -m safeforge.sqlite_gate`: exit 1 on an affected
    library, 0 on a fixed one; `SAFEFORGE_ALLOW_UNFIXED_SQLITE=<reason>` waives
    it visibly (verdict `"waived"`, reason printed), a blank reason exits 2.
    CI runs it on every test runner with a waiver that says PR CI is not a
    release, so the log shows each runner's real library.
  Tests: `tests/test_xf_sqlite_wal_reset.py`.

### R28 (2026-10-26): powercut_durable needs every prerequisite

- **Fixed SF-R28-01 (P2, cross-family reviews R28..R31, pinned to 34dfc50)**:
  `powercut_durable` was computed from the three per-ingest syncs and the
  `synchronous` value *requested* at open. It ignored `layout_sync` (a
  `"degraded:"`/`"unsupported:"` parent of root/ still gave True), never synced
  the directory of an external `db_path`, synced the store root before SQLite
  created the database and -wal names in it, and trusted a policy the caller
  could change on the connection, also only for the moment of the status
  COMMIT (SF-R31-01). Now:
  - Every open fsyncs the directory holding the database after the schema
    transaction and reports it in `layout_sync` (both directories of a
    symlinked `db_path`, the alias itself as `"degraded:"`); a raising fsync
    refuses the store with `LayoutNotDurable`, as in SF-R25-01.
  - `powercut_durable` = file, objects/ and staging/ syncs `"ok"` AND a
    non-empty `layout_sync` with every entry `"ok"` AND `synchronous`
    FULL/EXTRA, `journal_mode` WAL (macOS: `fullfsync`) read as the last
    statements inside the status-commit transaction (SQLite refuses to change
    `synchronous` there) AND this ingest's own UPDATE committed 'stored' (not
    a reconciler on another connection).
  - Missing evidence (e.g. a failing PRAGMA read) gives False; it never turns
    a committed ingest into a reported failure. No WAL checkpoint per ACK.
  - Same class, latent: the backup/restore report summarised an empty status
    list as `"ok"`; it is now `"missing:no-sync-status"` (no current caller
    passes an empty list).
  - The name stays `powercut_durable`; it reports software prerequisites, not
    a measured power-cut guarantee (README).
  - README: the upstream SQLite WAL-reset bug (fixed in 3.51.3, backports
    3.44.6 / 3.50.7) is a deployment requirement this library does not check.
  Tests: `tests/test_r28_durability_prerequisites.py`.

### R27 (2026-10-23): a logical ACK is not a power-cut-durable ACK

- **Fixed SF-R27-01 (P1 release scope, cross-family review R27)**: `IngestResult`
  carried only the objects/ directory status; `ingest()` dropped the object file's
  fsync status and the staging/ directory status, so a macOS
  `"degraded:darwin-F_FULLFSYNC-refused-fsync-only"` file sync was invisible.
  New fields `file_sync`, `staging_sync` and `powercut_durable`: True only if all
  three syncs are `"ok"` and SQLite runs `synchronous` FULL or EXTRA. The ACK itself
  is unchanged, so Windows keeps working and never claims power-cut durability; a
  deduplicated result attests nothing about the earlier ingest.
  Tests: `tests/test_r27_powercut_ack.py` (6).

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
