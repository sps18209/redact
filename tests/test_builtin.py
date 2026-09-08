from redact.backends.builtin import (
    BuiltinBackend,
    apply_redactions,
    detect_entities,
)
from redact.document import Document
from redact.types import MediaType, RedactionMode, RedactionOptions


def test_detect_common_entities():
    text = (
        "Email me at jane.doe@example.com or call (415) 555-0132. "
        "SSN 123-45-6789. Card 4111 1111 1111 1111."
    )
    ents = {e.entity_type for e in detect_entities(text)}
    assert "EMAIL_ADDRESS" in ents
    assert "PHONE_NUMBER" in ents
    assert "US_SSN" in ents
    assert "CREDIT_CARD" in ents


def test_credit_card_luhn_filtering():
    # 4111... is Luhn-valid; a random 16-digit run is not.
    good = detect_entities("pay 4111111111111111 now")
    assert any(e.entity_type == "CREDIT_CARD" for e in good)
    bad = detect_entities("id 1234567812345670000")
    # If matched at all it must pass Luhn; a clearly invalid run is dropped.
    assert not any(
        e.entity_type == "CREDIT_CARD" and e.text == "1234567812345670000" for e in bad
    )


def test_entity_filter_restricts():
    text = "a@b.com and 123-45-6789"
    ents = detect_entities(text, entities=["EMAIL_ADDRESS"])
    assert {e.entity_type for e in ents} == {"EMAIL_ADDRESS"}


def test_apply_replace_mode():
    text = "reach a@b.com"
    ents = detect_entities(text)
    out = apply_redactions(text, ents, RedactionOptions(mode=RedactionMode.REPLACE))
    assert out == "reach <EMAIL_ADDRESS>"


def test_apply_mask_mode():
    text = "reach a@b.com"
    ents = detect_entities(text)
    out = apply_redactions(text, ents, RedactionOptions(mode=RedactionMode.MASK))
    assert "a@b.com" not in out
    assert "*" in out


def test_apply_hash_mode_is_stable():
    text = "a@b.com"
    ents = detect_entities(text)
    o1 = apply_redactions(text, ents, RedactionOptions(mode=RedactionMode.HASH))
    o2 = apply_redactions(text, ents, RedactionOptions(mode=RedactionMode.HASH))
    assert o1 == o2 and "a@b.com" not in o1


def test_backend_writes_output(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("email a@b.com")
    doc = Document(path=src, media_type=MediaType.TEXT)
    backend = BuiltinBackend()
    res = backend.redact(doc, RedactionOptions(output_dir=tmp_path))
    assert res.success
    assert res.output_path.exists()
    assert "a@b.com" not in res.output_path.read_text()


def test_backend_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("email a@b.com")
    doc = Document(path=src, media_type=MediaType.TEXT)
    res = BuiltinBackend().redact(doc, RedactionOptions(dry_run=True))
    assert res.success
    assert res.output_path is None
    assert res.entity_count == 1


def test_builtin_always_available():
    assert BuiltinBackend().is_available()
