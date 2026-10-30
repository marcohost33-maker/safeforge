# safeforge

Reference implementation and test bench for three narrow problems that come up
whenever a service accepts uploads and keeps metadata in SQLite:

1. **Crash-consistent file + SQLite ingest.** A file on disk and a database row
   cannot change in one atomic step. `safeforge.store` orders the steps so that
   any crash leaves a state the reconciler can repair without guessing:
   stage, commit the intent, publish, fsync the directory, then commit the status.
2. **ZIP upload validation.** `safeforge.zipcheck` checks names (traversal,
   absolute paths, backslashes, drive letters and NTFS streams, Windows device
   names), collisions (case and Unicode), entry types (symlinks, devices,
   encrypted entries) and resource limits (size, ratio, overlapping entries),
   and decompresses every entry once to verify CRC and size.
3. **Idempotent events** such as payment webhooks: `safeforge.events` writes
   the event record and its effect in one transaction, guarded by a UNIQUE key,
   instead of checking first and inserting later.

Standard library only (Python ≥ 3.12). Apache-2.0.

**Maturity: research prototype, v0.1.0.** The test suite passes locally on
Windows with Python 3.12 and 3.14. The Linux paths (directory fsync, `EXDEV`,
POSIX rename) run only in CI. Nothing here has been audited externally.

## What this does **not** claim

- **Power loss: simulated in two halves, not proven.** Since R14 the ingest
  protocol is tested against a power cut at every failpoint:
  *data* with [LazyFS](https://github.com/dsrhaslab/lazyfs) (Ramos et al.,
  PVLDB 17(11), 2024), a FUSE file system that drops every unsynced byte,
  SQLite's WAL included (`tests/test_powerloss_lazyfs.py`, Linux, CI job
  `powerloss`); *directory entries* with a model that lets every entry
  created or removed since the directory's last fsync survive or vanish
  independently (`tests/test_powerloss_dirmodel.py`, ALICE-style, POSIX).
  LazyFS persists directory operations at once, so neither half alone is
  enough. Not covered: reordered or torn device writes, drives or bridges that
  ignore flushes, file systems whose fsync semantics differ from the model.
  Negative controls show each half detects a missing step: without the
  staging fsync an acknowledged object comes back empty; without the
  directory fsyncs it disappears; with `synchronous=NORMAL` the acknowledged
  upload is lost entirely.
- **Windows directory durability.** Windows offers Python no directory fsync.
  `publish()` uses `MoveFileExW(MOVEFILE_WRITE_THROUGH)`, which Microsoft
  documents as "does not return until the file is actually moved on the
  disk". Microsoft documents no atomicity guarantee for it. The result reports
  `dir_sync = "unsupported:…"` instead of hiding this, and
  `IngestResult.durability_prerequisites_ok` (formerly `powercut_durable`,
  still readable as a deprecated alias) is True only where every observed
  prerequisite was `"ok"`: the syncs of the ingest (file, objects/, staging/),
  every entry of `ObjectStore.layout_sync` (including the directory that
  holds the database, synced after the database exists), the SQLite
  policy read inside the status-commit transaction (`synchronous` FULL/EXTRA,
  `journal_mode` WAL, on macOS `fullfsync`), and a linked SQLite with the
  WAL-reset fix (`ObjectStore.sqlite_runtime["fixed"] is True`, see below)
  -- never on Windows. An ACK is a
  logical ACK; read that field for the power-cut claim. It reports software
  prerequisites, not a measured power-cut guarantee. The ACK also names the
  evidence it stands on (SF-R39-02): `layout_mount_basis` (what ended the
  ancestor walk; `"st_dev-only"` on macOS/BSD walks through same-device
  mounts and syncs more, never less) and `sqlite_version` /
  `sqlite_wal_reset_fixed` for the library behind the store.
- **SQLite WAL-reset bug (upstream).** sqlite.org documents a rare WAL-reset
  race that can corrupt a WAL database when two or more connections write or
  checkpoint at the same instant; it is fixed in SQLite 3.51.3 and in the
  backports 3.44.6 and 3.50.7 (3.52.0 was withdrawn). One `ObjectStore` per
  thread on the same database, coordinated stores in several processes and
  `coordination.event_writer` next to a store all meet the precondition.
  python.org builds of Python 3.12 (SQLite 3.49.1) and 3.13/3.14 (3.50.4) are
  affected. The check reads the library actually loaded (`sqlite_version()`
  and `sqlite_source_id()` from a live connection), not the Python version:
  - `ObjectStore.sqlite_runtime` reports it for the store's own connection;
  - `connect(..., require_wal_reset_fix=True)` / `ObjectStore(...,
    require_wal_reset_fix=True)` refuse an affected library with
    `WalResetBugUnfixed` (a `ConfigurationError`) before anything is opened or
    created. The default is `False` (decision E2, 2026-10-27): every
    python.org build is affected, so blocking by default would make the
    library unusable there. An affected library still never yields
    `durability_prerequisites_ok = True` (SF-R35-01);
  - release gate: `python -m safeforge.sqlite_gate` exits 1 on an affected
    library. Its default profile `release` accepts **no** waiver (exit 3 if
    one is offered), and a GitHub tag or `release` run is always gated as a
    release. Only `--profile ci` accepts
    `SAFEFORGE_ALLOW_UNFIXED_SQLITE="expires=YYYY-MM-DD; reason=<text>"`,
    at most 120 days ahead; a free-text, malformed or expired waiver exits 2,
    so every CI waiver comes back for a new decision (SF-R39-01). Who may
    write one is decided by review of the workflow file; there is no
    signature check. The decision is by version number: a vendor backport
    without a version bump is reported as affected.
- **macOS needs F_FULLFSYNC.** Apple's fsync(2) flushes to the drive, not
  through its cache, and CPython's `os.fsync` does not issue `F_FULLFSYNC`.
  `durable` issues it (falling back to fsync with status
  `"degraded:darwin-F_FULLFSYNC-refused-fsync-only"`), and `db.connect` sets
  SQLite's `PRAGMA fullfsync=ON` for FULL/EXTRA. Drives or bridges that ignore
  the flush command cannot be detected from software.
- **No content scanning.** A structurally valid archive can still carry
  malicious JavaScript. Serve uploads from a separate origin with a strict CSP.
- **"Exactly once" only inside the database transaction.** External side
  effects such as e-mail or HTTP calls need an outbox.
- **One host.** SQLite WAL does not work on network filesystems.

## Use

```python
import io
from safeforge import ObjectStore, validate_zip, ZipRejected

with ObjectStore("/srv/uploads") as store:          # objects/, staging/, safeforge.db
    validate_zip(upload_file)                        # raises ZipRejected(code=...)
    upload_file.seek(0)
    r = store.ingest(upload_file, max_bytes=50 << 20, idempotency_key=request_id)
    path = store.path_of(r.object_id)                # only for status 'stored'

# periodically, and on start-up:
with ObjectStore("/srv/uploads") as store:
    report = store.reconcile(grace_seconds=300)      # idempotent
```

Protocol, and what a crash at each step leaves behind once reconcile has run:

| step | crash after it leaves | reconcile does |
|---|---|---|
| 1 stream into `staging/<id>.part`, hash, `fsync` | staging file, no row | delete it after the grace period |
| 2 COMMIT row `pending` | pending row, staging file | mark `failed`, delete staging |
| 3 publish `objects/<id>` (same volume, no clobber) | pending row, object | re-hash; if it matches, mark `stored`, otherwise `failed` |
| 4 `fsync` objects/ and staging/ | same as 3 | same as 3 |
| 5 COMMIT `stored` **WHERE status='pending'** | nothing to do | — |

The final UPDATE is conditional, so a writer cannot revive a row that the
reconciler has already failed. The reconciler never deletes the file of a
young `pending` row, and it never deletes files it did not create.

## Results of the falsification (short)

The analysis of "SAFEFORGE V0.3" made 13 technical claims (T1–T13) and 6
legal/economic claims (L1–L6). The V0.3 code itself was not available. The
verdicts below come from our own experiments and primary sources. Numbers are
from Windows 11, Python 3.14.4, SQLite 3.50.4; the JSON in
`experiments/results/` records the clean git HEAD of each run.

| # | Claim | Verdict | Key evidence |
|---|---|---|---|
| T1 | V0.3 canonicalises ZIP paths, requires root index, blocks `../` and reserved names | UNGEPRÜFT | No code. Those checks are necessary but not sufficient. `zipfile` rewrites `\` to `/` on Windows only, so a check on `filename` instead of `orig_filename` gives platform-dependent verdicts. |
| T2 | Valid ZIP ≠ safe content | BESTÄTIGT | Holds by construction. |
| T3 | File + row not atomic, so orphans or dangling rows | BESTÄTIGT | Kill tests: orphan file (file-first), row without file (row-first). |
| T4 | Without WAL: `database is locked` | TEILWEISE | WAL does not remove writer/writer BUSY: 1412 of 1600 `BEGIN IMMEDIATE` attempts got BUSY with WAL and timeout 0. It does remove reader BUSY: 0 in WAL against 335–1942 in DELETE mode. |
| T5 | WAL: unlimited non-blocked readers | TEILWEISE | "Mostly true" (sqlite.org/wal.html §9). Exclusive locking mode blocks readers (reproduced). Further limits: checkpoint starvation, same host only. |
| T6 | `busy_timeout=5000` eliminates errors completely | **WIDERLEGT** | A DEFERRED read→write upgrade gets BUSY in <1 s despite 5000 ms. With WAL, 5000 ms and DEFERRED, 1401 of 1600 read-modify-write transactions failed. |
| T7 | `synchronous=NORMAL` + WAL stays crash-tolerant | TEILWEISE | Consistent yes, durable across power loss no (pragma.html). The kill test lost 0 acknowledged commits in both modes. Under a simulated power cut (LazyFS, R14) NORMAL lost an acknowledged upload (both commits gone, the object then reconciled away as an orphan); FULL lost none at any failpoint. WAL+FULL ran 327 commits/s against 13 842 for NORMAL. |
| T8 | `mmap_size=256MB` lowers CPU dramatically | TEILWEISE | Read-only, warm cache: CPU ×0.533 (lookups), ×0.285 (scans). The source warns gains are not guaranteed and lists risks. Linux not measured. |
| T9 | `BEGIN IMMEDIATE` avoids the upgrade deadlock | BESTÄTIGT (limit) | Real deadlock with DEFERRED in DELETE mode, reproduced. IMMEDIATE: 1600 of 1600 commits, 0 errors (WAL, 5000 ms). It does not prevent timeouts. |
| T10 | Reconciliation pattern guarantees absolute integrity | **WIDERLEGT** / corrected | The uncommitted `pending` row cannot survive a crash, so staging leaks. A concurrent reconciler deletes a renamed file just before the commit, leaving a committed row without a file. fsyncs are missing. `/tmp` staging gives `EXDEV`. The lock is held during file I/O. The corrected protocol holds every invariant at all 8 crash points and against a concurrent reconciler at each point. |
| T11 | clamd INSTREAM never touches disk; <100 ms | **WIDERLEGT** / UNGEPRÜFT | clamd writes INSTREAM data to a temp file in `TemporaryDirectory` (`clamd/session.c` L639, `server-th.c` L886). Latency not measured. |
| T12 | FastAPI `lifespan`; process pool for CPU work | BESTÄTIGT | FastAPI docs mark startup/shutdown events "deprecated". Not measured. |
| T13 | Unique transaction ids give exactly-once licences | TEILWEISE | Check-then-insert issued 2 licences for 1 event. UNIQUE + ON CONFLICT in one IMMEDIATE transaction issued exactly 1 across 8 concurrent deliveries. Holds only inside the DB transaction. |
| L1 | UWG 3 I s imprint duty | BESTÄTIGT | Fedlex SR 241. The provision also covers order steps, error correction and order confirmation. |
| L2 | OR 931 (100k) / OR 957 (500k, "double-entry") | TEILWEISE | Thresholds correct. The law says "Buchführung und Rechnungslegung", not "doppelte". |
| L3 | nDSG Art. 7; 10-year retention | TEILWEISE | Art. 7 correct since 1.9.2023. The 10 years come from **OR 958f**, not the nDSG. |
| L4 | CRA reporting from 11.09.2026, 24 h / 72 h, 5 years | TEILWEISE | Dates and deadlines match Reg. (EU) 2024/2847 Art. 14 and 71(2); 5 years is a *minimum* (Art. 13(8)). Whether the duties apply to a marketplace (manufacturer or distributor role) is open. |
| L5 | DAC7 30 sales / EUR 2 000; MoR 5 % + 0.50 | TEILWEISE | The threshold is for sale of **Goods = tangible property**, so it does not fit software. Paddle shows 5% + 50¢ (USD). |
| L6 | Contribution-margin table | UNGEPRÜFT | No data in the source. |

Primary sources are listed under [Sources](#sources); the exact quotes are in
the docstrings of the corresponding tests and modules.

## Reproduce

```bash
python -m pip install pytest
python -m pytest                     # full suite, includes one ~5 s "slow" test
python -m pytest -m "not slow"       # iteration
python experiments/exp_sqlite_contention.py   # T4/T5/T6/T9 matrix (~40 s)
python experiments/exp_sync_cost.py           # T7 cost side
python experiments/exp_mmap.py                # T8

# power cut, data half (Linux, FUSE 3; builds the pinned LazyFS):
experiments/lazyfs/build_lazyfs.sh /tmp/lazyfs-build
SAFEFORGE_LAZYFS_BIN=/tmp/lazyfs-build/lazyfs/lazyfs/build/lazyfs \
    python -m pytest tests/test_powerloss_lazyfs.py
```

Fault injection uses named failpoints (`safeforge._faults.point`). They can be
armed only from code running inside the process; there is no environment
switch. `tests/crash_driver.py` ends the child process with `os._exit` at a
chosen point, and `tests/invariants.py` checks disk and database without
importing safeforge.

## Layout

```
src/safeforge/   store.py  durable.py  db.py  zipcheck.py  events.py  _faults.py
tests/           test_fault_injection.py  test_sqlite_semantics.py  test_sqlite_durability.py
                 test_store.py  test_zipcheck.py  test_events.py
                 test_powerloss_lazyfs.py  test_powerloss_dirmodel.py  (+ powerloss_driver/_model, lazyfs_harness)
                 designs/ (naive + reconstructed V0.3 pattern, negative controls only)
experiments/     contention, sync cost, mmap + results/*.json; lazyfs/build_lazyfs.sh
```

## Sources

All retrieved 2026-10-19.

- SQLite: [wal.html](https://www.sqlite.org/wal.html) (§1, §2.3, §6, §9), [pragma.html](https://www.sqlite.org/pragma.html) (synchronous, busy_timeout, mmap_size), [lang_transaction.html](https://www.sqlite.org/lang_transaction.html), [c3ref/busy_handler.html](https://www.sqlite.org/c3ref/busy_handler.html), [mmap.html](https://www.sqlite.org/mmap.html)
- CPython at commit `02844c8`: `Doc/library/os.rst` (`os.replace`) and `Modules/posixmodule.c` (`MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`)
- Microsoft Learn: [MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)
- ClamAV at commit [`72cd48c`](https://github.com/Cisco-Talos/clamav/tree/72cd48c9faed4fa4afc22bc4ed0b9b19f8d3f8f7): `clamd/session.c` L638-646, `clamd/server-th.c` L886-889
- FastAPI: [Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)
- Fedlex: SR 241 (UWG, consolidation 2025-01-01), SR 220 (OR, 2026-10-01), SR 235.1 (DSG, 2025-07-07), from <https://www.fedlex.admin.ch/>
- EUR-Lex: CELEX 32024R2847 (Cyber Resilience Act), CELEX 32021L0514 (DAC7)
- [Paddle pricing](https://www.paddle.com/pricing)
