"""Word (.docx) support — standard library only.

A ``.docx`` is a zip of XML parts. Visible text lives in ``<w:t>`` runs inside
``<w:p>`` paragraphs, and Word freely splits one word across several runs
(after spell-check, formatting changes, or edits), so an email address may be
scattered over three ``<w:t>`` elements. Detection therefore runs on each
*paragraph's* concatenated text; the replacement is written into the run where
the entity starts (so it inherits that run's formatting) and the entity's
remainder is stripped from the following runs.

Text that is not visible in the rendered document is redacted too, because it
still ships inside the file:

* **Tracked deletions** (``<w:delText>``) — text someone removed with Track
  Changes on. It is invisible until the reviewer toggles markup, and survives
  "reject all changes". Each ``<w:del>`` is scanned as its own segment so its
  text is never concatenated into the visible flow.
* **Field codes** (``<w:instrText>``, ``<w:fldSimple w:instr>``) — e.g.
  ``HYPERLINK "mailto:jane@example.com"``.
* **Revision and comment authors** (``w:author``/``w:initials`` on ``w:del``,
  ``w:ins``, ``w:comment``, ``w:moveFrom``, ``w:moveTo``) and the author fields
  in ``docProps`` (``dc:creator``, ``cp:lastModifiedBy``, ``Manager``).
* **External hyperlink targets** in the ``.rels`` parts — a ``mailto:`` link's
  address lives there, not in the body.
* **Embedded images** under ``word/media/`` — see ``image_policy``.

Everything else (styles, numbering, relationships) is copied byte-for-byte.

Round-tripping through ElementTree has one trap: it only re-declares the
namespaces actually used, but Word requires every prefix listed in
``mc:Ignorable`` to be declared, or it reports the file as corrupt. The original
root start tag is therefore preserved verbatim on the way out.
"""

from __future__ import annotations

import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .types import Entity

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
)
_CORE_PART = "docProps/core.xml"
_APP_PART = "docProps/app.xml"
_CONTENT_TYPES = "[Content_Types].xml"
_MEDIA_PREFIX = "word/media/"

# Metadata elements that carry a person's name.
_METADATA_TAGS = {
    _CORE_PART: (
        "{http://purl.org/dc/elements/1.1/}creator",
        "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy",
    ),
    _APP_PART: (
        "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Manager",
    ),
}
# Attributes naming the person behind a revision or comment.
_AUTHOR_ATTRS = (W + "author", W + "initials")

#: Entity label reported for a scrubbed author name.
AUTHOR_ENTITY = "DOCUMENT_AUTHOR"
#: Entity label reported for an embedded image that was stripped or blurred.
IMAGE_ENTITY = "EMBEDDED_IMAGE"

#: Valid values for ``redact_docx(image_policy=...)``.
IMAGE_POLICIES = ("keep", "strip", "blur")

_XML_DECL = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
_NS_DECL = re.compile(r'xmlns:([\w.-]+)="([^"]*)"')
_DEFAULT_NS = re.compile(r'xmlns="([^"]*)"')
_FIRST_TAG = re.compile(r"<(?![?!])[^>]+>", re.S)  # first element start tag

Detect = Callable[[str], List[Entity]]
Replace = Callable[[Entity], str]
#: ``(image_bytes, suffix) -> new_bytes | None``. Returning ``None`` means the
#: redactor could not process that image, and it is stripped instead.
ImageRedactor = Callable[[bytes, str], Optional[bytes]]

Piece = Tuple[Optional[ET.Element], str]


class DocxError(Exception):
    """Raised when a .docx cannot be read or parsed."""


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
    with _open(path) as zf:
        for name in sorted(zf.namelist(), key=_part_order):
            if not _TEXT_PARTS.match(name):
                continue
            root, _ = _parse(zf.read(name))
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
    ``blur``   hand each image to ``image_redactor`` (e.g. the Anonymizer
               backend) and keep its output; any image the redactor declines is
               stripped instead.
    """
    if image_policy not in IMAGE_POLICIES:
        raise ValueError(f"image_policy must be one of {IMAGE_POLICIES}, got {image_policy!r}")

    result = DocxRedaction()
    replacements: Dict[str, bytes] = {}
    renames: Dict[str, str] = {}

    with _open(source) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]

        # 1. Images first: renames must be known before the .rels parts are rewritten.
        _plan_images(zf, names, image_policy, image_redactor, replacements, renames, result)

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
            for part, tags in _METADATA_TAGS.items():
                if part not in names:
                    continue
                new, ents = _scrub_metadata(zf.read(part), replace, tags)
                result.entities.extend(ents)
                if new is not None:
                    replacements[part] = new

        # 4. Relationships: retarget renamed media, scrub external link targets.
        for name in names:
            if not name.endswith(".rels"):
                continue
            new, ents = _rewrite_rels(zf.read(name), name, renames, detect, replace)
            result.entities.extend(ents)
            if new is not None:
                replacements[name] = new

        # 5. Content types must declare png once images have been renamed to it.
        if renames and _CONTENT_TYPES in names:
            new = _ensure_png_default(zf.read(_CONTENT_TYPES))
            if new is not None:
                replacements[_CONTENT_TYPES] = new

        if out is not None:
            _write(zf, infos, out, replacements, renames)
    return result


# -- images ------------------------------------------------------------------

def _plan_images(
    zf: zipfile.ZipFile,
    names: Sequence[str],
    policy: str,
    redactor: Optional[ImageRedactor],
    replacements: Dict[str, bytes],
    renames: Dict[str, str],
    result: DocxRedaction,
) -> None:
    media = [n for n in names if n.startswith(_MEDIA_PREFIX)]
    if not media:
        return
    if policy == "keep":
        result.notes.append(
            f"{len(media)} embedded image(s) left untouched "
            "(use --docx-images strip|blur to handle them)"
        )
        return

    blurred = stripped = 0
    for name in media:
        data = zf.read(name)
        new_bytes = None
        if policy == "blur" and redactor is not None:
            try:
                new_bytes = redactor(data, PurePosixPath(name).suffix)
            except Exception:  # a failing redactor must not lose the document
                new_bytes = None
        if new_bytes is None:
            # Stripping is the floor: an image we cannot process is never left
            # in place, and the note must say so rather than claim a blur.
            stripped += 1
            new_bytes = _blank_png()
            renamed = str(PurePosixPath(name).with_suffix(".png"))
            if renamed != name:
                renames[name] = renamed
        else:
            blurred += 1
        replacements[name] = new_bytes
        result.entities.append(
            Entity(entity_type=IMAGE_ENTITY, score=1.0, text=PurePosixPath(name).name)
        )
    if blurred and stripped:
        result.notes.append(
            f"{blurred} embedded image(s) blurred, {stripped} stripped (could not be processed)"
        )
    elif blurred:
        result.notes.append(f"{blurred} embedded image(s) blurred")
    else:
        result.notes.append(f"{stripped} embedded image(s) stripped")


def _blank_png() -> bytes:
    """A valid 1x1 fully transparent PNG, built rather than hard-coded."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # 1x1, 8-bit RGBA
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")        # filter byte + RGBA pixel
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


# -- text parts --------------------------------------------------------------

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


def _rewrite_pieces(
    pieces: Sequence[Piece], detect: Detect, replace: Replace
) -> Tuple[str, str, List[Entity], bool]:
    """Detect over the joined text and write replacements back into the elements.

    Returns ``(original, redacted, entities, changed)``. A replacement lands in
    the element where its entity *starts*, so it inherits that run's formatting;
    elements covering the rest of the entity lose those characters.
    """
    full = "".join(t for _, t in pieces)
    if not full.strip():
        return full, full, [], False
    entities = sorted(
        (e for e in detect(full) if e.start is not None and e.end is not None),
        key=lambda e: e.start,
    )
    if not entities:
        return full, full, [], False
    reps = {id(e): replace(e) for e in entities}

    changed = False
    pos = 0
    for el, text in pieces:
        a, b = pos, pos + len(text)
        pos = b
        if el is None:
            continue
        new, cur = "", a
        for e in entities:
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

    redacted, cur = "", 0
    for e in entities:
        redacted += full[cur:e.start] + reps[id(e)]
        cur = e.end
    return full, redacted + full[cur:], entities, changed


def _redact_text_part(
    raw: bytes, detect: Detect, replace: Replace, offset: int, scrub_authors: bool
) -> Tuple[Optional[bytes], List[Entity], List[str], int]:
    root, root_tag = _parse(raw)
    entities: List[Entity] = []
    chunks: List[str] = []
    seen: Set[int] = set()
    changed = False

    # Visible paragraph text — offsets are meaningful and accumulate.
    for p in root.iter(W + "p"):
        pieces = _visible_pieces(p, seen)
        if not pieces:
            continue
        full, redacted, ents, part_changed = _rewrite_pieces(pieces, detect, replace)
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
        _, _, ents, part_changed = _rewrite_pieces(pieces, detect, replace)
        changed = changed or part_changed
        entities.extend(_unpositioned(ents))

    # Field codes carried as an attribute (<w:fldSimple w:instr="...">).
    for el in root.iter(W + "fldSimple"):
        instr = el.get(W + "instr")
        if not instr:
            continue
        new, ents = _apply(instr, detect, replace)
        if ents:
            el.set(W + "instr", new)
            entities.extend(_unpositioned(ents))
            changed = True

    if scrub_authors:
        ents, author_changed = _scrub_author_attrs(root, replace)
        entities.extend(ents)
        changed = changed or author_changed

    return (_serialize(root, root_tag) if changed else None), entities, chunks, offset


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


def _scrub_metadata(
    raw: bytes, replace: Replace, tags: Sequence[str]
) -> Tuple[Optional[bytes], List[Entity]]:
    root, root_tag = _parse(raw)
    entities: List[Entity] = []
    for tag in tags:
        for el in root.iter(tag):
            if el.text and el.text.strip():
                entity = Entity(entity_type=AUTHOR_ENTITY, score=1.0, text=el.text)
                el.text = replace(entity)
                entities.append(entity)
    return (_serialize(root, root_tag) if entities else None), entities


# -- relationships & content types -------------------------------------------

def _rewrite_rels(
    raw: bytes, rels_name: str, renames: Dict[str, str], detect: Detect, replace: Replace
) -> Tuple[Optional[bytes], List[Entity]]:
    """Retarget renamed media parts and redact external hyperlink targets."""
    root, root_tag = _parse(raw)
    entities: List[Entity] = []
    changed = False
    base = _rels_base(rels_name)

    for rel in root.iter(R_NS + "Relationship"):
        target = rel.get("Target")
        if not target:
            continue
        if rel.get("TargetMode") == "External":
            # A mailto:/tel: address lives here, not in the document body.
            new, ents = _apply(target, detect, replace)
            if ents:
                rel.set("Target", new)
                entities.extend(_unpositioned(ents))
                changed = True
            continue
        resolved = _resolve_target(base, target)
        if resolved in renames:
            rel.set("Target", posixpath.relpath(renames[resolved], base) if base else renames[resolved])
            changed = True
    return (_serialize(root, root_tag) if changed else None), entities


def _rels_base(rels_name: str) -> str:
    """``word/_rels/document.xml.rels`` -> ``word``; ``_rels/.rels`` -> ``""``."""
    parent = posixpath.dirname(rels_name)          # word/_rels
    return posixpath.dirname(parent)               # word


def _resolve_target(base: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def _ensure_png_default(raw: bytes) -> Optional[bytes]:
    """Add ``<Default Extension="png">`` if the package does not declare it."""
    root, root_tag = _parse(raw)
    for default in root.iter(CT_NS + "Default"):
        if (default.get("Extension") or "").lower() == "png":
            return None
    el = ET.SubElement(root, CT_NS + "Default")
    el.set("Extension", "png")
    el.set("ContentType", "image/png")
    return _serialize(root, root_tag)


# -- shared helpers ----------------------------------------------------------

def _apply(text: str, detect: Detect, replace: Replace) -> Tuple[str, List[Entity]]:
    """Redact a standalone string (attribute or relationship target)."""
    entities = sorted(
        (e for e in detect(text) if e.start is not None and e.end is not None),
        key=lambda e: e.start,
    )
    if not entities:
        return text, []
    out, cur = "", 0
    for e in entities:
        out += text[cur:e.start] + replace(e)
        cur = e.end
    return out + text[cur:], entities


def _unpositioned(entities: Sequence[Entity]) -> List[Entity]:
    """Strip offsets from entities found outside the rendered text flow."""
    return [Entity(e.entity_type, e.score, None, None, e.text) for e in entities]


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
    # A default namespace (``xmlns="..."``, used by .rels and [Content_Types].xml)
    # must be registered too: otherwise ElementTree writes children as <ns0:Foo>
    # while the preserved root tag says <Foo>, and the part no longer parses.
    if m:
        default = _DEFAULT_NS.search(m.group(0))
        if default:
            ET.register_namespace("", default.group(1))
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


def _write(
    zf: zipfile.ZipFile,
    infos: Sequence[zipfile.ZipInfo],
    out: Path,
    replacements: Dict[str, bytes],
    renames: Dict[str, str],
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as zout:
        for info in infos:  # original order keeps [Content_Types].xml first
            name = info.filename
            new_name = renames.get(name, name)
            data = replacements.get(name)
            if data is None and new_name == name:
                zout.writestr(info, zf.read(name))
                continue
            zi = zipfile.ZipInfo(new_name, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data if data is not None else zf.read(name))
