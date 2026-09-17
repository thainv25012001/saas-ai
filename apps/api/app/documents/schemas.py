from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models import DocumentSourceType


class CreateDocumentInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    source_type: DocumentSourceType
    source_uri: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    checksum: str | None = None
    uploaded_by: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkInput(BaseModel):
    """One chunk to write via `DocumentService.replace_chunks`.

    No `chunk_index` field: `replace_chunks` assigns it from each input's
    position in the list, so a caller cannot desync a chunk's declared index
    from where it actually lands.
    """

    content: str
    token_count: int
    embedding: list[float]
    embedding_model: str
    metadata: dict[str, Any] = Field(default_factory=dict)
