"""Power cut, directory-entry half: every post-crash directory state the model allows.

For each crash point the child ingests once and records which directory
entries were durable (tests/powerloss_driver.py). tests/powerloss_model.py
then enumerates every combination of entries that a power cut could leave in
objects/ and staging/; each combination is materialised in a fresh copy and
reconciled. The data half (lost unsynced bytes, SQLite WAL) is tested with
LazyFS in test_powerloss_lazyfs.py.

Positive control: the shipped writer survives every state at every point.
Negative control: a writer without the directory fsyncs loses an
ACKNOWLEDGED upload in at least one state -- which LazyFS cannot show,
because it persists directory operations immediately.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

import pytest

from safeforge import ObjectStore, durable
from tests import invariants as inv
from tests import powerloss_model as model
from tests.conftest import run_powerloss
from tests.powerloss_driver import CRASH_EXIT

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows publishes with MoveFileExW and has no directory fsync to model"
)

LATER = 10.0
RELS = ("objects", "staging")
PAYLOAD = 65536

# point -> number of post-crash directory states for one ingest into a fresh,
# durable store. Pins the model against silently enumerating nothing.
STATES = {
    "staging.opened": 2,  # staging/<id>.part kept or lost
    "staging.chunk": 2,
    "staging.written": 2,
    "staging.fsynced": 2,
    "intent.committed": 2,
    "object.published": 4,  # objects/<id> kept or lost  x  staging/<id>.part back or not
    "dirs.fsynced": 1,
    "status.committed": 1,
    "no-crash": 1,
}


def _payload_sha() -> str:
    data = (b"safeforge-" * (PAYLOAD // 10 + 1))[:PAYLOAD]
    return hashlib.sha256(data).hexdigest()


def _fresh_store(root: str) -> dict[str, frozenset[str]]:
    ObjectStore(root).close()
    for d in (*(os.path.join(root, r) for r in RELS), root):
        durable.fsync_dir(d)
    return {r: frozenset(os.listdir(os.path.join(root, r))) for r in RELS}


def _explore(tmp_path, point: str, mutant: str) -> tuple[int, list[str]]:
    root = str(tmp_path / "crashed")
    baseline = _fresh_store(root)
    trace = str(tmp_path / "trace.jsonl")
    rc, ack, err = run_powerloss(root, point, mutant, trace, PAYLOAD)
    assert rc == (0 if point == "no-crash" else CRASH_EXIT), err
    assert (ack is not None) == (point == "no-crash"), err
    failures: list[str] = []
    all_states = model.states(root, trace, RELS, baseline)
    for i, state in enumerate(all_states):
        copy = str(tmp_path / ("state%d" % i))
        model.materialize(root, copy, state)
        label = " ".join("%s=%s" % (c.rel, sorted(c.present)) for c in state)
        with ObjectStore(copy) as s:
            s.reconcile(grace_seconds=0, now=time.time() + LATER)
            if s.reconcile(grace_seconds=0, now=time.time() + LATER).changes:
                failures.append("not idempotent: " + label)
        lay = inv.corrected(copy)
        failures += ["%s: %s" % (v, label) for v in inv.violations(lay)]
        if ack is not None:
            row = inv.rows(lay).get(ack)
            obj = os.path.join(lay.object_dir, ack)
            intact = os.path.isfile(obj) and hashlib.sha256(open(obj, "rb").read()).hexdigest() == _payload_sha()
            if row != ("stored", _payload_sha()) or not intact:
                failures.append("ACK LOST %s (row %r, file intact %s): %s" % (ack, row, intact, label))
    return len(all_states), failures


@pytest.mark.parametrize("point", list(STATES))
def test_shipped_writer_survives_every_directory_state(tmp_path, point):
    n, failures = _explore(tmp_path, point, "none")
    assert failures == []
    assert n == STATES[point]


@pytest.mark.parametrize("point", ["status.committed", "no-crash"])
def test_writer_without_dir_fsync_loses_data(tmp_path, point):
    n, failures = _explore(tmp_path, point, "no-dir-fsync")
    assert n > 1, "the mutant must leave non-durable entries to explore"
    assert any(f.startswith("I1 missing file") for f in failures), failures
    if point == "no-crash":
        assert any(f.startswith("ACK LOST") for f in failures), failures


def test_model_history_resets_at_fsync(tmp_path):
    trace = tmp_path / "t.jsonl"
    d = str(tmp_path)
    trace.write_text(
        '{"kind": "seen", "dir": "%s", "entries": ["a"]}\n'
        '{"kind": "fsync", "dir": "%s", "entries": ["b"]}\n'
        '{"kind": "seen", "dir": "%s", "entries": ["b", "c"]}\n' % (d, d, d)
    )
    durable_, seen = model.history(str(trace), d, frozenset())
    assert durable_ == {"b"} and seen == {"b", "c"}
    # current {"b"}: nothing to lose, "c" may come back -> 2 choices
    assert sorted(sorted(c.present) for c in model.choices("x", durable_, seen, frozenset({"b"}))) == [["b"], ["b", "c"]]
