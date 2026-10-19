"""Structural validation and safe extraction of uploaded ZIP archives.

What this checks (each rejection carries a stable ``code``):

* names: absolute paths, ``..``/``.``/empty components, backslashes, colons
  (drive letters, NTFS alternate data streams), NUL and control characters,
  trailing dot/space (Windows strips them), Windows device names (CON, NUL,
  COM1, ...), over-long names
* collisions: exact duplicates, names that differ only in case or Unicode
  normalisation (they overwrite each other on Windows/macOS), file-vs-directory
* entry types: symlinks, devices/FIFOs (Unix mode bits), reparse points,
  encrypted entries, compression methods other than STORED/DEFLATED
* resource limits: entry count, per-entry and total uncompressed size,
  compression ratio, overlapping entries (non-recursive "zip bombs"), and a
  full decompression pass that verifies sizes and CRCs
* optionally: an ``index.html`` at the archive root

What it does NOT do: judge content. A structurally perfect archive can carry
malicious JavaScript. Content scanning, sandboxed serving (separate origin,
CSP) and review are separate layers.

Names are checked on ``ZipInfo.orig_filename``: ``zipfile`` rewrites ``\\`` to
``/`` on Windows only and truncates at NUL, so checking ``filename`` would
make the verdict platform-dependent.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

__all__ = ["ZipPolicy", "ZipEntry", "ZipRejected", "validate_zip", "extract_zip"]

_RESERVED = (
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {"COM%s" % c for c in "123456789¹²³"}
    | {"LPT%s" % c for c in "123456789¹²³"}
)
_DRIVE_OR_STREAM = re.compile(r":")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_LOCAL_HEADER_MIN = 30


@dataclass(frozen=True)
class ZipPolicy:
    max_entries: int = 5000
    max_total_bytes: int = 256 * 1024 * 1024
    max_entry_bytes: int = 64 * 1024 * 1024
    max_ratio: float = 200.0
    ratio_min_bytes: int = 64 * 1024  # small files may compress extremely well; size caps cover them
    max_component_bytes: int = 255
    max_path_chars: int = 1024
    require_root_index: bool = True
    allowed_methods: tuple[int, ...] = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

    def __post_init__(self) -> None:
        for name in ("max_entries", "max_total_bytes", "max_entry_bytes", "max_component_bytes", "max_path_chars"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ValueError("%s must be a positive int" % name)
        if not (self.max_ratio > 0):
            raise ValueError("max_ratio must be > 0")


@dataclass(frozen=True)
class ZipEntry:
    name: str
    size: int
    is_dir: bool


class ZipRejected(ValueError):
    def __init__(self, code: str, detail: str, entry: str | None = None) -> None:
        super().__init__("%s: %s%s" % (code, detail, "" if entry is None else " [%r]" % entry))
        self.code = code
        self.detail = detail
        self.entry = entry


def _check_name(raw: str, is_dir: bool, policy: ZipPolicy) -> list[str]:
    if raw == "":
        raise ZipRejected("empty_name", "entry with empty name")
    if "\x00" in raw:
        raise ZipRejected("nul_in_name", "NUL byte in name (zipfile would truncate it)", raw)
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ZipRejected("control_char", "control character in name", raw)
    if "\\" in raw:
        raise ZipRejected("backslash", "backslash in name (path separator on Windows)", raw)
    if raw.startswith("/"):
        raise ZipRejected("absolute_path", "absolute path", raw)
    if _DRIVE_OR_STREAM.search(raw):
        raise ZipRejected("colon", "colon in name (drive letter or NTFS stream)", raw)
    if len(raw) > policy.max_path_chars:
        raise ZipRejected("path_too_long", "path longer than %d chars" % policy.max_path_chars, raw)
    body = raw[:-1] if is_dir else raw
    parts = body.split("/")
    for p in parts:
        if p == "":
            raise ZipRejected("empty_component", "empty path component", raw)
        if p in (".", ".."):
            raise ZipRejected("dot_component", "'.' or '..' component", raw)
        if p[-1] in " .":
            raise ZipRejected("trailing_dot_space", "component ends with dot or space", raw)
        if p.split(".")[0].rstrip(" ").upper() in _RESERVED:
            raise ZipRejected("reserved_name", "Windows device name", raw)
        if len(p.encode("utf-8")) > policy.max_component_bytes:
            raise ZipRejected("name_too_long", "component longer than %d bytes" % policy.max_component_bytes, raw)
    return parts


def _validate(zf: zipfile.ZipFile, policy: ZipPolicy) -> list[ZipEntry]:
    infos = zf.infolist()
    if len(infos) > policy.max_entries:
        raise ZipRejected("too_many_entries", "%d entries > %d" % (len(infos), policy.max_entries))
    seen: set[str] = set()
    keys: dict[str, str] = {}
    file_keys: set[str] = set()
    dir_keys: set[str] = set()
    total = 0
    out: list[ZipEntry] = []
    for info in infos:
        raw = info.orig_filename
        is_dir = raw.endswith("/")
        parts = _check_name(raw, is_dir, policy)
        if raw in seen:
            raise ZipRejected("duplicate_name", "name appears twice", raw)
        seen.add(raw)
        key = unicodedata.normalize("NFC", raw.rstrip("/")).casefold()
        if key in keys:
            raise ZipRejected("case_collision", "collides with %r (case/Unicode normalisation)" % keys[key], raw)
        keys[key] = raw
        for i in range(1, len(parts)):
            dir_keys.add(unicodedata.normalize("NFC", "/".join(parts[:i])).casefold())
        (dir_keys if is_dir else file_keys).add(key)

        mode = info.external_attr >> 16
        if info.create_system == 3 and mode:
            fmt = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode):
                raise ZipRejected("symlink", "symbolic link entry", raw)
            if fmt not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ZipRejected("special_file", "device/FIFO/socket entry", raw)
        if info.external_attr & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ZipRejected("reparse_point", "Windows reparse point attribute", raw)
        if info.flag_bits & 0x1:
            raise ZipRejected("encrypted", "encrypted entry", raw)
        if info.compress_type not in policy.allowed_methods:
            raise ZipRejected("compression_method", "method %d not allowed" % info.compress_type, raw)
        if is_dir:
            if info.file_size != 0:
                raise ZipRejected("dir_with_data", "directory entry with data", raw)
        else:
            if info.file_size > policy.max_entry_bytes:
                raise ZipRejected("entry_too_large", "%d > %d bytes" % (info.file_size, policy.max_entry_bytes), raw)
            total += info.file_size
            if total > policy.max_total_bytes:
                raise ZipRejected("total_too_large", "uncompressed total > %d bytes" % policy.max_total_bytes, raw)
            if info.file_size >= policy.ratio_min_bytes:
                if info.compress_size == 0 or info.file_size / info.compress_size > policy.max_ratio:
                    raise ZipRejected("ratio", "compression ratio above %g" % policy.max_ratio, raw)
        out.append(ZipEntry(raw.rstrip("/") if is_dir else raw, info.file_size, is_dir))

    clash = file_keys & dir_keys
    if clash:
        raise ZipRejected("file_dir_conflict", "name used as file and directory", sorted(clash)[0])

    # Overlap: each entry occupies at least [header_offset, header_offset + 30 + compress_size).
    # Entries that overlap share compressed data -- the non-recursive zip bomb construction.
    spans = sorted((i.header_offset, i.header_offset + _LOCAL_HEADER_MIN + i.compress_size, i.orig_filename) for i in infos)
    end_of_data = zf.start_dir
    for (a0, a1, name), nxt in zip(spans, spans[1:] + [(end_of_data, None, None)]):
        if a1 > nxt[0]:
            raise ZipRejected("overlap", "entry data overlaps the next entry or the central directory", name)

    if policy.require_root_index and "index.html" not in seen:
        raise ZipRejected("missing_root_index", "no index.html at archive root")

    # Full pass: decompress everything once. zipfile stops at the declared size
    # and verifies the CRC, so lying headers surface here as errors.
    read_total = 0
    for info in infos:
        if info.is_dir():
            continue
        n = 0
        try:
            with zf.open(info) as src:
                for chunk in iter(lambda: src.read(1 << 16), b""):
                    n += len(chunk)
                    read_total += len(chunk)
                    if n > info.file_size or read_total > policy.max_total_bytes:
                        raise ZipRejected("size_mismatch", "more data than declared", info.orig_filename)
        except (zipfile.BadZipFile, EOFError, NotImplementedError, RuntimeError) as e:
            raise ZipRejected("corrupt_data", "%s: %s" % (type(e).__name__, e), info.orig_filename) from e
        if n != info.file_size:
            raise ZipRejected("size_mismatch", "%d bytes read, %d declared" % (n, info.file_size), info.orig_filename)
    return out


def _open(source: str | os.PathLike[str] | BinaryIO) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(source)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError, NotImplementedError) as e:
        raise ZipRejected("not_a_zip", "%s: %s" % (type(e).__name__, e)) from e


def validate_zip(source: str | os.PathLike[str] | BinaryIO, policy: ZipPolicy | None = None) -> list[ZipEntry]:
    """Validate an archive. Returns its entries or raises :class:`ZipRejected`."""
    policy = policy or ZipPolicy()
    with _open(source) as zf:
        return _validate(zf, policy)


def extract_zip(
    source: str | os.PathLike[str] | BinaryIO, dest_dir: str, policy: ZipPolicy | None = None
) -> list[ZipEntry]:
    """Validate, then extract into a NEW directory ``dest_dir`` (must not exist).

    Validation and extraction use the same open archive (no re-read between
    check and use). Files are created exclusively; every target is checked to
    resolve inside ``dest_dir``.
    """
    policy = policy or ZipPolicy()
    with _open(source) as zf:
        entries = _validate(zf, policy)
        os.makedirs(dest_dir, exist_ok=False)
        base = os.path.realpath(dest_dir)
        for info in zf.infolist():
            rel = info.orig_filename.rstrip("/")
            target = os.path.join(base, *rel.split("/"))
            parent = target if info.is_dir() else os.path.dirname(target)
            os.makedirs(parent, exist_ok=True)
            if os.path.commonpath([base, os.path.realpath(parent)]) != base:
                raise ZipRejected("escape", "target resolves outside destination", info.orig_filename)
            if info.is_dir():
                continue
            with zf.open(info) as src, open(target, "xb") as dst:
                for chunk in iter(lambda: src.read(1 << 16), b""):
                    dst.write(chunk)
    return entries
