"""
AEOS Unit Tests — upload content validation (magic-byte sniffing)

Confirms uploads are validated by *content*, not just extension, so an
executable/archive renamed to an allowed extension is still rejected.
"""
import pytest

from app.rag.security import validate_upload_content, SecurityError


@pytest.mark.parametrize("data,label", [
    (b"MZ\x90\x00 payload", "windows-pe"),
    (b"\x7fELF\x02\x01\x01", "linux-elf"),
    (b"\xca\xfe\xba\xbe", "macho-java"),
    (b"PK\x03\x04\x14\x00", "zip-office-jar"),
    (b"\x1f\x8b\x08\x00", "gzip"),
])
def test_executable_or_archive_magic_rejected_even_as_txt(data, label):
    with pytest.raises(SecurityError):
        validate_upload_content(".txt", data)


def test_pdf_requires_pdf_signature():
    with pytest.raises(SecurityError):
        validate_upload_content(".pdf", b"this is not really a pdf")


def test_valid_pdf_accepted():
    validate_upload_content(".pdf", b"%PDF-1.7\n1 0 obj\n")  # no raise


def test_nul_bytes_in_text_rejected():
    with pytest.raises(SecurityError):
        validate_upload_content(".txt", b"hello\x00\x00world")


def test_undecodable_binary_text_rejected():
    with pytest.raises(SecurityError):
        # Bytes that are neither valid utf-8 nor... actually latin-1 decodes
        # anything, so this asserts the NUL guard path catches disguised binary.
        validate_upload_content(".md", b"\x00\xff\xfe garbage")


def test_empty_file_rejected():
    with pytest.raises(SecurityError):
        validate_upload_content(".txt", b"")


@pytest.mark.parametrize("ext,data", [
    (".txt", b"# Real notes about AEOS"),
    (".md", b"## Heading\n\nbody text"),
    (".json", b'{"key": "value"}'),
    (".html", b"<html><body>hi</body></html>"),
])
def test_legitimate_text_files_accepted(ext, data):
    validate_upload_content(ext, data)  # no raise
