"""Fail-closed offline backup/restore for the SafeForge reference object store.

IMPORTANT: The caller MUST fence all writers/reconcilers outside this library.
SQLite's Online Backup API snapshots the DB but cannot atomically snapshot
external object files. No process lease or power-loss guarantee is claimed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from . import durable
from .store import _file_matches, _open_regular

_NAME = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = "safeforge-offline-backup-v1"


class BackupRejected(RuntimeError):
    """A backup/restore invariant was not met. Never treat partial output as ready."""


def _check_confirmation(confirmed_quiescent: bool) -> None:
    if confirmed_quiescent is not True:
        raise BackupRejected("explicit confirmed_quiescent=True required; stop ALL writers and reconcilers first")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(str(path)) as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _file_copy_verified(src: Path, dst: Path, expected_sha: str, expected_size: int) -> None:
    # Hash the SOURCE descriptor while streaming, never a pathname reopened for copy.
    h = hashlib.sha256()
    size = 0
    with _open_regular(str(src)) as inp:
        with open(dst, "xb", buffering=0) as out:
            os.chmod(dst, 0o600)
            for chunk in iter(lambda: inp.read(1 << 20), b""):
                size += len(chunk)
                if size > expected_size:
                    raise BackupRejected("object exceeded expected size during copy")
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    if size != expected_size or h.hexdigest() != expected_sha:
        raise BackupRejected("object checksum or size mismatch while copying")
    if not _file_matches(str(src), expected_sha, expected_size):
        raise BackupRejected("source object changed or disappeared during copying")


def _integrity(con: sqlite3.Connection) -> None:
    check = con.execute("PRAGMA integrity_check").fetchall()
    if check != [("ok",)]:
        raise BackupRejected("SQLite integrity_check failed")


def _rows(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("SELECT id, status, sha256, size FROM objects ORDER BY id").fetchall()
    entries = []
    for oid, status, sha, size in rows:
        if (not isinstance(oid, str) or not _NAME.fullmatch(oid)
                or status not in ("stored", "failed")
                or not isinstance(sha, str) or not _HASH.fullmatch(sha)
                or type(size) is not int or size < 0):
            raise BackupRejected("invalid object metadata or unfinished pending ingest")
        if status == "stored":
            entries.append({"id": oid, "sha256": sha, "size": size})
    return entries


def _list_regular_names(path: Path) -> set[str]:
    if path.is_symlink() or not path.is_dir():
        raise BackupRejected("expected ordinary directory: " + str(path))
    result = set()
    for entry in path.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise BackupRejected("unexpected nonregular object: " + entry.name)
        result.add(entry.name)
    return result


def _write_sync(path: Path, payload: bytes) -> None:
    with open(path, "xb") as f:
        os.chmod(path, 0o600)
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def _canonical_json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def backup_offline(source_root: str | os.PathLike, destination: str | os.PathLike,
                   *, confirmed_quiescent: bool = False) -> dict:
    """Create a complete, verified *offline* archive in a new directory.

    Caller is responsible for ensuring there are NO writers/reconcilers on the
    entire source for the whole operation. Refuses pending/staged/orphan data.
    An incomplete backup has NO 'COMPLETE' marker and cannot be verified.
    """
    _check_confirmation(confirmed_quiescent)
    src, dst = Path(source_root).absolute(), Path(destination).absolute()
    if src == dst or os.path.commonpath((str(src), str(dst))) == str(src):
        raise BackupRejected("backup destination must be outside source root")
    if src.is_symlink() or not src.is_dir():
        raise BackupRejected("source root must be a real directory")
    if _list_regular_names(src / "staging"):
        raise BackupRejected("staging is not empty; refuse inconsistent offline snapshot")
    dbfile = src / "safeforge.db"
    if dbfile.is_symlink() or not dbfile.is_file():
        raise BackupRejected("expected local SQLite database")
    # Open a single observation connection: detect other-connection commits
    # via data_version. Detection does NOT substitute for external quiescence.
    read = sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        v_before = read.execute("PRAGMA data_version").fetchone()[0]
        _integrity(read)
        expected = _rows(read)
        if _list_regular_names(src / "objects") != {e["id"] for e in expected}:
            raise BackupRejected("unknown, missing or failed-row object file")
        os.mkdir(dst, 0o700)  # exclusive: will not overwrite an existing snapshot
        objects = dst / "objects"
        os.mkdir(objects, 0o700)
        # Database backup API handles WAL pages correctly (raw copy would not).
        target_db = dst / "database.sqlite3"
        with closing(sqlite3.connect(str(target_db), isolation_level=None)) as snap:
            read.backup(snap)
            _integrity(snap)
            if _rows(snap) != expected:
                raise BackupRejected("DB rows changed during snapshot")
            # A self-contained backup must not depend on a sidecar WAL file.
            mode = snap.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise BackupRejected("could not checkpoint and seal SQLite backup")
        os.chmod(target_db, 0o600)
        dbsha, dbsize = _hash_file(target_db)
        # "r+b", not "rb": on Windows os.fsync (_commit/FlushFileBuffers) needs a
        # handle with write access and fails with EBADF on a read-only one.
        # "r+b" neither creates nor truncates; POSIX accepts either mode.
        with open(target_db, "r+b") as f:
            os.fsync(f.fileno())
        for entry in expected:
            _file_copy_verified(src / "objects" / entry["id"], objects / entry["id"],
                                entry["sha256"], entry["size"])
        if read.execute("PRAGMA data_version").fetchone()[0] != v_before or _rows(read) != expected:
            raise BackupRejected("source DB modified during backup")
        if (_list_regular_names(src / "staging") or
                _list_regular_names(src / "objects") != {e["id"] for e in expected}):
            raise BackupRejected("source filesystem changed during backup")
        # R7: re-hash ALL original source files just before issuing COMPLETE.
        # R6 only rehashed immediately after each individual copy; a writer
        # could mutate an EARLIER source object while later objects were copied.
        # This remains best-effort detection, NOT a replacement for writer fencing.
        for entry in expected:
            if not _file_matches(str(src / "objects" / entry["id"]), entry["sha256"], entry["size"]):
                raise BackupRejected("source object changed during backup: " + entry["id"])
        manifest = {"schema": _SCHEMA, "database_sha256": dbsha, "database_size": dbsize,
                    "objects": expected, "writer_quiescence": "operator_attested_not_verified_by_library"}
        material = _canonical_json(manifest)
        _write_sync(dst / "manifest.json", material)
        durable.fsync_dir(str(objects))
        # COMPLETE is the LAST action; orphan/incomplete dirs never validate.
        _write_sync(dst / "COMPLETE", hashlib.sha256(material).hexdigest().encode("ascii") + b"\n")
        durable.fsync_dir(str(dst))
        durable.fsync_dir(str(dst.parent))
        return verify_backup(dst)
    finally:
        read.close()


def _verify_backup_and_manifest(directory: str | os.PathLike) -> tuple[dict, dict, bytes]:
    """Validate a backup and return the *same* manifest object that was checked."""
    root = Path(directory).absolute()
    if root.is_symlink() or not root.is_dir():
        raise BackupRejected("backup is not a real directory")
    names = {entry.name for entry in root.iterdir()}
    if names != {"manifest.json", "COMPLETE", "database.sqlite3", "objects"}:
        raise BackupRejected("missing COMPLETE or unexpected entry in backup root")
    if (root / "objects").is_symlink() or not (root / "objects").is_dir():
        raise BackupRejected("backup objects must be a real directory")
    for n in ("COMPLETE", "manifest.json", "database.sqlite3"):
        path = root / n
        if path.is_symlink() or not path.is_file():
            raise BackupRejected("missing or nonregular backup file: " + n)
    material = (root / "manifest.json").read_bytes()
    if len(material) > 10_000_000:
        raise BackupRejected("oversized manifest")
    if (root / "COMPLETE").read_bytes() != hashlib.sha256(material).hexdigest().encode("ascii") + b"\n":
        raise BackupRejected("missing/invalid COMPLETE marker")
    try:
        m = json.loads(material)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupRejected("invalid JSON manifest") from exc
    if not isinstance(m, dict) or m.get("schema") != _SCHEMA:
        raise BackupRejected("unrecognized backup schema")
    if m.get("writer_quiescence") != "operator_attested_not_verified_by_library":
        raise BackupRejected("invalid quiescence declaration")
    dbsha, dbsize = _hash_file(root / "database.sqlite3")
    if dbsha != m.get("database_sha256") or dbsize != m.get("database_size"):
        raise BackupRejected("database checksum failed")
    con = sqlite3.connect((root / "database.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        _integrity(con)
        expected = _rows(con)
    finally:
        con.close()
    if expected != m.get("objects"):
        raise BackupRejected("manifest and database disagree")
    if _list_regular_names(root / "objects") != {e["id"] for e in expected}:
        raise BackupRejected("missing, unexpected or nonregular backup object")
    for e in expected:
        if not _file_matches(str(root / "objects" / e["id"]), e["sha256"], e["size"]):
            raise BackupRejected("backup object hash mismatch: " + e["id"])
    report = {"schema": _SCHEMA, "verdict": "offline_snapshot_integrity_pass",
              "stored_objects": len(expected), "bytes": sum(e["size"] for e in expected),
              "limitations": "Requires externally fenced writers; no signature, encryption or power-cut proof"}
    return report, m, material


def verify_backup(directory: str | os.PathLike) -> dict:
    """Public read-only integrity check; not a signature/authenticity check."""
    return _verify_backup_and_manifest(directory)[0]


def restore_offline(backup: str | os.PathLike, new_root: str | os.PathLike,
                    *, confirmed_quiescent: bool = False) -> dict:
    """Restore a validated backup to a NEW root; never overwrite a live store."""
    _check_confirmation(confirmed_quiescent)
    checked, m, verified_material = _verify_backup_and_manifest(backup)
    source = Path(backup).absolute()
    target = Path(new_root).absolute()
    if os.path.commonpath((str(source), str(target))) == str(source):
        raise BackupRejected("restore root must be outside backup")
    os.mkdir(target, 0o700)
    # An incomplete restore is not a usable ObjectStore. The marker is checked
    # by ObjectStore's constructor. It is removed ONLY after verified sync.
    # NOTE: not atomic with mkdir; an untrusted concurrent process can race.
    _write_sync(target / "RESTORE_IN_PROGRESS", b"restore must finish before opening this store\n")
    os.mkdir(target / "objects", 0o700)
    os.mkdir(target / "staging", 0o700)
    try:
        # Copy ONLY the already validated manifest entries from this invocation.
        # R6 re-read the mutable manifest after validating it (TOCTOU).
        _file_copy_verified(source / "database.sqlite3", target / "safeforge.db",
                            m["database_sha256"], m["database_size"])
        for e in m["objects"]:
            _file_copy_verified(source / "objects" / e["id"], target / "objects" / e["id"],
                                e["sha256"], e["size"])
        # Verify the entire copied destination before making it startable.
        if _list_regular_names(target / "objects") != {e["id"] for e in m["objects"]}:
            raise BackupRejected("restored object set mismatch")
        for e in m["objects"]:
            if not _file_matches(str(target / "objects" / e["id"]), e["sha256"], e["size"]):
                raise BackupRejected("restored object checksum mismatch")
        with closing(sqlite3.connect((target / "safeforge.db").as_uri() + "?mode=ro", uri=True)) as con:
            _integrity(con)
            if _rows(con) != m["objects"]:
                raise BackupRejected("restored DB differs from manifest")
        # Re-check that the verified manifest and marker did not change during
        # restore. This is a tamper/race *detector*, not cryptographic authorship.
        expected_marker = hashlib.sha256(verified_material).hexdigest().encode("ascii") + b"\n"
        if ((source / "manifest.json").read_bytes() != verified_material
                or (source / "COMPLETE").read_bytes() != expected_marker):
            raise BackupRejected("backup manifest or COMPLETE changed during restore")
        for d in (target / "objects", target / "staging", target):
            durable.fsync_dir(str(d))
        os.unlink(target / "RESTORE_IN_PROGRESS")
        durable.fsync_dir(str(target))
        durable.fsync_dir(str(target.parent))
        return {**checked, "verdict": "offline_restore_copy_pass"}
    except BaseException:
        # Keep failed restore clearly incomplete: caller must discard this root.
        raise


def backup_coordinated(source_root: str | os.PathLike, destination: str | os.PathLike) -> dict:
    """Backup under EX lock against opted-in R8 ObjectStore/event_writer actors.

    No lease over raw SQLite, unmodified processes or filesystem writers.
    Do not claim this is a universal writer fence.
    """
    from .coordination import lock
    with lock(source_root, exclusive=True):
        result = backup_offline(source_root, destination, confirmed_quiescent=True)
        return {**result, "cooperative_writer_fence": "held_during_snapshot",
                "bypass_warning": "raw SQLite, legacy clients and raw filesystem writes are not fenced"}
