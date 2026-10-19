"""T8: does mmap_size=256 MiB "drastically" lower CPU? Read-heavy workload,
mmap_size 0 vs 268435456, process CPU time and wall time, interleaved
repetitions, medians. Each measurement opens a fresh connection; the OS file
cache is warm for both (a cold-cache comparison would need cache eviction).

    python experiments/exp_mmap.py [--rows 300000] [--lookups 200000] [--reps 5]
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import save  # noqa: E402

MMAP = {"mmap_0": 0, "mmap_256MiB": 268435456}


def build(path, rows):
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, k TEXT UNIQUE, payload BLOB)")
    c.execute("BEGIN")
    rnd = random.Random(1)
    for i in range(rows):
        c.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "k%09d" % i, rnd.randbytes(100)))
    c.execute("COMMIT")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()


def workload(path, mmap_size, keys, scans):
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA mmap_size=%d" % mmap_size)
    got = c.execute("PRAGMA mmap_size").fetchone()[0]
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for k in keys:
        c.execute("SELECT payload FROM t WHERE k=?", (k,)).fetchone()
    for _ in range(scans):
        c.execute("SELECT sum(length(payload)) FROM t").fetchone()
    cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
    c.close()
    return cpu, wall, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=300_000)
    ap.add_argument("--lookups", type=int, default=200_000)
    ap.add_argument("--scans", type=int, default=5)
    ap.add_argument("--reps", type=int, default=5)
    a = ap.parse_args()
    if min(a.rows, a.lookups, a.reps) <= 0 or a.scans < 0:
        ap.error("rows/lookups/reps must be > 0")
    rnd = random.Random(2)
    keys = ["k%09d" % rnd.randrange(a.rows) for _ in range(a.lookups)]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "m.db")
        build(path, a.rows)
        size = os.path.getsize(path)
        workload(path, 0, keys[:1000], 1)  # warm the OS cache once
        res = {k: {"cpu": [], "wall": [], "effective_mmap_size": None} for k in MMAP}
        for i in range(a.reps):
            order = list(MMAP.items()) if i % 2 == 0 else list(reversed(MMAP.items()))
            for name, size_req in order:
                cpu, wall, got = workload(path, size_req, keys, a.scans)
                res[name]["cpu"].append(round(cpu, 3))
                res[name]["wall"].append(round(wall, 3))
                res[name]["effective_mmap_size"] = got
    for v in res.values():
        v["median_cpu_s"] = statistics.median(v["cpu"])
        v["median_wall_s"] = statistics.median(v["wall"])
    ratio = res["mmap_256MiB"]["median_cpu_s"] / res["mmap_0"]["median_cpu_s"]
    for k, v in res.items():
        print("%-12s cpu=%.3fs wall=%.3fs effective_mmap=%s  cpu=%s" % (
            k, v["median_cpu_s"], v["median_wall_s"], v["effective_mmap_size"], v["cpu"]))
    print("cpu ratio mmap/no-mmap = %.3f   (db size %.1f MiB)" % (ratio, size / 2**20))
    print(save("mmap", {"params": vars(a), "db_bytes": size, "rows": res, "cpu_ratio_mmap_over_none": round(ratio, 4)}))


if __name__ == "__main__":
    main()
