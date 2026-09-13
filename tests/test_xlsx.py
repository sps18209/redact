"""Excel workbook redaction (stdlib zip + XML round-trip)."""

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from redact import RedactionOptions, RedactionSuite
from redact.backends.builtin import BuiltinBackend
from redact.document import Document, detect_media_type
from redact.types import MediaType, RedactionMode
from redact.xlsx import XlsxError, extract_text

S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
S = f"{{{S_NS}}}"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<workbook xmlns="{S_NS}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="People" sheetId="1" r:id="rId1"/>'
    '<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>'
)
WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    "</Relationships>"
)
# si 0: plain header; si 1: email split across rich-text runs (the Excel form of
# Word's split-run problem); si 2: SSN, referenced by TWO cells (dedupe).
SHARED_STRINGS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<sst xmlns="{S_NS}" count="4" uniqueCount="4">'
    "<si><t>Employee</t></si>"
    "<si><r><rPr><b/></rPr><t>jane</t></r><r><t>.doe@exa</t></r><r><t>mple.com</t></r></si>"
    "<si><t>123-45-6789</t></si>"
    "<si><t>Nothing sensitive.</t></si>"
    "</sst>"
)
SHEET1 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<worksheet xmlns="{S_NS}">'
    "<sheetData>"
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>2</v></c>'
    # a cached formula result carrying PII: rewriting <v> alone would be undone
    # by recalculation, so the formula must go
    '<c r="B2" t="str"><f>CONCATENATE(A9,"@x.io")</f><v>leak@x.io</v></c></row>'
    '<row r="3"><c r="A3"><v>42</v></c>'
    # inline string, not shared
    '<c r="B3" t="inlineStr"><is><t>call (415) 555-0199</t></is></c></row>'
    "</sheetData>"
    '<hyperlinks><hyperlink ref="B1" display="mailto:jane.doe@example.com"/></hyperlinks>'
    "<headerFooter><oddFooter>&amp;LPrepared for card 4111 1111 1111 1111</oddFooter></headerFooter>"
    "</worksheet>"
)
SHEET2 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<worksheet xmlns="{S_NS}"><sheetData>'
    '<row r="1"><c r="C7" t="s"><v>2</v></c><c r="D7" t="s"><v>3</v></c></row>'
    "</sheetData></worksheet>"
)
COMMENTS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<comments xmlns="{S_NS}"><authors><author>Carol White</author></authors>'
    '<commentList><comment ref="A1" authorId="0">'
    "<text><r><t>ping carol@example.com about this</t></r></text>"
    "</comment></commentList></comments>"
)
CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>Payroll</dc:title><dc:creator>Jane Doe</dc:creator></cp:coreProperties>"
)
IMAGE = b"\xff\xd8\xff\xe0" + bytes(range(48)) + b"\xff\xd9"  # placeholder jpeg


def make_xlsx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/sharedStrings.xml", SHARED_STRINGS)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET1)
        zf.writestr("xl/worksheets/sheet2.xml", SHEET2)
        zf.writestr("xl/comments1.xml", COMMENTS)
        zf.writestr("docProps/core.xml", CORE)
        zf.writestr("xl/media/image1.jpeg", IMAGE)
    return path


@pytest.fixture
def xlsx(tmp_path):
    return make_xlsx(tmp_path / "payroll.xlsx")


def _redact(xlsx, tmp_path, **kw):
    opts = RedactionOptions(output_dir=tmp_path / "out", **kw)
    res = BuiltinBackend().redact(Document(path=xlsx, media_type=MediaType.XLSX), opts)
    assert res.success, res.message
    return res


def _shared(zf):
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(S + "t")) for si in root.iter(S + "si")]


# -- detection -----------------------------------------------------------------

def test_media_type_by_extension_and_sniff(tmp_path, xlsx):
    assert detect_media_type(xlsx) is MediaType.XLSX
    assert detect_media_type(Path("book.xlsm")) is MediaType.XLSX
    blob = tmp_path / "noext"
    blob.write_bytes(xlsx.read_bytes())
    assert detect_media_type(blob) is MediaType.XLSX


def test_extract_text_covers_shared_and_inline(xlsx):
    text = extract_text(xlsx)
    assert "jane.doe@example.com" in text
    assert "123-45-6789" in text
    assert "call (415) 555-0199" in text


# -- the core traps --------------------------------------------------------------

def test_rich_text_split_across_runs_is_redacted(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    assert res.output_path == tmp_path / "out" / "payroll.redacted.xlsx"
    with zipfile.ZipFile(res.output_path) as zf:
        strings = _shared(zf)
        body = zf.read("xl/sharedStrings.xml")
    assert strings[1] == "<EMAIL_ADDRESS>"
    assert b"jane" not in body
    assert b"<b />" in body or b"<b/>" in body  # rich-run formatting survives


def test_deduped_string_redacts_every_referencing_cell_and_names_them(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        assert _shared(zf)[2] == "<US_SSN>"
    # one shared string, two cells across two sheets — both located by name
    note = next(n for n in res.message.split("; ") if "US_SSN" in n and " at " in n)
    assert "People!A2" in note and "Notes!C7" in note


def test_cached_formula_result_loses_its_formula(tmp_path, xlsx):
    """Rewriting only <v> is useless — Excel recalculates and restores the PII."""
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(sheet)
    b2 = next(c for c in root.iter(S + "c") if c.get("r") == "B2")
    assert b2.get("t") == "inlineStr"
    assert b2.find(S + "f") is None  # the formula is gone
    assert "".join(t.text or "" for t in b2.iter(S + "t")) == "<EMAIL_ADDRESS>"
    assert b"leak@x.io" not in sheet
    assert b"CONCATENATE" not in sheet


def test_inline_strings_footer_and_hyperlink_display(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        raw = zf.read("xl/worksheets/sheet1.xml")
    # Parse rather than string-match: placeholders are XML-escaped on disk
    # (<PHONE_NUMBER> is stored as &lt;PHONE_NUMBER&gt;).
    root = ET.fromstring(raw)
    assert b"555-0199" not in raw and b"4111 1111" not in raw

    b3 = next(c for c in root.iter(S + "c") if c.get("r") == "B3")
    assert "".join(t.text or "" for t in b3.iter(S + "t")) == "call <PHONE_NUMBER>"

    footer = next(root.iter(S + "oddFooter"))
    assert footer.text == "&LPrepared for card <CREDIT_CARD>"

    link = next(root.iter(S + "hyperlink"))
    assert link.get("display") == "mailto:<EMAIL_ADDRESS>"


def test_comments_and_authors_are_scrubbed(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        comments = zf.read("xl/comments1.xml")
    assert b"Carol White" not in comments
    assert b"carol@example.com" not in comments
    assert {e.entity_type for e in res.entities} >= {"DOCUMENT_AUTHOR", "EMAIL_ADDRESS"}


def test_docprops_creator_is_scrubbed(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        core = zf.read("docProps/core.xml")
    assert b"Jane Doe" not in core
    assert b"<dc:title>Payroll</dc:title>" in core  # non-PII metadata untouched


def test_untouched_cells_and_numbers_survive(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        assert _shared(zf)[0] == "Employee"
        assert _shared(zf)[3] == "Nothing sensitive."
        sheet = zf.read("xl/worksheets/sheet1.xml").decode()
    assert "<v>42</v>" in sheet  # numeric cells untouched


def test_package_stays_well_formed_and_ordered(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path)
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.namelist()[0] == "[Content_Types].xml"
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(zf.read(name))  # every part parses
        # default-namespace roots keep their form (the ns0: regression)
        for name in ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml", "[Content_Types].xml"):
            part = zf.read(name)
            assert b"ns0:" not in part, f"{name} was namespace-mangled"


# -- images, modes, plumbing -------------------------------------------------------

def test_image_policy_strip_renames_and_declares_png(tmp_path, xlsx):
    res = _redact(xlsx, tmp_path, docx_images="strip")
    assert "1 embedded image(s) stripped" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        names = zf.namelist()
        assert "xl/media/image1.png" in names and "xl/media/image1.jpeg" not in names
        assert zf.read("xl/media/image1.png").startswith(b"\x89PNG\r\n\x1a\n")
        assert 'Extension="png"' in zf.read("[Content_Types].xml").decode()


def test_mask_mode_and_entity_filter(tmp_path, xlsx):
    res = _redact(
        xlsx, tmp_path, mode=RedactionMode.MASK, entities=["EMAIL_ADDRESS"],
    )
    with zipfile.ZipFile(res.output_path) as zf:
        strings = _shared(zf)
    assert strings[1] == "*" * len("jane.doe@example.com")
    assert strings[2] == "123-45-6789"  # SSN untouched by the filter


def test_dry_run_detects_but_writes_nothing(tmp_path, xlsx):
    res = BuiltinBackend().redact(
        Document(path=xlsx, media_type=MediaType.XLSX), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None and res.entity_count >= 6
    assert list(tmp_path.iterdir()) == [xlsx]


def test_suite_routes_xlsx_and_reruns_are_idempotent(tmp_path, xlsx):
    suite = RedactionSuite()
    res = suite.redact_path(xlsx, RedactionOptions(output_dir=tmp_path / "o"))
    assert res.success and res.backend == "builtin"
    list(suite.redact_paths([str(tmp_path)]))
    list(suite.redact_paths([str(tmp_path)]))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o", "payroll.redacted.xlsx", "payroll.xlsx"]


def test_corrupt_xlsx_is_a_failed_result(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip")
    res = BuiltinBackend().redact(
        Document(path=bad, media_type=MediaType.XLSX), RedactionOptions()
    )
    assert res.success is False and "xlsx" in res.message
    with pytest.raises(XlsxError):
        extract_text(bad)


def test_presidio_driver_covers_xlsx():
    from redact.backends.presidio import PresidioBackend

    assert PresidioBackend().supports(MediaType.XLSX)
