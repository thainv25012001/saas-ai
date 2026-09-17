"""Turn uploaded document bytes into normalized text, ready for chunking.

Kept deliberately dumb: no OCR, no layout analysis, no table reconstruction.
Task 6's retrieval quality is bounded by chunk quality, not by extraction
fidelity, so the budget here goes to getting plain text out cleanly and
consistently rather than perfectly.
"""

import re
from dataclasses import dataclass, field
from io import BytesIO

from app.core.errors import AppError

# Single source of truth for "can we ingest this" -- Task 5's upload
# endpoint imports this rather than keeping its own list, so a file that
# uploads cleanly can never fail later in the worker for a mime type this
# module was never taught to handle.
SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)

# The separator `extract()` uses to join per-page text into `.text`, and the
# one `chunk.py` re-walks to map a chunk's absolute offset back to a page
# number. Shared as a constant rather than duplicated so the two modules
# can never quietly disagree about how far apart pages sit.
PAGE_SEPARATOR = "\n\n"


class UnsupportedDocumentType(AppError):
    code = "unsupported_document_type"
    status_code = 422


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    text: str
    pages: list[ExtractedPage] = field(default_factory=list)


_HYPHEN_LINEBREAK = re.compile(r"(\w)-\n(\w)")
_WHITESPACE_RUN = re.compile(r"[ \t\f\v]+")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


def _normalize(text: str) -> str:
    # Order matters: rejoin hyphenated breaks before collapsing whitespace --
    # once the newline is gone there is nothing left to tell "exam-ple" (a
    # real hyphenated word) apart from "exam-\nple" (a line-wrapped one).
    text = _HYPHEN_LINEBREAK.sub(r"\1\2", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RUN.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


def _extract_text(data: bytes) -> ExtractedDocument:
    # errors="replace" rather than a strict decode: a mis-declared charset on
    # an uploaded .txt must not crash the whole ingest job over one bad byte.
    return ExtractedDocument(text=_normalize(data.decode("utf-8", errors="replace")))


def _extract_html(data: bytes) -> ExtractedDocument:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(data.decode("utf-8", errors="replace"))
    for node in tree.css("script, style"):
        node.decompose()
    root = tree.body or tree.root
    raw = root.text(separator="\n") if root is not None else ""
    return ExtractedDocument(text=_normalize(raw))


def _extract_pdf(data: bytes) -> ExtractedDocument:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    pages = [
        ExtractedPage(number=number, text=_normalize(page.extract_text() or ""))
        for number, page in enumerate(reader.pages, start=1)
    ]
    return ExtractedDocument(text=PAGE_SEPARATOR.join(page.text for page in pages), pages=pages)


def _extract_docx(data: bytes) -> ExtractedDocument:
    from docx import Document as DocxDocument

    docx = DocxDocument(BytesIO(data))
    # Paragraphs only, no tables: python-docx does not record page breaks as
    # data (pagination is computed at render time by Word), so there is no
    # per-page structure to preserve here the way there is for PDF.
    text = "\n\n".join(paragraph.text for paragraph in docx.paragraphs)
    return ExtractedDocument(text=_normalize(text))


def extract(data: bytes, mime_type: str, filename: str | None = None) -> ExtractedDocument:
    """Extract normalized text (and, for PDF, per-page text) from raw bytes.

    Raises `UnsupportedDocumentType` for anything outside
    `SUPPORTED_MIME_TYPES` -- callers should check that set up front (Task
    5's upload validation does) rather than relying on this exception for
    control flow, but it is still raised here so a mime type that slips
    through fails loudly instead of producing garbage text.
    """
    if mime_type not in SUPPORTED_MIME_TYPES:
        suffix = f" ({filename})" if filename else ""
        raise UnsupportedDocumentType(f"unsupported document type '{mime_type}'{suffix}")
    if mime_type in ("text/plain", "text/markdown"):
        return _extract_text(data)
    if mime_type == "text/html":
        return _extract_html(data)
    if mime_type == "application/pdf":
        return _extract_pdf(data)
    return _extract_docx(data)
