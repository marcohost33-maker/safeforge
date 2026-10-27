"""SF-XF-WAL (GPT-6 cross-family): gate on the SQLite library actually loaded, not on the Python package.

sqlite.org/wal.html section 11 ("The WAL-Reset Bug"): every SQLite from 3.7.0
through 3.51.2 can corrupt a WAL database when two or more connections on the
same file, in separate threads or processes, write or checkpoint at the same
instant. Fixed in 3.51.3; backports for 3.44.6 and 3.50.7. 3.52.0 was
withdrawn (sqlite.org/changes.html). python.org builds of 3.12..3.14 bundle
3.49.1 / 3.50.4, i.e. an affected library.

Contract tested here:

* ``sqlite_wal_reset_status(version_info, source_id)`` -- pure, exact types,
  boundaries 3.51.2 / 3.51.3 / 3.52.0 / 3.53.0 and the two backport ranges;
* ``loaded_sqlite_status()`` reads ``sqlite_version()`` from a live connection,
  so a patched ``sqlite3.sqlite_version_info`` cannot forge the answer;
* ``connect`` / ``ObjectStore`` take ``require_wal_reset_fix`` (exact bool,
  default False -- owner decision E2 open) and refuse an affected library with
  ``WalResetBugUnfixed`` (a ``ConfigurationError``) BEFORE touching the disk;
* ``ObjectStore.sqlite_runtime`` reports the library its own connection uses;
* the release gate (``python -m safeforge.sqlite_gate``) fails on an
  affected library unless a non-blank waiver reason is given, and prints it.

Every negative test has a positive control: a forced "fixed" status passes,
so an implementation that always refuses cannot pass this file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from safeforge import ConfigurationError, ObjectStore, WalResetBugUnfixed, connect, sqlite_runtime
from safeforge.sqlite_runtime import WAIVER_ENV, loaded_sqlite_status, release_gate, sqlite_wal_reset_status

from .conftest import REPO, child_env

SID = "2025-07-30 19:33:53 4d8adfb30e0000000000000000000000000000000000000000000000000000000"


def _real_source_id() -> str:
    con = sqlite3.connect(":memory:")
    try:
        return con.execute("select sqlite_source_id()").fetchone()[0]
    finally:
        con.close()


# -- pure function --------------------------------------------------------------


@pytest.mark.parametrize(
    "version, fixed",
    [
        ((3, 51, 2), False),  # last affected release
        ((3, 51, 3), True),  # first fixed release
        ((3, 52, 0), False),  # withdrawn
        ((3, 53, 0), True),
        ((3, 54, 1), True),
        ((4, 0, 0), True),
        ((3, 50, 4), False),  # python.org 3.13 / 3.14
        ((3, 49, 1), False),  # python.org 3.12
        ((3, 45, 1), False),  # ubuntu-24.04 libsqlite3
        ((3, 50, 6), False),
        ((3, 50, 7), True),  # backport (branch-3.50)
        ((3, 50, 9), True),
        ((3, 44, 5), False),
        ((3, 44, 6), True),  # backport (branch-3.44)
        ((3, 45, 0), False),  # 3.45 line has no backport
        ((3, 51, 0), False),
        ((3, 7, 0), False),
        ((3, 6, 23), False),
    ],
)
def test_status_boundaries(version, fixed):
    st = sqlite_wal_reset_status(version, SID)
    assert st["fixed"] is fixed
    assert st["version"] == "%d.%d.%d" % version
    assert st["source_id"] == SID
    assert isinstance(st["reason"], str) and st["reason"]


def test_status_reason_names_backport_and_withdrawal():
    assert "backport" in sqlite_wal_reset_status((3, 50, 7), SID)["reason"]
    assert "withdrawn" in sqlite_wal_reset_status((3, 52, 0), SID)["reason"]


@pytest.mark.parametrize(
    "bad",
    ["3.51.3", b"3.51.3", [3, 51, 3], (3, 51, True), (True, 51, 3), (3.0, 51, 3), (3, 51, "3"), None, 3513],
)
def test_status_rejects_non_tuple_or_non_int(bad):
    with pytest.raises(TypeError):
        sqlite_wal_reset_status(bad, SID)


@pytest.mark.parametrize("bad", [(3, 51), (3, 51, 3, 0), (), (3, -1, 0), (-3, 51, 3)])
def test_status_rejects_wrong_shape_or_negative(bad):
    with pytest.raises(ValueError):
        sqlite_wal_reset_status(bad, SID)


@pytest.mark.parametrize("bad", [None, b"abc", 7, True])
def test_status_rejects_non_str_source_id(bad):
    with pytest.raises(TypeError):
        sqlite_wal_reset_status((3, 51, 3), bad)


def test_status_accepts_module_version_info():
    # sqlite3.sqlite_version_info itself must be accepted.
    st = sqlite_wal_reset_status(sqlite3.sqlite_version_info, SID)
    assert st["version"] == sqlite3.sqlite_version


# -- loaded library ------------------------------------------------------------


def test_loaded_status_matches_live_library():
    st = loaded_sqlite_status()
    assert st["version"] == sqlite3.sqlite_version
    assert st["source_id"] == _real_source_id()
    assert st == sqlite_wal_reset_status(sqlite3.sqlite_version_info, _real_source_id())


def test_loaded_status_ignores_forged_module_attributes(monkeypatch):
    # Strongest forger: the Python-side version attributes claim a fixed library.
    real = loaded_sqlite_status()
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 53, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.53.0")
    assert loaded_sqlite_status() == real


def test_loaded_status_unparseable_version_is_not_fixed():
    class _Con:
        def execute(self, sql):
            class _Cur:
                def fetchone(self_inner):
                    return ("3.51.3-custom", SID)

            return _Cur()

    st = loaded_sqlite_status(_Con())
    assert st["fixed"] is False
    assert "unparseable" in st["reason"]


# -- connect -------------------------------------------------------------------


def _force(monkeypatch, fixed: bool):
    version = (3, 53, 0) if fixed else (3, 50, 4)
    st = sqlite_wal_reset_status(version, SID)
    calls = []

    def fake(conn=None):
        calls.append(conn)
        return st

    monkeypatch.setattr(sqlite_runtime, "loaded_sqlite_status", fake)
    return calls


@pytest.mark.parametrize("bad", [1, 0, "True", "yes", None])
def test_connect_flag_exact_bool(tmp_path, bad):
    path = tmp_path / "x.db"
    with pytest.raises(TypeError):
        connect(str(path), require_wal_reset_fix=bad)
    assert not path.exists()


def test_connect_refuses_affected_library_before_touching_disk(tmp_path, monkeypatch):
    calls = _force(monkeypatch, fixed=False)
    path = tmp_path / "x.db"
    with pytest.raises(WalResetBugUnfixed) as ei:
        connect(str(path), require_wal_reset_fix=True)
    assert calls, "the gate never consulted the runtime status"
    assert isinstance(ei.value, ConfigurationError)
    assert ei.value.status["fixed"] is False and ei.value.status["version"] == "3.50.4"
    assert "3.50.4" in str(ei.value)
    assert not path.exists()


def test_connect_accepts_fixed_library_positive_control(tmp_path, monkeypatch):
    calls = _force(monkeypatch, fixed=True)
    conn = connect(str(tmp_path / "x.db"), require_wal_reset_fix=True)
    try:
        assert calls
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_connect_default_does_not_block_affected_library(tmp_path, monkeypatch):
    calls = _force(monkeypatch, fixed=False)
    conn = connect(str(tmp_path / "x.db"))
    conn.close()
    assert calls == []  # default False: the check is not even consulted


def test_connect_real_runtime(tmp_path):
    fixed = loaded_sqlite_status()["fixed"]
    path = tmp_path / "x.db"
    if fixed:
        connect(str(path), require_wal_reset_fix=True).close()
    else:
        with pytest.raises(WalResetBugUnfixed):
            connect(str(path), require_wal_reset_fix=True)
        assert not path.exists()


# -- ObjectStore -----------------------------------------------------------------


def test_store_reports_its_own_runtime(tmp_path):
    with ObjectStore(str(tmp_path / "root")) as s:
        st = s.sqlite_runtime
        assert st["version"] == sqlite3.sqlite_version
        assert st["source_id"] == _real_source_id()
        assert st["fixed"] is sqlite_wal_reset_status(sqlite3.sqlite_version_info, _real_source_id())["fixed"]


@pytest.mark.parametrize("bad", [1, "yes", None])
def test_store_flag_exact_bool_before_side_effects(tmp_path, bad):
    root = tmp_path / "root"
    with pytest.raises(TypeError):
        ObjectStore(str(root), require_wal_reset_fix=bad)
    assert not root.exists()


def test_store_refuses_affected_library_before_creating_root(tmp_path, monkeypatch):
    calls = _force(monkeypatch, fixed=False)
    root = tmp_path / "root"
    with pytest.raises(WalResetBugUnfixed):
        ObjectStore(str(root), require_wal_reset_fix=True)
    assert calls
    assert not root.exists()


def test_store_accepts_fixed_library_positive_control(tmp_path, monkeypatch):
    _force(monkeypatch, fixed=True)
    with ObjectStore(str(tmp_path / "root"), require_wal_reset_fix=True) as s:
        assert s.sqlite_runtime["fixed"] is True


# -- release gate --------------------------------------------------------------

_FIXED = sqlite_wal_reset_status((3, 51, 3), SID)
_UNFIXED = sqlite_wal_reset_status((3, 51, 2), SID)


def test_gate_pass_on_fixed():
    r = release_gate(_FIXED, {})
    assert (r["verdict"], r["exit_code"]) == ("pass", 0)


def test_gate_fails_on_unfixed_without_waiver():
    r = release_gate(_UNFIXED, {})
    assert (r["verdict"], r["exit_code"]) == ("fail", 1)
    assert r["sqlite"] == _UNFIXED


def test_gate_waiver_is_visible():
    r = release_gate(_UNFIXED, {WAIVER_ENV: "  PR CI, not a release  "})
    assert (r["verdict"], r["exit_code"]) == ("waived", 0)
    assert r["waiver_reason"] == "PR CI, not a release"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_gate_blank_waiver_is_not_a_waiver(blank):
    r = release_gate(_UNFIXED, {WAIVER_ENV: blank})
    assert (r["verdict"], r["exit_code"]) == ("invalid-waiver", 2)


def test_gate_fixed_with_waiver_still_pass_and_reports_it():
    r = release_gate(_FIXED, {WAIVER_ENV: "reason"})
    assert (r["verdict"], r["exit_code"]) == ("pass", 0)
    assert r["waiver_reason"] == "reason"


@pytest.mark.parametrize("forged", ["yes", 1, None])
def test_gate_rejects_truthy_non_bool_fixed(forged):
    r = release_gate({**_UNFIXED, "fixed": forged}, {})
    assert r["exit_code"] != 0


def _run_gate(extra_env: dict[str, str] | None):
    env = child_env()
    env.pop(WAIVER_ENV, None)
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        [sys.executable, "-m", "safeforge.sqlite_gate"],
        cwd=REPO,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return p.returncode, json.loads(p.stdout), p.stderr


def test_gate_cli_real_runtime_without_waiver():
    rc, out, err = _run_gate(None)
    assert "RuntimeWarning" not in err  # runpy double import (gate module outside the package graph)
    fixed = loaded_sqlite_status()["fixed"]
    assert out["sqlite"]["version"] == sqlite3.sqlite_version
    assert out["sqlite"]["source_id"] == _real_source_id()
    assert rc == (0 if fixed else 1)
    assert out["verdict"] == ("pass" if fixed else "fail")


def test_gate_cli_real_runtime_with_waiver():
    rc, out, _ = _run_gate({WAIVER_ENV: "test waiver"})
    assert rc == 0
    assert out["waiver_reason"] == "test waiver"
    assert out["verdict"] == ("pass" if loaded_sqlite_status()["fixed"] else "waived")


def test_gate_cli_blank_waiver_fails():
    rc, out, _ = _run_gate({WAIVER_ENV: "  "})
    if loaded_sqlite_status()["fixed"]:
        assert rc == 0
    else:
        assert (rc, out["verdict"]) == (2, "invalid-waiver")


def test_exports():
    import safeforge

    assert "WalResetBugUnfixed" in safeforge.__all__
    assert issubclass(WalResetBugUnfixed, ConfigurationError)
    assert os.path.basename(sqlite_runtime.__file__) == "sqlite_runtime.py"
