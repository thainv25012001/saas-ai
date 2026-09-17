"""Pack extracted text into retrieval-sized chunks.

Splits on markdown headings first (where present), then packs sentences up
to `target_tokens` with `overlap_ratio` overlap, never cutting mid-sentence.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.rag.extract import PAGE_SEPARATOR, ExtractedDocument, ExtractedPage


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    content: str
    token_count: int
    metadata: dict[str, Any]


def _approx_tokens(text: str) -> int:
    # Deliberate approximation, not real tokenization: whitespace-split word
    # count * 1.3 tracks subword-token counts closely enough to size chunks,
    # without pulling in tiktoken just for a packing heuristic that never
    # needs to match any specific model's actual tokenizer.
    words = text.split()
    return round(len(words) * 1.3)


_HEADING_LINE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_SENTENCE_PUNCT = re.compile(r"[.!?]+")


@dataclass(frozen=True, slots=True)
class _Section:
    heading: str | None
    start: int
    end: int


def _split_sections(text: str) -> list[_Section]:
    """Cut `text` at each markdown heading line, absolute offsets, no gaps.

    A section runs from its heading (inclusive) to the next heading, so the
    heading's own text stays part of the chunk it introduces. Text before
    the first heading (or the whole document, if there is none) is its own
    section with `heading=None`.
    """
    matches = list(_HEADING_LINE.finditer(text))
    if not matches:
        return [_Section(heading=None, start=0, end=len(text))]

    sections: list[_Section] = []
    if matches[0].start() > 0:
        sections.append(_Section(heading=None, start=0, end=matches[0].start()))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(_Section(heading=match.group(2).strip(), start=match.start(), end=end))
    return sections


def _split_sentences(text: str) -> list[tuple[int, int]]:
    """Contiguous, gap-free (start, end) spans covering all of `text`.

    Not a real sentence tokenizer (no abbreviation list, no locale rules) --
    it splits after `.`/`!`/`?` (plus a trailing quote/bracket) when that is
    followed by whitespace or end-of-string, and otherwise treats a run as
    part of the same sentence (so "3.14" or "Dr." mid-clause do not split).
    Trailing whitespace is folded into the end of the sentence it follows so
    the next span never starts with a leading space.
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    pos = 0
    n = len(text)
    while pos < n:
        match = _SENTENCE_PUNCT.search(text, pos)
        if not match:
            break
        end = match.end()
        while end < n and text[end] in "\"')]":
            end += 1
        if end < n and not text[end].isspace():
            pos = end
            continue
        while end < n and text[end].isspace():
            end += 1
        spans.append((start, end))
        start = end
        pos = end
    if start < n:
        spans.append((start, n))
    return spans


def _pack_sentences(
    text: str,
    sentences: list[tuple[int, int]],
    target_tokens: int,
    overlap_ratio: float,
) -> list[tuple[int, int]]:
    """Greedily group sentence spans into (start, end) chunk spans.

    A chunk always takes at least one sentence, even if that single sentence
    already exceeds `target_tokens` -- the alternative (splitting it anyway)
    would break the "never split mid-sentence" rule, and skipping it would
    silently drop text. The next chunk then backs up over the current
    chunk's trailing sentences until it has ~`overlap_ratio * target_tokens`
    worth of overlap, but never backs up past the start of the chunk that
    was just closed, which is what guarantees forward progress even when a
    single oversized sentence fills a whole chunk by itself.
    """
    spans: list[tuple[int, int]] = []
    n = len(sentences)
    idx = 0
    while idx < n:
        start = sentences[idx][0]
        end = sentences[idx][1]
        j = idx
        while j < n:
            candidate_end = sentences[j][1]
            if j == idx or _approx_tokens(text[start:candidate_end]) <= target_tokens:
                end = candidate_end
                j += 1
            else:
                break
        spans.append((start, end))
        if j >= n:
            break

        overlap_target = overlap_ratio * target_tokens
        back = j - 1
        while back > idx and _approx_tokens(text[sentences[back][0] : end]) < overlap_target:
            back -= 1
        idx = back if back > idx else j
    return spans


def _page_for_offset(pages: list[ExtractedPage], offset: int) -> int | None:
    """Which page (1-based) an absolute offset into the joined text falls
    in, walking the same `PAGE_SEPARATOR`-joined layout `extract()` built."""
    if not pages:
        return None
    cursor = 0
    for page in pages:
        end = cursor + len(page.text)
        if offset <= end:
            return page.number
        cursor = end + len(PAGE_SEPARATOR)
    return pages[-1].number


def chunk_document(
    doc: ExtractedDocument,
    *,
    target_tokens: int = 500,
    overlap_ratio: float = 0.15,
) -> list[Chunk]:
    text = doc.text
    if not text.strip():
        return []

    chunks: list[Chunk] = []
    for section in _split_sections(text):
        section_text = text[section.start : section.end]
        if not section_text.strip():
            continue
        sentences = _split_sentences(section_text)
        for rel_start, rel_end in _pack_sentences(
            section_text, sentences, target_tokens, overlap_ratio
        ):
            start = section.start + rel_start
            end = section.start + rel_end
            content = text[start:end]
            metadata: dict[str, Any] = {"char_start": start, "char_end": end}
            page = _page_for_offset(doc.pages, start)
            if page is not None:
                metadata["page"] = page
            if section.heading is not None:
                metadata["heading"] = section.heading
            chunks.append(
                Chunk(
                    index=len(chunks),
                    content=content,
                    token_count=_approx_tokens(content),
                    metadata=metadata,
                )
            )
    return chunks
