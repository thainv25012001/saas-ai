"""`_citation_payload` maps a retrieved chunk to the SSE-facing
`CitationPayload` -- in particular, it must NOT put the full chunk `content`
on the wire (see the brief: that would double the bytes of every grounded
turn). The truncation itself has no coverage anywhere else: the integration
SSE test's own corpus is too short to prove it (its comment says so), so
this is the only test in the suite that would catch `excerpt=chunk.content`
sneaking back in.
"""

import uuid

from app.chat.service import _EXCERPT_MAX_CHARS, _citation_payload
from app.rag.retrieve import RetrievedChunk


def _chunk(content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="Doc",
        content=content,
        score=0.5,
        rank=1,
        page=None,
    )


def test_a_short_chunk_is_carried_through_unchanged() -> None:
    payload = _citation_payload(_chunk("short passage text"))
    assert payload.excerpt == "short passage text"


def test_a_long_chunk_is_truncated_with_a_trailing_ellipsis() -> None:
    long_content = "word " * 100  # comfortably longer than _EXCERPT_MAX_CHARS
    assert len(long_content) > _EXCERPT_MAX_CHARS

    payload = _citation_payload(_chunk(long_content))

    assert payload.excerpt != long_content
    assert payload.excerpt.endswith("...")
    # Bounded, not merely shorter -- a version that truncated to some other
    # arbitrary length would still pass a bare "is it shorter" check.
    assert len(payload.excerpt) <= _EXCERPT_MAX_CHARS + len("...")
