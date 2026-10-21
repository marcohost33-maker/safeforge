"""Discrimination run for the coordination checks (SF-R9-04 and R11).

Deletes one check at a time from a COPY of the source tree and runs the
coordination tests against it. A check is discriminated when at least one test
goes red without it; a surviving mutant means the tests do not need that check.
The unmutated copy must be green first (positive control).

Linux only (real flock, O_NOFOLLOW, chown when run as root).

    python experiments/dk_coordination_mutants.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from _common import save

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = ["tests/test_r11_coordination_forgers.py", "tests/test_r9_context_attestation.py"]
C = "src/safeforge/coordination.py"
D = "src/safeforge/db.py"

# (id, file, old, new): old must occur exactly once in the file.
MUTANTS = [
    ("marker-type", C, "if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1", "if st.st_nlink != 1"),
    ("marker-nlink", C, "or st.st_nlink != 1 or st.st_uid", "or st.st_uid"),
    ("marker-owner", C, "or st.st_uid != os.getuid() or (st.st_mode & 0o077)", "or (st.st_mode & 0o077)"),
    ("marker-mode", C, " or (st.st_mode & 0o077):", ":"),
    ("marker-nofollow", C, 'os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))', "os.open(marker, os.O_RDONLY)"),
    ("marker-inode", C, "if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):", "if False:"),
    ("marker-content", C, "if content != MARKER_CONTENT:", "if False:"),
    ("marker-oserror-map", C, "    except OSError as exc:\n        # R11 (SF-R11-01): a marker",
     "    except ZeroDivisionError as exc:\n        # R11 (SF-R11-01): a marker"),
    ("lock-nofollow", C, 'os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)',
     "os.open(path, os.O_CREAT | os.O_RDWR, 0o600)"),
    ("lock-oserror-map", C, "    except OSError as exc:\n        # R11 (SF-R11-01): symlink",
     "    except ZeroDivisionError as exc:\n        # R11 (SF-R11-01): symlink"),
    ("lock-type", C, "if not stat.S_ISREG(mode):", "if False:"),
    ("lock-nlink", C, "if os.fstat(fd).st_nlink != 1:", "if False:"),
    ("lock-replaced-after-flock", C, "(opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)", "False"),
    ("marker-recheck-after-flock", C, "before.\n        if require_marker:", "before.\n        if False:"),
    ("root-symlink", C, "if root.is_symlink() or not root.is_dir():", "if not root.is_dir():"),
    ("root-isdir", C, "if root.is_symlink() or not root.is_dir():", "if root.is_symlink():"),
    ("root-mode", C, "if (st.st_mode & 0o077) or st.st_uid != os.getuid():", "if st.st_uid != os.getuid():"),
    ("root-owner", C, "if (st.st_mode & 0o077) or st.st_uid != os.getuid():", "if (st.st_mode & 0o077):"),
    ("connect-lexical-parent", D, "parent = os.path.dirname(os.fspath(path)) or os.curdir",
     "parent = os.path.dirname(os.path.abspath(path))"),
]


def _copy() -> str:
    dst = tempfile.mkdtemp(prefix="dk-coord-")
    for name in ("src", "tests", "pyproject.toml"):
        src = os.path.join(REPO, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dst, name), ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, os.path.join(dst, name))
    return dst


def _run(tree: str) -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf", *TESTS],
        cwd=tree, capture_output=True, text=True, timeout=300,
    )
    failed = re.findall(r"^FAILED (\S+)", proc.stdout, re.M)
    return proc.returncode, failed


def main() -> int:
    if sys.platform != "linux":
        print("Linux only")
        return 2
    t0 = time.time()
    tree = _copy()
    try:
        rc, failed = _run(tree)
        if rc != 0:
            print("positive control RED, aborting:", failed)
            return 1
        rows = []
        for mid, rel, old, new in MUTANTS:
            path = os.path.join(tree, rel)
            with open(path, encoding="utf-8") as f:
                original = f.read()
            if original.count(old) != 1:
                raise SystemExit("mutant %s: anchor found %d times" % (mid, original.count(old)))
            with open(path, "w", encoding="utf-8") as f:
                f.write(original.replace(old, new))
            try:
                rc, failed = _run(tree)
            finally:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(original)
            killed = rc != 0 and bool(failed)
            rows.append({"mutant": mid, "killed": killed, "killed_by": [x.split("::", 1)[-1] for x in failed]})
            print("%-28s %s %s" % (mid, "KILLED " if killed else "SURVIVED", ", ".join(r for r in rows[-1]["killed_by"])))
        rc, failed = _run(tree)
        restored = rc == 0
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    survived = [r["mutant"] for r in rows if not r["killed"]]
    print("\n%d/%d mutants killed; restore green: %s" % (len(rows) - len(survived), len(rows), restored))
    path = save("dk_coordination_mutants", {
        "euid": os.geteuid(),
        "tests": TESTS,
        "mutants": rows,
        "survived": survived,
        "restore_green": restored,
        "seconds": round(time.time() - t0, 1),
    })
    print("wrote", path)
    return 0 if not survived and restored else 1


if __name__ == "__main__":
    sys.exit(main())
