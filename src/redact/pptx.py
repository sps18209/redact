"""PowerPoint (.pptx) support — standard library only.

The third member of the Office trio, and the same OPC package as ``.docx`` and
``.xlsx`` (shared plumbing in ``opc.py``). Slide text lives in DrawingML:
``<a:p>`` paragraphs of ``<a:r><a:t>`` runs, the same split-run problem Word has
and the same fix.

What a deck hides beyond the visible slides:

* **Speaker notes** (``notesSlides``) — where people paste the detail they did
  not want on screen, which makes them a higher-yield target than the slides.
* **Slide layouts and masters** — a template header can carry a name long after
  the slide that introduced it was deleted.
* **Comments** and their authors.
* **Charts** — an embedded chart caches its source values and category labels in
  ``charts/chart*.xml``, so a "deleted" data table can survive there.
* **docProps**, ``.rels`` hyperlink targets and ``ppt/media`` images, handled
  exactly as in the other two formats.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .opc import (
    CONTENT_TYPES,
    IMAGE_POLICIES,
    METADATA_TAGS,
    Detect,
    ImageRedactor,
    OpcError,
    Replace,
    ensure_png_default,
    open_zip,
    parse_part,
    plan_images,
    rewrite_pieces,
    rewrite_rels,
    scrub_metadata,
    serialize_part,
    unpositioned,
    write_zip,
)
from .types import Entity

#: Backwards-consistent alias — a .pptx failure is an OPC failure.
PptxError = OpcError

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# Every part that can hold text. Notes and masters matter as much as slides.
_TEXT_PARTS = re.compile(
    r"^ppt/("
    r"slides/slide\d+|"
    r"notesSlides/notesSlide\d+|"
    r"slideLayouts/slideLayout\d+|"
    r"slideMasters/slideMaster\d+|"
    r"notesMasters/notesMaster\d+|"
    r"comments/(modern)?[Cc]omment\d*|"
    r"charts/chart\d+"
    r")\.xml$"
)
_MEDIA_PREFIX = "ppt/media/"

__all__ = ["PptxError", "PptxRedaction", "extract_text", "redact_pptx"]


@dataclass
class PptxRedaction:
    """What :func:`redact_pptx` found. Findings carry no offsets: a deck has no
    single linear text flow, so slides are identified by part name in ``notes``."""

    entities: List[Entity] = field(default_factory=list)
    redacted_text: str = ""
    notes: List[str] = field(default_factory=list)


def extract_text(path) -> str:
    """Text of every slide, note, layout, master, comment and chart label."""
    chunks: List[str] = []
    with open_zip(path) as zf:
        for name in sorted(n for n in zf.namelist() if _TEXT_PARTS.match(n)):
            root, _ = parse_part(zf.read(name))
            for group in _text_groups(root):
                text = "".join(el.text or "" for el in group)
                if text.strip():
                    chunks.append(text)
    return "\n".join(chunks)


def redact_pptx(
    source: Path,
    out: Optional[Path],
    detect: Detect,
    replace: Replace,
    scrub_authors: bool = True,
    image_policy: str = "keep",
    image_redactor: Optional[ImageRedactor] = None,
) -> PptxRedaction:
    """Redact ``source`` into ``out`` (``None`` = detect only, write nothing)."""
    if image_policy not in IMAGE_POLICIES:
        raise ValueError(f"image_policy must be one of {IMAGE_POLICIES}, got {image_policy!r}")

    result = PptxRedaction()
    replacements: Dict[str, bytes] = {}
    renames: Dict[str, str] = {}
    chunks: List[str] = []

    with open_zip(source) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]

        img_entities, img_notes = plan_images(
            zf, names, _MEDIA_PREFIX, image_policy, image_redactor, replacements, renames
        )
        result.entities.extend(img_entities)
        result.notes.extend(img_notes)

        for name in sorted(n for n in names if _TEXT_PARTS.match(n)):
            new, ents, part_chunks = _redact_text_part(
                zf.read(name), detect, replace, scrub_authors
            )
            if ents:
                result.entities.extend(ents)
                labels = ", ".join(sorted({e.entity_type for e in ents}))
                result.notes.append(f"{labels} in {_friendly(name)}")
            chunks.extend(part_chunks)
            if new is not None:
                replacements[name] = new
        result.redacted_text = "\n".join(chunks)

        if scrub_authors:
            for part, tags in METADATA_TAGS.items():
                if part not in names:
                    continue
                new, ents = scrub_metadata(zf.read(part), replace, tags)
                result.entities.extend(ents)
                if new is not None:
                    replacements[part] = new

        for name in names:
            if not name.endswith(".rels"):
                continue
            new, ents = rewrite_rels(zf.read(name), name, renames, detect, replace)
            result.entities.extend(ents)
            if new is not None:
                replacements[name] = new

        if renames and CONTENT_TYPES in names:
            new = ensure_png_default(zf.read(CONTENT_TYPES))
            if new is not None:
                replacements[CONTENT_TYPES] = new

        if out is not None:
            write_zip(zf, infos, out, replacements, renames)
    return result


# -- internals -----------------------------------------------------------------

def _friendly(part: str) -> str:
    """``ppt/notesSlides/notesSlide2.xml`` -> ``notesSlide2`` (for the report)."""
    return Path(part).stem


def _text_groups(root: ET.Element) -> List[List[ET.Element]]:
    """Runs to treat as one string: DrawingML paragraphs, then chart caches.

    A chart keeps its source values and category labels in ``<c:v>`` even after
    the underlying table is gone, so those are scanned too.
    """
    groups: List[List[ET.Element]] = []
    seen = set()
    for p in root.iter(A + "p"):
        runs = [t for t in p.iter(A + "t")]
        if runs:
            groups.append(runs)
            seen.update(id(t) for t in runs)
    # Chart caches and any stray <a:t> outside a paragraph.
    for tag in (C + "v", C + "f", A + "t"):
        for el in root.iter(tag):
            if id(el) not in seen:
                seen.add(id(el))
                groups.append([el])
    return groups


def _redact_text_part(
    raw: bytes, detect: Detect, replace: Replace, scrub_authors: bool
) -> Tuple[Optional[bytes], List[Entity], List[str]]:
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    chunks: List[str] = []
    changed = False

    for group in _text_groups(root):
        pieces = [(el, el.text or "") for el in group]
        _, redacted, ents, part_changed = rewrite_pieces(pieces, detect, replace)
        changed = changed or part_changed
        if redacted.strip():
            chunks.append(redacted)
        entities.extend(unpositioned(ents))

    if scrub_authors:
        # <p:cmAuthor name="..." initials="..."> and modern comment authors.
        for el in root.iter():
            for attr in ("name", "initials", "userId"):
                value = el.get(attr)
                if not value or not value.strip() or not el.tag.endswith("Author"):
                    continue
                entity = Entity(entity_type="DOCUMENT_AUTHOR", score=1.0, text=value)
                el.set(attr, replace(entity))
                entities.append(entity)
                changed = True

    return (serialize_part(root, root_tag) if changed else None), entities, chunks
