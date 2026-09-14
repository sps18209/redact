"""Find PII that is *pixels*, not text.

The suite could redact a screenshot's faces and report a clean run while an SSN
sat in plain sight in the image. Both the redactor and ``redact verify`` said
``clean``, because both look for text and there was none to find — the
characters were pixels. That is the suite's worst failure mode (a comfortable
answer nobody can act on) applied to a file type that is everywhere in legal and
medical work: screenshots, phone photos of documents, scanned pages.

This module reads text out of an image with OCR and reports where it sits, so
the image backends can either redact it or say plainly that they did not.

Two deliberate choices:

* **Whole-region redaction.** OCR returns a box per recognised line, and mapping
  a character span inside that line back to pixels means guessing glyph widths.
  A region containing PII is covered entirely. That over-redacts — "SSN" goes
  along with the number — and over-redaction is the safe direction.

* **No engine is not "no text".** When OCR is unavailable the answer is "this
  was not examined", never "nothing found". :func:`unexamined_note` returns the
  sentence a backend puts in ``unredacted`` so the run exits non-zero.

``rapidocr-onnxruntime`` is the engine: one pip install, no system binary, which
is the same bar ``pymupdf`` had to clear for PDFs.
"""

from __future__ import annotations

import importlib.util
import re
from typing import List, Optional, Sequence, Tuple

from .types import Entity

#: (x, y, width, height) in pixels.
Box = Tuple[int, int, int, int]

#: OCR on a low-contrast scan is noisy; below this a "reading" is a guess.
MIN_CONFIDENCE = 0.5

#: OCR loses spaces: the engine reads "SSN 078-05-1120" as "SSN078-05-1120".
#: Most patterns here are anchored with \b, and there is no word boundary
#: between "N" and "0", so the most sensitive value in the image goes
#: undetected. Restoring a boundary at letter/digit transitions recovers it.
#: Both the raw reading and the restored one are scanned and the results
#: unioned, because the split can equally break a value that was already
#: correct (an ID like "AB12CD34").
_LETTER_DIGIT = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def boundary_restored(text: str) -> str:
    """Put a space back at letter/digit transitions OCR ran together."""
    return _LETTER_DIGIT.sub(" ", text)

INSTALL_HINT = 'pip install "redact-suite[ocr]"'

__all__ = [
    "Box", "available", "missing_dependencies", "read_regions",
    "text_entities", "unexamined_note", "boundary_restored",
]

_ENGINE = None


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_dependencies() -> List[str]:
    """Cheap and never raises — this runs during backend discovery."""
    return [] if _module_present("rapidocr_onnxruntime") else ["rapidocr-onnxruntime"]


def available() -> bool:
    return not missing_dependencies()


def _engine():
    """One engine per process; construction loads ONNX models and is slow."""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # lazy: heavy import

        _ENGINE = RapidOCR()
    return _ENGINE


def read_regions(path) -> List[Tuple[str, Box]]:
    """Every line of text the engine reads, with its pixel box."""
    result, _ = _engine()(str(path))
    regions: List[Tuple[str, Box]] = []
    for item in result or []:
        try:
            polygon, text, score = item[0], item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not text or score < MIN_CONFIDENCE:
            continue
        xs = [float(p[0]) for p in polygon]
        ys = [float(p[1]) for p in polygon]
        left, top = int(min(xs)), int(min(ys))
        regions.append((text, (left, top, int(max(xs)) - left, int(max(ys)) - top)))
    return regions


def text_entities(
    path,
    entities: Optional[Sequence[str]] = None,
    threshold: float = 0.35,
) -> List[Tuple[Entity, Box]]:
    """PII found in an image's rendered text, paired with the box to cover.

    Offsets are dropped: they would index the OCR reading, not anything the
    caller holds, and an image has no linear text flow.
    """
    from .backends.builtin import detect_entities  # circular at module level

    found: List[Tuple[Entity, Box]] = []
    for text, box in read_regions(path):
        seen = set()
        for variant in (text, boundary_restored(text)):
            for entity in detect_entities(variant, entities, threshold):
                key = (entity.entity_type, entity.text)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    (Entity(entity.entity_type, entity.score, None, None, entity.text), box)
                )
    return found


def unexamined_note(path, entities=None, threshold: float = 0.35) -> Optional[str]:
    """What a backend must declare about text it did not redact.

    Returns None only when the image is genuinely settled: OCR ran and found no
    PII. Without an engine the answer is "not examined", because a tool that
    reports ``clean`` over something it never looked at is the failure this
    whole suite exists to prevent.
    """
    if not available():
        return (
            "text inside the image was NOT examined (no OCR engine installed) — "
            f"a screenshot or scan can carry PII as pixels; {INSTALL_HINT}"
        )
    try:
        hits = text_entities(path, entities, threshold)
    except Exception as exc:  # a broken engine must not fail the redaction
        return f"text inside the image could not be examined ({exc})"
    if not hits:
        return None
    kinds = sorted({e.entity_type for e, _ in hits})
    return (
        f"{len(hits)} text region(s) in the image carry PII ({', '.join(kinds)}) "
        "and were NOT redacted — re-run with -b ocr"
    )
