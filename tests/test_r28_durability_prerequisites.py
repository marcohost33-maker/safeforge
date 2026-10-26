"""R28/R29/R31 (SF-R28-01): ``powercut_durable`` may only be True when EVERY prerequisite was observed.

R27 made ``powercut_durable`` a separate claim, but it was computed from three
per-ingest syncs and the ``synchronous`` value *requested* at open. Cross-family
rounds R28..R31 (007acc2, GPT-6) found that this ignores:

* the parent-directory syncs of the layout (``layout_sync``): a ``"degraded:"`` or
  ``"unsupported:"`` parent passed the open and still gave True (R28 case A);
* the directory that holds an external ``db_path`` -- never synced at all -- and
  the database/WAL names, which are created *after* the layout fsyncs (R28 B, R29);
* the SQLite policy actually in force: a caller can change ``PRAGMA synchronous``
  or ``journal_mode`` on the open connection, also only for the moment of the
  status COMMIT (R28 D, R31 SF-R31-01).

Contract tested here, as ``D_pre = F and O and S and L and W`` (R28B):

    F  object file sync "ok"         O  objects/ sync "ok"      S  staging/ sync "ok"
    L  layout_sync non-empty and every entry "ok" (incl. the database's directory)
    W  synchronous FULL/EXTRA and journal_mode WAL, read INSIDE the status-commit
       transaction (SQLite refuses to change ``synchronous`` there), and this
       ingest's own UPDATE is the one that committed 'stored'

Anything not observed makes the flag False; the logical ACK is never withdrawn for
missing evidence (R29). The flag is a report of software prerequisites, not a
hardware power-cut guarantee.

Faults are injected by replacing ``durable.fsync_dir`` / ``durable.fsync_file``;
each negative test also asserts that its fault actually reached the store, and
the positive controls show the flag can still be True, so a fix that always
answers False cannot pass this file.
"""

from __future__ import annotations

import errno
import io
import os
import sqlite3
import sys
import time

import pytest

from safeforge import IngestResult, LayoutNotDurable, ObjectStore, _faults, durable

DEGRADED = "degraded:simulated-parent"
UNSUPPORTED = "unsupported:simulated-parent"


class FsyncDirFake:
    """``durable.fsync_dir`` replacement: "ok" by default, chosen statuses / failures per directory."""

    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.failing: set[str] = set()
        self.calls: list[str] = []
        self.probe = None  # optional: called with each path, result kept in ``probed``
        self.probed: list[tuple[str, object]] = []

    def __call__(self, path: str) -> str:
        path = os.path.abspath(path)
        self.calls.append(path)
        if self.probe is not None:
            self.probed.append((path, self.probe(path)))
        if path in self.failing:
            raise OSError(errno.EIO, "injected fsync failure", path)
        return self.status.get(path, "ok")


@pytest.fixture
def fake(monkeypatch):
    f = FsyncDirFake()
    monkeypatch.setattr(durable, "fsync_dir", f)
    monkeypatch.setattr(durable, "fsync_file", lambda fh: (fh.flush(), "ok")[1])
    return f


def _assert_three_syncs_ok(r: IngestResult) -> None:
    # Isolates the case: the per-ingest syncs are fine, only the tested prerequisite is not.
    assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")


# -- positive controls: the flag can be True at all ---------------------------------


def test_c_all_prerequisites_ok_is_durable(tmp_path, fake):
    with ObjectStore(str(tmp_path / "store")) as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        layout = dict(s.layout_sync)
    _assert_three_syncs_ok(r)
    assert layout and all(v == "ok" for v in layout.values()), layout
    assert r.powercut_durable is True


def test_c_external_db_all_ok_is_durable(tmp_path, fake):
    dbdir = tmp_path / "dbdir"
    dbdir.mkdir()
    with ObjectStore(str(tmp_path / "store"), db_path=str(dbdir / "meta.db")) as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        layout = dict(s.layout_sync)
    assert layout.get(str(dbdir)) == "ok", layout
    assert r.powercut_durable is True


# -- L: every layout status ----------------------------------------------------------


@pytest.mark.parametrize("status", [DEGRADED, UNSUPPORTED])
def test_a_layout_parent_not_ok_is_not_durable(tmp_path, fake, status):
    parent = str(tmp_path)  # parent of root/: synced at open, not per ingest
    fake.status[parent] = status
    with ObjectStore(str(tmp_path / "store")) as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        layout = dict(s.layout_sync)
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False, layout
    assert parent in fake.calls and layout[parent] == status


def test_empty_layout_report_is_not_durable(tmp_path, fake):
    with ObjectStore(str(tmp_path / "store")) as s:
        s.layout_sync = {}  # no evidence at all must not read as "all ok"
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False


# -- L: the database's directory -----------------------------------------------------


def test_b_external_db_parent_degraded_is_not_durable(tmp_path, fake):
    dbdir = tmp_path / "dbdir"
    dbdir.mkdir()
    fake.status[str(dbdir)] = DEGRADED
    with ObjectStore(str(tmp_path / "store"), db_path=str(dbdir / "meta.db")) as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        layout = dict(s.layout_sync)
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False, layout
    assert str(dbdir) in fake.calls and layout[str(dbdir)] == DEGRADED


def test_b_external_db_parent_fsync_failure_refuses_open(tmp_path, fake):
    dbdir = tmp_path / "dbdir"
    dbdir.mkdir()
    fake.failing.add(str(dbdir))
    root = str(tmp_path / "store")
    outcome = None
    try:
        with ObjectStore(root, db_path=str(dbdir / "meta.db")) as s:
            outcome = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
    except LayoutNotDurable as e:
        outcome = e
    assert isinstance(outcome, LayoutNotDurable), "store opened and acknowledged: %r" % (outcome,)
    assert outcome.layout_sync[str(dbdir)].startswith("failed:"), outcome.layout_sync
    # The refused open released its connection: a healthy reopen works.
    fake.failing.clear()
    with ObjectStore(root, db_path=str(dbdir / "meta.db")) as s:
        assert s.ingest(io.BytesIO(b"again"), max_bytes=100).powercut_durable is True


def test_db_parent_is_synced_after_the_database_exists(tmp_path, fake):
    # R29: the database and WAL names are created AFTER the layout fsyncs, so the
    # directory holding them must be synced once more, after the schema commit.
    dbdir = tmp_path / "dbdir"
    dbdir.mkdir()
    db_file = dbdir / "meta.db"
    fake.probe = lambda path: db_file.exists() if path == str(dbdir) else None
    with ObjectStore(str(tmp_path / "store"), db_path=str(db_file)) as s:
        assert s.layout_sync.get(str(dbdir)) == "ok", s.layout_sync
    seen = [exists for path, exists in fake.probed if path == str(dbdir)]
    assert seen and seen[-1] is True, seen


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="POSIX symlink")
def test_symlinked_db_path_is_not_durable(tmp_path, fake):
    real_dir = tmp_path / "real"
    link_dir = tmp_path / "links"
    real_dir.mkdir()
    link_dir.mkdir()
    target = real_dir / "meta.db"
    sqlite3.connect(str(target)).close()
    link = link_dir / "meta.db"
    os.symlink(str(target), str(link))
    with ObjectStore(str(tmp_path / "store"), db_path=str(link)) as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        layout = dict(s.layout_sync)
    assert r.powercut_durable is False, layout
    assert layout[str(real_dir)] == "ok" and layout[str(link_dir)] == "ok"
    assert layout[str(link)].startswith("degraded:"), layout


# -- W: the SQLite policy actually in force at the status commit ---------------------


def test_d_synchronous_normal_requested_is_not_durable(tmp_path, fake):
    with ObjectStore(str(tmp_path / "store"), synchronous="NORMAL") as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False


def test_d_synchronous_lowered_after_open_is_not_durable(tmp_path, fake):
    with ObjectStore(str(tmp_path / "store")) as s:
        s._conn.execute("PRAGMA synchronous=NORMAL")
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False


def test_d_normal_only_at_the_status_commit_is_not_durable(tmp_path, fake):
    # SF-R31-01: NORMAL exactly for the status COMMIT, FULL restored right after it.
    markers = []
    with ObjectStore(str(tmp_path / "store")) as s:

        def hook(name):
            if name == "dirs.fsynced":
                s._conn.execute("PRAGMA synchronous=NORMAL")
                markers.append((name, s._conn.execute("PRAGMA synchronous").fetchone()[0]))
            elif name == "status.committed":
                s._conn.execute("PRAGMA synchronous=FULL")
                markers.append((name, s._conn.execute("PRAGMA synchronous").fetchone()[0]))

        _faults.set_hook(hook)
        try:
            r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        finally:
            _faults.set_hook(None)
        assert s.status_of(r.object_id) == "stored"
    assert markers == [("dirs.fsynced", 1), ("status.committed", 2)]
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False


def test_d_journal_mode_left_wal_is_not_durable(tmp_path, fake):
    with ObjectStore(str(tmp_path / "store")) as s:
        assert s._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        assert s.status_of(r.object_id) == "stored"
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False


class _PragmaFailingConn:
    """Connection proxy whose ``PRAGMA synchronous`` read fails inside a transaction."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.raised = 0

    def execute(self, sql, *args):
        if sql.strip().upper() == "PRAGMA SYNCHRONOUS" and self._real.in_transaction:
            self.raised += 1
            raise sqlite3.OperationalError("injected: policy read failed")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_failed_policy_read_keeps_the_logical_ack(tmp_path, fake):
    # R29: missing evidence weakens the claim; it never turns a commit into a failure.
    with ObjectStore(str(tmp_path / "store")) as s:
        real = s._conn
        proxy = _PragmaFailingConn(real)
        s._conn = proxy
        try:
            r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        finally:
            s._conn = real
        assert s.status_of(r.object_id) == "stored"
        assert s.verify() == []
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False
    assert proxy.raised >= 1


def test_status_committed_by_another_connection_is_not_attested(tmp_path, fake):
    # Variant of the class: the reconciler (here on a NORMAL connection) rolls this
    # ingest forward before its own UPDATE; ingest() then ACKs a 'stored' row whose
    # commit policy it never saw.
    root = str(tmp_path / "store")
    rolled = []
    with ObjectStore(root) as s:

        def hook(name):
            if name == "dirs.fsynced":
                with ObjectStore(root, synchronous="NORMAL") as other:
                    rolled.append(other.reconcile(grace_seconds=0, now=time.time() + 1).rolled_forward)

        _faults.set_hook(hook)
        try:
            r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        finally:
            _faults.set_hook(None)
        assert s.status_of(r.object_id) == "stored"
    assert rolled == [1]
    _assert_three_syncs_ok(r)
    assert r.powercut_durable is False


# -- E: a deduplicated answer attests nothing ----------------------------------------


def test_e_deduplicated_result_attests_nothing(tmp_path, fake):
    with ObjectStore(str(tmp_path / "store")) as s:
        first = s.ingest(io.BytesIO(b"same"), max_bytes=100, idempotency_key="k")
        again = s.ingest(io.BytesIO(b"same"), max_bytes=100, idempotency_key="k")
    assert first.powercut_durable is True
    assert again.deduplicated is True and again.powercut_durable is False
