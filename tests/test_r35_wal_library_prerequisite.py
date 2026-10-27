"""R35 (SF-R35-01, SF-R35-02): the durability flag also needs a SQLite library with the WAL-reset fix.

SF-R35-01 (P0, cross-family review of 6a39c38): ``powercut_durable`` checked syncs, layout and
the commit policy, but not the linked library. SQLite 3.7.0 through 3.51.2 can lose committed WAL
frames when two connections on one file write or checkpoint at the same instant
(sqlite.org/wal.html section 11), and a store with its reconciler already has two. The flag is
now ``durability_prerequisites_ok`` and also requires ``sqlite_runtime["fixed"] is True``; open
and ingest are not blocked (decision E2). ``powercut_durable`` stays as a read-only alias.

SF-R35-02 (P2): the backup summary of an empty status part was ``""`` -- not "ok", but falsy.
"""

from __future__ import annotations

import io

import pytest

from safeforge import IngestResult, ObjectStore, durable, sqlite_runtime
from safeforge.offline_backup import _summary
from safeforge.sqlite_runtime import sqlite_wal_reset_status

SID = "2026-01-01 00:00:00 r35"


def _as_posix_ok(monkeypatch):
    monkeypatch.setattr(durable, "fsync_dir", lambda path: "ok")
    monkeypatch.setattr(durable, "fsync_file", lambda f: (f.flush(), "ok")[1])


def _runtime(monkeypatch, status: dict):
    monkeypatch.setattr(sqlite_runtime, "loaded_sqlite_status", lambda conn=None: dict(status))


def _ingest(tmp_path):
    with ObjectStore(str(tmp_path / "store")) as s:
        r = s.ingest(io.BytesIO(b"payload"), max_bytes=100)
        return r, dict(s.sqlite_runtime)


def test_fixed_library_and_every_other_prerequisite_is_durable(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)  # positive control: the flag can be True at all
    _runtime(monkeypatch, sqlite_wal_reset_status((3, 51, 3), SID))
    r, st = _ingest(tmp_path)
    assert st["fixed"] is True
    assert r.durability_prerequisites_ok is True


@pytest.mark.parametrize("version", [(3, 51, 2), (3, 50, 4), (3, 49, 1), (3, 45, 1), (3, 52, 0)])
def test_affected_library_is_not_durable_but_still_acked(tmp_path, monkeypatch, version):
    _as_posix_ok(monkeypatch)
    _runtime(monkeypatch, sqlite_wal_reset_status(version, SID))
    r, st = _ingest(tmp_path)
    assert st["fixed"] is False
    assert (r.file_sync, r.dir_sync, r.staging_sync) == ("ok", "ok", "ok")
    assert r.deduplicated is False and len(r.sha256) == 64  # the logical ACK stands
    assert r.durability_prerequisites_ok is False


@pytest.mark.parametrize("forged", [1, "yes", "True", [True], None])
def test_truthy_non_bool_fixed_is_not_accepted(tmp_path, monkeypatch, forged):
    _as_posix_ok(monkeypatch)
    st = sqlite_wal_reset_status((3, 53, 0), SID)
    st["fixed"] = forged
    _runtime(monkeypatch, st)
    r, _ = _ingest(tmp_path)
    assert r.durability_prerequisites_ok is False


def test_missing_fixed_key_is_not_accepted(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    st = sqlite_wal_reset_status((3, 53, 0), SID)
    del st["fixed"]
    _runtime(monkeypatch, st)
    r, _ = _ingest(tmp_path)
    assert r.durability_prerequisites_ok is False


@pytest.mark.real_sqlite
def test_real_library_decides_the_flag(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    r, st = _ingest(tmp_path)
    assert st["fixed"] is sqlite_runtime.loaded_sqlite_status()["fixed"]
    assert r.durability_prerequisites_ok is st["fixed"]


def test_powercut_durable_is_a_read_only_alias(tmp_path, monkeypatch):
    _as_posix_ok(monkeypatch)
    for fixed_version, expected in (((3, 53, 0), True), ((3, 50, 4), False)):
        _runtime(monkeypatch, sqlite_wal_reset_status(fixed_version, SID))
        r, _ = _ingest(tmp_path / str(expected))
        assert r.durability_prerequisites_ok is expected
        assert r.powercut_durable is expected
    with pytest.raises(AttributeError):  # FrozenInstanceError or a setter-less property
        r.powercut_durable = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        IngestResult("x", "0" * 64, 1, False, powercut_durable=True)  # type: ignore[call-arg]


# -- SF-R35-02 -----------------------------------------------------------------


@pytest.mark.parametrize("statuses", [[""], ["ok", ""], ["ok;"], [";ok"], ["ok;;ok"]])
def test_empty_status_part_is_named_missing_and_truthy(statuses):
    s = _summary(statuses)
    assert s != "ok"
    assert s, "an empty summary reads as 'nothing to report'"
    assert "missing:empty-status" in s.split(";")


def test_well_formed_statuses_still_summarise():
    assert _summary(["ok", "ok;ok"]) == "ok"  # negative control: no false alarm
    assert _summary(["ok", "degraded:x;ok", ""]) == "degraded:x;missing:empty-status"
