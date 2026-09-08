"""Adapter for Philter (self-hosted PII/PHI redaction service).

Philter runs as a service exposing a REST API. This adapter talks to a running
instance over HTTP using only the standard library, so it needs no extra Python
packages — just a reachable Philter endpoint.

Configure the endpoint via the ``PHILTER_ENDPOINT`` environment variable or
``options.extra['philter_endpoint']`` (default ``http://localhost:8080``).
See https://philterd.ai/
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import List

from ..document import Document
from ..types import Entity, MediaType, RedactionOptions, RedactionResult
from .base import Backend
from .builtin import default_output_path

_DEFAULT_ENDPOINT = "http://localhost:8080"
_HEALTH_TIMEOUT = 2.0
_REQUEST_TIMEOUT = 60.0


class PhilterBackend(Backend):
    name = "philter"
    description = "Philter — self-hosted PII/PHI redaction service for healthcare/legal/finance (Apache-2.0)."
    supported_media_types = (MediaType.TEXT, MediaType.STRUCTURED)
    priority = 70

    def _endpoint(self, options: RedactionOptions = None) -> str:
        if options and options.extra.get("philter_endpoint"):
            return options.extra["philter_endpoint"].rstrip("/")
        return os.environ.get("PHILTER_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/")

    def missing_dependencies(self) -> List[str]:
        # Reachability is the dependency here. A short health probe keeps
        # discovery cheap; failures are reported as the service being down.
        url = self._endpoint() + "/api/status"
        try:
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT):
                return []
        except (urllib.error.URLError, OSError):
            return [f"philter service at {self._endpoint()} (unreachable)"]

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        result = RedactionResult(
            source=document.path, backend=self.name,
            media_type=document.media_type,
        )
        try:
            text = document.read_text()
        except OSError as exc:
            result.success = False
            result.message = f"could not read file: {exc}"
            return result

        endpoint = self._endpoint(options)
        # Philter's filter endpoint returns the redacted text body.
        req = urllib.request.Request(
            endpoint + "/api/filter",
            data=text.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                redacted = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            result.success = False
            result.message = f"philter request failed ({endpoint}): {exc}"
            return result

        result.redacted_text = redacted
        # Philter does not return structured spans on the plain filter endpoint;
        # surface a count by diffing where content changed.
        result.entities = _infer_entities(text, redacted)

        if options.dry_run:
            result.message = "dry-run: filtered via philter, nothing written"
            return result

        out = default_output_path(document.path, options)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(redacted, encoding="utf-8")
        except OSError as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result
        result.output_path = out
        return result


def _infer_entities(original: str, redacted: str) -> List[Entity]:
    """Approximate a finding count when the service returns text only."""
    if original == redacted:
        return []
    # Philter masks with '{{{REDACTED}}}'-style tokens by default; count them.
    import re

    tokens = re.findall(r"\{\{\{[^}]*\}\}\}|\*{2,}", redacted)
    return [Entity(entity_type="REDACTED", score=1.0, text=t) for t in tokens]
