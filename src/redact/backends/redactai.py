"""Contextual redaction through Ollama. PDFs produce text extracts."""

from __future__ import annotations

import http.client
import importlib.util
import ipaddress
import json
import os
import tempfile
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import List
from urllib.parse import urlsplit

from ..document import Document, output_path
from ..types import Entity, MediaType, RedactionOptions, RedactionResult
from .base import Backend
from .builtin import apply_redactions, detect_entities

_DEFAULT_OLLAMA = "http://localhost:11434"
_DEFAULT_MODEL = "llama3"
_PROBE_TIMEOUT = 2.0
_GEN_TIMEOUT = 120.0
_CHUNK_SIZE = 2048
_CHUNK_OVERLAP = 256

_MODEL_FORMAT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}

_PROMPT = (
    "You are a PII/PHI detection engine. From the text below, extract every span "
    "of sensitive information (names, addresses, phone numbers, emails, IDs, "
    "medical or financial details). Respond ONLY with a JSON array of objects "
    '{"text": <exact substring>}. Use [] if there are no findings. Text:\n\n'
)


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA).rstrip("/")


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class OllamaError(ValueError):
    """A safe diagnostic for a failed Ollama request or response."""


class RedactAIBackend(Backend):
    name = "redactai"
    description = ("Contextual detection via a configured Ollama service. "
                   "Redacts text; for a PDF it extracts only — the PDF is not modified.")
    supported_media_types = (MediaType.PDF, MediaType.TEXT)
    install_hint = 'pip install "redact-suite[pdf]" and run a local Ollama server'
    priority = 60
    uses_service = True
    requires_service_consent = True

    def _model(self, options: RedactionOptions = None) -> str:
        if options and options.extra.get("ollama_model"):
            return options.extra["ollama_model"]
        return os.environ.get("OLLAMA_MODEL", _DEFAULT_MODEL)

    def _pdf_available(self) -> bool:
        return _module_present("pypdf")

    def missing_dependencies(self) -> List[str]:
        try:
            with urllib.request.urlopen(_ollama_host() + "/api/tags", timeout=_PROBE_TIMEOUT):
                return []
        except (OSError, ValueError):
            return ["configured Ollama service is unreachable"]

    # -- text extraction -----------------------------------------------------
    def _extract_text(self, document: Document) -> str:
        if document.media_type is MediaType.TEXT:
            return document.read_text()
        from pypdf import PdfReader  # lazy

        reader = PdfReader(str(document.path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    def _client(self, options: RedactionOptions):
        host = _ollama_host()

        def request(path, payload, timeout):
            req = urllib.request.Request(
                host + path, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)

        return self._model(options), request

    def _detect_with_model(self, text: str, options: RedactionOptions) -> List[Entity]:
        if not text.strip():
            raise OllamaError("the document has no readable text layer")
        model, request = self._client(options)
        values = set()
        for offset in range(0, len(text), _CHUNK_SIZE - _CHUNK_OVERLAP):
            chunk = text[offset : offset + _CHUNK_SIZE]
            try:
                body = request("/api/generate", {
                    "model": model, "prompt": _PROMPT + chunk, "stream": False,
                    "truncate": False, "format": _MODEL_FORMAT,
                }, _GEN_TIMEOUT)
                if (
                    not isinstance(body, dict) or body.get("done") is not True
                    or body.get("done_reason") != "stop"
                ):
                    raise OllamaError("generation did not complete")
                values.update(_model_values(body.get("response"), chunk))
            except OllamaError as exc:
                raise OllamaError(f"text section starting at character {offset + 1}: {exc}") from None
            except (OSError, ValueError, http.client.HTTPException):
                raise OllamaError(f"request failed for text section starting at character {offset + 1}") from None
            if offset + _CHUNK_SIZE >= len(text):
                break

        if options.entities is not None and "SENSITIVE" not in options.entities:
            return []
        entities = []
        for value in values:
            start = text.find(value)
            while start >= 0:
                entities.append(Entity("SENSITIVE", 0.9, start, start + len(value), value))
                start = text.find(value, start + 1)
        return entities

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        consent_error = self.service_consent_error(options)
        if consent_error:
            result.success = False
            result.message = consent_error
            return result
        if document.media_type is MediaType.PDF and not self._pdf_available():
            result.success = False
            result.message = "PDF text extraction requires pypdf"
            return result

        out = output_path(document, options, suffix=".txt")
        if not options.dry_run and (out.exists() or out.is_symlink()):
            result.success = False
            result.message = "output already exists from a prior run. Choose a fresh output folder."
            return result
        try:
            text = self._extract_text(document)
        except Exception:  # pypdf / IO errors
            result.success = False
            result.message = "could not extract document text"
            return result

        try:
            model_entities = self._detect_with_model(text, options)
        except OllamaError as exc:
            result.success = False
            result.message = f"ollama request failed: {exc}"
            return result

        # Fuse model output with deterministic regex detections for coverage.
        entities = _merge_entities(
            model_entities + detect_entities(text, options.entities, options.threshold), text
        )
        result.entities = entities
        redacted = apply_redactions(text, entities, options)
        result.redacted_text = redacted

        if options.dry_run:
            result.message = "dry-run: detected via ollama, nothing written"
            return result

        # Write the redacted text. For a .txt input this *is* the redacted
        # artifact. For a PDF it is only an extract: a true black-out needs a
        # PDF writer with span coordinates, which this adapter does not have.
        temp = None
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=out.parent, delete=False,
            ) as handle:
                temp = Path(handle.name)
                handle.write(redacted)
            # Publish a complete file and refuse concurrent overwrites.
            os.link(temp, out)
        except FileExistsError:
            result.success = False
            result.message = "output already exists from another run. Choose a fresh output folder."
            return result
        except OSError:
            result.success = False
            result.message = "could not publish the redacted output"
            return result
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        result.output_path = out

        if document.media_type is MediaType.PDF:
            # The source PDF is untouched and still carries every entity found
            # above. Reporting a clean success here tells any script checking
            # the exit code that the PDF is safe to share — the precise lie a
            # redaction tool must never tell. Declaring it unredacted makes the
            # CLI warn and exit non-zero.
            result.unredacted.append(document.path.name)
            result.message = (
                f"PDF NOT redacted — wrote a redacted text extract ({out.name}) only; "
                "the source PDF still contains every entity listed. Use the "
                "pymupdf backend, which redacts the PDF itself"
            )
        else:
            result.message = "text redacted"
        return result


class LocalOllamaBackend(RedactAIBackend):
    name = "ollama-local"
    description = "Local Ollama for text and PDF text extracts, with cloud disabled."
    install_hint = "start an up-to-date local Ollama server with cloud disabled and an installed model"
    requires_service_consent = False

    def missing_dependencies(self) -> List[str]:
        try:
            _loopback_target()
        except OllamaError as exc:
            return [str(exc)]
        return []

    def _client(self, options: RedactionOptions):
        host, port = _loopback_target()
        configured_model = self._model(options)
        model = configured_model.strip() if isinstance(configured_model, str) else ""
        if not model or model.lower().removesuffix(":local").endswith((":cloud", "-cloud")):
            raise OllamaError("choose an installed local model with OLLAMA_MODEL")
        if not model.endswith(":local"):
            model += ":local"

        def request(path, payload, timeout):
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
            try:
                method = "GET" if payload is None else "POST"
                data = None if payload is None else json.dumps(payload).encode("utf-8")
                connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
                with connection.getresponse() as response:
                    if response.status != 200:
                        raise OllamaError(f"local Ollama returned HTTP {response.status} for {path}")
                    body = json.load(response)
                    if not isinstance(body, dict):
                        raise OllamaError("local Ollama returned an invalid response")
                    return body
            finally:
                connection.close()

        try:
            status = request("/api/status", None, _PROBE_TIMEOUT)
            cloud = status.get("cloud")
            if not isinstance(cloud, dict) or cloud.get("disabled") is not True:
                raise OllamaError("disable Ollama cloud features and restart the server")
            details = request("/api/show", {"model": model}, _PROBE_TIMEOUT)
            if details.get("remote_host") or details.get("remote_model") or not details.get("model_info"):
                raise OllamaError("the selected Ollama model could not be verified as local")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            reason = str(exc) if isinstance(exc, OllamaError) else "local Ollama preflight failed"
            raise OllamaError(f"{reason}. {self.install_hint}.") from None
        return model, request


def _loopback_target():
    raw = _ollama_host()
    try:
        url = urlsplit(raw if "://" in raw else "http://" + raw)
        host = "127.0.0.1" if url.hostname == "localhost" else url.hostname
        address = ipaddress.ip_address(host)
        if (
            url.scheme == "http" and address.is_loopback and url.username is None
            and not url.path and not url.query and not url.fragment and url.port != 0
        ):
            return str(address), url.port or 11434
    except ValueError:
        pass
    raise OllamaError("set OLLAMA_HOST to localhost or a numeric loopback address")


def _model_values(raw, chunk: str) -> set:
    """Validate every exact substring before accepting the section."""
    try:
        items = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        items = None
    if not isinstance(items, list):
        raise OllamaError("model did not return a findings array")
    values = set()
    for item in items:
        value = item.get("text") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value.strip() or value not in chunk:
            raise OllamaError("model returned an invalid finding")
        values.add(value)
    return values


def _merge_entities(entities: List[Entity], text: str) -> List[Entity]:
    """Merge overlapping intervals so both findings are fully covered."""
    merged: List[Entity] = []
    for entity in sorted(entities, key=lambda item: (item.start, -item.end)):
        if not merged or entity.start >= merged[-1].end:
            merged.append(entity)
        elif entity.end > merged[-1].end:
            previous = merged[-1]
            merged[-1] = replace(
                previous, end=entity.end, text=text[previous.start : entity.end],
            )
    return merged
