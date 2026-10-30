"""SF-R38-01/02: two Codex findings on ac933f3.

* R38-01: a ``staging_dir`` outside ``root`` has its own ancestor chain. A failed
  open that created ``/external/new/spool/staging`` and could not sync
  ``/external`` left ``new/`` behind; on retry nothing is "created" and the
  SF-R36-01 walk covered only ``root``, so the retry ACKed with ``/external``
  never synced -- while the database in ``/external/new/spool`` depended on it.
* R38-02: ``parse_mountinfo`` scanned the whole table for every entry to drop
  covered mounts (quadratic; Codex measured ~10 s for 6,000 sibling mounts), and
  ``probe()`` parses on every open. The complexity is asserted by counting
  executed Python lines under ``sys.settrace`` against a budget linear in the
  table size -- deterministic per interpreter, never a wall-clock limit. The
  covering semantics are pinned by a differential test against the previous
  all-pairs algorithm, kept below verbatim as the oracle.

Faults are injected through ``durable.fsync_dir`` as in test_r36_codex_durability.py;
every store test asserts that its injection ran.
"""

from __future__ import annotations

import errno
import io
import os
import random
import re
import sys

import pytest

from safeforge import mounts
from safeforge import store as store_mod
from safeforge.mounts import parse_mountinfo, parse_mountinfo_filesystems
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


# ---- R38-01: the external staging chain is re-proven on retry --------------------


def _external_layout(tmp_path):
    """Codex's layout: root elsewhere, staging and database under /external/new/spool."""
    external = tmp_path / "external"
    external.mkdir()
    spool = external / "new" / "spool"
    return (
        str(tmp_path / "root"),
        {"staging_dir": str(spool / "staging"), "db_path": str(spool / "meta.db")},
        os.path.realpath(external),
    )


def test_r38_01_retry_reproves_an_external_staging_ancestor(tmp_path, fake):
    root, kw, external = _external_layout(tmp_path)
    fake.failing.add(external)

    first = _open_and_ingest(root, **kw)
    assert isinstance(first, store_mod.LayoutNotDurable), first
    assert os.path.isdir(os.path.join(external, "new")), "precondition: the failed open left new/ behind"

    fake.calls.clear()
    retry = _open_and_ingest(root, **kw)
    assert external in fake.calls, "the retry never re-synced the external staging ancestor"
    assert isinstance(retry, store_mod.LayoutNotDurable), "retry ACKed with /external unproven: %r" % (retry,)
    assert retry.layout_sync[external].startswith("failed:")


def test_r38_01_external_staging_opens_once_the_ancestor_sync_succeeds(tmp_path, fake):
    """Negative control: the same layout opens and ACKs when nothing fails."""
    root, kw, external = _external_layout(tmp_path)
    fake.failing.add(external)
    assert isinstance(_open_and_ingest(root, **kw), store_mod.LayoutNotDurable)

    fake.failing.clear()
    outcome = _open_and_ingest(root, **kw)
    assert isinstance(outcome, IngestResult), outcome
    assert outcome.durability_prerequisites_ok is True


def test_r38_01_one_mount_probe_per_open(tmp_path, fake, monkeypatch):
    """Both chains share one probe: the extra chain costs syncs, not a second mountinfo parse."""
    root, kw, _external = _external_layout(tmp_path)
    real, starts = mounts.probe, []

    def counting(start, **k):
        starts.append(start)
        return real(start, **k)

    monkeypatch.setattr(mounts, "probe", counting)
    outcome = _open_and_ingest(root, **kw)
    assert isinstance(outcome, IngestResult), outcome
    assert len(starts) == 1, starts


# ---- R38-02: covered-mount detection is linear -----------------------------------


class _OverBudget(Exception):
    pass


def _line_events(fn, budget: int) -> int | None:
    """Python lines executed by ``fn()``; None once ``budget`` is exceeded (aborts ``fn``)."""
    count = 0

    def tracer(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
            if count > budget:
                raise _OverBudget
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        fn()
    except _OverBudget:
        return None
    finally:
        sys.settrace(previous)
    return count


_ROOT_LINE = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
# Lines per entry the parser may execute. Measured 2026-10-29 for the two tables below:
# ~107 per entry on CPython 3.12 and 3.14, flat from 1,250 to 20,000 entries (the
# count grows with the length of a mount point, not with the table). The all-pairs
# scan needs at least one line per OTHER entry, i.e. ~5,000 per entry here.
_LINES_PER_ENTRY = 400
_N = 5000


def _siblings(n: int) -> str:
    return _ROOT_LINE + "".join("%d 22 0:%d / /mnt/s%d rw - tmpfs tmpfs rw\n" % (100 + i, 100 + i, i) for i in range(n))


def _stack(n: int) -> str:
    """n mounts stacked on one point, each on top of the previous one: only the top is reachable."""
    return _ROOT_LINE + "".join(
        "%d %d 0:%d / /mnt/stack rw - tmpfs tmpfs rw\n" % (100 + i, 99 + i if i else 22, 100 + i) for i in range(n)
    )


def test_r38_02_sibling_mounts_parse_in_linear_work():
    text = _siblings(_N)
    result = {}
    lines = _line_events(lambda: result.update(parse_mountinfo(text)), budget=_LINES_PER_ENTRY * (_N + 1))
    assert lines is not None, "parse_mountinfo exceeded %d lines per entry on %d sibling mounts" % (_LINES_PER_ENTRY, _N)
    assert len(result) == _N + 1 and result["/mnt/s%d" % (_N - 1)] == (0, 100 + _N - 1)


def test_r38_02_stacked_mounts_parse_in_linear_work():
    """A deep parent chain: the per-entry ancestor walk was quadratic on its own."""
    text = _stack(_N)
    result = {}
    lines = _line_events(lambda: result.update(parse_mountinfo(text)), budget=_LINES_PER_ENTRY * (_N + 1))
    assert lines is not None, "parse_mountinfo exceeded %d lines per entry on %d stacked mounts" % (_LINES_PER_ENTRY, _N)
    assert result == {"/": (8, 1), "/mnt/stack": (0, 100 + _N - 1)}


def _hidden_children(n: int) -> str:
    """n mounts under /mnt/a, then a tmpfs over /mnt/a: all n become unreachable (R41 ambiguity pass)."""
    return (
        _ROOT_LINE
        + "".join("%d 22 0:%d / /mnt/a/c%d rw - ext4 /dev/sdb rw\n" % (100 + i, 100 + i, i) for i in range(n))
        + "%d 22 0:9 / /mnt/a rw - tmpfs tmpfs rw\n" % (100 + n)
    )


@pytest.mark.parametrize("parse", [parse_mountinfo, parse_mountinfo_filesystems])
def test_r41_hidden_children_parse_in_linear_work(parse):
    """Codex on d4681e4 (P2): the ambiguous-point pass scanned the table once per hidden point."""
    text = _hidden_children(_N)
    result = {}
    lines = _line_events(lambda: result.update(parse(text)), budget=_LINES_PER_ENTRY * (_N + 2))
    assert lines is not None, "%s exceeded %d lines per entry on %d hidden mounts" % (parse.__name__, _LINES_PER_ENTRY, _N)
    assert set(result) == {"/", "/mnt/a"}


def test_r38_02_line_counter_sees_the_parser():
    """Positive control for the probe itself: it counts the parser's lines and does abort."""
    text = _siblings(50)
    lines = _line_events(lambda: parse_mountinfo(text), budget=10**9)
    assert lines is not None and lines > 50, lines
    assert _line_events(lambda: parse_mountinfo(text), budget=10) is None


# The covering rule as it stood on c349396 (src/safeforge/mounts.py:117-133), verbatim
# apart from names: the oracle for the differential test below.
def _reference_parse_mountinfo(text: str) -> dict[str, tuple[int, int]] | None:
    entries: list[tuple[int, int, tuple[int, int], str]] = []
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split(" ")
        if "-" not in fields:
            return None
        head = fields[: fields.index("-")]
        tail = fields[fields.index("-") + 1 :]
        if len(head) < 6 or len(tail) < 3:
            return None
        dev = re.fullmatch(r"([0-9]+):([0-9]+)", head[2])
        if not (head[0].isdigit() and head[1].isdigit() and dev):
            return None
        point = mounts._unescape(head[4])
        if point is None or not point.startswith("/"):
            return None
        entries.append((int(head[0]), int(head[1]), (int(dev.group(1)), int(dev.group(2))), point))
    if not entries:
        return None
    parent_of = {mid: parent for mid, parent, _dev, _point in entries}
    if len(parent_of) != len(entries):
        return None

    def ancestors(mid: int) -> set[int]:
        out: set[int] = set()
        while mid in parent_of and parent_of[mid] not in out and parent_of[mid] != mid:
            mid = parent_of[mid]
            out.add(mid)
        return out

    def covers(outer: str, inner: str) -> bool:
        return inner == outer or inner.startswith(outer.rstrip("/") + "/")

    visible: dict[str, tuple[int, int]] = {}
    for mid, _parent, dev, point in entries:
        above = ancestors(mid)
        if not any(o != mid and o not in above and covers(p, point) for o, _pp, _d, p in entries):
            visible[point] = dev
    return visible


# Overlapping points incl. trailing and doubled slashes, the root, and a near-prefix (/a/bc).
_POINTS = ["/", "//", "/a", "/a/", "/a//", "/a/b", "/a//b", "/a/b/", "/a/b/x", "/a/bc", "/b", "/b/a"]


def _random_table(rng: random.Random) -> str:
    n = rng.randint(1, 14)
    ids = rng.sample(range(2, 40), n)
    lines = []
    for mid in ids:
        # Parents: another entry (cycles allowed), itself, or an id outside the table.
        parent = rng.choice(ids + [mid, 1, 99])
        lines.append(
            "%d %d %d:%d / %s rw - ext4 /dev/x rw" % (mid, parent, rng.randint(0, 2), rng.randint(0, 2), rng.choice(_POINTS))
        )
    return "\n".join(lines) + "\n"


def test_r38_02_covering_semantics_match_the_previous_algorithm():
    rng = random.Random(20261029)
    tables = [_random_table(rng) for _ in range(3000)] + [_siblings(300), _stack(300)]
    mismatches = [t for t in tables if parse_mountinfo(t) != _reference_parse_mountinfo(t)]
    assert not mismatches, "differs from the c349396 covering rule on:\n%s" % mismatches[0]


def test_r38_02_reference_oracle_sees_covering():
    """Positive control for the oracle: the generator does produce covered and cyclic tables."""
    rng = random.Random(20261029)
    tables = [_random_table(rng) for _ in range(3000)]
    dropped = [t for t in tables if len(_reference_parse_mountinfo(t) or {}) < len({ln.split(" ")[4] for ln in t.splitlines()})]
    assert len(dropped) > 300, len(dropped)
