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

R40 (2026-10-30): ``source_canonical`` says whether ``sqlite_source_id()`` has
the form of an unmodified check-in, "YYYY-MM-DD HH:MM:SS <64 hex>"; sqlite.org
(c3ref/c_source_id.html): "If the source code has been edited in any way since
it was last checked in, then the last four hexadecimal digits of the hash may be
modified." Measured on GitHub's ubuntu-latest (libsqlite3 3.45.1, 2026-10-30):
``...82257ccalt1`` -- a distribution-patched build. Only such a build can carry
a backport without a version bump, so the flag tells an operator where the
version-number decision may be too strict. It never changes the decision.

Release gate: ``python -m safeforge.sqlite_gate [--profile release|ci]`` prints
a JSON verdict and exits 0 (pass, or waived), 1 (affected library, no waiver),
2 (malformed, expired or too-long waiver) or 3 (waiver offered where none is
allowed).

SF-R39-01 (GPT-6 cross-family A2, 2026-10-28): the first gate accepted any
non-blank value of ``SAFEFORGE_ALLOW_UNFIXED_SQLITE`` as a waiver, so a variable
inherited from a runner, an organisation secret or a stale job definition was
enough to pass a release. Now:

* profile ``release`` (the default) never accepts a waiver: an affected library
  fails, and a waiver offered for it is reported as forbidden (exit 3);
* profile ``ci`` accepts only a structured, time-bound waiver
  ``expires=YYYY-MM-DD; reason=<text>``: unknown or repeated keys, a blank
  reason, a past expiry or an expiry more than ``MAX_WAIVER_DAYS`` ahead are
  invalid (exit 2), so every waiver turns red again and must be re-decided;
* a GitHub Actions run for a tag or a ``release`` event is a release whatever
  profile was asked for (``GITHUB_REF_TYPE`` / ``GITHUB_EVENT_NAME``), so a
  release job cannot opt out by passing ``--profile ci``.

Who may write a ``ci`` waiver is decided by review of the file that sets it
(the workflow), not by this module: there is no signature check, and an
attacker who controls the release job's command line or environment is out of
scope (they can skip the gate altogether).
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from collections.abc import Mapping

__all__ = [
    "MAX_WAIVER_DAYS",
    "PROFILES",
    "WAIVER_ENV",
    "WAIVER_WARN_DAYS",
    "effective_profile",
    "loaded_sqlite_status",
    "parse_waiver",
    "release_gate",
    "sqlite_wal_reset_status",
    "WaiverInvalid",
]

WAIVER_ENV = "SAFEFORGE_ALLOW_UNFIXED_SQLITE"
PROFILES = ("release", "ci")
# A waiver must come back for a new decision at least this often (SF-R39-01).
MAX_WAIVER_DAYS = 120
# The CLI warns this many days before a waiver expires and turns its CI step red (R40).
WAIVER_WARN_DAYS = 21
_WAIVER_KEYS = frozenset({"expires", "reason"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

_FIXED_FROM = (3, 51, 3)
_WITHDRAWN = frozenset({(3, 52, 0)})
# (first fixed, end of that line) -- branch backports named by sqlite.org/wal.html section 11.
_BACKPORTS = (((3, 44, 6), (3, 45, 0)), ((3, 50, 7), (3, 51, 0)))
_VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
_SOURCE_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9a-f]{64}$")


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
    return {
        "fixed": fixed,
        "version": "%d.%d.%d" % v,
        "source_id": source_id,
        "source_canonical": _SOURCE_ID_RE.match(source_id) is not None,
        "reason": reason,
    }


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
            "source_canonical": type(source_id) is str and _SOURCE_ID_RE.match(source_id) is not None,
            "reason": "unparseable sqlite_version(); not accepted",
        }
    return sqlite_wal_reset_status(tuple(int(x) for x in m.groups()), str(source_id))


class WaiverInvalid(ValueError):
    """A waiver that does not have the form ``expires=YYYY-MM-DD; reason=<text>``."""


def parse_waiver(raw: str, today: datetime.date) -> dict:
    """Parse ``expires=YYYY-MM-DD; reason=<text>`` (keys in any order, each exactly once).

    Raises :class:`WaiverInvalid` for a malformed, expired or too-long waiver.
    The waiver is valid up to and including its ``expires`` day (UTC).
    """
    if type(raw) is not str:
        raise WaiverInvalid("waiver must be str")
    if type(today) is not datetime.date:
        raise TypeError("today must be a datetime.date")
    fields: dict[str, str] = {}
    for part in raw.split(";"):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or key not in _WAIVER_KEYS:
            raise WaiverInvalid("unknown waiver field %r (expected expires=YYYY-MM-DD; reason=<text>)" % key)
        if key in fields:
            raise WaiverInvalid("waiver field %r given twice" % key)
        fields[key] = value.strip()
    if set(fields) != _WAIVER_KEYS:
        raise WaiverInvalid("waiver needs expires=YYYY-MM-DD and reason=<text>")
    if not fields["reason"]:
        raise WaiverInvalid("waiver reason is blank")
    if not _DATE_RE.match(fields["expires"]):
        raise WaiverInvalid("waiver expires must be YYYY-MM-DD")
    try:
        expires = datetime.date.fromisoformat(fields["expires"])
    except ValueError as e:
        raise WaiverInvalid("waiver expires is not a date: %s" % e) from None
    if expires < today:
        raise WaiverInvalid("waiver expired on %s" % expires.isoformat())
    if (expires - today).days > MAX_WAIVER_DAYS:
        raise WaiverInvalid("waiver expires more than %d days ahead" % MAX_WAIVER_DAYS)
    return {"expires": expires.isoformat(), "reason": fields["reason"]}


def effective_profile(requested: str, env: Mapping[str, str]) -> tuple[str, str]:
    """The profile the gate applies, and why. A GitHub tag or release run is always ``release``."""
    if type(requested) is not str or requested not in PROFILES:
        raise ValueError("profile must be one of %s" % ", ".join(PROFILES))
    if env.get("GITHUB_REF_TYPE") == "tag":
        return "release", "GITHUB_REF_TYPE=tag"
    if env.get("GITHUB_EVENT_NAME") == "release":
        return "release", "GITHUB_EVENT_NAME=release"
    return requested, "requested"


def release_gate(
    status: Mapping,
    env: Mapping[str, str],
    *,
    profile: str = "release",
    today: datetime.date | None = None,
) -> dict:
    """Verdict for a release: only an exactly-True ``fixed`` passes without a waiver.

    ``profile="release"`` (default) accepts no waiver; ``profile="ci"`` accepts a
    structured, unexpired waiver (:func:`parse_waiver`). See the module docstring.
    """
    applied, why = effective_profile(profile, env)
    if today is None:
        today = datetime.datetime.now(datetime.UTC).date()
    raw = env.get(WAIVER_ENV)
    offered = raw is not None
    result = {
        "gate": "sqlite-wal-reset",
        "sqlite": dict(status),
        "profile": applied,
        "profile_source": why,
        "waiver_env": WAIVER_ENV,
        "waiver_offered": offered,
        "waiver": None,
        "waiver_error": None,
    }
    if status.get("fixed") is True:
        return {**result, "verdict": "pass", "exit_code": 0}
    if not offered:
        return {**result, "verdict": "fail", "exit_code": 1}
    if applied == "release":
        return {**result, "verdict": "waiver-forbidden", "exit_code": 3, "waiver_error": "no waiver in a release"}
    try:
        waiver = parse_waiver(raw, today)
    except WaiverInvalid as e:
        return {**result, "verdict": "invalid-waiver", "exit_code": 2, "waiver_error": str(e)}
    days_left = (datetime.date.fromisoformat(waiver["expires"]) - today).days
    return {**result, "verdict": "waived", "exit_code": 0, "waiver": {**waiver, "days_left": days_left}}
