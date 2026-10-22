"""SF-R14-01: directories the store creates must themselves be made durable.

``ObjectStore.__init__`` creates root/, objects/ and staging/. A new directory
entry lives in its PARENT; until the parent is fsync'ed a power cut can
remove it -- and with it every object later published inside, acknowledged or
not. publish() fsyncs objects/ and staging/, never their parents. (SQLite
happens to fsync the database's directory when it creates the WAL, which
covers objects/ and staging/ by accident but never root's own entry in its
parent.)

Spy test: which directories does __init__ fsync? It cannot observe a real
power cut; the dir-model and LazyFS suites cover the per-ingest steps.

SF-R25-01 changed the contract: a failed parent fsync no longer opens a store
with ``"failed:..."`` in ``layout_sync``; it raises ``LayoutNotDurable``. And
every open re-syncs the layout parents, not only the open that created them
(tests/test_r25_layout_ack_gate.py). The spy only records which directories
are synced, so these tests run on Windows too, where the real status is
``"unsupported:..."``.
"""

from __future__ import annotations

import os
import sys

import pytest

from safeforge import ObjectStore, durable
from safeforge.store import LayoutNotDurable


@pytest.fixture
def synced(monkeypatch):
    calls: list[str] = []
    real = durable.fsync_dir

    def spy(path: str) -> str:
        calls.append(os.path.abspath(path))
        return real(path)

    monkeypatch.setattr(durable, "fsync_dir", spy)
    return calls


def test_fresh_store_syncs_parents_of_created_directories(tmp_path, synced):
    root = tmp_path / "a" / "store"
    os.makedirs(root.parent)
    with ObjectStore(str(root)) as s:
        assert set(s.layout_sync) == {str(root.parent), str(root)}
        if sys.platform == "win32":
            assert all(v.startswith("unsupported:") for v in s.layout_sync.values())
        else:
            assert all(v == "ok" or v.startswith("degraded:") for v in s.layout_sync.values())
    assert str(root.parent) in synced  # root's own entry
    assert str(root) in synced  # objects/ and staging/ entries


def test_external_staging_parent_is_synced(tmp_path, synced):
    root, staging = tmp_path / "store", tmp_path / "spool" / "staging"
    os.makedirs(staging.parent)
    with ObjectStore(str(root), staging_dir=str(staging)) as s:
        assert str(staging.parent) in s.layout_sync
    assert str(staging.parent) in synced


def test_existing_layout_is_resynced_on_reopen(tmp_path, synced):
    # Was test_existing_layout_adds_no_syncs (R14). SF-R25-01: reopen must
    # re-prove the layout, otherwise a failure or crash before the first sync
    # is forgotten (created == [] on reopen).
    root = tmp_path / "store"
    ObjectStore(str(root)).close()
    synced.clear()
    with ObjectStore(str(root)) as s:
        assert set(s.layout_sync) == {str(tmp_path), str(root)}
    assert set(synced) == {str(tmp_path), str(root)}


def test_unopenable_parent_refuses_open_and_reports(tmp_path, monkeypatch):
    # Was test_unopenable_parent_is_reported_not_hidden (R14): the "failed:"
    # status stays visible, now on the exception instead of an opened store.
    calls: list[str] = []

    def refuse(path: str) -> str:
        calls.append(path)
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(durable, "fsync_dir", refuse)
    with pytest.raises(LayoutNotDurable) as exc:
        ObjectStore(str(tmp_path / "store"))
    assert calls
    assert exc.value.layout_sync and all(v.startswith("failed:") for v in exc.value.layout_sync.values())
