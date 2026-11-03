"""SF-R42-01: "local" is an allowlist, and flush-disabling mount options withhold it.

Finding (007acc2 cross-family R42, GPT-6; re-read on 066cbdf): mount and super
options were only consulted for overlayfs ``volatile``. Every other type that was
neither volatile nor network counted as ``local`` -- ext4 mounted ``nobarrier``,
f2fs with ``fsync_mode=nobarrier`` or ``checkpoint=disable``, FUSE (LazyFS), a
type never seen before. Invariant: ``durability_prerequisites_ok`` is never True
when the mount options switch off flush/barrier semantics, and never True on a
type that is not known to flush.

Tables here are synthetic proc_pid_mountinfo(5) lines (the classifier is pure);
the real loop-mounted ext4 with and without ``nobarrier`` is in the CI job
``mounts`` (tests/test_r41_filesystem_class.py, SF-R42-03).
"""

from __future__ import annotations

import io

import pytest

from safeforge import ObjectStore, durable, filesystems, mounts
from safeforge.filesystems import classify_darwin, classify_linux, classify_windows


def _table(fstype: str, mount_opts: str = "rw,relatime", super_opts: str = "rw", *, upper: bool = False):
    lines = [
        "22 1 252:0 / / rw,relatime - ext4 /dev/vda1 rw,errors=remount-ro",
        f"34 22 252:16 / /data {mount_opts} - {fstype} /dev/vdb {super_opts}",
    ]
    if upper:
        lines.append("40 22 0:70 / /merged rw,relatime - overlay overlay rw,lowerdir=/lo,upperdir=/data/up,workdir=/data/wk")
    return mounts.parse_mountinfo_filesystems("\n".join(lines))


# -- flush-disabling options (kernel prints them in the super options; both sets are read) --


FLUSH_OFF = [
    ("ext4", "nobarrier"),
    ("ext4", "barrier=0"),
    ("btrfs", "nobarrier"),
    ("xfs", "nobarrier"),  # accepted by kernels before 4.19
    ("f2fs", "nobarrier"),
    ("f2fs", "fsync_mode=nobarrier"),
    ("f2fs", "checkpoint=disable"),
    ("f2fs", "checkpoint=disable:10%"),
    ("f2fs", "checkpoint=disable:4096"),
]


def _barrier(point: str) -> frozenset[str]:
    return frozenset({"rw", "barrier"})


@pytest.mark.parametrize("fstype, option", FLUSH_OFF)
@pytest.mark.parametrize("where", ["super", "mount"])
def test_flush_disabling_option_is_not_local(fstype, option, where):
    opts = "rw,%s,errors=remount-ro" % option
    table = _table(fstype, super_opts=opts) if where == "super" else _table(fstype, mount_opts=opts)
    info = classify_linux("/data/store", table)
    assert info == {
        "fstype": fstype,
        "class": "unknown",
        "source": "mountinfo",
        "reason": "mountinfo-flush-disabled",
        "option": option,
    }


@pytest.mark.parametrize(
    "fstype, opts",
    [
        ("ext4", "rw,errors=remount-ro"),
        ("ext4", "rw,barrier"),
        ("ext4", "rw,barrier=1"),
        ("ext4", "rw,data=writeback,commit=60"),  # not a flush switch: fsync still flushes
        ("xfs", "rw,attr2,inode64,noquota"),
        ("btrfs", "rw,ssd,space_cache=v2,subvolid=5,subvol=/"),
        ("f2fs", "rw,fsync_mode=posix"),
        ("f2fs", "rw,fsync_mode=strict"),
        ("f2fs", "rw,checkpoint=enable"),
        ("f2fs", "rw,checkpoint_merge"),
    ],
)
def test_positive_control_allowlisted_types_with_flushes_on_stay_local(fstype, opts):
    info = classify_linux("/data/store", _table(fstype, super_opts=opts), ext4_options=_barrier)
    assert info == {"fstype": fstype, "class": "local", "source": "mountinfo"}


@pytest.mark.parametrize(
    "fstype",
    ["fuse.lazyfs", "fuse", "fuseblk", "ext3", "ext2", "vfat", "exfat", "ntfs3", "squashfs", "proc", "bcachefs", "zfs", "zzfs"],
)
def test_types_not_on_the_allowlist_are_unknown(fstype):
    info = classify_linux("/data/store", _table(fstype))
    assert info == {"fstype": fstype, "class": "unknown", "source": "mountinfo", "reason": "fs-type-not-attested"}


@pytest.mark.parametrize("fstype, cls", [("tmpfs", "volatile"), ("nfs4", "network")])
def test_worse_classes_are_not_softened_by_options(fstype, cls):
    assert classify_linux("/data/store", _table(fstype, super_opts="rw,nobarrier"))["class"] == cls


@pytest.mark.parametrize("option", ["nobarrier", "fsync_mode=nobarrier", "checkpoint=disable:70%"])
def test_overlay_inherits_the_upper_filesystems_flush_switch(option):
    fstype = "ext4" if option == "nobarrier" else "f2fs"
    table = _table(fstype, super_opts="rw," + option, upper=True)
    info = classify_linux("/merged/store", table, exists=lambda p: p == "/data/up")
    assert (info["fstype"], info.get("upper_fstype"), info["class"], info.get("reason"), info.get("option")) == (
        "overlay",
        fstype,
        "unknown",
        "mountinfo-flush-disabled",
        option,
    )


def test_overlay_upper_on_an_unattested_type_is_unknown():
    info = classify_linux("/merged/store", _table("fuse.lazyfs", upper=True), exists=lambda p: p == "/data/up")
    assert (info.get("upper_fstype"), info["class"], info.get("reason")) == ("fuse.lazyfs", "unknown", "fs-type-not-attested")


def test_overlay_positive_control_upper_on_ext4_stays_local():
    info = classify_linux("/merged/store", _table("ext4", upper=True), exists=lambda p: p == "/data/up", ext4_options=_barrier)
    assert (info["upper_fstype"], info["class"]) == ("ext4", "local")
    assert "reason" not in info


@pytest.mark.parametrize("fstype, cls", [("apfs", "local"), ("hfs", "unknown"), ("msdos", "unknown"), ("exfat", "unknown")])
def test_darwin_allowlist(fstype, cls):
    info = classify_darwin(fstype, filesystems.MNT_LOCAL, True)
    assert info["class"] == cls
    assert ("reason" in info) is (cls == "unknown")


# -- the store: the flag follows the option-aware class -----------------------------------


@pytest.fixture
def linux_view(monkeypatch):
    """Every layout path is classified as /data/store under the given table; syncs faked "ok"."""
    monkeypatch.setattr(durable, "fsync_dir", lambda d: "ok")
    monkeypatch.setattr(durable, "fsync_file", lambda fh: (fh.flush(), "ok")[1])
    state = {}

    def view(paths, **kw):
        return {p: classify_linux("/data/store", state["table"], ext4_options=_barrier) for p in paths}

    monkeypatch.setattr(filesystems, "filesystems", view)
    return state


@pytest.mark.parametrize(
    "fstype, super_opts, flag",
    [
        ("ext4", "rw,errors=remount-ro", True),  # positive control: same harness, flushes on
        ("ext4", "rw,nobarrier", False),
        ("ext4", "rw,barrier=0", False),
        ("f2fs", "rw,fsync_mode=nobarrier", False),
        ("f2fs", "rw,checkpoint=disable", False),
        ("fuse.lazyfs", "rw", False),
    ],
)
def test_store_flag_follows_the_mount_options(tmp_path, linux_view, fstype, super_opts, flag):
    linux_view["table"] = _table(fstype, super_opts=super_opts)
    with ObjectStore(str(tmp_path / "root")) as s:
        r = s.ingest(io.BytesIO(b"r42"), max_bytes=100)
    assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")
    assert r.durability_prerequisites_ok is flag, (s.layout_filesystems, r)
    assert r.filesystem_class == ("local" if flag else "unknown")


# -- Windows (SF-R42-02, SF-R44-01/02): only NTFS on a fixed drive --------------------------


@pytest.mark.parametrize(
    "fstype, drive, flag",
    [
        ("NTFS", filesystems.DRIVE_FIXED, True),  # positive control, same harness
        ("NTFS", 2, False),  # removable
        ("NTFS", 0, False),  # unknown drive type
        ("ReFS", filesystems.DRIVE_FIXED, False),
        ("exFAT", filesystems.DRIVE_FIXED, False),
    ],
)
def test_store_flag_follows_the_windows_volume(tmp_path, monkeypatch, fstype, drive, flag):
    monkeypatch.setattr(durable, "fsync_dir", lambda d: "ok")
    monkeypatch.setattr(durable, "fsync_file", lambda fh: (fh.flush(), "ok")[1])
    monkeypatch.setattr(filesystems, "filesystems", lambda paths, **kw: {p: classify_windows(fstype, drive) for p in paths})
    with ObjectStore(str(tmp_path / "root")) as s:
        r = s.ingest(io.BytesIO(b"r42 windows"), max_bytes=100)
    assert r.durability_prerequisites_ok is flag, (s.layout_filesystems, r)
