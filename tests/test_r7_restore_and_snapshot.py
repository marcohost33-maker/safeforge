"""R7: discriminating negatives for R6 offline snapshot and restore protocol."""
import io
import os
import sqlite3
from pathlib import Path
import pytest
from safeforge import ObjectStore, IngestError
from safeforge import offline_backup as m


def _source(tmp_path):
    root=tmp_path/'store'
    with ObjectStore(str(root)) as store:
        ids = [store.ingest(io.BytesIO(bytes([j])*4096), max_bytes=5000).object_id for j in (1,2)]
    return root, sorted(ids)


def test_source_mutated_after_earlier_copy_is_detected(tmp_path, monkeypatch):
    root, ids = _source(tmp_path)
    real=m._file_copy_verified
    calls=[]
    def mutate_after_last_copy(src,dst,sha,size):
        real(src,dst,sha,size)
        if src.parent == root/'objects':
            calls.append(src.name)
            if len(calls)==2:
                (root/'objects'/ids[0]).write_bytes(b'Z'*4096)
    monkeypatch.setattr(m,'_file_copy_verified',mutate_after_last_copy)
    backup=tmp_path/'backup'
    with pytest.raises(m.BackupRejected,match='source object changed'):
        m.backup_offline(root,backup,confirmed_quiescent=True)
    assert not (backup/'COMPLETE').exists()


def test_failed_restore_cannot_be_opened(tmp_path, monkeypatch):
    root,ids=_source(tmp_path)
    archive=tmp_path/'backup';m.backup_offline(root,archive,confirmed_quiescent=True)
    real=m._file_copy_verified
    def fail_on_first_obj(src,dst,sha,size):
        if src.parent.name=='objects':
            raise OSError('injected disk failure')
        return real(src,dst,sha,size)
    monkeypatch.setattr(m,'_file_copy_verified',fail_on_first_obj)
    target=tmp_path/'restored'
    with pytest.raises(OSError,match='injected'):
        m.restore_offline(archive,target,confirmed_quiescent=True)
    assert (target/'RESTORE_IN_PROGRESS').exists()
    with pytest.raises(IngestError,match='incomplete'):
        ObjectStore(str(target))


def test_successful_restore_has_no_in_progress_marker(tmp_path):
    root, ids=_source(tmp_path)
    archive=tmp_path/'backup';m.backup_offline(root,archive,confirmed_quiescent=True)
    target=tmp_path/'restored'
    assert m.restore_offline(archive,target,confirmed_quiescent=True)['stored_objects']==2
    assert not (target/'RESTORE_IN_PROGRESS').exists()
    with ObjectStore(str(target)) as store:
        assert store.verify()==[]


def test_restore_uses_the_verified_manifest_not_second_unchecked_read(tmp_path, monkeypatch):
    root,ids=_source(tmp_path)
    archive=tmp_path/'backup';m.backup_offline(root,archive,confirmed_quiescent=True)
    real=m._verify_backup_and_manifest
    def tamper_later(directory):
        answer=real(directory)
        (archive/'manifest.json').write_text('invalid replacement document')
        return answer
    monkeypatch.setattr(m,'_verify_backup_and_manifest',tamper_later)
    target=tmp_path/'restored'
    # A manifest changed after validation MUST invalidate restore and keep
    # the incomplete-restore marker in place.
    with pytest.raises(m.BackupRejected, match='manifest or COMPLETE changed'):
        m.restore_offline(archive,target,confirmed_quiescent=True)
    assert (target/'RESTORE_IN_PROGRESS').exists()
    with pytest.raises(IngestError,match='incomplete'):
        ObjectStore(str(target))
