"""Claims T3 and T10: crash at every step, then check the disk/DB invariants.

Positive control: the corrected protocol satisfies every invariant at every
crash point. Negative controls: the naive designs (T3) and the reconstructed
V0.3 pattern (T10) violate them at the points the analysis did not consider.
"""

from __future__ import annotations

import errno
import io
import os
import sys
import threading
import time

import pytest

from safeforge import IngestAborted, ObjectStore, _faults, durable
from tests import invariants as inv
from tests.conftest import run_crash
from tests.crash_driver import CRASH_EXIT
from tests.designs.v03_reconstructed import V03Store

LATER = 10.0  # reconcile "in the future" so that grace_seconds=0 covers every file

# point -> expected status of the single row after reconcile (None = no row at all)
CORRECTED = {
    "staging.opened": None,
    "staging.chunk": None,  # crashes after the first 4 KiB of 64 KiB: torn staging file
    "staging.written": None,
    "staging.fsynced": None,
    "intent.committed": "failed",
    "object.published": "stored",  # rolled forward after re-hash
    "dirs.fsynced": "stored",
    "status.committed": "stored",
    "no-crash": "stored",  # positive control: completes and acknowledges
}


@pytest.mark.parametrize("point", list(CORRECTED))
def test_corrected_crash_matrix(tmp_path, point):
    root = str(tmp_path)
    rc, ack, err = run_crash("corrected", root, point)
    if point == "no-crash":
        assert rc == 0 and ack, err
    else:
        assert rc == CRASH_EXIT, err  # proves the failpoint was actually reached
        assert ack is None
    with ObjectStore(root) as s:
        s.reconcile(grace_seconds=0, now=time.time() + LATER)
        second = s.reconcile(grace_seconds=0, now=time.time() + LATER)
    assert second.changes == 0, "reconcile must be idempotent"
    lay = inv.corrected(root)
    assert inv.violations(lay) == []
    statuses = [st for st, _ in inv.rows(lay).values()]
    assert statuses == ([] if CORRECTED[point] is None else [CORRECTED[point]])
    if ack:
        assert inv.rows(lay)[ack][0] == "stored"


def test_corrected_reconcile_respects_grace_for_inflight(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("corrected", root, "intent.committed")
    assert rc == CRASH_EXIT, err
    with ObjectStore(root) as s:
        rep = s.reconcile(grace_seconds=300)  # the row is seconds old: must be left alone
        assert rep.changes == 0
        assert [st for st, _ in inv.rows(inv.corrected(root)).values()] == ["pending"]
        assert len(os.listdir(s.staging_dir)) == 1


def test_corrected_orphan_object_respects_grace(tmp_path):
    root = str(tmp_path)
    with ObjectStore(root) as s:
        orphan = os.path.join(s.objects_dir, "0" * 32)
        with open(orphan, "wb") as f:
            f.write(b"x")
        assert s.reconcile(grace_seconds=300).deleted_objects == 0
        assert os.path.exists(orphan)
        assert s.reconcile(grace_seconds=0, now=time.time() + LATER).deleted_objects == 1
        assert not os.path.exists(orphan)


def test_corrected_roll_forward_requires_matching_hash(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("corrected", root, "object.published")
    assert rc == CRASH_EXIT, err
    lay = inv.corrected(root)
    (oid,) = inv.rows(lay)
    p = os.path.join(lay.object_dir, oid)
    data = bytearray(open(p, "rb").read())
    data[0] ^= 0xFF  # same size, different content
    with open(p, "wb") as f:
        f.write(data)
    with ObjectStore(root) as s:
        rep = s.reconcile(grace_seconds=0, now=time.time() + LATER)
    assert rep.rolled_forward == 0 and rep.marked_failed == 1
    assert inv.violations(lay) == []


def test_corrected_reconcile_never_deletes_foreign_files(tmp_path):
    with ObjectStore(str(tmp_path)) as s:
        foreign = os.path.join(s.objects_dir, "README.txt")
        with open(foreign, "w") as f:
            f.write("not ours")
        rep = s.reconcile(grace_seconds=0, now=time.time() + LATER)
        assert os.path.exists(foreign) and foreign in rep.foreign


RACE_POINTS = [p for p in CORRECTED if p != "no-crash"]


@pytest.mark.parametrize("point", RACE_POINTS)
def test_corrected_concurrent_reconciler_at_every_point(tmp_path, point):
    """A reconciler with grace 0 runs exactly at ``point`` while the ingest is in flight."""
    root = str(tmp_path)
    writer, other = ObjectStore(root), ObjectStore(root)
    fired = []

    def hook(name):
        if name == point and not fired:
            fired.append(name)
            other.reconcile(grace_seconds=0, now=time.time() + LATER)

    _faults.set_hook(hook)
    acked = None
    try:
        acked = writer.ingest(io.BytesIO(b"r" * 65536), max_bytes=1 << 20, chunk_size=4096).object_id
    except (FileNotFoundError, IngestAborted):
        pass
    finally:
        _faults.set_hook(None)
    assert fired == [point]
    other.reconcile(grace_seconds=0, now=time.time() + LATER)
    writer.close()
    other.close()
    lay = inv.corrected(root)
    assert inv.violations(lay) == []
    if acked:
        assert inv.rows(lay)[acked][0] == "stored"


def test_corrected_writer_cannot_resurrect_a_failed_row(tmp_path):
    """The interleaving the conditional UPDATE exists for:

    reconciler marks the pending row 'failed' (object not there yet)
    -> writer publishes the object
    -> reconciler deletes the object of the failed row
    -> writer commits its status.

    An unconditional UPDATE would now report success for a 'stored' row
    without a file. Threads + failpoints make the order deterministic."""
    root = str(tmp_path)
    writer = ObjectStore(root)
    paused, resume, done = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def reconciler():
        try:
            s = ObjectStore(root)
            s.reconcile(grace_seconds=0, now=time.time() + LATER)
            s.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            done.set()

    def hook(name):
        if name == "intent.committed":
            threading.Thread(target=reconciler).start()
            assert paused.wait(10)
        elif name == "reconcile.pending_resolved":
            paused.set()
            assert resume.wait(10)
        elif name == "object.published":
            resume.set()
            assert done.wait(10)

    _faults.set_hook(hook)
    acked = None
    try:
        acked = writer.ingest(io.BytesIO(b"w" * 1000), max_bytes=1 << 20).object_id
    except IngestAborted:
        pass
    finally:
        _faults.set_hook(None)
        writer.close()
    assert errors == []
    assert acked is None, "writer reported success for an object the reconciler had already failed"
    assert inv.violations(inv.corrected(root)) == []


def test_corrected_young_pending_object_is_kept(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("corrected", root, "object.published")
    assert rc == CRASH_EXIT, err
    with ObjectStore(root) as s:
        assert s.reconcile(grace_seconds=300).changes == 0
        assert len(os.listdir(s.objects_dir)) == 1


def test_corrected_young_staging_file_is_kept(tmp_path):
    with ObjectStore(str(tmp_path)) as s:
        p = os.path.join(s.staging_dir, "1" * 32 + ".part")
        with open(p, "wb") as f:
            f.write(b"still streaming")
        assert s.reconcile(grace_seconds=300).deleted_staging == 0
        assert os.path.exists(p)
        assert s.reconcile(grace_seconds=0, now=time.time() + LATER).deleted_staging == 1


def test_error_after_commit_never_deletes_the_stored_object(tmp_path):
    root = str(tmp_path)

    def hook(name):
        if name == "status.committed":
            raise RuntimeError("caller-side failure after the commit")

    with ObjectStore(root) as s:
        _faults.set_hook(hook)
        with pytest.raises(RuntimeError):
            s.ingest(io.BytesIO(b"keep me"), max_bytes=100)
        _faults.set_hook(None)
    lay = inv.corrected(root)
    assert [st for st, _ in inv.rows(lay).values()] == ["stored"]
    assert inv.violations(lay) == []


# ---------------------------------------------------------------- T3 (naive)
def test_t3_naive_file_first_crash_leaves_orphan_file(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("naive_file_first", root, "naive.file_written")
    assert rc == CRASH_EXIT, err
    v = inv.violations(inv.naive(root))
    assert any(x.startswith("I2 orphan file") for x in v), v


def test_t3_naive_row_first_crash_leaves_dangling_row(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("naive_row_first", root, "naive.row_inserted")
    assert rc == CRASH_EXIT, err
    v = inv.violations(inv.naive(root))
    assert any(x.startswith("I1 missing file") for x in v), v


# ------------------------------------------------------- T10 (V0.3 pattern)
def test_t10_v03_uncommitted_pending_row_does_not_survive_a_crash(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("v03", root, "v03.pending_inserted")
    assert rc == CRASH_EXIT, err
    lay = inv.v03(root)
    assert inv.rows(lay) == {}, "the 'pending_storage' row was never committed"
    V03Store(root).reconcile(grace_seconds=0, now=time.time() + LATER)
    v = inv.violations(lay)
    assert any(x.startswith("I4 staging leftover") for x in v), v  # leak the reconciler cannot see


def test_t10_v03_crash_after_staging_leaks(tmp_path):
    root = str(tmp_path)
    rc, _, err = run_crash("v03", root, "v03.staged")
    assert rc == CRASH_EXIT, err
    V03Store(root).reconcile(grace_seconds=0, now=time.time() + LATER)
    assert any(x.startswith("I4") for x in inv.violations(inv.v03(root)))


def test_t10_v03_crash_after_rename_is_repaired(tmp_path):
    """Positive control for the V0.3 reconciler: rule 1 handles this point."""
    root = str(tmp_path)
    rc, _, err = run_crash("v03", root, "v03.renamed")
    assert rc == CRASH_EXIT, err
    V03Store(root).reconcile(grace_seconds=0, now=time.time() + LATER)
    assert inv.violations(inv.v03(root)) == []


def test_t10_v03_concurrent_reconciler_creates_dangling_row(tmp_path):
    root = str(tmp_path)
    writer, other = V03Store(root), V03Store(root)

    def hook(name):
        if name == "v03.renamed":
            other.reconcile()  # default 5-min rule; rule 1 has no grace at all

    _faults.set_hook(hook)
    try:
        uid = writer.ingest(b"payload")
    finally:
        _faults.set_hook(None)
    v = inv.violations(inv.v03(root))
    assert inv.rows(inv.v03(root))[uid][0] == "quarantine"  # committed and acknowledged ...
    assert any(x.startswith("I1 missing file") for x in v), v  # ... but the file is gone


# ------------------------------------------------- volume / platform semantics
def test_cross_device_guard_refuses(monkeypatch, tmp_path):
    devs = {}

    def fake_device(path):
        return devs.setdefault(os.path.abspath(path), len(devs) + 1)  # every path its own device

    monkeypatch.setattr(durable, "_device", fake_device)
    with pytest.raises(durable.CrossDeviceError):
        ObjectStore(str(tmp_path))


def test_same_device_positive_control(tmp_path):
    durable.require_same_volume(str(tmp_path), str(tmp_path))


def _other_fs_dir():
    if not sys.platform.startswith("linux") or not os.path.isdir("/dev/shm"):
        return None
    return "/dev/shm"


@pytest.mark.skipif(_other_fs_dir() is None, reason="needs Linux with /dev/shm")
def test_t10_real_exdev_on_linux(tmp_path):
    shm = _other_fs_dir()
    if os.stat(shm).st_dev == os.stat(str(tmp_path)).st_dev:
        pytest.skip("/dev/shm is on the same device here")
    stage = os.path.join(shm, "safeforge-test-%d" % os.getpid())
    os.makedirs(stage, exist_ok=True)
    try:
        with pytest.raises(OSError) as ei:
            V03Store(str(tmp_path), staging_dir=stage).ingest(b"x")  # the /tmp-on-tmpfs layout
        assert ei.value.errno == errno.EXDEV
        with pytest.raises(durable.CrossDeviceError):
            ObjectStore(str(tmp_path), staging_dir=stage)
    finally:
        for n in os.listdir(stage):
            os.unlink(os.path.join(stage, n))
        os.rmdir(stage)


def test_publish_never_clobbers(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"new")
    b.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        durable.publish(str(a), str(b))
    assert b.read_bytes() == b"old" and a.read_bytes() == b"new"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_windows_os_replace_fails_while_target_is_open(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"new")
    b.write_bytes(b"old")
    with open(b, "rb"):
        with pytest.raises(PermissionError):
            os.replace(a, b)
    assert b.read_bytes() == b"old"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rename semantics")
def test_posix_os_replace_succeeds_while_target_is_open(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"new")
    b.write_bytes(b"old")
    with open(b, "rb") as held:
        os.replace(a, b)
        assert held.read() == b"old"  # the open handle still sees the old inode
    assert b.read_bytes() == b"new"
