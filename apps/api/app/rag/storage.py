"""Local filesystem storage for uploaded document bytes.

This is a placeholder, not the production design. A local directory under
`settings.upload_dir` lives on one instance's disk: it does not survive a
multi-replica deploy, because the api container that received the upload and
the worker container that later ingests it are not guaranteed to be the same
instance (or, on a redeploy, the same disk at all). Object storage (S3 or
compatible) is the production answer once there is more than one instance to
schedule work across. Swapping this module for one backed by a bucket is the
whole migration -- both keep the same `organization_id/document_id` key, so
nothing above this module needs to change.
"""

import uuid
from pathlib import Path

from app.core.config import get_settings


def _path_for(organization_id: uuid.UUID, document_id: uuid.UUID) -> Path:
    return Path(get_settings().upload_dir) / str(organization_id) / str(document_id)


def store_document_bytes(organization_id: uuid.UUID, document_id: uuid.UUID, data: bytes) -> Path:
    """Write `data` to its slot, creating the organization's directory as needed."""
    path = _path_for(organization_id, document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def load_document_bytes(organization_id: uuid.UUID, document_id: uuid.UUID) -> bytes:
    """Read back what `store_document_bytes` wrote for this key.

    Raises `FileNotFoundError` when nothing was ever stored there. Callers
    (the worker task) let that propagate rather than substituting empty
    bytes -- a missing upload should fail the ingest job loudly, not ingest
    a blank document.
    """
    return _path_for(organization_id, document_id).read_bytes()


def delete_document_bytes(organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
    """Remove the bytes `store_document_bytes` wrote for this key, if any.

    Called from `DocumentService.delete` so deleting a document actually
    deletes the uploaded plaintext, not just the row -- without this, the
    file at `{upload_dir}/{organization_id}/{document_id}` outlives the
    document that named it, indefinitely.

    `missing_ok=True`: unlike `load_document_bytes`, a missing file here is
    not a bug to surface loudly. It is the ordinary case for a document
    whose upload never got as far as writing bytes (a row created directly
    by a test, or a future creation path that fails between `create()` and
    `store_document_bytes()`), and it must stay ordinary for a second
    delete of the same document, too -- the deletion this function does is
    what should be authoritative, not the file's prior existence.
    """
    _path_for(organization_id, document_id).unlink(missing_ok=True)
