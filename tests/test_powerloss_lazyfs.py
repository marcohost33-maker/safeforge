"""Power cut, data half: crash the writer, then drop every unsynced byte (LazyFS).

Runs only where a LazyFS binary is configured (``SAFEFORGE_LAZYFS_BIN``,
Linux with FUSE 3; experiments/lazyfs/build_lazyfs.sh builds the pinned
version). The CI job that builds LazyFS sets ``SAFEFORGE_LAZYFS_REQUIRED=1``
so that a broken setup fails instead of skipping.

Sequence per case: fresh store on the LazyFS mount -> clear-cache (baseline)
-> child ingests and dies at the failpoint (or acknowledges) -> clear-cache
(the power cut) -> reconcile -> invariants.

What this adds over tests/test_fault_injection.py: a process crash keeps the
OS page cache, so bytes that were written but never fsync'ed still reach the
disk. Here they do not -- including SQLite's WAL frames. What it does NOT
cover: lost directory entries (LazyFS persists them at once; see
test_powerloss_dirmodel.py), reordered or torn device writes, drives that
ignore flushes.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

import pytest

from safeforge import ObjectStore
from tests import invariants as inv
from tests.conftest import run_powerloss
from tests.lazyfs_harness import ENV_BIN, LazyFS
from tests.powerloss_driver import CRASH_EXIT

PAYLOAD = 65536
LATER = 10.0

# point -> status of the single row after power cut + reconcile (None = no row).
# Identical to the process-crash matrix in test_fault_injection.py: the fsyncs
# make the power cut indistinguishable from a process crash.
EXPECTED = {
    "staging.opened": None,
    "staging.chunk": None,
    "staging.written": None,  # unsynced staging bytes are gone, the row was never written
    "staging.fsynced": None,
    "intent.committed": "failed",
    "object.published": "stored",
    "dirs.fsynced": "stored",
    "status.committed": "stored",
    "no-crash": "stored",
}


def _lazyfs_binary() -> str | None:
    b = os.environ.get(ENV_BIN)
    usable = bool(b) and sys.platform.startswith("linux") and os.path.exists("/dev/fuse") and os.access(b, os.X_OK)
    if not usable and os.environ.get("SAFEFORGE_LAZYFS_REQUIRED") == "1":
        pytest.fail("SAFEFORGE_LAZYFS_REQUIRED=1 but no usable LazyFS (%s=%r)" % (ENV_BIN, b))
    return b if usable else None


@pytest.fixture(scope="module")
def lazyfs(tmp_path_factory):
    binary = _lazyfs_binary()
    if binary is None:
        pytest.skip("set %s to a LazyFS binary (Linux + FUSE 3) to run power-loss tests" % ENV_BIN)
    fs = LazyFS(binary, str(tmp_path_factory.mktemp("lazyfs")))
    fs.start()
    yield fs
    fs.stop()


_counter = iter(range(10**6))


def _cut(lazyfs: LazyFS, point: str, mutant: str, tmp_path):
    root = os.path.join(lazyfs.mnt, "case%d" % next(_counter))
    ObjectStore(root).close()
    lazyfs.clear_cache()
    # Non-vacuity: an unsynced canary must be empty after the cut.
    canary = os.path.join(root, "canary")
    with open(canary, "wb") as f:
        f.write(b"x" * 4096)
    trace = str(tmp_path / "trace.jsonl")
    rc, ack, err = run_powerloss(root, point, mutant, trace, PAYLOAD)
    assert rc == (0 if point == "no-crash" else CRASH_EXIT), err
    lazyfs.clear_cache()
    assert os.path.getsize(canary) == 0, "clear-cache did not drop unsynced data"
    os.unlink(canary)
    with ObjectStore(root) as s:
        first = s.reconcile(grace_seconds=0, now=time.time() + LATER)
        second = s.reconcile(grace_seconds=0, now=time.time() + LATER)
    return root, ack, first, second


def _intact(root: str, oid: str) -> bool:
    data = (b"safeforge-" * (PAYLOAD // 10 + 1))[:PAYLOAD]
    p = os.path.join(root, "objects", oid)
    return os.path.isfile(p) and open(p, "rb").read() == data


@pytest.mark.parametrize("point", list(EXPECTED))
def test_shipped_writer_survives_power_cut(lazyfs, tmp_path, point):
    root, ack, _, second = _cut(lazyfs, point, "none", tmp_path)
    assert second.changes == 0
    lay = inv.corrected(root)
    assert inv.violations(lay) == []
    assert [st for st, _ in inv.rows(lay).values()] == ([] if EXPECTED[point] is None else [EXPECTED[point]])
    if ack is not None:
        assert inv.rows(lay)[ack][0] == "stored" and _intact(root, ack)


def test_writer_without_staging_fsync_acknowledges_lost_bytes(lazyfs, tmp_path):
    """Negative control: publish before fsync -> an acknowledged object comes back empty."""
    root, ack, first, _ = _cut(lazyfs, "no-crash", "no-staging-fsync", tmp_path)
    assert ack is not None and not _intact(root, ack)
    assert ack in first.missing_or_corrupt
    assert any(v.startswith("I1 hash mismatch") for v in inv.violations(inv.corrected(root)))


def test_synchronous_normal_loses_acknowledged_upload(lazyfs, tmp_path):
    """T7 under a real cut: WAL + NORMAL does not sync the WAL at commit.

    The intent and the 'stored' commit both vanish; the reconciler then sees
    an object without a row and deletes it as an orphan. The upload was
    acknowledged and is gone -- this is why ObjectStore defaults to FULL.
    """
    root, ack, first, _ = _cut(lazyfs, "no-crash", "sync-normal", tmp_path)
    assert ack is not None
    assert ack not in inv.rows(inv.corrected(root))
    assert first.deleted_objects == 1
    assert not os.path.exists(os.path.join(root, "objects", ack))


def test_harness_payload_matches_driver():
    """The intact-check payload must be the driver's payload, or every check passes vacuously."""
    from tests import powerloss_driver

    src = open(powerloss_driver.__file__, encoding="utf-8").read()
    assert 'data = (b"safeforge-" * (nbytes // 10 + 1))[:nbytes]' in src
    assert hashlib.sha256((b"safeforge-" * (PAYLOAD // 10 + 1))[:PAYLOAD]).hexdigest()
