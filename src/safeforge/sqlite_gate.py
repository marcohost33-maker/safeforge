"""Release gate: ``python -m safeforge.sqlite_gate``.

Fails (exit 1) when the SQLite library actually loaded by this interpreter
lacks the WAL-reset fix (see :mod:`safeforge.sqlite_runtime`), unless
``SAFEFORGE_ALLOW_UNFIXED_SQLITE`` carries a non-blank reason (exit 0, verdict
"waived", reason printed). A blank reason is not a waiver (exit 2). The JSON
verdict on stdout always names the version and ``sqlite_source_id()``.

Kept out of the package's import graph on purpose: ``python -m`` on a module
the package already imported triggers runpy's double-import warning.
"""

from __future__ import annotations

import json
import os
import sys

from .sqlite_runtime import WAIVER_ENV, loaded_sqlite_status, release_gate


def main() -> int:
    result = release_gate(loaded_sqlite_status(), os.environ)
    print(json.dumps(result, sort_keys=True))
    if result["exit_code"] != 0:
        print(
            "SQLite %s lacks the WAL-reset fix (needs >= 3.51.3, not 3.52.0); "
            "set %s=<reason> only for a documented exception" % (result["sqlite"].get("version"), WAIVER_ENV),
            file=sys.stderr,
        )
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
