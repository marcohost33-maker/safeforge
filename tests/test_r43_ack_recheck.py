"""SF-R43-TOCTOU: the filesystem class is observed again after the status COMMIT.

Finding (007acc2 cross-family R43, GPT-6; re-read on 066cbdf): ``ObjectStore``
classified its layout once in the constructor (store.py:515-518) and the ACK
condition read that stored ``filesystem_class``. A remount (``nobarrier``), a
mount over a layout directory, or a layout path that now resolves elsewhere,
between construction and ACK, was not seen.

Invariant: after the COMMIT the class and ``(realpath, st_dev, st_ino)`` of every
layout path are read again; any difference gives ``durability_prerequisites_ok
is False`` -- never an exception, the ingest stays committed. Unchanged: True.

The mount table is synthetic (one Linux view for every layout path, so the test
runs on each runner); the path identity is the real one of tmp_path.
"""

from __future__ import annotations

import io
import os

import pytest

from safeforge import ObjectStore, _faults, durable, filesystems, mounts
from safeforge.filesystems import classify_linux

EXT4 = "22 1 252:0 / / rw,relatime - ext4 /dev/vda1 rw,errors=remount-ro\n34 22 252:16 / /data rw - ext4 /dev/vdb rw"
NOBARRIER = EXT4.replace("/dev/vdb rw", "/dev/vdb rw,nobarrier")
TMPFS_OVER = EXT4 + "\n35 34 0:40 / /data rw - tmpfs tmpfs rw"  # a tmpfs mounted over /data


@pytest.fixture
def view(monkeypatch):
    """Syncs faked "ok"; every layout path classified as /data/store under ``view["table"]``."""
    monkeypatch.setattr(durable, "fsync_dir", lambda d: "ok")
    monkeypatch.setattr(durable, "fsync_file", lambda fh: (fh.flush(), "ok")[1])
    state = {"table": EXT4, "calls": 0}

    def fake(paths, **kw):
        state["calls"] += 1
        if state.get("raise_on") == state["calls"]:
            raise RuntimeError("probe broke")
        table = mounts.parse_mountinfo_filesystems(state["table"])
        return {p: classify_linux("/data/store", table, ext4_options=lambda point: frozenset({"rw", "barrier"})) for p in paths}

    monkeypatch.setattr(filesystems, "filesystems", fake)
    return state


def _ingest(s: ObjectStore, data: bytes = b"r43"):
    return s.ingest(io.BytesIO(data), max_bytes=1 << 20)


def test_positive_control_unchanged_layout_is_durable(tmp_path, view):
    with ObjectStore(str(tmp_path / "root")) as s:
        first, second = _ingest(s), _ingest(s, b"again")
    assert s.filesystem_class == "local"
    assert (first.filesystem_class, first.durability_prerequisites_ok) == ("local", True)
    assert (second.filesystem_class, second.durability_prerequisites_ok) == ("local", True)


@pytest.mark.parametrize(
    "table, cls", [(TMPFS_OVER, "volatile"), (NOBARRIER, "unknown")], ids=["tmpfs-over", "remount-nobarrier"]
)
def test_mount_table_swapped_between_open_and_ingest(tmp_path, view, table, cls):
    with ObjectStore(str(tmp_path / "root")) as s:
        view["table"] = table
        r = _ingest(s)
        assert s.status_of(r.object_id) == "stored"  # the logical ACK stands
    assert s.filesystem_class == "local"  # the class at open
    assert (r.filesystem_class, r.durability_prerequisites_ok) == (cls, False)


def test_swap_at_the_commit_is_seen_the_check_runs_after_it(tmp_path, view):
    """The swap happens at the failpoint right after the status COMMIT: only a check
    that runs after the COMMIT can see it."""

    def hook(name):
        if name == "status.committed":
            view["table"] = NOBARRIER

    with ObjectStore(str(tmp_path / "root")) as s:
        _faults.set_hook(hook)
        r = _ingest(s)
    assert r.durability_prerequisites_ok is False


def test_swap_back_restores_the_claim(tmp_path, view):
    """Compared with the constructor's state, not latched: a later ingest on the
    original mounts is durable again."""
    with ObjectStore(str(tmp_path / "root")) as s:
        view["table"] = NOBARRIER
        bad = _ingest(s)
        view["table"] = EXT4
        good = _ingest(s, b"again")
    assert (bad.durability_prerequisites_ok, good.durability_prerequisites_ok) == (False, True)


def test_layout_directory_replaced_between_open_and_ingest(tmp_path, view):
    """Same class, other inode: objects/ is now a different directory (what a mount over it
    or a moved path looks like to stat). The mount table cannot show it; st_ino does."""
    with ObjectStore(str(tmp_path / "root")) as s:
        os.rename(s.objects_dir, s.objects_dir + ".old")
        os.mkdir(s.objects_dir)
        r = _ingest(s)
        assert s.status_of(r.object_id) == "stored"
    assert (r.filesystem_class, r.durability_prerequisites_ok) == ("unknown", False)


def _outcome(fn):
    """Result or the exception, so a raise fails an assertion instead of crashing the test."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the test asserts there is none
        return e


def test_probe_failure_after_commit_is_false_not_an_exception(tmp_path, view):
    with ObjectStore(str(tmp_path / "root")) as s:
        view["raise_on"] = view["calls"] + 1  # the next classification: the one after the COMMIT
        r = _outcome(lambda: _ingest(s))
        assert not isinstance(r, Exception), r
        assert s.status_of(r.object_id) == "stored"
    assert (r.filesystem_class, r.durability_prerequisites_ok) == ("unknown", False)


def test_layout_path_vanished_after_commit_is_false_not_an_exception(tmp_path, view, monkeypatch):
    from safeforge import store as store_mod

    def gone(paths):
        raise FileNotFoundError("layout path vanished")

    with ObjectStore(str(tmp_path / "root")) as s:
        monkeypatch.setattr(store_mod, "_layout_identity", gone)
        r = _outcome(lambda: _ingest(s))
    assert not isinstance(r, Exception), r
    assert (r.filesystem_class, r.durability_prerequisites_ok) == ("unknown", False)


def test_identity_not_observable_at_open_never_claims(tmp_path, view, monkeypatch):
    """stat fails at open only: the store opens (a report, never a refusal) and the
    missing baseline never matches the identity observed after the COMMIT."""
    from safeforge import store as store_mod

    real, calls = store_mod._layout_identity, []

    def first_call_refused(paths):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("stat refused")
        return real(paths)

    monkeypatch.setattr(store_mod, "_layout_identity", first_call_refused)
    s = _outcome(lambda: ObjectStore(str(tmp_path / "root")))
    assert not isinstance(s, Exception), s
    with s:
        r = _ingest(s)
    assert len(calls) == 2
    assert (r.filesystem_class, r.durability_prerequisites_ok) == ("unknown", False)


def test_a_different_local_filesystem_at_ack_is_not_the_one_seen_at_open(tmp_path, view):
    """Same inodes, but the table now names another (allowlisted) filesystem for them:
    the class is still "local", the observation is not the constructor's."""
    with ObjectStore(str(tmp_path / "root")) as s:
        view["table"] = EXT4.replace("- ext4 /dev/vdb rw", "- xfs /dev/vdb rw")
        r = _ingest(s)
    assert (r.filesystem_class, r.durability_prerequisites_ok) == ("unknown", False)


def test_not_local_at_open_is_not_rechecked(tmp_path, view):
    view["table"] = NOBARRIER
    with ObjectStore(str(tmp_path / "root")) as s:
        calls = view["calls"]
        view["table"] = EXT4  # a later "local" view must not upgrade the store
        r = _ingest(s)
    assert view["calls"] == calls
    assert (s.filesystem_class, r.filesystem_class, r.durability_prerequisites_ok) == ("unknown", "unknown", False)
