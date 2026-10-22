"""T8: does mmap_size=256 MiB "drastically" lower CPU? Read-heavy workload,
mmap_size 0 vs 268435456, process CPU time and wall time per phase (indexed
point lookups, full table scans), interleaved repetitions, medians. Each
measurement opens a fresh connection; the OS file cache is warm for both (a
cold-cache comparison would need cache eviction). Writes are not measured:
sqlite.org/mmap.html says mmap "does not significantly change the performance
of database changes".

    python experiments/exp_mmap.py [--rows 300000] [--lookups 200000] [--scans 5] [--reps 5]
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
PHASES = ("lookups", "scans")


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
    out = {}
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for k in keys:
        c.execute("SELECT payload FROM t WHERE k=?", (k,)).fetchone()
    out["lookups"] = (time.process_time() - cpu0, time.perf_counter() - wall0)
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for _ in range(scans):
        c.execute("SELECT sum(length(payload)) FROM t").fetchone()
    out["scans"] = (time.process_time() - cpu0, time.perf_counter() - wall0)
    c.close()
    return out, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=300_000)
    ap.add_argument("--lookups", type=int, default=200_000)
    ap.add_argument("--scans", type=int, default=5)
    ap.add_argument("--reps", type=int, default=5)
    a = ap.parse_args()
    if min(a.rows, a.lookups, a.reps, a.scans) <= 0:
        ap.error("rows/lookups/scans/reps must be > 0")
    rnd = random.Random(2)
    keys = ["k%09d" % rnd.randrange(a.rows) for _ in range(a.lookups)]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "m.db")
        build(path, a.rows)
        size = os.path.getsize(path)
        workload(path, 0, keys[:1000], 1)  # warm the OS cache once
        res = {k: {p: {"cpu": [], "wall": []} for p in PHASES} for k in MMAP}
        effective = {}
        for i in range(a.reps):
            order = list(MMAP.items()) if i % 2 == 0 else list(reversed(MMAP.items()))
            for name, size_req in order:
                out, got = workload(path, size_req, keys, a.scans)
                effective[name] = got
                for p in PHASES:
                    res[name][p]["cpu"].append(round(out[p][0], 3))
                    res[name][p]["wall"].append(round(out[p][1], 3))
    ratios = {}
    for p in PHASES:
        for name in MMAP:
            r = res[name][p]
            r["median_cpu_s"] = statistics.median(r["cpu"])
            r["median_wall_s"] = statistics.median(r["wall"])
            print("%-8s %-12s cpu=%.3fs wall=%.3fs  cpu-runs=%s" % (p, name, r["median_cpu_s"], r["median_wall_s"], r["cpu"]))
        ratios[p] = round(res["mmap_256MiB"][p]["median_cpu_s"] / res["mmap_0"][p]["median_cpu_s"], 4)
        print("%-8s cpu ratio mmap/no-mmap = %.3f" % (p, ratios[p]))
    print("db size %.1f MiB, effective mmap_size %s" % (size / 2**20, effective))
    print(
        save(
            "mmap",
            {
                "params": vars(a),
                "db_bytes": size,
                "effective_mmap_size": effective,
                "phases": res,
                "cpu_ratio_mmap_over_none": ratios,
            },
        )
    )


if __name__ == "__main__":
    main()
