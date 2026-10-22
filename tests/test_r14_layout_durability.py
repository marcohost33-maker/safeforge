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
"""

from __future__ import annotations

import os
import sys

import pytest

from safeforge import ObjectStore, durable

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no directory fsync")


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
        assert all(v == "ok" or v.startswith("degraded:") for v in s.layout_sync.values())
    assert str(root.parent) in synced  # root's own entry
    assert str(root) in synced  # objects/ and staging/ entries


def test_external_staging_parent_is_synced(tmp_path, synced):
    root, staging = tmp_path / "store", tmp_path / "spool" / "staging"
    os.makedirs(staging.parent)
    with ObjectStore(str(root), staging_dir=str(staging)) as s:
        assert str(staging.parent) in s.layout_sync
    assert str(staging.parent) in synced


def test_existing_layout_adds_no_syncs(tmp_path, synced):
    root = tmp_path / "store"
    ObjectStore(str(root)).close()
    synced.clear()
    with ObjectStore(str(root)) as s:
        assert s.layout_sync == {}
    assert synced == []


def test_unopenable_parent_is_reported_not_hidden(tmp_path, monkeypatch):
    def refuse(path: str) -> str:
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(durable, "fsync_dir", refuse)
    with ObjectStore(str(tmp_path / "store")) as s:
        assert s.layout_sync and all(v.startswith("failed:") for v in s.layout_sync.values())
