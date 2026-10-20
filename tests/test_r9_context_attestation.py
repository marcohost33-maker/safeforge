"""R9: a bool flag must not impersonate an active cooperative writer lock."""

import io
import os
import subprocess
import sys

import pytest

from safeforge import ConfigurationError, IngestError, ObjectStore, connect
from safeforge.coordination import MODE, CoordinationRejected, event_writer, initialize, lock

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="R9 POSIX coordination gate")


def mkroot(tmp_path):
    root = tmp_path / "source"
    with ObjectStore(str(root), enforce_private=True) as s:
        s.ingest(io.BytesIO(b"123"), max_bytes=10)
    initialize(root, externally_quiescent=True)
    return root


def test_coordinated_bool_without_context_rejected(tmp_path):
    root = mkroot(tmp_path)
    with pytest.raises(CoordinationRejected, match="live lock"):
        connect(str(root / "safeforge.db"), coordinated=True)
    with event_writer(root) as con:
        assert con.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
    with pytest.raises(CoordinationRejected, match="live lock"):
        connect(str(root / "safeforge.db"), coordinated=True)


def test_nested_lock_registry_survives_inner_close(tmp_path):
    root = mkroot(tmp_path)
    with lock(root, exclusive=False):
        with lock(root, exclusive=False):
            with connect(str(root / "safeforge.db"), coordinated=True) as con:
                assert con.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        c = connect(str(root / "safeforge.db"), coordinated=True)
        c.close()
    with pytest.raises(CoordinationRejected):
        connect(str(root / "safeforge.db"), coordinated=True)


def test_no_implicit_creation_with_coordinated_flag(tmp_path):
    root = tmp_path / "unmarked"
    root.mkdir(mode=0o700)
    with pytest.raises(ConfigurationError, match="initialized"):
        connect(str(root / "safeforge.db"), coordinated=True)
    assert not (root / "safeforge.db").exists()


@pytest.mark.parametrize("mutate", ["symlink", "contents", "permissions", "hardlink"])
def test_corrupted_marker_refused(tmp_path, mutate):
    root = mkroot(tmp_path)
    marker = root / MODE
    if mutate == "symlink":
        marker.unlink()
        marker.symlink_to(root / "target")
    elif mutate == "contents":
        marker.write_text("NOT A VALID MARKER")
    elif mutate == "permissions":
        marker.chmod(0o644)
    else:
        os.link(marker, root / "other")
    with pytest.raises(CoordinationRejected):
        with lock(root, exclusive=False):
            pass


def test_noncanonical_paths_refused(tmp_path):
    root = mkroot(tmp_path)
    with pytest.raises(IngestError, match="database inside"):
        ObjectStore(str(root), coordinated=True, db_path=str(tmp_path / "external.db"))
    with pytest.raises(IngestError, match="staging"):
        ObjectStore(str(root), coordinated=True, staging_dir=str(tmp_path / "external-staging"))
    with lock(root, exclusive=False):
        with pytest.raises(ConfigurationError, match="root/safeforge.db"):
            connect(str(root / "another.db"), coordinated=True)


def test_fork_does_not_inherit_api_capability(tmp_path):
    root = mkroot(tmp_path)
    script = """import os,sys
from safeforge import connect
from safeforge.coordination import lock, CoordinationRejected
root=sys.argv[1]
with lock(root,exclusive=False):
    pid=os.fork()
    if pid==0:
        try: connect(os.path.join(root,'safeforge.db'),coordinated=True)
        except CoordinationRejected: os._exit(0)
        os._exit(20)
    _,code=os.waitpid(pid,0)
    if os.waitstatus_to_exitcode(code)!=0: sys.exit(9)
print('FORK_CONTEXT_DENIED')
"""
    repo = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ, PYTHONPATH=os.path.join(repo, "src"))
    result = subprocess.run([sys.executable, "-c", script, str(root)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "FORK_CONTEXT_DENIED" in result.stdout


def test_coordinated_connection_escape_after_unlock_is_refused(tmp_path):
    root = mkroot(tmp_path)
    with lock(root, exclusive=False):
        con = connect(str(root / "safeforge.db"), coordinated=True)
        cur = con.cursor()
        assert con.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
    try:
        with pytest.raises(CoordinationRejected, match="live lock"):
            con.execute("UPDATE objects SET updated_at=updated_at+1")
        with pytest.raises(CoordinationRejected, match="live lock"):
            cur.execute("UPDATE objects SET updated_at=updated_at+1")
        with pytest.raises(CoordinationRejected, match="live lock"):
            con.cursor()
        with pytest.raises(CoordinationRejected, match="live lock"):
            con.commit()
    finally:
        con.close()


def test_native_baseclass_sqlite_execute_is_authorized_each_time(tmp_path):
    import sqlite3

    root = mkroot(tmp_path)
    with lock(root, exclusive=False):
        conn = connect(str(root / "safeforge.db"), coordinated=True)
        # Prepare exactly this SQL while the lock is held. With cached
        # statements and no SQL-level guard a later execution could bypass.
        sqlite3.Connection.execute(conn, "SELECT count(*) FROM objects").fetchone()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            sqlite3.Connection.execute(conn, "SELECT count(*) FROM objects").fetchone()
        with pytest.raises(sqlite3.DatabaseError):
            sqlite3.Connection.execute(conn, "UPDATE objects SET updated_at=updated_at+1")
    finally:
        conn.close()
