"""Crash-consistent ingest of files plus SQLite metadata (intent / publish / reconcile).

A file on disk and a row in SQLite cannot be changed in ONE atomic step. This
module does not pretend otherwise. It orders the steps so that every crash
leaves a state the reconciler can classify without guessing:

    1. stream into  staging/<id>.part   (same volume as objects/), hash, fsync
    2. COMMIT intent row  status='pending'            -- short IMMEDIATE txn
    3. publish via exclusive link, then unlink stage -- no-clobber, two operations (POSIX)
    4. fsync objects/ and staging/                    -- persist both directory changes
    5. COMMIT  status='stored'  WHERE status='pending' -- conditional, short txn
    6. only now report success to the caller

Crash anywhere -> after :meth:`ObjectStore.reconcile`:

    before 2   staging file without row  -> deleted after the grace period
    after 2    pending row, no object    -> row 'failed', staging deleted
    after 3/4  pending row, object       -> object re-hashed; matches -> 'stored'
                                            (roll forward), else 'failed'
    after 5    nothing to do

Why this differs from the "reconciliation pattern" it replaces (see
docs/FINDINGS.md, T10): there the 'pending' row was written INSIDE the same
uncommitted transaction as the final status. After a crash such a row does not
exist, so the reconciler's "pending older than 5 min" rule can never fire; and a
reconciler running concurrently sees an already-renamed file without a row and
deletes it a moment before the writer commits -- a committed row that points at
nothing. Here the intent is committed first, the final UPDATE is conditional,
and the reconciler never deletes a file whose row is still a young 'pending'.

The write lock is held only for the two short metadata transactions, never
during file I/O.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass, field
from typing import BinaryIO

from . import _faults, db, durable

__all__ = [
    "ObjectStore",
    "IngestResult",
    "ReconcileReport",
    "IngestError",
    "TooLarge",
    "DigestMismatch",
    "IngestInFlight",
    "IngestAborted",
    "IdempotencyConflict",
    "LayoutNotDurable",
]

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PART = ".part"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL CHECK (status IN ('pending', 'stored', 'failed')),
    sha256           TEXT NOT NULL,
    size             INTEGER NOT NULL CHECK (size >= 0),
    idempotency_key  TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS objects_idem
    ON objects(idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status != 'failed';
CREATE INDEX IF NOT EXISTS objects_status ON objects(status, created_at);
"""


class IngestError(Exception):
    """Base class. The staged data was not stored."""


class TooLarge(IngestError):
    """The stream exceeded ``max_bytes``."""


class DigestMismatch(IngestError):
    """The stream's SHA-256 differs from ``expected_sha256``."""


class IngestInFlight(IngestError):
    """Another ingest with the same idempotency key has not finished yet."""


class IngestAborted(IngestError):
    """The reconciler declared this ingest failed before it could finish."""


class IdempotencyConflict(IngestError):
    """The idempotency key was already used for different content."""


class LayoutNotDurable(IngestError):
    """A store directory's entry could not be fsync'ed in its parent; the store does not open.

    SF-R25-01: an opened store can acknowledge ingests, so it must not exist
    while root/, objects/ or staging/ may vanish in a power cut. ``layout_sync``
    keeps every parent's status, the failed ones as ``"failed:<error>"``.
    """

    def __init__(self, message: str, layout_sync: dict[str, str]) -> None:
        super().__init__(message)
        self.layout_sync = dict(layout_sync)


@dataclass(frozen=True)
class IngestResult:
    object_id: str
    sha256: str
    size: int
    deduplicated: bool
    publish: str = ""
    dir_sync: str = ""


@dataclass
class ReconcileReport:
    rolled_forward: int = 0
    marked_failed: int = 0
    deleted_objects: int = 0
    deleted_staging: int = 0
    missing_or_corrupt: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        """Number of state changes made. A second pass right after must return 0."""
        return self.rolled_forward + self.marked_failed + self.deleted_objects + self.deleted_staging


def _bounded_digest(stream: BinaryIO, max_bytes: int, chunk_size: int) -> tuple[str, int]:
    """Consume a replay stream once; enforce exactly the same size cap as staging.

    A reused idempotency key is not evidence that its NEW request body matches
    the old body. Reading/hashing is deliberate: fast replay with an
    unverified body could silently acknowledge unrelated content.
    """
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("stream.read() must return bytes")
        size += len(chunk)
        if size > max_bytes:
            raise TooLarge("stream exceeds max_bytes=%d" % max_bytes)
        digest.update(chunk)
    return digest.hexdigest(), size


def _open_regular(path: str):
    """Open a regular file, refusing final-component symlinks where supported.

    Compare the descriptor identity to lstat both before and after opening.
    This is stronger than islink()+open(), but parent directories must remain
    private/trusted. On Windows O_NOFOLLOW is unavailable; identity checks are
    defensive, not a full reparse-point isolation guarantee.
    """
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("object is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        after = os.lstat(path)

        def sig(st: os.stat_result) -> tuple[int, int]:
            return (st.st_dev, st.st_ino)

        if not stat.S_ISREG(opened.st_mode) or sig(before) != sig(opened) or sig(after) != sig(opened):
            raise OSError("object changed while opening")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _file_matches(path: str, sha256: str, size: int) -> bool:
    try:
        with _open_regular(path) as f:
            if os.fstat(f.fileno()).st_size != size:
                return False
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
            # Catch a path swap or in-place size mutation during hashing.
            opened, after = os.fstat(f.fileno()), os.lstat(path)
            return (
                stat.S_ISREG(after.st_mode)
                and (opened.st_dev, opened.st_ino) == (after.st_dev, after.st_ino)
                and opened.st_size == size
                and h.hexdigest() == sha256
            )
    except (OSError, ValueError):
        return False


def _missing_dirs(path: str) -> list[str]:
    """``path`` and its ancestors that do not exist yet (what makedirs will create)."""
    out = []
    path = os.path.abspath(path)
    while not os.path.lexists(path):
        out.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return out


def _sync_parents(dirs: list[str]) -> dict[str, str]:
    """fsync the parent of every directory in ``dirs``; report, never hide, a failure.

    Returns ``{parent: status}`` with the statuses of :func:`durable.fsync_dir`
    plus ``"failed:<error>"`` when it raises (e.g. a parent with execute but
    no read permission, or EIO). Every parent is attempted, so the report is
    complete; the caller decides -- :class:`ObjectStore` refuses to open on any
    ``"failed:"`` (SF-R25-01).
    """
    out: dict[str, str] = {}
    for parent in sorted({os.path.dirname(d) for d in dirs}, key=len, reverse=True):
        try:
            out[parent] = durable.fsync_dir(parent)
        except OSError as e:
            out[parent] = "failed:%s" % (e.strerror or e.__class__.__name__)
    return out


def _young(path: str, cutoff: float) -> bool:
    """True if modified after ``cutoff`` -- or already gone (nothing to delete then)."""
    try:
        return os.path.getmtime(path) > cutoff
    except FileNotFoundError:
        return True


def _check_private_path(path: str, *, directory: bool) -> None:
    """Inspect the final path without following a symlink; fail closed on POSIX.

    This is a *deployment preflight*, not a hostile-parent-directory defence.
    Parent directories and mount configuration must be controlled separately.
    """
    st = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise PermissionError("private storage gate failed: %s" % path)


class ObjectStore:
    """One instance per thread (it owns one SQLite connection).

    ``synchronous`` must stay FULL (the default) for an acknowledged ingest to
    survive a power cut: under NORMAL the WAL is not synced at commit, and the
    LazyFS test shows an acknowledged upload lost entirely
    (tests/test_powerloss_lazyfs.py). NORMAL survives process crashes only.
    ``layout_sync`` reports the fsyncs of the parents of root/, objects/,
    staging/ and of every directory this instance created (SF-R14-01); a
    failed one raises :class:`LayoutNotDurable` instead (SF-R25-01).
    """

    def __init__(
        self,
        root: str,
        *,
        db_path: str | None = None,
        staging_dir: str | None = None,
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5000,
        enforce_private: bool = False,
        coordinated: bool = False,
    ) -> None:
        if type(coordinated) is not bool:
            raise ValueError("coordinated must be bool")
        if type(enforce_private) is not bool:
            raise ValueError("enforce_private must be bool")
        if enforce_private and os.name != "posix":
            raise ValueError("enforce_private requires POSIX permission semantics")
        root = os.path.abspath(root)
        from . import coordination

        if coordination.initialized(root) != coordinated:
            raise IngestError("coordination mode mismatch; migrate/opt in explicitly")
        if coordinated:
            # The offline backup snapshots ONLY root/safeforge.db and root/staging.
            # Arbitrary auxiliary paths would silently escape its inventory.
            if db_path is not None and os.path.abspath(db_path) != os.path.join(root, "safeforge.db"):
                raise IngestError("coordinated mode requires database inside store root")
            if staging_dir is not None and os.path.abspath(staging_dir) != os.path.join(root, "staging"):
                raise IngestError("coordinated mode requires canonical staging directory")
        self._coordinated_lock = None
        if coordinated:
            # A live, long-lived store holds SH across *all* writes and
            # reconciliation; EX offline backup fails until it is closed.
            self._coordinated_lock = coordination.lock(root, exclusive=False)
            self._coordinated_lock.__enter__()
        try:
            # R7: prevent an ordinary ObjectStore from using a failed or interrupted
            # restore root; external clients bypassing this API are NOT protected.
            if os.path.lexists(os.path.join(root, "RESTORE_IN_PROGRESS")):
                raise IngestError("restore root is incomplete; do not open")
            created = _missing_dirs(root)
            os.makedirs(root, mode=0o700, exist_ok=True)
            if enforce_private:
                # Reject an already-accessible root BEFORE touching child paths.
                _check_private_path(root, directory=True)
            self.objects_dir = os.path.join(root, "objects")
            self.staging_dir = os.path.abspath(staging_dir) if staging_dir else os.path.join(root, "staging")
            if enforce_private:
                if os.path.dirname(self.staging_dir) != root:
                    raise ValueError("enforce_private requires staging directly under root")
                for directory in (self.objects_dir, self.staging_dir):
                    if os.path.lexists(directory):
                        _check_private_path(directory, directory=True)
            created += _missing_dirs(self.objects_dir) + _missing_dirs(self.staging_dir)
            os.makedirs(self.objects_dir, mode=0o700, exist_ok=True)
            os.makedirs(self.staging_dir, mode=0o700, exist_ok=True)
            # SF-R14-01: a new directory's entry lives in its parent. Without
            # this a power cut can drop objects/ (or root itself) together with
            # every object acknowledged inside it; publish() only syncs
            # objects/ and staging/, never their parents.
            # SF-R25-01: a failure must stop the store, not just be recorded --
            # an open store acknowledges ingests and idempotency replays. And
            # the parents are re-synced on EVERY open, not only when this call
            # created something: after a failed or crashed earlier open the
            # layout exists (created == []) but was never proven durable. The
            # extra fsyncs are idempotent and cheap. Platform statuses
            # ("unsupported:" on Windows, "degraded:" on macOS) pass, as in
            # ingest(), where only a raising fsync_dir prevents the ACK.
            self.layout_sync = _sync_parents(created + [root, self.objects_dir, self.staging_dir])
            failed = sorted(p for p, s in self.layout_sync.items() if s.startswith("failed:"))
            if failed:
                raise LayoutNotDurable(
                    "store layout not durable: directory fsync failed for %s" % ", ".join(failed),
                    self.layout_sync,
                )
            # Publication must stay on one filesystem. Refuse a staging area on
            # another volume (e.g. /tmp on tmpfs) up front instead of failing with
            # EXDEV per upload -- or worse, silently degrading to copy+delete.
            durable.require_same_volume(self.staging_dir, self.objects_dir)
            self.db_path = db_path or os.path.join(root, "safeforge.db")
            if enforce_private:
                if os.path.abspath(os.path.dirname(self.db_path)) != root:
                    raise ValueError("enforce_private requires the SQLite database directly under root")
                for directory in (root, self.objects_dir, self.staging_dir):
                    _check_private_path(directory, directory=True)
                if not os.path.lexists(self.db_path):
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(self.db_path, flags, 0o600)
                    os.close(fd)
                _check_private_path(self.db_path, directory=False)
            self._conn = db.connect(
                self.db_path, synchronous=synchronous, busy_timeout_ms=busy_timeout_ms, coordinated=coordinated
            )
            with db.transaction(self._conn):
                for stmt in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                    self._conn.execute(stmt)

        except BaseException:
            if hasattr(self, "_conn"):
                self._conn.close()
            if self._coordinated_lock is not None:
                self._coordinated_lock.__exit__(None, None, None)
                self._coordinated_lock = None
            raise

    # -- plumbing -----------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            if self._coordinated_lock is not None:
                self._coordinated_lock.__exit__(None, None, None)
                self._coordinated_lock = None

    def __enter__(self) -> ObjectStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _obj(self, oid: str) -> str:
        return os.path.join(self.objects_dir, oid)

    def _stg(self, oid: str) -> str:
        return os.path.join(self.staging_dir, oid + _PART)

    def _status(self, oid: str) -> str | None:
        row = self._conn.execute("SELECT status FROM objects WHERE id=?", (oid,)).fetchone()
        return row[0] if row else None

    def _by_key(self, key: str) -> tuple[str, str, str, int] | None:
        return self._conn.execute(
            "SELECT id, status, sha256, size FROM objects WHERE idempotency_key=? AND status != 'failed'",
            (key,),
        ).fetchone()

    @staticmethod
    def _unlink(path: str) -> bool:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False

    # -- public API ---------------------------------------------------------
    def path_of(self, oid: str) -> str:
        """Path of a STORED object. Raises KeyError for anything else."""
        if not isinstance(oid, str) or not _ID_RE.fullmatch(oid) or self._status(oid) != "stored":
            raise KeyError(oid)
        path = self._obj(oid)
        # Path exposure does not imply content integrity: call verify() for that.
        if os.path.islink(path) or not os.path.isfile(path):
            raise KeyError(oid)
        return path

    def status_of(self, oid: str) -> str | None:
        return self._status(oid)

    def open_verified(self, oid: str):
        """Return a binary file handle after SHA-256 verification.

        Unlike path_of(), the caller reads from the same opened descriptor that
        was verified. Do not grant other processes write access to object files:
        the API cannot stop mutation of the same inode after verification.
        The caller must close the returned handle.
        """
        if not isinstance(oid, str) or not _ID_RE.fullmatch(oid):
            raise KeyError(oid)
        row = self._conn.execute("SELECT sha256, size FROM objects WHERE id=? AND status='stored'", (oid,)).fetchone()
        if row is None:
            raise KeyError(oid)
        try:
            f = _open_regular(self._obj(oid))
        except OSError as exc:
            raise IngestError("stored object cannot be opened safely") from exc
        try:
            sha, size = row
            if os.fstat(f.fileno()).st_size != size:
                raise IngestError("stored object has unexpected size")
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
            opened, after = os.fstat(f.fileno()), os.lstat(self._obj(oid))
            if (
                (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(after.st_mode)
                or opened.st_size != size
                or h.hexdigest() != sha
            ):
                raise IngestError("stored object digest or identity mismatch")
            f.seek(0)
            return f
        except BaseException:
            f.close()
            raise

    def ingest(
        self,
        stream: BinaryIO,
        *,
        max_bytes: int,
        expected_sha256: str | None = None,
        idempotency_key: str | None = None,
        chunk_size: int = 1 << 20,
    ) -> IngestResult:
        """Store ``stream``. Returns only after the 'stored' row is committed."""
        if not hasattr(stream, "read"):
            raise TypeError("stream must have .read()")
        for name, v in (("max_bytes", max_bytes), ("chunk_size", chunk_size)):
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ValueError("%s must be a positive int" % name)
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not _SHA_RE.fullmatch(expected_sha256)):
            raise ValueError("expected_sha256 must be 64 lowercase hex chars")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 0 < len(idempotency_key) <= 256):
            raise ValueError("idempotency_key must be a non-empty str of at most 256 chars")

        if idempotency_key is not None:
            hit = self._by_key(idempotency_key)
            if hit is not None:
                # Pending replays must not steal or consume an in-flight upload.
                if hit[1] != "stored":
                    raise IngestInFlight("ingest for this idempotency key is still pending")
                if hit[3] > max_bytes:
                    raise TooLarge("existing object exceeds max_bytes=%d" % max_bytes)
                # Never trust a reused idempotency key as proof of identical bytes.
                observed_sha, observed_size = _bounded_digest(stream, max_bytes, chunk_size)
                if expected_sha256 is not None and observed_sha != expected_sha256:
                    raise DigestMismatch("replayed stream differs from expected_sha256")
                if observed_size != hit[3]:
                    raise IdempotencyConflict("idempotency key reused for different content size")
                return self._dedup(hit, observed_sha)

        oid = uuid.uuid4().hex
        spath, opath = self._stg(oid), self._obj(oid)

        # (1) staging: stream, hash, fsync -- no database lock held.
        h = hashlib.sha256()
        size = 0
        try:
            # Restrict newly staged object bytes irrespective of process umask.
            stage_fd = os.open(spath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(stage_fd, "wb") as f:
                _faults.point("staging.opened")
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise TypeError("stream.read() must return bytes")
                    size += len(chunk)
                    if size > max_bytes:
                        raise TooLarge("stream exceeds max_bytes=%d" % max_bytes)
                    h.update(chunk)
                    f.write(chunk)
                    _faults.point("staging.chunk")
                _faults.point("staging.written")
                durable.fsync_file(f)
            _faults.point("staging.fsynced")
            digest = h.hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise DigestMismatch("sha256 %s != expected %s" % (digest, expected_sha256))
        except BaseException:
            self._unlink(spath)
            raise

        # (2) intent: committed BEFORE the file becomes visible under its final name.
        now = time.time()
        try:
            with db.transaction(self._conn):
                self._conn.execute(
                    "INSERT INTO objects(id, status, sha256, size, idempotency_key, created_at, updated_at)"
                    " VALUES (?, 'pending', ?, ?, ?, ?, ?)",
                    (oid, digest, size, idempotency_key, now, now),
                )
        except sqlite3.IntegrityError:
            self._unlink(spath)
            hit = self._by_key(idempotency_key) if idempotency_key is not None else None
            if hit is None:
                raise
            return self._dedup(hit, digest)
        except BaseException:
            self._unlink(spath)
            raise
        _faults.point("intent.committed")

        try:
            # (3) publish + (4) directory fsync
            mech = durable.publish(spath, opath)
            _faults.point("object.published")
            dsync = durable.fsync_dir(self.objects_dir)
            durable.fsync_dir(self.staging_dir)
            _faults.point("dirs.fsynced")
            # (5) conditional status commit
            with db.transaction(self._conn):
                cur = self._conn.execute(
                    "UPDATE objects SET status='stored', updated_at=? WHERE id=? AND status='pending'",
                    (time.time(), oid),
                )
                if cur.rowcount != 1:
                    final = self._status(oid)
                    if final != "stored":
                        raise IngestAborted("ingest %s was marked %r by the reconciler" % (oid, final))
            _faults.point("status.committed")
        except BaseException:
            self._abort(oid)
            raise
        return IngestResult(oid, digest, size, False, mech, dsync)

    def _dedup(self, hit: tuple[str, str, str, int], digest: str | None) -> IngestResult:
        oid, status, sha, size = hit
        if digest is not None and digest != sha:
            raise IdempotencyConflict("idempotency key already used for different content")
        if status != "stored":
            raise IngestInFlight("ingest for this idempotency key is still pending")
        if not _file_matches(self._obj(oid), sha, size):
            raise IngestError("cannot acknowledge a missing or corrupted stored object")
        return IngestResult(oid, sha, size, True)

    def _abort(self, oid: str) -> None:
        """Best effort after a failure past the intent commit. Never deletes a stored object."""
        try:
            with db.transaction(self._conn):
                self._conn.execute(
                    "UPDATE objects SET status='failed', updated_at=? WHERE id=? AND status='pending'",
                    (time.time(), oid),
                )
            if self._status(oid) == "failed":
                self._unlink(self._obj(oid))
                self._unlink(self._stg(oid))
        except Exception:  # noqa: BLE001 -- the original error is what the caller must see
            pass  # the reconciler resolves whatever is left

    def reconcile(
        self,
        *,
        grace_seconds: float = 300.0,
        now: float | None = None,
        verify_stored_digests: bool = False,
    ) -> ReconcileReport:
        """Bring disk and database back into agreement. Idempotent.

        ``grace_seconds`` protects in-flight ingests: young 'pending' rows and
        young unreferenced files are left alone. Correctness does NOT depend on
        the grace period (the final UPDATE is conditional); it only avoids
        failing uploads that are merely slow.

        By default stored objects get a cheap existence + size check. Set
        verify_stored_digests=True for an O(total stored bytes) SHA-256 audit.
        """
        if (
            not isinstance(grace_seconds, (int, float))
            or isinstance(grace_seconds, bool)
            or not math.isfinite(grace_seconds)
            or grace_seconds < 0
        ):
            raise ValueError("grace_seconds must be a finite number >= 0")
        if type(verify_stored_digests) is not bool:
            raise ValueError("verify_stored_digests must be bool")
        now = time.time() if now is None else now
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("now must be a finite numeric timestamp")
        cutoff = now - grace_seconds
        rep = ReconcileReport()

        # (a) stale 'pending' rows: roll forward if the object is complete, else fail.
        stale = self._conn.execute(
            "SELECT id, sha256, size FROM objects WHERE status='pending' AND created_at <= ?", (cutoff,)
        ).fetchall()
        for oid, sha, size in stale:
            if _file_matches(self._obj(oid), sha, size):
                durable.fsync_dir(self.objects_dir)  # the crashed writer may not have
                with db.transaction(self._conn):
                    cur = self._conn.execute(
                        "UPDATE objects SET status='stored', updated_at=? WHERE id=? AND status='pending'",
                        (now, oid),
                    )
                rep.rolled_forward += cur.rowcount
            else:
                with db.transaction(self._conn):
                    cur = self._conn.execute(
                        "UPDATE objects SET status='failed', updated_at=? WHERE id=? AND status='pending'",
                        (now, oid),
                    )
                rep.marked_failed += cur.rowcount
        _faults.point("reconcile.pending_resolved")

        # (b) objects/: delete files of failed rows and old unreferenced files.
        for name in sorted(os.listdir(self.objects_dir)):
            path = os.path.join(self.objects_dir, name)
            if not _ID_RE.fullmatch(name) or os.path.islink(path) or not os.path.isfile(path):
                rep.foreign.append(path)  # never delete what we did not create
                continue
            st = self._status(name)
            if st in ("stored", "pending"):
                continue
            if st is None and _young(path, cutoff):
                continue
            try:
                rep.deleted_objects += self._unlink(path)
            except OSError as e:  # e.g. Windows sharing violation
                rep.skipped.append("%s: %s" % (path, e))

        # (c) staging/: delete leftovers of failed/finished ingests and old orphans.
        for name in sorted(os.listdir(self.staging_dir)):
            path = os.path.join(self.staging_dir, name)
            oid = name[: -len(_PART)] if name.endswith(_PART) else ""
            if not _ID_RE.fullmatch(oid) or os.path.islink(path) or not os.path.isfile(path):
                rep.foreign.append(path)
                continue
            st = self._status(oid)
            if st == "pending":
                continue
            if st is None and _young(path, cutoff):
                continue  # an ingest may still be streaming into it
            try:
                rep.deleted_staging += self._unlink(path)
            except OSError as e:
                rep.skipped.append("%s: %s" % (path, e))

        # (d) stored rows whose file is gone or wrong: cannot be repaired, only reported.
        for oid, size in self._conn.execute("SELECT id, size FROM objects WHERE status='stored'").fetchall():
            p = self._obj(oid)
            if os.path.islink(p) or not os.path.isfile(p) or os.path.getsize(p) != size:
                rep.missing_or_corrupt.append(oid)
            elif verify_stored_digests:
                row = self._conn.execute("SELECT sha256 FROM objects WHERE id=?", (oid,)).fetchone()
                if row is not None and not _file_matches(p, row[0], size):
                    rep.missing_or_corrupt.append(oid)
        return rep

    def verify(self) -> list[str]:
        """Full re-hash of every stored object. Returns the ids that do not match."""
        bad = []
        for oid, sha, size in self._conn.execute("SELECT id, sha256, size FROM objects WHERE status='stored'").fetchall():
            if not _file_matches(self._obj(oid), sha, size):
                bad.append(oid)
        return bad
