from app.rag.chunk import chunk_document
from app.rag.extract import ExtractedDocument, ExtractedPage


def _doc(text: str, pages: list[ExtractedPage] | None = None) -> ExtractedDocument:
    return ExtractedDocument(text=text, pages=pages or [])


def _sentence(i: int) -> str:
    return f"This is sentence number {i} with a few extra padding words."


def test_empty_input_produces_zero_chunks():
    assert chunk_document(_doc("")) == []


def test_whitespace_only_input_produces_zero_chunks():
    assert chunk_document(_doc("    \n   ")) == []


def test_a_short_document_produces_exactly_one_chunk():
    text = "A short document with only a few words."
    chunks = chunk_document(_doc(text), target_tokens=500)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].index == 0


def test_char_offsets_of_every_chunk_reproduce_its_content():
    """The offset arithmetic that Task 7's citations depend on. Overlap and
    sentence-boundary back-off are exactly where this goes subtly wrong."""
    text = " ".join(_sentence(i) for i in range(40))
    doc = _doc(text)
    chunks = chunk_document(doc, target_tokens=50, overlap_ratio=0.2)
    assert len(chunks) > 1
    for chunk in chunks:
        start = chunk.metadata["char_start"]
        end = chunk.metadata["char_end"]
        assert doc.text[start:end] == chunk.content


def test_consecutive_chunks_overlap_by_roughly_the_requested_ratio():
    text = " ".join(_sentence(i) for i in range(40))
    chunks = chunk_document(_doc(text), target_tokens=50, overlap_ratio=0.2)
    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        # the tail of the previous chunk (a whole trailing sentence) must
        # reappear verbatim at the head of the next one.
        tail = previous.content[-20:]
        assert tail in current.content


def test_no_chunk_ends_mid_sentence():
    text = " ".join(_sentence(i) for i in range(40))
    chunks = chunk_document(_doc(text), target_tokens=50, overlap_ratio=0.2)
    for chunk in chunks[:-1]:
        assert chunk.content.rstrip()[-1] in ".!?"


def test_an_oversized_single_sentence_still_produces_one_chunk():
    """No sentence punctuation anywhere, and far longer than target_tokens.
    Must not split mid-sentence, loop forever, or emit an empty chunk."""
    text = " ".join(f"word{i}" for i in range(300))
    chunks = chunk_document(_doc(text), target_tokens=20, overlap_ratio=0.2)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].token_count > 20


def test_an_oversized_sentence_amid_normal_ones_does_not_stall_chunking():
    giant = " ".join(f"word{i}" for i in range(300)) + "."
    text = f"{_sentence(0)} {giant} {_sentence(1)} {_sentence(2)}"
    doc = _doc(text)
    chunks = chunk_document(doc, target_tokens=20, overlap_ratio=0.2)
    assert len(chunks) >= 3
    assert all(chunk.content.strip() != "" for chunk in chunks)
    assert chunks[-1].metadata["char_end"] == len(text)


def test_markdown_headings_are_carried_in_following_chunks_metadata():
    text = (
        "# Heading One\n\n"
        "Body text one. Body text one continued.\n\n"
        "# Heading Two\n\n"
        "Body text two. Body text two continued."
    )
    chunks = chunk_document(_doc(text), target_tokens=500)
    headings = {chunk.metadata.get("heading") for chunk in chunks}
    assert "Heading One" in headings
    assert "Heading Two" in headings
    for chunk in chunks:
        if "Body text one" in chunk.content:
            assert chunk.metadata["heading"] == "Heading One"
        if "Body text two" in chunk.content:
            assert chunk.metadata["heading"] == "Heading Two"


def test_content_before_the_first_heading_has_no_heading_metadata():
    text = "Preamble text with no heading above it.\n\n# Heading\n\nBody under heading."
    chunks = chunk_document(_doc(text), target_tokens=500)
    preamble = next(c for c in chunks if "Preamble" in c.content)
    assert "heading" not in preamble.metadata


def test_a_document_with_no_heading_has_no_heading_metadata():
    text = "Just plain text. No headings at all here."
    chunks = chunk_document(_doc(text), target_tokens=500)
    assert all("heading" not in chunk.metadata for chunk in chunks)


def test_page_metadata_names_the_page_a_chunk_came_from():
    from app.rag.chunk import _approx_tokens
    from app.rag.extract import PAGE_SEPARATOR

    pages = [
        ExtractedPage(number=1, text=_sentence(0)),
        ExtractedPage(number=2, text=_sentence(1)),
    ]
    text = PAGE_SEPARATOR.join(page.text for page in pages)
    # A target sized to exactly one sentence, with no overlap, forces each
    # page's single sentence into its own chunk.
    chunks = chunk_document(
        _doc(text, pages), target_tokens=_approx_tokens(pages[0].text), overlap_ratio=0.0
    )
    assert len(chunks) == 2
    assert chunks[0].metadata["page"] == 1
    assert chunks[1].metadata["page"] == 2


def test_no_page_metadata_when_pages_are_unknown():
    chunks = chunk_document(_doc("Plain text with no page information."))
    assert all("page" not in chunk.metadata for chunk in chunks)


def test_chunk_indexes_are_sequential_from_zero():
    text = " ".join(_sentence(i) for i in range(40))
    chunks = chunk_document(_doc(text), target_tokens=50, overlap_ratio=0.2)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_token_count_is_the_whitespace_heuristic():
    """Approximation, not real tokenization: whitespace tokens * 1.3."""
    text = "one two three four five"
    chunks = chunk_document(_doc(text), target_tokens=500)
    assert chunks[0].token_count == round(5 * 1.3)
