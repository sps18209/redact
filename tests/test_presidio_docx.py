"""Presidio driving .docx, exercised against a stand-in for the real library.

Presidio pulls in spaCy and is not installed in CI, so these tests inject fake
``presidio_analyzer``/``presidio_anonymizer`` modules and let the *real* adapter
code run against them. The fake recognizes labels the builtin regex engine can
never produce (PERSON), so a PERSON placeholder in the output proves Presidio's
detection actually drove the Word rewrite rather than the builtin fallback.
"""

import re
import sys
import types
import zipfile
import xml.etree.ElementTree as ET
from importlib.machinery import ModuleSpec

import pytest

from redact import RedactionOptions, RedactionSuite
from redact.backends.presidio import PresidioBackend
from redact.document import Document
from redact.types import MediaType, RedactionMode

from test_docx import W_NS, make_docx


class _Result:
    def __init__(self, entity_type, start, end, score):
        self.entity_type, self.start, self.end, self.score = entity_type, start, end, score


class _FakeAnalyzerEngine:
    """Finds two fixed phrases as PERSON — nothing the builtin engine detects."""

    PATTERN = re.compile(r"new text|click here")

    def __init__(self):
        type(self).instances = getattr(type(self), "instances", 0) + 1

    def analyze(self, text, language="en", entities=None, score_threshold=0.0):
        return [_Result("PERSON", m.start(), m.end(), 0.99) for m in self.PATTERN.finditer(text)]


class _FakeAnonymizerEngine:
    def anonymize(self, text, analyzer_results, operators):
        out, cur = "", 0
        for r in sorted(analyzer_results, key=lambda r: r.start):
            out += text[cur : r.start] + f"<{r.entity_type}>"
            cur = r.end
        return types.SimpleNamespace(text=out + text[cur:])


def _module(name, **attrs):
    m = types.ModuleType(name)
    m.__spec__ = ModuleSpec(name, loader=None)  # so importlib.find_spec sees it
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def fake_presidio(monkeypatch):
    import redact.backends.presidio as mod

    mod._ANALYZER_CACHE.clear()
    _FakeAnalyzerEngine.instances = 0
    entities_mod = _module(
        "presidio_anonymizer.entities",
        OperatorConfig=lambda name, params=None: (name, params),
    )
    monkeypatch.setitem(sys.modules, "presidio_analyzer", _module("presidio_analyzer", AnalyzerEngine=_FakeAnalyzerEngine))
    monkeypatch.setitem(sys.modules, "presidio_anonymizer", _module("presidio_anonymizer", AnonymizerEngine=_FakeAnonymizerEngine, entities=entities_mod))
    monkeypatch.setitem(sys.modules, "presidio_anonymizer.entities", entities_mod)
    yield
    mod._ANALYZER_CACHE.clear()


@pytest.fixture
def docx(tmp_path):
    return make_docx(tmp_path / "memo.docx")


def test_presidio_reports_unavailable_without_the_library():
    backend = PresidioBackend()
    if "presidio_analyzer" not in sys.modules:
        assert backend.missing_dependencies()
        assert not backend.supports.__self__.is_available()


def test_presidio_declares_docx_support():
    assert PresidioBackend().supports(MediaType.DOCX)


def test_presidio_is_available_with_fakes(fake_presidio):
    assert PresidioBackend().refresh_availability()


def test_presidio_drives_docx_rewrite(fake_presidio, tmp_path, docx):
    res = PresidioBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.backend == "presidio"
    assert res.output_path == tmp_path / "out" / "memo.redacted.docx"

    with zipfile.ZipFile(res.output_path) as zf:
        body = zf.read("word/document.xml")
    texts = [t.text for t in ET.fromstring(body).iter(f"{{{W_NS}}}t")]
    # Presidio's PERSON findings were applied ...
    assert "<PERSON>" in texts
    assert "new text" not in texts and "click here" not in texts
    # ... and the builtin engine did not also run (its labels are absent)
    assert b"EMAIL_ADDRESS" not in body
    assert {e.entity_type for e in res.entities} == {"PERSON", "DOCUMENT_AUTHOR"}


def test_presidio_docx_honours_mask_mode(fake_presidio, tmp_path, docx):
    res = PresidioBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX),
        RedactionOptions(output_dir=tmp_path / "out", mode=RedactionMode.MASK),
    )
    assert res.success
    with zipfile.ZipFile(res.output_path) as zf:
        texts = [t.text for t in ET.fromstring(zf.read("word/document.xml")).iter(f"{{{W_NS}}}t")]
    assert "*" * len("new text") in texts


def test_presidio_docx_honours_image_policy(fake_presidio, tmp_path, docx):
    res = PresidioBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX),
        RedactionOptions(output_dir=tmp_path / "out", docx_images="strip"),
    )
    assert res.success and "stripped" in res.message
    with zipfile.ZipFile(res.output_path) as zf:
        assert zf.read("word/media/image1.png").startswith(b"\x89PNG\r\n\x1a\n")


def test_presidio_docx_dry_run_writes_nothing(fake_presidio, tmp_path, docx):
    res = PresidioBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None and res.entities
    assert list(tmp_path.iterdir()) == [docx]


def test_router_prefers_presidio_over_builtin_for_docx(fake_presidio, tmp_path, docx):
    res = RedactionSuite().redact_path(docx, RedactionOptions(output_dir=tmp_path / "out"))
    assert res.backend == "presidio"  # priority 80 beats builtin's 10


def test_builtin_still_handles_docx_when_presidio_absent(tmp_path, docx):
    res = RedactionSuite().redact_path(docx, RedactionOptions(output_dir=tmp_path / "out"))
    assert res.backend == "builtin" and res.success


def test_analyzer_is_constructed_once_across_documents(fake_presidio, tmp_path, docx):
    backend = PresidioBackend()
    for _ in range(3):
        backend.redact(
            Document(path=docx, media_type=MediaType.DOCX),
            RedactionOptions(output_dir=tmp_path / "out"),
        )
    assert _FakeAnalyzerEngine.instances == 1  # models loaded once, not per file


def test_presidio_text_path_still_works(fake_presidio, tmp_path):
    src = tmp_path / "n.txt"
    src.write_text("say new text please")
    res = PresidioBackend().redact(
        Document(path=src, media_type=MediaType.TEXT),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success
    assert res.output_path.read_text() == "say <PERSON> please"
