"""SF-R36-01..03: three Codex findings on 4f7404d, each a positive durability report
that was not earned.

* R36-01: an earlier open created ``/existing/new/store`` and failed to sync
  ``/existing``; on retry nothing is "created", so ``/existing`` was never
  synced again and the retry ACKed.
* R36-02: ``_file_copy_verified`` returned the fsync status before re-checking
  the source, so a source changed after its bytes were read passed.
* R36-03: ``abspath()`` collapses ``link/..`` lexically; SQLite follows the link
  first and opens a database in a directory that was never synced.

Faults are injected through ``durable.fsync_dir`` / ``durable.fsync_file`` as in
test_r25_layout_ack_gate.py; every test asserts that its injection ran.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
from pathlib import Path

import pytest

from safeforge import offline_backup
from safeforge import store as store_mod
from safeforge.offline_backup import BackupRejected
from safeforge.store import IngestError, IngestResult, ObjectStore

durable = store_mod.durable


class FsyncDirFake:
    def __init__(self) -> None:
        self.failing: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        path = os.path.realpath(path)
        self.calls.append(path)
        if path in self.failing:
            raise OSError(errno.EIO, "injected fsync failure", path)
        return "ok"


@pytest.fixture
def fake(monkeypatch):
    f = FsyncDirFake()
    monkeypatch.setattr(durable, "fsync_dir", f)
    return f


def _open_and_ingest(root: str, **kw) -> IngestResult | IngestError:
    try:
        with ObjectStore(root, **kw) as s:
            return s.ingest(io.BytesIO(b"payload"), max_bytes=1024)
    except IngestError as e:
        return e


# ---- R36-01 -------------------------------------------------------------------


def test_r36_01_retry_reproves_an_ancestor_created_by_a_failed_open(tmp_path, fake):
    existing = tmp_path / "existing"
    existing.mkdir()
    root = str(existing / "new" / "store")
    fake.failing.add(os.path.realpath(existing))

    first = _open_and_ingest(root)
    assert isinstance(first, store_mod.LayoutNotDurable), first
    assert os.path.isdir(existing / "new"), "precondition: the failed open left new/ behind"

    fake.calls.clear()
    retry = _open_and_ingest(root)
    assert os.path.realpath(existing) in fake.calls, "the retry never re-synced /existing"
    assert isinstance(retry, store_mod.LayoutNotDurable), "retry ACKed with /existing unproven: %r" % (retry,)
    assert retry.layout_sync[os.path.realpath(existing)].startswith("failed:")


def test_r36_01_retry_opens_once_the_ancestor_sync_succeeds(tmp_path, fake):
    """Negative control: the same layout opens and ACKs when nothing fails."""
    existing = tmp_path / "existing"
    existing.mkdir()
    root = str(existing / "new" / "store")
    fake.failing.add(os.path.realpath(existing))
    assert isinstance(_open_and_ingest(root), store_mod.LayoutNotDurable)

    fake.failing.clear()
    outcome = _open_and_ingest(root)
    assert isinstance(outcome, IngestResult), outcome
    assert outcome.durability_prerequisites_ok is True


# ---- R36-02 -------------------------------------------------------------------


def _source(tmp_path: Path, data: bytes = b"object-bytes") -> tuple[Path, str, int]:
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    return src, hashlib.sha256(data).hexdigest(), len(data)


def test_r36_02_source_changed_during_fsync_is_rejected(tmp_path, monkeypatch):
    src, sha, size = _source(tmp_path)
    ran = []

    def fsync_and_mutate(f):
        ran.append(True)
        with open(src, "r+b") as g:
            g.write(b"X")
        return "ok"

    monkeypatch.setattr(offline_backup.durable, "fsync_file", fsync_and_mutate)
    with pytest.raises(BackupRejected, match="source object changed"):
        offline_backup._file_copy_verified(src, tmp_path / "dst.bin", sha, size)
    assert ran, "the injected fsync never ran"


def test_r36_02_unchanged_source_returns_the_fsync_status(tmp_path, monkeypatch):
    """Negative control: an untouched source still copies and reports its fsync status."""
    src, sha, size = _source(tmp_path)
    monkeypatch.setattr(offline_backup.durable, "fsync_file", lambda f: "degraded:test")
    assert offline_backup._file_copy_verified(src, tmp_path / "dst.bin", sha, size) == "degraded:test"
    assert (tmp_path / "dst.bin").read_bytes() == src.read_bytes()


# ---- R36-03 -------------------------------------------------------------------


def _link_layout(tmp_path: Path) -> tuple[Path, Path]:
    """alias/link -> actual/child; "alias/link/.." is actual/, lexically alias/."""
    alias, child = tmp_path / "alias", tmp_path / "actual" / "child"
    alias.mkdir()
    child.mkdir(parents=True)
    try:
        os.symlink(child, alias / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot create a directory symlink here: %s" % exc)
    return alias, tmp_path / "actual"


def test_r36_03_database_directory_is_resolved_like_the_kernel(tmp_path, fake):
    alias, actual = _link_layout(tmp_path)
    db_path = os.path.join(str(alias), "link", os.pardir, "meta.db")
    fake.failing.add(os.path.realpath(actual))

    outcome = _open_and_ingest(str(tmp_path / "root"), db_path=db_path)
    assert os.path.exists(actual / "meta.db"), "precondition: SQLite opened actual/meta.db"
    assert os.path.realpath(actual) in fake.calls, "the database's real directory was never synced"
    assert isinstance(outcome, store_mod.LayoutNotDurable), outcome


def test_r36_03_coordinated_mode_refuses_a_database_outside_root_behind_a_link(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere" / "child"
    elsewhere.mkdir(parents=True)
    try:
        os.symlink(elsewhere, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot create a directory symlink here: %s" % exc)
    db_path = os.path.join(str(root), "link", os.pardir, "safeforge.db")
    with pytest.raises(IngestError, match="coordinated mode requires database inside store root"):
        ObjectStore(str(root), db_path=db_path, coordinated=True)


def test_r36_03_plain_database_path_inside_root_still_opens(tmp_path, fake):
    """Negative control: the default layout is unaffected."""
    outcome = _open_and_ingest(str(tmp_path / "root"))
    assert isinstance(outcome, IngestResult), outcome
