from io import BytesIO

import pytest
from backend.services import ingestion
from docx import Document


def test_pdf_extraction_preserves_page_offsets(sample_pdf):
    result = ingestion.extract_document("policy.pdf", "application/pdf", sample_pdf)

    assert result.text == "Medication incident policy"
    assert result.metadata == {"pages": [{"page": 1, "start": 0, "end": 26}]}


def test_markdown_extraction_normalizes_text_and_preserves_sections():
    result = ingestion.extract_document(
        "policy.md",
        "text/markdown",
        b"# Policy\r\n\r\n  Keep   records.\r\n## Review\r\nCheck weekly.",
    )

    assert result.text == "# Policy\n\nKeep records.\n\n## Review\nCheck weekly."
    assert result.metadata["sections"] == [
        {"title": "Policy", "level": 1, "start": 0, "end": 23},
        {"title": "Review", "level": 2, "start": 25, "end": 48},
    ]


def test_docx_extraction_keeps_heading_and_table_text():
    document = Document()
    document.add_heading("Incident policy", level=1)
    document.add_paragraph("Record the incident.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Owner"
    table.cell(0, 1).text = "Nurse"
    output = BytesIO()
    document.save(output)

    result = ingestion.extract_document(
        "policy.docx", ingestion.DOCX_MIME_TYPE, output.getvalue()
    )

    assert result.text == "Incident policy\nRecord the incident.\nOwner | Nurse"
    assert result.metadata["sections"] == [
        {"title": "Incident policy", "level": 1, "start": 0, "end": 50}
    ]


def test_txt_extraction_normalizes_unicode_and_offsets():
    result = ingestion.extract_document(
        "notes.txt", "text/plain", b"Cafe\xcc\x81\r\n\r\nDone"
    )

    assert result.text == "Caf\u00e9\n\nDone"
    assert result.metadata["sections"] == [
        {"title": None, "level": None, "start": 0, "end": 10}
    ]


def test_upload_validation_rejects_mime_spoof_and_non_utf8_text():
    with pytest.raises(ingestion.UnsupportedDocument):
        ingestion.validate_upload("policy.pdf", "text/plain", b"%PDF-1.4")

    with pytest.raises(ingestion.UnsupportedDocument):
        ingestion.validate_upload("notes.txt", "text/plain", b"\xff")


def test_upload_validation_checks_pdf_and_docx_signatures(sample_pdf):
    assert (
        ingestion.validate_upload("policy.pdf", "application/pdf", sample_pdf)
        == "application/pdf"
    )
    with pytest.raises(ingestion.UnsupportedDocument, match="content"):
        ingestion.validate_upload("policy.pdf", "application/pdf", b"not a pdf")

    document = Document()
    document.add_paragraph("A Word document")
    output = BytesIO()
    document.save(output)
    assert (
        ingestion.validate_upload(
            "policy.docx", ingestion.DOCX_MIME_TYPE, output.getvalue()
        )
        == ingestion.DOCX_MIME_TYPE
    )


def test_extraction_rejects_documents_without_text(sample_pdf):
    result = ingestion.extract_document("policy.pdf", "application/pdf", sample_pdf)
    assert result.text
    with pytest.raises(ingestion.UnsupportedDocument, match="No extractable text"):
        ingestion.extract_document("notes.txt", "text/plain", b"  \n")


def test_upload_validation_rejects_oversized_file():
    with pytest.raises(ingestion.DocumentTooLarge):
        ingestion.validate_upload(
            "notes.txt", "text/plain", b"x" * (ingestion.MAX_UPLOAD_BYTES + 1)
        )
