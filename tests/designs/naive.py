"""Claim T3: "save the file, then insert the DB row" (or the reverse) without
any coordination. Two variants, both used only to demonstrate the crash gap."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid

from safeforge import _faults


class NaiveStore:
    def __init__(self, root: str) -> None:
        self.objects_dir = os.path.join(root, "objects")
        os.makedirs(self.objects_dir, exist_ok=True)
        self.db_path = os.path.join(root, "naive.db")
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " sha256 TEXT NOT NULL, created_at REAL NOT NULL)"
        )

    def _row(self, uid: str, data: bytes) -> None:
        self.conn.execute(
            "INSERT INTO objects VALUES (?, 'stored', ?, ?)",
            (uid, hashlib.sha256(data).hexdigest(), time.time()),
        )

    def ingest_file_first(self, data: bytes) -> str:
        uid = uuid.uuid4().hex
        with open(os.path.join(self.objects_dir, uid), "wb") as f:
            f.write(data)
        _faults.point("naive.file_written")
        self._row(uid, data)
        return uid

    def ingest_row_first(self, data: bytes) -> str:
        uid = uuid.uuid4().hex
        self._row(uid, data)
        _faults.point("naive.row_inserted")
        with open(os.path.join(self.objects_dir, uid), "wb") as f:
            f.write(data)
        return uid

    def close(self) -> None:
        self.conn.close()
