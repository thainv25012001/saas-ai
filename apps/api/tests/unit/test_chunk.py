from app.rag.chunk import chunk_document
from app.rag.extract import ExtractedDocument, ExtractedPage


def _doc(text: str, pages: list[ExtractedPage] | None = None) -> ExtractedDocument:
    return ExtractedDocument(text=text, pages=pages or [])


def _sentence(i: int) -> str:
    # The unique part is at the very end, right before the period: tests
    # that check "does chunk n's tail reappear in chunk n+1" need a tail
    # that could only have come from *this* sentence, not one that every
    # sentence shares (a shared suffix would make such a test pass whether
    # or not real overlap exists).
    return f"This is a padding sentence that ends with the unique marker zzq{i}."


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
    sentence-boundary back-off are exactly where this goes subtly wrong.

    Deliberately does NOT derive the expected offsets from `chunk.metadata`
    itself (that was the bug in an earlier version of this test: comparing
    `text[start:end]` against `content` when both `start`/`end` and
    `content` came from the same two variables is a tautology that no
    implementation bug could ever fail). Instead, the expected offsets are
    computed independently from the test's own knowledge of how the fixture
    text was assembled -- each sentence's exact text is unique (via its
    `zzq{i}` marker) and locatable with `str.index`, and the fixture joins
    sentences with a single space, so a chunk's end must land exactly at
    the start of the sentence following its last one (or at `len(text)` if
    it contains the final sentence).
    """
    sentences = [_sentence(i) for i in range(40)]
    text = " ".join(sentences)
    doc = _doc(text)
    # Independently located, not read back from anything chunk.py computed.
    sentence_starts = [text.index(sentences[i]) for i in range(len(sentences))]

    chunks = chunk_document(doc, target_tokens=50, overlap_ratio=0.2)
    assert len(chunks) > 1

    for chunk in chunks:
        contained = [i for i, s in enumerate(sentences) if s in chunk.content]
        assert contained, "every chunk must contain at least one whole sentence"
        first_idx, last_idx = min(contained), max(contained)
        expected_start = sentence_starts[first_idx]
        expected_end = sentence_starts[last_idx + 1] if last_idx + 1 < len(sentences) else len(text)
        assert chunk.metadata["char_start"] == expected_start
        assert chunk.metadata["char_end"] == expected_end
        assert doc.text[expected_start:expected_end] == chunk.content

    assert chunks[-1].metadata["char_end"] == len(text)
    starts = [chunk.metadata["char_start"] for chunk in chunks]
    ends = [chunk.metadata["char_end"] for chunk in chunks]
    assert all(a < b for a, b in zip(starts, starts[1:], strict=False))
    assert all(a < b for a, b in zip(ends, ends[1:], strict=False))


def test_consecutive_chunks_overlap_by_roughly_the_requested_ratio():
    """Each sentence's tail is unique (see `_sentence`), so this can only
    pass if a whole trailing sentence genuinely reappears -- see
    `test_zero_overlap_ratio_produces_no_overlap` for the falsifying half of
    this proof: the same fixture at `overlap_ratio=0.0` finds no overlap."""
    text = " ".join(_sentence(i) for i in range(40))
    chunks = chunk_document(_doc(text), target_tokens=50, overlap_ratio=0.2)
    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        # the tail of the previous chunk (a whole trailing sentence) must
        # reappear verbatim at the head of the next one.
        tail = previous.content[-30:]
        assert tail in current.content


def test_zero_overlap_ratio_produces_no_overlap():
    """The falsifying half of the overlap proof: with the exact same
    fixture as the test above, asking for zero overlap must actually
    produce none -- no previous chunk's tail sentence should reappear."""
    text = " ".join(_sentence(i) for i in range(40))
    chunks = chunk_document(_doc(text), target_tokens=50, overlap_ratio=0.0)
    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        tail = previous.content[-30:]
        assert tail not in current.content


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


def test_page_for_offset_at_the_exact_page_separator_seam():
    """Pins the seam rule documented on `_page_for_offset`: an offset that
    lands exactly one-past a page's last character (where its trailing
    `PAGE_SEPARATOR` begins) belongs to that page, not the next one; an
    offset anywhere inside the separator itself belongs to the next page."""
    from app.rag.chunk import _page_for_offset
    from app.rag.extract import PAGE_SEPARATOR

    pages = [ExtractedPage(number=1, text="AAAAA"), ExtractedPage(number=2, text="BBBBB")]
    page_one_end = len(pages[0].text)  # 5: one past "AAAAA"'s last character
    separator_interior = page_one_end + 1  # inside the "\n\n" gap
    page_two_start = page_one_end + len(PAGE_SEPARATOR)  # 7: "BBBBB" begins here

    assert _page_for_offset(pages, page_one_end) == 1
    assert _page_for_offset(pages, separator_interior) == 2
    assert _page_for_offset(pages, page_two_start) == 2


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
