"""R8 test gates: POSIX cooperative fence and Linux atomic restore publication."""
from __future__ import annotations

import io
import os
import sqlite3
import sys

import pytest

from safeforge import ObjectStore, IngestError
from safeforge.coordination import CoordinationRejected, initialize, lock, event_writer
from safeforge.offline_backup import backup_coordinated, backup_offline, BackupRejected, verify_backup
from safeforge.atomic_restore import restore_atomic
from safeforge import offline_backup
from safeforge.events import ensure_schema, process_event

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='POSIX Linux R8 features')


def _store(tmp_path):
    root = tmp_path / 'src'
    with ObjectStore(str(root), enforce_private=True) as store:
        oid = store.ingest(io.BytesIO(b'object\0payload'), max_bytes=100).object_id
    initialize(root, externally_quiescent=True)
    return root, oid


def test_initialization_needs_quiescence_and_private_directory(tmp_path):
    root = tmp_path/'root';root.mkdir(mode=0o700)
    with pytest.raises(CoordinationRejected, match='external quiescence'):
        initialize(root)
    root.chmod(0o755)
    with pytest.raises(CoordinationRejected, match='owned and mode'):
        initialize(root, externally_quiescent=True)
    root.chmod(0o700)
    initialize(root, externally_quiescent=True)
    assert (root/'.safeforge-coordinated-v1').is_file()


def test_uncoordinated_objectstore_and_connect_rejected(tmp_path):
    from safeforge import connect, ConfigurationError
    root, _ = _store(tmp_path)
    with pytest.raises(IngestError, match='coordination mode mismatch'):
        ObjectStore(str(root))
    with pytest.raises(ConfigurationError, match='coordinated store'):
        connect(str(root/'safeforge.db'))


def test_coordinated_backup_denied_while_objectstore_open(tmp_path):
    root, _ = _store(tmp_path)
    with ObjectStore(str(root), coordinated=True) as store:
        with pytest.raises(CoordinationRejected, match='busy'):
            backup_coordinated(root, tmp_path/'inflight')
        store.ingest(io.BytesIO(b'new'), max_bytes=10)
    report=backup_coordinated(root, tmp_path/'done')
    assert report['cooperative_writer_fence']=='held_during_snapshot'
    assert verify_backup(tmp_path/'done')['stored_objects']==2
    assert not (tmp_path/'inflight').exists()


def test_event_writes_obey_fence(tmp_path):
    root, _=_store(tmp_path)
    with event_writer(root) as con:
        ensure_schema(con)
        process_event(con,'evt_r8',b'body',lambda c:{'ok': True})
        with pytest.raises(CoordinationRejected,match='busy'):
            backup_coordinated(root,tmp_path/'blocked')
    assert backup_coordinated(root,tmp_path/'archive')['stored_objects']==1
    con = sqlite3.connect(tmp_path/'archive'/'database.sqlite3')
    try:
        assert con.execute('SELECT event_id FROM processed_events').fetchone()[0]=='evt_r8'
    finally: con.close()


def test_exclusive_lock_blocks_shared_writer(tmp_path):
    root,_=_store(tmp_path)
    with lock(root, exclusive=True):
        with pytest.raises(CoordinationRejected, match='busy'):
            ObjectStore(str(root),coordinated=True)
        with pytest.raises(CoordinationRejected,match='busy'):
            with event_writer(root): pass
    with ObjectStore(str(root), coordinated=True) as store:
        assert store.verify()==[]


def test_raw_sqlite_write_can_bypass_advisory_lock_explicit_limitation(tmp_path):
    root,_=_store(tmp_path)
    with lock(root,exclusive=True):
        # Negative control: do not claim mandatory protection against raw SQLite.
        raw=sqlite3.connect(root/'safeforge.db')
        try:
            raw.execute('UPDATE objects SET updated_at=updated_at+1')
            raw.commit()
        finally: raw.close()


def _archive(tmp_path):
    root, oid=_store(tmp_path)
    archive=tmp_path/'archive'
    backup_coordinated(root,archive)
    return root, oid, archive


def test_atomic_restore_is_usable_only_after_no_replace_publication(tmp_path):
    _, oid, archive=_archive(tmp_path)
    target=tmp_path/'result'
    r=restore_atomic(archive,target,confirmed_quiescent=True)
    assert r['publication']=='renameat2_RENAME_NOREPLACE'
    with ObjectStore(str(target)) as s:
        assert s.verify()==[]
        assert s.status_of(oid)=='stored'
    assert sorted(p.name for p in tmp_path.iterdir() if '.restore-' in p.name)==[]


def test_atomic_restore_collision_preserves_original(tmp_path):
    _,_,archive=_archive(tmp_path)
    target=tmp_path/'existing'
    target.mkdir();(target/'keep').write_text('DO NOT TOUCH')
    with pytest.raises(FileExistsError):
        restore_atomic(archive,target,confirmed_quiescent=True)
    assert (target/'keep').read_text()=='DO NOT TOUCH'


def test_racing_collision_does_not_clobber(tmp_path, monkeypatch):
    from safeforge import atomic_restore as a
    _,_,archive=_archive(tmp_path)
    target=tmp_path/'raced'
    original=a._publish_new_directory
    def race(stage,dst):
        dst.mkdir();(dst/'keep').write_text('existing')
        return original(stage,dst)
    monkeypatch.setattr(a,'_publish_new_directory',race)
    with pytest.raises(FileExistsError):
        restore_atomic(archive,target,confirmed_quiescent=True)
    assert (target/'keep').read_text()=='existing'
    assert not list(tmp_path.glob('.raced.restore-*'))


def test_atomic_restore_failure_before_publication_never_exposes_target(tmp_path, monkeypatch):
    from safeforge import atomic_restore as a
    _,_,archive=_archive(tmp_path)
    target=tmp_path/'failed'
    def fail(stage,dst):
        assert stage.exists()
        assert not dst.exists()
        raise OSError('injected prepublication failure')
    monkeypatch.setattr(a,'_publish_new_directory',fail)
    with pytest.raises(OSError,match='injected'):
        restore_atomic(archive,target,confirmed_quiescent=True)
    assert not target.exists()
    assert not list(tmp_path.glob('.failed.restore-*'))


def test_atomic_restore_refuses_missing_quiescence(tmp_path):
    _,_,archive=_archive(tmp_path)
    with pytest.raises(BackupRejected,match='quiescence'):
        restore_atomic(archive,tmp_path/'dest')


def test_coordinated_backup_refuses_missing_mode(tmp_path):
    root=tmp_path/'src'
    with ObjectStore(str(root)) as s:
        s.ingest(io.BytesIO(b'a'),max_bytes=5)
    with pytest.raises(CoordinationRejected,match='not initialized'):
        backup_coordinated(root,tmp_path/'dest')


def test_cross_process_writer_fence(tmp_path):
    import subprocess
    import sys
    root,_=_store(tmp_path)
    script = '''import sys
from safeforge.coordination import lock, CoordinationRejected
try:
    with lock(sys.argv[1], exclusive=True):
        print('LOCKED')
except CoordinationRejected:
    print('BUSY')
'''
    env=dict(os.environ, PYTHONPATH=str((__import__('pathlib').Path(__file__).resolve().parents[1] / 'src')))
    def child():
        return subprocess.run([sys.executable,'-c',script,str(root)], env=env,
                              capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    with ObjectStore(str(root),coordinated=True):
        assert child() == 'BUSY'
    assert child() == 'LOCKED'


def test_failed_coordinated_open_releases_shared_lock(tmp_path):
    root,_=_store(tmp_path)
    dbfile=root/'safeforge.db'
    dbfile.write_bytes(b'invalid sqlite header and pages')
    with pytest.raises(sqlite3.DatabaseError):
        ObjectStore(str(root),coordinated=True)
    # If constructor leaked its lock, this exclusive acquisition would fail.
    with lock(root,exclusive=True): pass


def test_process_crash_before_atomic_publish_keeps_final_path_absent(tmp_path):
    import subprocess
    _,_,archive=_archive(tmp_path)
    target=tmp_path/'crashed'
    code='''import os,sys
from safeforge import atomic_restore as a
def crash(_src,_dst): os._exit(86)
a._publish_new_directory=crash
a.restore_atomic(sys.argv[1],sys.argv[2],confirmed_quiescent=True)
'''
    env=dict(os.environ,PYTHONPATH=str((__import__('pathlib').Path(__file__).resolve().parents[1]/'src')))
    p=subprocess.run([sys.executable,'-c',code,str(archive),str(target)], env=env, timeout=25)
    assert p.returncode==86
    assert not target.exists()
    orphan=list(tmp_path.glob('.crashed.restore-*'))
    assert len(orphan)==1
    assert (orphan[0].stat().st_mode & 0o077)==0
    # Crash leaves private debris to clean up administratively; no implicit overwrite.
    import shutil
    shutil.rmtree(orphan[0])
    assert restore_atomic(archive,target,confirmed_quiescent=True)['stored_objects']==1
