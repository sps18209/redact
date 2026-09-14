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
from redact.backends.builtin import MASK_WIDTH
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


def test_presidio_reports_unavailable_without_the_library(monkeypatch):
    """Simulate absence rather than keying off whatever happens to be installed."""
    import redact.backends.presidio as mod

    monkeypatch.setattr(mod, "_module_present", lambda name: False)
    backend = PresidioBackend()
    assert backend.missing_dependencies() == ["presidio-analyzer"]
    assert backend.refresh_availability() is False


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
    # Presidio's PERSON findings were applied — a label the builtin engine
    # cannot produce, so Presidio genuinely drove the run ...
    assert "<PERSON>" in texts
    assert "new text" not in texts and "click here" not in texts
    labels = {e.entity_type for e in res.entities}
    assert "PERSON" in labels and "DOCUMENT_AUTHOR" in labels
    # ... *and* the deterministic recognizers are unioned in, so upgrading to
    # Presidio never finds less than the builtin engine would have.
    assert "EMAIL_ADDRESS" in labels
    assert b"jane.doe@example.com" not in body


def test_presidio_docx_honours_mask_mode(fake_presidio, tmp_path, docx):
    res = PresidioBackend().redact(
        Document(path=docx, media_type=MediaType.DOCX),
        RedactionOptions(output_dir=tmp_path / "out", mode=RedactionMode.MASK),
    )
    assert res.success
    with zipfile.ZipFile(res.output_path) as zf:
        texts = [t.text for t in ET.fromstring(zf.read("word/document.xml")).iter(f"{{{W_NS}}}t")]
    assert "*" * MASK_WIDTH in texts


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


def test_builtin_still_handles_docx_when_presidio_absent(tmp_path, docx, builtin_only_suite):
    """Without Presidio the builtin engine must still cover .docx."""
    res = builtin_only_suite.redact_path(docx, RedactionOptions(output_dir=tmp_path / "out"))
    assert res.backend == "builtin" and res.success


def test_presidio_outranks_builtin_for_office_formats():
    """The routing policy itself, independent of what is installed."""
    from redact.backends.builtin import BuiltinBackend

    assert PresidioBackend.priority > BuiltinBackend.priority
    for media in (MediaType.TEXT, MediaType.DOCX, MediaType.XLSX):
        assert PresidioBackend().supports(media) and BuiltinBackend().supports(media)


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


# -- overlap resolution & deterministic union ---------------------------------

class _OverlappingAnalyzer:
    """Returns overlapping spans, as real Presidio does for an email."""

    def analyze(self, text, language="en", entities=None, score_threshold=0.0):
        return [
            _Result("EMAIL_ADDRESS", 9, 29, 1.0),
            _Result("URL", 9, 16, 0.5),   # inside the email
            _Result("URL", 18, 29, 0.5),  # also inside the email
        ]


def test_overlapping_spans_are_resolved_longest_first(fake_presidio):
    """Naive rewriting of overlaps produced '<EMAIL_ADDRESS><URL>e@<URL>'."""
    from redact.backends.presidio import _analyze
    from redact.types import RedactionOptions

    found = _analyze(_OverlappingAnalyzer(), "Contact: jane.doe@example.com", RedactionOptions())
    spans = [(e.entity_type, e.start, e.end) for e in found]
    assert ("EMAIL_ADDRESS", 9, 29) in spans
    assert not any(t == "URL" for t, _, _ in spans)  # swallowed by the longer span
    # and nothing overlaps anything else
    ordered = sorted(found, key=lambda e: e.start)
    assert all(a.end <= b.start for a, b in zip(ordered, ordered[1:]))


class _BlindAnalyzer:
    """A model that misses a pattern the deterministic engine catches."""

    def analyze(self, text, language="en", entities=None, score_threshold=0.0):
        return []


def test_presidio_never_loses_deterministic_detections(fake_presidio):
    """Presidio outranks builtin, so it must not detect *less* than builtin."""
    from redact.backends.presidio import _analyze
    from redact.types import RedactionOptions

    text = "SSN\t123-45-6789 and a@b.com"
    found = _analyze(_BlindAnalyzer(), text, RedactionOptions())
    labels = {e.entity_type for e in found}
    assert "US_SSN" in labels and "EMAIL_ADDRESS" in labels


def test_union_respects_the_entity_filter(fake_presidio):
    from redact.backends.presidio import _analyze
    from redact.types import RedactionOptions

    found = _analyze(
        _BlindAnalyzer(), "SSN 123-45-6789 and a@b.com",
        RedactionOptions(entities=["EMAIL_ADDRESS"]),
    )
    assert {e.entity_type for e in found} == {"EMAIL_ADDRESS"}


class _MislabellingAnalyzer:
    """Tags an email span as PERSON, as en_core_web_lg actually does."""

    def analyze(self, text, language="en", entities=None, score_threshold=0.0):
        i = text.index("a@b.com")
        return [_Result("PERSON", i, i + 7, 0.99)]


def test_deterministic_label_wins_an_exact_span_collision(fake_presidio):
    """NER labels flip between spaCy models; a validated regex match does not."""
    from redact.backends.presidio import _analyze
    from redact.types import RedactionOptions

    found = _analyze(_MislabellingAnalyzer(), "write to a@b.com now", RedactionOptions())
    assert [e.entity_type for e in found] == ["EMAIL_ADDRESS"]
    assert "PERSON" not in {e.entity_type for e in found}
