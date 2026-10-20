"""Coordinated mode and atomic restore must fail closed where they are unsupported.

The R8/R9 coordination tests are Linux-only and skip elsewhere. These tests
cover the other side on non-POSIX platforms (Windows): no advisory-lock
emulation, no silent fallback to an uncoordinated store, no rename fallback.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from safeforge import ConfigurationError, IngestError, ObjectStore, connect
from safeforge.atomic_restore import restore_atomic
from safeforge.coordination import MARKER_CONTENT, MODE, CoordinationRejected, initialize, lock
from safeforge.offline_backup import BackupRejected, backup_offline

pytestmark = pytest.mark.skipif(os.name == "posix", reason="non-POSIX fail-closed path; POSIX is covered by R8/R9 tests")


def _plain_store(tmp_path):
    root = tmp_path / "store"
    with ObjectStore(str(root)) as s:
        s.ingest(io.BytesIO(b"payload"), max_bytes=100)
    return root


def test_initialize_refuses_without_posix_flock(tmp_path):
    root = _plain_store(tmp_path)
    with pytest.raises(CoordinationRejected, match="POSIX flock"):
        initialize(root, externally_quiescent=True)
    assert not (root / MODE).exists()
    # Positive control: the store stays usable in uncoordinated mode.
    with ObjectStore(str(root)) as s:
        assert s.verify() == []


def test_coordinated_objectstore_without_marker_is_a_mode_mismatch(tmp_path):
    root = _plain_store(tmp_path)
    with pytest.raises(IngestError, match="coordination mode mismatch"):
        ObjectStore(str(root), coordinated=True)


def test_foreign_marker_is_never_silently_ignored(tmp_path):
    # A store initialized on Linux and copied here: every path must refuse.
    root = _plain_store(tmp_path)
    (root / MODE).write_bytes(MARKER_CONTENT)
    with pytest.raises(IngestError, match="coordination mode mismatch"):
        ObjectStore(str(root))
    with pytest.raises(CoordinationRejected, match="POSIX flock"):
        ObjectStore(str(root), coordinated=True)
    with pytest.raises(ConfigurationError, match="coordinated store"):
        connect(str(root / "safeforge.db"))
    with pytest.raises(CoordinationRejected, match="POSIX flock"):
        with lock(root, exclusive=False):
            pass


@pytest.mark.skipif(sys.platform == "linux", reason="Linux publishes via renameat2")
def test_atomic_restore_refuses_without_renameat2(tmp_path):
    root = _plain_store(tmp_path)
    archive = tmp_path / "archive"
    backup_offline(root, archive, confirmed_quiescent=True)
    target = tmp_path / "restored"
    with pytest.raises(BackupRejected, match="Linux-only"):
        restore_atomic(archive, target, confirmed_quiescent=True)
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.restore-*"))
