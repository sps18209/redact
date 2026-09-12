"""Adapter for pdf-redact-tools (high-assurance PDF sanitisation).

pdf-redact-tools is a CLI that flattens a PDF into images and back, destroying
the underlying text layer and hidden metadata so redactions can't be
reverse-engineered. This adapter shells out to that CLI when it is on ``PATH``.

Note: this tool *sanitises* rather than *detects* — it strips the whole text
layer, so it reports no per-entity findings. Combine it with a detection pass
(builtin/presidio over extracted text) when you need to know what was present.

Install: see https://github.com/firstlookmedia/pdf-redact-tools
"""

from __future__ import annotations

import shutil
import subprocess
from typing import List

from ..document import Document, output_path
from ..types import MediaType, RedactionOptions, RedactionResult
from .base import Backend

_BINARY = "pdf-redact-tools"


class PdfRedactToolsBackend(Backend):
    name = "pdf-redact-tools"
    description = "pdf-redact-tools — strips PDF text layer & metadata by flattening to images."
    supported_media_types = (MediaType.PDF,)
    priority = 40

    def missing_dependencies(self) -> List[str]:
        return [] if shutil.which(_BINARY) else [_BINARY]

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        if self.missing_dependencies():
            return RedactionResult(
                source=document.path, backend=self.name,
                media_type=document.media_type, success=False,
                message=f"'{_BINARY}' not found on PATH",
            )

        result = RedactionResult(
            source=document.path, backend=self.name,
            media_type=document.media_type,
        )
        if options.dry_run:
            result.message = "dry-run: would flatten & sanitise PDF (no detection performed)"
            return result

        # pdf-redact-tools --sanitize writes "<name>-final.pdf" next to the input.
        try:
            subprocess.run(
                [_BINARY, "--sanitize", str(document.path)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            result.success = False
            result.message = f"{_BINARY} failed: {exc.stderr.strip() or exc}"
            return result

        # The tool writes "<stem>-final.pdf" beside the input; move it to the
        # suite's canonical "<stem>.redacted.pdf" (mirrored under -o if given).
        produced = document.path.with_name(document.path.stem + "-final.pdf")
        if not produced.exists():
            result.success = False
            result.message = f"{_BINARY} exited 0 but {produced.name} was not written"
            return result
        dest = output_path(document, options)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(dest))
        result.output_path = dest
        result.message = "sanitised: text layer & metadata stripped"
        return result
