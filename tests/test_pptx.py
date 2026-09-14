"""PowerPoint redaction.

A deck's riskiest text is usually not on the slide: speaker notes are where
people paste the detail they did not want on screen, a master or layout can
carry a name long after the slide that introduced it was deleted, and an
embedded chart caches its source values even when the data table is gone.
"""

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from redact import RedactionOptions
from redact.backends.builtin import MASK_WIDTH, BuiltinBackend
from redact.document import Document, detect_media_type
from redact.pptx import PptxError, extract_text
from redact.types import MediaType, RedactionMode

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = f"{{{A_NS}}}"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/></Types>'
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
    "</Relationships>"
)
PRESENTATION = f'<?xml version="1.0"?><p:presentation xmlns:p="{P_NS}"/>'


def _sp(*paragraphs):
    body = "".join(
        "<a:p>" + "".join(f"<a:r><a:t>{t}</a:t></a:r>" for t in runs) + "</a:p>"
        for runs in paragraphs
    )
    return f"<p:sp><p:txBody>{body}</p:txBody></p:sp>"


def _slide_doc(*shapes):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree>'
        + "".join(shapes)
        + "</p:spTree></p:cSld></p:sld>"
    )


# email deliberately split across runs, as PowerPoint does after editing
SLIDE1 = _slide_doc(_sp(["Contact ", "jane", ".doe@exa", "mple.com"], ["Q3 revenue"]))
NOTES1 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<p:notes xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree>'
    + _sp(["Do not say the SSN 123-45-6789 out loud"])
    + "</p:spTree></p:cSld></p:notes>"
)
LAYOUT1 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<p:sldLayout xmlns:p="{P_NS}" xmlns:a="{A_NS}"><p:cSld><p:spTree>'
    + _sp(["Prepared by carol@example.com"])
    + "</p:spTree></p:cSld></p:sldLayout>"
)
CHART1 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<c:chartSpace xmlns:c="{C_NS}" xmlns:a="{A_NS}"><c:chart><c:plotArea>'
    "<c:ser><c:cat><c:strRef><c:strCache>"
    "<c:pt><c:v>bob@example.com</c:v></c:pt>"
    "</c:strCache></c:strRef></c:cat></c:ser>"
    "</c:plotArea></c:chart></c:chartSpace>"
)
CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>Deck</dc:title><dc:creator>Jane Doe</dc:creator></cp:coreProperties>"
)
IMAGE = b"\xff\xd8\xff\xe0" + bytes(range(32)) + b"\xff\xd9"


def make_pptx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("ppt/presentation.xml", PRESENTATION)
        zf.writestr("ppt/slides/slide1.xml", SLIDE1)
        zf.writestr("ppt/notesSlides/notesSlide1.xml", NOTES1)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", LAYOUT1)
        zf.writestr("ppt/charts/chart1.xml", CHART1)
        zf.writestr("docProps/core.xml", CORE)
        zf.writestr("ppt/media/image1.jpeg", IMAGE)
    return path


@pytest.fixture
def pptx(tmp_path):
    return make_pptx(tmp_path / "deck.pptx")


def _redact(pptx, tmp_path, **kw):
    res = BuiltinBackend().redact(
        Document(path=pptx, media_type=MediaType.PPTX),
        RedactionOptions(output_dir=tmp_path / "out", **kw),
    )
    assert res.success, res.message
    return res


def _texts(zf, part):
    return [t.text for t in ET.fromstring(zf.read(part)).iter(A + "t")]


# -- detection -----------------------------------------------------------------

def test_detected_by_extension_and_by_sniff(tmp_path, pptx):
    assert detect_media_type(pptx) is MediaType.PPTX
    assert detect_media_type(Path("x.pptm")) is MediaType.PPTX
    blob = tmp_path / "noext"
    blob.write_bytes(pptx.read_bytes())
    assert detect_media_type(blob) is MediaType.PPTX


def test_extract_text_reaches_notes_and_layouts(pptx):
    text = extract_text(pptx)
    assert "jane.doe@example.com" in text
    assert "123-45-6789" in text        # speaker notes
    assert "carol@example.com" in text  # layout


# -- the hiding places ----------------------------------------------------------

def test_split_runs_on_a_slide_are_rejoined(tmp_path, pptx):
    res = _redact(pptx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        texts = _texts(zf, "ppt/slides/slide1.xml")
        raw = zf.read("ppt/slides/slide1.xml")
    assert "<EMAIL_ADDRESS>" in texts
    assert b"jane" not in raw
    assert "Q3 revenue" in texts  # untouched content survives


def test_speaker_notes_are_redacted(tmp_path, pptx):
    """Notes are where the detail people did not want on screen ends up."""
    res = _redact(pptx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        raw = zf.read("ppt/notesSlides/notesSlide1.xml")
    assert b"123-45-6789" not in raw
    assert b"US_SSN" in raw


def test_layouts_and_masters_are_redacted(tmp_path, pptx):
    res = _redact(pptx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        raw = zf.read("ppt/slideLayouts/slideLayout1.xml")
    assert b"carol@example.com" not in raw


def test_chart_value_cache_is_redacted(tmp_path, pptx):
    """A chart keeps its source values even when the data table is gone."""
    res = _redact(pptx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        raw = zf.read("ppt/charts/chart1.xml")
    assert b"bob@example.com" not in raw
    assert b"EMAIL_ADDRESS" in raw


def test_findings_name_the_part_they_came_from(tmp_path, pptx):
    res = _redact(pptx, tmp_path)
    assert "notesSlide1" in res.message
    assert "slideLayout1" in res.message


def test_docprops_author_scrubbed(tmp_path, pptx):
    res = _redact(pptx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        core = zf.read("docProps/core.xml")
    assert b"Jane Doe" not in core and b"<dc:title>Deck</dc:title>" in core


# -- package integrity, modes, plumbing ------------------------------------------

def test_package_remains_valid(tmp_path, pptx):
    res = _redact(pptx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.namelist()[0] == "[Content_Types].xml"
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                part = zf.read(name)
                ET.fromstring(part)
                assert b"ns0:" not in part, f"{name} namespace-mangled"


def test_image_policy_strip(tmp_path, pptx):
    res = _redact(pptx, tmp_path, docx_images="strip")
    assert "1 embedded image(s) stripped" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        assert "ppt/media/image1.png" in zf.namelist()
        assert zf.read("ppt/media/image1.png").startswith(b"\x89PNG")


def test_mask_mode_and_entity_filter(tmp_path, pptx):
    res = _redact(pptx, tmp_path, mode=RedactionMode.MASK, entities=["EMAIL_ADDRESS"])
    with zipfile.ZipFile(res.output_path) as zf:
        assert b"123-45-6789" in zf.read("ppt/notesSlides/notesSlide1.xml")  # filtered out
        assert "*" * MASK_WIDTH in _texts(zf, "ppt/slides/slide1.xml")  # fixed width


def test_dry_run_writes_nothing(tmp_path, pptx):
    res = BuiltinBackend().redact(
        Document(path=pptx, media_type=MediaType.PPTX), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None and res.entities
    assert list(tmp_path.iterdir()) == [pptx]


def test_corrupt_pptx_is_a_failed_result(tmp_path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a zip")
    res = BuiltinBackend().redact(
        Document(path=bad, media_type=MediaType.PPTX), RedactionOptions()
    )
    assert res.success is False
    with pytest.raises(PptxError):
        extract_text(bad)


def test_suite_routes_pptx(tmp_path, pptx, builtin_only_suite):
    res = builtin_only_suite.redact_path(pptx, RedactionOptions(output_dir=tmp_path / "o"))
    assert res.success and res.media_type is MediaType.PPTX
