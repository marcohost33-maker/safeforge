from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([SRC, REPO, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def run_crash(design: str, root: str, point: str, nbytes: int = 65536, staging: str | None = None):
    """Run one ingest in a child process that dies at ``point``. Returns (returncode, acked_id|None, stderr)."""
    args = [sys.executable, "-m", "tests.crash_driver", design, root, point, str(nbytes)]
    if staging:
        args.append(staging)
    p = subprocess.run(args, cwd=REPO, env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    ack = None
    for line in p.stdout.splitlines():
        if line.startswith("ACK "):
            ack = line.split()[1]
    return p.returncode, ack, p.stderr


@pytest.fixture(autouse=True)
def _pin_fixed_sqlite(request, monkeypatch):
    """SF-R35-01: the durability flag is False on every SQLite without the WAL-reset fix,
    which today is every python.org build and Ubuntu 24.04. Pin the library as fixed so
    the True path stays covered on each runner; ``real_sqlite`` tests see the real one,
    and tests/test_r35_wal_library_prerequisite.py proves the flag drops when it is not."""
    if request.node.get_closest_marker("real_sqlite"):
        yield
        return
    from safeforge import sqlite_runtime

    real = sqlite_runtime.loaded_sqlite_status

    def pinned(conn=None):
        st = dict(real(conn))
        st["fixed"] = True
        st["reason"] = "pinned fixed by tests/conftest.py; real: " + st["reason"]
        return st

    monkeypatch.setattr(sqlite_runtime, "loaded_sqlite_status", pinned)
    yield


@pytest.fixture(autouse=True)
def _clear_failpoints():
    from safeforge import _faults

    _faults.set_hook(None)
    yield
    _faults.set_hook(None)


INJECTED_LOCAL = {"fstype": "ext4", "class": "local", "source": "test-injected"}


@pytest.fixture
def injected_local_filesystem(monkeypatch):
    """Test seam: every layout path is classified ``local`` (``source: "test-injected"``).

    For tests whose subject is ANOTHER durability prerequisite (syncs, SQLite
    policy/library, the ancestor walk): their result must not depend on the
    filesystem under tmp_path. The GitHub-hosted ubuntu-24.04 root disk is
    mounted ``nobarrier`` (observed 2026-11-01, CI mounts job 125000197175), so
    tmp_path there is ``unknown`` since SF-R42-01. A test that needs a REAL local
    filesystem runs in the CI job ``mounts`` on ``$RUNNER_TEMP/mnt/ext4-barrier``.
    Requested by name (usefixtures), never autouse.
    """
    from safeforge import filesystems

    def local(paths, **kw):
        return {p: dict(INJECTED_LOCAL) for p in paths}

    monkeypatch.setattr(filesystems, "filesystems", local)


def run_powerloss(root: str, point: str, mutant: str, trace: str, nbytes: int = 65536):
    """Run tests.powerloss_driver in a child. Returns (returncode, acked_id|None, stderr)."""
    args = [sys.executable, "-m", "tests.powerloss_driver", root, point, mutant, trace, str(nbytes)]
    p = subprocess.run(args, cwd=REPO, env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    ack = None
    for line in p.stdout.splitlines():
        if line.startswith("ACK "):
            ack = line.split()[1]
    return p.returncode, ack, p.stderr
