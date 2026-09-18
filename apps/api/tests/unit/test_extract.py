from io import BytesIO

import pytest

from app.rag.extract import (
    SUPPORTED_MIME_TYPES,
    UnsupportedDocumentType,
    extract,
)


def test_unsupported_mime_type_raises_the_specific_error():
    """Not a generic error: Task 5's upload endpoint and the ingest worker
    both need to recognise this one deliberately."""
    with pytest.raises(UnsupportedDocumentType) as excinfo:
        extract(b"whatever", "application/zip")
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "unsupported_document_type"


def test_supported_mime_types_is_a_frozenset_containing_the_five_types():
    assert SUPPORTED_MIME_TYPES == frozenset(
        {
            "text/plain",
            "text/markdown",
            "text/html",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    )


def test_plain_text_round_trips():
    doc = extract(b"Hello there.", "text/plain")
    assert doc.text == "Hello there."
    assert doc.pages == []


def test_hyphenated_line_break_is_rejoined():
    doc = extract(b"This is an exam-\nple of a rejoined word.", "text/plain")
    assert "exam-\nple" not in doc.text
    assert "example" in doc.text


def test_whitespace_runs_are_collapsed():
    doc = extract(b"Too    many     spaces", "text/plain")
    assert doc.text == "Too many spaces"


def test_leading_and_trailing_space_is_stripped():
    doc = extract(b"   padded   ", "text/plain")
    assert doc.text == "padded"


def test_markdown_is_extracted_like_plain_text():
    doc = extract(b"# Title\n\nSome body text.", "text/markdown")
    assert "# Title" in doc.text
    assert "Some body text." in doc.text


def test_html_strips_script_and_style_and_collapses_whitespace():
    html = b"""
    <html><head><style>.a { color: red; }</style></head>
    <body>
      <script>var secret = 1;</script>
      <h1>Heading</h1>
      <p>Some    paragraph   text.</p>
    </body></html>
    """
    doc = extract(html, "text/html")
    assert "color" not in doc.text
    assert "var secret" not in doc.text
    assert "Heading" in doc.text
    assert "Some paragraph text." in doc.text


def _build_pdf_bytes(page_texts: list[str]) -> bytes:
    """Build a tiny, real PDF in-memory with pypdf rather than committing a
    binary fixture. pypdf itself has no text-drawing API (that's reportlab's
    job), so each page's content stream is written by hand: it's just
    `BT ... Tj ET` -- the minimal PDF instruction to place a text string."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject

    writer = PdfWriter()
    for page_text in page_texts:
        page = writer.add_blank_page(width=200, height=200)

        content = f"BT /F1 12 Tf 20 100 Td ({page_text}) Tj ET".encode()
        stream_obj = StreamObject()
        stream_obj.set_data(content)
        stream_ref = writer._add_object(stream_obj)
        page[NameObject("/Contents")] = stream_ref

        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        font_ref = writer._add_object(font)
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font_ref
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_extracts_text_per_page():
    pdf_bytes = _build_pdf_bytes(["First page text", "Second page text"])
    doc = extract(pdf_bytes, "application/pdf")
    assert len(doc.pages) == 2
    assert doc.pages[0].number == 1
    assert "First page text" in doc.pages[0].text
    assert doc.pages[1].number == 2
    assert "Second page text" in doc.pages[1].text
    # the joined text carries both pages, in order, for chunking to split.
    assert "First page text" in doc.text
    assert "Second page text" in doc.text
    assert doc.text.index("First page text") < doc.text.index("Second page text")


def _build_docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document as DocxDocument

    docx = DocxDocument()
    for paragraph in paragraphs:
        docx.add_paragraph(paragraph)
    buf = BytesIO()
    docx.save(buf)
    return buf.getvalue()


def test_docx_extracts_paragraph_text():
    docx_bytes = _build_docx_bytes(["Hello docx world.", "Second paragraph here."])
    doc = extract(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "Hello docx world." in doc.text
    assert "Second paragraph here." in doc.text
