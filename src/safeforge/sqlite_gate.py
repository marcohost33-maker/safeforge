"""Release gate: ``python -m safeforge.sqlite_gate [--profile release|ci]``.

Fails (exit 1) when the SQLite library actually loaded by this interpreter
lacks the WAL-reset fix (see :mod:`safeforge.sqlite_runtime`). The default
profile ``release`` accepts no waiver (exit 3 if one is offered). Only
``--profile ci`` accepts ``SAFEFORGE_ALLOW_UNFIXED_SQLITE=expires=YYYY-MM-DD;
reason=<text>`` (exit 0, verdict "waived"); a malformed, expired or too-long
waiver exits 2. A GitHub Actions run for a tag or a release event is always
gated as ``release`` (SF-R39-01). The JSON verdict on stdout always names the
version, ``sqlite_source_id()`` and the profile applied.

Kept out of the package's import graph on purpose: ``python -m`` on a module
the package already imported triggers runpy's double-import warning.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .sqlite_runtime import PROFILES, WAIVER_ENV, loaded_sqlite_status, release_gate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m safeforge.sqlite_gate", description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=PROFILES, default="release")
    args = parser.parse_args(argv)
    result = release_gate(loaded_sqlite_status(), os.environ, profile=args.profile)
    print(json.dumps(result, sort_keys=True))
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
