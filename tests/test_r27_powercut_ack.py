"""R27 (SF-R27-01): a logical ACK is not a power-cut-durable ACK, and the result says which one it is.

``ingest`` acknowledges once the database says ``stored``. Before R27 the result carried only
the objects/ directory status; the object file's fsync status and the staging/ directory status
were dropped, so a macOS "degraded:" file sync was invisible. ``powercut_durable`` is True only
if all three syncs were "ok" and SQLite runs synchronous FULL or EXTRA. The ACK itself is
unchanged: Windows keeps working, it just never claims power-cut durability.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from safeforge import ObjectStore, durable

DEGRADED = "degraded:darwin-F_FULLFSYNC-refused-fsync-only"


def _as_posix_ok(monkeypatch):
    monkeypatch.setattr(durable, "fsync_dir", lambda path: "ok")
    monkeypatch.setattr(durable, "fsync_file", lambda f: (f.flush(), "ok")[1])


def _ingest(tmp_path, data=b"payload", **kw):
    with ObjectStore(str(tmp_path / "store"), **kw) as s:
        return s.ingest(io.BytesIO(data), max_bytes=100)


def test_all_syncs_ok_and_full_is_powercut_durable(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)  # positive control: the flag can be True at all
    r = _ingest(tmp_path)
    assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")
    assert r.powercut_durable is True


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows statuses")
def test_windows_ack_is_logical_only(tmp_path):
    r = _ingest(tmp_path)
    assert r.dir_sync == "unsupported:windows-has-no-directory-fsync"
    assert r.powercut_durable is False


def test_degraded_file_sync_is_reported_and_not_durable(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    monkeypatch.setattr(durable, "fsync_file", lambda f: (f.flush(), DEGRADED)[1])
    r = _ingest(tmp_path)
    assert r.file_sync == DEGRADED
    assert r.powercut_durable is False


def test_degraded_staging_sync_alone_is_not_durable(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    calls = []

    def fsync_dir(path):
        calls.append(path)
        return DEGRADED if os.path.basename(os.path.normpath(path)) == "staging" else "ok"

    monkeypatch.setattr(durable, "fsync_dir", fsync_dir)
    r = _ingest(tmp_path)
    assert any(os.path.basename(os.path.normpath(p)) == "staging" for p in calls)
    assert (r.dir_sync, r.staging_sync) == ("ok", DEGRADED)
    assert r.powercut_durable is False


def test_synchronous_normal_is_not_durable(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    r = _ingest(tmp_path, synchronous="NORMAL")
    assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")
    assert r.powercut_durable is False


def test_deduplicated_result_attests_nothing(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    with ObjectStore(str(tmp_path / "store")) as s:
        first = s.ingest(io.BytesIO(b"same"), max_bytes=100, idempotency_key="k")
        again = s.ingest(io.BytesIO(b"same"), max_bytes=100, idempotency_key="k")
    assert first.powercut_durable is True
    assert again.deduplicated is True and again.powercut_durable is False
