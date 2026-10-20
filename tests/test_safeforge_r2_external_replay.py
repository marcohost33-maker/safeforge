"""New adversarial regression cases for pinned SafeForge main + local v2 patch.
All mutations occur within temporary test dirs; no actual user files affected.
"""
from __future__ import annotations

import io
import os
import pathlib
import time

import pytest

from safeforge import ObjectStore, _faults, durable
from tests import invariants as inv
from tests.conftest import run_crash
from tests.crash_driver import CRASH_EXIT


@pytest.mark.skipif(os.name=='nt',reason='POSIX exclusive link scenario')
def test_crash_after_link_before_unlink_rolls_forward_and_cleans_staging(tmp_path):
    rc, ack, err=run_crash('corrected',str(tmp_path),'object.linked')
    assert rc==CRASH_EXIT,err
    assert ack is None
    with ObjectStore(str(tmp_path)) as store:
        assert len(os.listdir(store.staging_dir))==1
        assert len(os.listdir(store.objects_dir))==1
        rep=store.reconcile(grace_seconds=0,now=time.time()+100)
        assert rep.rolled_forward==1
        assert rep.deleted_staging==1
        assert store.reconcile(grace_seconds=0,now=time.time()+100).changes==0
    assert inv.violations(inv.corrected(str(tmp_path)))==[]

@pytest.mark.skipif(os.name=='nt',reason='POSIX exclusive link scenario')
def test_exception_from_link_failpoint_does_not_ack_or_lose_external_content(tmp_path):
    marker=tmp_path/'external'
    marker.write_bytes(b'external-kept')
    def crash_hook(name):
        if name=='object.linked':
            raise RuntimeError('injected error between link/unlink')
    with ObjectStore(str(tmp_path/'store')) as store:
        _faults.set_hook(crash_hook)
        try:
            with pytest.raises(RuntimeError,match='injected'):
                store.ingest(io.BytesIO(b'private'),max_bytes=100)
        finally:
            _faults.set_hook(None)
        store.reconcile(grace_seconds=0,now=time.time()+100)
        assert store.verify()==[]
    assert inv.violations(inv.corrected(str(tmp_path/'store')))==[]
    assert marker.read_bytes()==b'external-kept'

@pytest.mark.parametrize('now',[float('nan'),float('inf'),-float('inf'),True,'now',object()])
def test_reconcile_rejects_bad_clock_values(tmp_path,now):
    with ObjectStore(str(tmp_path)) as store:
        with pytest.raises(ValueError,match='finite numeric timestamp'):
            store.reconcile(now=now)

@pytest.mark.skipif(os.name=='nt' or not hasattr(os,'symlink'),reason='POSIX symlink conditions')
def test_foreign_symlink_is_not_a_stored_object(tmp_path):
    external=tmp_path/'keep-out'
    external.write_bytes(b'known-content')
    with ObjectStore(str(tmp_path/'store')) as store:
        result=store.ingest(io.BytesIO(b'good-content'),max_bytes=100)
        internal=pathlib.Path(store.objects_dir)/result.object_id
        internal.unlink()
        os.symlink(external,internal)
        with pytest.raises(KeyError):
            store.path_of(result.object_id)
        assert store.verify()==[result.object_id]
        rep=store.reconcile(grace_seconds=0,now=time.time()+100)
        assert result.object_id in rep.missing_or_corrupt
        assert str(internal) in rep.foreign
        assert internal.is_symlink()
    assert external.read_bytes()==b'known-content'

@pytest.mark.skipif(os.name=='nt',reason='POSIX no-clobber property')
# Explicit ids: the default id for the 100000-byte value is ~400000 chars, which
# exceeds the Windows 32767-char limit for PYTEST_CURRENT_TEST (teardown ERROR).
@pytest.mark.parametrize('data',[b'',b'payload'*4096,b'\x00'*100000],ids=['empty','payload-28672B','zeros-100000B'])
def test_noreplace_exclusive_name_property(tmp_path,data):
    a,b=tmp_path/'src',tmp_path/'dst'
    a.write_bytes(data)
    b.write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        durable.publish(str(a),str(b))
    assert a.read_bytes()==data
    assert b.read_bytes()==b'existing'
