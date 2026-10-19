"""Durability primitives: file fsync, directory fsync, same-volume publish.

Why each step exists (POSIX):

1. ``fsync(file)`` BEFORE the rename. Without it, the rename (metadata) can
   reach the disk before the data blocks. After a power loss the new name may
   then point at a zero-length or partially written file.
2. ``rename`` within ONE filesystem. POSIX ``rename(2)`` is atomic only there;
   across filesystems it fails with ``EXDEV``. Copy-and-delete fallbacks such as
   ``shutil.move`` are NOT atomic: a crash mid-copy leaves a truncated file
   under the final name.
3. ``fsync(directory)`` AFTER the rename. The rename makes the new name visible
   atomically; it does not make the directory entry durable. Until the
   directory is synced, a power loss can roll the entry back.

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

__all__ = [
    "CrossDeviceError",
    "fsync_file",
    "fsync_dir",
    "publish",
    "require_same_volume",
]


class CrossDeviceError(OSError):
    """Source and destination are on different volumes; an atomic rename is impossible."""


def fsync_file(f: IO[bytes]) -> None:
    """Flush Python buffers and force the file's data to stable storage."""
    f.flush()
    os.fsync(f.fileno())


def fsync_dir(path: str) -> str:
    """fsync a directory so that entries created/renamed in it become durable.

    Returns ``"ok"`` or ``"unsupported:<reason>"`` (Windows). Never silently
    skips: the caller receives the status and can surface it.
    """
    if sys.platform == "win32":
        return "unsupported:windows-has-no-directory-fsync"
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return "ok"


def _device(path: str) -> int:
    return os.stat(path).st_dev


def require_same_volume(a: str, b: str) -> None:
    """Raise :class:`CrossDeviceError` unless both existing paths share a device."""
    da, db = _device(a), _device(b)
    if da != db:
        raise CrossDeviceError(
            errno.EXDEV,
            "staging and destination are on different volumes (st_dev %r != %r); "
            "rename would not be atomic" % (da, db),
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
    """Atomically move ``src`` to a NEW name ``dst`` on the same volume.

    Refuses to overwrite an existing ``dst``. Returns a short description of
    the mechanism used. The caller is responsible for :func:`fsync_dir`.
    """
    if sys.platform == "win32":
        return _publish_windows(src, dst)
    if os.path.lexists(dst):
        # Names are random UUIDs, so this check-then-rename window cannot be hit
        # by honest use; it exists to fail loudly on a logic error.
        raise FileExistsError(errno.EEXIST, "destination exists", dst)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossDeviceError(errno.EXDEV, "cross-device rename refused", dst) from e
        raise
    return "posix:rename"
