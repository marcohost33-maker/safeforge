"""R9 library guards, exercised on every platform.

test_r9_context_attestation.py runs only on Linux because it needs a real
flock. The guards SF-R9-01/02/03/05/06 themselves are plain Python/SQLite
logic over the process-local lock registry. Here the registry entry comes from
the REAL ``coordination.lock()`` with only its OS layer stubbed (``hold``
fixture: ``_fcntl`` -> recording no-op flock, ``_private_root`` and
``_check_marker`` -> no-ops, because they need ``os.getuid``), so the guards
can be checked on Windows and macOS too, against the registration, key
derivation and release that ``lock()`` really performs.

An earlier version wrote a copy of lock()'s registry bookkeeping into the test.
It stayed green when lock() registered under a different key or did not
deregister on exit (DK-20261020T094739, both BLIND on Windows): it tested the
guards against the copy, not against lock().

WHITE-BOX: says nothing about flock semantics or the POSIX marker/ownership
checks (SF-R9-04: O_NOFOLLOW/owner/mode/link count); those stay Linux-tested.
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


class _RecordingFlock:
    LOCK_SH, LOCK_EX, LOCK_NB = 1, 2, 4

    def __init__(self):
        self.calls = []

    def flock(self, fd, flags):
        self.calls.append(flags)


@pytest.fixture
def hold(monkeypatch):
    """``hold(root)``: the real coordination.lock(root, exclusive=False), OS layer stubbed."""
    flock = _RecordingFlock()
    monkeypatch.setattr(coordination, "_fcntl", lambda: flock)
    monkeypatch.setattr(coordination, "_private_root", lambda root: None)
    monkeypatch.setattr(coordination, "_check_marker", lambda root: None)

    @contextmanager
    def _hold(root):
        before = len(flock.calls)
        with coordination.lock(root, exclusive=False):
            # Proof that lock() itself ran (and took the shared, non-blocking lock).
            assert flock.calls[before:] == [flock.LOCK_SH | flock.LOCK_NB]
            yield

    return _hold


def test_r9_01_coordinated_connect_needs_live_registry_entry(tmp_path, hold):
    root = _marked_root(tmp_path)
    with pytest.raises(CoordinationRejected, match="live lock"):
        connect(str(root / "safeforge.db"), coordinated=True)
    # Positive control: the same call succeeds while held.
    with hold(root):
        con = connect(str(root / "safeforge.db"), coordinated=True)
        try:
            assert con.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        finally:
            con.close()
    with pytest.raises(CoordinationRejected, match="live lock"):
        connect(str(root / "safeforge.db"), coordinated=True)


def test_r9_02_connection_and_cursor_recheck_after_release(tmp_path, hold):
    root = _marked_root(tmp_path)
    with hold(root):
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


def test_r9_03_authorizer_denies_native_execute_after_release(tmp_path, hold):
    root = _marked_root(tmp_path)
    sql = "SELECT count(*) FROM objects"
    with hold(root):
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


def test_r9_05_coordinated_store_and_connect_refuse_noncanonical_paths(tmp_path, hold):
    root = _marked_root(tmp_path)
    # These checks run before the lock is taken, so they hold on every platform.
    # Must be the path guard itself, not a later lock/marker/connect failure.
    _rejected_by(IngestError, "database inside", ObjectStore, str(root), coordinated=True, db_path=str(tmp_path / "external.db"))
    _rejected_by(IngestError, "staging", ObjectStore, str(root), coordinated=True, staging_dir=str(tmp_path / "external-staging"))
    assert not (tmp_path / "external.db").exists()
    assert not (tmp_path / "external-staging").exists()
    with hold(root):
        with pytest.raises(ConfigurationError, match="root/safeforge.db"):
            connect(str(root / "another.db"), coordinated=True)
    assert not (root / "another.db").exists()


def test_r9_06_registry_from_another_pid_is_discarded(tmp_path, monkeypatch, hold):
    root = tmp_path / "any"
    root.mkdir()  # the real lock() opens root/LOCK
    with hold(root):
        require_held(root)  # positive control: same pid, entry present
        real_pid = os.getpid()
        # What a forked child sees: inherited thread-local data, new pid.
        monkeypatch.setattr(coordination.os, "getpid", lambda: real_pid + 1)
        with pytest.raises(CoordinationRejected, match="live lock"):
            require_held(root)
        monkeypatch.undo()


def test_r9_nested_hold_survives_inner_release(tmp_path, hold):
    """lock()'s reference count: closing an inner context must not drop the outer one."""
    root = _marked_root(tmp_path)
    with hold(root):
        with hold(root):
            require_held(root)
        require_held(root)  # outer context still live
    with pytest.raises(CoordinationRejected, match="live lock"):
        require_held(root)
