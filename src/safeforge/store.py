"""Crash-consistent ingest of files plus SQLite metadata (intent / publish / reconcile).

A file on disk and a row in SQLite cannot be changed in ONE atomic step. This
module does not pretend otherwise. It orders the steps so that every crash
leaves a state the reconciler can classify without guessing:

    1. stream into  staging/<id>.part   (same volume as objects/), hash, fsync
    2. COMMIT intent row  status='pending'            -- short IMMEDIATE txn
    3. publish  staging/<id>.part -> objects/<id>     -- atomic rename, no clobber
    4. fsync objects/ and staging/                    -- make the rename durable
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


def _file_matches(path: str, sha256: str, size: int) -> bool:
    try:
        if os.path.getsize(path) != size:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except FileNotFoundError:
        return False
    return h.hexdigest() == sha256


def _young(path: str, cutoff: float) -> bool:
    """True if modified after ``cutoff`` -- or already gone (nothing to delete then)."""
    try:
        return os.path.getmtime(path) > cutoff
    except FileNotFoundError:
        return True


class ObjectStore:
    """One instance per thread (it owns one SQLite connection)."""

    def __init__(
        self,
        root: str,
        *,
        db_path: str | None = None,
        staging_dir: str | None = None,
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5000,
    ) -> None:
        root = os.path.abspath(root)
        self.objects_dir = os.path.join(root, "objects")
        self.staging_dir = os.path.abspath(staging_dir) if staging_dir else os.path.join(root, "staging")
        os.makedirs(self.objects_dir, exist_ok=True)
        os.makedirs(self.staging_dir, exist_ok=True)
        # A rename is only atomic within one filesystem. Refuse a staging area on
        # another volume (e.g. /tmp on tmpfs) up front instead of failing with
        # EXDEV per upload -- or worse, silently degrading to copy+delete.
        durable.require_same_volume(self.staging_dir, self.objects_dir)
        self.db_path = db_path or os.path.join(root, "safeforge.db")
        self._conn = db.connect(self.db_path, synchronous=synchronous, busy_timeout_ms=busy_timeout_ms)
        with db.transaction(self._conn):
            for stmt in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                self._conn.execute(stmt)

    # -- plumbing -----------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

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
        return self._obj(oid)

    def status_of(self, oid: str) -> str | None:
        return self._status(oid)

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
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 0 < len(idempotency_key) <= 256
        ):
            raise ValueError("idempotency_key must be a non-empty str of at most 256 chars")

        if idempotency_key is not None:
            hit = self._by_key(idempotency_key)
            if hit is not None:
                return self._dedup(hit, expected_sha256)

        oid = uuid.uuid4().hex
        spath, opath = self._stg(oid), self._obj(oid)

        # (1) staging: stream, hash, fsync -- no database lock held.
        h = hashlib.sha256()
        size = 0
        try:
            with open(spath, "xb") as f:
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

    def reconcile(self, *, grace_seconds: float = 300.0, now: float | None = None) -> ReconcileReport:
        """Bring disk and database back into agreement. Idempotent.

        ``grace_seconds`` protects in-flight ingests: young 'pending' rows and
        young unreferenced files are left alone. Correctness does NOT depend on
        the grace period (the final UPDATE is conditional); it only avoids
        failing uploads that are merely slow.
        """
        if (
            not isinstance(grace_seconds, (int, float))
            or isinstance(grace_seconds, bool)
            or not math.isfinite(grace_seconds)
            or grace_seconds < 0
        ):
            raise ValueError("grace_seconds must be a finite number >= 0")
        now = time.time() if now is None else now
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
            if not _ID_RE.fullmatch(name) or not os.path.isfile(path):
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
            if not _ID_RE.fullmatch(oid) or not os.path.isfile(path):
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
        for oid, size in self._conn.execute(
            "SELECT id, size FROM objects WHERE status='stored'"
        ).fetchall():
            p = self._obj(oid)
            if not os.path.isfile(p) or os.path.getsize(p) != size:
                rep.missing_or_corrupt.append(oid)
        return rep

    def verify(self) -> list[str]:
        """Full re-hash of every stored object. Returns the ids that do not match."""
        bad = []
        for oid, sha, size in self._conn.execute(
            "SELECT id, sha256, size FROM objects WHERE status='stored'"
        ).fetchall():
            if not _file_matches(self._obj(oid), sha, size):
                bad.append(oid)
        return bad
