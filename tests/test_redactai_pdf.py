"""The redactai backend must not call a PDF redacted when it only extracted text.

This adapter has no PDF writer: for a PDF it detects in the text layer and
writes a redacted .txt *extract*, leaving the source PDF untouched. It used to
return success with nothing in `unredacted`, so `redact run report.pdf -b
redactai` exited 0 while the PDF the user holds still contained every entity the
run had just reported. A script checking the exit code was told the PDF was safe
to share.
"""

import pytest

from redact.backends.redactai import RedactAIBackend
from redact.document import Document
from redact.types import Entity, MediaType, RedactionOptions

SECRET = "123-45-6789"


@pytest.fixture
def backend():
    b = RedactAIBackend()
    # Stand in for pypdf + a running Ollama so the real redact() path runs.
    b.missing_dependencies = lambda: []
    b._extract_text = lambda d: f"Employee SSN {SECRET}"
    b._detect_with_model = lambda t, o: [
        Entity(entity_type="US_SSN", score=0.9, start=13, end=24, text=SECRET)
    ]
    return b


def _run(backend, path, media_type, tmp_path):
    return backend.redact(
        Document(path=path, media_type=media_type),
        RedactionOptions(output_dir=tmp_path / "out"),
    )


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(f"%PDF-1.4\n% SSN {SECRET} lives here\n".encode())
    return path


def test_a_pdf_is_declared_unredacted(backend, pdf, tmp_path):
    """The whole point: exit 0 must not promise a safe PDF."""
    res = _run(backend, pdf, MediaType.PDF, tmp_path)
    assert res.unredacted == ["report.pdf"]
    assert not res.fully_redacted


def test_the_source_pdf_really_is_untouched(backend, pdf, tmp_path):
    """Pins the reason the flag is set, not just the flag."""
    _run(backend, pdf, MediaType.PDF, tmp_path)
    assert SECRET.encode() in pdf.read_bytes()


def test_the_message_says_the_pdf_is_not_redacted(backend, pdf, tmp_path):
    res = _run(backend, pdf, MediaType.PDF, tmp_path)
    assert "NOT redacted" in res.message
    # Must name a backend that actually works. This asserted pdf-redact-tools
    # until it turned out to be unmaintained Python 2 that will not run
    # unpatched — pointing a user there is a dead end, not a fix.
    assert "pymupdf" in res.message, "must name a backend that can"


def test_the_text_extract_is_still_produced_and_redacted(backend, pdf, tmp_path):
    res = _run(backend, pdf, MediaType.PDF, tmp_path)
    assert res.output_path.name == "report.redacted.txt"
    assert SECRET not in res.output_path.read_text()


def test_a_plain_text_input_is_a_genuinely_clean_run(backend, tmp_path):
    """For .txt the written file IS the redacted artifact — don't cry wolf."""
    src = tmp_path / "notes.txt"
    src.write_text(f"Employee SSN {SECRET}")
    res = _run(backend, src, MediaType.TEXT, tmp_path)
    assert res.success and res.unredacted == []
    assert res.fully_redacted
    assert SECRET not in res.output_path.read_text()


def test_dry_run_writes_nothing_and_claims_nothing(backend, pdf, tmp_path):
    res = backend.redact(
        Document(path=pdf, media_type=MediaType.PDF),
        RedactionOptions(dry_run=True, output_dir=tmp_path / "out"),
    )
    assert res.output_path is None
    assert not (tmp_path / "out").exists()
