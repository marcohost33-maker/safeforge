"""Discrimination run for the guards of R9-R12 (coordination, containment, durability report).

Deletes one check at a time from a COPY of the source tree and runs the
coordination tests against it. A check is discriminated when at least one test
goes red without it; a surviving mutant means the tests do not need that check.
The unmutated copy must be green first (positive control).

Linux only (real flock, O_NOFOLLOW, chown when run as root).

    python experiments/dk_guard_mutants.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from xml.etree import ElementTree

from _common import save

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = [
    "tests/test_r11_coordination_forgers.py",
    "tests/test_r9_context_attestation.py",
    "tests/test_r12_hardening.py",
    "tests/test_r11_durability_report.py",
]
C = "src/safeforge/coordination.py"
D = "src/safeforge/db.py"
B = "src/safeforge/offline_backup.py"
U = "src/safeforge/durable.py"

# (id, file, old, new): old must occur exactly once in the file.
MUTANTS = [
    # marker (SF-R9-04, R11)
    ("marker-type", C, "if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1", "if st.st_nlink != 1"),
    ("marker-nlink", C, "or st.st_nlink != 1 or st.st_uid", "or st.st_uid"),
    ("marker-owner", C, "or st.st_uid != os.geteuid() or (st.st_mode & 0o077)", "or (st.st_mode & 0o077)"),
    ("marker-owner-real-uid", C, "or st.st_uid != os.geteuid() or (st.st_mode & 0o077)",
     "or st.st_uid != os.getuid() or (st.st_mode & 0o077)"),
    ("marker-mode", C, " or (st.st_mode & 0o077):", ":"),
    ("marker-nofollow", C, "os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)", "os.open(marker, os.O_RDONLY)"),
    ("marker-inode", C, "if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):", "if False:"),
    ("marker-content", C, "if content != MARKER_CONTENT:", "if False:"),
    ("marker-oserror-map", C, "    except OSError as exc:\n        # R11 (SF-R11-01): a marker",
     "    except ZeroDivisionError as exc:\n        # R11 (SF-R11-01): a marker"),
    ("marker-recheck-after-flock", C, "before.\n        if require_marker:", "before.\n        if False:"),
    # lock file
    ("nofollow-required", C, 'if not hasattr(os, "O_NOFOLLOW"):', "if False:"),
    ("lock-nofollow", C, 'os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)',
     "os.open(path, os.O_CREAT | os.O_RDWR, 0o600)"),
    ("lock-oserror-map", C, "    except OSError as exc:\n        # R11 (SF-R11-01): symlink",
     "    except ZeroDivisionError as exc:\n        # R11 (SF-R11-01): symlink"),
    ("lock-type", C, "if not stat.S_ISREG(st.st_mode):\n        os.close(fd)", "if False:\n        os.close(fd)"),
    ("lock-nlink", C, "if st.st_nlink != 1:\n        os.close(fd)", "if False:\n        os.close(fd)"),
    ("lock-owner", C, "(st.st_uid != os.geteuid() or st.st_mode & 0o077)", "(st.st_mode & 0o077)"),
    ("lock-mode", C, "(st.st_uid != os.geteuid() or st.st_mode & 0o077)", "(st.st_uid != os.geteuid())"),
    ("lock-replaced-after-flock", C, "(opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)", "False"),
    # root
    ("root-symlink", C, "if root.is_symlink() or not root.is_dir():", "if not root.is_dir():"),
    ("root-isdir", C, "if root.is_symlink() or not root.is_dir():", "if root.is_symlink():"),
    ("root-mode", C, "if (st.st_mode & 0o077) or st.st_uid != os.geteuid():", "if st.st_uid != os.geteuid():"),
    ("root-owner", C, "if (st.st_mode & 0o077) or st.st_uid != os.geteuid():", "if (st.st_mode & 0o077):"),
    # registry identity (SF-P3-03)
    ("connect-lexical-parent", D, "parent = os.path.dirname(os.fspath(path)) or os.curdir",
     "parent = os.path.dirname(os.path.abspath(path))"),
    # containment (SF-R12-01)
    ("containment-lexical", B, "c, p = os.path.realpath(child), os.path.realpath(parent)",
     "c, p = os.path.abspath(child), os.path.abspath(parent)"),
    ("containment-other-drive", B, "    except ValueError:\n        return False",
     "    except ZeroDivisionError:\n        return False"),
    # durability reporting (SF-P2-01, SF-R12-02)
    ("darwin-fullfsync", U, 'if sys.platform == "darwin":\n        import fcntl', 'if False:\n        import fcntl'),
    ("darwin-fallback-says-ok", U, 'return "degraded:darwin-F_FULLFSYNC-refused-fsync-only"', 'return "ok"'),
    ("sqlite-fullfsync", D, 'if sys.platform == "darwin" and sync != "NORMAL":', "if False:"),
    ("report-dir-syncs-dropped", B, '{"directory_fsync": _summary(dir_syncs)', '{"directory_fsync": _summary([])'),
    ("report-file-syncs-dropped", B, '"file_fsync": _summary(file_syncs)', '"file_fsync": _summary([])'),
    ("report-nested-split", B, 'for part in s.split(";")', 'for part in [s]'),
]


def _copy() -> str:
    dst = tempfile.mkdtemp(prefix="dk-guard-")
    for name in ("src", "tests", "pyproject.toml"):
        src = os.path.join(REPO, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dst, name), ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, os.path.join(dst, name))
    return dst


def _run(tree: str) -> tuple[int, list[tuple[str, bool]]]:
    """Run the tests; return (exit code, [(failed test, failed by assertion?)]).

    Like Vero's diskriminierung.py, only a test that fails by ASSERTION
    discriminates: a test that dies of an unrelated exception would be red for
    a reason that has nothing to do with the guard. The exception type comes
    from JUnit XML; pytest's terminal summary truncates it to the line width.
    """
    report = os.path.join(tree, "junit.xml")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml", report, *TESTS],
        cwd=tree, capture_output=True, text=True, timeout=300,
    )
    failed = []
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        for bad in [*case.findall("failure"), *case.findall("error")]:
            # "AssertionError: msg", "Failed: DID NOT RAISE", or a rewritten bare
            # "assert x == y" (no type prefix at all); anything else is a crash.
            message = bad.get("message") or ""
            by_assertion = message.startswith(("AssertionError", "Failed:", "assert "))
            name = "%s::%s" % (case.get("classname", "").replace(".", "/") + ".py", case.get("name"))
            failed.append((name, bad.tag == "failure" and by_assertion))
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
            by = [t.split("::", 1)[-1] for t, by_assert in failed if by_assert]
            crashed = [t.split("::", 1)[-1] for t, by_assert in failed if not by_assert]
            killed = rc != 0 and bool(by)
            rows.append({"mutant": mid, "killed": killed, "killed_by": by, "red_by_crash": crashed})
            verdict = "KILLED  " if killed else ("CRASH   " if crashed else "SURVIVED")
            print("%-28s %s %s" % (mid, verdict, ", ".join(by or crashed)))
        rc, failed = _run(tree)
        restored = rc == 0
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    survived = [r["mutant"] for r in rows if not r["killed"]]
    print("\n%d/%d mutants killed; restore green: %s" % (len(rows) - len(survived), len(rows), restored))
    path = save("dk_guard_mutants", {
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
