"""True in-place PDF redaction.

PDF was the suite's largest field gap: the only backends that claimed it needed
either a running Ollama server or the pdf-redact-tools CLI with its
ImageMagick/exiftool/poppler stack, and the former does not modify the PDF at
all. The most common document type in legal and medical work could not be
redacted by a default install.

The bar this backend has to clear is that a black rectangle drawn over text is
*not* redaction — the characters stay in the content stream for any extractor to
recover. So these tests check the bytes, not the rendering.
"""

import pytest

from redact.backends.pymupdf import PyMuPDFBackend
from redact.document import Document
from redact.types import MediaType, RedactionMode, RedactionOptions

pymupdf = pytest.importorskip("pymupdf")

SSN = "123-45-6789"
EMAIL = "jane.doe@example.com"


@pytest.fixture
def pdf(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), f"Patient Jane Doe SSN {SSN}", fontsize=11)
    page.insert_text((72, 130), f"Contact {EMAIL}", fontsize=11)
    doc.set_metadata({"author": "Jane Doe", "title": f"SSN {SSN}"})
    path = tmp_path / "chart.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def scanned(tmp_path):
    """A page whose only content is an image of text — no text layer at all."""
    inner = pymupdf.open()
    page = inner.new_page()
    page.insert_text((40, 60), f"Patient Jane Doe SSN {SSN}", fontsize=14)
    pix = page.get_pixmap(dpi=110)
    img = tmp_path / "_scan.png"
    pix.save(str(img))
    inner.close()

    doc = pymupdf.open()
    doc.new_page().insert_image(pymupdf.Rect(0, 0, 400, 200), filename=str(img))
    path = tmp_path / "scanned.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _run(path, tmp_path, **kw):
    return PyMuPDFBackend().redact(
        Document(path=path, media_type=MediaType.PDF),
        RedactionOptions(output_dir=tmp_path / "out", **kw),
    )


def _text(path):
    doc = pymupdf.open(path)
    try:
        return "".join(p.get_text() for p in doc)
    finally:
        doc.close()


# -- the core claim: removal, not concealment ---------------------------------

def test_the_value_is_gone_from_the_text_layer(pdf, tmp_path):
    res = _run(pdf, tmp_path)
    assert res.success, res.message
    assert SSN not in _text(res.output_path)
    assert EMAIL not in _text(res.output_path)


def test_the_value_is_gone_from_the_raw_bytes(pdf, tmp_path):
    """A drawn rectangle leaves the characters in the content stream."""
    res = _run(pdf, tmp_path)
    raw = res.output_path.read_bytes()
    assert SSN.encode() not in raw
    assert EMAIL.encode() not in raw


def test_the_secret_really_was_findable_before(pdf):
    """Positive control: without it, a passing test above proves nothing."""
    assert SSN in _text(pdf)
    assert SSN.encode() in pdf.read_bytes()


def test_an_independent_extractor_cannot_recover_it(pdf, tmp_path):
    """Negative control: a tool tends to be blind to its own blind spots."""
    res = _run(pdf, tmp_path)
    doc = pymupdf.open(res.output_path)
    try:
        words = " ".join(w[4] for page in doc for w in page.get_text("words"))
    finally:
        doc.close()
    assert SSN.replace("-", "") not in words.replace("-", "").replace(" ", "")


def test_metadata_is_scrubbed(pdf, tmp_path):
    res = _run(pdf, tmp_path)
    doc = pymupdf.open(res.output_path)
    try:
        meta = doc.metadata
    finally:
        doc.close()
    assert not meta.get("author")
    assert SSN not in (meta.get("title") or "")


# -- a scan must never read as a clean run ------------------------------------

def test_a_scanned_page_is_reported_not_silently_passed(scanned, tmp_path):
    """Every word is pixels, so detection finds nothing. "0 entities" on a
    document full of PII is the worst possible outcome for a redaction tool."""
    res = _run(scanned, tmp_path)
    assert res.entities == []
    assert res.unredacted, "a page with no text layer must be declared"
    assert not res.fully_redacted, "so the CLI exits non-zero"
    assert "no text layer" in res.message


def test_a_blank_page_is_not_mistaken_for_a_scan(tmp_path):
    """An empty page carries no images and must not raise a false alarm."""
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "blank.pdf"
    doc.save(str(path))
    doc.close()
    res = _run(path, tmp_path)
    assert res.success and res.unredacted == []


# -- a detected entity that cannot be placed ----------------------------------

def test_an_unlocatable_entity_is_declared_not_dropped(pdf, tmp_path, monkeypatch):
    """Detection reads extracted text; redaction needs coordinates. Ligatures
    and split spans can make a detected string unfindable on the page."""
    # Patch the class: iterating a document yields fresh Page objects, so
    # patching instances up front would not survive into _plan.
    monkeypatch.setattr(pymupdf.Page, "search_for", lambda *a, **k: [])
    res = _run(pdf, tmp_path)
    assert res.entities == []
    assert res.unredacted, "detected-but-unplaceable must not vanish"
    assert not res.fully_redacted
    assert "could not be located" in res.message


# -- ordinary plumbing --------------------------------------------------------

def test_dry_run_writes_nothing(pdf, tmp_path):
    res = _run(pdf, tmp_path, dry_run=True)
    assert res.success and res.output_path is None
    assert res.entities and not (tmp_path / "out").exists()


def test_output_is_named_for_idempotent_reingest(pdf, tmp_path):
    res = _run(pdf, tmp_path)
    assert res.output_path.name == "chart.redacted.pdf"


def test_mode_changes_the_placeholder(pdf, tmp_path):
    res = _run(pdf, tmp_path, mode=RedactionMode.REPLACE, entities=["US_SSN"])
    assert "<US_SSN>" in _text(res.output_path)


def test_a_corrupt_pdf_is_a_failed_result_not_a_crash(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot really a pdf")
    res = _run(bad, tmp_path)
    assert res.success is False and res.message


def test_the_router_prefers_it_for_pdf():
    """It is the only backend that redacts the PDF itself and keeps it usable."""
    from redact.backends.pdf_redact_tools import PdfRedactToolsBackend
    from redact.backends.redactai import RedactAIBackend

    assert PyMuPDFBackend.priority > RedactAIBackend.priority
    assert PyMuPDFBackend.priority > PdfRedactToolsBackend.priority
