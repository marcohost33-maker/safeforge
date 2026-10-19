"""ZIP upload validation (claim T1 class; T2 limit). Every rejection code has a
dedicated malicious archive; a well-formed site archive is the positive control."""

from __future__ import annotations

import io
import os
import stat
import struct
import zipfile

import pytest

from safeforge import ZipPolicy, ZipRejected, extract_zip, validate_zip

SITE = {"index.html": b"<!doctype html><title>ok</title>", "js/app.js": b"console.log(1)", "css/a.css": b"body{}"}


def build(entries, *, method=zipfile.ZIP_DEFLATED, mutate=None):
    """entries: list of (name, data) or (ZipInfo-mutator, name, data)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        for e in entries:
            if len(e) == 2:
                name, data = e
                zi = zipfile.ZipInfo("placeholder")
                zi.filename = name  # bypass ZipInfo's Windows-only "\\" -> "/" rewrite
                zi.compress_type = method
                zf.writestr(zi, data)
            else:
                fn, name, data = e
                zi = zipfile.ZipInfo("placeholder")
                zi.filename = name
                zi.compress_type = method
                fn(zi)
                zf.writestr(zi, data)
    raw = buf.getvalue()
    return io.BytesIO(mutate(raw) if mutate else raw)


def site(*extra):
    return build(list(SITE.items()) + list(extra))


def code_of(source, policy=None):
    """Contract: validate_zip either returns or raises ZipRejected -- nothing else.
    Any other exception is an assertion failure here, not a test crash."""
    try:
        validate_zip(source, policy)
        err = None
    except Exception as e:  # noqa: BLE001
        err = e
    assert isinstance(err, ZipRejected), "expected ZipRejected, got %r" % (err,)
    return err.code


# ----------------------------------------------------------- positive control
def test_valid_site_passes_and_extracts(tmp_path):
    entries = validate_zip(site())
    assert {e.name for e in entries} == set(SITE)
    dest = tmp_path / "out"
    extract_zip(site(), str(dest))
    for name, data in SITE.items():
        assert (dest / name).read_bytes() == data


def test_directory_entries_are_fine():
    assert validate_zip(site(("assets/", b""), ("assets/x.txt", b"x")))


# ------------------------------------------------------------- name attacks
NAME_CASES = {
    "dot_component": "../evil.html",
    "dot_component_nested": "a/../../evil.html",
    "dot_component_single": "./index2.html",
    "absolute_path": "/etc/passwd",
    "backslash": "..\\evil.html",
    "colon": "C:/evil.html",
    "colon_ads": "index2.html:stream",
    "empty_component": "a//b.html",
    "trailing_dot_space": "evil.html.",
    "trailing_dot_space_dir": "dir /x.html",
    "reserved_name": "CON.html",
    "reserved_name_nested": "a/aux",
    "reserved_name_superscript": "COM\u00b9.txt",
    "control_char": "a\x07.html",
    "nul_in_name": "evil.html\x00.png",
}


@pytest.mark.parametrize("case", list(NAME_CASES))
def test_name_attack_rejected(case):
    expected = case.split("_nested")[0].split("_single")[0].split("_ads")[0].split("_superscript")[0]
    if case == "trailing_dot_space_dir":
        expected = "trailing_dot_space"
    assert code_of(site((NAME_CASES[case], b"x"))) == expected


def test_component_too_long():
    assert code_of(site(("a" * 256 + ".html", b"x"))) == "name_too_long"


def test_path_too_long():
    assert code_of(site(("/".join(["d" * 50] * 30) + "/x", b"x"))) == "path_too_long"


# -------------------------------------------------------------- collisions
@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_duplicate_name():
    assert code_of(site(("js/app.js", b"other"))) == "duplicate_name"


def test_case_collision():
    assert code_of(site(("INDEX.html", b"x"))) == "case_collision"


def test_unicode_normalisation_collision():
    nfc, nfd = "caf\u00e9.html", "cafe\u0301.html"
    assert code_of(build(list(SITE.items()) + [(nfc, b"1"), (nfd, b"2")])) == "case_collision"


def test_file_dir_conflict():
    assert code_of(site(("js", b"file named like a dir"))) == "file_dir_conflict"


# ------------------------------------------------------------- entry types
def _unix(mode):
    def fn(zi):
        zi.create_system = 3
        zi.external_attr = mode << 16

    return fn


def test_symlink_rejected():
    assert code_of(site((_unix(stat.S_IFLNK | 0o777), "link.html", b"/etc/passwd"))) == "symlink"


def test_fifo_rejected():
    assert code_of(site((_unix(stat.S_IFIFO | 0o644), "pipe", b""))) == "special_file"


def test_regular_unix_file_ok():
    assert validate_zip(site((_unix(stat.S_IFREG | 0o644), "ok.txt", b"x")))


def test_reparse_point_rejected():
    def fn(zi):
        zi.external_attr = 0x400

    assert code_of(site((fn, "rp.html", b"x"))) == "reparse_point"


def _set_encrypted_flag(raw: bytes) -> bytes:
    """zipfile cannot write encrypted entries; set bit 0 of the general-purpose
    flags in the first central-directory record (that is where readers look)."""
    eocd = raw.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", raw[eocd + 16: eocd + 20])[0]
    b = bytearray(raw)
    flags = struct.unpack("<H", b[cd_off + 8: cd_off + 10])[0]
    b[cd_off + 8: cd_off + 10] = struct.pack("<H", flags | 0x1)
    return bytes(b)


def test_encrypted_rejected():
    assert code_of(build(list(SITE.items()), mutate=_set_encrypted_flag)) == "encrypted"


def test_compression_method_rejected():
    bz = build(list(SITE.items()), method=zipfile.ZIP_BZIP2)
    assert code_of(bz) == "compression_method"


# -------------------------------------------------------------- resources
def test_too_many_entries():
    assert code_of(site(*[("f%d.txt" % i, b"") for i in range(10)]), ZipPolicy(max_entries=5)) == "too_many_entries"


def test_entry_too_large():
    assert code_of(site(("big.bin", b"\0" * 2048)), ZipPolicy(max_entry_bytes=1024)) == "entry_too_large"


def test_total_too_large():
    files = [("p%d.bin" % i, os.urandom(800)) for i in range(5)]
    assert code_of(site(*files), ZipPolicy(max_total_bytes=3000)) == "total_too_large"


def test_compression_ratio_bomb():
    assert code_of(site(("zeros.bin", b"\0" * (4 << 20)))) == "ratio"


def test_small_highly_compressible_file_is_not_a_ratio_hit():
    assert validate_zip(site(("small.txt", b"\0" * 4000)))


def _dup_central_record(raw: bytes) -> bytes:
    """Append a second central-directory record that points at the SAME local
    header as the first one (shared data = overlapping entries)."""
    eocd = raw.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", raw[eocd + 12: eocd + 20])
    first = cd_off
    n_len, x_len, c_len = struct.unpack("<HHH", raw[first + 28: first + 34])
    rec = bytearray(raw[first: first + 46 + n_len + x_len + c_len])
    name = bytes(rec[46: 46 + n_len])
    new_name = b"Z" + name[1:]
    rec[46: 46 + n_len] = new_name
    cd = raw[cd_off: cd_off + cd_size] + bytes(rec)
    n_entries = struct.unpack("<H", raw[eocd + 10: eocd + 12])[0] + 1
    tail = bytearray(raw[eocd:])
    tail[8:10] = struct.pack("<H", n_entries)
    tail[10:12] = struct.pack("<H", n_entries)
    tail[12:16] = struct.pack("<I", len(cd))
    return raw[:cd_off] + cd + bytes(tail)


def test_overlapping_entries_rejected():
    assert code_of(build(list(SITE.items()), mutate=_dup_central_record)) == "overlap"


def _shrink_declared_size(raw: bytes) -> bytes:
    """Lie in the central directory: declare 'index.html' 5 bytes shorter."""
    eocd = raw.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", raw[eocd + 16: eocd + 20])[0]
    b = bytearray(raw)
    real = struct.unpack("<I", b[cd_off + 24: cd_off + 28])[0]
    b[cd_off + 24: cd_off + 28] = struct.pack("<I", real - 5)
    return bytes(b)


def test_lying_size_header_is_caught_by_full_pass():
    # Contract: only ZipRejected leaves validate_zip -- a raw zipfile.BadZipFile
    # escaping would be a contract break, so it is asserted, not just expected.
    try:
        validate_zip(build(list(SITE.items()), mutate=_shrink_declared_size))
        err = None
    except Exception as e:  # noqa: BLE001
        err = e
    assert isinstance(err, ZipRejected) and err.code == "corrupt_data", repr(err)


def _grow_declared_size(raw: bytes) -> bytes:
    """Lie the other way: declare 'index.html' 5 bytes LONGER than its data."""
    eocd = raw.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", raw[eocd + 16: eocd + 20])[0]
    b = bytearray(raw)
    real = struct.unpack("<I", b[cd_off + 24: cd_off + 28])[0]
    b[cd_off + 24: cd_off + 28] = struct.pack("<I", real + 5)
    return bytes(b)


def test_declared_size_larger_than_data():
    try:
        validate_zip(build(list(SITE.items()), mutate=_grow_declared_size))
        err = None
    except Exception as e:  # noqa: BLE001
        err = e
    assert isinstance(err, ZipRejected) and err.code == "size_mismatch", repr(err)


def test_empty_name():
    assert code_of(site(("", b"x"))) == "empty_name"


def test_directory_entry_with_data():
    assert code_of(site(("d/", b"payload"))) == "dir_with_data"


# ------------------------------------------------------------------- misc
def test_missing_root_index():
    assert code_of(build([("site/index.html", b"x")])) == "missing_root_index"


def test_root_index_optional():
    assert validate_zip(build([("site/index.html", b"x")]), ZipPolicy(require_root_index=False))


def test_not_a_zip():
    assert code_of(io.BytesIO(b"PK\x03\x04 definitely not a zip")) == "not_a_zip"


def test_empty_input():
    assert code_of(io.BytesIO(b"")) == "not_a_zip"


def test_extract_rejects_before_creating_anything(tmp_path):
    dest = tmp_path / "out"
    with pytest.raises(ZipRejected):
        extract_zip(site(("../evil.html", b"x")), str(dest))
    assert not dest.exists()
    assert not (tmp_path / "evil.html").exists()


def test_extract_refuses_existing_destination(tmp_path):
    with pytest.raises(FileExistsError):
        extract_zip(site(), str(tmp_path))


@pytest.mark.parametrize("bad", [0, -1, True])
def test_policy_rejects_nonpositive_limits(bad):
    with pytest.raises(ValueError):
        ZipPolicy(max_entries=bad)


def test_policy_rejects_nan_ratio():
    with pytest.raises(ValueError):
        ZipPolicy(max_ratio=float("nan"))
