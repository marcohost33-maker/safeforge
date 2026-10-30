"""R11: one forger per coordination check (SF-R9-04 discrimination, SF-R11-01, SF-P3-03).

R10 left SF-R9-04 (marker O_NOFOLLOW/owner/mode/link count) "Linux-only, not
discriminated": test_r9_context_attestation.py rejects four corrupted markers,
but nobody checked that each individual check is load-bearing. Several checks
overlap (a symlink fails S_ISREG before O_NOFOLLOW is ever reached), so a test
that only expects *some* CoordinationRejected stays green when a check is
deleted. Each forger below is built to hit exactly one check, and each test
asserts that check's own message. experiments/dk_coordination_mutants.py
deletes the checks one at a time and requires the matching test to go red.

Real kernel objects throughout (symlinks, hard links, FIFOs, sockets, chown).
Only the race windows (lstat -> open, open -> flock) are hit
deterministically by wrapping the call that opens the window.
"""

from __future__ import annotations

import io
import os
import socket
import sys
from pathlib import Path

import pytest

from safeforge import ObjectStore, connect, coordination
from safeforge.coordination import LOCK, MARKER_CONTENT, MODE, CoordinationRejected, event_writer, initialize, lock

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real POSIX marker/lock/root checks")

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
NOBODY = 65534


def mkroot(base: Path, name: str = "source") -> Path:
    root = base / name
    with ObjectStore(str(root), enforce_private=True) as s:
        s.ingest(io.BytesIO(b"123"), max_bytes=10)
    initialize(root, externally_quiescent=True)
    return root


def rejected(root, needle: str) -> None:
    """lock() must refuse with exactly this check's message (no leaked OSError)."""
    try:
        with lock(root, exclusive=False):
            pass
    except CoordinationRejected as exc:
        assert needle in str(exc), "rejected by another check: %s" % exc
        return
    except Exception as exc:  # noqa: BLE001 -- the type is what we assert on
        raise AssertionError("contract broken, %s escaped: %s" % (type(exc).__name__, exc)) from exc
    raise AssertionError("forged state accepted; expected %r" % needle)


def valid_marker_copy(root: Path) -> Path:
    other = root.parent / "forged-marker"
    other.write_bytes(MARKER_CONTENT)
    other.chmod(0o600)
    return other


def test_positive_control_valid_store_is_accepted(tmp_path):
    root = mkroot(tmp_path)
    with lock(root, exclusive=False):
        pass


# -- marker ------------------------------------------------------------------


def test_marker_socket_rejected_by_type_check(tmp_path, monkeypatch):
    # nlink 1, our uid, mode 0600: only S_ISREG distinguishes it from a marker.
    root = mkroot(tmp_path)
    (root / MODE).unlink()
    # Bind by a relative name: sun_path holds ~107 bytes, a deep basetemp does not fit.
    monkeypatch.chdir(root)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(MODE)
        os.chmod(root / MODE, 0o600)
        rejected(root, "marker metadata")


def test_marker_hardlink_rejected_by_link_count(tmp_path):
    root = mkroot(tmp_path)
    os.link(root / MODE, tmp_path / "outside-alias")
    rejected(root, "marker metadata")


@pytest.mark.parametrize("how", ["chown", "lstat-shim"])
def test_marker_foreign_owner_rejected(tmp_path, monkeypatch, how):
    root = mkroot(tmp_path)
    marker = root / MODE
    if how == "chown":
        if not IS_ROOT:
            pytest.skip("a real foreign owner needs chown (root); lstat-shim covers the comparison")
        os.chown(marker, NOBODY, -1)
    else:
        real = os.lstat

        def lstat(path, *a, **k):
            st = real(path, *a, **k)
            if os.fspath(path) == os.fspath(marker):
                fields = list(st)
                fields[4] = st.st_uid + 1  # st_uid
                return os.stat_result(fields)
            return st

        monkeypatch.setattr(coordination.os, "lstat", lstat)
    rejected(root, "marker metadata")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o610])
def test_marker_group_or_other_bits_rejected(tmp_path, mode):
    root = mkroot(tmp_path)
    (root / MODE).chmod(mode)
    rejected(root, "marker metadata")


def test_marker_content_rejected(tmp_path):
    root = mkroot(tmp_path)
    (root / MODE).write_bytes(MARKER_CONTENT + b"x")
    rejected(root, "marker content")


def _swap_after_lstat(monkeypatch, path: Path, swap) -> None:
    real = os.lstat
    done = []

    def lstat(p, *a, **k):
        st = real(p, *a, **k)
        if os.fspath(p) == os.fspath(path) and not done:
            done.append(True)
            swap()
        return st

    monkeypatch.setattr(coordination.os, "lstat", lstat)


def test_marker_symlink_swapped_in_after_lstat_rejected_by_nofollow(tmp_path, monkeypatch):
    # TOCTOU: lstat saw the real marker, open() meets a symlink to valid content.
    # O_NOFOLLOW turns that into ELOOP, which must surface as CoordinationRejected
    # (SF-R11-01: it used to escape as a raw OSError).
    root = mkroot(tmp_path)
    marker = root / MODE
    target = valid_marker_copy(root)

    def swap():
        marker.unlink()
        marker.symlink_to(target)

    _swap_after_lstat(monkeypatch, marker, swap)
    rejected(root, "cannot be opened safely")


def test_marker_regular_file_swapped_in_after_lstat_rejected_by_inode_check(tmp_path, monkeypatch):
    # A regular file renamed over the marker passes O_NOFOLLOW; only the
    # lstat/fstat (st_dev, st_ino) comparison sees the replacement.
    root = mkroot(tmp_path)
    marker = root / MODE
    forged = valid_marker_copy(root)
    _swap_after_lstat(monkeypatch, marker, lambda: os.replace(forged, marker))
    rejected(root, "marker replaced")


def test_marker_changed_after_flock_rejected_by_second_check(tmp_path, monkeypatch):
    root = mkroot(tmp_path)
    real = coordination._fcntl()

    class Flock:
        LOCK_SH, LOCK_EX, LOCK_NB = real.LOCK_SH, real.LOCK_EX, real.LOCK_NB

        @staticmethod
        def flock(fd, flags):
            real.flock(fd, flags)
            (root / MODE).write_bytes(b"changed while we waited for the lock\n")

    monkeypatch.setattr(coordination, "_fcntl", lambda: Flock)
    rejected(root, "marker content")


# -- lock file ---------------------------------------------------------------


def _plant_lock(root: Path, make) -> None:
    path = root / LOCK
    if os.path.lexists(path):
        path.unlink()
    make(path)


def test_lock_symlink_rejected_at_open(tmp_path):
    root = mkroot(tmp_path)
    target = tmp_path / "lock-target"
    target.write_bytes(b"")
    target.chmod(0o600)
    _plant_lock(root, lambda p: p.symlink_to(target))
    rejected(root, "lock must be a regular file")


def test_lock_directory_rejected_at_open(tmp_path):
    root = mkroot(tmp_path)
    _plant_lock(root, lambda p: p.mkdir(0o700))
    rejected(root, "lock must be a regular file")


def test_lock_fifo_rejected_by_type_check(tmp_path):
    root = mkroot(tmp_path)
    _plant_lock(root, lambda p: os.mkfifo(p, 0o600))
    rejected(root, "lock must be a regular file")


def test_lock_hardlink_rejected_by_link_count(tmp_path):
    root = mkroot(tmp_path)

    def make(p):
        p.write_bytes(b"")
        p.chmod(0o600)
        os.link(p, tmp_path / "lock-alias")

    _plant_lock(root, make)
    rejected(root, "extra hard links")


def test_lock_replaced_after_open_rejected(tmp_path, monkeypatch):
    root = mkroot(tmp_path)
    real = coordination._fcntl()

    class Flock:
        LOCK_SH, LOCK_EX, LOCK_NB = real.LOCK_SH, real.LOCK_EX, real.LOCK_NB

        @staticmethod
        def flock(fd, flags):
            real.flock(fd, flags)
            fresh = root / "fresh-lock"
            fresh.write_bytes(b"")
            os.replace(fresh, root / LOCK)  # a second actor now locks another inode

    monkeypatch.setattr(coordination, "_fcntl", lambda: Flock)
    rejected(root, "lock inode replaced")


# -- store root --------------------------------------------------------------


def test_root_symlink_rejected(tmp_path):
    root = mkroot(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    rejected(alias, "real directory")


def test_root_regular_file_rejected(tmp_path):
    fake = tmp_path / "not-a-dir"
    fake.write_bytes(b"")
    fake.chmod(0o600)
    rejected(fake, "real directory")


@pytest.mark.parametrize("mode", [0o750, 0o701])
def test_root_mode_rejected(tmp_path, mode):
    root = mkroot(tmp_path)
    root.chmod(mode)
    rejected(root, "mode 0700")


@pytest.mark.parametrize("how", ["chown", "stat-shim"])
def test_root_foreign_owner_rejected(tmp_path, monkeypatch, how):
    root = mkroot(tmp_path)
    if how == "chown":
        if not IS_ROOT:
            pytest.skip("a real foreign owner needs chown (root); stat-shim covers the comparison")
        os.chown(root, NOBODY, -1)
    else:
        real = os.stat

        def fake_stat(path, *a, **k):
            st = real(path, *a, **k)
            if os.fspath(path) == os.fspath(root):
                fields = list(st)
                fields[4] = st.st_uid + 1  # st_uid
                return os.stat_result(fields)
            return st

        monkeypatch.setattr(coordination.os, "stat", fake_stat)
    rejected(root, "mode 0700")


# -- registry identity (SF-P3-03) --------------------------------------------


def test_lock_on_one_store_does_not_unlock_another_through_dotdot_alias(tmp_path):
    # p/a is a symlink into elsewhere/, so the kernel resolves p/a/../source to
    # elsewhere/source while os.path.abspath says p/source. Before R11 holding
    # p/source opened elsewhere/source/safeforge.db through the coordinated gate.
    held = mkroot(tmp_path / "p")
    victim = mkroot(tmp_path / "elsewhere")
    (tmp_path / "elsewhere" / "deep").mkdir()
    (tmp_path / "p" / "a").symlink_to(tmp_path / "elsewhere" / "deep", target_is_directory=True)
    alias = os.path.join(str(tmp_path / "p" / "a"), "..", held.name, "safeforge.db")
    assert os.path.realpath(alias) == str(victim / "safeforge.db")
    assert os.path.abspath(alias) == str(held / "safeforge.db")
    with lock(held, exclusive=False):
        with pytest.raises(CoordinationRejected, match="live lock"):
            connect(alias, coordinated=True)
        # positive control: the held store itself still opens
        connect(str(held / "safeforge.db"), coordinated=True).close()


def test_dotdot_spelling_of_a_held_root_is_the_same_root(tmp_path):
    # Interoperability side of SF-P3-03: lock() kept "..", connect() dropped it,
    # so a legitimate root spelled with ".." was refused.
    root = mkroot(tmp_path)
    spelled = os.path.join(str(root), "..", root.name)
    with event_writer(spelled) as con:
        assert con.execute("SELECT count(*) FROM objects").fetchone()[0] == 1


def test_relative_root_matches_absolute_registration(tmp_path, monkeypatch):
    root = mkroot(tmp_path)
    monkeypatch.chdir(tmp_path)
    with lock(root.name, exclusive=False):
        connect(str(root / "safeforge.db"), coordinated=True).close()
    with lock(root, exclusive=False):
        connect(os.path.join(root.name, "safeforge.db"), coordinated=True).close()
