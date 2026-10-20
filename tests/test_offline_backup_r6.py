"""R6 offline backup positive and adversarial controls. Test artifacts are isolated."""

from __future__ import annotations

import io
import sqlite3

import pytest

from safeforge import ObjectStore
from safeforge.offline_backup import BackupRejected, backup_offline, restore_offline, verify_backup


def _stored(tmp_path):
    src = tmp_path / "store"
    with ObjectStore(str(src)) as s:
        ids = [s.ingest(io.BytesIO(b"object" + bytes([j]) * 8192), max_bytes=16384).object_id for j in range(2)]
    return src, ids


def test_backup_and_restore_bytes_and_sqlite_rows(tmp_path):
    src, ids = _stored(tmp_path)
    archive = tmp_path / "archive"
    with pytest.raises(BackupRejected, match="confirmed_quiescent"):
        backup_offline(src, archive)
    report = backup_offline(src, archive, confirmed_quiescent=True)
    assert report["stored_objects"] == 2
    assert verify_backup(archive) == report
    target = tmp_path / "restored"
    assert restore_offline(archive, target, confirmed_quiescent=True)["stored_objects"] == 2
    with ObjectStore(str(target)) as s:
        assert s.verify() == []
        assert {s.status_of(oid) for oid in ids} == {"stored"}


def test_refuse_pending_or_staging_or_extra_object(tmp_path):
    src, _ = _stored(tmp_path)
    (src / "staging" / "leftover.part").write_bytes(b"bad")
    with pytest.raises(BackupRejected, match="staging"):
        backup_offline(src, tmp_path / "b1", confirmed_quiescent=True)
    (src / "staging" / "leftover.part").unlink()
    (src / "objects" / ("0" * 32)).write_bytes(b"unexpected")
    with pytest.raises(BackupRejected, match="unknown"):
        backup_offline(src, tmp_path / "b2", confirmed_quiescent=True)
    (src / "objects" / ("0" * 32)).unlink()
    con = sqlite3.connect(src / "safeforge.db")
    # UPDATE ... LIMIT needs SQLITE_ENABLE_UPDATE_DELETE_LIMIT, which the
    # python.org Windows build lacks; a subquery is portable.
    cur = con.execute("UPDATE objects SET status='pending' WHERE id = (SELECT id FROM objects ORDER BY id LIMIT 1)")
    assert cur.rowcount == 1
    con.commit()
    con.close()
    with pytest.raises(BackupRejected, match="pending"):
        backup_offline(src, tmp_path / "b3", confirmed_quiescent=True)


def test_corruption_and_missing_marker_rejected(tmp_path):
    src, ids = _stored(tmp_path)
    backup_offline(src, tmp_path / "b", confirmed_quiescent=True)
    b = tmp_path / "b"
    (b / "COMPLETE").unlink()
    with pytest.raises(BackupRejected, match="COMPLETE"):
        verify_backup(b)
    (b / "COMPLETE").write_text("invalid\n")
    with pytest.raises(BackupRejected):
        verify_backup(b)
    # Backup cannot be restored to an existing path or be overwritten.
    with pytest.raises((BackupRejected, FileExistsError)):
        restore_offline(b, src, confirmed_quiescent=True)


def test_data_corruption_and_unexpected_content_rejected(tmp_path):
    src, ids = _stored(tmp_path)
    b = tmp_path / "b"
    backup_offline(src, b, confirmed_quiescent=True)
    target = b / "objects" / ids[0]
    payload = bytearray(target.read_bytes())
    payload[0] ^= 1
    target.write_bytes(payload)
    with pytest.raises(BackupRejected, match="hash mismatch"):
        verify_backup(b)
    target.write_bytes(bytes(payload))
    (b / "objects" / "ATTACKER").write_text("not in manifest")
    with pytest.raises(BackupRejected, match="unexpected"):
        verify_backup(b)


def test_refuse_existing_destination_or_inside_source(tmp_path):
    src, _ = _stored(tmp_path)
    with pytest.raises(BackupRejected, match="outside"):
        backup_offline(src, src / "sub", confirmed_quiescent=True)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        backup_offline(src, existing, confirmed_quiescent=True)


def test_restore_refuses_existing_target(tmp_path):
    src, _ = _stored(tmp_path)
    b = tmp_path / "b"
    backup_offline(src, b, confirmed_quiescent=True)
    with pytest.raises(FileExistsError):
        restore_offline(b, src, confirmed_quiescent=True)


def test_concurrent_writer_detected_and_no_complete_marker(tmp_path, monkeypatch):
    # Deterministic interleaving via a second SQLite connection, not a timing gamble.
    from safeforge import offline_backup as bmod

    src, _ = _stored(tmp_path)
    original = bmod._file_copy_verified
    triggered = False

    def competing_commit(source, destination, digest, size):
        nonlocal triggered
        original(source, destination, digest, size)
        if not triggered:
            triggered = True
            conn = sqlite3.connect(src / "safeforge.db")
            conn.execute("UPDATE objects SET updated_at=updated_at+1")
            conn.commit()
            conn.close()

    monkeypatch.setattr(bmod, "_file_copy_verified", competing_commit)
    archive = tmp_path / "interleaved"
    with pytest.raises(BackupRejected, match="modified"):
        backup_offline(src, archive, confirmed_quiescent=True)
    assert not (archive / "COMPLETE").exists()
    with pytest.raises(BackupRejected):
        verify_backup(archive)


def test_staging_creation_during_backup_detected(tmp_path, monkeypatch):
    from safeforge import offline_backup as bmod

    src, _ = _stored(tmp_path)
    original = bmod._file_copy_verified
    fired = False

    def race(source, destination, digest, size):
        nonlocal fired
        original(source, destination, digest, size)
        if not fired:
            fired = True
            (src / "staging" / "surprise.part").write_bytes(b"in flight")

    monkeypatch.setattr(bmod, "_file_copy_verified", race)
    archive = tmp_path / "interleaved"
    with pytest.raises(BackupRejected, match="filesystem"):
        backup_offline(src, archive, confirmed_quiescent=True)
    assert not (archive / "COMPLETE").exists()


def test_tamper_with_db_backup_fails_manifest_integrity(tmp_path):
    src, _ = _stored(tmp_path)
    archive = tmp_path / "b"
    backup_offline(src, archive, confirmed_quiescent=True)
    dbpath = archive / "database.sqlite3"
    raw = bytearray(dbpath.read_bytes())
    raw[-5] ^= 1
    dbpath.write_bytes(raw)
    with pytest.raises(BackupRejected, match="checksum"):
        verify_backup(archive)


def test_backup_includes_event_records_not_just_objects(tmp_path):
    from safeforge import connect, ensure_schema, process_event

    src, ids = _stored(tmp_path)
    con = connect(str(src / "safeforge.db"))
    ensure_schema(con)
    process_event(con, "evt_backup", b"hello", lambda c: {"ok": True})
    con.close()
    archive = tmp_path / "b"
    backup_offline(src, archive, confirmed_quiescent=True)
    target = tmp_path / "restored"
    restore_offline(archive, target, confirmed_quiescent=True)
    db = sqlite3.connect(target / "safeforge.db")
    assert db.execute("SELECT event_id FROM processed_events").fetchall() == [("evt_backup",)]
    db.close()
