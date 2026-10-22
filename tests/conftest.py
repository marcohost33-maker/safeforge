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
def _clear_failpoints():
    from safeforge import _faults

    _faults.set_hook(None)
    yield
    _faults.set_hook(None)


def run_powerloss(root: str, point: str, mutant: str, trace: str, nbytes: int = 65536):
    """Run tests.powerloss_driver in a child. Returns (returncode, acked_id|None, stderr)."""
    args = [sys.executable, "-m", "tests.powerloss_driver", root, point, mutant, trace, str(nbytes)]
    p = subprocess.run(args, cwd=REPO, env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    ack = None
    for line in p.stdout.splitlines():
        if line.startswith("ACK "):
            ack = line.split()[1]
    return p.returncode, ack, p.stderr
