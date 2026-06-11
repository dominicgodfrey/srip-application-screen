"""Tests for resume PDF text extraction (Phase 12.3). Pure — no network, no LLM, no fixtures
on disk: PDFs are built in memory (synthetic content only)."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter

from srip_filter.config import AppConfig
from srip_filter.resume_extract import (
    FAIL_NO_TEXT,
    FAIL_NOT_PDF,
    FAIL_PDF_ENCRYPTED,
    FAIL_PDF_PARSE,
    extract_resume_text,
)


def make_config(**resume_overrides: object) -> AppConfig:
    return AppConfig.model_validate({"resume": resume_overrides})


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_text_pdf(*page_texts: str) -> bytes:
    """Build a minimal valid PDF with one text line per page (correct xref offsets)."""
    n = len(page_texts)
    font_id = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, text in enumerate(page_texts):
        content_id = 4 + 2 * i
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_id} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 712 Td ({_escape(text)}) Tj ET".encode("latin-1")
        objs.append(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def build_blank_pdf(*, encrypted_with: str | None = None) -> bytes:
    """A structurally valid PDF with one blank (no-text) page, optionally password-protected."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    if encrypted_with is not None:
        writer.encrypt(encrypted_with)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_extracts_text_from_single_page() -> None:
    pdf = build_text_pdf("Python developer with three shipped projects")
    result = extract_resume_text(pdf, make_config())
    assert result.ok and result.failure == ""
    assert "three shipped projects" in result.text


def test_extracts_and_joins_multiple_pages() -> None:
    pdf = build_text_pdf("Page one: education", "Page two: USACO silver award")
    result = extract_resume_text(pdf, make_config())
    assert result.ok
    assert "education" in result.text and "USACO silver" in result.text


def test_text_capped_at_max_text_chars() -> None:
    pdf = build_text_pdf("A" * 200)
    result = extract_resume_text(pdf, make_config(max_text_chars=50))
    assert result.ok
    assert len(result.text) == 50


def test_non_pdf_bytes_fail_fast_on_magic_check() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # an image upload, e.g. a screenshot "resume"
    result = extract_resume_text(png, make_config())
    assert not result.ok and result.failure == FAIL_NOT_PDF


def test_empty_bytes_are_not_a_pdf() -> None:
    result = extract_resume_text(b"", make_config())
    assert not result.ok and result.failure == FAIL_NOT_PDF


def test_garbage_with_pdf_magic_never_raises() -> None:
    result = extract_resume_text(b"%PDF-1.4 this is not really a pdf body", make_config())
    assert not result.ok
    assert result.failure in (FAIL_PDF_PARSE, FAIL_NO_TEXT)


def test_blank_page_pdf_is_no_extractable_text() -> None:
    """A scanned/image-only resume extracts no text — typed failure, no OCR by design."""
    result = extract_resume_text(build_blank_pdf(), make_config())
    assert not result.ok and result.failure == FAIL_NO_TEXT


def test_password_protected_pdf_is_typed_failure() -> None:
    result = extract_resume_text(build_blank_pdf(encrypted_with="secret"), make_config())
    assert not result.ok and result.failure == FAIL_PDF_ENCRYPTED
