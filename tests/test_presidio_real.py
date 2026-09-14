"""Behavioural tests against the REAL Presidio library.

Everything else in the suite fakes Presidio, because CI does not install it.
Fakes prove the adapter's plumbing, not the model's behaviour — and the claims
this project relies on are claims about the *model*: that it finds names the
regex engine cannot, and that it misses things the regex engine catches (which
is why the deterministic recognizers are unioned in).

Those claims were written from one-off measurements and never re-run. One of
them had drifted; see test_the_union_is_load_bearing and the module comment in
backends/presidio.py. Gated behind REDACT_TEST_PRESIDIO=1 and run by
.github/workflows/heavy.yml so drift surfaces in CI rather than in a user's
output.
"""

import os

import pytest

from redact.backends.builtin import detect_entities
from redact.document import Document
from redact.types import MediaType, RedactionOptions

REAL = os.environ.get("REDACT_TEST_PRESIDIO") == "1"
needs_presidio = pytest.mark.skipif(
    not REAL, reason="set REDACT_TEST_PRESIDIO=1 (and install presidio + a spaCy model)"
)

pytestmark = needs_presidio

EMAIL = "maria.g@clinic.example"
SSN = "123-45-6789"


@pytest.fixture(scope="module")
def backend():
    from redact.backends.presidio import PresidioBackend

    b = PresidioBackend()
    missing = b.missing_dependencies()
    if missing:
        pytest.skip(f"presidio not usable: {missing}")
    return b


@pytest.fixture(scope="module")
def raw_analyzer():
    """Presidio on its own, with no deterministic recognizers unioned in."""
    from presidio_analyzer import AnalyzerEngine

    from redact.backends.presidio import _get_analyzer

    return _get_analyzer(AnalyzerEngine)


def _run(backend, tmp_path, text, **kw):
    src = tmp_path / "note.txt"
    src.write_text(text)
    res = backend.redact(
        Document(path=src, media_type=MediaType.TEXT),
        RedactionOptions(output_dir=tmp_path / "out", **kw),
    )
    assert res.success, res.message
    return res, res.output_path.read_text()


# -- the reason the union exists ----------------------------------------------

def test_presidio_alone_does_not_find_the_tab_separated_ssn(raw_analyzer):
    """The measurement the union is justified by. If this ever starts passing,
    the union is still wanted, but this rationale needs rewriting."""
    text = f"SSN\t{SSN}"
    labels = {r.entity_type for r in raw_analyzer.analyze(text=text, language="en")}
    assert "US_SSN" not in labels
    assert {e.entity_type for e in detect_entities(text)} == {"US_SSN"}


def test_the_union_is_load_bearing(backend, tmp_path):
    """Installing something better must never find less."""
    res, out = _run(backend, tmp_path, f"SSN\t{SSN}")
    assert "US_SSN" in {e.entity_type for e in res.entities}
    assert SSN not in out


# -- what Presidio adds over the regex engine ---------------------------------

def test_presidio_finds_a_person_the_builtin_engine_cannot(backend, tmp_path):
    text = "Maria Gonzalez called."
    res, out = _run(backend, tmp_path, text)
    assert "PERSON" in {e.entity_type for e in res.entities}
    assert "Maria Gonzalez" not in out
    # the builtin engine is pattern-based and structurally cannot do this
    assert detect_entities(text) == []


# -- the regression that mattered most ----------------------------------------

def test_a_presidio_run_ships_nothing_unredacted(backend, tmp_path):
    """Presidio once wrote the SSN verbatim and reported success."""
    text = (
        f"Maria Gonzalez called from 555-867-5309.\nSSN\t{SSN}\n"
        f"Email {EMAIL} and see https://clinic.example/chart\n"
        "Card 4111 1111 1111 1111\n"
    )
    res, out = _run(backend, tmp_path, text)
    for secret in (SSN, EMAIL, "555-867-5309", "4111 1111 1111 1111", "Maria Gonzalez"):
        assert secret not in out, f"{secret!r} survived a Presidio run"


def test_overlapping_spans_do_not_interleave(backend, tmp_path):
    """Presidio reports an email as EMAIL_ADDRESS and as URLs inside it;
    rewriting naively produced <EMAIL_ADDRESS><URL>e@<URL>."""
    _, out = _run(backend, tmp_path, f"Email {EMAIL} now")
    assert "@" not in out and ">e" not in out


# -- a model label may swallow a deterministic one ----------------------------

def test_a_model_span_containing_a_regex_span_still_redacts_it(backend, tmp_path):
    """The exact-span tie-break only fires on *equal* spans. Here Presidio
    returns PERSON over 'Email <address>' — a strict superset of the
    EMAIL_ADDRESS span — so longest-wins keeps the model's label. The label is
    then unstable across contexts, but the address must still be redacted:
    over-redaction is the safe direction, under-redaction is not."""
    _, out = _run(backend, tmp_path, f"Email {EMAIL} today")
    assert EMAIL not in out


def test_entity_filtering_survives_that_collision(backend, tmp_path):
    """-e must not be defeated by the model preferring a different label:
    both detectors are filtered before the collision is resolved."""
    res, out = _run(backend, tmp_path, f"Email {EMAIL} today",
                    entities=["EMAIL_ADDRESS"])
    assert {e.entity_type for e in res.entities} == {"EMAIL_ADDRESS"}
    assert EMAIL not in out
