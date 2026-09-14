"""`redact verify` — re-scan a finished artifact for PII that survived.

The suite's governing principle is that a redaction tool must not tell you the
output is safe when it isn't. Everything else here enforces that at *write*
time, trusting the backend that did the work. This verb does not trust it: it
reopens the finished file and looks, independently of whatever produced it.

The failure it exists to prevent is the one that started the .eml work — a
user's own `grep` over a redacted mailbox returns nothing whether the file is
clean or whether the SSN is sitting in a base64 attachment. The search was
never capable of finding it, so a clean result meant nothing.
"""

import base64

import pytest

from redact.cli import main
from redact.types import MediaType
from redact.verify import extract_layers, verify_path

SSN = "078-05-1120"  # a structurally valid SSN; 9xx area numbers are never issued


def _eml_with_b64_attachment(path, secret=SSN):
    payload = base64.b64encode(f"Invoice for patient SSN {secret}".encode()).decode()
    path.write_text(
        "From: a@example.com\nSubject: hello\nMIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="B"\n\n'
        "--B\nContent-Type: text/plain\n\nnothing here\n"
        '--B\nContent-Type: application/pdf; name="inv.pdf"\n'
        "Content-Transfer-Encoding: base64\n"
        'Content-Disposition: attachment; filename="inv.pdf"\n\n'
        f"{payload}\n--B--\n"
    )
    return path


# -- the failure this verb exists for -----------------------------------------


def test_it_finds_pii_that_grep_cannot(tmp_path):
    """The base64 trap: invisible to a raw search, and the search's silence is
    indistinguishable from success."""
    src = _eml_with_b64_attachment(tmp_path / "leak.eml")
    assert SSN not in src.read_text(), "precondition: a raw search finds nothing"

    report = verify_path(src)
    assert not report.clean
    kinds = {f.entity.entity_type for f in report.findings}
    assert "US_SSN" in kinds
    assert any(f.layer.startswith("base64") for f in report.findings)


def test_a_genuinely_clean_file_is_reported_clean(tmp_path):
    """Crying wolf destroys the verb: an ignored verifier is worse than none."""
    path = tmp_path / "ok.txt"
    path.write_text("mail <EMAIL_ADDRESS> ssn <US_SSN>")
    assert verify_path(path).clean


# -- layer coverage -----------------------------------------------------------


def test_every_zip_part_is_scanned_not_just_edited_ones(tmp_path):
    """A leak in a part no backend touches must still be caught."""
    import zipfile

    path = tmp_path / "thing.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", "<w:t>clean</w:t>")
        z.writestr("customXml/item1.xml", f"<x>ssn {SSN}</x>")  # nothing edits this

    layers = extract_layers(path, MediaType.DOCX)
    assert any("customXml" in name for name in layers)
    report = verify_path(path)
    assert not report.clean
    assert any("customXml" in f.layer for f in report.findings)


def test_office_namespace_urls_are_not_reported_as_leaks(tmp_path):
    """Every OOXML package carries a dozen schema URLs. Reporting them would
    flag every Office document ever redacted."""
    import zipfile

    path = tmp_path / "ns.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        z.writestr("word/document.xml", '<w:t xmlns:w="http://www.w3.org/1999/xhtml">hi</w:t>')
    assert verify_path(path).clean


def test_a_real_url_is_still_reported(tmp_path):
    """The namespace filter matches hosts, so it must not swallow real URLs."""
    path = tmp_path / "u.txt"
    path.write_text("see https://clinic.example/patient/12345")
    report = verify_path(path)
    assert any(f.entity.entity_type == "URL" for f in report.findings)


# -- the positive control -----------------------------------------------------


def test_a_control_that_finds_nothing_makes_the_result_inconclusive(tmp_path):
    """If the scan cannot see PII in the original, a clean verdict on the
    redacted copy proves nothing about it."""
    original = tmp_path / "plain.txt"
    original.write_text("nothing sensitive at all")
    redacted = tmp_path / "plain.redacted.txt"
    redacted.write_text("nothing sensitive at all")

    report = verify_path(redacted, original=original)
    assert report.clean
    assert report.inconclusive
    assert report.status == "INCONCLUSIVE"
    assert "proves nothing" in report.note


def test_a_control_that_finds_pii_makes_a_clean_result_meaningful(tmp_path):
    original = tmp_path / "n.txt"
    original.write_text(f"ssn {SSN}")
    redacted = tmp_path / "n.redacted.txt"
    redacted.write_text("ssn <US_SSN>")

    report = verify_path(redacted, original=original)
    assert report.clean and not report.inconclusive
    assert report.control_entities == 1
    assert report.status == "clean"


# -- CLI contract -------------------------------------------------------------


def test_cli_exits_nonzero_on_a_leak(tmp_path, capsys):
    _eml_with_b64_attachment(tmp_path / "leak.eml")
    rc = main(["verify", str(tmp_path / "leak.eml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "NOT safe to share" in err


def test_cli_exits_nonzero_when_inconclusive(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("nothing here")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("nothing here")
    rc = main(["verify", str(tmp_path / "a.txt"), "--original", str(src)])
    assert rc == 1
    assert "Treat as unverified" in capsys.readouterr().err


def test_cli_exits_zero_on_a_clean_artifact(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("mail <EMAIL_ADDRESS>")
    assert main(["verify", str(tmp_path / "a.txt")]) == 0


def test_verify_reads_redaction_outputs(tmp_path, capsys):
    """Ingestion normally skips `.redacted.` files to keep batches idempotent.
    Verification's entire input is those files."""
    out = tmp_path / "x.redacted.txt"
    out.write_text(f"ssn {SSN}")
    rc = main(["verify", str(tmp_path)])
    assert rc == 1, "a redacted artifact must not be skipped by the verifier"
    assert "x.redacted.txt" in capsys.readouterr().out


def test_a_bad_original_directory_is_a_usage_error(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hi")
    rc = main(["verify", str(tmp_path / "a.txt"), "--original", str(tmp_path / "nope")])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


# -- PDF structure must not read as PII ---------------------------------------

def test_pdf_structure_is_not_reported_as_pii(tmp_path):
    """Two shapes in every PDF match PII patterns:

    * the cross-reference table — ten-digit zero-padded offsets, which look
      exactly like phone numbers (two adjacent ones like a card);
    * the trailer's document ID — two random hex strings, and a 32-char random
      hex run matches the IBAN shape roughly a fifth of the time.

    The ID is *random*, so a single save catches it only sometimes; this
    asserted clean on one PDF and failed about 20% of runs, which read as a
    flaky test when the flaky thing was the input. Saving many makes it
    deterministic: at n=25 the odds of missing a 20% effect are ~0.4%.
    """
    pymupdf = pytest.importorskip("pymupdf")
    offenders = []
    for i in range(25):
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), "nothing sensitive here", fontsize=11)
        path = tmp_path / f"plain{i}.pdf"
        doc.save(str(path))
        doc.close()
        if i == 0:
            assert b"00000 n" in path.read_bytes(), "precondition: xref table present"
            assert b"/ID" in path.read_bytes(), "precondition: document ID present"
        report = verify_path(path)
        if not report.clean:
            offenders.append([str(f) for f in report.findings])
    assert not offenders, f"structural noise reported as PII: {offenders[:3]}"


def test_a_real_number_in_pdf_content_is_still_reported(tmp_path):
    """The filter matches only the xref shape, so page content is untouched."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), f"patient ssn {SSN}", fontsize=11)
    path = tmp_path / "leak.pdf"
    doc.save(str(path))
    doc.close()

    report = verify_path(path)
    assert any(f.entity.entity_type == "US_SSN" for f in report.findings)


# -- MIME wraps base64 at 76 characters ---------------------------------------

def test_a_value_straddling_a_base64_line_break_is_found(tmp_path):
    """Matching single lines (^...$) decoded each 76-char line into disjoint
    57-byte chunks, so a value spanning the boundary was split in half and never
    found — roughly one in five, in this verb's headline feature."""
    hits = []
    for pad in (48, 52, 56, 60):
        plain = ("word " * 40)[:pad] + f" ssn {SSN} tail"
        payload = base64.b64encode(plain.encode()).decode()
        wrapped = "\n".join(payload[i:i + 76] for i in range(0, len(payload), 76))
        path = tmp_path / f"b{pad}.eml"
        path.write_text(
            "From: a@b.com\nSubject: x\nMIME-Version: 1.0\n"
            'Content-Type: application/pdf; name="x.pdf"\n'
            "Content-Transfer-Encoding: base64\n\n" + wrapped + "\n"
        )
        assert len(wrapped.splitlines()) > 1, "precondition: the payload wraps"
        hits.append(any(f.entity.entity_type == "US_SSN" for f in verify_path(path).findings))
    assert all(hits), f"missed at offsets {[p for p, h in zip((48,52,56,60), hits) if not h]}"


def test_a_host_is_matched_as_a_prefix_not_a_character_set(tmp_path):
    """`lstrip("www.")` strips a character *set*: "www.w3.org" became "3.org",
    and a real host like "w.sun.com" was suppressed as namespace noise."""
    from redact.types import Entity
    from redact.verify import _is_structural

    assert _is_structural(Entity("URL", 1.0, None, None, "http://www.w3.org/x"))
    assert not _is_structural(Entity("URL", 1.0, None, None, "https://w.sun.com/x"))


def test_an_unreadable_pdf_is_inconclusive_not_clean(tmp_path, monkeypatch):
    """Format extraction swallowed every exception, leaving no layer AND no
    marker, so verify printed [clean] having searched only compressed bytes."""
    import redact.verify as mod

    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    monkeypatch.setattr(mod, "_pdf_text", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))

    report = verify_path(path)
    assert report.inconclusive, "an unscannable file must not be reported clean"
    assert report.status == "INCONCLUSIVE"
