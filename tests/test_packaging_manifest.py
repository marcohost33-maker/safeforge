"""The sdist must carry what the test suite needs (MANIFEST.in).

setuptools' default sdist ships tests/test_*.py but not conftest.py,
crash_driver.py, invariants.py or tests/designs/; the sdist's own tests then
fail to collect. This test evaluates the small subset of MANIFEST.in used here
(graft, include, global-exclude) and FAILS on any other directive rather than
guessing its meaning. The authoritative check is still building the sdist and
running pytest inside it (see CHANGELOG); this guards the rule on every run.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KNOWN = {"graft", "include", "global-exclude"}


def _rules(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        directive, *args = line.split()
        if directive not in KNOWN:
            raise ValueError(f"unsupported MANIFEST.in directive {directive!r}")
        rules.append((directive, args))
    return rules


def _shipped(rel: str, rules) -> bool:
    included = False
    for directive, args in rules:
        if directive == "graft" and any(rel == a or rel.startswith(a.rstrip("/") + "/") for a in args):
            included = True
        elif directive == "include" and any(fnmatch.fnmatch(rel, a) for a in args):
            included = True
        elif directive == "global-exclude" and any(fnmatch.fnmatch(Path(rel).name, a) or a in Path(rel).parts for a in args):
            included = False
    return included


def _needed() -> list[str]:
    """Every tracked-looking support file under tests/ that is not a test module, plus docs/."""
    support = [p for p in (ROOT / "tests").rglob("*.py") if not p.name.startswith("test_") and "__pycache__" not in p.parts]
    docs = [p for p in (ROOT / "docs").rglob("*") if p.is_file()]
    return sorted(p.relative_to(ROOT).as_posix() for p in support + docs)


def test_sdist_manifest_ships_test_dependencies():
    rules = _rules((ROOT / "MANIFEST.in").read_text(encoding="utf-8"))
    needed = _needed()
    # positive control: the files the suite imports are in the list at all
    expected = {
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/crash_driver.py",
        "tests/invariants.py",
        "tests/designs/naive.py",
        "tests/designs/v03_reconstructed.py",
        "docs/R3_SECURITY_DECISIONS.md",
    }
    assert expected <= set(needed)
    assert [rel for rel in needed if not _shipped(rel, rules)] == []
    assert not _shipped("tests/__pycache__/conftest.cpython-312.pyc", rules)


@pytest.mark.parametrize(
    "text, rel, shipped",
    [
        ("graft tests\n", "tests/designs/naive.py", True),
        ("", "tests/conftest.py", False),
        ("graft tests\nglobal-exclude *.py[cod]\n", "tests/x.pyc", False),
        ("include CHANGELOG.md\n", "docs/R3_SECURITY_DECISIONS.md", False),
    ],
)
def test_manifest_evaluator_controls(text, rel, shipped):
    assert _shipped(rel, _rules(text)) is shipped


def test_manifest_evaluator_refuses_unknown_directives():
    with pytest.raises(ValueError, match="unsupported"):
        _rules("prune tests\n")
