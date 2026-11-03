"""SF-R45: ``local`` must not rest on a flush switch the observed source cannot show.

Findings (cross-family R28/R31/R36 on 3cfcacd, source inspection; PR-R29-SF-01):
ZFS ``sync=disabled`` is a dataset property, absent from mountinfo, and makes
fsync return before anything is on stable storage (OpenZFS zfsprops(7)). So a
zfs entry in mountinfo says nothing about the flush: SF-R45-01 takes zfs off the
allowlist, whatever options a table claims.

Variant search against the same invariant found a second switch, measured on a
live kernel (6.6.87.2, loop-mounted ext4, 2026-11-03): ``tune2fs -o nobarrier``
and ``tune2fs -E mount_opts=nobarrier`` set the superblock default, and
_ext4_show_options() prints only what differs from that default. mountinfo of
such a mount is ``rw,relatime - ext4 /dev/loop1 rw``, identical in everything but
the numbers to a mount with barriers on, while /proc/fs/ext4/loop1/options says
``nobarrier``. SF-R45-02: ext4 is local only with ``barrier`` attested there.
"""

from __future__ import annotations

import io

import pytest

from safeforge import ObjectStore, durable, filesystems, mounts
from safeforge.filesystems import classify_darwin, classify_linux, classify_windows

# Verbatim from the measurement above: same options, different superblock default.
MEASURED_MOUNTINFO = (
    "80 65 8:48 / / rw,relatime - ext4 /dev/sdd rw,discard,errors=remount-ro,data=ordered\n"
    "130 80 7:0 / /m/barrier rw,relatime - ext4 /dev/loop0 rw\n"
    "131 80 7:1 / /m/sb-nobarrier rw,relatime - ext4 /dev/loop1 rw\n"
    "133 80 7:3 / /m/nobarrier rw,relatime - ext4 /dev/loop3 rw,nobarrier\n"
)
ATTESTED = frozenset({"rw", "bsddf", "barrier", "delalloc", "commit=5"})
SB_OFF = frozenset({"rw", "bsddf", "nobarrier", "delalloc", "commit=5"})


def _table(fstype: str, super_opts: str = "rw", *, upper: bool = False):
    lines = [
        "22 1 252:0 / / rw,relatime - ext4 /dev/vda1 rw",
        f"34 22 252:16 / /data rw,relatime - {fstype} /dev/vdb {super_opts}",
    ]
    if upper:
        lines.append("40 22 0:70 / /merged rw,relatime - overlay overlay rw,lowerdir=/lo,upperdir=/data/up,workdir=/data/wk")
    return mounts.parse_mountinfo_filesystems("\n".join(lines))


# -- SF-R45-01: zfs -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "super_opts",
    [
        "rw,xattr,noacl",  # what the kernel really prints, for sync=disabled and sync=standard alike
        "rw,sync=standard",  # never printed by the kernel: a table that claims it attests nothing
        "rw,sync=always",
        "rw,sync=disabled",
    ],
)
def test_zfs_is_never_local_from_mountinfo(super_opts):
    assert classify_linux("/data/store", _table("zfs", super_opts)) == {
        "fstype": "zfs",
        "class": "unknown",
        "source": "mountinfo",
        "reason": "fs-type-not-attested",
    }


def test_overlay_upper_on_zfs_is_not_local():
    info = classify_linux("/merged/store", _table("zfs", "rw,xattr,noacl", upper=True), exists=lambda p: p == "/data/up")
    assert (info["fstype"], info.get("upper_fstype"), info["class"], info.get("reason")) == (
        "overlay",
        "zfs",
        "unknown",
        "fs-type-not-attested",
    )


def test_zfs_on_macos_and_windows_is_not_local():
    assert classify_darwin("zfs", filesystems.MNT_LOCAL, True)["class"] == "unknown"
    assert classify_windows("ZFS", filesystems.DRIVE_FIXED)["class"] == "unknown"


@pytest.mark.parametrize("fstype, flag", [("xfs", True), ("zfs", False)])  # xfs: positive control, same harness
def test_store_flag_on_zfs_is_false_and_nothing_is_refused(tmp_path, monkeypatch, fstype, flag):
    monkeypatch.setattr(durable, "fsync_dir", lambda d: "ok")
    monkeypatch.setattr(durable, "fsync_file", lambda fh: (fh.flush(), "ok")[1])
    table = _table(fstype)
    monkeypatch.setattr(filesystems, "filesystems", lambda paths, **kw: {p: classify_linux("/data/store", table) for p in paths})
    with ObjectStore(str(tmp_path / "root")) as s:
        r = s.ingest(io.BytesIO(b"r45"), max_bytes=100)
    assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")
    assert r.durability_prerequisites_ok is flag, (s.layout_filesystems, r)
    assert r.filesystem_class == ("local" if flag else "unknown")


# -- f2fs: fs/f2fs/super.c runs "recover fsynced data" at mount only without this option ---------


@pytest.mark.parametrize("where", ["super", "mount"])
def test_f2fs_without_roll_forward_is_not_local(where):
    opts = "rw,disable_roll_forward"
    line = f"34 22 252:16 / /data {opts if where == 'mount' else 'rw'} - f2fs /dev/vdb {opts if where == 'super' else 'rw'}"
    table = mounts.parse_mountinfo_filesystems("22 1 252:0 / / rw - ext4 /dev/vda1 rw\n" + line)
    info = classify_linux("/data/store", table)
    assert (info["class"], info.get("reason"), info.get("option")) == (
        "unknown",
        "mountinfo-flush-disabled",
        "disable_roll_forward",
    )


# -- SF-R45-02: ext4 superblock default ------------------------------------------------------


def _ext4(in_force):
    return classify_linux("/data/store", _table("ext4"), ext4_options=lambda point: in_force)


def test_ext4_with_barrier_attested_stays_local():
    assert _ext4(ATTESTED) == {"fstype": "ext4", "class": "local", "source": "mountinfo"}


def test_ext4_superblock_nobarrier_is_not_local():
    assert _ext4(SB_OFF) == {
        "fstype": "ext4",
        "class": "unknown",
        "source": "proc-fs-ext4",
        "reason": "superblock-flush-disabled",
        "option": "nobarrier",
    }


@pytest.mark.parametrize("in_force", [None, frozenset(), frozenset({"rw", "commit=5"})])
def test_ext4_without_an_attested_barrier_line_is_not_local(in_force):
    assert _ext4(in_force) == {
        "fstype": "ext4",
        "class": "unknown",
        "source": "proc-fs-ext4",
        "reason": "ext4-options-unavailable",
    }


def test_overlay_upper_on_ext4_with_superblock_nobarrier_is_not_local():
    table = _table("ext4", upper=True)
    seen = []

    def in_force(point):
        seen.append(point)
        return SB_OFF

    info = classify_linux("/merged/store", table, exists=lambda p: p == "/data/up", ext4_options=in_force)
    assert (info["fstype"], info["upper_fstype"], info["class"], info["reason"]) == (
        "overlay",
        "ext4",
        "unknown",
        "superblock-flush-disabled",
    )
    assert seen == ["/data"]  # the upper directory's mount, not the overlay's


def test_visible_nobarrier_keeps_its_reason_and_needs_no_second_source():
    def must_not_read(point):
        raise AssertionError(point)

    info = classify_linux("/data/store", _table("ext4", "rw,nobarrier"), ext4_options=must_not_read)
    assert (info["reason"], info["option"]) == ("mountinfo-flush-disabled", "nobarrier")


# -- SF-R46-01: a direct call without the source attests nothing -------------------------------


def test_ext4_without_a_source_is_not_local():
    assert classify_linux("/data/store", _table("ext4")) == {
        "fstype": "ext4",
        "class": "unknown",
        "source": "proc-fs-ext4",
        "reason": "ext4-options-unavailable",
    }


def test_ext4_upper_of_an_overlay_without_a_source_is_not_local():
    info = classify_linux("/merged/store", _table("ext4", upper=True), exists=lambda p: p == "/data/up")
    assert (info["upper_fstype"], info["class"], info.get("reason")) == ("ext4", "unknown", "ext4-options-unavailable")


def test_ext4_with_the_source_attesting_a_barrier_is_local_on_the_same_table():
    """Positive control for the two tests above: only the source differs."""
    assert classify_linux("/data/store", _table("ext4"), ext4_options=lambda point: ATTESTED)["class"] == "local"


@pytest.mark.parametrize("fstype", ["xfs", "btrfs", "f2fs"])
def test_no_source_does_not_touch_other_allowlisted_types(fstype):
    """Negative control: the guard must not withhold local from every type."""
    assert classify_linux("/data/store", _table(fstype))["class"] == "local"


@pytest.mark.parametrize("fstype", ["xfs", "btrfs", "f2fs"])
def test_other_allowlisted_types_do_not_depend_on_the_ext4_source(fstype):
    info = classify_linux("/data/store", _table(fstype), ext4_options=lambda point: None)
    assert (info["class"], "reason" in info) == ("local", False)


# -- production wiring: filesystems() must hand the classifier the superblock source --------------


@pytest.fixture
def linux_paths(monkeypatch):
    monkeypatch.setattr(filesystems.os.path, "realpath", lambda p: p)  # Linux paths on every host


def _wired(path, by_device):
    seen = []

    def read(device):
        seen.append(device)
        return by_device.get(device)

    info = filesystems.filesystems([path], platform="linux", read_mountinfo=lambda: MEASURED_MOUNTINFO, read_ext4_options=read)[
        path
    ]
    return info, seen


def test_measured_mountinfo_cannot_tell_the_two_mounts_apart():
    """The fact behind SF-R45-02: the table alone cannot separate them, so it attests neither."""
    table = mounts.parse_mountinfo_filesystems(MEASURED_MOUNTINFO)
    assert table["/m/barrier"] == table["/m/sb-nobarrier"]
    assert classify_linux("/m/barrier/s", table) == classify_linux("/m/sb-nobarrier/s", table)
    assert classify_linux("/m/barrier/s", table)["class"] == "unknown"


def test_filesystems_wires_the_device_of_the_mount_to_the_source(linux_paths):
    by_device = {(7, 0): ATTESTED, (7, 1): SB_OFF}
    good, seen_good = _wired("/m/barrier/store", by_device)
    bad, seen_bad = _wired("/m/sb-nobarrier/store", by_device)
    assert (good["class"], seen_good) == ("local", [(7, 0)])
    assert (bad["class"], bad["reason"], seen_bad) == ("unknown", "superblock-flush-disabled", [(7, 1)])


def test_filesystems_fails_closed_when_the_source_has_no_answer(linux_paths):
    info, _seen = _wired("/m/barrier/store", {})
    assert (info["class"], info["reason"]) == ("unknown", "ext4-options-unavailable")


def test_filesystems_default_source_is_the_real_reader(linux_paths, monkeypatch):
    monkeypatch.setattr(mounts, "_read_ext4_options", lambda device: SB_OFF if device == (7, 1) else ATTESTED)
    infos = filesystems.filesystems(
        ["/m/barrier/s", "/m/sb-nobarrier/s"], platform="linux", read_mountinfo=lambda: MEASURED_MOUNTINFO
    )
    assert infos["/m/barrier/s"]["class"] == "local"
    assert infos["/m/sb-nobarrier/s"]["reason"] == "superblock-flush-disabled"


# -- the reader itself ------------------------------------------------------------------------


@pytest.fixture
def proc_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(mounts, "_PROC_FS_EXT4", str(tmp_path))
    monkeypatch.setattr(mounts.os, "readlink", lambda p: "../../devices/virtual/block/loop1", raising=False)
    (tmp_path / "loop1").mkdir()
    return tmp_path / "loop1" / "options"


def test_read_ext4_options_reads_the_lines_of_the_named_device(proc_tree):
    proc_tree.write_text("rw\nbsddf\nnobarrier\ncommit=5\n", encoding="ascii")
    assert mounts._read_ext4_options((7, 1)) == {"rw", "bsddf", "nobarrier", "commit=5"}


def test_read_ext4_options_asks_sysfs_for_the_device_number(proc_tree, monkeypatch):
    proc_tree.write_text("barrier\n", encoding="ascii")
    asked = []
    monkeypatch.setattr(mounts.os, "readlink", lambda p: (asked.append(p), "../x/loop1")[1], raising=False)
    mounts._read_ext4_options((7, 1))
    assert asked == ["/sys/dev/block/7:1"]


def test_read_ext4_options_missing_file_is_none(proc_tree):
    assert mounts._read_ext4_options((7, 1)) is None


def test_read_ext4_options_undecodable_file_is_none(proc_tree):
    proc_tree.write_bytes(b"barrier\n\xff\xfe\n")
    assert mounts._read_ext4_options((7, 1)) is None


def test_read_ext4_options_no_sysfs_entry_is_none(proc_tree, monkeypatch):
    def gone(p):
        raise OSError(p)

    monkeypatch.setattr(mounts.os, "readlink", gone, raising=False)
    assert mounts._read_ext4_options((7, 1)) is None


def test_parse_mountinfo_devices_covers_visible_points_and_rejects_garbage():
    assert mounts.parse_mountinfo_devices(MEASURED_MOUNTINFO)["/m/sb-nobarrier"] == (7, 1)
    assert mounts.parse_mountinfo_devices("garbage") is None
