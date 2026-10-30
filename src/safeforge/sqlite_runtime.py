"""Which SQLite library is actually loaded, and does it carry the WAL-reset fix?

Primary source, https://sqlite.org/wal.html section 11 "The WAL-Reset Bug"
(read 2026-10-27):

    "The bug only affects databases in WAL mode when there are two or more
    database connections open on the same file, in separate threads or
    processes, and when those two connections attempt to write or checkpoint
    at the same instant."
    "The bug is likely present in all version of SQLite from 3.7.0
    (2010-07-21) through 3.51.2 (2026-01-09). It is fixed in version 3.51.3
    (2026-03-13) and later."
    "Backports of the fix are available for some earlier releases:" 3.44.6, 3.50.7.

https://sqlite.org/changes.html: 3.51.3 and 3.53.0 each list "Fix the
WAL-reset database corruption bug."; 3.52.0 is marked "Withdrawn".

SafeForge meets the precondition by design: ``ObjectStore`` is "one instance
per thread" (one connection each) on the same database, coordinated stores hold
a SHARED lock across processes, and ``coordination.event_writer`` opens another
writer connection under that same shared lock.

Why the gate reads the library and not the Python version: the sqlite3 module
is linked against whatever libsqlite3 the build or the OS provides (python.org
3.12 bundles 3.49.1, 3.13/3.14 bundle 3.50.4; Linux builds may use the system
library). ``loaded_sqlite_status`` therefore asks a live connection for
``sqlite_version()`` / ``sqlite_source_id()`` instead of trusting the
``sqlite3.sqlite_version_info`` attribute.

Limits: the decision is by version number. 3.44.6 and 3.50.7 exist only as
branch builds (no tagged release); a vendor that backports the fix WITHOUT
raising the version number is reported as not fixed (fail-closed, needs its own
evidence and the documented waiver). The source id is reported, not matched.

Release gate: ``python -m safeforge.sqlite_gate`` prints a JSON verdict and
exits 0 (pass, or waived), 1 (affected library, no waiver) or 2 (blank waiver).
The waiver is the environment variable ``SAFEFORGE_ALLOW_UNFIXED_SQLITE``; its
value is the reason and is printed in the verdict.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

__all__ = ["WAIVER_ENV", "loaded_sqlite_status", "release_gate", "sqlite_wal_reset_status"]

WAIVER_ENV = "SAFEFORGE_ALLOW_UNFIXED_SQLITE"

_FIXED_FROM = (3, 51, 3)
_WITHDRAWN = frozenset({(3, 52, 0)})
# (first fixed, end of that line) -- branch backports named by sqlite.org/wal.html section 11.
_BACKPORTS = (((3, 44, 6), (3, 45, 0)), ((3, 50, 7), (3, 51, 0)))
_VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")


def sqlite_wal_reset_status(version_info: tuple[int, int, int], source_id: str) -> dict:
    """Classify one SQLite version. Pure; exact types (no str, no bool, no list)."""
    if type(version_info) is not tuple:
        raise TypeError("version_info must be a tuple of three int")
    if any(type(part) is not int for part in version_info):
        raise TypeError("version_info must be a tuple of three int")
    if len(version_info) != 3:
        raise ValueError("version_info must have exactly three parts")
    if any(part < 0 for part in version_info):
        raise ValueError("version_info parts must be >= 0")
    if type(source_id) is not str:
        raise TypeError("source_id must be str")
    v = version_info
    if v in _WITHDRAWN:
        fixed, reason = False, "3.52.0 was withdrawn (sqlite.org/changes.html); not accepted"
    elif v >= _FIXED_FROM:
        fixed, reason = True, "fixed in 3.51.3 and later (sqlite.org/wal.html section 11)"
    elif any(lo <= v < hi for lo, hi in _BACKPORTS):
        fixed, reason = True, "branch backport 3.44.6/3.50.7 (sqlite.org/wal.html section 11; no tagged release)"
    else:
        fixed, reason = False, "affected: 3.7.0 through 3.51.2 carry the WAL-reset bug (sqlite.org/wal.html section 11)"
    return {"fixed": fixed, "version": "%d.%d.%d" % v, "source_id": source_id, "reason": reason}


def loaded_sqlite_status(conn=None) -> dict:
    """Status of the library behind ``conn`` (default: a fresh ``:memory:`` connection).

    Reads ``sqlite_version()`` from the connection, not ``sqlite3.sqlite_version_info``.
    An unparseable version is reported as not fixed.
    """
    own = conn is None
    if own:
        conn = sqlite3.connect(":memory:")
    try:
        version, source_id = conn.execute("select sqlite_version(), sqlite_source_id()").fetchone()
    finally:
        if own:
            conn.close()
    m = _VERSION_RE.match(version) if type(version) is str else None
    if m is None:
        return {
            "fixed": False,
            "version": str(version),
            "source_id": str(source_id),
            "reason": "unparseable sqlite_version(); not accepted",
        }
    return sqlite_wal_reset_status(tuple(int(x) for x in m.groups()), str(source_id))


def release_gate(status: Mapping, env: Mapping[str, str]) -> dict:
    """Verdict for a release: only an exactly-True ``fixed`` passes without a waiver."""
    raw = env.get(WAIVER_ENV)
    reason = raw.strip() if raw is not None else None
    result = {"gate": "sqlite-wal-reset", "sqlite": dict(status), "waiver_env": WAIVER_ENV, "waiver_reason": reason}
    if status.get("fixed") is True:
        return {**result, "verdict": "pass", "exit_code": 0}
    if raw is None:
        return {**result, "verdict": "fail", "exit_code": 1}
    if not reason:
        return {**result, "verdict": "invalid-waiver", "exit_code": 2}
    return {**result, "verdict": "waived", "exit_code": 0}
