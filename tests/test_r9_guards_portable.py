"""R9 library guards, exercised on every platform.

test_r9_context_attestation.py runs only on Linux because it needs a real
flock. The guards SF-R9-01/02/03/05/06 themselves are plain Python/SQLite
logic over the process-local lock registry. Here that registry entry is set
directly (``_simulated_hold``), so the guards can be checked on Windows and
macOS too. WHITE-BOX: this replaces only the flock bookkeeping, not the
lock; it says nothing about flock semantics (those stay Linux-tested).
SF-R9-04 (marker O_NOFOLLOW/owner/mode/link count) needs POSIX and is not
covered here.
"""

from __future__ import annotations

import io
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from safeforge import ConfigurationError, IngestError, ObjectStore, connect, coordination
from safeforge.coordination import MARKER_CONTENT, MODE, CoordinationRejected, require_held


def _marked_root(tmp_path) -> Path:
    root = tmp_path / "root"
    with ObjectStore(str(root)) as s:
        s.ingest(io.BytesIO(b"123"), max_bytes=10)
    (root / MODE).write_bytes(MARKER_CONTENT)
    return root


def _rejected_by(exc_type, needle, fn, *args, **kwargs):
    """Assert WHICH layer rejected, not only that something raised.

    The guards are layered (re-check, then SQLite authorizer, then flock), so a
    removed layer usually still ends in *some* exception from the next one.
    A plain pytest.raises would then fail by an escaping foreign exception;
    this turns "the wrong layer rejected" into an explicit assertion.
    """
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the type is what we assert on
        assert type(exc) is exc_type, "rejected by %s(%s), expected %s" % (type(exc).__name__, exc, exc_type.__name__)
        assert needle in str(exc), str(exc)
        return
    raise AssertionError("not rejected at all; expected %s" % exc_type.__name__)


@contextmanager
def _simulated_hold(root):
    """Registry bookkeeping exactly as coordination.lock() does, without flock."""
    key = str(Path(root).absolute())
    contexts = coordination._contexts()
    contexts[key] = contexts.get(key, 0) + 1
    try:
        yield
    finally:
        contexts[key] -= 1
        if not contexts[key]:
            del contexts[key]


def test_r9_01_coordinated_connect_needs_live_registry_entry(tmp_path):
    root = _marked_root(tmp_path)
    with pytest.raises(CoordinationRejected, match="live lock"):
        connect(str(root / "safeforge.db"), coordinated=True)
    # Positive control: the same call succeeds while held.
    with _simulated_hold(root):
        con = connect(str(root / "safeforge.db"), coordinated=True)
        try:
            assert con.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        finally:
            con.close()
    with pytest.raises(CoordinationRejected, match="live lock"):
        connect(str(root / "safeforge.db"), coordinated=True)


def test_r9_02_connection_and_cursor_recheck_after_release(tmp_path):
    root = _marked_root(tmp_path)
    with _simulated_hold(root):
        con = connect(str(root / "safeforge.db"), coordinated=True)
        cur = con.cursor()
        assert cur.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
    try:
        # Must be the Python re-check, not the authorizer backstop behind it.
        _rejected_by(CoordinationRejected, "live lock", con.execute, "UPDATE objects SET updated_at=updated_at+1")
        _rejected_by(CoordinationRejected, "live lock", cur.execute, "UPDATE objects SET updated_at=updated_at+1")
        _rejected_by(CoordinationRejected, "live lock", con.cursor)
        _rejected_by(CoordinationRejected, "live lock", con.commit)
    finally:
        con.close()


def test_r9_03_authorizer_denies_native_execute_after_release(tmp_path):
    root = _marked_root(tmp_path)
    sql = "SELECT count(*) FROM objects"
    with _simulated_hold(root):
        con = connect(str(root / "safeforge.db"), coordinated=True)
        # Prepared while held: a statement cache would let it run again later.
        assert sqlite3.Connection.execute(con, sql).fetchone()[0] == 1
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            sqlite3.Connection.execute(con, sql).fetchone()
        with pytest.raises(sqlite3.DatabaseError):
            sqlite3.Connection.execute(con, "UPDATE objects SET updated_at=updated_at+1")
    finally:
        con.close()


def test_r9_05_coordinated_store_and_connect_refuse_noncanonical_paths(tmp_path):
    root = _marked_root(tmp_path)
    # These checks run before the lock is taken, so they hold on every platform.
    # Must be the path guard itself, not a later lock/marker/connect failure.
    _rejected_by(IngestError, "database inside", ObjectStore, str(root), coordinated=True, db_path=str(tmp_path / "external.db"))
    _rejected_by(IngestError, "staging", ObjectStore, str(root), coordinated=True, staging_dir=str(tmp_path / "external-staging"))
    assert not (tmp_path / "external.db").exists()
    assert not (tmp_path / "external-staging").exists()
    with _simulated_hold(root):
        with pytest.raises(ConfigurationError, match="root/safeforge.db"):
            connect(str(root / "another.db"), coordinated=True)
    assert not (root / "another.db").exists()


def test_r9_06_registry_from_another_pid_is_discarded(tmp_path, monkeypatch):
    root = tmp_path / "any"
    with _simulated_hold(root):
        require_held(root)  # positive control: same pid, entry present
        real_pid = os.getpid()
        # What a forked child sees: inherited thread-local data, new pid.
        monkeypatch.setattr(coordination.os, "getpid", lambda: real_pid + 1)
        with pytest.raises(CoordinationRejected, match="live lock"):
            require_held(root)
        monkeypatch.undo()
