"""Adapter for understand.ai's Anonymizer (faces & license plates).

Anonymizer uses local deep-learning models to detect and blur faces and license
plates in images and video frames. It ships as a Python package and a CLI. This
adapter drives its CLI when present so the suite can route image/video inputs to
it without importing TensorFlow at discovery time.

Install: https://github.com/understand-ai/anonymizer
The CLI form used here:  ``anonymizer -i <in_dir> -o <out_dir> -w <weights>``
(weights auto-download on first run). Set ``ANONYMIZER_WEIGHTS`` to point at a
local weights directory.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

from ..document import Document
from ..types import MediaType, RedactionOptions, RedactionResult
from .base import Backend


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class AnonymizerBackend(Backend):
    name = "anonymizer"
    description = "understand.ai Anonymizer — blurs faces & license plates in images/video (CNN)."
    supported_media_types = (MediaType.IMAGE, MediaType.VIDEO)
    priority = 60

    def _cli(self) -> str:
        return os.environ.get("ANONYMIZER_BIN", "anonymize")

    def missing_dependencies(self) -> List[str]:
        # Either the installed package or a CLI on PATH is enough to run.
        if _module_present("anonymizer") or shutil.which(self._cli()):
            return []
        return ["anonymizer (package or CLI)"]

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        if self.missing_dependencies():
            return RedactionResult(
                source=document.path, backend=self.name,
                media_type=document.media_type, success=False,
                message="anonymizer package/CLI not found",
            )

        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        if options.dry_run:
            result.message = "dry-run: would blur faces/plates (detection count unavailable pre-run)"
            return result

        out_dir = Path(options.output_dir) if options.output_dir else document.path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        # Anonymizer processes a directory; stage the single file into a temp in-dir.
        with tempfile.TemporaryDirectory() as staging:
            staged = Path(staging) / document.path.name
            shutil.copy2(document.path, staged)
            cmd = [self._cli(), "-i", staging, "-o", str(out_dir)]
            weights = os.environ.get("ANONYMIZER_WEIGHTS")
            if weights:
                cmd += ["-w", weights]
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as exc:
                result.success = False
                result.message = f"anonymizer failed: {exc.stderr.strip() or exc}"
                return result

        produced = out_dir / document.path.name
        result.output_path = produced if produced.exists() else out_dir
        result.message = "faces & license plates blurred"
        return result
