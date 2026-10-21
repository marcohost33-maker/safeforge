"""R12: second-pass findings on the R11 head (c52a7b1).

* SF-R12-01: backup/restore containment checks compared path spellings. A
  backup could be written inside the live store through ``..`` or a symlink,
  and a restore inside its own backup (which then no longer verifies). On
  Windows, ``commonpath`` raised ValueError for a destination on another drive.
* SF-R12-02: macOS fsync(2) does not flush the drive cache; ``durable`` and
  SQLite (``fullfsync`` default OFF) reported/used plain fsync as "durable".
* SF-R12-03: coordination hardening asked for in R10 (SF-P2-02) and not done
  in R11: refuse without O_NOFOLLOW instead of opening with a weaker flag set;
  owner/mode rule for the lock file like for the marker; effective uid.
* Regression lock for a side effect of the R11 fix: an *uncoordinated*
  connect() through a ``..`` alias reached a coordinated database.
"""

from __future__ import annotations

import io
import os
import sys
import types

import pytest

from safeforge import ConfigurationError, ObjectStore, connect, coordination, db, durable
from safeforge.coordination import LOCK, CoordinationRejected, initialize, lock
from safeforge.offline_backup import BackupRejected, backup_offline, restore_offline

POSIX_LINKS = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="real POSIX coordination checks")


def _store(base, name="store"):
    src = base / name
    with ObjectStore(str(src)) as s:
        s.ingest(io.BytesIO(b"payload"), max_bytes=100)
    return src


# -- SF-R12-01 containment ---------------------------------------------------

def test_backup_into_source_through_dotdot_is_rejected(tmp_path):
    src = _store(tmp_path)
    (tmp_path / "x").mkdir()
    with pytest.raises(BackupRejected, match="outside source root"):
        backup_offline(src, tmp_path / "x" / ".." / "store" / "bk", confirmed_quiescent=True)
    assert not (src / "bk").exists()


@POSIX_LINKS
def test_backup_into_source_through_symlink_is_rejected(tmp_path):
    src = _store(tmp_path)
    (tmp_path / "alias").symlink_to(src, target_is_directory=True)
    with pytest.raises(BackupRejected, match="outside source root"):
        backup_offline(src, tmp_path / "alias" / "bk", confirmed_quiescent=True)
    assert not (src / "bk").exists()


@POSIX_LINKS
def test_restore_into_backup_through_symlink_is_rejected_and_backup_stays_valid(tmp_path):
    from safeforge.offline_backup import verify_backup

    archive = tmp_path / "archive"
    backup_offline(_store(tmp_path), archive, confirmed_quiescent=True)
    (tmp_path / "alias").symlink_to(archive, target_is_directory=True)
    with pytest.raises(BackupRejected, match="outside backup"):
        restore_offline(archive, tmp_path / "alias" / "restored", confirmed_quiescent=True)
    assert verify_backup(archive)["verdict"] == "offline_snapshot_integrity_pass"


@pytest.mark.skipif(sys.platform != "linux", reason="atomic restore is Linux-only")
def test_atomic_restore_into_backup_through_symlink_is_rejected(tmp_path):
    from safeforge.atomic_restore import restore_atomic

    archive = tmp_path / "archive"
    backup_offline(_store(tmp_path), archive, confirmed_quiescent=True)
    (tmp_path / "alias").symlink_to(archive, target_is_directory=True)
    with pytest.raises(BackupRejected, match="inside backup"):
        restore_atomic(archive, tmp_path / "alias" / "restored", confirmed_quiescent=True)


def test_sibling_with_common_name_prefix_is_not_inside(tmp_path):
    # positive control: "store2" is not below "store" (no string-prefix check)
    src = _store(tmp_path)
    report = backup_offline(src, tmp_path / "store2", confirmed_quiescent=True)
    assert report["stored_objects"] == 1


def test_other_drive_is_disjoint_not_an_error(monkeypatch, tmp_path):
    # Windows: commonpath(("C:\\a", "D:\\b")) raises ValueError. That pair is
    # disjoint; a backup to another drive must not die with ValueError.
    def commonpath(paths):
        raise ValueError("Paths don't have the same drive")

    from safeforge.offline_backup import _inside

    monkeypatch.setattr(os.path, "commonpath", commonpath)
    assert _inside(tmp_path / "a", tmp_path / "b") is False


# -- SF-R12-02 macOS F_FULLFSYNC ---------------------------------------------

class _FakeFcntl(types.ModuleType):
    F_FULLFSYNC = 51

    def __init__(self, refuse: bool):
        super().__init__("fcntl")
        self.refuse = refuse
        self.calls = []

    def fcntl(self, fd, cmd):
        self.calls.append(cmd)
        if self.refuse:
            raise OSError(45, "Operation not supported")
        return 0


@pytest.mark.parametrize("refuse", [False, True])
def test_darwin_fsync_uses_f_fullfsync_and_reports_fallback(tmp_path, monkeypatch, refuse):
    fake = _FakeFcntl(refuse)
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(durable.sys, "platform", "darwin")
    fsyncs = []
    real = os.fsync
    monkeypatch.setattr(durable.os, "fsync", lambda fd: (fsyncs.append(fd), real(fd))[1])
    with open(tmp_path / "f", "wb") as f:
        f.write(b"x")
        status = durable.fsync_file(f)
    assert fake.calls == [fake.F_FULLFSYNC]
    if refuse:
        assert status == "degraded:darwin-F_FULLFSYNC-refused-fsync-only"
        assert len(fsyncs) == 1  # fell back to fsync, like SQLite
    else:
        assert status == "ok"
        assert fsyncs == []


@pytest.mark.skipif(sys.platform != "darwin", reason="real F_FULLFSYNC only on macOS")
def test_real_macos_f_fullfsync_on_file_and_directory(tmp_path):
    with open(tmp_path / "f", "wb") as f:
        f.write(b"x")
        assert durable.fsync_file(f) == "ok"
    assert durable.fsync_dir(str(tmp_path)) == "ok"


def _fullfsync(conn) -> int:
    return conn.execute("PRAGMA fullfsync").fetchone()[0]


@pytest.mark.parametrize("platform,sync,expected", [
    ("darwin", "FULL", 1), ("darwin", "EXTRA", 1), ("darwin", "NORMAL", 0), ("linux", "FULL", 0),
])
def test_connect_sets_sqlite_fullfsync_on_macos(tmp_path, monkeypatch, platform, sync, expected):
    monkeypatch.setattr(db.sys, "platform", platform)
    conn = connect(str(tmp_path / "t.db"), synchronous=sync)
    try:
        assert _fullfsync(conn) == expected
    finally:
        conn.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS SQLite build")
def test_real_macos_connect_has_fullfsync(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    try:
        assert _fullfsync(conn) == 1
    finally:
        conn.close()


def test_report_surfaces_degraded_file_fsync(tmp_path, monkeypatch):
    real = durable.fsync_file
    monkeypatch.setattr(durable, "fsync_file",
                        lambda f: (real(f), "degraded:darwin-F_FULLFSYNC-refused-fsync-only")[1])
    report = backup_offline(_store(tmp_path), tmp_path / "archive", confirmed_quiescent=True)
    assert report["file_fsync"] == "degraded:darwin-F_FULLFSYNC-refused-fsync-only"
    assert report["verdict"] == "offline_snapshot_integrity_pass"


def test_nested_summary_is_not_duplicated():
    from safeforge.offline_backup import _summary

    assert _summary(["unsupported:a;unsupported:b", "unsupported:a", "ok"]) == "unsupported:a;unsupported:b"
    assert _summary(["ok", "ok"]) == "ok"


# -- SF-R12-03 coordination --------------------------------------------------

def _coordinated(base):
    root = base / "source"
    with ObjectStore(str(root), enforce_private=True) as s:
        s.ingest(io.BytesIO(b"123"), max_bytes=10)
    initialize(root, externally_quiescent=True)
    return root


@LINUX
def test_missing_o_nofollow_refuses_instead_of_weakening(tmp_path, monkeypatch):
    root = _coordinated(tmp_path)
    monkeypatch.delattr(coordination.os, "O_NOFOLLOW")
    with pytest.raises(CoordinationRejected, match="O_NOFOLLOW is required"):
        with lock(root, exclusive=False):
            pass


@LINUX
@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_lock_file_group_or_other_bits_rejected(tmp_path, mode):
    root = _coordinated(tmp_path)
    (root / LOCK).chmod(mode)
    with pytest.raises(CoordinationRejected, match="lock must be owned by us"):
        with lock(root, exclusive=False):
            pass


@LINUX
@pytest.mark.parametrize("how", ["chown", "fstat-shim"])
def test_lock_file_foreign_owner_rejected(tmp_path, monkeypatch, how):
    root = _coordinated(tmp_path)
    path = root / LOCK
    if how == "chown":
        if os.geteuid() != 0:
            pytest.skip("a real foreign owner needs chown (root); fstat-shim covers the comparison")
        os.chown(path, 65534, -1)
    else:
        lock_ino = os.stat(path).st_ino
        real = os.fstat

        def fstat(fd):
            st = real(fd)
            if st.st_ino == lock_ino:
                fields = list(st)
                fields[4] = st.st_uid + 1
                return os.stat_result(fields)
            return st

        monkeypatch.setattr(coordination.os, "fstat", fstat)
    with pytest.raises(CoordinationRejected, match="lock must be owned by us"):
        with lock(root, exclusive=False):
            pass


@LINUX
def test_ownership_is_checked_against_the_effective_uid(tmp_path, monkeypatch):
    # Files are created with the effective uid. A process whose real uid
    # differs (setuid helper) must still accept the store it initialized.
    root = _coordinated(tmp_path)
    monkeypatch.setattr(coordination.os, "getuid", lambda: os.geteuid() + 1)
    with lock(root, exclusive=False):
        pass


@LINUX
def test_uncoordinated_connect_through_dotdot_alias_is_refused(tmp_path):
    # At fe123ab connect() checked initialized() on the abspath spelling, so a
    # plain (uncoordinated) writer reached a coordinated database through
    # p/a/../source while p/source was an ordinary directory.
    victim = _coordinated(tmp_path / "e")
    (tmp_path / "p" / "source").mkdir(parents=True, mode=0o700)
    (tmp_path / "e" / "deep").mkdir()
    (tmp_path / "p" / "a").symlink_to(tmp_path / "e" / "deep", target_is_directory=True)
    alias = os.path.join(str(tmp_path / "p" / "a"), "..", "source", "safeforge.db")
    assert os.path.realpath(alias) == str(victim / "safeforge.db")
    with pytest.raises(ConfigurationError, match="coordinated store"):
        connect(alias)
    assert not (tmp_path / "p" / "source" / "safeforge.db").exists()
