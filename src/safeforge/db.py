"""SQLite connection policy for crash-consistent ingest.

Defaults and why:

* ``journal_mode=WAL`` -- readers do not block the writer and vice versa
  ("mostly true", sqlite.org/wal.html section 9). The mode is VERIFIED after
  setting it: SQLite silently keeps another mode where WAL is impossible
  (``:memory:``), and WAL does not work on network filesystems.
* ``synchronous=FULL`` -- in WAL mode FULL is ACID; NORMAL "might roll back
  following a power loss or system crash" (sqlite.org/pragma.html). An ingest
  that acknowledges an upload must not lose that acknowledgement, so FULL is
  the default. NORMAL is allowed but must be requested explicitly.
* ``busy_timeout`` -- waits for a lock instead of failing at once. It does NOT
  remove SQLITE_BUSY: SQLite skips the busy handler when waiting could
  deadlock (read->write upgrade), and any finite timeout can expire.
* Writes use ``BEGIN IMMEDIATE`` (see :func:`transaction`), which takes the
  write lock up front and so avoids the read->write upgrade failure.
* ``isolation_level=None`` -- the ``sqlite3`` module's implicit transaction
  handling is switched off; every transaction in this package is explicit.
"""

from __future__ import annotations

import sqlite3
import os
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["connect", "transaction", "ConfigurationError"]

_SYNC = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}


class ConfigurationError(RuntimeError):
    """The database could not be put into the required mode."""


class _CoordinatedCursor(sqlite3.Cursor):
    """R9 cooperative gate on cursor statement execution (not a raw SQLite fence)."""

    def _check(self):
        from .coordination import require_held
        require_held(self.connection._guard_root)

    def execute(self, *args, **kwargs):
        self._check()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._check()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._check()
        return super().executescript(*args, **kwargs)


class _CoordinatedConnection(sqlite3.Connection):
    """Validate a held writer lock at each DB-API execute, not only at connect.

    Does NOT replace process isolation: direct sqlite3 and uncoordinated native
    database clients can still bypass this library, as can inherited raw FDs.
    """

    def _check(self):
        from .coordination import require_held
        require_held(self._guard_root)

    def cursor(self, *args, **kwargs):
        self._check()
        kwargs.setdefault('factory', _CoordinatedCursor)
        return super().cursor(*args, **kwargs)

    def execute(self, *args, **kwargs):
        self._check()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._check()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._check()
        return super().executescript(*args, **kwargs)

    def commit(self):
        self._check()
        return super().commit()

    # Rollback, closing and interrupt are allowed without a held lock to
    # facilitate fail-closed cleanup. Native sqlite3.Connection method calls
    # can bypass Python overrides; this is NOT a security sandbox.


def connect(
    path: str,
    *,
    synchronous: str = "FULL",
    busy_timeout_ms: int = 5000,
    coordinated: bool = False,
) -> sqlite3.Connection:
    """Open ``path`` in WAL mode with an explicit-transaction connection."""
    if type(coordinated) is not bool:
        raise TypeError("coordinated must be bool")
    # A new R8 connection must acknowledge the coordinated protocol. This is
    # NOT an enforcement mechanism against old versions or raw sqlite3.connect.
    from .coordination import initialized, require_held, CoordinationRejected
    parent = os.path.dirname(os.path.abspath(path))
    if initialized(parent):
        if not coordinated:
            raise ConfigurationError("coordinated store: use coordination.event_writer / ObjectStore(coordinated=True)")
        if os.path.basename(path) != 'safeforge.db':
            raise ConfigurationError("coordinated database must be root/safeforge.db")
        require_held(parent)
    elif coordinated:
        raise ConfigurationError("coordinated=True requires an initialized root")
    sync = synchronous.upper()
    if sync not in ("NORMAL", "FULL", "EXTRA"):
        raise ValueError("synchronous must be NORMAL, FULL or EXTRA (OFF is not crash-safe)")
    if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool):
        raise TypeError("busy_timeout_ms must be int")
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be >= 0")
    factory = _CoordinatedConnection if coordinated else sqlite3.Connection
    # Disable SQLite statement-cache reuse for coordinated connections: the
    # authorizer is called on prepare, not reliably on every cached execution.
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0,
                           isolation_level=None, factory=factory,
                           cached_statements=0 if coordinated else 128)
    if coordinated:
        conn._guard_root = parent
        def _require_live_context(action, arg1, arg2, database_name, caller):
            try:
                require_held(parent)
            except CoordinationRejected:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        # Defence in depth for direct sqlite3.Connection.execute subclass bypass.
        # A malicious in-process actor can clear this authorizer via the C API;
        # this is NOT OS-enforced mandatory writer fencing.
        conn.set_authorizer(_require_live_context)
    try:
        conn.execute("PRAGMA busy_timeout=%d" % busy_timeout_ms)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise ConfigurationError("journal_mode=WAL not available (got %r)" % (mode,))
        conn.execute("PRAGMA synchronous=%s" % sync)
        got = conn.execute("PRAGMA synchronous").fetchone()[0]
        if got != _SYNC[sync]:
            raise ConfigurationError("synchronous=%s not applied (got %r)" % (sync, got))
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
    """``BEGIN <mode>`` ... ``COMMIT``; ``ROLLBACK`` on any exception."""
    m = mode.upper()
    if m not in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
        raise ValueError("invalid transaction mode %r" % (mode,))
    conn.execute("BEGIN " + m)
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        # A failed COMMIT (e.g. SQLITE_BUSY) leaves the transaction open; do
        # not hand a half-open transaction back to the caller.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
