"""SF-R41-01: durability_prerequisites_ok needs a LOCAL filesystem, not only "ok" syncs.

Reproduced on 70d9009 (Linux, /dev/shm = tmpfs, library pinned fixed): a store
on tmpfs ingested with file/objects/staging syncs "ok" and reported
``durability_prerequisites_ok = True``. tmpfs answers fsync with success and
loses everything on a power cut. Same for overlayfs mounted ``volatile`` (it
drops every sync) or with its upper directory on tmpfs, and a network
filesystem is no local prerequisite (SQLite WAL needs one host).

Contract tested here:

* :func:`safeforge.mounts.parse_mountinfo_filesystems` exposes fstype and both
  option sets of the visible mounts (same strict parser, covered mounts dropped);
* :func:`safeforge.filesystems.classify_linux` -- longest covering mount,
  volatile / network lists, overlay resolved through ``upperdir`` (volatile
  option, upper on tmpfs, upper invisible = container root -> unknown);
* macOS ``statfs`` fields (``MNT_LOCAL``) and Windows drive types, pure;
* the store: ``filesystem_class`` on the ACK, the flag False for anything but
  ``local``; the real /dev/shm regression and an ext4 positive control;
* with ``SAFEFORGE_MOUNT_MATRIX`` (CI job ``mounts``): real kernel mounts --
  bind, tmpfs, overlay on disk, overlay on tmpfs, overlay volatile -- and the
  R15 walk stop at a same-device bind mount proven on a live kernel.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from safeforge import ObjectStore, filesystems, mounts
from safeforge.filesystems import aggregate, classify_darwin, classify_linux, classify_windows

LINUX = sys.platform.startswith("linux")

# proc_pid_mountinfo(5) lines; ids/devices realistic, paths invented.
TABLE = "\n".join(
    [
        "22 1 252:0 / / rw,relatime shared:1 - ext4 /dev/vda1 rw,errors=remount-ro",
        "30 22 0:26 / /dev/shm rw,nosuid,nodev shared:4 - tmpfs tmpfs rw,size=4g",
        "31 22 0:27 / /srv/ram rw,relatime shared:5 - ramfs ramfs rw",
        "32 22 0:50 / /srv/nfs rw,relatime shared:6 - nfs4 fs.example:/export rw,vers=4.2,hard",
        "33 22 0:51 / /srv/smb rw,relatime shared:7 - cifs //host/share rw,vers=3.1.1",
        "34 22 252:16 / /data rw,noatime shared:8 - xfs /dev/vdb rw,attr2",
        "35 34 0:60 / /data/lazy rw,nosuid shared:9 - fuse.lazyfs lazyfs rw,user_id=0",
        "40 22 0:70 / /ovl/disk rw,relatime - overlay overlay rw,lowerdir=/l1,upperdir=/data/up,workdir=/data/wk",
        "41 22 0:71 / /ovl/ram rw,relatime - overlay overlay rw,lowerdir=/l2,upperdir=/dev/shm/up,workdir=/dev/shm/wk",
        "42 22 0:72 / /ovl/vol rw,relatime - overlay overlay rw,lowerdir=/l3,upperdir=/data/up3,workdir=/data/wk3,volatile",
        "43 22 0:73 / /ovl/fsv rw,relatime - overlay overlay rw,lowerdir=/l4,upperdir=/data/up4,workdir=/data/wk4,fsync=volatile",
        "44 22 0:74 / /ovl/ro rw,relatime - overlay overlay ro,lowerdir=/l5:/l6",
        "45 22 0:75 / /ovl/hidden rw,relatime - overlay overlay rw,lowerdir=/l7,upperdir=/var/lib/docker/x/diff,workdir=/w",
        "46 22 0:76 / /ovl/nested rw,relatime - overlay overlay rw,lowerdir=/l8,upperdir=/ovl/ram/up2,workdir=/ovl/ram/wk2",
        "47 22 0:77 / /ovl/self rw,relatime - overlay overlay rw,lowerdir=/l9,upperdir=/ovl/self/u,workdir=/ovl/self/w",
        "48 22 0:78 / /mnt/with\\040space rw,relatime - tmpfs tmpfs rw",
    ]
)
EXISTING = {"/data/up", "/dev/shm/up", "/data/up3", "/data/up4", "/ovl/ram/up2", "/ovl/self/u"}


def _cls(path: str, exists=EXISTING.__contains__) -> dict:
    return classify_linux(path, mounts.parse_mountinfo_filesystems(TABLE), exists=exists)


# -- parser ---------------------------------------------------------------------


def test_parser_exposes_fstype_and_both_option_sets():
    table = mounts.parse_mountinfo_filesystems(TABLE)
    fstype, mount_opts, super_opts = table["/ovl/vol"]
    assert fstype == "overlay" and "relatime" in mount_opts and "volatile" in super_opts
    assert table["/mnt/with space"][0] == "tmpfs"  # \\040 decoded like the mount point
    # the device map is unchanged by R41
    assert mounts.parse_mountinfo(TABLE)["/data"] == (252, 16)


def test_parser_follows_a_mount_stacked_on_a_mount_point():
    # Mounting over /x makes the existing /x mount the new one's parent (61 -> 60).
    text = TABLE + "\n60 22 252:0 / /x rw - ext4 /dev/vda1 rw\n61 60 0:90 / /x rw - tmpfs tmpfs rw"
    assert mounts.parse_mountinfo_filesystems(text)["/x"][0] == "tmpfs"
    assert classify_linux("/x/a", mounts.parse_mountinfo_filesystems(text))["class"] == "volatile"


def test_two_siblings_on_one_point_are_ambiguous_not_the_parent_filesystem():
    # Siblings cover each other (R38 rule): both drop out. Falling back to "/" (ext4)
    # would call a possible tmpfs "local"; the class is unknown instead.
    text = TABLE + "\n60 22 252:0 / /x rw - ext4 /dev/vda1 rw\n61 22 0:90 / /x rw - tmpfs tmpfs rw"
    assert "/x" not in mounts.parse_mountinfo(text)  # walk semantics unchanged (no stop proof)
    info = classify_linux("/x/a", mounts.parse_mountinfo_filesystems(text))
    assert (info["class"], info["source"]) == ("unknown", "mountinfo-ambiguous")


@pytest.mark.parametrize("bad", ["", "22 1 252:0 / / rw - \n", "22 1 252:0 / / rw shared:1 - ext4"])
def test_parser_rejects_a_line_without_fstype_or_source(bad):
    assert mounts.parse_mountinfo_filesystems(bad) is None


# -- Linux classification -----------------------------------------------------------


@pytest.mark.parametrize(
    "path, fstype, cls",
    [
        ("/home/u/store", "ext4", "local"),
        ("/", "ext4", "local"),
        ("/dev/shm/store", "tmpfs", "volatile"),
        ("/dev/shm", "tmpfs", "volatile"),
        ("/dev/shmX/store", "ext4", "local"),  # prefix of a name is not a covering mount
        ("/srv/ram/a", "ramfs", "volatile"),
        ("/srv/nfs/a", "nfs4", "network"),
        ("/srv/smb/a", "cifs", "network"),
        ("/data/a", "xfs", "local"),
        ("/data/lazy/a", "fuse.lazyfs", "local"),  # longest covering mount wins
        ("/mnt/with space/a", "tmpfs", "volatile"),
    ],
)
def test_linux_types(path, fstype, cls):
    info = _cls(path)
    assert (info["fstype"], info["class"], info["source"]) == (fstype, cls, "mountinfo")


@pytest.mark.parametrize(
    "path, cls, detail",
    [
        ("/ovl/disk/a", "local", ("upper_fstype", "xfs")),
        ("/ovl/ram/a", "volatile", ("upper_fstype", "tmpfs")),
        ("/ovl/vol/a", "volatile", ("overlay", "volatile")),
        ("/ovl/fsv/a", "volatile", ("overlay", "volatile")),
        ("/ovl/ro/a", "unknown", ("overlay", "no-upperdir")),
        ("/ovl/hidden/a", "unknown", ("overlay", "upperdir-not-visible")),  # container root
        ("/ovl/nested/a", "volatile", ("upper_fstype", "tmpfs")),  # overlay on overlay on tmpfs
        ("/ovl/self/a", "unknown", ("overlay", "upperdir-not-visible")),  # upper inside itself: no loop
    ],
)
def test_linux_overlay_follows_its_upper_directory(path, cls, detail):
    info = _cls(path)
    assert (info["fstype"], info["class"]) == ("overlay", cls)
    assert info[detail[0]] == detail[1]


def test_linux_without_a_mount_table_is_unknown():
    assert classify_linux("/x", None)["class"] == "unknown"
    assert filesystems.filesystems(["/x"], platform="linux", read_mountinfo=lambda: None)["/x"]["class"] == "unknown"
    malformed = filesystems.filesystems(["/x"], platform="linux", read_mountinfo=lambda: "garbage")
    assert malformed["/x"]["class"] == "unknown"


def test_unknown_platform_is_unknown():
    assert filesystems.filesystems(["/x"], platform="freebsd14")["/x"] == {
        "fstype": "",
        "class": "unknown",
        "source": "no-source",
    }


# -- macOS / Windows (pure) ------------------------------------------------------------


DATA = "/System/Volumes/Data/private/var/x"


@pytest.mark.parametrize(
    "path, fstype, on, flags, cls",
    [
        (DATA, "apfs", "/System/Volumes/Data", filesystems.MNT_LOCAL, "local"),
        ("/private/x", "apfs", "/", filesystems.MNT_LOCAL, "local"),
        ("/Volumes/share/x", "smbfs", "/Volumes/share", 0, "network"),
        ("/Volumes/nfs/x", "nfs", "/Volumes/nfs", 0, "network"),
        (DATA, "apfs", "/System/Volumes/Data", 0, "network"),  # MNT_LOCAL decides, not the name
        (DATA, "apfs", "/Volumes/Other", filesystems.MNT_LOCAL, "unknown"),  # misread struct: path not under it
        (DATA, "", "/System/Volumes/Data", filesystems.MNT_LOCAL, "unknown"),
    ],
)
def test_darwin_statfs_fields(path, fstype, on, flags, cls):
    assert classify_darwin(path, fstype, on, flags)["class"] == cls


@pytest.mark.parametrize(
    "fstype, drive, cls",
    [("NTFS", 3, "local"), ("ReFS", 3, "local"), ("NTFS", 4, "network"), ("FAT32", 6, "volatile"), ("", 3, "unknown")],
)
def test_windows_drive_types(fstype, drive, cls):
    assert classify_windows(fstype, drive)["class"] == cls


@pytest.mark.parametrize(
    "classes, expected",
    [
        (["local", "local"], "local"),
        (["local", "volatile"], "volatile"),
        (["network", "volatile"], "volatile"),
        (["local", "network"], "network"),
        (["local", "unknown"], "unknown"),
        (["local", "bogus"], "unknown"),
        ([], "unknown"),
    ],
)
def test_aggregate_is_local_only_if_all_are(classes, expected):
    assert aggregate({"class": c} for c in classes) == expected


def test_real_platform_reports_a_source():
    info = filesystems.filesystems([os.getcwd()])[os.getcwd()]
    assert set(info) >= {"fstype", "class", "source"} and info["class"] in filesystems.CLASSES
    if sys.platform in ("linux", "darwin", "win32"):
        assert info["fstype"], info  # CI runners: ext4 / apfs / NTFS


# -- the store ---------------------------------------------------------------------------


def _ingest(root: str):
    with ObjectStore(root) as s:
        r = s.ingest(io.BytesIO(b"filesystem class"), max_bytes=1 << 20)
        return s, r


@pytest.mark.parametrize("cls", ["volatile", "network", "unknown"])
def test_flag_needs_a_local_filesystem(tmp_path, monkeypatch, cls):
    real = filesystems.filesystems

    def forced(paths, **kw):
        return {p: {**info, "class": cls} for p, info in real(paths, **kw).items()}

    monkeypatch.setattr(filesystems, "filesystems", forced)
    s, r = _ingest(str(tmp_path / "root"))
    assert (s.filesystem_class, r.filesystem_class) == (cls, cls)
    assert r.durability_prerequisites_ok is False
    assert len(s.layout_filesystems) >= 3  # root, objects/, staging/ (+ db dir when distinct)


def test_flag_true_on_local_positive_control(tmp_path):
    s, r = _ingest(str(tmp_path / "root"))
    if s.filesystem_class != "local" or not LINUX:
        pytest.skip("positive control needs a local filesystem with real directory fsync (Linux CI)")
    assert r.filesystem_class == "local" and r.durability_prerequisites_ok is True


def test_deduplicated_ack_has_no_filesystem_class(tmp_path):
    root = str(tmp_path / "root")
    with ObjectStore(root) as s:
        s.ingest(io.BytesIO(b"dup"), max_bytes=10, idempotency_key="k")
        again = s.ingest(io.BytesIO(b"dup"), max_bytes=10, idempotency_key="k")
    assert again.deduplicated and again.filesystem_class == ""


def _is_tmpfs(path: str) -> bool:
    return os.path.isdir(path) and filesystems.filesystems([path])[path]["fstype"] == "tmpfs"


@pytest.mark.skipif(not (LINUX and _is_tmpfs("/dev/shm")), reason="needs Linux with /dev/shm on tmpfs")
def test_real_tmpfs_store_never_claims_durability():
    """The reproduction: every sync says "ok" on tmpfs, the flag must still be False."""
    import tempfile

    base = tempfile.mkdtemp(dir="/dev/shm")
    try:
        s, r = _ingest(os.path.join(base, "root"))
        assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")
        assert r.filesystem_class == "volatile"
        assert r.durability_prerequisites_ok is False
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


# -- real kernel mounts (CI job "mounts") ---------------------------------------------

MATRIX = os.environ.get("SAFEFORGE_MOUNT_MATRIX")
requires_matrix = pytest.mark.skipif(not MATRIX, reason="set SAFEFORGE_MOUNT_MATRIX (CI job mounts) to run")


@requires_matrix
@pytest.mark.parametrize(
    "sub, fstype, cls, flag",
    [
        ("disk/outer/bind", "ext4", "local", True),
        ("tmpfs", "tmpfs", "volatile", False),
        ("ovl-disk/merged", "overlay", "local", True),
        ("ovl-tmpfs/merged", "overlay", "volatile", False),
        ("ovl-volatile/merged", "overlay", "volatile", False),
    ],
)
def test_required_real_mount_classes(sub, fstype, cls, flag):
    base = os.path.join(MATRIX, sub)
    assert os.path.ismount(base) or sub.startswith("disk/"), base  # bind mounts share st_dev; ismount cannot see them
    s, r = _ingest(os.path.join(base, "store", "root"))
    info = s.layout_filesystems
    assert {v["fstype"] for v in info.values()} == {fstype}, info
    assert (r.filesystem_class, r.durability_prerequisites_ok) == (cls, flag), (info, r)


@requires_matrix
def test_required_real_bind_mount_stops_the_walk():
    """R15 on a live kernel: a same-device bind mount is invisible to st_dev, mountinfo proves it."""
    outer = os.path.realpath(os.path.join(MATRIX, "disk", "outer"))
    point = os.path.join(outer, "bind")
    assert os.stat(point).st_dev == os.stat(outer).st_dev  # the hard case: same filesystem
    s, _r = _ingest(os.path.join(point, "a", "root"))
    assert s.layout_mount_basis in ("mountinfo", "statx-mnt-id", "statx-mnt-id-unique")
    assert point in s.layout_sync  # parent of a/ is synced ...
    assert outer not in s.layout_sync  # ... the walk stops at the mount point, outer/ is not ours
