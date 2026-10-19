"""Independent consistency oracle for disk + database state.

Deliberately does NOT import safeforge: it reads the SQLite file with a plain
connection and hashes files itself, so a bug in the library cannot also hide
in the check.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    db_path: str
    table: str
    object_dir: str
    staging_dir: str
    ok_status: str  # status that promises "file present and intact"
    final_statuses: tuple[str, ...]  # statuses allowed in a quiescent state


def corrected(root: str) -> Layout:
    return Layout(os.path.join(root, "safeforge.db"), "objects", os.path.join(root, "objects"),
                  os.path.join(root, "staging"), "stored", ("stored", "failed"))


def v03(root: str, staging_dir: str | None = None) -> Layout:
    return Layout(os.path.join(root, "v03.db"), "uploads", os.path.join(root, "quarantine"),
                  staging_dir or os.path.join(root, "staging"), "quarantine", ("quarantine", "failed"))


def naive(root: str) -> Layout:
    return Layout(os.path.join(root, "naive.db"), "objects", os.path.join(root, "objects"),
                  os.path.join(root, "staging"), "stored", ("stored",))


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def rows(layout: Layout) -> dict[str, tuple[str, str]]:
    con = sqlite3.connect(layout.db_path)
    try:
        return {r[0]: (r[1], r[2]) for r in con.execute(f"SELECT id, status, sha256 FROM {layout.table}")}
    finally:
        con.close()


def violations(layout: Layout) -> list[str]:
    """I1 ok-row => intact file; I2 file => ok-row; I3 no non-final rows; I4 staging empty."""
    out: list[str] = []
    r = rows(layout)
    files = set(os.listdir(layout.object_dir)) if os.path.isdir(layout.object_dir) else set()
    for oid, (status, sha) in r.items():
        if status == layout.ok_status:
            p = os.path.join(layout.object_dir, oid)
            if oid not in files:
                out.append(f"I1 missing file for {status} row {oid}")
            elif _sha(p) != sha:
                out.append(f"I1 hash mismatch for {oid}")
        if status not in layout.final_statuses:
            out.append(f"I3 non-final row {oid} status={status}")
    for name in files:
        if r.get(name, ("",))[0] != layout.ok_status:
            out.append(f"I2 orphan file {name} (row status={r.get(name, ('none',))[0]})")
    if os.path.isdir(layout.staging_dir):
        for name in os.listdir(layout.staging_dir):
            out.append(f"I4 staging leftover {name}")
    return out
