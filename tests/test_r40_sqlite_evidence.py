"""R40 (2026-10-30): close the blind spots left by R39.

1. The True path of ``durability_prerequisites_ok`` and the release gate's
   no-waiver pass had never run against a REAL SQLite with the WAL-reset fix:
   every CI runner links 3.45.1 / 3.49.1 / 3.50.4 (measured in run
   40984016099), and tests/conftest.py pins "fixed" for all other tests. CI job
   ``sqlite-fixed`` builds 3.54.1 from the official mirror and sets
   ``SAFEFORGE_EXPECT_FIXED_SQLITE=<version>``; then the ``required_*`` tests
   below must pass against that library, and a wrong library fails them --
   they never skip once the variable is set.
2. ``source_canonical`` marks a modified (e.g. distribution-patched) build:
   ubuntu-latest reports ``...82257ccalt1``.
3. A CI waiver expires by design; the CLI now warns ``WAIVER_WARN_DAYS``
   ahead (stderr, GitHub job summary) without breaking the one-JSON-line
   stdout contract.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys

import pytest

from safeforge import ObjectStore
from safeforge.sqlite_gate import summary_markdown
from safeforge.sqlite_runtime import (
    WAIVER_ENV,
    WAIVER_WARN_DAYS,
    loaded_sqlite_status,
    release_gate,
    sqlite_wal_reset_status,
)

from .conftest import REPO, child_env

CANONICAL = "2026-10-16 14:45:20 " + "57520227c95396ce39ba6ecaac8d475b78dd3964" + "0" * 24
UBUNTU_NOBLE = "2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1"
TODAY = datetime.date(2026, 10, 30)
EXPECT_FIXED = os.environ.get("SAFEFORGE_EXPECT_FIXED_SQLITE")
# Optional second pin: the exact sqlite_source_id() of the build (its Fossil check-in).
EXPECT_SOURCE_ID = os.environ.get("SAFEFORGE_EXPECT_FIXED_SQLITE_SOURCE_ID")


# -- source_canonical -----------------------------------------------------------


def test_canonical_source_id_is_recognised():
    assert len(CANONICAL.split()[2]) == 64
    assert sqlite_wal_reset_status((3, 54, 1), CANONICAL)["source_canonical"] is True


@pytest.mark.parametrize(
    "source_id",
    [
        UBUNTU_NOBLE,  # measured on GitHub ubuntu-latest, 2026-10-30
        CANONICAL[:-4] + "alt2",
        CANONICAL + "-modified",
        CANONICAL.upper(),
        CANONICAL[:-1],
        "",
        "3.54.1",
    ],
)
def test_modified_or_foreign_source_id_is_not_canonical(source_id):
    st = sqlite_wal_reset_status((3, 54, 1), source_id)
    assert st["source_canonical"] is False
    assert st["fixed"] is True  # reported, never decides


def test_unparseable_version_reports_source_canonical_too():
    class _Con:
        def execute(self, sql):
            class _Cur:
                def fetchone(self_inner):
                    return ("3.54.1-custom", UBUNTU_NOBLE)

            return _Cur()

    st = loaded_sqlite_status(_Con())
    assert (st["fixed"], st["source_canonical"]) == (False, False)


# -- days_left and the job summary -------------------------------------------


def _waived(expires: datetime.date) -> dict:
    unfixed = sqlite_wal_reset_status((3, 50, 4), CANONICAL)
    env = {WAIVER_ENV: "expires=%s; reason=ci" % expires.isoformat()}
    return release_gate(unfixed, env, profile="ci", today=TODAY)


@pytest.mark.parametrize("days", [0, 1, WAIVER_WARN_DAYS, WAIVER_WARN_DAYS + 1, 120])
def test_days_left_is_exact(days):
    r = _waived(TODAY + datetime.timedelta(days=days))
    assert (r["verdict"], r["waiver"]["days_left"]) == ("waived", days)


def test_summary_warns_inside_the_window_only():
    inside = summary_markdown(_waived(TODAY + datetime.timedelta(days=WAIVER_WARN_DAYS)))
    outside = summary_markdown(_waived(TODAY + datetime.timedelta(days=WAIVER_WARN_DAYS + 1)))
    assert "**Warning:**" in inside and "%d days left" % WAIVER_WARN_DAYS in inside
    assert "**Warning:**" not in outside


def test_summary_keeps_each_field_on_one_line():
    unfixed = sqlite_wal_reset_status((3, 50, 4), "x\n### forged heading")
    r = release_gate(unfixed, {WAIVER_ENV: "expires=2026-11-01; reason=a\n### forged"}, profile="ci", today=TODAY)
    md = summary_markdown(r)
    assert md.count("\n### ") == 0 and md.startswith("### SQLite WAL-reset gate")


def test_summary_names_a_rejected_waiver():
    unfixed = sqlite_wal_reset_status((3, 50, 4), CANONICAL)
    md = summary_markdown(release_gate(unfixed, {WAIVER_ENV: "free text"}, profile="ci", today=TODAY))
    assert "invalid-waiver" in md and "waiver rejected" in md


def _cli(extra: dict[str, str], *args: str):
    env = child_env()
    for k in (WAIVER_ENV, "GITHUB_REF_TYPE", "GITHUB_EVENT_NAME", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
        env.pop(k, None)
    env.update(extra)
    return subprocess.run(
        [sys.executable, "-m", "safeforge.sqlite_gate", *args],
        cwd=REPO,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _soon(days: int) -> str:
    expires = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=days)
    return "expires=%s; reason=cli test" % expires.isoformat()


def test_cli_writes_the_job_summary_and_warns_near_expiry(tmp_path):
    summary = tmp_path / "summary.md"
    p = _cli({WAIVER_ENV: _soon(3), "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}, "--profile", "ci")
    assert p.returncode == 0
    out = json.loads(p.stdout)  # stdout stays exactly one JSON document
    assert p.stdout.count("\n") == 1
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("### SQLite WAL-reset gate")
    if out["verdict"] == "waived":
        assert "**Warning:**" in text and "warning: the SQLite waiver expires" in p.stderr
    else:  # a fixed library passes without using the waiver: nothing to warn about
        assert out["verdict"] == "pass" and "**Warning:**" not in text


def test_cli_outside_github_actions_writes_no_summary(tmp_path):
    summary = tmp_path / "summary.md"
    p = _cli({WAIVER_ENV: _soon(3), "GITHUB_STEP_SUMMARY": str(summary)}, "--profile", "ci")
    assert p.returncode == 0 and not summary.exists()


def test_cli_unwritable_summary_does_not_change_the_verdict(tmp_path):
    p = _cli({"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(tmp_path)})  # a directory
    out = json.loads(p.stdout)
    assert p.returncode == out["exit_code"]
    assert "could not write the job summary" in p.stderr


# -- real library with the fix (CI job sqlite-fixed) --------------------------------

requires_fixed = pytest.mark.skipif(
    not EXPECT_FIXED, reason="set SAFEFORGE_EXPECT_FIXED_SQLITE=<version> (CI job sqlite-fixed) to run"
)


@pytest.mark.real_sqlite
@requires_fixed
def test_required_loaded_library_is_the_expected_fixed_build():
    st = loaded_sqlite_status()
    assert st["version"] == EXPECT_FIXED, st
    assert st["fixed"] is True, st
    assert st["source_canonical"] is True, st
    if EXPECT_SOURCE_ID is not None:
        assert st["source_id"] == EXPECT_SOURCE_ID, st


@pytest.mark.real_sqlite
@requires_fixed
def test_required_release_gate_passes_without_waiver():
    p = _cli({})
    out = json.loads(p.stdout)
    assert (p.returncode, out["verdict"], out["profile"], out["waiver_offered"]) == (0, "pass", "release", False)
    assert out["sqlite"]["version"] == EXPECT_FIXED


@pytest.mark.real_sqlite
@requires_fixed
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the job builds the library on Linux")
def test_required_durability_flag_is_true_on_the_real_fixed_library(tmp_path):
    # Assertion first: on a wrong library this fails as an assertion, not as WalResetBugUnfixed.
    assert loaded_sqlite_status()["fixed"] is True
    with ObjectStore(str(tmp_path / "root"), require_wal_reset_fix=True) as s:
        r = s.ingest(io.BytesIO(b"real fixed library"), max_bytes=1 << 20)
    assert (r.sqlite_version, r.sqlite_wal_reset_fixed) == (EXPECT_FIXED, True)
    assert r.durability_prerequisites_ok is True, r
