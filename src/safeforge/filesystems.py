"""Which kind of filesystem holds a path: local, volatile, network or unknown? (SF-R41-01)

Finding (2026-10-30, reproduced on 70d9009): a store on tmpfs reported
``durability_prerequisites_ok = True``. Every fsync returned "ok" -- on tmpfs
fsync is a no-op that succeeds -- yet tmpfs lives in RAM and loses everything
on a power cut. The flag observed the syncs but never the filesystem they ran
on. Realistic, not exotic: Debian 13 moved /tmp to tmpfs (trixie release
notes), containers run on overlayfs, and overlayfs has a ``volatile`` mode
that drops every sync (kernel overlayfs documentation, fsync=volatile).

Classes, decided per path:

``volatile``
    tmpfs, ramfs; overlayfs mounted ``volatile`` / ``fsync=volatile``; overlayfs
    whose upper directory is itself volatile.
``network``
    NFS, SMB/CIFS, 9p, Ceph, GlusterFS, Lustre, AFS, sshfs, WebDAV, AFP, and on
    macOS any mount without ``MNT_LOCAL``. SQLite's WAL needs shared memory
    between all connections, so it does not work across hosts (sqlite.org/wal.html),
    and a network filesystem's fsync semantics are the server's.
``local``
    A filesystem type that was observed and is in neither list (ext4, xfs,
    btrfs, zfs, apfs, NTFS, FUSE such as LazyFS, ...).
``unknown``
    The type could not be observed: no mount table, a malformed one, an
    overlayfs whose upper directory is not visible from this mount namespace
    (e.g. a container's root), a platform without a source.

Only ``local`` counts as a durability prerequisite; ``unknown`` gives False like
any other missing evidence (R28). Nothing here refuses to open or ingest.

Sources per platform: Linux ``/proc/self/mountinfo`` (field 9 fstype, field 6
mount options, last field super options; proc_pid_mountinfo(5)), parsed by
:func:`safeforge.mounts.parse_mountinfo_filesystems`. macOS ``statfs(2)``
(``f_fstypename``, ``MNT_LOCAL``) through ctypes, accepted only if the reported
mount point is a prefix of the path. Windows ``GetVolumePathNameW`` +
``GetDriveTypeW`` (DRIVE_REMOTE, DRIVE_RAMDISK) + ``GetVolumeInformationW``.

Limits: a RAM disk formatted with a disk filesystem (macOS ``hdiutil`` ram://,
Linux brd/zram + ext4) looks local; a block device's write cache is not
visible here (README "No power-loss proof").
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable

from . import mounts

__all__ = ["CLASSES", "aggregate", "classify_linux", "filesystems"]

CLASSES = ("local", "volatile", "network", "unknown")

VOLATILE_TYPES = frozenset({"tmpfs", "ramfs"})
NETWORK_TYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "ceph",
        "glusterfs",
        "fuse.glusterfs",
        "lustre",
        "afs",
        "fuse.sshfs",
        "sshfs",
        "davfs",
        "fuse.davfs2",
        "webdav",
        "afpfs",
    }
)
_OVERLAY_VOLATILE = frozenset({"volatile", "fsync=volatile"})
_MAX_OVERLAY_DEPTH = 4


def _info(fstype: str, cls: str, source: str, **extra: str) -> dict:
    return {"fstype": fstype, "class": cls, "source": source, **extra}


def _type_class(fstype: str) -> str:
    if fstype in VOLATILE_TYPES:
        return "volatile"
    if fstype in NETWORK_TYPES:
        return "network"
    return "local"


def _mount_of(path: str, table: dict) -> str | None:
    """The visible mount point that contains ``path`` (longest covering point)."""
    candidates = {path.rstrip("/"), *mounts._slash_prefixes(path)}
    best = None
    for point in table:
        if point.rstrip("/") in candidates and (best is None or len(point) > len(best)):
            best = point
    return best


def classify_linux(path: str, table: dict | None, *, exists: Callable[[str], bool] = os.path.isdir) -> dict:
    """Class of the filesystem holding ``path`` (an absolute, resolved path) from a parsed mount table. Pure."""
    if table is None:
        return _info("", "unknown", "mountinfo-unavailable")
    seen: set[str] = set()
    current = path
    for _ in range(_MAX_OVERLAY_DEPTH):
        point = _mount_of(current, table)
        if point is None:
            return _info("", "unknown", "mountinfo-no-mount")
        fstype, mount_opts, super_opts = table[point]
        if not fstype:
            return _info("", "unknown", "mountinfo-ambiguous")
        if fstype != "overlay":
            cls = _type_class(fstype)
            if current == path:
                return _info(fstype, cls, "mountinfo")
            return _info("overlay", cls, "mountinfo", upper_fstype=fstype)
        if (mount_opts | super_opts) & _OVERLAY_VOLATILE:
            return _info("overlay", "volatile", "mountinfo", overlay="volatile")
        uppers = [o[len("upperdir=") :] for o in super_opts if o.startswith("upperdir=")]
        if len(uppers) != 1:
            return _info("overlay", "unknown", "mountinfo", overlay="no-upperdir")
        upper = uppers[0]
        if not upper.startswith("/") or point in seen or not exists(upper):
            # e.g. a container root: the host's upperdir is not in this namespace.
            return _info("overlay", "unknown", "mountinfo", overlay="upperdir-not-visible")
        seen.add(point)
        current = os.path.normpath(upper)
    return _info("overlay", "unknown", "mountinfo", overlay="too-deep")


def _darwin(path: str) -> dict:
    import ctypes

    class _Statfs(ctypes.Structure):  # struct statfs, 64-bit-inode layout (sys/mount.h)
        _fields_ = [
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        ]

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            fn = libc["statfs$INODE64"]  # x86_64 exports the 64-bit-inode variant under this name
        except AttributeError:
            fn = libc["statfs"]  # arm64: statfs is the 64-bit-inode variant
        buf = _Statfs()
        if fn(os.fsencode(path), ctypes.byref(buf)) != 0:
            return _info("", "unknown", "statfs-failed")
        fstype = buf.f_fstypename.decode("ascii")
        on = os.fsdecode(buf.f_mntonname)
    except (OSError, UnicodeDecodeError, AttributeError, ValueError):
        return _info("", "unknown", "statfs-failed")
    return classify_darwin(path, fstype, on, buf.f_flags)


MNT_LOCAL = 0x00001000


def classify_darwin(path: str, fstype: str, mounted_on: str, flags: int) -> dict:
    """Class from ``statfs`` fields. Pure. A mount point that does not contain ``path``
    means the struct was misread: unknown, not a guess."""
    if not fstype or not fstype.isascii() or not (mounted_on == "/" or _covers(mounted_on, path)):
        return _info(fstype if fstype.isascii() else "", "unknown", "statfs-inconsistent")
    if not flags & MNT_LOCAL:
        return _info(fstype, "network", "statfs")
    return _info(fstype, _type_class(fstype), "statfs")


def _covers(outer: str, inner: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


DRIVE_REMOTE = 4
DRIVE_RAMDISK = 6


def classify_windows(fstype: str, drive_type: int) -> dict:
    """Class from ``GetVolumeInformationW`` / ``GetDriveTypeW``. Pure."""
    if drive_type == DRIVE_REMOTE:
        return _info(fstype, "network", "volume-info")
    if drive_type == DRIVE_RAMDISK:
        return _info(fstype, "volatile", "volume-info")
    if not fstype:
        return _info("", "unknown", "volume-info")
    return _info(fstype, "local", "volume-info")


def _windows(path: str) -> dict:
    import ctypes
    from ctypes import wintypes

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        root = ctypes.create_unicode_buffer(1024)
        if not k32.GetVolumePathNameW(wintypes.LPCWSTR(path), root, 1024):
            return _info("", "unknown", "volume-info-failed")
        drive_type = k32.GetDriveTypeW(root)
        name = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumeInformationW(root, None, 0, None, None, None, name, 261):
            return classify_windows("", drive_type)
        return classify_windows(name.value, drive_type)
    except (OSError, AttributeError, ValueError):
        return _info("", "unknown", "volume-info-failed")


def filesystems(
    paths: Iterable[str],
    *,
    platform: str | None = None,
    read_mountinfo: Callable[[], str | None] | None = None,
) -> dict[str, dict]:
    """``{path: {"fstype", "class", "source", ...}}`` for each path (resolved with realpath).

    Linux reads the mount table once for all paths. ``platform`` and
    ``read_mountinfo`` exist for tests.
    """
    platform = sys.platform if platform is None else platform
    resolved = {p: os.path.realpath(p) for p in paths}
    if platform.startswith("linux"):
        text = (read_mountinfo or mounts._read_mountinfo)()
        table = mounts.parse_mountinfo_filesystems(text) if text is not None else None
        return {p: classify_linux(r, table) for p, r in resolved.items()}
    if platform == "darwin":
        return {p: _darwin(r) for p, r in resolved.items()}
    if platform == "win32":
        return {p: _windows(r) for p, r in resolved.items()}
    return {p: _info("", "unknown", "no-source") for p in resolved}


def aggregate(infos: Iterable[dict]) -> str:
    """One class for a set of paths: ``local`` only if every path is local; else the worst seen."""
    classes = {i.get("class") for i in infos}
    if not classes:
        return "unknown"
    for cls in ("volatile", "network", "unknown"):
        if cls in classes:
            return cls
    return "local" if classes == {"local"} else "unknown"
