"""R5 differential falsification for security and format gates."""

import io
import math
import os
import stat
import zipfile

import pytest

from safeforge import ObjectStore, ZipPolicy, validate_zip
from safeforge.db import connect
from safeforge.events import ensure_schema, process_event


def test_event_rejects_nonfinite_effect_and_rolls_back(tmp_path):
    con = connect(str(tmp_path / "test.db"))
    ensure_schema(con)
    with pytest.raises(ValueError):
        process_event(con, "bad_nonfinite", b"body", lambda c: {"v": math.nan})
    assert con.execute("SELECT count(*) FROM processed_events").fetchone()[0] == 0
    con.close()


def test_event_rejects_nonfinite_corrupt_replay(tmp_path):
    con = connect(str(tmp_path / "test.db"))
    ensure_schema(con)
    process_event(con, "good", b"body", lambda c: {"valid": True})
    con.execute("UPDATE processed_events SET result_json='{\"v\": NaN}' WHERE event_id='good'")
    with pytest.raises(ValueError):
        process_event(con, "good", b"body", lambda c: {"unused": 0})
    con.close()


def test_zip_require_index_is_boolean():
    ZipPolicy(require_root_index=True)
    with pytest.raises(ValueError, match="bool"):
        ZipPolicy(require_root_index=1)


def test_zip_empty_member_strict_ratio(tmp_path):
    z = tmp_path / "empty.zip"
    with zipfile.ZipFile(z, "w") as out:
        out.writestr("index.html", b"")
    assert [v.name for v in validate_zip(z, ZipPolicy(ratio_min_bytes=0))] == ["index.html"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions gate")
def test_private_storage_gate_rejects_world_accessible_root(tmp_path):
    r = tmp_path / "public"
    r.mkdir(mode=0o755)
    r.chmod(0o755)
    with pytest.raises(PermissionError):
        ObjectStore(str(r), enforce_private=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions gate")
def test_private_storage_gate_rejects_symlinked_root(tmp_path):
    r = tmp_path / "good"
    r.mkdir(mode=0o700)
    (tmp_path / "rootlink").symlink_to(r, target_is_directory=True)
    with pytest.raises(PermissionError):
        ObjectStore(str(tmp_path / "rootlink"), enforce_private=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions gate")
def test_private_storage_gate_roundtrip_and_permissions(tmp_path):
    r = tmp_path / "new"
    with ObjectStore(str(r), enforce_private=True) as store:
        v = store.ingest(io.BytesIO(b"secret"), max_bytes=40)
        assert store.verify() == []
        for path in (r, r / "objects", r / "staging"):
            assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0
        for path in (r / "safeforge.db", r / "objects" / v.object_id):
            assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions gate")
def test_private_storage_rejects_world_readable_existing_database(tmp_path):
    r = tmp_path / "root"
    r.mkdir(mode=0o700)
    db = r / "safeforge.db"
    db.touch(mode=0o600)
    db.chmod(0o644)
    with pytest.raises(PermissionError):
        ObjectStore(str(r), enforce_private=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions gate")
def test_private_root_failure_has_no_child_side_effect(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        ObjectStore(str(root), enforce_private=True)
    assert not (root / "objects").exists() and not (root / "staging").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions gate")
def test_private_stage_must_reside_under_root(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="staging"):
        ObjectStore(str(root), staging_dir=str(external), enforce_private=True)
