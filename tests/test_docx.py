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
    # tracked deletion: invisible, but the address still ships in the file
    '<w:p><w:del w:id="1" w:author="Alice Smith" w:date="2026-01-01T00:00:00Z">'
    "<w:r><w:delText>old addr deleted@old.com</w:delText></w:r></w:del>"
    '<w:ins w:id="2" w:author="Bob Jones"><w:r><w:t>new text</w:t></w:r></w:ins></w:p>'
    # field code carrying an address in its instruction
    "<w:p><w:r><w:fldChar w:fldCharType=\"begin\"/></w:r>"
    '<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:field@example.com" </w:instrText></w:r>'
    "<w:r><w:fldChar w:fldCharType=\"separate\"/></w:r>"
    "<w:r><w:t>click here</w:t></w:r>"
    "<w:r><w:fldChar w:fldCharType=\"end\"/></w:r></w:p>"
    # field code carried as an attribute
    '<w:p><w:fldSimple w:instr=" HYPERLINK &quot;mailto:simple@example.com&quot; ">'
    "<w:r><w:t>link</w:t></w:r></w:fldSimple></w:p>"
    "<w:p><w:r><w:t>Nothing here.</w:t></w:r></w:p>"
    "</w:body></w:document>"
)
COMMENTS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:comments xmlns:w="{W_NS}">'
    '<w:comment w:id="1" w:author="Carol White" w:initials="CW">'
    "<w:p><w:r><w:t>ping me at carol@example.com</w:t></w:r></w:p></w:comment></w:comments>"
)
DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'
    '<Relationship Id="rId10" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
    'Target="mailto:linked@example.com" TargetMode="External"/>'
    "</Relationships>"
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
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(48)) + b"\xff\xd9"


def make_docx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", DOCUMENT)
        zf.writestr("word/_rels/document.xml.rels", DOC_RELS)
        zf.writestr("word/header1.xml", HEADER)
        zf.writestr("word/comments.xml", COMMENTS)
        zf.writestr("docProps/core.xml", CORE)
        zf.writestr("word/media/image1.png", IMAGE)
        zf.writestr("word/media/photo.jpeg", JPEG)
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
    }  # no EMBEDDED_IMAGE: the default policy leaves images alone
    assert "<EMAIL_ADDRESS>" in res.redacted_text and "Nothing here." in res.redacted_text

    with zipfile.ZipFile(res.output_path) as zf:
        body = zf.read("word/document.xml")
        assert zf.namelist()[0] == "[Content_Types].xml"
        assert zf.read("word/media/image1.png") == IMAGE  # untouched parts byte-identical
        header = zf.read("word/header1.xml")
        core = zf.read("docProps/core.xml")

    # placeholder lands in the run where the entity started; the rest is emptied
    assert run_texts(body) == [
        "Contact: ", "<EMAIL_ADDRESS>", "", "", " today.",   # split entity rejoined
        "SSN", "<US_SSN>",
        "new text", "click here", "link", "Nothing here.",   # untouched runs
    ]
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
    assert res.entity_count == 13  # visible + hidden findings, nothing written
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


# -- hidden content: tracked deletions, field codes, authors, rels ------------

def _redact(docx, tmp_path, **kw):
    opts = RedactionOptions(output_dir=tmp_path / "out", **kw)
    res = BuiltinBackend().redact(Document(path=docx, media_type=MediaType.DOCX), opts)
    assert res.success, res.message
    return res


def test_tracked_deletion_text_is_redacted(tmp_path, docx):
    res = _redact(docx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        body = zf.read("word/document.xml")
    assert b"deleted@old.com" not in body          # the whole point
    root = ET.fromstring(body)
    dels = [d.text for d in root.iter(f"{{{W_NS}}}delText")]
    assert dels == ["old addr <EMAIL_ADDRESS>"]    # structure kept, address gone


def test_deleted_text_is_not_merged_into_visible_flow(docx):
    # A deletion sitting next to visible text must not corrupt detection there.
    text = extract_text(docx)
    assert "deleted@old.com" not in text
    assert "new text" in text


def test_field_codes_are_redacted(tmp_path, docx):
    res = _redact(docx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        body = zf.read("word/document.xml")
    assert b"field@example.com" not in body
    assert b"simple@example.com" not in body
    root = ET.fromstring(body)
    instr = [i.text for i in root.iter(f"{{{W_NS}}}instrText")]
    assert instr == [' HYPERLINK "mailto:<EMAIL_ADDRESS>" ']
    simple = next(root.iter(f"{{{W_NS}}}fldSimple")).get(f"{{{W_NS}}}instr")
    assert "<EMAIL_ADDRESS>" in simple and "simple@example.com" not in simple


def test_revision_and_comment_authors_are_scrubbed(tmp_path, docx):
    res = _redact(docx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        body = zf.read("word/document.xml")
        comments = zf.read("word/comments.xml")
    for name in (b"Alice Smith", b"Bob Jones"):
        assert name not in body
    assert b"Carol White" not in comments and b'w:initials="CW"' not in comments
    assert b"carol@example.com" not in comments   # comment body redacted too
    assert b'w:author="&lt;DOCUMENT_AUTHOR&gt;"' in body


def test_external_hyperlink_target_is_redacted(tmp_path, docx):
    res = _redact(docx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        rels = zf.read("word/_rels/document.xml.rels")
    assert b"linked@example.com" not in rels
    assert b"&lt;EMAIL_ADDRESS&gt;" in rels


def test_hidden_entities_are_reported_without_offsets(tmp_path, docx):
    res = _redact(docx, tmp_path)
    hidden = [e for e in res.entities if e.start is None]
    assert hidden, "hidden-content findings should still be reported"
    # every hidden finding is genuinely off the visible text flow
    assert all(e.end is None for e in hidden)
    # visible offsets index the source text, as the plain-text backends do
    visible = [e for e in res.entities if e.start is not None]
    assert visible and all(0 <= e.start < e.end for e in visible)
    source = extract_text(docx)
    assert all(source[e.start : e.end] == e.text for e in visible)


# -- embedded images ---------------------------------------------------------

def test_images_kept_by_default_but_reported(tmp_path, docx):
    res = _redact(docx, tmp_path)
    assert "2 embedded image(s) left untouched" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.read("word/media/image1.png") == IMAGE
        assert zf.read("word/media/photo.jpeg") == JPEG


def test_strip_replaces_images_and_keeps_package_valid(tmp_path, docx):
    res = _redact(docx, tmp_path, docx_images="strip")
    assert "2 embedded image(s) stripped" in res.message
    assert sum(1 for e in res.entities if e.entity_type == "EMBEDDED_IMAGE") == 2

    with zipfile.ZipFile(res.output_path) as zf:
        names = zf.namelist()
        # the jpeg is renamed to .png because its replacement is a PNG
        assert "word/media/photo.png" in names and "word/media/photo.jpeg" not in names
        assert zf.read("word/media/image1.png").startswith(b"\x89PNG\r\n\x1a\n")
        assert JPEG not in zf.read("word/media/photo.png")
        # the relationship follows the rename, so the part still resolves
        rels = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
        targets = {r.get("Target") for r in rels}
        assert "media/image1.png" in targets
        # png is declared in content types
        ct = zf.read("[Content_Types].xml").decode()
        assert 'Extension="png"' in ct
        assert names[0] == "[Content_Types].xml"


def test_blank_png_is_structurally_valid():
    from redact.docx import _blank_png

    data = _blank_png()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xae\x42\x60\x82")
    # walk the chunks and verify every CRC
    import struct, zlib
    pos, tags = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 4 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(body) & 0xFFFFFFFF, f"bad CRC in {tag!r}"
        tags.append(tag)
        pos += 12 + length
    assert tags == [b"IHDR", b"IDAT", b"IEND"]


def test_blur_uses_the_injected_redactor(tmp_path, docx):
    seen = []

    def redactor(data: bytes, suffix: str):
        seen.append(suffix)
        return b"BLURRED" + data[:4]

    res = _redact(docx, tmp_path, docx_images="blur", extra={"image_redactor": redactor})
    assert sorted(seen) == [".jpeg", ".png"]
    assert "2 embedded image(s) blurred" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        # blurred output keeps its original format, so no rename happens
        assert zf.read("word/media/photo.jpeg").startswith(b"BLURRED")
        assert "word/media/photo.png" not in zf.namelist()


def test_blur_falls_back_to_strip_when_redactor_declines(tmp_path, docx):
    res = _redact(docx, tmp_path, docx_images="blur", extra={"image_redactor": lambda d, s: None})
    assert "2 image(s) could not be blurred and were stripped" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.read("word/media/image1.png").startswith(b"\x89PNG")


def test_failing_redactor_does_not_lose_the_document(tmp_path, docx):
    def boom(data, suffix):
        raise RuntimeError("model crashed")

    res = _redact(docx, tmp_path, docx_images="blur", extra={"image_redactor": boom})
    assert res.output_path.exists()
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.read("word/media/image1.png").startswith(b"\x89PNG")


def test_entity_filter_excluding_images_leaves_them_alone(tmp_path, docx):
    res = _redact(docx, tmp_path, docx_images="strip", entities=["EMAIL_ADDRESS"])
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.read("word/media/image1.png") == IMAGE


def test_invalid_image_policy_is_a_failed_result(tmp_path, docx):
    res = BuiltinBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX),
        RedactionOptions(output_dir=tmp_path / "o", docx_images="nonsense"),
    )
    assert res.success is False and "image_policy" in res.message


# -- suite wiring: blur uses a real image backend -----------------------------

def test_suite_injects_no_redactor_without_an_image_backend():
    from redact import RedactionSuite

    suite = RedactionSuite()
    # Anonymizer is the only image backend and is unconfigured here.
    assert suite._image_redactor() is None


def test_suite_blur_uses_the_anonymizer_backend(tmp_path, docx, monkeypatch):
    """End-to-end: --docx-images blur routes embedded images through Anonymizer."""
    import stat

    from redact import RedactionSuite

    script = tmp_path / "fake_anonymize"
    script.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do case \"$1\" in --input) IN=$2; shift;; "
        "--image-output) OUT=$2; shift;; *) ;; esac; shift; done\n"
        "for f in \"$IN\"/*; do printf 'BLURRED' > \"$OUT/$(basename $f)\"; done\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.delenv("ANONYMIZER_HOME", raising=False)
    monkeypatch.setenv("ANONYMIZER_BIN", str(script))

    suite = RedactionSuite()
    assert suite._image_redactor() is not None
    res = suite.redact_path(
        docx, RedactionOptions(output_dir=tmp_path / "out", docx_images="blur")
    )
    assert res.success, res.message
    assert "blurred" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.read("word/media/image1.png") == b"BLURRED"


def test_suite_does_not_mutate_the_caller_options(tmp_path, docx):
    from redact import RedactionSuite

    opts = RedactionOptions(output_dir=tmp_path / "out", docx_images="blur")
    RedactionSuite().redact_path(docx, opts)
    assert opts.extra == {}  # injection happens on a copy
