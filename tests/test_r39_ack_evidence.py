"""SF-R39-02: the ACK names the evidence behind ``durability_prerequisites_ok``.

Question 3 of the 2026-10-29 second round (007acc2, session 6dc40639): is "keep
walking without proof" (macOS/BSD, basis ``st_dev-only``) fail-safe, or does
the flag need its own basis? Answer tested here:

* fail-safe for durability: without a mount proof the walk goes on, so the
  set of synced parents is a SUPERSET of what a mount-aware probe syncs on the
  same tree. Shown on one tree with a simulated same-device bind mount: the
  mountinfo probe stops at it, ``st_dev-only`` syncs its outer parent too, and
  both reach the same flag. The cost is availability (an unreadable outer
  ancestor refuses the open, SF-R25-01), never a missing sync.
* so the flag keeps its meaning, and the ACK reports the basis instead of
  folding it in: ``IngestResult.layout_mount_basis`` plus the SQLite library
  behind the store (``sqlite_version``, ``sqlite_wal_reset_fixed``), the
  2026-10-27 recommendation (Claude R28-R34 review, section 5).
* a deduplicated result attests nothing: all three stay empty/False.
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys

import pytest

from safeforge import ObjectStore, mounts
from safeforge.sqlite_runtime import loaded_sqlite_status

POSIX_SYNC = sys.platform.startswith("linux")


def _ingest(root: str, payload: bytes = b"evidence", key: str | None = None):
    with ObjectStore(root) as s:
        r = s.ingest(io.BytesIO(payload), max_bytes=1 << 20, idempotency_key=key)
        return s, r


def test_ack_reports_the_store_basis_and_library(tmp_path):
    s, r = _ingest(str(tmp_path / "root"))
    assert r.layout_mount_basis == s.layout_mount_basis != ""
    assert r.sqlite_version == s.sqlite_runtime["version"] == sqlite3.sqlite_version
    assert r.sqlite_wal_reset_fixed is (s.sqlite_runtime["fixed"] is True)
    if r.durability_prerequisites_ok:
        assert r.sqlite_wal_reset_fixed is True


def test_deduplicated_ack_attests_nothing(tmp_path):
    root = str(tmp_path / "root")
    _ingest(root, key="k1")
    _s, again = _ingest(root, key="k1")
    assert again.deduplicated is True
    assert (again.layout_mount_basis, again.sqlite_version, again.sqlite_wal_reset_fixed) == ("", "", False)
    assert again.durability_prerequisites_ok is False


@pytest.mark.real_sqlite
def test_ack_reports_the_real_library_and_an_affected_one_never_claims_durability(tmp_path):
    _s, r = _ingest(str(tmp_path / "root"))
    real = loaded_sqlite_status()
    assert r.sqlite_version == real["version"]
    assert r.sqlite_wal_reset_fixed is real["fixed"]
    if not real["fixed"]:
        assert r.durability_prerequisites_ok is False


def _layout(monkeypatch, root: str, make_probe):
    monkeypatch.setattr(mounts, "probe", lambda start, **kw: make_probe())
    with ObjectStore(root) as s:
        r = s.ingest(io.BytesIO(b"walk"), max_bytes=1 << 20)
        return dict(s.layout_sync), r


def test_st_dev_only_walk_syncs_a_superset_of_a_mount_aware_walk(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    root = outer / "a" / "root"
    outer.mkdir()
    outer_real = os.path.realpath(outer)
    parent_of_outer = os.path.dirname(outer_real)

    def mount_aware():
        # A same-device bind mount at outer/: only mount identity can see it (R37).
        return mounts.MountProbe("mountinfo", stat_dev=mounts._stat_dev, mount_points={outer_real: mounts._stat_dev(outer_real)})

    def blind():
        return mounts.MountProbe("st_dev-only", stat_dev=mounts._stat_dev)

    aware_sync, aware_ack = _layout(monkeypatch, str(root), mount_aware)
    blind_sync, blind_ack = _layout(monkeypatch, str(root), blind)

    # The mount-aware walk stops at outer/: the parent of outer/ is not ours to sync.
    assert parent_of_outer not in aware_sync
    assert outer_real in aware_sync
    # Without proof the walk goes on: every parent synced above, and more.
    assert set(aware_sync) < set(blind_sync)
    assert parent_of_outer in blind_sync
    assert (aware_ack.layout_mount_basis, blind_ack.layout_mount_basis) == ("mountinfo", "st_dev-only")
    # Same flag either way: the basis is reported, it does not lower the claim.
    assert aware_ack.durability_prerequisites_ok == blind_ack.durability_prerequisites_ok
    if POSIX_SYNC:
        assert blind_ack.durability_prerequisites_ok is True
