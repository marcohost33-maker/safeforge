"""R4 independently observable failure paths; no stale partial extraction."""

import io
import math
import zipfile

import pytest

from safeforge import ZipPolicy, ZipRejected, extract_zip, validate_zip


def zdata():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", "<h1>ok</h1>")
        z.writestr("app.js", "safe")
    return io.BytesIO(buf.getvalue())


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan, True, "200", 0, -1])
def test_rejects_invalid_max_ratio(bad):
    with pytest.raises((TypeError, ValueError)):
        ZipPolicy(max_ratio=bad)


@pytest.mark.parametrize("bad", [math.inf, 1.0, -1, True, "0"])
def test_rejects_invalid_ratio_threshold(bad):
    with pytest.raises((TypeError, ValueError)):
        ZipPolicy(ratio_min_bytes=bad)


@pytest.mark.parametrize("bad", [(), [zipfile.ZIP_STORED], (99,), (True,), (0, 0), "0"])
def test_rejects_unsafe_methods_policy(bad):
    with pytest.raises((TypeError, ValueError)):
        ZipPolicy(allowed_methods=bad)


def test_valid_policy_and_zip_positive_control(tmp_path):
    policy = ZipPolicy(max_ratio=300, ratio_min_bytes=0, allowed_methods=(0, 8))
    assert len(validate_zip(zdata(), policy)) == 2
    p = tmp_path / "positive"
    assert len(extract_zip(zdata(), str(p), policy)) == 2
    assert (p / "app.js").read_bytes() == b"safe"


def test_second_pass_corruption_removes_destination(monkeypatch, tmp_path):
    real = zipfile.ZipFile.open
    count = 0

    def changed(self, name, *a, **kw):
        nonlocal count
        count += 1
        # two validation opens, then the first extraction open: corrupt at the boundary
        if count == 3:
            raise zipfile.BadZipFile("simulated I/O mutation")
        return real(self, name, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "open", changed)
    dest = tmp_path / "partial"
    with pytest.raises(ZipRejected, match="corrupt_data"):
        extract_zip(zdata(), str(dest))
    assert count >= 3
    assert not dest.exists(), "no partial application may be left behind"


def test_existing_destination_is_preserved(tmp_path):
    dest = tmp_path / "existing"
    dest.mkdir()
    (dest / "keep.txt").write_text("safe")
    with pytest.raises(FileExistsError):
        extract_zip(zdata(), str(dest))
    assert (dest / "keep.txt").read_text() == "safe"
