"""`app/rag/storage.py`'s local-directory read/write, in isolation from the
default `./var/uploads` path (which is real, gitignored, and not something a
test should read or leave files behind in). Every test here monkeypatches
`get_settings` to point `upload_dir` at `tmp_path` instead.
"""

import uuid

import pytest

from app.core.config import get_settings
from app.rag import storage


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    isolated = get_settings().model_copy(update={"upload_dir": str(tmp_path)})
    monkeypatch.setattr(storage, "get_settings", lambda: isolated)


def test_store_then_load_round_trips_the_same_bytes():
    org_id, doc_id = uuid.uuid4(), uuid.uuid4()
    storage.store_document_bytes(org_id, doc_id, b"the quick brown fox")

    assert storage.load_document_bytes(org_id, doc_id) == b"the quick brown fox"


def test_store_creates_the_organizations_directory_as_needed():
    """The org's subdirectory does not exist yet on the very first upload for
    that organization -- `mkdir(parents=True, exist_ok=True)` is not optional
    here, it is the only thing that makes a first upload work at all."""
    org_id, doc_id = uuid.uuid4(), uuid.uuid4()
    storage.store_document_bytes(org_id, doc_id, b"data")
    assert storage.load_document_bytes(org_id, doc_id) == b"data"


def test_different_documents_do_not_collide():
    org_id = uuid.uuid4()
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    storage.store_document_bytes(org_id, doc_a, b"first")
    storage.store_document_bytes(org_id, doc_b, b"second")

    assert storage.load_document_bytes(org_id, doc_a) == b"first"
    assert storage.load_document_bytes(org_id, doc_b) == b"second"


def test_loading_a_key_nothing_was_ever_stored_under_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        storage.load_document_bytes(uuid.uuid4(), uuid.uuid4())


def test_restoring_a_document_under_the_same_key_overwrites_rather_than_appends():
    org_id, doc_id = uuid.uuid4(), uuid.uuid4()
    storage.store_document_bytes(org_id, doc_id, b"original")
    storage.store_document_bytes(org_id, doc_id, b"replacement")

    assert storage.load_document_bytes(org_id, doc_id) == b"replacement"
