from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def env() -> dict:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, stdin=subprocess.DEVNULL
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=HERE, capture_output=True, text=True, stdin=subprocess.DEVNULL
            ).stdout.strip()
        )
    except OSError:
        head, dirty = "", None
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "cpu_count": os.cpu_count(),
        "git_head": head,
        "git_dirty": dirty,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def save(name: str, payload: dict) -> str:
    out_dir = os.path.join(HERE, "results")
    os.makedirs(out_dir, exist_ok=True)
    tag = "%s-%s-py%s" % (name, sys.platform, sys.version_info[0] * 100 + sys.version_info[1])
    path = os.path.join(out_dir, tag + ".json")
    # env() BEFORE opening: open(..., "w") truncates the tracked result file,
    # and git would then report the tree as dirty because of this very write.
    meta = env()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"env": meta, **payload}, f, indent=2, sort_keys=True)
        f.write("\n")
    return path
