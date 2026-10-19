"""Child process for crash tests: run one ingest and die abruptly at a failpoint.

    python -m tests.crash_driver <design> <root> <point> <payload_bytes> [staging_dir]

``os._exit`` ends the process immediately: no ``finally``, no ``atexit``, no
Python-level buffer flush, open SQLite transactions are NOT committed -- the
same observable effect for the database and the file system as SIGKILL /
TerminateProcess. The OS page cache survives (this is a process crash, not a
power loss).

Prints ``ACK <id>`` only after the ingest call has returned.
"""

from __future__ import annotations

import io
import os
import sys

CRASH_EXIT = 86


def main() -> int:
    design, root, point, nbytes = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    staging = sys.argv[5] if len(sys.argv) > 5 else None
    from safeforge import _faults

    def hook(name: str) -> None:
        if name == point:
            os._exit(CRASH_EXIT)

    _faults.set_hook(hook)
    data = (b"safeforge-" * (nbytes // 10 + 1))[:nbytes]
    if design == "corrected":
        from safeforge import ObjectStore

        s = ObjectStore(root)
        oid = s.ingest(io.BytesIO(data), max_bytes=1 << 30, chunk_size=4096).object_id
    elif design == "v03":
        from tests.designs.v03_reconstructed import V03Store

        oid = V03Store(root, staging).ingest(data)
    elif design == "naive_file_first":
        from tests.designs.naive import NaiveStore

        oid = NaiveStore(root).ingest_file_first(data)
    elif design == "naive_row_first":
        from tests.designs.naive import NaiveStore

        oid = NaiveStore(root).ingest_row_first(data)
    else:
        raise SystemExit("unknown design %r" % design)
    sys.stdout.write("ACK %s\n" % oid)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
