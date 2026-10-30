"""Release gate: ``python -m safeforge.sqlite_gate [--profile release|ci]``.

Fails (exit 1) when the SQLite library actually loaded by this interpreter
lacks the WAL-reset fix (see :mod:`safeforge.sqlite_runtime`). The default
profile ``release`` accepts no waiver (exit 3 if one is offered). Only
``--profile ci`` accepts ``SAFEFORGE_ALLOW_UNFIXED_SQLITE=expires=YYYY-MM-DD;
reason=<text>`` (exit 0, verdict "waived"); a malformed, expired or too-long
waiver exits 2. A GitHub Actions run for a tag or a release event is always
gated as ``release`` (SF-R39-01). The JSON verdict on stdout always names the
version, ``sqlite_source_id()`` and the profile applied.

Stdout carries exactly one JSON line. R40: under GitHub Actions
(``GITHUB_ACTIONS=true`` and ``GITHUB_STEP_SUMMARY`` set) the verdict is also
appended to the job summary, and from ``WAIVER_WARN_DAYS`` before a waiver's
expiry a warning goes to stderr and the summary -- the waiver turns the step
red on purpose when it runs out, so the people who must re-decide it hear of it
first. Workflow commands such as ``::warning::`` are not used: GitHub reads them
from stdout, which would break the one-line contract.

Kept out of the package's import graph on purpose: ``python -m`` on a module
the package already imported triggers runpy's double-import warning.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .sqlite_runtime import PROFILES, WAIVER_ENV, WAIVER_WARN_DAYS, loaded_sqlite_status, release_gate


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def summary_markdown(result: dict) -> str:
    """The job-summary block for one verdict (pure)."""
    lib = result["sqlite"]
    source = "canonical" if lib.get("source_canonical") is True else "modified or unknown"
    lines = [
        "### SQLite WAL-reset gate: `%s` (exit %d)" % (result["verdict"], result["exit_code"]),
        "",
        "- library: SQLite %s, source id %s (`%s`), fixed: %s"
        % (_one_line(lib.get("version")), source, _one_line(lib.get("source_id")), lib.get("fixed") is True),
        "- profile: %s (%s)" % (result["profile"], result["profile_source"]),
    ]
    waiver = result.get("waiver")
    if waiver:
        lines.append(
            "- waiver: expires %s (%d days left): %s" % (waiver["expires"], waiver["days_left"], _one_line(waiver["reason"]))
        )
        if waiver["days_left"] <= WAIVER_WARN_DAYS:
            lines += ["", "> **Warning:** %s" % expiry_warning(waiver)]
    elif result.get("waiver_error"):
        lines.append("- waiver rejected: %s" % _one_line(result["waiver_error"]))
    return "\n".join(lines) + "\n"


def expiry_warning(waiver: dict) -> str:
    return (
        "the SQLite waiver expires on %s (%d days left); after that this step fails. "
        "Re-decide it (owner decision E2) or run on SQLite >= 3.51.3." % (waiver["expires"], waiver["days_left"])
    )


def _write_step_summary(result: dict, env) -> None:
    path = env.get("GITHUB_STEP_SUMMARY")
    if env.get("GITHUB_ACTIONS") != "true" or not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(summary_markdown(result))
    except OSError as e:  # the verdict, not the report, decides the exit code
        print("could not write the job summary: %s" % e, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m safeforge.sqlite_gate", description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=PROFILES, default=None, help="default: release (no waiver)")
    args = parser.parse_args(argv)
    result = release_gate(loaded_sqlite_status(), os.environ, profile=args.profile)
    print(json.dumps(result, sort_keys=True))
    _write_step_summary(result, os.environ)
    waiver = result.get("waiver")
    if waiver and waiver["days_left"] <= WAIVER_WARN_DAYS:
        print("warning: " + expiry_warning(waiver), file=sys.stderr)
    if result["exit_code"] != 0:
        hint = {
            1: "set %s only under --profile ci, as expires=YYYY-MM-DD; reason=<text>" % WAIVER_ENV,
            2: "invalid waiver: %s" % result["waiver_error"],
            3: "a release accepts no waiver (profile from %s)" % result["profile_source"],
        }[result["exit_code"]]
        print(
            "SQLite %s lacks the WAL-reset fix (needs >= 3.51.3, not 3.52.0); %s" % (result["sqlite"].get("version"), hint),
            file=sys.stderr,
        )
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
