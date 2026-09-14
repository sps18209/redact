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


#: Memoised probe result, keyed by path. `missing_dependencies()` must stay
#: cheap — it runs during discovery and once per document in a batch — and
#: forking a subprocess each time would make a 200-PDF run pay it 200 times.
_PROBED = {}


def _runs(candidate: str) -> bool:
    """True only if the binary actually executes.

    Upstream stopped at Python 2, so what is on PATH is usually a script that
    raises SyntaxError the moment an interpreter reads it.
    """
    if candidate in _PROBED:
        return _PROBED[candidate]
    _PROBED[candidate] = _probe(candidate)
    return _PROBED[candidate]


def _probe(candidate: str) -> bool:
    try:
        completed = subprocess.run(
            [candidate, "--help"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class PdfRedactToolsBackend(Backend):
    name = "pdf-redact-tools"
    description = (
        "pdf-redact-tools — strips PDF text layer & metadata by flattening to "
        "images. Unmaintained Python 2; needs patching to run at all."
    )
    supported_media_types = (MediaType.PDF,)
    install_hint = (
        "unmaintained Python 2 script: needs 2to3 plus a bytes/str fix in "
        "check_output before it runs, and ImageMagick, exiftool and poppler "
        'installed. Prefer pip install "redact-suite[pymupdf]"'
    )
    priority = 40

    def missing_dependencies(self) -> List[str]:
        found = shutil.which(_BINARY)
        if not found:
            return [_BINARY]
        if not _runs(found):
            # Being on PATH is not being usable — the same trap as a system
            # ffmpeg whose libraries moved. Upstream is an unmaintained Python 2
            # script (bare `print`, `0700` octal literals), so on any current
            # interpreter it raises SyntaxError before doing anything. Reporting
            # "available" would list it as ready and let the router hand it a
            # document it cannot process.
            return [f"{_BINARY} (found, but it does not run — see the hint below)"]
        return []

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
