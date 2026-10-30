"""Claim T7: synchronous=NORMAL vs FULL in WAL mode under a PROCESS kill.

What this can show: every transaction whose COMMIT returned survives a hard
kill of the process (TerminateProcess / SIGKILL), in BOTH modes -- as
pragma.html states ("Transactions are durable across application crashes
regardless of the synchronous setting").

What this cannot show: the power-loss case, where NORMAL "might roll back"
committed transactions. A process kill leaves the OS page cache intact;
simulating power loss needs a block-device fault injector (e.g. dm-log-writes,
a VM snapshot) and is out of scope. The claim is therefore graded on the
primary source for that half.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

CHILD = r"""
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA synchronous=" + sys.argv[2])
c.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, pad BLOB)")
i = 0
while True:
    c.execute("INSERT INTO t(id, pad) VALUES (?, randomblob(512))", (i,))
    sys.stdout.write("ACK %d\n" % i)
    sys.stdout.flush()
    i += 1
"""


@pytest.mark.parametrize("sync", ["NORMAL", "FULL"])
def test_t7_acknowledged_commits_survive_process_kill(tmp_path, sync):
    db = str(tmp_path / "d.db")
    p = subprocess.Popen(
        [sys.executable, "-c", CHILD, db, sync],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    acked = -1
    try:
        for line in p.stdout:
            if line.startswith("ACK "):
                acked = int(line.split()[1])
            if acked >= 150:
                break
    finally:
        p.kill()  # TerminateProcess on Windows, SIGKILL on POSIX
        p.wait(30)
    assert acked >= 150, p.stderr.read()
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ids = {r[0] for r in c.execute("SELECT id FROM t")}
    c.close()
    missing = [i for i in range(acked + 1) if i not in ids]
    assert missing == []
