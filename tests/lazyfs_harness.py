"""Start, control and stop a LazyFS mount for power-loss tests.

LazyFS (Ramos et al., "When Amnesia Strikes", PVLDB 17(11), 2024,
https://github.com/dsrhaslab/lazyfs) is a FUSE file system with its own page
cache. ``lazyfs::clear-cache`` drops every byte that was written but not yet
fsync'ed -- the data side of a power cut. Measured on the pinned build
(commit fa7d32e, see experiments/lazyfs/build_lazyfs.sh) before relying on it:

* unsynced file data is lost (a 5000-byte unsynced file comes back as 0 bytes);
* directory operations (create, link, unlink, rename) are passed straight to
  the backing file system and are NOT rolled back by clear-cache.

So LazyFS checks the *data* half of the protocol (file fsync, SQLite's WAL
syncs). The *directory-entry* half is covered by tests/powerloss_model.py.

LazyFS opens its "completed" FIFO once, for writing, at start-up and never
reopens it. The controller therefore keeps one reader open for the whole
lifetime of the mount; a reader that closed after the first command would make
every later completion message disappear (observed: the second clear-cache
never acknowledged).
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time

ENV_BIN = "SAFEFORGE_LAZYFS_BIN"

_CONFIG = """\
[faults]
fifo_path="{fifo}"
fifo_path_completed="{done}"
[cache]
apply_eviction=false
[cache.simple]
custom_size="{size}"
blocks_per_page=1
[filesystem]
log_all_operations=false
logfile="{log}"
"""


class LazyFS:
    """One mounted LazyFS instance below ``workdir``."""

    def __init__(self, binary: str, workdir: str, cache_size: str = "0.25gb") -> None:
        self.binary = binary
        self.workdir = workdir
        self.mnt = os.path.join(workdir, "mnt")
        self.backing = os.path.join(workdir, "backing")
        self.fifo = os.path.join(workdir, "faults.fifo")
        self.done = os.path.join(workdir, "done.fifo")
        self.log = os.path.join(workdir, "lazyfs.log")
        self.cache_size = cache_size
        self._proc: subprocess.Popen | None = None
        self._done_fd: int | None = None
        self.clears = 0

    def start(self, timeout: float = 30.0) -> None:
        os.makedirs(self.mnt, exist_ok=True)
        os.makedirs(self.backing, exist_ok=True)
        for p in (self.fifo, self.done):
            if os.path.exists(p):
                os.unlink(p)
            os.mkfifo(p, 0o600)
        cfg = os.path.join(self.workdir, "lazyfs.toml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(_CONFIG.format(fifo=self.fifo, done=self.done, size=self.cache_size, log=self.log))
        # O_NONBLOCK: open the read end before LazyFS opens the write end.
        self._done_fd = os.open(self.done, os.O_RDONLY | os.O_NONBLOCK)
        self.console = os.path.join(self.workdir, "lazyfs.console")
        with open(self.console, "wb") as out:
            self._proc = subprocess.Popen(
                # Relative config path, cwd=workdir: LazyFS main.cpp treats a
                # --config-path argument containing "-o" (strcasestr, so e.g.
                # /tmp/pytest-of-root/...) as a missing value and silently
                # falls back to config/default.toml.
                [self.binary, self.mnt, "--config-path", os.path.basename(cfg)]
                + ["-o", "modules=subdir", "-o", "subdir=" + self.backing, "-s", "-f"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                cwd=self.workdir,
            )
        deadline = time.monotonic() + timeout
        while not os.path.ismount(self.mnt):
            if self._proc.poll() is not None:
                raise RuntimeError("lazyfs exited with %s; see %s" % (self._proc.returncode, self.console))
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("lazyfs did not mount within %.0f s" % timeout)
            time.sleep(0.05)
        os.set_blocking(self._done_fd, True)

    def clear_cache(self, timeout: float = 30.0) -> None:
        """Drop all unsynced data and wait until LazyFS confirms it."""
        assert self._proc is not None and self._proc.poll() is None, "lazyfs is not running"
        with open(self.fifo, "w", encoding="ascii") as f:
            f.write("lazyfs::clear-cache\n")
        buf = b""
        deadline = time.monotonic() + timeout
        while b"finished::clear-cache\n" not in buf:
            if time.monotonic() > deadline:
                raise TimeoutError("lazyfs did not confirm clear-cache")
            r, _, _ = select.select([self._done_fd], [], [], 0.5)
            if r:
                chunk = os.read(self._done_fd, 4096)
                if not chunk:
                    raise RuntimeError("lazyfs closed the completion fifo")
                buf += chunk
        self.clears += 1

    def stop(self) -> None:
        if self._proc is not None:
            fusermount = shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount3"
            subprocess.run([fusermount, "-uz", self.mnt], capture_output=True)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            self._proc = None
        if self._done_fd is not None:
            os.close(self._done_fd)
            self._done_fd = None
