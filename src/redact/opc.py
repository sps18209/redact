"""Shared plumbing for Office Open XML packages (.docx, .xlsx, ...).

Both Word and Excel files are OPC containers: a zip of XML parts plus
relationships, content types, document properties and embedded media. This
module holds everything that is format-agnostic — parsing parts without
mangling their namespaces, rewriting split text runs, the embedded-image
policy, relationship retargeting, and writing the package back out — so
``docx.py`` and ``xlsx.py`` contain only their format's semantics.

Two serialization traps live here and are regression-tested; keep both:

* ElementTree drops ``xmlns:`` declarations it considers unused, but Word
  requires every prefix in ``mc:Ignorable`` to be declared or it reports the
  file as corrupt — so the original root start tag is preserved verbatim.
* ElementTree rewrites a *default* namespace (``xmlns="..."``, used by
  ``.rels`` and ``[Content_Types].xml``) into ``ns0:`` children under the
  preserved root, producing mismatched tags — so the default namespace is
  registered before serializing.
"""

from __future__ import annotations

import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .types import Entity

R_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

CONTENT_TYPES = "[Content_Types].xml"
CORE_PART = "docProps/core.xml"
APP_PART = "docProps/app.xml"

#: docProps elements that carry a person's name, shared by every OPC format.
METADATA_TAGS = {
    CORE_PART: (
        "{http://purl.org/dc/elements/1.1/}creator",
        "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy",
    ),
    APP_PART: (
        "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Manager",
    ),
}

#: Entity label reported for a scrubbed author name.
AUTHOR_ENTITY = "DOCUMENT_AUTHOR"
#: Entity label reported for an embedded image that was stripped or blurred.
IMAGE_ENTITY = "EMBEDDED_IMAGE"
#: Valid values for the embedded-image policy.
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


class OpcError(Exception):
    """Raised when an OPC package cannot be read or parsed."""


# -- zip / XML round-trip ------------------------------------------------------

def open_zip(path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise OpcError(f"not a valid Office file: {exc}") from exc


def parse_part(raw: bytes) -> Tuple[ET.Element, Optional[str]]:
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
        raise OpcError(f"malformed XML part: {exc}") from exc
    return root, (m.group(0) if m else None)


def serialize_part(root: ET.Element, original_root_tag: Optional[str]) -> bytes:
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


def write_zip(
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


# -- text rewriting --------------------------------------------------------------

def rewrite_pieces(
    pieces: Sequence[Piece], detect: Detect, replace: Replace
) -> Tuple[str, str, List[Entity], bool]:
    """Detect over the joined text and write replacements back into the elements.

    Office formats split one logical string across several runs (Word after
    spell-check or formatting changes, Excel in rich-text shared strings), so an
    email address may span three elements. Detection runs on the concatenation;
    the replacement lands in the element where its entity *starts* (inheriting
    that run's formatting) and elements covering the rest of the entity lose
    those characters.

    Returns ``(original, redacted, entities, changed)``.
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
            el.set(XML_SPACE, "preserve")
            changed = True

    redacted, cur = "", 0
    for e in entities:
        redacted += full[cur:e.start] + reps[id(e)]
        cur = e.end
    return full, redacted + full[cur:], entities, changed


def apply_text(text: str, detect: Detect, replace: Replace) -> Tuple[str, List[Entity]]:
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


def unpositioned(entities: Sequence[Entity]) -> List[Entity]:
    """Strip offsets from entities found outside a document's visible text flow."""
    return [Entity(e.entity_type, e.score, None, None, e.text) for e in entities]


def scrub_metadata(
    raw: bytes, replace: Replace, tags: Sequence[str]
) -> Tuple[Optional[bytes], List[Entity]]:
    """Replace the text of author-bearing docProps elements."""
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    for tag in tags:
        for el in root.iter(tag):
            if el.text and el.text.strip():
                entity = Entity(entity_type=AUTHOR_ENTITY, score=1.0, text=el.text)
                el.text = replace(entity)
                entities.append(entity)
    return (serialize_part(root, root_tag) if entities else None), entities


# -- embedded images -----------------------------------------------------------

def plan_images(
    zf: zipfile.ZipFile,
    names: Sequence[str],
    media_prefix: str,
    policy: str,
    redactor: Optional[ImageRedactor],
    replacements: Dict[str, bytes],
    renames: Dict[str, str],
) -> Tuple[List[Entity], List[str]]:
    """Apply the image policy to every part under ``media_prefix``.

    Mutates ``replacements``/``renames`` and returns ``(entities, notes)``. The
    notes state what actually happened — a declined blur is reported as a strip,
    never as a blur.
    """
    media = [n for n in names if n.startswith(media_prefix)]
    entities: List[Entity] = []
    notes: List[str] = []
    if not media:
        return entities, notes
    if policy == "keep":
        notes.append(
            f"{len(media)} embedded image(s) left untouched "
            "(use --docx-images strip|blur to handle them)"
        )
        return entities, notes

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
            new_bytes = blank_png()
            renamed = str(PurePosixPath(name).with_suffix(".png"))
            if renamed != name:
                renames[name] = renamed
        else:
            blurred += 1
        replacements[name] = new_bytes
        entities.append(
            Entity(entity_type=IMAGE_ENTITY, score=1.0, text=PurePosixPath(name).name)
        )
    if blurred and stripped:
        notes.append(
            f"{blurred} embedded image(s) blurred, {stripped} stripped (could not be processed)"
        )
    elif blurred:
        notes.append(f"{blurred} embedded image(s) blurred")
    else:
        notes.append(f"{stripped} embedded image(s) stripped")
    return entities, notes


def blank_png() -> bytes:
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


# -- relationships & content types ----------------------------------------------

def rewrite_rels(
    raw: bytes, rels_name: str, renames: Dict[str, str], detect: Detect, replace: Replace
) -> Tuple[Optional[bytes], List[Entity]]:
    """Retarget renamed media parts and redact external hyperlink targets."""
    root, root_tag = parse_part(raw)
    entities: List[Entity] = []
    changed = False
    base = _rels_base(rels_name)

    for rel in root.iter(R_NS + "Relationship"):
        target = rel.get("Target")
        if not target:
            continue
        if rel.get("TargetMode") == "External":
            # A mailto:/tel: address lives here, not in the document body.
            new, ents = apply_text(target, detect, replace)
            if ents:
                rel.set("Target", new)
                entities.extend(unpositioned(ents))
                changed = True
            continue
        resolved = _resolve_target(base, target)
        if resolved in renames:
            rel.set("Target", posixpath.relpath(renames[resolved], base) if base else renames[resolved])
            changed = True
    return (serialize_part(root, root_tag) if changed else None), entities


def _rels_base(rels_name: str) -> str:
    """``word/_rels/document.xml.rels`` -> ``word``; ``_rels/.rels`` -> ``""``."""
    parent = posixpath.dirname(rels_name)          # word/_rels
    return posixpath.dirname(parent)               # word


def _resolve_target(base: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(base, target)) if base else posixpath.normpath(target)


def ensure_png_default(raw: bytes) -> Optional[bytes]:
    """Add ``<Default Extension="png">`` if the package does not declare it."""
    root, root_tag = parse_part(raw)
    for default in root.iter(CT_NS + "Default"):
        if (default.get("Extension") or "").lower() == "png":
            return None
    el = ET.SubElement(root, CT_NS + "Default")
    el.set("Extension", "png")
    el.set("ContentType", "image/png")
    return serialize_part(root, root_tag)
