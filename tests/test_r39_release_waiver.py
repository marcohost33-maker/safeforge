"""SF-R39-01 (GPT-6 cross-family A2, 2026-10-28): a release cannot be waived through.

Finding on 89a0381 (src/safeforge/sqlite_runtime.py, release_gate): any
non-blank value of ``SAFEFORGE_ALLOW_UNFIXED_SQLITE`` turned an affected SQLite
into verdict "waived", exit 0. A variable inherited from a runner image, an
organisation-wide secret or a copied job definition was therefore enough to
pass a release job, with no owner and no end date.

Contract tested here:

* profile ``release`` is the default of both the function and the CLI and
  accepts no waiver: offered anyway -> "waiver-forbidden", exit 3;
* profile ``ci`` accepts exactly ``expires=YYYY-MM-DD; reason=<text>``;
  free text, unknown/duplicate keys, blank reason, impossible dates, a past
  expiry or one more than MAX_WAIVER_DAYS ahead -> "invalid-waiver", exit 2;
* a GitHub tag run or ``release`` event is gated as ``release`` even when
  ``--profile ci`` was passed (the release job cannot opt out);
* the expiry day itself is still valid (inclusive), the next day is not.

Each forger is paired with a positive control (a well-formed CI waiver passes,
a fixed library passes in every profile), so a gate that always refuses fails
this file as surely as the old one that always waived.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys

import pytest

from safeforge.sqlite_runtime import (
    MAX_WAIVER_DAYS,
    WAIVER_ENV,
    WaiverInvalid,
    effective_profile,
    loaded_sqlite_status,
    parse_waiver,
    release_gate,
    sqlite_wal_reset_status,
)

from .conftest import REPO, child_env

SID = "2026-03-13 00:00:00 0000000000000000000000000000000000000000000000000000000000000000"
FIXED = sqlite_wal_reset_status((3, 51, 3), SID)
UNFIXED = sqlite_wal_reset_status((3, 50, 4), SID)
TODAY = datetime.date(2026, 10, 30)
GOOD = "expires=2026-12-31; reason=PR CI is not a release (owner decision E2)"


def _gate(status, env, **kw):
    kw.setdefault("today", TODAY)
    return release_gate(status, env, **kw)


# -- the finding: an arbitrary text no longer passes a release ------------------


@pytest.mark.parametrize("waiver", ["yes", "1", "inherited from runner image", GOOD])
def test_release_default_forbids_any_waiver(waiver):
    r = _gate(UNFIXED, {WAIVER_ENV: waiver})
    assert (r["verdict"], r["exit_code"]) == ("waiver-forbidden", 3)
    assert r["profile"] == "release" and r["waiver"] is None


def test_release_without_waiver_fails():
    r = _gate(UNFIXED, {})
    assert (r["verdict"], r["exit_code"], r["waiver_offered"]) == ("fail", 1, False)


@pytest.mark.parametrize("profile", ["release", "ci"])
def test_fixed_library_passes_in_every_profile_positive_control(profile):
    r = _gate(FIXED, {WAIVER_ENV: "anything"}, profile=profile)
    assert (r["verdict"], r["exit_code"]) == ("pass", 0)


def test_ci_structured_waiver_passes_positive_control():
    r = _gate(UNFIXED, {WAIVER_ENV: GOOD}, profile="ci")
    assert (r["verdict"], r["exit_code"]) == ("waived", 0)
    assert r["waiver"] == {"expires": "2026-12-31", "reason": "PR CI is not a release (owner decision E2)"}


# -- CI waivers must be structured and time-bound -------------------------------


@pytest.mark.parametrize(
    "waiver, why",
    [
        ("PR CI is not a release", "unknown waiver field"),  # the old free-text form
        ("reason=no end date", "needs expires"),
        ("expires=2026-12-31", "needs expires"),
        ("expires=2026-12-31; reason=   ", "blank"),
        ("expires=2026-12-31; reason=a; reason=b", "twice"),
        ("expires=2026-12-31; reason=a; owner=me", "unknown waiver field"),
        ("expires=31.12.2026; reason=a", "YYYY-MM-DD"),
        ("expires=2026-02-30; reason=a", "not a date"),
        ("expires=+2026-12-31; reason=a", "YYYY-MM-DD"),
        ("expires=2026-10-29; reason=a", "expired"),
        ("expires=9999-12-31; reason=a", "days ahead"),
    ],
)
def test_ci_rejects_malformed_or_unbounded_waiver(waiver, why):
    r = _gate(UNFIXED, {WAIVER_ENV: waiver}, profile="ci")
    assert (r["verdict"], r["exit_code"]) == ("invalid-waiver", 2)
    assert why in r["waiver_error"]
    with pytest.raises(WaiverInvalid, match=why):
        parse_waiver(waiver, TODAY)


def test_expiry_day_is_inclusive_and_the_next_day_is_not():
    w = "expires=2026-10-30; reason=last day"
    assert _gate(UNFIXED, {WAIVER_ENV: w}, profile="ci")["verdict"] == "waived"
    later = _gate(UNFIXED, {WAIVER_ENV: w}, profile="ci", today=TODAY + datetime.timedelta(days=1))
    assert (later["verdict"], later["exit_code"]) == ("invalid-waiver", 2)


def test_horizon_boundary():
    edge = (TODAY + datetime.timedelta(days=MAX_WAIVER_DAYS)).isoformat()
    over = (TODAY + datetime.timedelta(days=MAX_WAIVER_DAYS + 1)).isoformat()
    assert parse_waiver("expires=%s; reason=x" % edge, TODAY)["expires"] == edge
    with pytest.raises(WaiverInvalid, match="days ahead"):
        parse_waiver("expires=%s; reason=x" % over, TODAY)


def test_keys_in_any_order_and_spacing():
    assert parse_waiver(" reason = r ;expires=2026-11-01 ; ", TODAY) == {"expires": "2026-11-01", "reason": "r"}


def test_reason_may_contain_equals_sign():
    assert parse_waiver("expires=2026-11-01; reason=a=b", TODAY)["reason"] == "a=b"


@pytest.mark.parametrize("bad", [None, b"expires=2026-11-01; reason=x", 7])
def test_parse_waiver_rejects_non_str(bad):
    with pytest.raises(WaiverInvalid, match="must be str"):
        parse_waiver(bad, TODAY)


def test_parse_waiver_today_exact_type():
    with pytest.raises(TypeError, match="today"):
        parse_waiver(GOOD, datetime.datetime(2026, 10, 30))


# -- a release job cannot opt out ----------------------------------------------


@pytest.mark.parametrize(
    "ctx, source",
    [({"GITHUB_REF_TYPE": "tag"}, "GITHUB_REF_TYPE=tag"), ({"GITHUB_EVENT_NAME": "release"}, "GITHUB_EVENT_NAME=release")],
)
def test_release_context_overrides_ci_profile(ctx, source):
    r = _gate(UNFIXED, {**ctx, WAIVER_ENV: GOOD}, profile="ci")
    assert (r["verdict"], r["exit_code"], r["profile"], r["profile_source"]) == ("waiver-forbidden", 3, "release", source)


@pytest.mark.parametrize("ctx", [{"GITHUB_REF_TYPE": "branch"}, {"GITHUB_EVENT_NAME": "pull_request"}, {}])
def test_non_release_context_keeps_ci_profile_positive_control(ctx):
    assert effective_profile("ci", ctx) == ("ci", "requested")


@pytest.mark.parametrize("bad", ["", "CI", "prod", None, b"ci"])
def test_unknown_profile_is_refused(bad):
    with pytest.raises(ValueError, match="profile must be one of"):
        release_gate(FIXED, {}, profile=bad)


# -- CLI against the real library -------------------------------------------------


def _cli(env_extra: dict[str, str], *args: str):
    env = child_env()
    for k in (WAIVER_ENV, "GITHUB_REF_TYPE", "GITHUB_EVENT_NAME"):
        env.pop(k, None)
    env.update(env_extra)
    p = subprocess.run(
        [sys.executable, "-m", "safeforge.sqlite_gate", *args],
        cwd=REPO,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return p.returncode, json.loads(p.stdout)


def _valid_waiver() -> str:
    expires = datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7)
    return "expires=%s; reason=cli test" % expires.isoformat()


def test_cli_inherited_free_text_waiver_does_not_pass_a_release():
    rc, out = _cli({WAIVER_ENV: "inherited"})
    if loaded_sqlite_status()["fixed"]:
        assert (rc, out["verdict"]) == (0, "pass")
    else:
        assert (rc, out["verdict"]) == (3, "waiver-forbidden")


def test_cli_tag_run_ignores_profile_ci():
    rc, out = _cli({WAIVER_ENV: _valid_waiver(), "GITHUB_REF_TYPE": "tag"}, "--profile", "ci")
    assert out["profile"] == "release"
    if not loaded_sqlite_status()["fixed"]:
        assert (rc, out["verdict"]) == (3, "waiver-forbidden")


def test_cli_ci_profile_with_structured_waiver_positive_control():
    rc, out = _cli({WAIVER_ENV: _valid_waiver()}, "--profile", "ci")
    assert rc == 0 and out["profile"] == "ci"
    assert out["verdict"] in ("pass", "waived")


def test_cli_rejects_unknown_profile():
    p = subprocess.run(
        [sys.executable, "-m", "safeforge.sqlite_gate", "--profile", "prod"],
        cwd=REPO,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 2 and "invalid choice" in p.stderr


# -- the workflow's own waiver --------------------------------------------------


def test_ci_workflow_waiver_is_structured_and_ci_only():
    """The waiver CI sets must parse, and only next to ``--profile ci``.

    Checked against its own expiry day, so the date rotation the waiver forces
    does not break this test; an expired waiver turns the CI step itself red.
    """
    import os
    import re

    path = os.path.join(REPO, ".github", "workflows", "ci.yml")
    if not os.path.exists(path):
        pytest.skip("workflow not shipped (sdist)")
    text = open(path, encoding="utf-8").read()
    values = re.findall(r'^\s*%s:\s*"([^"]*)"\s*$' % WAIVER_ENV, text, re.MULTILINE)
    assert values, "CI no longer sets a waiver: drop this test with it"
    gates = re.findall(r"python -m safeforge\.sqlite_gate[^\n]*", text)
    assert gates and all("--profile ci" in g for g in gates)
    for v in values:
        expires = re.search(r"expires=([0-9-]+)", v)
        assert expires, v
        day = datetime.date.fromisoformat(expires.group(1))
        assert parse_waiver(v, day)["expires"] == expires.group(1)
