"""Adapter for Microsoft Presidio (text & structured data PII detection).

Presidio combines NLP models (spaCy/transformers) with rule-based recognizers.
It is the strongest text backend when installed, so it is given a high priority
in the router. The heavy imports happen lazily inside ``redact``/discovery so
that merely importing this module never pulls in spaCy.

Install with:  ``pip install "redact-suite[presidio]"`` then
``python -m spacy download en_core_web_lg``
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
from .base import Backend
from .builtin import redact_office_document


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
        if not _module_present("presidio_anonymizer"):
            missing.append("presidio-anonymizer")
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

        if document.media_type in (MediaType.DOCX, MediaType.XLSX):
            # Word rewriting needs a replacement *per entity* (each lands in the
            # run where it starts), so Presidio supplies detection and the
            # suite's own operators do the rewriting — modes stay identical
            # across backends.
            analyzer = _get_analyzer(AnalyzerEngine)
            return redact_office_document(
                self.name, document, options,
                detect=lambda text: _analyze(analyzer, text, options),
            )

        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig

        try:
            text = document.read_text()
        except OSError as exc:
            return RedactionResult(
                source=document.path, backend=self.name,
                media_type=document.media_type, success=False,
                message=f"could not read file: {exc}",
            )

        analyzer = _get_analyzer(AnalyzerEngine)
        results = analyzer.analyze(
            text=text,
            language=options.language,
            entities=options.entities,  # None => all
            score_threshold=options.threshold,
        )

        entities = [
            Entity(
                entity_type=r.entity_type,
                score=float(r.score),
                start=r.start,
                end=r.end,
                text=text[r.start : r.end],
            )
            for r in results
        ]

        operator = self._OPERATOR.get(options.mode, "replace")
        op_params = {}
        if operator == "mask":
            op_params = {
                "masking_char": options.mask_char,
                "chars_to_mask": 100,
                "from_end": False,
            }
        anonymizer = AnonymizerEngine()
        anonymized = anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators={"DEFAULT": OperatorConfig(operator, op_params)},
        )

        result = RedactionResult(
            source=document.path, backend=self.name,
            media_type=document.media_type, entities=entities,
            redacted_text=anonymized.text,
        )
        if options.dry_run:
            result.message = "dry-run: detected only, nothing written"
            return result

        out = output_path(document, options)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(anonymized.text, encoding="utf-8")
        except OSError as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result
        result.output_path = out
        return result


def _analyze(analyzer, text: str, options: RedactionOptions) -> List[Entity]:
    """Run Presidio over one segment and translate to the suite's Entity type."""
    return [
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


# Analyzer construction is expensive (loads NLP models); cache one per process.
_ANALYZER_CACHE = {}


def _get_analyzer(analyzer_cls):
    if "instance" not in _ANALYZER_CACHE:
        _ANALYZER_CACHE["instance"] = analyzer_cls()
    return _ANALYZER_CACHE["instance"]
