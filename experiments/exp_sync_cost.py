"""T7 cost side: single-row commits per second for WAL+NORMAL, WAL+FULL and
DELETE+FULL on the local disk. Durability is a correctness question
(tests/test_sqlite_durability.py + sqlite.org/pragma.html); this only
measures what FULL costs.

    python experiments/exp_sync_cost.py [--commits 2000] [--reps 3]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import save  # noqa: E402

CONFIGS = [("wal", "NORMAL"), ("wal", "FULL"), ("delete", "FULL")]


def one(journal, sync, commits):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        c = sqlite3.connect(os.path.join(d, "s.db"), isolation_level=None)
        c.execute("PRAGMA journal_mode=%s" % journal)
        c.execute("PRAGMA synchronous=%s" % sync)
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, pad BLOB)")
        t0 = time.perf_counter()
        for i in range(commits):
            c.execute("INSERT INTO t VALUES (?, randomblob(256))", (i,))
        dt = time.perf_counter() - t0
        c.close()
    return commits / dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", type=int, default=2000)
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    if a.commits <= 0 or a.reps <= 0:
        ap.error("commits and reps must be > 0")
    res = {}
    for _ in range(a.reps):
        for journal, sync in CONFIGS:  # interleaved to spread drift over all configs
            res.setdefault("%s+%s" % (journal, sync), []).append(one(journal, sync, a.commits))
    rows = {k: {"median_commits_per_s": round(statistics.median(v), 1), "all": [round(x, 1) for x in v]} for k, v in res.items()}
    for k, v in rows.items():
        print("%-12s %8.1f commits/s  %s" % (k, v["median_commits_per_s"], v["all"]))
    print(save("sync_cost", {"params": vars(a), "rows": rows}))


if __name__ == "__main__":
    main()
