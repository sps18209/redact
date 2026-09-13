"""Adapter for Microsoft Presidio (text & structured data PII detection).

Presidio combines NLP models (spaCy/transformers) with rule-based recognizers.
It is the strongest text backend when installed, so it is given a high priority
in the router. The heavy imports happen lazily inside ``redact``/discovery so
that merely importing this module never pulls in spaCy.

Install with:  ``pip install "redact-suite[presidio]"`` then
``python -m spacy download en_core_web_lg``. Model choice changes *labels* in
both directions rather than being strictly better — ``en_core_web_lg`` tags
"maria.g@clinic.example" as a PERSON where ``en_core_web_sm`` correctly says
EMAIL_ADDRESS — which is why the deterministic recognizers below win exact span
collisions.

Presidio supplies *detection* only; the suite's own operators do the rewriting,
so redaction modes behave identically across every backend and media type.
"""

from __future__ import annotations

import importlib.util
from typing import List

from ..document import Document, output_path
from ..types import (
    Entity,
    MediaType,
    RedactionMode,
    RedactionOptions,
    RedactionResult,
)
from ..opc import resolve_overlaps
from .base import Backend
from .builtin import (
    _OFFICE_MEDIA,
    apply_redactions,
    detect_entities,
    redact_office_document,
)


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class PresidioBackend(Backend):
    name = "presidio"
    description = "Microsoft Presidio — NLP + rules PII detection/anonymization for text (MIT)."
    supported_media_types = (
        MediaType.TEXT, MediaType.STRUCTURED, MediaType.DOCX, MediaType.XLSX,
        MediaType.PPTX, MediaType.EMAIL,
    )
    install_hint = (
        'pip install "redact-suite[presidio]" && python -m spacy download en_core_web_lg'
    )
    priority = 80  # beats the builtin engine when installed

    # Map the suite's neutral modes onto Presidio anonymizer operators.
    _OPERATOR = {
        RedactionMode.MASK: "mask",
        RedactionMode.REPLACE: "replace",
        RedactionMode.HASH: "hash",
        RedactionMode.REDACT: "redact",
    }

    def missing_dependencies(self) -> List[str]:
        missing = []
        if not _module_present("presidio_analyzer"):
            missing.append("presidio-analyzer")
        return missing

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        missing = self.missing_dependencies()
        if missing:
            return RedactionResult(
                source=document.path, backend=self.name,
                media_type=document.media_type, success=False,
                message=f"missing dependencies: {', '.join(missing)}",
            )

        from presidio_analyzer import AnalyzerEngine  # lazy, heavy

        analyzer = _get_analyzer(AnalyzerEngine)
        detect = lambda text: _analyze(analyzer, text, options)  # noqa: E731

        # Every media type goes through the same detection, and the suite's own
        # operators do the rewriting. Office rewriting needs a replacement *per
        # entity* (each lands in the run where it starts) and plain text must
        # agree with it, so there is deliberately no second code path here:
        # an earlier version let Presidio's AnonymizerEngine handle text, which
        # skipped the deterministic union below and shipped a .txt file with the
        # SSN still in it.
        if document.media_type in _OFFICE_MEDIA:
            return redact_office_document(self.name, document, options, detect=detect)

        try:
            text = document.read_text()
        except OSError as exc:
            return RedactionResult(
                source=document.path, backend=self.name,
                media_type=document.media_type, success=False,
                message=f"could not read file: {exc}",
            )

        entities = detect(text)
        redacted = apply_redactions(text, entities, options)

        result = RedactionResult(
            source=document.path, backend=self.name,
            media_type=document.media_type, entities=entities,
            redacted_text=redacted,
        )
        if options.dry_run:
            result.message = "dry-run: detected only, nothing written"
            return result

        out = output_path(document, options)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(redacted, encoding="utf-8")
        except OSError as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result
        result.output_path = out
        return result


def _analyze(analyzer, text: str, options: RedactionOptions) -> List[Entity]:
    """Run Presidio over one segment and translate to the suite's Entity type.

    The deterministic recognizers are unioned in. Presidio is a *model*, and a
    model misses things a regex does not: measured here with ``en_core_web_sm``,
    Presidio returned nothing at all for ``SSN\t123-45-6789`` while the builtin
    engine matched it. Since Presidio outranks builtin in the router, using it
    would otherwise *lose* detections — a redaction tool must never find less
    because you installed something better. Overlaps between the two are
    resolved by ``opc.resolve_overlaps`` (longest span wins).
    """
    found = [
        Entity(
            entity_type=r.entity_type,
            score=float(r.score),
            start=r.start,
            end=r.end,
            text=text[r.start : r.end],
        )
        for r in analyzer.analyze(
            text=text,
            language=options.language,
            entities=options.entities,
            score_threshold=options.threshold,
        )
    ]
    deterministic = detect_entities(text, options.entities, options.threshold)

    # On an *exact* span collision the deterministic label wins. A validated
    # regex match is a fact; the model's label is a guess, and which guess you
    # get depends on the model: measured here, en_core_web_sm calls
    # "maria.g@clinic.example" an EMAIL_ADDRESS while en_core_web_lg calls the
    # same span a PERSON. Both redact it, but a label that flips with the model
    # makes reports and ``-e`` filtering unreliable.
    exact = {(e.start, e.end) for e in deterministic}
    found = [e for e in found if (e.start, e.end) not in exact]
    found.extend(deterministic)
    return resolve_overlaps(found)


# Analyzer construction is expensive (loads NLP models); cache one per process.
_ANALYZER_CACHE = {}


def _get_analyzer(analyzer_cls):
    if "instance" not in _ANALYZER_CACHE:
        _ANALYZER_CACHE["instance"] = analyzer_cls()
    return _ANALYZER_CACHE["instance"]
