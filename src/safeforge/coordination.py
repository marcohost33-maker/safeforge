"""R8 opt-in, POSIX-local *cooperative* store coordination.

This is NOT a mandatory filesystem fence. Every writer must opt in, including
webhook/event writes, reconciler jobs and migrations. Non-cooperating raw SQLite
clients and filesystem writers can bypass flock. Never use on network FS.
"""
from __future__ import annotations

import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

MODE = ".safeforge-coordinated-v1"
LOCK = ".safeforge-coordination.lock"
MARKER_CONTENT = b'R8 cooperative POSIX-only; raw writes are not fenced\n'
# A registry is only a process-local API consistency gate, NOT a security boundary.
# It binds opt-in to a live coordination context rather than a forgeable bool.
_held = threading.local()


def _contexts() -> dict:
    if getattr(_held, 'pid', None) != os.getpid():
        _held.pid = os.getpid()
        _held.contexts = {}
    return _held.contexts


def require_held(root: str | os.PathLike) -> None:
    """Reject a coordinated DB connection unless this thread actually holds a lock.

    Direct sqlite3 clients still bypass this *library* guard. Inherited/forked
    thread-local context is invalid because the PID must match.
    """
    key = str(Path(root).absolute())
    if not _contexts().get(key):
        raise CoordinationRejected('coordinated=True requires a live lock in this process/thread')


def _check_marker(root: Path) -> None:
    marker = root / MODE
    try:
        st = os.lstat(marker)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                st.st_uid != os.getuid() or (st.st_mode & 0o077)):
            raise CoordinationRejected('invalid coordination marker metadata')
        fd = os.open(marker, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                raise CoordinationRejected('coordination marker replaced')
            content = os.read(fd, len(MARKER_CONTENT) + 1)
        finally:
            os.close(fd)
        if content != MARKER_CONTENT:
            raise CoordinationRejected('invalid coordination marker content')
    except FileNotFoundError as exc:
        raise CoordinationRejected('coordination not initialized') from exc


class CoordinationRejected(RuntimeError):
    """Cannot safely establish cooperative coordination."""


def _fcntl():
    if os.name != 'posix':
        raise CoordinationRejected("POSIX flock is required; no Windows guarantee")
    import fcntl
    return fcntl


def _regular_fd(root: Path) -> int:
    path = root / LOCK
    # No symlink following, never replace the lock inode.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        os.close(fd)
        raise CoordinationRejected("lock must be a regular file")
    if os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise CoordinationRejected("lock must not have extra hard links")
    return fd


def _private_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise CoordinationRejected("source root must be a real directory")
    st = root.stat()
    if (st.st_mode & 0o077) or st.st_uid != os.getuid():
        raise CoordinationRejected("coordinated source root must be owned and mode 0700 or stricter")


def initialized(root: str | os.PathLike) -> bool:
    return os.path.lexists(os.fspath(Path(root) / MODE))


def initialize(root: str | os.PathLike, *, externally_quiescent: bool = False) -> None:
    """One-time migration, requiring the operator to stop all legacy writers."""
    if externally_quiescent is not True:
        raise CoordinationRejected("external quiescence required to migrate legacy writers")
    _fcntl()
    path = Path(root).absolute()
    _private_root(path)
    with lock(path, exclusive=True, require_marker=False):
        marker = path / MODE
        if marker.is_symlink():
            raise CoordinationRejected("coordination marker is a symlink")
        if not marker.exists():
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                os.write(fd, MARKER_CONTENT)
                os.fsync(fd)
            finally:
                os.close(fd)
            dfd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(dfd)
            finally: os.close(dfd)
        _check_marker(path)


@contextmanager
def lock(root: str | os.PathLike, *, exclusive: bool, require_marker: bool = True, nonblocking: bool = True):
    """Hold a live SH(writer) / EX(snapshot) flock until the context exits."""
    fcntl = _fcntl()
    root = Path(root).absolute()
    _private_root(root)
    if require_marker:
        _check_marker(root)
    fd = _regular_fd(root)
    try:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if nonblocking: flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise CoordinationRejected("coordinated writer/snapshot lock is busy") from exc
        # Detect replacement of the lock filename after opening (untrusted actors
        # with write permission on the root violate our private-directory model).
        opened, named = os.fstat(fd), os.lstat(root / LOCK)
        if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1):
            raise CoordinationRejected("lock inode replaced")
        # Validate marker AFTER acquiring the lock as well as before.
        if require_marker:
            _check_marker(root)
        key = str(root)
        contexts = _contexts()
        contexts[key] = contexts.get(key, 0) + 1
        try:
            yield
        finally:
            contexts[key] -= 1
            if not contexts[key]:
                del contexts[key]
    finally:
        os.close(fd)


@contextmanager
def event_writer(root: str | os.PathLike):
    """Event mutations must run on this connection within the shared lock."""
    from . import db
    with lock(root, exclusive=False):
        conn = db.connect(str(Path(root) / 'safeforge.db'), coordinated=True)
        try:
            yield conn
        finally:
            conn.close()
