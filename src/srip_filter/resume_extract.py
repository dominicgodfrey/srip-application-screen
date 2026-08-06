"""Stage 6 resume PDF text extraction (PRD §7.2). Pure: bytes in, text out, and it never raises
— every failure is a typed reason that becomes a 0 bonus plus an audit note.

``pypdf`` over the PRD's ``pdfplumber``: a much lighter tree for text-only extraction
(documented deviation), and there is deliberately no OCR dependency, so a scanned PDF is a
typed failure. Magic bytes are checked first so a non-PDF fails cheaply, and page iteration
stops at ``resume.max_text_chars``, which bounds Task E spend and makes a 200-page upload free.

Nothing here retains or logs resume content; the caller discards the bytes on return.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

from .config import AppConfig

logger = logging.getLogger(__name__)

# Typed failure reasons (audit-facing), continuing the resume_fetch vocabulary.
FAIL_NOT_PDF = "not_a_pdf"
FAIL_PDF_ENCRYPTED = "pdf_encrypted"
FAIL_PDF_PARSE = "pdf_parse_error"
FAIL_NO_TEXT = "no_extractable_text"

# Some generators prepend junk, so search a small window rather than byte 0 only.
_MAGIC = b"%PDF-"
_MAGIC_WINDOW = 1024


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of one extraction. ``text`` is ``""`` on failure; ``failure`` is ``""`` on ok."""

    ok: bool
    text: str
    failure: str


def _ok(text: str) -> ExtractResult:
    return ExtractResult(ok=True, text=text, failure="")


def _fail(reason: str) -> ExtractResult:
    return ExtractResult(ok=False, text="", failure=reason)


def extract_resume_text(content: bytes, cfg: AppConfig) -> ExtractResult:
    """Extract plain text from PDF bytes, capped at ``resume.max_text_chars``. Never raises."""
    if _MAGIC not in content[:_MAGIC_WINDOW]:
        return _fail(FAIL_NOT_PDF)
    max_chars = cfg.resume.max_text_chars
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):  # some are "encrypted" with an empty password
                    return _fail(FAIL_PDF_ENCRYPTED)
            except Exception:
                return _fail(FAIL_PDF_ENCRYPTED)
        pieces: list[str] = []
        total = 0
        for page in reader.pages:
            page_text = (page.extract_text() or "").strip()
            if page_text:
                pieces.append(page_text)
                total += len(page_text)
            if total >= max_chars:
                break  # cap reached — later pages cost nothing
        text = "\n".join(pieces).strip()
    except Exception as error:  # boundary: any pypdf failure degrades to a typed reason
        logger.warning("resume extraction failed: %s", type(error).__name__)  # never content
        return _fail(FAIL_PDF_PARSE)
    if not text:
        return _fail(FAIL_NO_TEXT)
    return _ok(text[:max_chars])
