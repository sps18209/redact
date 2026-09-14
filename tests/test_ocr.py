"""PII rendered as pixels — screenshots, scans, photos of paper.

This was the suite's worst live failure. A screenshot showing a name, an SSN and
an email went through the image path and came back:

    [ok] screenshot.png via deface (image): 0 entities | 0 face(s) blurred
    exit 0

and `redact verify` agreed it was `clean`. Both look for text; the characters
were pixels. Two tools affirming a file is safe while the SSN is plainly legible
is worse than either of them refusing to handle it — and screenshots and scans
are ordinary in exactly the work this suite is for.
"""

import pytest

from redact.backends.deface import DefaceBackend
from redact.backends.ocr import OcrBackend
from redact.document import Document
from redact.types import MediaType, RedactionMode, RedactionOptions
from redact.verify import verify_path

pymupdf = pytest.importorskip("pymupdf")

SSN = "078-05-1120"
EMAIL = "jane.doe@example.com"


@pytest.fixture(scope="module")
def screenshot(tmp_path_factory):
    """An image of a record: every character is pixels, no text layer."""
    path = tmp_path_factory.mktemp("ocr") / "screenshot.png"
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=140)
    page.insert_text((20, 40), "Patient: Jane Doe", fontsize=13)
    page.insert_text((20, 65), f"SSN {SSN}", fontsize=13)
    page.insert_text((20, 90), EMAIL, fontsize=13)
    page.get_pixmap(dpi=140).save(str(path))
    doc.close()
    return path


def _ocr_available():
    from redact import ocr

    return ocr.available()


needs_ocr = pytest.mark.skipif(not _ocr_available(), reason="needs [ocr] extra")


# -- the engine's own quirk ---------------------------------------------------

def test_boundary_restoration_recovers_a_run_together_ssn():
    """OCR reads "SSN 078-05-1120" as "SSN078-05-1120". There is no word
    boundary between "N" and "0", so a \\b-anchored pattern misses the most
    sensitive value in the image. Found by testing, not by reading the regex."""
    from redact.backends.builtin import detect_entities
    from redact.ocr import boundary_restored

    run_together = f"SSN{SSN}"
    assert detect_entities(run_together) == [], "precondition: the raw reading misses it"
    assert [e.entity_type for e in detect_entities(boundary_restored(run_together))] == ["US_SSN"]


# -- detection and redaction --------------------------------------------------

@needs_ocr
def test_it_finds_pii_that_is_pixels(screenshot):
    from redact.ocr import text_entities

    kinds = {e.entity_type for e, _ in text_entities(screenshot)}
    assert {"US_SSN", "EMAIL_ADDRESS"} <= kinds


@needs_ocr
def test_redacting_makes_the_pii_unreadable(screenshot, tmp_path):
    """Read the output back with the same engine: the point is that the pixels
    changed, not that a box was drawn somewhere."""
    from redact.ocr import read_regions

    res = OcrBackend().redact(
        Document(path=screenshot, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    recovered = " ".join(t for t, _ in read_regions(res.output_path)).replace(" ", "")
    assert SSN.replace("-", "") not in recovered.replace("-", "")
    assert EMAIL not in recovered


@needs_ocr
def test_the_original_really_was_readable(screenshot):
    """Positive control: without it the assertion above proves nothing."""
    from redact.ocr import read_regions

    recovered = " ".join(t for t, _ in read_regions(screenshot)).replace(" ", "")
    assert SSN in recovered


@needs_ocr
def test_blur_mode_is_strong_enough_to_be_unreadable(screenshot, tmp_path):
    from redact.ocr import read_regions

    res = OcrBackend().redact(
        Document(path=screenshot, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "b", mode=RedactionMode.BLUR),
    )
    recovered = " ".join(t for t, _ in read_regions(res.output_path))
    assert SSN not in recovered.replace(" ", "")


@needs_ocr
def test_dry_run_writes_nothing(screenshot, tmp_path):
    res = OcrBackend().redact(
        Document(path=screenshot, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out", dry_run=True),
    )
    assert res.success and res.output_path is None and res.entities
    assert not (tmp_path / "out").exists()


# -- the face backends must stop claiming a screenshot is finished ------------

@needs_ocr
def test_the_face_backend_declares_text_it_did_not_redact(screenshot, tmp_path):
    """deface finds faces. It must not report a clean run over legible text."""
    if DefaceBackend().missing_dependencies():
        pytest.skip("deface not installed")
    res = DefaceBackend().redact(
        Document(path=screenshot, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success
    assert res.unredacted, "a screenshot full of PII must not come back clean"
    assert not res.fully_redacted, "so the CLI exits non-zero"


# -- verification must be able to read the format it was handed ---------------

@needs_ocr
def test_verify_reads_text_out_of_an_image(screenshot):
    report = verify_path(screenshot)
    kinds = {f.entity.entity_type for f in report.findings}
    assert {"US_SSN", "EMAIL_ADDRESS"} <= kinds
    assert all(f.layer == "image:ocr" for f in report.findings)


@needs_ocr
def test_verify_is_clean_once_the_regions_are_covered(screenshot, tmp_path):
    res = OcrBackend().redact(
        Document(path=screenshot, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert verify_path(res.output_path).clean


def test_verify_without_an_engine_is_inconclusive_not_clean(screenshot, monkeypatch):
    """Searching a PNG's compressed bytes finds nothing whether or not an SSN is
    legible on screen. Reporting `clean` there is the base64 failure again."""
    from redact import ocr

    monkeypatch.setattr(ocr, "available", lambda: False)
    report = verify_path(screenshot)
    assert report.inconclusive
    assert report.status == "INCONCLUSIVE"
    assert "NOT examined" in report.note


@needs_ocr
def test_the_yolo_backend_also_declares_unexamined_text(screenshot, tmp_path):
    """The honesty check was wired into deface only, so
    `redact run screenshot.png -b yolo` reported "nothing detected", empty
    unredacted, exit 0 — over a legible SSN. Every image backend owes the same
    declaration, not just the first one that got it."""
    from redact.backends.yolo import YoloBackend

    backend = YoloBackend()
    if backend.missing_dependencies():
        pytest.skip("yolo extra not installed")

    monkey = pytest.MonkeyPatch()
    try:
        # Stub detection: this test is about the declaration, not the model.
        monkey.setattr(backend, "_load_model", lambda *a, **k: (object(), {}), raising=False)
        import redact.backends.yolo as mod

        monkey.setattr(mod, "_load_model", lambda *a, **k: (object(), {}))
        monkey.setattr(mod, "_redact_image", lambda *a, **k: [])
        res = backend.redact(
            Document(path=screenshot, media_type=MediaType.IMAGE),
            RedactionOptions(output_dir=tmp_path / "out"),
        )
    finally:
        monkey.undo()

    assert res.success, res.message
    assert res.unredacted, "yolo must declare legible text it did not redact"
    assert not res.fully_redacted
