"""R3 idempotency and at-rest integrity; adversarial + positive controls."""
import io
import time
import pytest
from safeforge import ObjectStore, IdempotencyConflict, TooLarge, IngestError


def test_same_key_identical_replay_is_verified(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        first=st.ingest(io.BytesIO(b"safe"),max_bytes=20,idempotency_key="k")
        second=st.ingest(io.BytesIO(b"safe"),max_bytes=20,idempotency_key="k")
        assert first.object_id == second.object_id and second.deduplicated


def test_same_key_different_bytes_without_expected_hash_must_conflict(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        first=st.ingest(io.BytesIO(b"safe"),max_bytes=20,idempotency_key="k")
        with pytest.raises(IdempotencyConflict):
            st.ingest(io.BytesIO(b"evil"),max_bytes=20,idempotency_key="k")
        assert st.verify() == []
        assert st.path_of(first.object_id)


def test_same_key_different_size_is_rejected(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        st.ingest(io.BytesIO(b"abc"),max_bytes=20,idempotency_key="k")
        with pytest.raises(IdempotencyConflict):
            st.ingest(io.BytesIO(b"abcd"),max_bytes=20,idempotency_key="k")


def test_replay_cannot_bypass_max_bytes_on_existing_row(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        st.ingest(io.BytesIO(b"abcdef"),max_bytes=20,idempotency_key="k")
        with pytest.raises(TooLarge):
            st.ingest(io.BytesIO(b"abcdef"),max_bytes=4,idempotency_key="k")


def test_replay_stream_exceeding_budget_fails(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        st.ingest(io.BytesIO(b"a"),max_bytes=50,idempotency_key="k")
        with pytest.raises(TooLarge):
            st.ingest(io.BytesIO(b"x"*51),max_bytes=50,idempotency_key="k")


def test_replay_does_not_ack_corrupted_stored_object(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        first=st.ingest(io.BytesIO(b"safe"),max_bytes=20,idempotency_key="k")
        with open(st.path_of(first.object_id),"wb") as f:
            f.write(b"evil") # same size
        with pytest.raises(IngestError):
            st.ingest(io.BytesIO(b"safe"),max_bytes=20,idempotency_key="k")
        assert st.verify() == [first.object_id]


def test_reconcile_full_hash_option_finds_same_size_corruption(tmp_path):
    with ObjectStore(str(tmp_path)) as st:
        first=st.ingest(io.BytesIO(b"abcd"),max_bytes=20)
        with open(st.path_of(first.object_id),"wb") as f:
            f.write(b"wxyz")
        assert st.reconcile().missing_or_corrupt == []  # cheap mode: known limit
        assert st.reconcile(verify_stored_digests=True).missing_or_corrupt == [first.object_id]
        assert st.verify() == [first.object_id]


@pytest.mark.parametrize("invalid",[1,None,"true"])
def test_full_hash_option_requires_bool(tmp_path,invalid):
    with ObjectStore(str(tmp_path)) as st:
        with pytest.raises(ValueError):
            st.reconcile(verify_stored_digests=invalid)
