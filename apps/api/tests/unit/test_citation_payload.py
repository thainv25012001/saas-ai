"""`build_citation` maps a retrieved chunk to the SSE- and tool-facing
`CitationPayload` -- in particular, it must NOT put the full chunk `content`
on the wire (see the brief: that would double the bytes of every grounded
turn). The truncation itself has no coverage anywhere else: the integration
SSE test's own corpus is too short to prove it (its comment says so), so
this is the only test in the suite that would catch `excerpt=chunk.content`
sneaking back in.

Task 7 note: this module used to test `app.chat.service._citation_payload`,
a private re-implementation of this exact mapping that `ChatService`
carried from before retrieval became a tool. Task 7 deletes that
duplicate -- `RetrieveKnowledgeTool` (and therefore every citation
`ChatService` now ever sees) already builds `CitationPayload` via
`build_citation`, the one place this mapping is defined -- so this module
is retargeted at that single surviving implementation rather than removed:
the truncation behaviour it protects did not go away, only where it lives.
"""

import uuid

from app.rag.retrieve import _EXCERPT_MAX_CHARS, RetrievedChunk, build_citation


def _chunk(content: str, page: int | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="Doc",
        content=content,
        score=0.5,
        rank=1,
        page=page,
    )


def test_a_short_chunk_is_carried_through_unchanged() -> None:
    payload = build_citation(_chunk("short passage text"))
    assert payload.excerpt == "short passage text"


def test_a_long_chunk_is_truncated_with_a_trailing_ellipsis() -> None:
    long_content = "word " * 100  # comfortably longer than _EXCERPT_MAX_CHARS
    assert len(long_content) > _EXCERPT_MAX_CHARS

    payload = build_citation(_chunk(long_content))

    assert payload.excerpt != long_content
    assert payload.excerpt.endswith("...")
    # Bounded, not merely shorter -- a version that truncated to some other
    # arbitrary length would still pass a bare "is it shorter" check.
    assert len(payload.excerpt) <= _EXCERPT_MAX_CHARS + len("...")


def test_a_pdf_sourced_chunk_carries_its_page() -> None:
    # `docs/PHASE-3.md` §3 justifies preserving PDF page offsets through
    # extraction specifically "so citations can name a page" -- this is
    # that promise reaching the SSE-facing payload.
    payload = build_citation(_chunk("passage from page 3", page=3))
    assert payload.page == 3


def test_a_non_paginated_chunk_carries_no_page() -> None:
    # Plain text/Markdown/HTML have no page concept -- `RetrievedChunk.page`
    # is `None` for them (see `_page_for_offset` in `app/rag/chunk.py`), and
    # that must survive as `None` here rather than being coerced to some
    # placeholder like `1`.
    payload = build_citation(_chunk("passage with no page", page=None))
    assert payload.page is None
