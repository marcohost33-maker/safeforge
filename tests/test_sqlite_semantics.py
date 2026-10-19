"""Claims T4, T5, T6, T9: deterministic reproductions of SQLite locking behaviour.

Every test drives two (or more) real connections to a real database file into
a precise interleaving -- no sleeps-and-hope. Elapsed times are measured to
show WHETHER the busy handler waited, not to benchmark.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

JOURNALS = ["delete", "wal"]


def conn(path, timeout_ms=0, journal="wal"):
    c = sqlite3.connect(path, timeout=timeout_ms / 1000.0, isolation_level=None, check_same_thread=False)
    assert c.execute("PRAGMA journal_mode=%s" % journal).fetchone()[0] == journal
    return c


@pytest.fixture
def dbfile(tmp_path):
    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p, isolation_level=None)
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER)")
    c.execute("INSERT INTO t(v) VALUES (0)")
    c.close()
    return p


def busy(excinfo) -> str:
    return getattr(excinfo.value, "sqlite_errorname", "?")


def test_python_sqlite3_default_timeout_is_already_5000ms(dbfile):
    """Context for T4/T6: Python's sqlite3.connect() defaults to timeout=5.0 s."""
    c = sqlite3.connect(dbfile)
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    c.close()


@pytest.mark.parametrize("journal", JOURNALS)
def test_t4_second_writer_gets_busy_in_both_journal_modes(dbfile, journal):
    """WAL does not remove writer/writer contention: there is one writer at a time."""
    w1, w2 = conn(dbfile, 0, journal), conn(dbfile, 0, journal)
    w1.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError) as ei:
        w2.execute("BEGIN IMMEDIATE")
    assert busy(ei).startswith("SQLITE_BUSY")
    w1.execute("ROLLBACK")


def test_t5_wal_reader_proceeds_while_writer_holds_exclusive(dbfile):
    w, r = conn(dbfile, 0, "wal"), conn(dbfile, 0, "wal")
    w.execute("BEGIN EXCLUSIVE")
    w.execute("INSERT INTO t(v) VALUES (1)")
    assert r.execute("SELECT count(*) FROM t").fetchone()[0] == 1  # old snapshot, no wait
    w.execute("COMMIT")
    assert r.execute("SELECT count(*) FROM t").fetchone()[0] == 2


def test_t5_rollback_journal_exclusive_writer_blocks_reader(dbfile):
    """Negative control for T5: without WAL an exclusive writer locks readers out."""
    w, r = conn(dbfile, 0, "delete"), conn(dbfile, 0, "delete")
    w.execute("BEGIN EXCLUSIVE")
    with pytest.raises(sqlite3.OperationalError) as ei:
        r.execute("SELECT count(*) FROM t").fetchone()
    assert busy(ei).startswith("SQLITE_BUSY")
    w.execute("ROLLBACK")


def test_t5_wal_reader_blocked_by_exclusive_locking_mode(dbfile):
    """Documented WAL exception (wal.html section 9): locking_mode=EXCLUSIVE blocks all readers."""
    w = conn(dbfile, 0, "wal")
    w.execute("PRAGMA locking_mode=EXCLUSIVE")
    w.execute("INSERT INTO t(v) VALUES (1)")  # first write takes and keeps the exclusive lock
    r = sqlite3.connect(dbfile, timeout=0, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError) as ei:
        r.execute("SELECT count(*) FROM t").fetchone()
    assert busy(ei).startswith("SQLITE_BUSY")
    w.close()


@pytest.mark.parametrize("journal", JOURNALS)
def test_t6_t9_deferred_upgrade_fails_at_once_despite_busy_timeout(dbfile, journal):
    """Two DEFERRED read->write upgrades: the second gets SQLITE_BUSY WITHOUT waiting,
    although busy_timeout is 5000 ms -- SQLite skips the busy handler because
    waiting could deadlock (c3ref/busy_handler.html)."""
    a, b = conn(dbfile, 5000, journal), conn(dbfile, 5000, journal)
    a.execute("BEGIN")
    a.execute("SELECT v FROM t").fetchall()
    b.execute("BEGIN")
    b.execute("SELECT v FROM t").fetchall()
    a.execute("UPDATE t SET v = v + 1")  # a upgrades first
    t0 = time.perf_counter()
    with pytest.raises(sqlite3.OperationalError) as ei:
        b.execute("UPDATE t SET v = v + 1")
    waited = time.perf_counter() - t0
    assert busy(ei).startswith("SQLITE_BUSY")
    assert waited < 1.0, "busy handler was invoked (%.3fs)" % waited
    b.execute("ROLLBACK")
    a.execute("COMMIT")


def test_t9_rollback_journal_upgrade_is_a_real_deadlock(dbfile):
    """In DELETE mode the first upgrader cannot COMMIT either while the other
    holds its read lock: both are stuck until one side rolls back."""
    a, b = conn(dbfile, 300, "delete"), conn(dbfile, 300, "delete")
    a.execute("BEGIN")
    a.execute("SELECT v FROM t").fetchall()
    b.execute("BEGIN")
    b.execute("SELECT v FROM t").fetchall()
    a.execute("UPDATE t SET v = v + 1")
    with pytest.raises(sqlite3.OperationalError):
        b.execute("UPDATE t SET v = v + 1")
    with pytest.raises(sqlite3.OperationalError) as ei:
        a.execute("COMMIT")  # needs EXCLUSIVE; b still holds SHARED
    assert busy(ei).startswith("SQLITE_BUSY")
    b.execute("ROLLBACK")
    a.execute("COMMIT")
    assert a.execute("SELECT v FROM t").fetchone()[0] == 1


def test_t9_wal_stale_snapshot_after_other_commit(dbfile):
    """WAL: once the other writer has committed, the upgrade fails with BUSY_SNAPSHOT."""
    a, b = conn(dbfile, 5000, "wal"), conn(dbfile, 5000, "wal")
    b.execute("BEGIN")
    b.execute("SELECT v FROM t").fetchall()
    a.execute("UPDATE t SET v = v + 1")  # autocommit
    with pytest.raises(sqlite3.OperationalError) as ei:
        b.execute("UPDATE t SET v = v + 1")
    assert busy(ei) == "SQLITE_BUSY_SNAPSHOT"
    b.execute("ROLLBACK")


@pytest.mark.parametrize("journal", JOURNALS)
def test_t9_begin_immediate_waits_and_succeeds(dbfile, journal):
    """Positive control for T9: BEGIN IMMEDIATE takes the write lock up front;
    the second writer WAITS in the busy handler and then succeeds."""
    a = conn(dbfile, 5000, journal)
    b = conn(dbfile, 5000, journal)
    a.execute("BEGIN IMMEDIATE")
    a.execute("UPDATE t SET v = v + 1")
    out = {}

    def second():
        b.execute("BEGIN IMMEDIATE")
        out["acquired"] = time.perf_counter()
        v = b.execute("SELECT v FROM t").fetchone()[0]
        b.execute("UPDATE t SET v = ?", (v + 1,))
        b.execute("COMMIT")

    th = threading.Thread(target=second)
    th.start()
    time.sleep(0.3)
    t_commit = time.perf_counter()
    a.execute("COMMIT")
    th.join(10)
    assert not th.is_alive()
    assert out["acquired"] >= t_commit  # b waited for a, it did not fail
    assert a.execute("SELECT v FROM t").fetchone()[0] == 2  # no lost update


def test_t6_finite_timeout_expires(dbfile):
    a, b = conn(dbfile, 0, "wal"), conn(dbfile, 300, "wal")
    a.execute("BEGIN IMMEDIATE")
    t0 = time.perf_counter()
    with pytest.raises(sqlite3.OperationalError) as ei:
        b.execute("BEGIN IMMEDIATE")
    waited = time.perf_counter() - t0
    assert busy(ei).startswith("SQLITE_BUSY")
    assert waited >= 0.25
    a.execute("ROLLBACK")


@pytest.mark.slow
def test_t6_busy_timeout_5000_still_returns_busy_when_lock_held_longer(dbfile):
    a, b = conn(dbfile, 0, "wal"), conn(dbfile, 5000, "wal")
    a.execute("BEGIN IMMEDIATE")
    t0 = time.perf_counter()
    with pytest.raises(sqlite3.OperationalError) as ei:
        b.execute("BEGIN IMMEDIATE")
    waited = time.perf_counter() - t0
    assert busy(ei).startswith("SQLITE_BUSY")
    assert 4.5 <= waited < 15
    a.execute("ROLLBACK")
