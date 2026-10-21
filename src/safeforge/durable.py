"""Durability primitives: file fsync, directory fsync, same-volume publish.

Why each step exists (POSIX):

1. ``fsync(file)`` BEFORE publication. Without it, the newly published name
   might refer to unwritten content following power loss.
2. ``link(src, dst)`` creates ``dst`` exclusively and atomically on a single
   filesystem; a concurrent existing ``dst`` is never overwritten.
   ``unlink(src)`` is a SECOND operation. A crash between them leaves two
   directory entries for the same inode, handled by the reconciler.
   On filesystems without hardlinks, publication fails closed.
3. ``fsync(objects_dir)`` and ``fsync(staging_dir)`` after publication persist
   the creation and removal of directory entries on supported POSIX filesystems.
   These calls do not constitute a tested whole-device power-loss proof.

Windows has no directory fsync that Python can reach (``os.open`` on a
directory raises ``PermissionError``). The best documented mechanism is
``MoveFileExW(..., MOVEFILE_WRITE_THROUGH)``, which "does not return until the
file is actually moved on the disk" (Microsoft Learn). Microsoft does not
document the rename as atomic; this module reports what it did instead of
claiming more than the platform guarantees.
"""

from __future__ import annotations

import errno
import os
import sys
from typing import IO

from . import _faults

__all__ = [
    "CrossDeviceError",
    "fsync_fd",
    "fsync_file",
    "fsync_dir",
    "publish",
    "require_same_volume",
]


class CrossDeviceError(OSError):
    """Source and destination are on different volumes; atomic publication is impossible."""


def fsync_fd(fd: int) -> str:
    """Force ``fd``'s data to stable storage; returns ``"ok"`` or ``"degraded:<reason>"``.

    macOS: fsync(2) "will flush all data from the host to the drive", but the
    drive may keep it in its cache; Apple's man page points to F_FULLFSYNC for
    data that must survive power loss, and CPython's os.fsync does not issue it
    (bpo-3512 / bpo-11877). R12: use F_FULLFSYNC there, as SQLite (PRAGMA
    fullfsync), RocksDB and Rust's ``File::sync_all`` do, and fall back to
    fsync like SQLite does when the filesystem refuses it -- but say so instead
    of reporting a plain "ok".
    """
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return "ok"
        except OSError:
            os.fsync(fd)
            return "degraded:darwin-F_FULLFSYNC-refused-fsync-only"
    os.fsync(fd)
    return "ok"


def fsync_file(f: IO[bytes]) -> str:
    """Flush Python buffers and force the file's data to stable storage (see :func:`fsync_fd`)."""
    f.flush()
    return fsync_fd(f.fileno())


def fsync_dir(path: str) -> str:
    """fsync a directory so that entries created/renamed in it become durable.

    Returns ``"ok"``, ``"degraded:<reason>"`` (macOS without F_FULLFSYNC, see
    :func:`fsync_fd`) or ``"unsupported:<reason>"`` (Windows). Never silently
    skips: the caller receives the status and can surface it.
    """
    if sys.platform == "win32":
        return "unsupported:windows-has-no-directory-fsync"
    fd = os.open(path, os.O_RDONLY)
    try:
        return fsync_fd(fd)
    finally:
        os.close(fd)


def _device(path: str) -> int:
    return os.stat(path).st_dev


def require_same_volume(a: str, b: str) -> None:
    """Raise :class:`CrossDeviceError` unless both existing paths share a device."""
    da, db = _device(a), _device(b)
    if da != db:
        raise CrossDeviceError(
            errno.EXDEV,
            "staging and destination are on different volumes (st_dev %r != %r); "
            "same-volume publication would not be possible" % (da, db),
            a,
        )


_MOVEFILE_WRITE_THROUGH = 0x8
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_EXISTS = 80
_ERROR_NOT_SAME_DEVICE = 17


def _publish_windows(src: str, dst: str) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    # No MOVEFILE_REPLACE_EXISTING: never clobber. No MOVEFILE_COPY_ALLOWED:
    # a cross-volume move fails instead of degrading to copy+delete.
    if not move(src, dst, _MOVEFILE_WRITE_THROUGH):
        err = ctypes.get_last_error()
        if err in (_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS):
            raise FileExistsError(errno.EEXIST, "destination exists", dst)
        if err == _ERROR_NOT_SAME_DEVICE:
            raise CrossDeviceError(errno.EXDEV, "cross-volume move refused", dst)
        raise ctypes.WinError(err)
    return "windows:MoveFileExW(WRITE_THROUGH)"


def publish(src: str, dst: str) -> str:
    """Publish ``src`` to a NEW name ``dst`` on the same volume.

    POSIX uses atomic no-replace *creation* via hard link followed by a separate
    unlink of the staging name; both names can coexist after a crash. This is
    not a single atomic move. A filesystem without hard links is rejected.
    Windows uses MoveFileExW as documented, with platform limits unchanged.
    The caller must fsync both directories after publication.
    """
    if sys.platform == "win32":
        return _publish_windows(src, dst)
    # A check-then-rename can overwrite a path installed by a competing writer.
    # POSIX link(2) creates the final name *exclusively* in one operation.
    # Unlinking the staging name is a separate operation: recovery may see both
    # names after a crash, and the existing reconciler handles that condition.
    try:
        os.link(src, dst, follow_symlinks=False)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossDeviceError(errno.EXDEV, "cross-device publish refused", dst) from e
        raise  # EEXIST, EOPNOTSUPP and permissions fail closed.
    # Failpoint intentionally between atomic exclusive publish and unlink.
    # If killed here, reconcile must roll forward and purge staging.
    _faults.point("object.linked")
    os.unlink(src)
    return "posix:link+unlink(no-replace)"
