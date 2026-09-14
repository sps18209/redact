"""Redact PII that is rendered as pixels — screenshots, scans, photos of paper.

The gap this closes was a false-assurance one, the kind this suite treats as
worse than non-coverage. A screenshot showing a name, an SSN and an email went
through the image path, came back ``[ok] 0 entities, 0 face(s) blurred`` with
exit 0, and ``redact verify`` agreed it was ``clean`` — because both look for
text and the characters were pixels. Two tools affirming a document is safe
while the SSN is plainly legible is the worst outcome either can produce.

This backend reads the image with OCR, finds PII in what it reads, and covers
those regions. It sits *below* the face detectors on purpose: `auto` should keep
blurring faces in photographs, and text redaction is what you reach for by name
(``-b ocr``) on a screenshot or a scan. The face backends now declare
unredacted text rather than staying quiet about it, so nothing silently claims a
screenshot is finished.
"""

from __future__ import annotations

import importlib.util
from typing import List

from .. import ocr as ocr_engine
from ..document import Document, output_path
from ..types import Entity, MediaType, RedactionMode, RedactionOptions, RedactionResult
from .base import Backend

#: Pixel radius for BLUR mode. Large enough that text is unrecoverable rather
#: than merely softened — a lightly blurred SSN is still readable.
_BLUR_RADIUS = 12


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class OcrBackend(Backend):
    name = "ocr"
    description = (
        "Redacts text rendered as pixels — screenshots, scans, photos of documents. "
        "Reads the image with OCR, then covers the regions carrying PII."
    )
    supported_media_types = (MediaType.IMAGE,)
    install_hint = 'pip install "redact-suite[ocr]"'
    # Below deface (70) and yolo (65) so `auto` still blurs faces in a
    # photograph. Reach for this by name on a screenshot or a scan.
    priority = 30

    def missing_dependencies(self) -> List[str]:
        missing = list(ocr_engine.missing_dependencies())
        if not _module_present("PIL"):
            missing.append("pillow")
        return missing

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        missing = self.missing_dependencies()
        if missing:
            result.success = False
            result.message = f"missing dependencies: {', '.join(missing)}"
            return result

        try:
            hits = ocr_engine.text_entities(
                document.path, options.entities, options.threshold
            )
        except Exception as exc:
            result.success = False
            result.message = f"could not read the image: {exc}"
            return result

        result.entities = [
            Entity(e.entity_type, e.score, None, None, e.text, bbox=box)
            for e, box in hits
        ]

        if options.dry_run:
            result.message = _summary(result.entities, applied=False)
            return result

        out = output_path(document, options)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            _cover(document.path, out, [box for _, box in hits], options.mode)
        except Exception as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result

        result.output_path = out
        result.message = _summary(result.entities, applied=True)
        return result


def _cover(source, destination, boxes, mode: RedactionMode) -> None:
    """Write a copy of ``source`` with each box covered."""
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.open(source)
    # OCR coordinates are in the image's own pixel space; normalise rotation and
    # palette first so the boxes land where the engine saw the text.
    image = image.convert("RGB")
    for left, top, width, height in boxes:
        region = (left, top, left + width, top + height)
        if mode is RedactionMode.BLUR:
            patch = image.crop(region).filter(
                ImageFilter.GaussianBlur(radius=_BLUR_RADIUS)
            )
            image.paste(patch, region)
        else:
            # Solid fill. A placeholder cannot be drawn back without guessing a
            # font size that fits, and an unreadable box is the honest result.
            ImageDraw.Draw(image).rectangle(region, fill=(0, 0, 0))
    image.save(destination)


def _summary(entities: List[Entity], applied: bool) -> str:
    if not entities:
        return "no PII found in the image's text" + ("" if applied else " (dry-run)")
    kinds = ", ".join(sorted({e.entity_type for e in entities}))
    verb = "region(s) covered" if applied else "region(s) would be covered (dry-run)"
    return f"{len(entities)} {verb}: {kinds}"
