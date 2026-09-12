"""Word document redaction (stdlib zip + XML round-trip)."""

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from redact import RedactionOptions, RedactionSuite
from redact.backends.builtin import BuiltinBackend
from redact.document import Document, detect_media_type
from redact.docx import DocxError, extract_text
from redact.types import MediaType, RedactionMode

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
# Root tag deliberately carries mc:Ignorable + an *unused* w14 namespace: Word
# rejects the file if a naive serializer drops that declaration.
DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    f'<w:document xmlns:w="{W_NS}" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14">'
    "<w:body>"
    # email split across three runs, the middle one bold
    '<w:p><w:r><w:t xml:space="preserve">Contact: </w:t></w:r>'
    "<w:r><w:rPr><w:b/></w:rPr><w:t>jane</w:t></w:r>"
    "<w:r><w:t>.doe@exa</w:t></w:r>"
    "<w:r><w:t>mple.com</w:t></w:r>"
    '<w:r><w:t xml:space="preserve"> today.</w:t></w:r></w:p>'
    # a tab element between runs
    "<w:p><w:r><w:t>SSN</w:t></w:r><w:r><w:tab/><w:t>123-45-6789</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Nothing here.</w:t></w:r></w:p>"
    "</w:body></w:document>"
)
HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:hdr xmlns:w="{W_NS}"><w:p><w:r><w:t>Call (415) 555-0199</w:t></w:r></w:p></w:hdr>'
)
CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>Memo</dc:title><dc:creator>Jane Doe</dc:creator></cp:coreProperties>"
)
IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


def make_docx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", DOCUMENT)
        zf.writestr("word/header1.xml", HEADER)
        zf.writestr("docProps/core.xml", CORE)
        zf.writestr("word/media/image1.png", IMAGE)
    return path


def run_texts(xml: bytes):
    return [t.text or "" for t in ET.fromstring(xml).iter(f"{{{W_NS}}}t")]


@pytest.fixture
def docx(tmp_path):
    return make_docx(tmp_path / "memo.docx")


def test_media_type_by_extension_and_by_sniff(tmp_path, docx):
    assert detect_media_type(docx) is MediaType.DOCX
    noext = tmp_path / "blob"
    noext.write_bytes(docx.read_bytes())
    assert detect_media_type(noext) is MediaType.DOCX
    plain_zip = tmp_path / "other"
    with zipfile.ZipFile(plain_zip, "w") as zf:
        zf.writestr("a.txt", "x")
    assert detect_media_type(plain_zip) is MediaType.UNKNOWN


def test_extract_text(docx):
    text = extract_text(docx)
    assert "Contact: jane.doe@example.com today." in text
    assert "SSN\t123-45-6789" in text
    assert "Call (415) 555-0199" in text  # header included


def test_redacts_entity_split_across_runs(tmp_path, docx):
    res = BuiltinBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path == tmp_path / "out" / "memo.redacted.docx"
    assert {e.entity_type for e in res.entities} == {
        "EMAIL_ADDRESS", "US_SSN", "PHONE_NUMBER", "DOCUMENT_AUTHOR",
    }
    assert "<EMAIL_ADDRESS>" in res.redacted_text and "Nothing here." in res.redacted_text

    with zipfile.ZipFile(res.output_path) as zf:
        body = zf.read("word/document.xml")
        assert zf.namelist()[0] == "[Content_Types].xml"
        assert zf.read("word/media/image1.png") == IMAGE  # untouched parts byte-identical
        header = zf.read("word/header1.xml")
        core = zf.read("docProps/core.xml")

    # placeholder lands in the run where the entity started; the rest is emptied
    assert run_texts(body) == ["Contact: ", "<EMAIL_ADDRESS>", "", "", " today.", "SSN", "<US_SSN>", "Nothing here."]
    assert b"jane" not in body and b"6789" not in body
    # formatting/structure around the rewritten runs survives (ET writes "<w:b />")
    tree = ET.fromstring(body)
    runs = list(tree.iter(f"{{{W_NS}}}r"))
    assert runs[1].find(f"{{{W_NS}}}rPr/{{{W_NS}}}b") is not None  # bold run holds the placeholder
    assert tree.find(f".//{{{W_NS}}}tab") is not None
    # Word-compat invariants
    assert body.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    assert b'mc:Ignorable="w14"' in body and b'xmlns:w14=' in body
    assert b'<w:document xmlns:w="' in body  # original prefix, no ns0
    assert run_texts(header) == ["Call <PHONE_NUMBER>"]
    assert b"<dc:creator>&lt;DOCUMENT_AUTHOR&gt;</dc:creator>" in core
    for part in (body, header, core):
        ET.fromstring(part)  # well-formed


def test_mask_mode_and_entity_filter(tmp_path, docx):
    opts = RedactionOptions(
        output_dir=tmp_path / "o", mode=RedactionMode.MASK, entities=["EMAIL_ADDRESS"],
    )
    res = BuiltinBackend().redact(Document(path=docx, media_type=MediaType.DOCX), opts)
    assert res.success
    assert {e.entity_type for e in res.entities} == {"EMAIL_ADDRESS"}  # no author scrub when filtered
    with zipfile.ZipFile(res.output_path) as zf:
        texts = run_texts(zf.read("word/document.xml"))
        assert texts[1] == "*" * len("jane.doe@example.com")
        assert "123-45-6789" in texts  # SSN untouched by filter
        assert b"Jane Doe" in zf.read("docProps/core.xml")


def test_dry_run_detects_but_writes_nothing(tmp_path, docx):
    res = BuiltinBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None
    assert res.entity_count == 4
    assert list(tmp_path.iterdir()) == [docx]


def test_suite_routes_docx_to_builtin(tmp_path, docx):
    res = RedactionSuite().redact_path(docx, RedactionOptions(output_dir=tmp_path / "o"))
    assert res.success and res.backend == "builtin"


def test_corrupt_docx_is_a_failed_result_not_an_exception(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    res = BuiltinBackend().redact(Document(path=bad, media_type=MediaType.DOCX), RedactionOptions())
    assert res.success is False and "docx" in res.message.lower()
    with pytest.raises(DocxError):
        extract_text(bad)


def test_output_is_skipped_on_rerun(tmp_path, docx):
    suite = RedactionSuite()
    list(suite.redact_paths([str(tmp_path)]))
    list(suite.redact_paths([str(tmp_path)]))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["memo.docx", "memo.redacted.docx"]
