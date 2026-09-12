"""Word (.docx) support — standard library only.

A ``.docx`` is a zip of XML parts. Visible text lives in ``<w:t>`` runs inside
``<w:p>`` paragraphs, and Word freely splits one word across several runs
(after spell-check, formatting changes, or edits), so an email address may be
scattered over three ``<w:t>`` elements. Detection therefore runs on each
*paragraph's* concatenated text; the replacement is written into the run where
the entity starts (so it inherits that run's formatting) and the entity's
remainder is stripped from the following runs.

Parts processed: the main body, headers, footers, footnotes, endnotes and
comments. ``docProps/core.xml`` author fields are scrubbed too, since they carry
names. Everything else (images, styles, relationships) is copied byte-for-byte.

Round-tripping through ElementTree has one trap: it only re-declares the
namespaces actually used, but Word requires every prefix listed in
``mc:Ignorable`` to be declared, or it reports the file as corrupt. The original
root start tag is therefore preserved verbatim on the way out.

Known limits: text inside tracked deletions (``w:delText``), field codes and
embedded images is untouched.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .types import Entity

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
)
_CORE_PART = "docProps/core.xml"
_AUTHOR_TAGS = (
    "{http://purl.org/dc/elements/1.1/}creator",
    "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy",
)
#: Entity label reported for scrubbed author metadata.
AUTHOR_ENTITY = "DOCUMENT_AUTHOR"

_XML_DECL = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
_NS_DECL = re.compile(r'xmlns:([\w.-]+)="([^"]*)"')
_FIRST_TAG = re.compile(r"<(?![?!])[^>]+>", re.S)  # first element start tag

Detect = Callable[[str], List[Entity]]
Replace = Callable[[Entity], str]


class DocxError(Exception):
    """Raised when a .docx cannot be read or parsed."""


@dataclass
class DocxRedaction:
    """What :func:`redact_docx` found; offsets index :attr:`redacted_text`'s source."""

    entities: List[Entity] = field(default_factory=list)
    redacted_text: str = ""


def extract_text(path) -> str:
    """Plain text of every text-bearing part, one paragraph per line."""
    chunks: List[str] = []
    with _open(path) as zf:
        for name in sorted(zf.namelist(), key=_part_order):
            if not _TEXT_PARTS.match(name):
                continue
            root, _ = _parse(zf.read(name))
            seen: Set[int] = set()
            for p in root.iter(W + "p"):
                pieces = _pieces(p, seen)
                if pieces:
                    chunks.append("".join(t for _, t in pieces))
    return "\n".join(chunks)


def redact_docx(
    source: Path,
    out: Optional[Path],
    detect: Detect,
    replace: Replace,
    scrub_authors: bool = True,
) -> DocxRedaction:
    """Redact ``source`` into ``out`` (``None`` = detect only, write nothing).

    ``detect`` receives one paragraph's text and returns positioned entities;
    ``replace`` maps an entity to its replacement string.
    """
    result = DocxRedaction()
    text_chunks: List[str] = []
    replacements: Dict[str, bytes] = {}
    offset = 0

    with _open(source) as zf:
        infos = zf.infolist()
        for info in sorted(infos, key=lambda i: _part_order(i.filename)):
            name = info.filename
            if _TEXT_PARTS.match(name):
                new, ents, chunks, offset = _redact_part(zf.read(name), detect, replace, offset)
                result.entities.extend(ents)
                text_chunks.extend(chunks)
                if new is not None:
                    replacements[name] = new
            elif name == _CORE_PART and scrub_authors:
                new, ents = _scrub_authors(zf.read(name), replace)
                result.entities.extend(ents)
                if new is not None:
                    replacements[name] = new
        result.redacted_text = "\n".join(text_chunks)

        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(out, "w") as zout:
                for info in infos:  # original order keeps [Content_Types].xml first
                    data = replacements.get(info.filename)
                    if data is None:
                        zout.writestr(info, zf.read(info.filename))
                    else:
                        zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        zout.writestr(zi, data)
    return result


# -- internals ---------------------------------------------------------------

def _open(path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocxError(f"not a valid .docx: {exc}") from exc


def _part_order(name: str) -> Tuple[bool, str]:
    return (name != "word/document.xml", name)  # body first, then the rest


def _parse(raw: bytes) -> Tuple[ET.Element, Optional[str]]:
    """Parse a part, registering its prefixes and keeping its root start tag."""
    text = raw.decode("utf-8")
    for prefix, uri in _NS_DECL.findall(text):
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:  # reserved "nsN" prefixes
            pass
    m = _FIRST_TAG.search(text)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DocxError(f"malformed XML part: {exc}") from exc
    return root, (m.group(0) if m else None)


def _serialize(root: ET.Element, original_root_tag: Optional[str]) -> bytes:
    body = ET.tostring(root, encoding="unicode")
    m = _FIRST_TAG.search(body)
    if original_root_tag and m:
        merged = _merge_root_tag(original_root_tag, m.group(0))
        body = body[: m.start()] + merged + body[m.end():]
    return _XML_DECL + body.encode("utf-8")


def _merge_root_tag(original: str, generated: str) -> str:
    """Original root tag, plus any namespace ElementTree used that it lacked."""
    have = dict(_NS_DECL.findall(original))
    extra = [(p, u) for p, u in _NS_DECL.findall(generated) if p not in have]
    if not extra:
        return original
    decls = "".join(f' xmlns:{p}="{u}"' for p, u in extra)
    tag = original.rstrip()
    if tag.endswith("/>"):
        return tag[:-2] + decls + "/>"
    return tag[:-1] + decls + ">"


def _pieces(p: ET.Element, seen: Set[int]) -> List[Tuple[Optional[ET.Element], str]]:
    """The paragraph's text-bearing children in order: ``(element|None, text)``.

    Tabs and breaks contribute a character but are not editable (``None``).
    ``seen`` prevents a paragraph nested in a text box from being processed twice.
    """
    pieces: List[Tuple[Optional[ET.Element], str]] = []
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


def _redact_part(
    raw: bytes, detect: Detect, replace: Replace, offset: int
) -> Tuple[Optional[bytes], List[Entity], List[str], int]:
    root, root_tag = _parse(raw)
    entities: List[Entity] = []
    chunks: List[str] = []
    seen: Set[int] = set()
    changed = False

    for p in root.iter(W + "p"):
        pieces = _pieces(p, seen)
        if not pieces:
            continue
        full = "".join(t for _, t in pieces)
        ents = sorted(
            (e for e in detect(full) if e.start is not None) if full.strip() else [],
            key=lambda e: e.start,
        )
        reps = {id(e): replace(e) for e in ents}

        # Rewrite runs: the replacement lands in the run where the entity
        # starts; later runs covered by the same entity lose that text.
        pos = 0
        for el, text in pieces:
            a, b = pos, pos + len(text)
            pos = b
            if el is None or not ents:
                continue
            new, cur = "", a
            for e in ents:
                if e.end <= a or e.start >= b:
                    continue
                new += full[cur:max(e.start, a)]
                if e.start >= a:
                    new += reps[id(e)]
                cur = min(e.end, b)
            new += full[cur:b]
            if new != text:
                el.text = new
                el.set(_XML_SPACE, "preserve")
                changed = True

        red, cur = "", 0
        for e in ents:
            red += full[cur:e.start] + reps[id(e)]
            cur = e.end
        chunks.append(red + full[cur:])
        entities.extend(
            Entity(e.entity_type, e.score, e.start + offset, e.end + offset, e.text)
            for e in ents
        )
        offset += len(full) + 1  # +1 for the newline joining paragraphs

    return (_serialize(root, root_tag) if changed else None), entities, chunks, offset


def _scrub_authors(raw: bytes, replace: Replace) -> Tuple[Optional[bytes], List[Entity]]:
    root, root_tag = _parse(raw)
    entities: List[Entity] = []
    for tag in _AUTHOR_TAGS:
        for el in root.iter(tag):
            if el.text and el.text.strip():
                entity = Entity(entity_type=AUTHOR_ENTITY, score=1.0, text=el.text)
                el.text = replace(entity)
                entities.append(entity)
    return (_serialize(root, root_tag) if entities else None), entities
