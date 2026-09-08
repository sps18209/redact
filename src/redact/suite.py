"""The high-level orchestrator that ties ingestion, routing and backends together.

:class:`RedactionSuite` is the main entry point for programmatic use::

    from redact import RedactionSuite, RedactionOptions

    suite = RedactionSuite()
    result = suite.redact_path("contract.pdf")
    print(result.summary())

    for res in suite.redact_paths(["./inbox"], RedactionOptions(mode="mask")):
        print(res.summary())
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator, List, Optional

from .document import Document, iter_documents, load_document
from .registry import BackendRegistry
from .router import RoutingError, select_backend
from .types import MediaType, RedactionOptions, RedactionResult


class RedactionSuite:
    """Routes documents to the best available redaction backend and runs them."""

    def __init__(self, registry: Optional[BackendRegistry] = None):
        self.registry = registry or BackendRegistry.with_defaults()

    # -- single document -----------------------------------------------------
    def redact_document(
        self, document: Document, options: Optional[RedactionOptions] = None
    ) -> RedactionResult:
        options = options or RedactionOptions()
        try:
            backend = select_backend(document, options, self.registry)
        except RoutingError as exc:
            return RedactionResult(
                source=document.path,
                backend=options.backend,
                media_type=document.media_type,
                success=False,
                message=str(exc),
            )
        return backend.redact(document, options)

    def redact_path(
        self, path, options: Optional[RedactionOptions] = None
    ) -> RedactionResult:
        document = load_document(path)
        return self.redact_document(document, options)

    # -- batch ---------------------------------------------------------------
    def redact_paths(
        self,
        inputs: Iterable[str],
        options: Optional[RedactionOptions] = None,
        recursive: bool = True,
        include_unknown: bool = False,
    ) -> Iterator[RedactionResult]:
        """Ingest every file under ``inputs`` and redact each, yielding results.

        This is the "pull any document in" batch path: files, directories and
        globs are all accepted, and each surfaced document is routed independently
        so a folder of mixed PDFs, images and CSVs is handled in one pass.
        """
        options = options or RedactionOptions()
        for document in iter_documents(inputs, recursive, include_unknown):
            yield self.redact_document(document, options)

    # -- introspection -------------------------------------------------------
    def describe_backends(self) -> List[dict]:
        """Structured view of every backend for ``redact list`` / diagnostics."""
        rows = []
        for backend in self.registry.all():
            rows.append(
                {
                    "name": backend.name,
                    "available": backend.is_available(),
                    "missing": backend.missing_dependencies(),
                    "media_types": [str(m) for m in backend.supported_media_types],
                    "priority": backend.priority,
                    "description": backend.description,
                }
            )
        return sorted(rows, key=lambda r: (not r["available"], -r["priority"], r["name"]))
