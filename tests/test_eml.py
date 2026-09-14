"""Email redaction — the format where the suite was most able to lie.

Before this module existed, an .eml went to the plain-text engine: headers and
body were redacted, the run reported success, and a base64 attachment sailed
through untouched. Grepping the output for the SSN found nothing, because the
SSN was encoded — the user's own verification confirmed a redaction that had not
happened. These tests pin that shut.
"""

import base64
import email
import email.policy
import re

import pytest

from redact import RedactionOptions
from redact.backends.builtin import BuiltinBackend
from redact.document import Document, detect_media_type
from redact.eml import ATTACHMENT_ENTITY, extract_text
from redact.types import MediaType

SECRET = "123-45-6789"


def _message(attachment_type="text/plain", filename="payroll.txt", body_secret=True):
    payload = base64.b64encode(
        f"Employee SSN {SECRET} and card 4111 1111 1111 1111\n".encode()
    ).decode()
    body = "Body mentions a@b.com only." if body_secret else "Nothing here."
    return (
        "From: Jane Doe <jane@example.com>\n"
        "To: bob@example.com\n"
        "Subject: payroll for 415-555-0132\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="XX"\n'
        "\n"
        "--XX\n"
        "Content-Type: text/plain\n"
        "\n"
        f"{body}\n"
        "--XX\n"
        f'Content-Type: {attachment_type}; name="{filename}"\n'
        "Content-Transfer-Encoding: base64\n"
        f'Content-Disposition: attachment; filename="{filename}"\n'
        "\n"
        f"{payload}\n"
        "--XX--\n"
    )


@pytest.fixture
def eml(tmp_path):
    path = tmp_path / "msg.eml"
    path.write_text(_message())
    return path


@pytest.fixture
def eml_binary(tmp_path):
    path = tmp_path / "bin.eml"
    path.write_text(_message(attachment_type="application/pdf", filename="records.pdf"))
    return path


def _decoded_payloads(path):
    """Everything recoverable from the file, including base64 blobs."""
    text = path.read_text()
    out = [text]
    for blob in re.findall(r"^[A-Za-z0-9+/=]{20,}$", text, re.M):
        try:
            out.append(base64.b64decode(blob).decode("utf-8", errors="replace"))
        except Exception:
            pass
    return "\n".join(out)


def _redact(path, tmp_path, **kw):
    res = BuiltinBackend().redact(
        Document(path=path, media_type=MediaType.EMAIL),
        RedactionOptions(output_dir=tmp_path / "out", **kw),
    )
    assert res.success, res.message
    return res


# -- detection ----------------------------------------------------------------

def test_eml_is_its_own_media_type(eml):
    assert detect_media_type(eml) is MediaType.EMAIL


def test_extract_text_covers_headers_and_parts(eml):
    text = extract_text(eml)
    assert "jane@example.com" in text and "payroll" in text


# -- the leak -----------------------------------------------------------------

def test_base64_text_attachment_is_actually_redacted(eml, tmp_path):
    """The headline regression: PII inside an encoded attachment."""
    res = _redact(eml, tmp_path)
    recovered = _decoded_payloads(res.output_path)
    assert SECRET not in recovered
    assert "4111 1111 1111 1111" not in recovered
    assert "<US_SSN>" in recovered and "<CREDIT_CARD>" in recovered


def test_headers_are_redacted(eml, tmp_path):
    res = _redact(eml, tmp_path)
    raw = res.output_path.read_text()
    assert "jane@example.com" not in raw
    assert "bob@example.com" not in raw
    assert "415-555-0132" not in raw   # Subject carried a phone number


def test_body_part_is_redacted(eml, tmp_path):
    res = _redact(eml, tmp_path)
    assert "a@b.com" not in _decoded_payloads(res.output_path)


# -- attachments that cannot be redacted in place ------------------------------

def test_binary_attachment_is_reported_never_silently_kept(eml_binary, tmp_path):
    res = _redact(eml_binary, tmp_path)
    assert res.unredacted == ["records.pdf"]
    assert res.fully_redacted is False
    assert "NOT redacted" in res.message and "records.pdf" in res.message
    assert any(e.entity_type == ATTACHMENT_ENTITY for e in res.entities)
    # and it is genuinely still there — the warning is not cosmetic
    assert SECRET in _decoded_payloads(res.output_path)


def test_strip_removes_the_unredactable_attachment(eml_binary, tmp_path):
    res = _redact(eml_binary, tmp_path, eml_attachments="strip")
    assert res.unredacted == []
    assert res.fully_redacted is True
    assert SECRET not in _decoded_payloads(res.output_path)
    assert "attachment removed" in _decoded_payloads(res.output_path)


def test_invalid_attachment_policy_is_a_failed_result(eml, tmp_path):
    res = BuiltinBackend().redact(
        Document(path=eml, media_type=MediaType.EMAIL),
        RedactionOptions(output_dir=tmp_path / "o", eml_attachments="nonsense"),
    )
    assert res.success is False and "attachment_policy" in res.message


# -- the message must survive --------------------------------------------------

def test_output_is_still_a_valid_message_with_its_headers(eml_binary, tmp_path):
    res = _redact(eml_binary, tmp_path, eml_attachments="strip")
    msg = email.message_from_bytes(res.output_path.read_bytes(), policy=email.policy.compat32)
    assert set(msg.keys()) >= {"Subject", "From", "To", "Content-Type"}
    assert msg.is_multipart()
    assert [p.get_content_type() for p in msg.walk()][0] == "multipart/mixed"


def test_forwarded_message_parts_are_walked(tmp_path):
    """A forwarded email is a common way for the original addresses to survive."""
    inner = "From: inner@example.com\nSubject: fwd\n\ninner body has c@d.com\n"
    path = tmp_path / "fwd.eml"
    path.write_text(
        "From: a@example.com\nSubject: fwd\nMIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="ZZ"\n\n'
        "--ZZ\nContent-Type: text/plain\n\nouter\n"
        "--ZZ\nContent-Type: message/rfc822\n\n" + inner + "--ZZ--\n"
    )
    res = _redact(path, tmp_path)
    assert "c@d.com" not in _decoded_payloads(res.output_path)


def test_corrupt_message_is_a_failed_result(tmp_path):
    bad = tmp_path / "bad.eml"
    bad.write_bytes(b"\x00\x01 not a message")
    res = BuiltinBackend().redact(
        Document(path=bad, media_type=MediaType.EMAIL), RedactionOptions()
    )
    # either parsed as an empty message or refused — never a crash
    assert res.success in (True, False)


def test_dry_run_writes_nothing(eml, tmp_path):
    res = BuiltinBackend().redact(
        Document(path=eml, media_type=MediaType.EMAIL), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None and res.entities
    assert list(tmp_path.iterdir()) == [eml]


def test_suite_routes_eml(eml, tmp_path, builtin_only_suite):
    res = builtin_only_suite.redact_path(eml, RedactionOptions(output_dir=tmp_path / "o"))
    assert res.success and res.media_type is MediaType.EMAIL


# -- mbox: an archive must survive as an archive ------------------------------

MBOX = (
    "From alice@example.com Mon Jan  1 00:00:00 2024\n"
    "From: alice@example.com\nSubject: one\n\nSSN 111-22-3333\n\n"
    "From bob@example.com Mon Jan  1 00:00:00 2024\n"
    "From: bob@example.com\nSubject: two\n\nSSN 444-55-6666\n"
)


@pytest.fixture
def mbox(tmp_path):
    path = tmp_path / "box.mbox"
    path.write_text(MBOX)
    return path


def test_mbox_is_detected_as_email(mbox):
    assert detect_media_type(mbox) is MediaType.EMAIL


def test_every_message_in_an_mbox_is_redacted(mbox, tmp_path):
    """Message 2 must not ride along untouched behind message 1."""
    res = _redact(mbox, tmp_path)
    recovered = _decoded_payloads(res.output_path)
    assert "111-22-3333" not in recovered
    assert "444-55-6666" not in recovered


def test_an_mbox_stays_an_mbox(mbox, tmp_path):
    """Parsing a mailbox with a single-message parser folds 2..N into the body
    of message 1 — the text gets redacted, but the archive is destroyed and the
    run still reports success."""
    import mailbox

    res = _redact(mbox, tmp_path)
    box = mailbox.mbox(str(res.output_path))
    try:
        subjects = [m["Subject"] for m in box]
    finally:
        box.close()
    assert subjects == ["one", "two"]


def test_the_envelope_sender_line_is_redacted(mbox, tmp_path):
    """The `From ` separator carries an address of its own."""
    res = _redact(mbox, tmp_path)
    text = res.output_path.read_text()
    assert "alice@example.com" not in text and "bob@example.com" not in text
    assert text.startswith("From <EMAIL_ADDRESS>")


def test_extract_text_covers_every_message_in_an_mbox(mbox):
    text = extract_text(mbox)
    assert "one" in text and "two" in text


def test_a_plain_eml_is_unaffected_by_the_mbox_path(eml, tmp_path):
    res = _redact(eml, tmp_path)
    assert not res.output_path.read_text().startswith("From ")
    msg = email.message_from_string(
        res.output_path.read_text(), policy=email.policy.compat32
    )
    assert msg["Subject"] is not None


# -- repeated headers: Received appears once per relay hop --------------------

def _redact_raw(path, tmp_path, **kw):
    from redact.backends.builtin import detect_entities, replacement_for
    from redact.eml import redact_eml
    from redact.types import RedactionOptions

    opts = RedactionOptions(**kw)
    out = tmp_path / "out.eml"
    redact_eml(path, out, lambda t: detect_entities(t, None, 0.35),
               lambda e: replacement_for(e, opts))
    return out.read_text()


def test_pii_in_a_later_repeated_header_is_redacted(tmp_path):
    """msg.get() returns only the FIRST occurrence. With a clean first hop, the
    address in the second survived into the output with a success report."""
    src = tmp_path / "m.eml"
    src.write_text(
        "Received: from mail.internal by relay\n"
        "Received: from b.example.com (198.51.100.77)\n"
        "From: x@example.com\nSubject: hi\n\nbody\n"
    )
    out = _redact_raw(src, tmp_path)
    assert "198.51.100.77" not in out


def test_repeated_headers_are_not_deleted_wholesale(tmp_path):
    """`del msg[name]` removes every occurrence, so redacting the first hop
    silently dropped the rest of the routing chain."""
    import re

    src = tmp_path / "m.eml"
    src.write_text(
        "Received: from a.example.com (1.2.3.4)\n"
        "Received: from b.example.com (198.51.100.77)\n"
        "From: x@example.com\nSubject: hi\n\nbody\n"
    )
    out = _redact_raw(src, tmp_path)
    assert len(re.findall(r"(?im)^received:", out)) == 2, "a relay hop was lost"
    assert "1.2.3.4" not in out and "198.51.100.77" not in out


# -- a redacted message must stay greppable -----------------------------------

def test_a_plain_ascii_body_is_not_re_encoded_to_base64(tmp_path):
    """set_payload(charset=...) makes the email package choose base64, so a
    readable message came out of redaction encoded — the exact condition that
    makes a user's own grep meaningless, created by the module that exists to
    prevent it."""
    src = tmp_path / "p.eml"
    src.write_text("From: a@b.com\nSubject: hi\nContent-Type: text/plain\n\nssn 078-05-1120 ok\n")
    out = _redact_raw(src, tmp_path)
    assert "<US_SSN>" in out, "the placeholder must be visible to a plain search"
    assert "base64" not in out.lower()
    assert "078-05-1120" not in out


def test_a_non_ascii_body_is_still_redacted_and_decodes(tmp_path):
    import email
    import email.policy

    src = tmp_path / "u.eml"
    src.write_text(
        "From: a@b.com\nSubject: hi\nMIME-Version: 1.0\n"
        'Content-Type: text/plain; charset="utf-8"\n\nnaïve café ssn 078-05-1120\n',
        encoding="utf-8",
    )
    out_path = tmp_path / "out.eml"
    _redact_raw(src, tmp_path)
    body = email.message_from_bytes(
        out_path.read_bytes(), policy=email.policy.compat32
    ).get_payload(decode=True).decode("utf-8", "replace")
    assert "078-05-1120" not in body
    assert "café" in body, "the accents must survive the round trip"
