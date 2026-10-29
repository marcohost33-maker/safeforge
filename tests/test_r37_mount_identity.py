"""R15 (GPT-6, 2026-10-28, PR #2 @89a0381): "die st_dev-Stopregel beweist keine
Mountidentitaet".

``store._same_mount_ancestors`` re-syncs the ancestors of the store root up to
the mount point (SF-R36-01). It used to stop where the parent's ``st_dev``
differs, which misses a bind mount of the same filesystem: the walk went on
into directories that are not the store's and refused to open when one of
them could not be synced. ``safeforge.mounts`` now stops only on proof (other
st_dev, statx mount id, /proc/self/mountinfo, GetVolumePathNameW) and keeps
walking when it has none.

Which tests discriminate where:
* parser, ``may_stop`` and probe selection are pure (sources injected): they
  run and discriminate on every OS, Windows included;
* the store-level bind-mount tests inject the probe, the fsync fake decides:
  they discriminate on every OS;
* the real-OS controls run only on the OS that provides the source (SKIP with
  the reason elsewhere). A real bind mount needs root and is NOT tested here;
  that stays part of R15's integration gate.
"""

from __future__ import annotations

import errno
import io
import os
import sys

import pytest

from safeforge import mounts
from safeforge import store as store_mod
from safeforge.mounts import MountProbe, parse_mountinfo
from safeforge.store import IngestError, IngestResult, ObjectStore

durable = store_mod.durable

# Shape of real lines (proc_pid_mountinfo(5)), incl. optional fields and an escaped space.
MOUNTINFO = (
    "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "23 22 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw\n"
    "40 22 8:1 /srv/data /mnt/with\\040space rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "41 22 8:1 /srv/data /mnt/bind rw,relatime - ext4 /dev/sda1 rw\n"
)


# ---- parser ----------------------------------------------------------------------


def test_parse_mountinfo_reads_mount_points_and_decodes_escapes():
    assert parse_mountinfo(MOUNTINFO) == {"/": (8, 1), "/proc": (0, 21), "/mnt/with space": (8, 1), "/mnt/bind": (8, 1)}


# Captured 2026-10-29 from a real kernel (WSL2 6.6.87.2, docker-desktop distro, root):
# `mount --bind /tmp/r37probe/src /tmp/r37probe/outer/mnt`; `stat -c %d` gave 2096 for
# BOTH outer/ and outer/mnt -- the bind mount is invisible to st_dev. 2096 == makedev(8, 48).
REAL_BIND = (
    "80 65 8:48 / / rw,relatime - ext4 /dev/sdd rw,discard,errors=remount-ro,data=ordered\n"
    "75 80 0:29 / /lib/modules/6.6.87.2-microsoft-standard-WSL2 rw,nosuid,nodev,noatime - overlay none "
    "rw,lowerdir=/modules,upperdir=/lib/modules/6.6.87.2-microsoft-standard-WSL2/rw/upper,"
    "workdir=/lib/modules/6.6.87.2-microsoft-standard-WSL2/rw/work,uuid=on\n"
    "130 80 8:48 /tmp/r37probe/src /tmp/r37probe/outer/mnt rw,relatime - ext4 /dev/sdd "
    "rw,discard,errors=remount-ro,data=ordered\n"
)


def test_real_kernel_bind_mount_is_proven_by_mountinfo_not_by_st_dev():
    real_dev = {"/tmp/r37probe/outer": 2096, "/tmp/r37probe/outer/mnt": 2096}
    parsed = parse_mountinfo(REAL_BIND)
    assert parsed is not None and parsed["/tmp/r37probe/outer/mnt"] == (8, 48)
    mnt = "/tmp/r37probe/outer/mnt"
    p = mounts.probe(mnt, platform="linux", statx=None, read_mountinfo=lambda: REAL_BIND, stat_dev=real_dev.get)
    assert p.basis == "mountinfo"
    assert p.may_stop("/tmp/r37probe/outer/mnt", "/tmp/r37probe/outer") is True
    blind = MountProbe("st_dev-only", stat_dev=real_dev.get)
    assert blind.may_stop("/tmp/r37probe/outer/mnt", "/tmp/r37probe/outer") is False  # what st_dev alone sees


def test_mountinfo_device_numbers_use_the_linux_encoding_on_every_host():
    # mountinfo is Linux-only; macOS os.makedev encodes major << 24 | minor (CI on ac933f3)
    assert mounts._linux_makedev(8, 48) == 2096  # real kernel: stat -c %d gave 2096 for 8:48
    assert mounts._linux_makedev(259, 0) == 259 << 8
    assert mounts._linux_makedev(0, 256) == 256 << 12  # minor bits above 0xFF move up by 12
    assert mounts._linux_makedev(4096, 0) == 4096 << 32  # major bits above 0xFFF move up by 32


def test_parse_mountinfo_drops_a_mount_covered_by_a_later_one():
    """A bind mount at /a/b/x, then tmpfs over /a/b: /a/b/x now resolves into the tmpfs."""
    base = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n30 22 8:1 /srv /a/b/x rw - ext4 /dev/sda1 rw\n"
    assert "/a/b/x" in parse_mountinfo(base)  # control: visible before the cover
    covered = parse_mountinfo(base + "31 22 0:50 / /a/b rw - tmpfs tmpfs rw\n")
    assert covered == {"/": (8, 1), "/a/b": (0, 50)}


def test_parse_mountinfo_keeps_a_mount_inside_or_on_top():
    text = (
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "31 22 0:50 / /a/b rw - tmpfs tmpfs rw\n"
        "32 31 8:1 /srv /a/b/x rw - ext4 /dev/sda1 rw\n"  # mounted INSIDE the tmpfs: visible
        "33 32 0:60 / /a/b/x rw - tmpfs tmpfs rw\n"  # stacked on /a/b/x: the top one wins
    )
    assert parse_mountinfo(text) == {"/": (8, 1), "/a/b": (0, 50), "/a/b/x": (0, 60)}


@pytest.mark.parametrize(
    "text",
    [
        "",  # no table at all is no evidence
        "\n\n",
        "22 1 8:1 / / rw,relatime shared:1 ext4 /dev/sda1 rw\n",  # no " - " separator
        "22 1 8:1 / / - ext4 /dev/sda1 rw\n",  # only five fields before the separator
        "22 1 8:1 / / rw - ext4\n",  # fewer than three fields after it
        "x 1 8:1 / / rw - ext4 /dev/sda1 rw\n",  # non-numeric mount id
        "22 1 8-1 / / rw - ext4 /dev/sda1 rw\n",  # bad major:minor
        "22 1 8:1 / /mnt/a\\x rw - ext4 /dev/sda1 rw\n",  # backslash the kernel never writes
        "22 1 8:1 / mnt/rel rw - ext4 /dev/sda1 rw\n",  # relative mount point
        MOUNTINFO + "garbage\n",  # one bad line rejects the whole table
        MOUNTINFO + "41 22 8:1 / /other rw - ext4 /dev/sda1 rw\n",  # duplicate mount id
    ],
    ids=[
        "empty",
        "blank-lines",
        "no-separator",
        "five-fields",
        "short-tail",
        "non-numeric-id",
        "bad-major-minor",
        "stray-backslash",
        "relative-point",
        "one-bad-line",
        "duplicate-id",
    ],
)
def test_parse_mountinfo_rejects_malformed_tables(text):
    assert parse_mountinfo(text) is None


def test_parse_mountinfo_requires_str():
    with pytest.raises(TypeError):
        parse_mountinfo(MOUNTINFO.encode())


# ---- may_stop (pure) -------------------------------------------------------------

DEV = {"/a": 1, "/a/b": 1, "/a/b/c": 1, "/x": 2}


def _dev(path: str) -> int:
    return DEV[path]


@pytest.mark.parametrize("basis", ["st_dev-only", "mountinfo", "volume-path", "statx-mnt-id"])
def test_other_st_dev_always_stops(basis):
    p = MountProbe(basis, stat_dev=_dev, mount_points={}, ismount=lambda _: False, mnt_id=lambda _: 7)
    assert p.may_stop("/x", "/a") is True


def test_st_dev_only_never_stops_on_same_device():
    """Fail-closed: same st_dev is no proof of the same mount -- and no proof of another one."""
    p = MountProbe("st_dev-only", stat_dev=_dev)
    assert p.may_stop("/a/b", "/a") is False


def test_mountinfo_stops_at_a_listed_same_device_mount_point():
    p = MountProbe("mountinfo", stat_dev=_dev, mount_points={"/": 1, "/a/b": 1})
    assert p.may_stop("/a/b", "/a") is True  # bind mount of the same filesystem
    assert p.may_stop("/a/b/c", "/a/b") is False  # control: an ordinary directory


def test_mountinfo_entry_of_another_device_is_no_proof():
    """Listed, but the path resolves to another device than the entry's: it does not reach that mount."""
    p = MountProbe("mountinfo", stat_dev=_dev, mount_points={"/a/b": 99})
    assert p.may_stop("/a/b", "/a") is False


def test_statx_mount_ids_decide_on_same_device():
    ids = {"/a": 10, "/a/b": 11, "/a/b/c": 11}
    p = MountProbe("statx-mnt-id-unique", stat_dev=_dev, mnt_id=ids.get, mount_points=None)
    assert p.may_stop("/a/b", "/a") is True
    assert p.may_stop("/a/b/c", "/a/b") is False


def test_statx_without_a_mount_id_falls_back_to_mountinfo_or_keeps_walking():
    no_id = MountProbe("statx-mnt-id", stat_dev=_dev, mnt_id=lambda _: None, mount_points={"/a/b": 1})
    assert no_id.may_stop("/a/b", "/a") is True
    assert no_id.may_stop("/a/b/c", "/a/b") is False
    blind = MountProbe("statx-mnt-id", stat_dev=_dev, mnt_id=lambda _: None, mount_points=None)
    assert blind.may_stop("/a/b", "/a") is False


def test_volume_path_stops_where_windows_reports_a_mount_point():
    p = MountProbe("volume-path", stat_dev=_dev, ismount=lambda path: path == "/a/b")
    assert p.may_stop("/a/b", "/a") is True
    assert p.may_stop("/a/b/c", "/a/b") is False


# ---- probe selection (pure) -------------------------------------------------------


class _Statx:
    def __init__(self, mask: int, ids: dict[str, int] | None = None, error: int | None = None) -> None:
        self.mask, self.ids, self.error, self.calls = mask, ids or {}, error, []

    def __call__(self, path, mask):
        self.calls.append((path, mask))
        if self.error is not None:
            raise OSError(self.error, "injected statx failure")

        class R:
            pass

        r = R()
        r.stx_mask = self.mask
        r.stx_mnt_id = self.ids.get(path, 1) if self.mask else None
        return r


@pytest.fixture
def statx_constants(monkeypatch):
    # Values from linux/stat.h; os has them only on Python >= 3.15 on Linux.
    monkeypatch.setattr(os, "STATX_MNT_ID", 0x1000, raising=False)
    monkeypatch.setattr(os, "STATX_MNT_ID_UNIQUE", 0x4000, raising=False)


def _probe(**kw):
    kw.setdefault("read_mountinfo", lambda: MOUNTINFO)
    kw.setdefault("stat_dev", _dev)
    return mounts.probe("/a/b", **kw)


def test_probe_prefers_unique_statx_mount_id(statx_constants):
    fake = _Statx(0x4000 | 0x1000, ids={"/a": 1, "/a/b": 2})
    p = _probe(platform="linux", statx=fake)
    assert p.basis == "statx-mnt-id-unique"
    assert p.may_stop("/a/b", "/a") is True
    assert len(fake.calls) == 3, fake.calls
    assert all(mask & 0x4000 for _path, mask in fake.calls), "a call did not request the unique mount id: %r" % fake.calls


def test_unique_basis_never_compares_a_reusable_id(statx_constants):
    """The probe saw the unique id; a later answer without it is no statx proof (mountinfo decides)."""

    class Downgrading(_Statx):
        def __call__(self, path, mask):
            r = super().__call__(path, mask)
            if len(self.calls) > 1:
                r.stx_mask = 0x1000  # only the old, reusable id
            return r

    fake = Downgrading(0x4000 | 0x1000, ids={"/a": 1, "/a/b": 2})
    p = _probe(platform="linux", statx=fake, read_mountinfo=lambda: None)
    assert p.basis == "statx-mnt-id-unique"
    assert p.may_stop("/a/b", "/a") is False  # ids differ, but they are not the ones the basis names


def test_probe_uses_old_statx_mount_id_before_linux_6_8(statx_constants):
    assert _probe(platform="linux", statx=_Statx(0x1000)).basis == "statx-mnt-id"


def test_probe_falls_back_to_mountinfo_when_statx_reports_no_mount_id(statx_constants):
    assert _probe(platform="linux", statx=_Statx(0)).basis == "mountinfo"


@pytest.mark.parametrize("err", [errno.ENOSYS, errno.EPERM])
def test_probe_falls_back_to_mountinfo_when_statx_is_blocked(statx_constants, err):
    escaped = None
    try:
        p = _probe(platform="linux", statx=_Statx(0x1000, error=err))
    except OSError as e:
        escaped = e
    assert escaped is None, "a blocked statx must fall back, not abort the open: %r" % escaped
    assert p.basis == "mountinfo"


def test_probe_mountinfo_without_statx():
    p = _probe(platform="linux", statx=None)
    assert p.basis == "mountinfo"
    assert p.may_stop("/a/b", "/a") is False  # /a/b is not in MOUNTINFO


def test_probe_without_any_linux_source_is_st_dev_only():
    p = _probe(platform="linux", statx=None, read_mountinfo=lambda: None)
    assert p.basis == "st_dev-only"
    assert p.may_stop("/a/b", "/a") is False


def test_probe_malformed_mountinfo_is_no_source():
    assert _probe(platform="linux", statx=None, read_mountinfo=lambda: "garbage\n").basis == "st_dev-only"


def test_probe_statx_without_mount_id_and_without_mountinfo_is_st_dev_only(statx_constants):
    p = _probe(platform="linux", statx=_Statx(0), read_mountinfo=lambda: None)
    assert p.basis == "st_dev-only"
    assert p.may_stop("/a/b", "/a") is False


@pytest.mark.parametrize("platform", ["darwin", "freebsd14", "sunos5"])
def test_probe_other_posix_is_st_dev_only(platform):
    assert _probe(platform=platform).basis == "st_dev-only"


def test_probe_windows_uses_volume_path():
    assert _probe(platform="win32").basis == "volume-path"


# ---- store: the walk stops at a bind mount, and only there ------------------------


class FsyncDirFake:
    def __init__(self) -> None:
        self.failing: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        path = os.path.realpath(path)
        self.calls.append(path)
        if path in self.failing:
            raise OSError(errno.EIO, "injected fsync failure", path)
        return "ok"


@pytest.fixture
def fake(monkeypatch):
    f = FsyncDirFake()
    monkeypatch.setattr(durable, "fsync_dir", f)
    return f


def _open_and_ingest(root: str) -> IngestResult | IngestError:
    try:
        with ObjectStore(root) as s:
            return s.ingest(io.BytesIO(b"payload"), max_bytes=1024)
    except IngestError as e:
        return e


def _layout(tmp_path):
    outer = tmp_path / "outer"
    mnt = outer / "mnt"
    mnt.mkdir(parents=True)
    return os.path.realpath(outer), os.path.realpath(mnt), str(mnt / "store")


def _force_probe(monkeypatch, basis: str, mount_points: frozenset[str] | None):
    def forced(start, **_kw):
        points = None if mount_points is None else {p: os.stat(p).st_dev for p in mount_points}
        return MountProbe(basis, stat_dev=lambda p: os.stat(p).st_dev, mount_points=points)

    monkeypatch.setattr(mounts, "probe", forced)


def test_walk_stops_at_a_same_device_mount_point(tmp_path, fake, monkeypatch):
    """mnt/ is a bind mount (same st_dev, listed as mount point): outer/ is not ours."""
    outer, mnt, root = _layout(tmp_path)
    fake.failing.add(outer)
    _force_probe(monkeypatch, "mountinfo", frozenset({mnt}))
    outcome = _open_and_ingest(root)
    assert isinstance(outcome, IngestResult), outcome
    assert outer not in fake.calls, "the walk crossed the mount point into %s" % outer
    assert mnt in fake.calls, "positive control: the store's own parent was synced"
    with ObjectStore(root) as s:
        assert s.layout_mount_basis == "mountinfo"


def test_walk_without_mount_identity_crosses_and_reports(tmp_path, fake, monkeypatch):
    """Negative control: same layout, no OS mount identity -> the walk goes on (fail-closed)."""
    outer, mnt, root = _layout(tmp_path)
    fake.failing.add(outer)
    _force_probe(monkeypatch, "st_dev-only", None)
    outcome = _open_and_ingest(root)
    assert outer in fake.calls, "the walk stopped without proof"
    assert isinstance(outcome, store_mod.LayoutNotDurable), outcome
    assert outcome.layout_sync[outer].startswith("failed:")


def test_mount_identity_does_not_shorten_the_r36_chain(tmp_path, fake, monkeypatch):
    """SF-R36-01 must still hold with a mount table: an unlisted ancestor is re-proven."""
    existing = tmp_path / "existing"
    existing.mkdir()
    root = str(existing / "new" / "store")
    fake.failing.add(os.path.realpath(existing))
    _force_probe(monkeypatch, "mountinfo", frozenset({os.path.realpath(tmp_path.anchor or "/")}))
    assert isinstance(_open_and_ingest(root), store_mod.LayoutNotDurable)
    fake.calls.clear()
    retry = _open_and_ingest(root)
    assert os.path.realpath(existing) in fake.calls
    assert isinstance(retry, store_mod.LayoutNotDurable), retry


# ---- real OS controls --------------------------------------------------------------

_KNOWN = {"statx-mnt-id-unique", "statx-mnt-id", "mountinfo", "volume-path", "st_dev-only"}


def test_store_reports_its_mount_basis(tmp_path):
    with ObjectStore(str(tmp_path / "store")) as s:
        basis = s.layout_mount_basis
    assert basis in _KNOWN
    if sys.platform == "win32":
        assert basis == "volume-path"
    elif sys.platform.startswith("linux") and os.path.exists("/proc/self/mountinfo"):
        assert basis != "st_dev-only"
    elif sys.platform == "darwin":
        assert basis == "st_dev-only"


@pytest.mark.skipif(not os.path.exists("/proc/self/mountinfo"), reason="no /proc/self/mountinfo on %s" % sys.platform)
def test_real_mountinfo_parses_and_marks_proc_not_tmp(tmp_path):
    points = parse_mountinfo(mounts._read_mountinfo())
    assert points is not None and "/" in points
    real = os.path.realpath(tmp_path)
    assert real not in points
    p = mounts.probe(real)
    assert p.may_stop(real, os.path.dirname(real)) is False  # negative control
    if "/proc" in points:
        assert p.may_stop("/proc", "/") is True  # positive control from the real kernel


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and hasattr(os, "statx")),
    reason="os.statx needs Linux and Python >= 3.15 (this is %s, Python %d.%d)" % ((sys.platform,) + sys.version_info[:2]),
)
def test_real_statx_mount_id(tmp_path):
    real = os.path.realpath(tmp_path)
    p = mounts.probe(real)
    assert p.basis in {"statx-mnt-id-unique", "statx-mnt-id", "mountinfo"}
    assert p.may_stop(real, os.path.dirname(real)) is False


@pytest.mark.skipif(sys.platform != "win32", reason="GetVolumePathNameW is Windows-only")
def test_real_windows_volume_path(tmp_path):
    real = os.path.realpath(tmp_path)
    p = mounts.probe(real)
    assert p.basis == "volume-path"
    assert p.may_stop(real, os.path.dirname(real)) is False  # negative control
    # A positive control needs a volume mounted on a folder (mountvol, admin): not here.
