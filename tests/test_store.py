"""ObjectStore API contract: happy path, input validation, idempotency, step order."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import sys
import time

import pytest

from safeforge import (
    DigestMismatch,
    IdempotencyConflict,
    ObjectStore,
    TooLarge,
    _faults,
    connect,
    durable,
    transaction,
)
from safeforge.db import ConfigurationError
from tests import invariants as inv


@pytest.fixture
def store(tmp_path):
    s = ObjectStore(str(tmp_path))
    yield s
    s.close()


def test_roundtrip(store, tmp_path):
    data = os.urandom(200_000)
    r = store.ingest(io.BytesIO(data), max_bytes=1 << 20, chunk_size=7000)
    assert r.sha256 == hashlib.sha256(data).hexdigest() and r.size == len(data) and not r.deduplicated
    with open(store.path_of(r.object_id), "rb") as f:
        assert f.read() == data
    assert os.listdir(store.staging_dir) == []
    assert inv.violations(inv.corrected(str(tmp_path))) == []


def test_empty_stream_is_a_valid_object(store):
    r = store.ingest(io.BytesIO(b""), max_bytes=1)
    assert r.size == 0 and os.path.getsize(store.path_of(r.object_id)) == 0


def test_too_large_leaves_nothing(store, tmp_path):
    with pytest.raises(TooLarge):
        store.ingest(io.BytesIO(b"x" * 101), max_bytes=100)
    assert os.listdir(store.staging_dir) == [] and os.listdir(store.objects_dir) == []
    assert inv.rows(inv.corrected(str(tmp_path))) == {}


def test_exactly_max_bytes_is_accepted(store):
    assert store.ingest(io.BytesIO(b"x" * 100), max_bytes=100).size == 100


def test_digest_mismatch_leaves_nothing(store, tmp_path):
    with pytest.raises(DigestMismatch):
        store.ingest(io.BytesIO(b"abc"), max_bytes=10, expected_sha256="0" * 64)
    assert os.listdir(store.staging_dir) == [] and inv.rows(inv.corrected(str(tmp_path))) == {}


def test_expected_digest_accepted(store):
    d = hashlib.sha256(b"abc").hexdigest()
    assert store.ingest(io.BytesIO(b"abc"), max_bytes=10, expected_sha256=d).sha256 == d


@pytest.mark.parametrize(
    "kwargs, exc",
    [
        ({"max_bytes": 0}, ValueError),
        ({"max_bytes": -5}, ValueError),
        ({"max_bytes": True}, ValueError),
        ({"max_bytes": 1.5}, ValueError),
        ({"max_bytes": 10, "chunk_size": 0}, ValueError),
        ({"max_bytes": 10, "expected_sha256": "ABC"}, ValueError),
        ({"max_bytes": 10, "expected_sha256": "0" * 64 + "\n"}, ValueError),
        ({"max_bytes": 10, "idempotency_key": ""}, ValueError),
        ({"max_bytes": 10, "idempotency_key": "k" * 257}, ValueError),
    ],
)
def test_invalid_arguments_rejected_before_any_io(store, kwargs, exc):
    try:
        store.ingest(io.BytesIO(b"abc"), **kwargs)
        err = None
    except Exception as e:  # noqa: BLE001 -- the TYPE is the assertion
        err = e
    assert type(err) is exc, "expected %s before any I/O, got %r" % (exc.__name__, err)
    assert os.listdir(store.staging_dir) == []


def test_stream_returning_text_is_rejected_and_cleaned(store):
    with pytest.raises(TypeError):
        store.ingest(io.StringIO("abc"), max_bytes=10)
    assert os.listdir(store.staging_dir) == []


def test_not_a_stream(store):
    with pytest.raises(TypeError):
        store.ingest(b"abc", max_bytes=10)


def test_idempotency_key_dedups(store, tmp_path):
    a = store.ingest(io.BytesIO(b"same"), max_bytes=10, idempotency_key="upload-1")
    b = store.ingest(io.BytesIO(b"same"), max_bytes=10, idempotency_key="upload-1")
    assert b.deduplicated and b.object_id == a.object_id
    assert len(inv.rows(inv.corrected(str(tmp_path)))) == 1


def test_idempotency_key_with_different_content_conflicts(store):
    store.ingest(io.BytesIO(b"one"), max_bytes=10, idempotency_key="upload-2")
    with pytest.raises(IdempotencyConflict):
        store.ingest(
            io.BytesIO(b"two"), max_bytes=10, idempotency_key="upload-2", expected_sha256=hashlib.sha256(b"two").hexdigest()
        )


def test_path_of_refuses_unknown_and_malformed(store):
    for bad in ["0" * 32, "../x", "0" * 32 + "\n", None]:
        with pytest.raises(KeyError):
            store.path_of(bad)


@pytest.mark.parametrize("grace", [-1, float("nan"), float("inf"), True, "5"])
def test_reconcile_rejects_bad_grace(store, grace):
    with pytest.raises(ValueError):
        store.reconcile(grace_seconds=grace)


def test_step_order_fsync_publish_dirsync_commit(monkeypatch, store):
    """The durability order: fsync(file) < publish < fsync(objects dir) < status commit."""
    events = []
    real_ff, real_pub, real_fd = durable.fsync_file, durable.publish, durable.fsync_dir
    monkeypatch.setattr(durable, "fsync_file", lambda f: (events.append("fsync_file"), real_ff(f))[1])
    monkeypatch.setattr(durable, "publish", lambda a, b: (events.append("publish"), real_pub(a, b))[1])
    monkeypatch.setattr(durable, "fsync_dir", lambda d: (events.append("fsync_dir:" + os.path.basename(d)), real_fd(d))[1])
    _faults.set_hook(lambda name: events.append(name) if name in ("intent.committed", "status.committed") else None)
    store.ingest(io.BytesIO(b"order"), max_bytes=10)
    assert events == ["fsync_file", "intent.committed", "publish", "fsync_dir:objects", "fsync_dir:staging", "status.committed"]


def _record_syncs(monkeypatch) -> list:
    """Record each sync primitive as (name, fd): os.fsync, and on macOS F_FULLFSYNC.

    R12: on macOS durable issues fcntl(F_FULLFSYNC), because fsync(2) there
    does not flush the drive cache; plain os.fsync is only the fallback.
    """
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(("fsync", fd)), real(fd))[1])
    if sys.platform == "darwin":
        import fcntl

        real_fcntl = fcntl.fcntl

        def spy(fd, cmd, *a):
            if cmd == fcntl.F_FULLFSYNC:
                calls.append(("F_FULLFSYNC", fd))
            return real_fcntl(fd, cmd, *a)

        monkeypatch.setattr(fcntl, "fcntl", spy)
    return calls


EXPECTED_SYNC = "F_FULLFSYNC" if sys.platform == "darwin" else "fsync"


def test_fsync_file_really_syncs(monkeypatch, tmp_path):
    calls = _record_syncs(monkeypatch)
    with open(tmp_path / "f", "wb") as f:
        f.write(b"x")
        assert durable.fsync_file(f) == "ok"
        assert calls == [(EXPECTED_SYNC, f.fileno())]


def test_fsync_dir_reports_platform_truthfully(tmp_path, monkeypatch):
    calls = _record_syncs(monkeypatch)
    status = durable.fsync_dir(str(tmp_path))
    if sys.platform == "win32":
        assert status.startswith("unsupported:") and calls == []
    else:
        assert status == "ok" and [name for name, _ in calls] == [EXPECTED_SYNC]


def test_ingest_result_reports_dir_sync(store):
    r = store.ingest(io.BytesIO(b"x"), max_bytes=10)
    assert r.dir_sync == ("ok" if sys.platform != "win32" else "unsupported:windows-has-no-directory-fsync")
    assert r.publish.startswith("windows:" if sys.platform == "win32" else "posix:")


def test_verify_detects_bitrot(store):
    r = store.ingest(io.BytesIO(b"abcdef"), max_bytes=10)
    assert store.verify() == []
    with open(store.path_of(r.object_id), "r+b") as f:
        f.write(b"X")
    assert store.verify() == [r.object_id]


def test_reconcile_reports_missing_stored_file(store):
    r = store.ingest(io.BytesIO(b"abc"), max_bytes=10)
    os.unlink(store.path_of(r.object_id))
    assert store.reconcile(grace_seconds=0, now=time.time() + 10).missing_or_corrupt == [r.object_id]


# ----------------------------------------------------------------- db policy
def test_connect_defaults_to_wal_full(tmp_path):
    c = connect(str(tmp_path / "x.db"))
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    c.close()


def test_connect_refuses_when_wal_impossible():
    with pytest.raises(ConfigurationError):
        connect(":memory:")


@pytest.mark.parametrize("kw", [{"synchronous": "OFF"}, {"synchronous": "bogus"}, {"busy_timeout_ms": -1}])
def test_connect_rejects_unsafe_settings(tmp_path, kw):
    with pytest.raises(ValueError):
        connect(str(tmp_path / "x.db"), **kw)


def test_transaction_rolls_back_on_error(tmp_path):
    c = connect(str(tmp_path / "x.db"))
    c.execute("CREATE TABLE t(x)")
    with pytest.raises(RuntimeError):
        with transaction(c):
            c.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    assert not c.in_transaction


def test_transaction_rolls_back_when_commit_fails(tmp_path):
    p = str(tmp_path / "x.db")
    c = connect(p)
    c.execute("CREATE TABLE p(id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE ch(pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(c):
            c.execute("INSERT INTO ch VALUES (42)")  # FK violation surfaces only at COMMIT
    assert not c.in_transaction
    assert c.execute("SELECT count(*) FROM ch").fetchone()[0] == 0
