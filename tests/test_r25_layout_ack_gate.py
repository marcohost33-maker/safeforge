"""SF-R25-01: a failed layout fsync must block every positive ACK, also after reopen.

R14 (SF-R14-01) made ``ObjectStore.__init__`` fsync the parents of the
directories it creates, but a failure was only *recorded* as
``layout_sync[parent] == "failed:<error>"``; the store then opened normally and
``ingest()`` acknowledged objects whose directory entries were never proven
durable. On reopen ``created == []`` erased even that record.

Contract tested here (variant (a), fail-closed at open):

* any parent fsync that raises -> ``ObjectStore(...)`` raises
  :class:`LayoutNotDurable` (an :class:`IngestError`), so no ingest, no
  idempotency replay, no other ACK path exists;
* every open re-fsyncs the parents of root/, objects/ and staging/, so a
  layout that was not proven durable in an earlier run is proven -- or refused
  -- again; the invariant survives a restart;
* platform statuses stay as they are and do not block, exactly as in the
  per-ingest publish path: ``"unsupported:..."`` (Windows) and
  ``"degraded:..."`` (macOS without F_FULLFSYNC). Only a raised ``OSError`` --
  reported as ``"failed:<error>"`` -- blocks.

Faults are injected by replacing ``durable.fsync_dir``; every test asserts
that its replacement was actually called for the targeted directory, so a
green result cannot come from a patch that never ran. The tests do not need a
directory fsync from the platform and therefore also run on Windows.
"""

from __future__ import annotations

import errno
import io
import os

import pytest

from safeforge import store as store_mod
from safeforge.store import IngestError, IngestResult, ObjectStore

durable = store_mod.durable


class FsyncDirFake:
    """Replacement for ``durable.fsync_dir``: records calls, fails chosen directories."""

    def __init__(self, status: str = "ok") -> None:
        self.status = status
        self.failing: set[str] = set()
        self.calls: list[str] = []
        self.failed_calls: list[str] = []

    def __call__(self, path: str) -> str:
        path = os.path.abspath(path)
        self.calls.append(path)
        if path in self.failing:
            self.failed_calls.append(path)
            raise OSError(errno.EIO, "injected fsync failure", path)
        return self.status


@pytest.fixture
def fake(monkeypatch):
    f = FsyncDirFake()
    monkeypatch.setattr(durable, "fsync_dir", f)
    return f


def _open_and_ingest(root: str, payload: bytes = b"payload", **kw) -> IngestResult | IngestError:
    """Open a store and ingest once; return the ACK or the refusal (never both)."""
    try:
        with ObjectStore(root) as s:
            return s.ingest(io.BytesIO(payload), max_bytes=1024, **kw)
    except IngestError as e:
        return e


def _assert_refused_as_layout_not_durable(outcome, parent: str) -> None:
    assert not isinstance(outcome, IngestResult), "ACK given although the layout fsync failed: %r" % (outcome,)
    assert isinstance(outcome, store_mod.LayoutNotDurable), outcome
    assert outcome.layout_sync[parent].startswith("failed:"), outcome.layout_sync


def _open_ingest_with_layout(root: str, payload: bytes) -> tuple[IngestResult | IngestError, dict | None]:
    """Like :func:`_open_and_ingest`, plus the layout report of the open (or of the refusal)."""
    try:
        with ObjectStore(root) as s:
            return s.ingest(io.BytesIO(payload), max_bytes=1024), dict(s.layout_sync)
    except IngestError as e:
        return e, getattr(e, "layout_sync", None)


def test_positive_control_healthy_layout_sync_acks(tmp_path, fake):
    # A refusal is returned, not raised, so a gate that blocks the healthy
    # path fails HERE as an assertion (the diskriminierung tool's evidence).
    outcome, layout = _open_ingest_with_layout(str(tmp_path / "store"), b"positive control")
    assert isinstance(outcome, IngestResult), "healthy layout fsync was refused: %r" % (outcome,)
    assert layout and all(v == "ok" for v in layout.values()), layout
    assert fake.calls, "fsync_dir fake was never called -- the patch did not take effect"
    assert str(tmp_path) in fake.calls  # root's own entry was synced
    with ObjectStore(str(tmp_path / "store")) as s:
        assert s.status_of(outcome.object_id) == "stored"


@pytest.mark.parametrize(
    "status", ["unsupported:windows-has-no-directory-fsync", "degraded:darwin-F_FULLFSYNC-refused-fsync-only"]
)
def test_platform_status_is_reported_and_does_not_block(tmp_path, fake, status):
    # Same treatment as publish(): IngestResult.dir_sync carries the status, the ACK happens.
    fake.status = status
    outcome, layout = _open_ingest_with_layout(str(tmp_path / "store"), b"payload")
    assert isinstance(outcome, IngestResult), "platform status %r was treated as a failure: %r" % (status, outcome)
    assert layout and all(v == status for v in layout.values()), layout
    assert outcome.dir_sync == status
    assert fake.calls


def test_new_layout_parent_fsync_failure_refuses_open_no_ack(tmp_path, fake):
    fake.failing.add(str(tmp_path))  # root's entry lives here
    outcome = _open_and_ingest(str(tmp_path / "store"))
    assert fake.failed_calls == [str(tmp_path)], "injected fault did not fire: %r" % fake.calls
    _assert_refused_as_layout_not_durable(outcome, str(tmp_path))


def test_failure_report_lists_every_parent_not_only_the_first(tmp_path, fake):
    # Report, never hide: the refusal carries all statuses, the failed one included.
    root = tmp_path / "store"
    fake.failing.add(str(root))  # objects/ and staging/ entries live here
    outcome = _open_and_ingest(str(root))
    _assert_refused_as_layout_not_durable(outcome, str(root))
    assert outcome.layout_sync[str(tmp_path)] == "ok"


def test_reopen_after_failed_layout_sync_still_refuses(tmp_path, fake):
    root = str(tmp_path / "store")
    fake.failing.add(str(tmp_path))
    first = _open_and_ingest(root)
    assert not isinstance(first, IngestResult), first
    assert os.path.isdir(os.path.join(root, "objects"))  # the layout exists now: reopen creates nothing
    fake.failed_calls.clear()
    second = _open_and_ingest(root, b"retry is still unsafe")
    assert fake.failed_calls == [str(tmp_path)], "reopen did not re-sync root's parent: %r" % fake.calls
    _assert_refused_as_layout_not_durable(second, str(tmp_path))


def test_reopen_reproves_layout_once_fsync_works(tmp_path, fake):
    # Reopen is where the gap was: created == [] synced nothing. Now each open
    # re-syncs the layout parents, so success after a failed run is a fresh proof.
    root = str(tmp_path / "store")
    fake.failing.add(str(tmp_path))
    assert not isinstance(_open_and_ingest(root), IngestResult)
    fake.failing.clear()
    fake.calls.clear()
    outcome = _open_and_ingest(root, b"after repair")
    assert isinstance(outcome, IngestResult), outcome
    assert {str(tmp_path), root} <= set(fake.calls), fake.calls


def test_idempotency_replay_is_not_acknowledged_without_layout_proof(tmp_path, fake):
    root = str(tmp_path / "store")
    first = _open_and_ingest(root, b"replayed", idempotency_key="k1")
    assert isinstance(first, IngestResult) and not first.deduplicated
    fake.failing.add(str(tmp_path))
    replay = _open_and_ingest(root, b"replayed", idempotency_key="k1")
    assert fake.failed_calls == [str(tmp_path)], fake.calls
    _assert_refused_as_layout_not_durable(replay, str(tmp_path))
    fake.failing.clear()
    again = _open_and_ingest(root, b"replayed", idempotency_key="k1")
    assert isinstance(again, IngestResult) and again.deduplicated and again.object_id == first.object_id


def test_external_staging_parent_failure_refuses(tmp_path, fake):
    root, staging = tmp_path / "store", tmp_path / "spool" / "staging"
    os.makedirs(staging.parent)
    fake.failing.add(str(staging.parent))
    with pytest.raises(store_mod.LayoutNotDurable) as exc:
        ObjectStore(str(root), staging_dir=str(staging))
    assert exc.value.layout_sync[str(staging.parent)].startswith("failed:")
    assert str(staging.parent) in fake.failed_calls
