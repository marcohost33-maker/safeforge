import io
import os
import hashlib
import pytest
from safeforge import ObjectStore, IngestError


def test_verified_read_returns_original_data_on_stable_inode(tmp_path):
    data=os.urandom(4096)
    with ObjectStore(str(tmp_path)) as st:
        r=st.ingest(io.BytesIO(data),max_bytes=len(data))
        with st.open_verified(r.object_id) as f:
            assert f.read()==data
        assert st.verify()==[]


def test_verified_read_rejects_same_length_tamper(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        r=st.ingest(io.BytesIO(b'good'),max_bytes=4)
        with open(st.path_of(r.object_id),'wb') as f:f.write(b'evil')
        with pytest.raises(IngestError):st.open_verified(r.object_id)
        assert st.verify()==[r.object_id]


def test_verified_read_rejects_symlink(tmp_path):
    if not hasattr(os,'symlink'):pytest.skip('no symlink support')
    with ObjectStore(str(tmp_path)) as st:
        r=st.ingest(io.BytesIO(b'good'),max_bytes=4)
        path=st.path_of(r.object_id)
        dest=tmp_path/'external';dest.write_bytes(b'good')
        os.unlink(path)
        try:os.symlink(dest,path)
        except (OSError,NotImplementedError):pytest.skip('cannot create symlinks')
        with pytest.raises(IngestError):st.open_verified(r.object_id)
        assert dest.read_bytes()==b'good'


def test_open_verified_no_path_of_missing(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        with pytest.raises(KeyError):st.open_verified('0'*32)
        with pytest.raises(KeyError):st.open_verified('../x')


def test_path_swap_during_open_fails_closed(monkeypatch, tmp_path):
    import safeforge.store as module
    with ObjectStore(str(tmp_path)) as st:
        r=st.ingest(io.BytesIO(b'good'),max_bytes=4)
        path=st.path_of(r.object_id)
        ext=tmp_path/'outside';ext.write_bytes(b'good')
        real_open=os.open
        swapped=False
        def attack(p, flags, *a, **kw):
            nonlocal swapped
            if str(p)==path and not swapped:
                swapped=True
                os.unlink(path)
                os.symlink(ext,path)
            return real_open(p,flags,*a,**kw)
        if not hasattr(os,'O_NOFOLLOW'):pytest.skip('requires O_NOFOLLOW')
        monkeypatch.setattr(module.os,'open',attack)
        with pytest.raises(IngestError): st.open_verified(r.object_id)
        assert swapped
        assert ext.read_bytes()==b'good'
