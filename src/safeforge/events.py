"""Idempotent event handling (e.g. payment webhooks) on SQLite.

The race this prevents: "SELECT ... WHERE event_id=?; if absent: do effect;
INSERT event". Two deliveries of the same event that interleave between the
SELECT and the INSERT both see "absent" and both run the effect -- two
licences for one payment. A busy_timeout does not help; the problem is the
gap between check and act, not a lock error.

The fix: let the database decide. In ONE ``BEGIN IMMEDIATE`` transaction,
``INSERT ... ON CONFLICT(event_id) DO NOTHING``; only if a row was inserted,
run the effect with the same connection; commit both together. A duplicate
delivery inserts nothing and runs nothing. If the effect raises, the
transaction rolls back, the event is NOT recorded, and a redelivery can retry.

Scope ("exactly-once" is only true inside this boundary): effects written
through ``conn`` inside the callback happen at most once and are atomic with
the event record. Side effects OUTSIDE the database (sending an e-mail,
calling another API) are not covered; use an outbox table written in the same
transaction and deliver it separately. Authenticating the webhook (signature,
timestamp tolerance) is a separate, prior step and is not done here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import db

__all__ = ["ensure_schema", "process_event", "EventResult", "PayloadMismatch"]

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,200}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events(
    event_id     TEXT PRIMARY KEY,
    payload_sha  TEXT NOT NULL,
    result_json  TEXT,
    received_at  REAL NOT NULL
) STRICT
"""


class PayloadMismatch(ValueError):
    """Same event id, different payload: replay with altered body, or a provider bug."""


@dataclass(frozen=True)
class EventResult:
    applied: bool  # True: effect ran now. False: duplicate, effect not run again.
    result: Any


def _reject_json_constant(value: str):
    """Reject NaN/Infinity in previously persisted JSON, including corrupt rows."""
    raise ValueError("non-standard JSON constant in persisted event result: %s" % value)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def process_event(
    conn: sqlite3.Connection,
    event_id: str,
    payload: bytes,
    effect: Callable[[sqlite3.Connection], Any],
) -> EventResult:
    """Run ``effect(conn)`` at most once per ``event_id``, atomically with the record.

    ``effect`` must do its database writes through the given connection and
    return a JSON-serialisable result, which is stored and returned again for
    duplicates.
    """
    if not isinstance(event_id, str) or not _EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("event_id must match %s" % _EVENT_ID_RE.pattern)
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes (hash the raw body, not a re-serialisation)")
    psha = hashlib.sha256(payload).hexdigest()
    with db.transaction(conn, "IMMEDIATE"):
        cur = conn.execute(
            "INSERT INTO processed_events(event_id, payload_sha, received_at) VALUES (?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
            (event_id, psha, time.time()),
        )
        if cur.rowcount == 1:
            result = effect(conn)
            conn.execute(
                "UPDATE processed_events SET result_json=? WHERE event_id=?",
                (json.dumps(result, allow_nan=False), event_id),
            )
            return EventResult(True, result)
        old_sha, old_json = conn.execute(
            "SELECT payload_sha, result_json FROM processed_events WHERE event_id=?", (event_id,)
        ).fetchone()
    if old_sha != psha:
        raise PayloadMismatch("event %s was already processed with a different payload" % event_id)
    return EventResult(False, json.loads(old_json, parse_constant=_reject_json_constant) if old_json is not None else None)
