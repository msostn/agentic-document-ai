"""Pure PDF text extraction logic. No database or HTTP awareness."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

MIN_EXTRACTABLE_CHARS = 50


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str


@dataclass(frozen=True)
class ExtractionResult:
    pages: list[PageText]
    total_chars: int


class PDFParsingError(Exception):
    """Raised when PyMuPDF cannot open/parse the file at all (corrupted, invalid)."""


class NoExtractableTextError(Exception):
    """Raised when the PDF opens but contains no usable text (scanned/image-only)."""


def extract_text(pdf_bytes: bytes) -> ExtractionResult:
    """Extract text from a PDF given as raw bytes.

    Returns an ExtractionResult with per-page text and total character count.

    Raises:
        PDFParsingError: if the bytes cannot be opened as a valid PDF.
        NoExtractableTextError: if the PDF opens but has no extractable text
            (below MIN_EXTRACTABLE_CHARS non-whitespace characters total).
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PDFParsingError(
            "Could not parse the uploaded file as a PDF."
        ) from exc

    try:
        pages: list[PageText] = []
        for page_index in range(len(doc)):
            page = doc.load_page(page_index)
            text = page.get_text()
            pages.append(PageText(page_number=page_index + 1, text=text))

        total_chars = sum(
            len(pt.text.strip()) for pt in pages
        )

        if total_chars < MIN_EXTRACTABLE_CHARS:
            raise NoExtractableTextError(
                "No extractable text found in this document. "
                "Scanned/image-only PDFs are not supported."
            )

        return ExtractionResult(pages=pages, total_chars=total_chars)
    finally:
        doc.close()
