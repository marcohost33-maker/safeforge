"""Child process for power-loss tests: one ingest, optional mutant, die at a failpoint.

    python -m tests.powerloss_driver <root> <point> <mutant> <trace.jsonl> [payload_bytes]

Like tests/crash_driver.py, ``os._exit`` ends the process at ``point`` (or
after acknowledging, for ``no-crash``). The power cut itself is applied
afterwards by the parent: LazyFS ``clear-cache`` for lost data, or the
directory-entry model in tests/powerloss_model.py.

The trace file (JSON lines) records

* ``fsync``: after every *real* ``durable.fsync_dir``, the directory and its
  entries at that moment -- durable from then on;
* ``seen``: at every failpoint, the entries of objects/ and staging/ -- names
  that existed at some time and so might have reached the disk.

It lives outside the store (and outside any LazyFS mount) and is read only
after this process has exited.

Mutants are negative controls. Each removes exactly one durability step from
the WRITER; the reconciler run by the parent is never mutated.

* ``none``              -- the library as shipped (positive control)
* ``no-staging-fsync``  -- staging data flushed to the OS but never fsync'ed
* ``no-dir-fsync``      -- directory fsyncs after publish skipped
* ``sync-normal``       -- SQLite ``synchronous=NORMAL`` instead of FULL
"""

from __future__ import annotations

import io
import json
import os
import sys

CRASH_EXIT = 86
MUTANTS = ("none", "no-staging-fsync", "no-dir-fsync", "sync-normal")


def main() -> int:
    root, point, mutant, trace = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    nbytes = int(sys.argv[5]) if len(sys.argv) > 5 else 65536
    if mutant not in MUTANTS:
        raise SystemExit("unknown mutant %r" % mutant)
    from safeforge import ObjectStore, _faults, durable

    real_fsync_dir = durable.fsync_dir

    def _record(kind: str, path: str) -> None:
        with open(trace, "a", encoding="utf-8") as t:
            t.write(json.dumps({"kind": kind, "dir": os.path.abspath(path), "entries": sorted(os.listdir(path))}) + "\n")

    def traced_fsync_dir(path: str) -> str:
        if mutant == "no-dir-fsync":
            return "ok"
        status = real_fsync_dir(path)
        _record("fsync", path)
        return status

    durable.fsync_dir = traced_fsync_dir
    if mutant == "no-staging-fsync":

        def flush_only(f) -> str:
            f.flush()
            return "ok"

        durable.fsync_file = flush_only

    def hook(name: str) -> None:
        for sub in ("objects", "staging"):
            _record("seen", os.path.join(root, sub))
        if name == point:
            os._exit(CRASH_EXIT)

    _faults.set_hook(hook)
    data = (b"safeforge-" * (nbytes // 10 + 1))[:nbytes]
    store = ObjectStore(root, synchronous="NORMAL" if mutant == "sync-normal" else "FULL")
    oid = store.ingest(io.BytesIO(data), max_bytes=1 << 30, chunk_size=4096).object_id
    sys.stdout.write("ACK %s\n" % oid)
    sys.stdout.flush()
    # Skip close(): a clean close would checkpoint the WAL (and fsync it),
    # hiding exactly what sync-normal is supposed to expose.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
