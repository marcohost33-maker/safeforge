"""R11 (SF-P2-01): backup/restore reports say what the directory fsyncs did.

R10: ``backup_offline`` and ``restore_offline`` ignored the return value of
``durable.fsync_dir``. On Windows that value is
``"unsupported:windows-has-no-directory-fsync"``, so a report of
``offline_snapshot_integrity_pass`` could not be told apart from a run whose
directory entries were forced to disk. The integrity verdict stays; the
durability outcome is reported next to it, never folded into it.
"""

from __future__ import annotations

import io
import sys

import pytest

from safeforge import ObjectStore, durable
from safeforge.offline_backup import backup_offline, restore_offline

UNSUPPORTED = "unsupported:windows-has-no-directory-fsync"


def _store(tmp_path):
    src = tmp_path / "store"
    with ObjectStore(str(src)) as s:
        s.ingest(io.BytesIO(b"payload"), max_bytes=100)
    return src


def test_reports_carry_the_platforms_real_dir_sync_status(tmp_path):
    expected = UNSUPPORTED if sys.platform == "win32" else "ok"
    archive = tmp_path / "archive"
    backup = backup_offline(_store(tmp_path), archive, confirmed_quiescent=True)
    restore = restore_offline(archive, tmp_path / "restored", confirmed_quiescent=True)
    for report in (backup, restore):
        assert report["directory_fsync"] == expected
        assert report["power_loss_verified"] is False


@pytest.mark.parametrize("failing_call", [0, 1, 2])
def test_one_unsupported_dir_sync_is_never_reported_as_ok(tmp_path, monkeypatch, failing_call):
    # Each of the three directory fsyncs of a backup on its own; an "ok" that
    # aggregates only the last (or the first) call would pass two of these.
    real = durable.fsync_dir
    calls = []

    def fsync_dir(path):
        calls.append(path)
        return UNSUPPORTED if len(calls) - 1 == failing_call else real(path)

    src = _store(tmp_path)
    monkeypatch.setattr(durable, "fsync_dir", fsync_dir)
    report = backup_offline(src, tmp_path / "archive", confirmed_quiescent=True)
    assert len(calls) == 3
    assert report["directory_fsync"] == UNSUPPORTED
    assert report["verdict"] == "offline_snapshot_integrity_pass"  # integrity is a separate verdict


def test_restore_reports_unsupported_dir_sync(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    backup_offline(_store(tmp_path), archive, confirmed_quiescent=True)
    monkeypatch.setattr(durable, "fsync_dir", lambda path: UNSUPPORTED)
    report = restore_offline(archive, tmp_path / "restored", confirmed_quiescent=True)
    assert report["directory_fsync"] == UNSUPPORTED
    assert report["verdict"] == "offline_restore_copy_pass"


@pytest.mark.skipif(sys.platform != "linux", reason="atomic restore is Linux-only (renameat2)")
def test_atomic_restore_keeps_an_inner_unsupported_status(tmp_path, monkeypatch):
    from safeforge.atomic_restore import restore_atomic

    archive = tmp_path / "archive"
    backup_offline(_store(tmp_path), archive, confirmed_quiescent=True)
    real = durable.fsync_dir
    calls = []

    def fsync_dir(path):
        calls.append(path)
        return UNSUPPORTED if len(calls) == 1 else real(path)  # inside restore_offline

    monkeypatch.setattr(durable, "fsync_dir", fsync_dir)
    report = restore_atomic(archive, tmp_path / "restored", confirmed_quiescent=True)
    assert report["directory_fsync"] == UNSUPPORTED
    assert report["power_loss_verified"] is False
