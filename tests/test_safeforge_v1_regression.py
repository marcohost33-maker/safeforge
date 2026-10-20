"""Run after applying hardening in a safeforge source checkout (pip install -e .)."""
import errno
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from safeforge.durable import publish, CrossDeviceError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard-link path only; Windows unchanged")


def test_publish_nominal_same_volume(tmp_path: Path):
    src = tmp_path / 'stage'
    dst = tmp_path / 'out'
    src.write_bytes(b'correct')
    mech = publish(str(src), str(dst))
    assert dst.read_bytes() == b'correct'
    assert not src.exists()
    assert 'no-replace' in mech


def test_publish_does_not_clobber_existing_file(tmp_path: Path):
    src, dst = tmp_path / 'source', tmp_path / 'target'
    src.write_bytes(b'new')
    dst.write_bytes(b'original')
    with pytest.raises(FileExistsError):
        publish(str(src), str(dst))
    assert dst.read_bytes() == b'original'
    assert src.read_bytes() == b'new'


def test_concurrent_destination_creation_is_never_overwritten(tmp_path: Path):
    src, dst = tmp_path / 'source', tmp_path / 'target'
    src.write_bytes(b'new')
    real_link = __import__('os').link
    def competitor(s, d, *a, **kw):
        Path(d).write_bytes(b'concurrent-writer')
        return real_link(s, d, *a, **kw)
    with patch('safeforge.durable.os.link', side_effect=competitor):
        with pytest.raises(FileExistsError):
            publish(str(src), str(dst))
    assert dst.read_bytes() == b'concurrent-writer'
    assert src.read_bytes() == b'new'


def test_cross_device_fails_closed(tmp_path: Path):
    src, dst = tmp_path / 'source', tmp_path / 'target'
    src.write_bytes(b'data')
    with patch('safeforge.durable.os.link', side_effect=OSError(errno.EXDEV, 'different fs')):
        with pytest.raises(CrossDeviceError):
            publish(str(src), str(dst))
    assert src.exists() and not dst.exists()


def test_unlink_failure_leaves_reconcilable_both_names(tmp_path: Path):
    src, dst = tmp_path / 'source', tmp_path / 'target'
    src.write_bytes(b'data')
    with patch('safeforge.durable.os.unlink', side_effect=OSError(errno.EIO, 'simulated')):
        with pytest.raises(OSError):
            publish(str(src), str(dst))
    assert src.read_bytes() == dst.read_bytes() == b'data'
