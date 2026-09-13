"""Word (.docx) support — standard library only.

A ``.docx`` is an OPC zip of XML parts (shared plumbing lives in ``opc.py``).
Visible text lives in ``<w:t>`` runs inside ``<w:p>`` paragraphs, and Word
freely splits one word across several runs, so detection runs on each
*paragraph's* concatenated text and is written back run by run.

Text that is not visible in the rendered document is redacted too, because it
still ships inside the file:

* **Tracked deletions** (``<w:delText>``) — text someone removed with Track
  Changes on. It is invisible until the reviewer toggles markup, and survives
  "reject all changes". Each ``<w:del>`` is scanned as its own segment so its
  text is never concatenated into the visible flow.
* **Field codes** (``<w:instrText>``, ``<w:fldSimple w:instr>``) — e.g.
  ``HYPERLINK "mailto:jane@example.com"``.
* **Revision and comment authors** (``w:author``/``w:initials``) and the author
  fields in ``docProps``.
* **External hyperlink targets** in the ``.rels`` parts.
* **Embedded images** under ``word/media/`` — see ``image_policy``.

Everything else (styles, numbering, relationships) is copied byte-for-byte.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .opc import (
    AUTHOR_ENTITY,
    CONTENT_TYPES,
    IMAGE_ENTITY,
    IMAGE_POLICIES,
    METADATA_TAGS,
    Detect,
    ImageRedactor,
    OpcError,
    Piece,
    Replace,
    apply_text,
    blank_png as _blank_png,  # re-exported for compatibility
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

#: Backwards-compatible alias — a .docx failure is an OPC failure.
DocxError = OpcError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
)
_MEDIA_PREFIX = "word/media/"

# Attributes naming the person behind a revision or comment.
_AUTHOR_ATTRS = (W + "author", W + "initials")

__all__ = [
    "_blank_png",  # used by tests to validate the placeholder
    "AUTHOR_ENTITY",
    "IMAGE_ENTITY",
    "IMAGE_POLICIES",
    "DocxError",
    "DocxRedaction",
    "extract_text",
    "redact_docx",
]


@dataclass
class DocxRedaction:
    """What :func:`redact_docx` found.

    ``entities`` offsets index the document's *source* visible text (every
    paragraph joined by newlines), matching how the plain-text backends report
    offsets. Entities found in non-visible content (tracked deletions, field
    codes, metadata, images) carry ``start``/``end`` of ``None``, since they
    have no position in the rendered document.
    """

    entities: List[Entity] = field(default_factory=list)
    redacted_text: str = ""
    notes: List[str] = field(default_factory=list)


def extract_text(path) -> str:
    """Plain visible text of every text-bearing part, one paragraph per line."""
    chunks: List[str] = []
    with open_zip(path) as zf:
        for name in sorted(zf.namelist(), key=_part_order):
            if not _TEXT_PARTS.match(name):
                continue
            root, _ = parse_part(zf.read(name))
            seen: Set[int] = set()
            for p in root.iter(W + "p"):
                pieces = _visible_pieces(p, seen)
                if pieces:
                    chunks.append("".join(t for _, t in pieces))
    return "\n".join(chunks)


def redact_docx(
    source: Path,
    out: Optional[Path],
    detect: Detect,
    replace: Replace,
    scrub_authors: bool = True,
    image_policy: str = "keep",
    image_redactor: Optional[ImageRedactor] = None,
) -> DocxRedaction:
    """Redact ``source`` into ``out`` (``None`` = detect only, write nothing).

    ``detect`` receives one segment of text and returns positioned entities;
    ``replace`` maps an entity to its replacement string. Together they let any
    text engine (the builtin regex engine, Presidio, …) drive Word redaction.

    ``image_policy`` decides what happens to ``word/media/*``:

    ``keep``   leave images untouched (default); a note reports how many.
    ``strip``  replace every image with a blank 1x1 PNG — guarantees nothing
               survives in image form. Parts are renamed to ``.png`` and the
               referencing relationships updated, so the file stays valid.
    ``blur``   hand each image to ``image_redactor`` (e.g. the deface backend)
               and keep its output; any image the redactor declines is stripped
               instead.
    """
    if image_policy not in IMAGE_POLICIES:
        raise ValueError(f"image_policy must be one of {IMAGE_POLICIES}, got {image_policy!r}")

    result = DocxRedaction()
    replacements: Dict[str, bytes] = {}
    renames: Dict[str, str] = {}

    with open_zip(source) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]

        # 1. Images first: renames must be known before the .rels parts are rewritten.
        img_entities, img_notes = plan_images(
            zf, names, _MEDIA_PREFIX, image_policy, image_redactor, replacements, renames
        )
        result.entities.extend(img_entities)
        result.notes.extend(img_notes)

        # 2. Text parts, in document-first order so visible offsets accumulate sanely.
        offset = 0
        chunks: List[str] = []
        for name in sorted((n for n in names if _TEXT_PARTS.match(n)), key=_part_order):
            new, ents, part_chunks, offset = _redact_text_part(
                zf.read(name), detect, replace, offset, scrub_authors
            )
            result.entities.extend(ents)
            chunks.extend(part_chunks)
            if new is not None:
                replacements[name] = new
        result.redacted_text = "\n".join(chunks)

        # 3. Document metadata.
        if scrub_authors:
            for part, tags in METADATA_TAGS.items():
                if part not in names:
                    continue
                new, ents = scrub_metadata(zf.read(part), replace, tags)
                result.entities.extend(ents)
                if new is not None:
                    replacements[part] = new

        # 4. Relationships: retarget renamed media, scrub external link targets.
        for name in names:
            if not name.endswith(".rels"):
                continue
            new, ents = rewrite_rels(zf.read(name), name, renames, detect, replace)
            result.entities.extend(ents)
            if new is not None:
                replacements[name] = new

        # 5. Content types must declare png once images have been renamed to it.
        if renames and CONTENT_TYPES in names:
            new = ensure_png_default(zf.read(CONTENT_TYPES))
            if new is not None:
                replacements[CONTENT_TYPES] = new

        if out is not None:
            write_zip(zf, infos, out, replacements, renames)
    return result


# -- Word-specific internals ---------------------------------------------------

def _part_order(name: str) -> Tuple[bool, str]:
    return (name != "word/document.xml", name)  # body first, then the rest


def _visible_pieces(p: ET.Element, seen: Set[int]) -> List[Piece]:
    """The paragraph's *rendered* text in order: ``(element|None, text)``.

    Tabs and breaks contribute a character but are not editable (``None``).
    ``seen`` prevents a paragraph nested in a text box being processed twice.
    """
    pieces: List[Piece] = []
    for el in p.iter():
        if id(el) in seen:
            continue
        if el.tag == W + "t":
            seen.add(id(el))
            pieces.append((el, el.text or ""))
        elif el.tag == W + "tab":
            seen.add(id(el))
            pieces.append((None, "\t"))
        elif el.tag in (W + "br", W + "cr"):
            seen.add(id(el))
            pieces.append((None, "\n"))
    return pieces


def _hidden_groups(root: ET.Element) -> List[List[ET.Element]]:
    """Non-rendered text segments: tracked deletions and field codes.

    Each ``<w:del>``'s ``<w:delText>`` children form one segment (so deleted
    text is never concatenated into the visible flow), and each maximal run of
    consecutive ``<w:instrText>`` siblings forms another.
    """
    groups: List[List[ET.Element]] = []
    for el in root.iter():
        if el.tag == W + "del":
            deleted = [d for d in el.iter(W + "delText")]
            if deleted:
                groups.append(deleted)
    # Field codes: group consecutive instrText elements within each parent.
    for parent in root.iter():
        run: List[ET.Element] = []
        for child in list(parent):
            if child.tag == W + "instrText":
                run.append(child)
                continue
            instr = [i for i in child.iter(W + "instrText")] if len(child) else []
            if instr:
                run.extend(instr)
                continue
            if run:
                groups.append(run)
                run = []
        if run:
            groups.append(run)
    return groups


def _redact_text_part(
    raw: bytes, detect: Detect, replace: Replace, offset: int, scrub_authors: bool
) -> Tuple[Optional[bytes], List[Entity], List[str], int]:
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    chunks: List[str] = []
    seen: Set[int] = set()
    changed = False

    # Visible paragraph text — offsets are meaningful and accumulate.
    for p in root.iter(W + "p"):
        pieces = _visible_pieces(p, seen)
        if not pieces:
            continue
        full, redacted, ents, part_changed = rewrite_pieces(pieces, detect, replace)
        changed = changed or part_changed
        chunks.append(redacted)
        entities.extend(
            Entity(e.entity_type, e.score, e.start + offset, e.end + offset, e.text)
            for e in ents
        )
        offset += len(full) + 1  # +1 for the newline joining paragraphs

    # Hidden text — redacted all the same, but reported without a position.
    for group in _hidden_groups(root):
        pieces = [(el, el.text or "") for el in group]
        _, _, ents, part_changed = rewrite_pieces(pieces, detect, replace)
        changed = changed or part_changed
        entities.extend(unpositioned(ents))

    # Field codes carried as an attribute (<w:fldSimple w:instr="...">).
    for el in root.iter(W + "fldSimple"):
        instr = el.get(W + "instr")
        if not instr:
            continue
        new, ents = apply_text(instr, detect, replace)
        if ents:
            el.set(W + "instr", new)
            entities.extend(unpositioned(ents))
            changed = True

    if scrub_authors:
        ents, author_changed = _scrub_author_attrs(root, replace)
        entities.extend(ents)
        changed = changed or author_changed

    return (serialize_part(root, root_tag) if changed else None), entities, chunks, offset


def _scrub_author_attrs(root: ET.Element, replace: Replace) -> Tuple[List[Entity], bool]:
    """Replace ``w:author``/``w:initials`` on revision and comment marks."""
    entities: List[Entity] = []
    changed = False
    for el in root.iter():
        for attr in _AUTHOR_ATTRS:
            value = el.get(attr)
            if not value or not value.strip():
                continue
            entity = Entity(entity_type=AUTHOR_ENTITY, score=1.0, text=value)
            el.set(attr, replace(entity))
            entities.append(entity)
            changed = True
    return entities, changed
