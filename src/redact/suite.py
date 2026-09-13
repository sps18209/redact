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

import dataclasses
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional

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
        options = self._prepare(options or RedactionOptions(), document)
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

    # -- image redaction inside documents ------------------------------------
    def _prepare(self, options: RedactionOptions, document: Document) -> RedactionOptions:
        """Attach an image redactor when a document's images must be blurred.

        Blurring images embedded in a .docx/.xlsx needs an image-capable backend
        (Anonymizer). Rather than give text backends registry access, the suite
        injects a callable; ``docx.redact_docx`` strips any image the callable
        declines, so the policy degrades safely when no such backend exists.
        """
        office = (MediaType.DOCX, MediaType.XLSX)
        if document.media_type not in office or options.docx_images != "blur":
            return options
        if options.extra.get("image_redactor") is not None:
            return options
        redactor = self._image_redactor()
        if redactor is None:
            return options
        return dataclasses.replace(options, extra={**options.extra, "image_redactor": redactor})

    def _image_redactor(self) -> Optional[Callable[[bytes, str], Optional[bytes]]]:
        """A ``(bytes, suffix) -> bytes|None`` shim over the best image backend."""
        backend = next(
            (
                b
                for b in sorted(self.registry.all(), key=lambda b: -b.priority)
                if b.supports(MediaType.IMAGE) and b.is_available()
            ),
            None,
        )
        if backend is None:
            return None

        def redact_image(data: bytes, suffix: str) -> Optional[bytes]:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / f"image{suffix or '.png'}"
                src.write_bytes(data)
                out_dir = Path(tmp) / "out"
                res = backend.redact(
                    Document(path=src, media_type=MediaType.IMAGE),
                    RedactionOptions(output_dir=out_dir),
                )
                if not res.success or not res.output_path or not Path(res.output_path).is_file():
                    return None
                return Path(res.output_path).read_bytes()

        return redact_image

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
                    "install_hint": backend.install_hint,
                }
            )
        return sorted(rows, key=lambda r: (not r["available"], -r["priority"], r["name"]))
