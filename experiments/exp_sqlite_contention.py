"""T4/T5/T6/T9 under load: N writer processes doing read-modify-write increments
plus R reader processes, for every combination of

    journal_mode   DELETE | WAL
    busy_timeout   0 | 5000 ms
    transaction    BEGIN DEFERRED (SELECT, then UPDATE) | BEGIN IMMEDIATE

No retries: every SQLITE_BUSY* is counted, by extended error name. The final
counter is compared with the number of successful commits (lost-update check).

    python experiments/exp_sqlite_contention.py [--writers 8] [--readers 4] [--ops 200]

synchronous=NORMAL is used to keep fsync cost out of a LOCKING experiment.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import save  # noqa: E402


def _open(path, journal, timeout_ms):
    c = sqlite3.connect(path, timeout=timeout_ms / 1000.0, isolation_level=None)
    c.execute("PRAGMA busy_timeout=%d" % timeout_ms)
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def writer(path, journal, timeout_ms, begin, ops, start, q):
    c = _open(path, journal, timeout_ms)
    errors: Counter = Counter()
    ok = 0
    start.wait()
    for _ in range(ops):
        stage = "begin"
        try:
            c.execute("BEGIN " + begin)
            stage = "select"
            v = c.execute("SELECT v FROM counter WHERE id=1").fetchone()[0]
            stage = "update"
            c.execute("UPDATE counter SET v=? WHERE id=1", (v + 1,))
            stage = "commit"
            c.execute("COMMIT")
            ok += 1
        except sqlite3.OperationalError as e:
            errors["%s@%s" % (getattr(e, "sqlite_errorname", str(e)), stage)] += 1
            if c.in_transaction:
                c.execute("ROLLBACK")
    c.close()
    q.put(("w", ok, dict(errors)))


def reader(path, journal, timeout_ms, stop, start, q):
    c = _open(path, journal, timeout_ms)
    errors: Counter = Counter()
    ok = 0
    start.wait()
    while not stop.is_set():
        try:
            c.execute("SELECT v FROM counter WHERE id=1").fetchone()
            ok += 1
        except sqlite3.OperationalError as e:
            errors[getattr(e, "sqlite_errorname", str(e))] += 1
    c.close()
    q.put(("r", ok, dict(errors)))


def run_one(journal, timeout_ms, begin, writers, readers, ops):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        path = os.path.join(d, "c.db")
        c = sqlite3.connect(path, isolation_level=None)
        assert c.execute("PRAGMA journal_mode=%s" % journal).fetchone()[0] == journal
        c.execute("CREATE TABLE counter(id INTEGER PRIMARY KEY, v INTEGER NOT NULL)")
        c.execute("INSERT INTO counter VALUES (1, 0)")
        c.close()
        ctx = mp.get_context("spawn")
        start, stop, q = ctx.Event(), ctx.Event(), ctx.Queue()
        ws = [ctx.Process(target=writer, args=(path, journal, timeout_ms, begin, ops, start, q)) for _ in range(writers)]
        rs = [ctx.Process(target=reader, args=(path, journal, 0, stop, start, q)) for _ in range(readers)]
        for p in ws + rs:
            p.start()
        time.sleep(1.0)  # let every process open its connection
        t0 = time.perf_counter()
        start.set()
        results = [q.get(timeout=600) for _ in ws]
        wall = time.perf_counter() - t0
        stop.set()
        results += [q.get(timeout=60) for _ in rs]
        for p in ws + rs:
            p.join(60)
        fc = sqlite3.connect(path)
        final = fc.execute("SELECT v FROM counter WHERE id=1").fetchone()[0]
        fc.close()
    w_ok = sum(r[1] for r in results if r[0] == "w")
    w_err: Counter = Counter()
    r_err: Counter = Counter()
    r_ok = 0
    for kind, ok, err in results:
        (w_err if kind == "w" else r_err).update(err)
        if kind == "r":
            r_ok += ok
    return {
        "journal": journal,
        "busy_timeout_ms": timeout_ms,
        "begin": begin,
        "writer_attempts": writers * ops,
        "writer_commits": w_ok,
        "writer_errors": dict(w_err),
        "final_counter": final,
        "lost_updates": w_ok - final,
        "reader_ok": r_ok,
        "reader_errors": dict(r_err),
        "wall_s": round(wall, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--writers", type=int, default=8)
    ap.add_argument("--readers", type=int, default=4)
    ap.add_argument("--ops", type=int, default=200)
    a = ap.parse_args()
    if min(a.writers, a.ops) <= 0 or a.readers < 0:
        ap.error("writers/ops must be > 0, readers >= 0")
    rows = []
    for journal in ("delete", "wal"):
        for timeout_ms in (0, 5000):
            for begin in ("DEFERRED", "IMMEDIATE"):
                r = run_one(journal, timeout_ms, begin, a.writers, a.readers, a.ops)
                rows.append(r)
                print(
                    "%-6s timeout=%-5d %-9s commits=%4d/%d errors=%s lost=%d reader_errors=%s wall=%.2fs"
                    % (
                        journal,
                        timeout_ms,
                        begin,
                        r["writer_commits"],
                        r["writer_attempts"],
                        r["writer_errors"],
                        r["lost_updates"],
                        r["reader_errors"],
                        r["wall_s"],
                    ),
                    flush=True,
                )
    print(save("contention", {"params": vars(a), "rows": rows}))


if __name__ == "__main__":
    main()
