"""Linux R8: private staging + atomic no-replace directory publication.

No power-cut guarantee, no Windows/macOS fallback, no cryptographic signature.
The restore is invisible at its FINAL path until it has been reverified.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import durable
from .offline_backup import BackupRejected, _durability, _inside, restore_offline

RENAME_NOREPLACE = 1
AT_FDCWD = -100


def _publish_new_directory(src: Path, dst: Path) -> None:
    if sys.platform != "linux":
        raise BackupRejected("atomic no-replace restore requires verified Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise BackupRejected("libc renameat2 unavailable; refusing unsafe rename fallback")
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    if fn(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE) != 0:
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, "refusing to overwrite existing restore destination", str(dst))
        if err in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise BackupRejected("filesystem lacks atomic RENAME_NOREPLACE; no unsafe fallback")
        raise OSError(err, os.strerror(err), str(dst))


def restore_atomic(backup: str | os.PathLike, new_root: str | os.PathLike, *, confirmed_quiescent: bool = False) -> dict:
    """Fully restore to hidden private sibling; publish complete dir in one step.

    Parent must be trusted (no untrusted rename/delete) and same filesystem.
    An unexpected child-process crash may leave a hidden, mode-0700 workspace,
    but never exposes an incomplete FINAL directory through this API.
    """
    if confirmed_quiescent is not True:
        raise BackupRejected("explicit quiescence confirmation required")
    destination = Path(new_root).absolute()
    source = Path(backup).absolute()
    if _inside(destination, source):
        raise BackupRejected("restore target cannot be inside backup")
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "restore destination already exists", str(destination))
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BackupRejected("restore destination parent must be an existing ordinary directory")
    if sys.platform != "linux":
        raise BackupRejected("Linux-only atomic restore; no unverified fallback")
    workspace = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=parent))
    stage = workspace / "payload"
    try:
        report = restore_offline(source, stage, confirmed_quiescent=True)
        # The private workspace's access mode is 0700, including before the
        # restore creates any child paths. It stays private if interrupted.
        syncs = [report["directory_fsync"], durable.fsync_dir(str(stage)), durable.fsync_dir(str(workspace))]
        _publish_new_directory(stage, destination)
        syncs.append(durable.fsync_dir(str(parent)))
        return {
            **report,
            "verdict": "atomic_restore_published_linux",
            "publication": "renameat2_RENAME_NOREPLACE",
            **_durability(syncs, [report["file_fsync"]]),
        }
    finally:
        if workspace.exists():
            # Never delete the published destination; leftover workspace is
            # private and not a startable path of the configured deployment.
            shutil.rmtree(workspace)
