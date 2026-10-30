"""Directory-entry half of a power cut: which names can a directory hold afterwards?

LazyFS drops unsynced *data* but persists directory operations at once (see
tests/lazyfs_harness.py). This module covers the other half with a model in
the style of ALICE (Pillai et al., "All File Systems Are Not Created Equal",
OSDI 2014): a directory's entries are durable only up to its last fsync;
after that, any entry added may be missing and any entry removed may still be
there -- independently per entry, which is weaker than ext4/XFS guarantee in
practice and therefore a superset of what a real file system can produce.
A name that existed only between two fsyncs (created, then removed) may also
come back: its creation can reach the disk while its removal does not.

Assumptions, stated so a reader can reject them:

* A1 file DATA is intact for files whose data was fsync'ed before they were
  published (checked separately, with LazyFS).
* A2 the SQLite database is exactly as committed (synchronous=FULL; also
  checked with LazyFS, including the NORMAL counterexample).
* A3 a name removed after the last directory fsync that comes back refers to
  the same inode it had (POSIX link/unlink do not touch data); it is
  re-created here as a hard link to that inode's other surviving name.

Inputs come from tests/powerloss_driver.py: the trace of real directory
fsyncs with the entries present right after each one.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
from dataclasses import dataclass


def history(trace_path: str, directory: str, baseline: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """(entries as of the last fsync of ``directory``, names seen since then).

    Without any recorded fsync the durable set is ``baseline`` (the state the
    parent made durable before starting the child).
    """
    directory = os.path.abspath(directory)
    durable, seen = baseline, set(baseline)
    if os.path.exists(trace_path):
        with open(trace_path, encoding="utf-8") as t:
            for line in t:
                rec = json.loads(line)
                if rec["dir"] != directory:
                    continue
                if rec["kind"] == "fsync":
                    durable, seen = frozenset(rec["entries"]), set(rec["entries"])
                else:
                    seen.update(rec["entries"])
    return durable, frozenset(seen)


@dataclass(frozen=True)
class DirChoice:
    """One possible post-crash content of one directory."""

    rel: str  # directory relative to the store root
    present: frozenset[str]
    added_lost: frozenset[str]  # in the directory now, not durable, gone after the cut
    back: frozenset[str]  # gone now, but its creation (or survival) may have reached the disk


def choices(rel: str, durable: frozenset[str], seen: frozenset[str], current: frozenset[str]) -> list[DirChoice]:
    """Every subset choice: each non-durable current entry may vanish, each vanished one may return."""
    added = sorted(current - durable)
    gone = sorted((durable | seen) - current)
    out = []
    for k_lost in range(len(added) + 1):
        for lost in itertools.combinations(added, k_lost):
            for k_back in range(len(gone) + 1):
                for back in itertools.combinations(gone, k_back):
                    present = (current - set(lost)) | set(back)
                    out.append(DirChoice(rel, frozenset(present), frozenset(lost), frozenset(back)))
    return out


def states(root: str, trace_path: str, rels: tuple[str, ...], baseline: dict[str, frozenset[str]]) -> list[tuple[DirChoice, ...]]:
    """Cartesian product of the per-directory choices."""
    per_dir = []
    for rel in rels:
        d = os.path.join(root, rel)
        durable, seen = history(trace_path, d, baseline[rel])
        per_dir.append(choices(rel, durable, seen, frozenset(os.listdir(d))))
    return list(itertools.product(*per_dir))


def _inode_twin(root: str, rels: tuple[str, ...], name: str) -> str:
    """A surviving path that shares the inode a removed ``name`` had (A3).

    safeforge publishes ``staging/<id>.part`` as ``objects/<id>``; the only
    removal in ingest is that staging name after the link.
    """
    oid = name[: -len(".part")] if name.endswith(".part") else name
    for rel in rels:
        p = os.path.join(root, rel, oid)
        if os.path.isfile(p):
            return p
    raise LookupError("no surviving name shares the inode of removed entry %r" % name)


def materialize(src_root: str, dst_root: str, state: tuple[DirChoice, ...]) -> None:
    """Copy ``src_root`` to ``dst_root`` and apply one post-crash directory state."""
    shutil.copytree(src_root, dst_root, symlinks=True)
    rels = tuple(c.rel for c in state)
    for c in state:
        d = os.path.join(dst_root, c.rel)
        for name in c.added_lost:
            os.unlink(os.path.join(d, name))
    for c in state:
        d = os.path.join(dst_root, c.rel)
        for name in c.back:
            # Same bytes as the inode twin; a copy keeps dst independent of src.
            shutil.copyfile(_inode_twin(src_root, rels, name), os.path.join(d, name))
