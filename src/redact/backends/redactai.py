"""Adapter for RedactAI-style contextual PDF redaction via local Ollama models.

RedactAI is an MCP server that uses local Ollama models (Llama, Qwen, …) to
*contextually* identify sensitive data in PDFs and black it out. This adapter
reproduces the core pipeline in-process so it fits the suite's backend contract:

    1. extract the PDF text layer (via pypdf, if installed),
    2. ask a local Ollama model to identify sensitive spans,
    3. apply redactions to the extracted text and, when pypdf can, black out the
       corresponding spans in a new PDF.

Availability requires a reachable Ollama server (``OLLAMA_HOST`` or
localhost:11434) and pypdf for PDF parsing. Everything runs locally.
See https://github.com/AtharvSabde/RedactAI
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
import urllib.request
from typing import List

from ..document import Document
from ..types import Entity, MediaType, RedactionOptions, RedactionResult
from .base import Backend
from .builtin import apply_redactions, default_output_path, detect_entities

_DEFAULT_OLLAMA = "http://localhost:11434"
_DEFAULT_MODEL = "llama3"
_PROBE_TIMEOUT = 2.0
_GEN_TIMEOUT = 120.0

_PROMPT = (
    "You are a PII/PHI detection engine. From the text below, extract every span "
    "of sensitive information (names, addresses, phone numbers, emails, IDs, "
    "medical or financial details). Respond ONLY with a JSON array of objects "
    '{"text": <exact substring>, "type": <LABEL>}. Text:\n\n'
)


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA).rstrip("/")


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class RedactAIBackend(Backend):
    name = "redactai"
    description = "RedactAI-style contextual PDF redaction using local Ollama models (Llama/Qwen)."
    supported_media_types = (MediaType.PDF, MediaType.TEXT)
    priority = 60

    def _model(self, options: RedactionOptions = None) -> str:
        if options and options.extra.get("ollama_model"):
            return options.extra["ollama_model"]
        return os.environ.get("OLLAMA_MODEL", _DEFAULT_MODEL)

    def missing_dependencies(self) -> List[str]:
        missing: List[str] = []
        if not _module_present("pypdf"):
            missing.append("pypdf")
        try:
            with urllib.request.urlopen(_ollama_host() + "/api/tags", timeout=_PROBE_TIMEOUT):
                pass
        except (urllib.error.URLError, OSError):
            missing.append(f"ollama server at {_ollama_host()} (unreachable)")
        return missing

    # -- text extraction -----------------------------------------------------
    def _extract_text(self, document: Document) -> str:
        if document.media_type is MediaType.TEXT:
            return document.read_text()
        from pypdf import PdfReader  # lazy

        reader = PdfReader(str(document.path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    # -- model call ----------------------------------------------------------
    def _detect_with_model(self, text: str, options: RedactionOptions) -> List[Entity]:
        payload = json.dumps(
            {"model": self._model(options), "prompt": _PROMPT + text, "stream": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            _ollama_host() + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_GEN_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        raw = body.get("response", "")
        return _entities_from_model_output(raw, text)

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        missing = self.missing_dependencies()
        if missing:
            return RedactionResult(
                source=document.path, backend=self.name,
                media_type=document.media_type, success=False,
                message=f"missing dependencies: {', '.join(missing)}",
            )

        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        try:
            text = self._extract_text(document)
        except Exception as exc:  # pypdf / IO errors
            result.success = False
            result.message = f"could not extract text: {exc}"
            return result

        try:
            model_entities = self._detect_with_model(text, options)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            result.success = False
            result.message = f"ollama request failed: {exc}"
            return result

        # Fuse model output with deterministic regex detections for coverage.
        entities = _merge_entities(
            model_entities, detect_entities(text, options.entities, options.threshold)
        )
        result.entities = entities
        redacted = apply_redactions(text, entities, options)
        result.redacted_text = redacted

        if options.dry_run:
            result.message = "dry-run: detected via ollama, nothing written"
            return result

        # Write a redacted text sidecar. A true PDF black-out requires a PDF
        # writer with span coordinates; we emit .redacted.txt to stay honest
        # about what was applied.
        out = default_output_path(document.path, options).with_suffix(".redacted.txt")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(redacted, encoding="utf-8")
        except OSError as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result
        result.output_path = out
        result.message = "text redacted (PDF text layer); see .redacted.txt"
        return result


def _entities_from_model_output(raw: str, text: str) -> List[Entity]:
    """Parse the model's JSON array and locate each span within ``text``."""
    entities: List[Entity] = []
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        return entities
    try:
        items = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return entities
    search_from = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("text")
        if not value:
            continue
        idx = text.find(value, search_from)
        if idx == -1:
            idx = text.find(value)  # retry from the top
        if idx == -1:
            continue
        entities.append(
            Entity(
                entity_type=str(item.get("type", "SENSITIVE")).upper(),
                score=0.9,
                start=idx,
                end=idx + len(value),
                text=value,
            )
        )
        search_from = idx + len(value)
    return entities


def _merge_entities(primary: List[Entity], secondary: List[Entity]) -> List[Entity]:
    """Combine two entity lists, dropping positional overlaps."""
    from .builtin import _dedupe_overlaps

    positioned = [e for e in primary + secondary if e.start is not None]
    return _dedupe_overlaps(positioned)
