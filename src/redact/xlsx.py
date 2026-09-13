"""Excel (.xlsx) support — standard library only.

Spreadsheets are where PII actually accumulates in most organisations, and an
``.xlsx`` is the same OPC zip family as ``.docx`` (shared plumbing in
``opc.py``). Excel's storage model has its own traps:

* **Cell text is deduplicated.** Most text lives once in
  ``xl/sharedStrings.xml`` and cells merely reference it by index — so
  redacting one shared string redacts *every* cell that uses it, and the cell
  locations are recovered by walking the worksheets (reported in the notes).
* **Rich text splits runs.** A formatted cell stores
  ``<si><r><t>ja</t></r><r><t>ne@x.com</t></r></si>`` — the same split-run
  problem as Word, solved by the same ``rewrite_pieces``.
* **Formula results are cached.** A cell like ``=A1&"@x.com"`` stores its last
  computed string in ``<v>``; rewriting only that cache is useless because
  Excel recalculates on open and restores the PII. When a cached formula
  result matches, the *formula itself* is removed and the cell becomes a
  static redacted string.
* Comments carry **author names** and comment text; hyperlink ``mailto:``
  targets live in the ``.rels`` parts; text boxes live in ``xl/drawings``;
  ``docProps`` carries creator/lastModifiedBy/Manager; images live under
  ``xl/media`` and follow the same keep/strip/blur policy as Word.

Known limits (documented, not hidden): numeric cell values are not scanned (an
SSN stored as the number 123456789 is indistinguishable from any other id
without column context), defined names are untouched, and a ``vbaProject.bin``
macro payload in ``.xlsm`` is copied as-is.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .opc import (
    AUTHOR_ENTITY,
    CONTENT_TYPES,
    IMAGE_POLICIES,
    METADATA_TAGS,
    R_NS,
    Detect,
    ImageRedactor,
    OpcError,
    Replace,
    apply_text,
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

#: Backwards-consistent alias — an .xlsx failure is an OPC failure.
XlsxError = OpcError

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

_SHARED_STRINGS = "xl/sharedStrings.xml"
_WORKBOOK = "xl/workbook.xml"
_WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
_SHEET_PART = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
_COMMENTS_PART = re.compile(r"^xl/comments\d*\.xml$")
_DRAWING_PART = re.compile(r"^xl/drawings/drawing\d+\.xml$")
_MEDIA_PREFIX = "xl/media/"

#: How many cell locations a note lists before eliding.
_MAX_LOCATIONS = 6

__all__ = ["XlsxError", "XlsxRedaction", "extract_text", "redact_xlsx"]


@dataclass
class XlsxRedaction:
    """What :func:`redact_xlsx` found.

    Spreadsheets have no linear text flow, so entities carry no offsets;
    instead, findings in shared strings are located by *cell* in ``notes``
    (e.g. ``EMAIL_ADDRESS at Sheet1!B2, Sheet2!C7``).
    """

    entities: List[Entity] = field(default_factory=list)
    redacted_text: str = ""
    notes: List[str] = field(default_factory=list)


def extract_text(path) -> str:
    """All cell text (shared + inline strings), one string per line."""
    lines: List[str] = []
    with open_zip(path) as zf:
        names = zf.namelist()
        if _SHARED_STRINGS in names:
            root, _ = parse_part(zf.read(_SHARED_STRINGS))
            for si in root.iter(S + "si"):
                lines.append("".join(t.text or "" for t in si.iter(S + "t")))
        for name in sorted(n for n in names if _SHEET_PART.match(n)):
            root, _ = parse_part(zf.read(name))
            for c in root.iter(S + "c"):
                if c.get("t") == "inlineStr":
                    lines.append(
                        "".join(t.text or "" for t in c.iter(S + "t"))
                    )
    return "\n".join(lines)


def redact_xlsx(
    source: Path,
    out: Optional[Path],
    detect: Detect,
    replace: Replace,
    scrub_authors: bool = True,
    image_policy: str = "keep",
    image_redactor: Optional[ImageRedactor] = None,
) -> XlsxRedaction:
    """Redact ``source`` into ``out`` (``None`` = detect only, write nothing)."""
    if image_policy not in IMAGE_POLICIES:
        raise ValueError(f"image_policy must be one of {IMAGE_POLICIES}, got {image_policy!r}")

    result = XlsxRedaction()
    replacements: Dict[str, bytes] = {}
    renames: Dict[str, str] = {}
    redacted_lines: List[str] = []

    with open_zip(source) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]

        img_entities, img_notes = plan_images(
            zf, names, _MEDIA_PREFIX, image_policy, image_redactor, replacements, renames
        )
        result.entities.extend(img_entities)
        result.notes.extend(img_notes)

        # Where does each shared string appear? Needed to say *which cells* a
        # finding lives in, since the string itself is stored only once.
        sheet_names = _sheet_display_names(zf, names)
        locations = _shared_string_locations(zf, names, sheet_names)

        # Shared strings: the main body of cell text.
        if _SHARED_STRINGS in names:
            new, hits = _redact_shared_strings(
                zf.read(_SHARED_STRINGS), detect, replace, redacted_lines
            )
            if new is not None:
                replacements[_SHARED_STRINGS] = new
            for index, ents in hits:
                result.entities.extend(unpositioned(ents))
                cells = locations.get(index, [])
                if cells:
                    shown = ", ".join(cells[:_MAX_LOCATIONS])
                    more = f" (+{len(cells) - _MAX_LOCATIONS} more)" if len(cells) > _MAX_LOCATIONS else ""
                    labels = ", ".join(sorted({e.entity_type for e in ents}))
                    result.notes.append(f"{labels} at {shown}{more}")

        # Worksheets: inline strings, cached formula strings, header/footer text.
        for name in sorted(n for n in names if _SHEET_PART.match(n)):
            new, ents = _redact_sheet(zf.read(name), detect, replace, redacted_lines)
            result.entities.extend(unpositioned(ents))
            if new is not None:
                replacements[name] = new

        # Comments: author names and comment text.
        for name in sorted(n for n in names if _COMMENTS_PART.match(n)):
            new, ents = _redact_comments(zf.read(name), detect, replace, scrub_authors)
            result.entities.extend(unpositioned(ents))
            if new is not None:
                replacements[name] = new

        # Text boxes and shapes in drawings.
        for name in sorted(n for n in names if _DRAWING_PART.match(n)):
            new, ents = _redact_drawing(zf.read(name), detect, replace)
            result.entities.extend(unpositioned(ents))
            if new is not None:
                replacements[name] = new

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

        result.redacted_text = "\n".join(redacted_lines)
        if out is not None:
            write_zip(zf, infos, out, replacements, renames)
    return result


# -- cell location mapping -------------------------------------------------------

def _sheet_display_names(zf, names) -> Dict[str, str]:
    """Map worksheet part name (``xl/worksheets/sheet1.xml``) -> display name."""
    mapping: Dict[str, str] = {}
    if _WORKBOOK not in names or _WORKBOOK_RELS not in names:
        return mapping
    rels_root, _ = parse_part(zf.read(_WORKBOOK_RELS))
    rid_to_part = {}
    for rel in rels_root.iter(R_NS + "Relationship"):
        target = rel.get("Target", "")
        rid_to_part[rel.get("Id")] = posixpath.normpath(posixpath.join("xl", target))
    wb_root, _ = parse_part(zf.read(_WORKBOOK))
    for sheet in wb_root.iter(S + "sheet"):
        part = rid_to_part.get(sheet.get(_R_ATTR))
        if part:
            mapping[part] = sheet.get("name", part)
    return mapping


def _shared_string_locations(zf, names, sheet_names) -> Dict[int, List[str]]:
    """Shared-string index -> the cells referencing it (``Sheet1!B2``)."""
    locations: Dict[int, List[str]] = defaultdict(list)
    for part in sorted(n for n in names if _SHEET_PART.match(n)):
        display = sheet_names.get(part, Path(part).stem)
        root, _ = parse_part(zf.read(part))
        for c in root.iter(S + "c"):
            if c.get("t") != "s":
                continue
            v = c.find(S + "v")
            if v is None or v.text is None:
                continue
            try:
                locations[int(v.text)].append(f"{display}!{c.get('r', '?')}")
            except ValueError:
                continue
    return locations


# -- part rewriters ----------------------------------------------------------------

def _redact_shared_strings(
    raw: bytes, detect: Detect, replace: Replace, redacted_lines: List[str]
) -> Tuple[Optional[bytes], List[Tuple[int, List[Entity]]]]:
    root, root_tag = parse_part(raw)
    hits: List[Tuple[int, List[Entity]]] = []
    changed = False
    for index, si in enumerate(root.iter(S + "si")):
        pieces = [(t, t.text or "") for t in si.iter(S + "t")]
        if not pieces:
            continue
        _, redacted, ents, part_changed = rewrite_pieces(pieces, detect, replace)
        redacted_lines.append(redacted)
        if ents:
            hits.append((index, ents))
        changed = changed or part_changed
    return (serialize_part(root, root_tag) if changed else None), hits


def _redact_sheet(
    raw: bytes, detect: Detect, replace: Replace, redacted_lines: List[str]
) -> Tuple[Optional[bytes], List[Entity]]:
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    changed = False

    for c in root.iter(S + "c"):
        kind = c.get("t")
        if kind == "inlineStr":
            pieces = [(t, t.text or "") for t in c.iter(S + "t")]
            if not pieces:
                continue
            _, redacted, ents, part_changed = rewrite_pieces(pieces, detect, replace)
            redacted_lines.append(redacted)
            entities.extend(ents)
            changed = changed or part_changed
        elif kind == "str":
            # A cached formula result. Rewriting the cache alone is pointless —
            # Excel recalculates on open and restores the value — so a matching
            # cell loses its formula and becomes a static redacted string.
            v = c.find(S + "v")
            if v is None or not v.text:
                continue
            new, ents = apply_text(v.text, detect, replace)
            if not ents:
                continue
            for child in list(c):
                c.remove(child)
            c.set("t", "inlineStr")
            is_el = ET.SubElement(c, S + "is")
            t_el = ET.SubElement(is_el, S + "t")
            t_el.text = new
            entities.extend(ents)
            redacted_lines.append(new)
            changed = True

    # Page header/footer text can carry names ("&LPrepared by Jane Doe").
    for tag in ("oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"):
        for el in root.iter(S + tag):
            if not el.text:
                continue
            new, ents = apply_text(el.text, detect, replace)
            if ents:
                el.text = new
                entities.extend(ents)
                changed = True

    # <hyperlink display="mailto:..."> duplicates the target in the sheet itself.
    for link in root.iter(S + "hyperlink"):
        for attr in ("display", "tooltip", "location"):
            value = link.get(attr)
            if not value:
                continue
            new, ents = apply_text(value, detect, replace)
            if ents:
                link.set(attr, new)
                entities.extend(ents)
                changed = True

    return (serialize_part(root, root_tag) if changed else None), entities


def _redact_comments(
    raw: bytes, detect: Detect, replace: Replace, scrub_authors: bool
) -> Tuple[Optional[bytes], List[Entity]]:
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    changed = False
    if scrub_authors:
        for author in root.iter(S + "author"):
            if author.text and author.text.strip():
                entity = Entity(entity_type=AUTHOR_ENTITY, score=1.0, text=author.text)
                author.text = replace(entity)
                entities.append(entity)
                changed = True
    for comment in root.iter(S + "comment"):
        pieces = [(t, t.text or "") for t in comment.iter(S + "t")]
        if not pieces:
            continue
        _, _, ents, part_changed = rewrite_pieces(pieces, detect, replace)
        entities.extend(ents)
        changed = changed or part_changed
    return (serialize_part(root, root_tag) if changed else None), entities


def _redact_drawing(
    raw: bytes, detect: Detect, replace: Replace
) -> Tuple[Optional[bytes], List[Entity]]:
    """Text boxes and shape labels: <a:p> paragraphs of <a:t> runs."""
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    changed = False
    for p in root.iter(A + "p"):
        pieces = [(t, t.text or "") for t in p.iter(A + "t")]
        if not pieces:
            continue
        _, _, ents, part_changed = rewrite_pieces(pieces, detect, replace)
        entities.extend(ents)
        changed = changed or part_changed
    return (serialize_part(root, root_tag) if changed else None), entities
