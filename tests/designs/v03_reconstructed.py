"""The "reconciliation pattern" from the SAFEFORGE analysis (claim T10), rebuilt
from its prose so that it can be crash-tested. The V0.3 source was not
available; every step below follows the quoted description, nothing more:

    stream into staging (UUID name) -> SHA-256 -> BEGIN IMMEDIATE ->
    INSERT status 'pending_storage' -> rename(staging, quarantine) ->
    UPDATE status 'quarantine' -> COMMIT

    reconciler: file without DB row -> delete;
                'pending_storage' older than 5 min -> 'failed', clean staging

Not part of the library. Do not use.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid

from safeforge import _faults


class V03Store:
    def __init__(self, root: str, staging_dir: str | None = None) -> None:
        self.quarantine_dir = os.path.join(root, "quarantine")
        self.staging_dir = staging_dir or os.path.join(root, "staging")
        os.makedirs(self.quarantine_dir, exist_ok=True)
        os.makedirs(self.staging_dir, exist_ok=True)
        self.db_path = os.path.join(root, "v03.db")
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " sha256 TEXT NOT NULL, created_at REAL NOT NULL)"
        )

    def ingest(self, data: bytes) -> str:
        uid = uuid.uuid4().hex
        spath = os.path.join(self.staging_dir, uid)
        with open(spath, "wb") as f:  # no fsync -- none is described
            f.write(data)
        _faults.point("v03.staged")
        sha = hashlib.sha256(data).hexdigest()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO uploads VALUES (?, 'pending_storage', ?, ?)", (uid, sha, time.time())
            )
            _faults.point("v03.pending_inserted")
            os.rename(spath, os.path.join(self.quarantine_dir, uid))  # no dir fsync described
            _faults.point("v03.renamed")
            self.conn.execute("UPDATE uploads SET status='quarantine' WHERE id=?", (uid,))
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        _faults.point("v03.committed")
        return uid

    def reconcile(self, grace_seconds: float = 300.0, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for name in os.listdir(self.quarantine_dir):
            if self.conn.execute("SELECT 1 FROM uploads WHERE id=?", (name,)).fetchone() is None:
                os.unlink(os.path.join(self.quarantine_dir, name))
        rows = self.conn.execute(
            "SELECT id FROM uploads WHERE status='pending_storage' AND created_at <= ?",
            (now - grace_seconds,),
        ).fetchall()
        for (uid,) in rows:
            self.conn.execute("UPDATE uploads SET status='failed' WHERE id=?", (uid,))
            p = os.path.join(self.staging_dir, uid)
            if os.path.exists(p):
                os.unlink(p)

    def close(self) -> None:
        self.conn.close()
