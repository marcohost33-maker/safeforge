"""Where may the layout-ancestor walk stop? Mount identity, not just ``st_dev`` (R15).

GPT-6 cross-family R15 (2026-10-28, PR #2 @89a0381): "die st_dev-Stopregel
beweist keine Mountidentitaet". ``st_dev`` names a filesystem, not a mount:

* Python docs, ``os.path.ismount``: on POSIX it compares ``st_dev`` of a path
  and its parent and "is not able to reliably detect bind mounts on the same
  filesystem".
* rename(2), EXDEV: "Linux permits a filesystem to be mounted at multiple
  points, but rename() does not work across different mount points, even if
  the same filesystem is mounted on both."

What the walk needs (``store._same_mount_ancestors``): it re-syncs the entry of
every ancestor of the store root that a failed earlier open may have created,
and it may stop where an ancestor is the root of another mount -- that
directory existed before the mount, its entry was never ours. A stop is
therefore a claim ("this entry is not ours"); continuing is never wrong for
durability, it only syncs more. So the rule is fail-closed in one direction:

    may_stop(child, parent) is True only on PROOF that child roots another
    mount or filesystem; unknown means "keep walking".

Proof sources, best first (``MountProbe.basis`` names the one used):

``statx-mnt-id-unique`` / ``statx-mnt-id``
    Linux, Python >= 3.15 (``os.statx``, "Added in version 3.15"):
    ``stx_mnt_id`` with ``STATX_MNT_ID`` (Linux 5.8) or ``STATX_MNT_ID_UNIQUE``
    (Linux 6.8, "guaranteed to not be reused while the system is running").
    The kernel clears an unsupported bit in ``stx_mask`` instead of failing
    (statx(2)); a cleared bit falls through to the next source.
``mountinfo``
    Linux: ``/proc/self/mountinfo`` field 5, the mount point relative to the
    process root (proc_pid_mountinfo(5)). Parsed strictly: one malformed line
    rejects the whole table, because a misparsed path could invent a mount
    point and stop the walk early. For the same reason an entry covered by a
    later mount is dropped, and a listed point stops the walk only if its
    ``major:minor`` is the ``st_dev`` the path resolves to.
``volume-path``
    Windows: ``os.path.ismount`` (``GetVolumePathNameW``; volume mount points).
``st_dev-only``
    Anything else (macOS, BSD, Linux without /proc): no mount identity from
    the OS. Same ``st_dev`` never stops the walk -- bind mounts or nullfs
    mounts on the same filesystem are walked through and their outer
    ancestors synced (or reported ``failed:``), never silently skipped.

A different ``st_dev`` always stops (as before). That is a filesystem
boundary, which also covers btrfs subvolume roots that are not mounts:
SafeForge never creates a subvolume, so their entries are not ours either.

Not covered here (R15, still an integration/durability gate): what a
directory fsync means on OverlayFS or through a bind mount after a power cut.
This module decides which entries to sync, not whether the sync holds.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable

__all__ = ["MountProbe", "parse_mountinfo", "parse_mountinfo_filesystems", "probe"]

_MOUNTINFO = "/proc/self/mountinfo"
_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str | None:
    """Decode the kernel's ``\\ooo`` escapes (space, tab, newline, backslash).

    Any other backslash is not something the kernel writes: None (reject).
    """
    if "\\" in _OCTAL.sub("", field):
        return None
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def _covers(outer: str, inner: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def parse_mountinfo(text: str) -> dict[str, tuple[int, int]] | None:
    parsed = _parse_visible(text)
    return None if parsed is None else {point: dev for point, (dev, _fs) in parsed[0].items()}


def parse_mountinfo_filesystems(text: str) -> dict[str, tuple[str, frozenset[str], frozenset[str]]] | None:
    """Visible mount points -> ``(fstype, mount options, super options)``; None if malformed (R41).

    Same strict parser and the same covered-entry rule as :func:`parse_mountinfo`,
    so a path resolves to the filesystem it can actually reach. Options are the
    comma-separated fields 6 (per mount) and the last field (per superblock).

    A point whose entries ALL drop out (two siblings on one point cover each
    other, e.g. after mount propagation) maps to ``("", frozenset(), frozenset())``:
    which filesystem a path there reaches is not decidable from the table, and
    falling back to the parent mount could name the wrong one.
    """
    parsed = _parse_visible(text)
    if parsed is None:
        return None
    visible, ambiguous = parsed
    out = {point: fs for point, (_dev, fs) in visible.items()}
    for point in ambiguous:
        out.setdefault(point, ("", frozenset(), frozenset()))
    return out


_Visible = dict[str, tuple[tuple[int, int], tuple[str, frozenset[str], frozenset[str]]]]


def _parse_visible(text: str) -> tuple[_Visible, set[str]] | None:
    """Visible mount points of a ``/proc/self/mountinfo`` text -> ``(major, minor)``; None if malformed.

    Line format (proc_pid_mountinfo(5)): ``id parent major:minor root mountpoint
    options [optional...] - fstype source superoptions``. Strict on purpose:
    an empty table, a line without the `` - `` separator, fewer than six fields
    before it, a non-numeric or duplicate id or a stray backslash all give None.

    A mount stays listed after another mount covers its path (mounted later on
    the same point or on an ancestor, from outside its own parent chain); the
    path then resolves into the newer mount. Such a covered entry is dropped:
    only a mount the path can actually reach may stop the walk.
    """
    if type(text) is not str:
        raise TypeError("mountinfo text must be str")
    entries: list[tuple[int, int, tuple[int, int], str]] = []
    filesystems: dict[int, tuple[str, frozenset[str], frozenset[str]]] = {}
    for line in text.splitlines():
        if not line:
            continue
        fields = line.split(" ")
        if "-" not in fields:
            return None
        head = fields[: fields.index("-")]
        tail = fields[fields.index("-") + 1 :]
        if len(head) < 6 or len(tail) < 3:
            return None
        dev = re.fullmatch(r"([0-9]+):([0-9]+)", head[2])
        if not (head[0].isdigit() and head[1].isdigit() and dev):
            return None
        point = _unescape(head[4])
        if point is None or not point.startswith("/"):
            return None
        fstype = _unescape(tail[0])
        if not fstype:
            return None
        entries.append((int(head[0]), int(head[1]), (int(dev.group(1)), int(dev.group(2))), point))
        filesystems[int(head[0])] = (fstype, frozenset(head[5].split(",")), frozenset(tail[-1].split(",")))
    if not entries:
        return None
    parent_of = {mid: parent for mid, parent, _dev, _point in entries}
    if len(parent_of) != len(entries):
        return None
    hidden = _covered_entries(entries, parent_of)
    visible: _Visible = {}
    for mid, _parent, dev, point in entries:
        if mid not in hidden:
            visible[point] = (dev, filesystems[mid])
    # Points with two or more entries, none reachable. One counting pass: a
    # per-point scan of the table was quadratic again (Codex on d4681e4, P2).
    occurrences: dict[str, int] = {}
    for _mid, _parent, _dev, point in entries:
        occurrences[point] = occurrences.get(point, 0) + 1
    ambiguous = {p for p, n in occurrences.items() if n > 1 and p not in visible}
    return visible, ambiguous


def _slash_prefixes(inner: str) -> list[str]:
    """Every ``outer.rstrip("/")`` for which ``inner.startswith(outer.rstrip("/") + "/")``:
    ``inner[:i]`` where ``inner[i]`` is the first ``/`` of a run."""
    return [inner[:i] for i, c in enumerate(inner) if c == "/" and (i == 0 or inner[i - 1] != "/")]


class _CoverCount:
    """How many entries of a multiset of mount points cover ``inner`` (:func:`_covers`),
    in O(len(inner)) instead of a scan: ``_covers(outer, inner)`` holds iff
    ``outer == inner`` or ``outer.rstrip("/")`` is one of ``_slash_prefixes(inner)``.
    Both can hold for the same entry only if ``inner`` ends with ``/`` (then the
    prefix term already counts every ``outer == inner``)."""

    def __init__(self) -> None:
        self.exact: dict[str, int] = {}
        self.stripped: dict[str, int] = {}

    def add(self, point: str, k: int = 1) -> None:
        self.exact[point] = self.exact.get(point, 0) + k
        key = point.rstrip("/")
        self.stripped[key] = self.stripped.get(key, 0) + k

    def covering(self, inner: str) -> int:
        n = sum(self.stripped.get(p, 0) for p in _slash_prefixes(inner))
        return n if inner.endswith("/") else n + self.exact.get(inner, 0)


def _covered_entries(entries: list[tuple[int, int, tuple[int, int], str]], parent_of: dict[int, int]) -> set[int]:
    """Mount ids whose point another entry covers from outside their own parent chain.

    Same rule as the all-pairs scan up to c349396 (kept as the oracle in
    tests/test_r38_codex_staging_mountinfo.py): entry ``e`` is covered iff some
    ``o != e``, not among ``e``'s ancestors (following ``parent_of`` until an id
    outside the table, a self-parent or a repeat), has ``_covers(point(o),
    point(e))``. SF-R38-02 (Codex on ac933f3): that scan was quadratic. Here ``e``
    is covered iff the entries covering it, minus itself, outnumber those among
    its ancestors -- counted with one table-wide :class:`_CoverCount` and one for
    the current path of a depth-first walk over the parent forest (cycles, which
    the kernel never writes, are walked as a root whose ancestors are the whole
    cycle). Linear in the table times the path length.
    """
    point_of = {mid: point for mid, _parent, _dev, point in entries}

    def up(mid: int) -> int | None:
        parent = parent_of[mid]
        return parent if parent in parent_of and parent != mid else None

    total = _CoverCount()
    for point in point_of.values():
        total.add(point)
    hidden: set[int] = set()

    def judge(mid: int, path: _CoverCount, path_has_self: bool) -> None:
        on_path = path.covering(point_of[mid]) - (1 if path_has_self else 0)
        if total.covering(point_of[mid]) - 1 > on_path:
            hidden.add(mid)

    # Cycles of the parent graph (iterative colouring; up() drops self-parents).
    on_cycle: dict[int, list[int]] = {}
    state: dict[int, int] = {}
    for start in parent_of:
        trail: list[int] = []
        mid: int | None = start
        while mid is not None and mid not in state:
            state[mid] = 1
            trail.append(mid)
            mid = up(mid)
        if mid is not None and state[mid] == 1:
            cycle = trail[trail.index(mid) :]
            for c in cycle:
                on_cycle[c] = cycle
        for t in trail:
            state[t] = 2

    children: dict[int, list[int]] = {}
    for mid in parent_of:
        parent = up(mid)
        if parent is not None and mid not in on_cycle:
            children.setdefault(parent, []).append(mid)

    def walk(top: int, path: _CoverCount) -> None:
        # Pre-order over top's subtree; ``path`` holds the ancestors of the node judged.
        stack: list[tuple[int, bool]] = [(c, False) for c in children.get(top, [])]
        while stack:
            mid, leaving = stack.pop()
            if leaving:
                path.add(point_of[mid], -1)
                continue
            judge(mid, path, False)
            path.add(point_of[mid])
            stack.append((mid, True))
            stack.extend((c, False) for c in children.get(mid, []))

    seen_cycles: set[int] = set()
    for mid in parent_of:
        if up(mid) is None:
            path = _CoverCount()
            judge(mid, path, False)
            path.add(point_of[mid])
            walk(mid, path)
        elif mid in on_cycle and on_cycle[mid][0] not in seen_cycles:
            cycle = on_cycle[mid]
            seen_cycles.add(cycle[0])
            path = _CoverCount()
            for c in cycle:
                path.add(point_of[c])
            for c in cycle:
                judge(c, path, True)
            for c in cycle:
                walk(c, path)
    return hidden


def _read_mountinfo() -> str | None:
    try:
        with open(_MOUNTINFO, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except OSError:
        return None


def _stat_dev(path: str) -> int:
    return os.stat(path).st_dev


def _linux_makedev(major: int, minor: int) -> int:
    """``st_dev`` for a mountinfo ``major:minor``, always in glibc's encoding
    (gnu_dev_makedev). mountinfo exists only on Linux, where this equals
    ``os.makedev``; the host's ``os.makedev`` is NOT used, because macOS/BSD encode
    ``major << 24 | minor`` and turned makedev(8, 48) into 134217776 instead of
    2096 (CI macos-latest on ac933f3)."""
    return ((major & 0xFFFFF000) << 32) | ((major & 0xFFF) << 8) | ((minor & 0xFFFFFF00) << 12) | (minor & 0xFF)


class MountProbe:
    """One walk's view of mount identity. Build it with :func:`probe`."""

    def __init__(
        self,
        basis: str,
        *,
        stat_dev: Callable[[str], int],
        mnt_id: Callable[[str], int | None] | None = None,
        mount_points: dict[str, int] | None = None,
        ismount: Callable[[str], bool] | None = None,
    ) -> None:
        self.basis = basis
        self._stat_dev = stat_dev
        self._mnt_id = mnt_id
        self._mount_points = mount_points
        self._ismount = ismount

    def may_stop(self, child: str, parent: str) -> bool:
        """True only if ``child`` provably roots another filesystem or mount than ``parent``."""
        if self._stat_dev(child) != self._stat_dev(parent):
            return True
        if self._mnt_id is not None:
            a, b = self._mnt_id(child), self._mnt_id(parent)
            if a is not None and b is not None:
                return a != b
            # The kernel did not report a mount id for this path: no proof from statx.
            if self._mount_points is None:
                return False
        if self._mount_points is not None:
            # Listed AND its device is the one the path resolves to; otherwise
            # the path does not reach that mount (no proof, keep walking).
            return child in self._mount_points and self._mount_points[child] == self._stat_dev(child)
        if self._ismount is not None:
            return bool(self._ismount(child))
        return False  # st_dev-only: same device is no proof of the same mount; keep walking


def _statx_mnt_id(statx, mask: int, want: int) -> Callable[[str], int | None]:
    """Mount id per path, from the SAME request as the probe (``mask``), and only
    if the kernel set the bit the basis was named after (``want``): a probe
    named "unique" never compares a reusable id."""

    def mnt_id(path: str) -> int | None:
        r = statx(path, mask)
        if not r.stx_mask & want:
            return None
        return r.stx_mnt_id

    return mnt_id


_REAL = object()


def probe(
    start: str,
    *,
    platform: str | None = None,
    statx=_REAL,
    read_mountinfo: Callable[[], str | None] = _read_mountinfo,
    ismount: Callable[[str], bool] = os.path.ismount,
    stat_dev: Callable[[str], int] = _stat_dev,
) -> MountProbe:
    """The best mount-identity source this OS offers for a walk starting at ``start``.

    ``platform``/``statx``/``read_mountinfo``/``ismount``/``stat_dev`` default to
    the real OS and exist so tests can force each branch on any runner
    (``statx=None`` = this interpreter has no ``os.statx``).
    """
    platform = sys.platform if platform is None else platform
    if statx is _REAL:
        statx = getattr(os, "statx", None)
    if platform.startswith("linux"):
        mount_points = None
        text = read_mountinfo()
        parsed = parse_mountinfo(text) if text is not None else None
        if parsed is not None:
            mount_points = {point: _linux_makedev(*dev) for point, dev in parsed.items()}
        if statx is not None and hasattr(os, "STATX_MNT_ID"):
            unique = getattr(os, "STATX_MNT_ID_UNIQUE", 0)
            mask = os.STATX_MNT_ID | unique
            try:
                r = statx(start, mask)
            except OSError:
                r = None  # e.g. ENOSYS/EPERM under a seccomp filter: no statx proof
            if r is not None:
                if unique and r.stx_mask & unique:
                    mnt_id = _statx_mnt_id(statx, mask, unique)
                    return MountProbe("statx-mnt-id-unique", stat_dev=stat_dev, mnt_id=mnt_id, mount_points=mount_points)
                if r.stx_mask & os.STATX_MNT_ID:
                    mnt_id = _statx_mnt_id(statx, mask, os.STATX_MNT_ID)
                    return MountProbe("statx-mnt-id", stat_dev=stat_dev, mnt_id=mnt_id, mount_points=mount_points)
        if mount_points is not None:
            return MountProbe("mountinfo", stat_dev=stat_dev, mount_points=mount_points)
        return MountProbe("st_dev-only", stat_dev=stat_dev)
    if platform == "win32":
        return MountProbe("volume-path", stat_dev=stat_dev, ismount=ismount)
    return MountProbe("st_dev-only", stat_dev=stat_dev)
