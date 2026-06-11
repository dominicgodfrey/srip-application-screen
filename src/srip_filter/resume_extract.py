"""Stage 6 resume PDF text extraction (Phase 12.3, PRD §7.2).

Pure transformation: PDF bytes in, plain text out — no network, no LLM. Sits between the
download layer (:mod:`srip_filter.resume_fetch`) and Task E. Mirrors the fetch layer's
bonus-only discipline: :func:`extract_resume_text` **never raises** — every failure becomes a
typed reason the Stage 6 aggregator turns into a 0 bonus plus an audit note, never a block.

Design points (PLAN.md Phase 12):

* ``pypdf`` over the PRD's ``pdfplumber`` — a much lighter dependency tree for text-only
  extraction on small hosts (documented deviation).
* Magic-bytes check first, so a non-PDF upload (image, docx, HTML error page) fails fast and
  cheaply with ``not_a_pdf``.
* Extracted text is capped at ``resume.max_text_chars`` (bounds Task E token spend); page
  iteration stops as soon as the cap is reached, so a 200-page upload costs no extra work.
* A PDF with no extractable text (scanned/image-only) is a typed failure — there is
  deliberately **no OCR dependency** (hosting analysis: keep the tree small).
* The caller discards the PDF bytes immediately after this returns (the per-applicant
  fetch → extract → discard memory rule); nothing here retains or logs resume content.
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

# %PDF- must appear near the start; some generators prepend a little junk, so search a
# small window rather than byte 0 only.
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
    """Extract plain text from PDF bytes, capped at ``resume.max_text_chars``. Never raises.

    Failure reasons: ``not_a_pdf`` (magic bytes missing), ``pdf_encrypted`` (password-protected
    beyond an empty-password unlock), ``pdf_parse_error`` (malformed), ``no_extractable_text``
    (scanned/image-only — no OCR by design).
    """
    if _MAGIC not in content[:_MAGIC_WINDOW]:
        return _fail(FAIL_NOT_PDF)
    max_chars = cfg.resume.max_text_chars
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):  # some PDFs are "encrypted" with an empty password
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
