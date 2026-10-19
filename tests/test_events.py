"""Claim T13: idempotent webhook processing. Negative control: check-then-insert races."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from safeforge import PayloadMismatch, connect, ensure_schema, process_event


def _setup(path):
    c = connect(path)
    ensure_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS licences(id INTEGER PRIMARY KEY, event_id TEXT NOT NULL)")
    return c


def _issue(event_id):
    def effect(conn):
        cur = conn.execute("INSERT INTO licences(event_id) VALUES (?)", (event_id,))
        return {"licence": cur.lastrowid}

    return effect


def _licences(path, event_id):
    c = sqlite3.connect(path)
    n = c.execute("SELECT count(*) FROM licences WHERE event_id=?", (event_id,)).fetchone()[0]
    c.close()
    return n


def test_t13_check_then_insert_issues_two_licences(tmp_path):
    """Negative control: the 'look up the transaction id first' pattern, two concurrent deliveries."""
    path = str(tmp_path / "e.db")
    _setup(path).close()
    gate = threading.Barrier(2)

    def deliver():
        c = sqlite3.connect(path, isolation_level=None, timeout=5)
        seen = c.execute("SELECT 1 FROM processed_events WHERE event_id='evt_1'").fetchone()
        gate.wait()  # both deliveries have checked; neither has inserted yet
        if seen is None:
            c.execute("INSERT INTO licences(event_id) VALUES ('evt_1')")
            c.execute("INSERT OR IGNORE INTO processed_events(event_id, payload_sha, received_at)"
                      " VALUES ('evt_1', 'x', 0)")
        c.close()

    ts = [threading.Thread(target=deliver) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert _licences(path, "evt_1") == 2


def test_t13_unique_insert_issues_exactly_one_licence_under_concurrency(tmp_path):
    path = str(tmp_path / "e.db")
    _setup(path).close()
    n = 8
    gate = threading.Barrier(n)
    results, errors = [], []

    def deliver():
        try:
            c = connect(path)
            gate.wait()
            results.append(process_event(c, "evt_1", b'{"paid":true}', _issue("evt_1")))
            c.close()
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    ts = [threading.Thread(target=deliver) for _ in range(n)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert errors == []
    assert sum(r.applied for r in results) == 1
    assert len({r.result["licence"] for r in results}) == 1  # duplicates get the original result
    assert _licences(path, "evt_1") == 1


def test_t13_same_id_different_payload_is_rejected(tmp_path):
    c = _setup(str(tmp_path / "e.db"))
    process_event(c, "evt_2", b"A", _issue("evt_2"))
    with pytest.raises(PayloadMismatch):
        process_event(c, "evt_2", b"B", _issue("evt_2"))


def test_t13_failed_effect_is_not_recorded_and_can_be_retried(tmp_path):
    path = str(tmp_path / "e.db")
    c = _setup(path)

    def boom(conn):
        conn.execute("INSERT INTO licences(event_id) VALUES ('evt_3')")
        raise RuntimeError("downstream failed")

    with pytest.raises(RuntimeError):
        process_event(c, "evt_3", b"A", boom)
    assert _licences(path, "evt_3") == 0  # rolled back together with the event record
    r = process_event(c, "evt_3", b"A", _issue("evt_3"))
    assert r.applied and _licences(path, "evt_3") == 1


@pytest.mark.parametrize("bad", ["", "a b", "x" * 201, None, 5, "evt\n1", "evt1\n"])
def test_t13_rejects_malformed_event_ids(tmp_path, bad):
    c = _setup(str(tmp_path / "e.db"))
    with pytest.raises(ValueError):
        process_event(c, bad, b"A", _issue("x"))


def test_t13_rejects_non_bytes_payload(tmp_path):
    c = _setup(str(tmp_path / "e.db"))
    with pytest.raises(TypeError):
        process_event(c, "evt_4", '{"a":1}', _issue("evt_4"))
