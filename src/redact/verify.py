"""Re-scan a redacted artifact for PII that survived.

Every other module in this suite answers "did the redactor do its job?" by
trusting the redactor. This one does not: it reopens the finished file, decodes
every layer an adversary could decode, and looks for sensitive data in what
comes back. It is deliberately independent of which backend produced the file,
so a bug in any of them shows up here.

Two disciplines it enforces, because both are how real verification fails:

* **Decode before searching.** ``grep`` on a redacted ``.eml`` finds nothing
  whether the message is clean or whether the SSN is sitting in a base64
  attachment — the search was never capable of finding it. Containers are
  unpacked (every zip part, not only the parts the redactor edits), transfer
  encodings decoded, PDF text extracted.

* **Positive control.** A verification that cannot find the secret in the
  *original* proves nothing about the redacted copy. With ``--original`` the
  same extraction runs against the source first: if it turns up no PII there,
  the result is reported as INCONCLUSIVE rather than clean, because the method
  is blind to that file.

A clean report means "no PII this engine can recognise, in any layer it can
reach". It never means "safe to publish" — an unrecognised identifier, text
burned into an image, or an inference from surrounding context all survive it.
"""

from __future__ import annotations

import base64
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .backends.builtin import detect_entities
from .document import Document, detect_media_type
from .types import Entity, MediaType

__all__ = ["Finding", "VerificationReport", "extract_layers", "verify_path"]

#: A base64 payload, which MIME wraps at 76 characters. Matching single lines
#: (``^...$``) decoded each 76-char line separately into disjoint 57-byte
#: chunks, so any value straddling a line boundary was split in half and never
#: found — roughly a one-in-five miss, in the verb's headline feature. Match the
#: whole run of consecutive base64 lines and join before decoding.
_B64 = re.compile(rb"(?:^[A-Za-z0-9+/=]{4,}[ \t]*\r?\n?)+", re.M)

#: Below this a "run" is an identifier or a hash, not a payload worth decoding.
_B64_MIN = 24

#: Hosts that appear in XML namespace declarations, not in user content. Every
#: OOXML package carries a dozen of them, so reporting them as findings would
#: flag every Office document ever redacted. A verifier that cries wolf gets
#: ignored, and an ignored verifier is worse than none — but the filter is kept
#: deliberately narrow: it matches the *host*, so a real URL leak pointing at a
#: patient record or an internal host is still reported.
_SCHEMA_HOSTS = frozenset({
    "schemas.openxmlformats.org", "schemas.microsoft.com", "schemas.xmlsoap.org",
    "www.w3.org", "w3.org", "purl.org", "ns.adobe.com", "docs.oasis-open.org",
    "schemas.google.com", "www.iana.org", "openoffice.org", "sun.com",
})

_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/:?#]+)", re.I)

#: A PDF cross-reference entry: ten-digit offset, five-digit generation, n|f.
#: Every PDF ends with a table of these, and ten zero-padded digits look exactly
#: like a phone number to the engine (two adjacent ones look like a card). Left
#: in, this flags every PDF ever produced — the same cry-wolf failure as the
#: namespace URLs. Only this exact structural shape is removed; a real number
#: sitting in the page content is untouched.
_PDF_XREF = re.compile(r"^\d{10} \d{5} [nf]\s*$", re.M)

#: The trailer's document ID: two random hex strings. A 32-char random hex run
#: matches the IBAN shape (two letters, two digits, then alphanumerics) about a
#: fifth of the time, which presented as a ~20% "flaky test" until the input,
#: not the test, turned out to be the random thing. It identifies the file, not
#: a person.
_PDF_ID = re.compile(r"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")


def _strip_pdf_structure(text: str) -> str:
    """Drop cross-reference tables and the document ID before scanning.

    Both are file plumbing that happens to match PII patterns. Only these exact
    shapes go; page content is never touched.
    """
    return _PDF_ID.sub("", _PDF_XREF.sub("", text))


#: Office Open XML identifiers. A braced GUID ("{9C124767-2382-...}") carries a
#: shape ID, and ``p14:creationId val="1805331330"`` is a random 32-bit slide
#: stamp; ten digits read as a phone number and a GUID's digit runs read as one
#: too, flagging every slide of every deck. Only these exact shapes are removed,
#: and only from XML parts — text a person typed is never touched.
_OOXML_GUID = re.compile(r"\{[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\}")
_OOXML_CREATION_ID = re.compile(r"(<\w+:creationId\b[^>]*?\bval=\")\d+(\")")
#: ``<p:sldMasterId id="2147483758">`` / ``<p:sldLayoutId id="...">`` — ten-digit
#: counters that index masters and layouts, never content.
_OOXML_SLD_ID = re.compile(r"(<\w+:(?:sldMasterId|sldLayoutId|sldId|notesMasterId|handoutMasterId)\b[^>]*?\bid=\")\d+(\")")


def _strip_ooxml_structure(text: str) -> str:
    """Drop GUIDs and creationId stamps from OOXML markup before scanning."""
    return _OOXML_SLD_ID.sub(r"\1\2", _OOXML_CREATION_ID.sub(r"\1\2", _OOXML_GUID.sub("", text)))


def _is_structural(entity: Entity) -> bool:
    """True for a namespace/schema URL — file plumbing, never user data."""
    if entity.entity_type != "URL" or not entity.text:
        return False
    match = _HOST.match(entity.text.strip())
    if not match:
        return False
    host = match.group(1).lower()
    # NOT lstrip("www."): that strips a character *set*, so "www.w3.org" became
    # "3.org" and a real host like "w.sun.com" was wrongly suppressed as noise.
    bare = host[4:] if host.startswith("www.") else host
    return host in _SCHEMA_HOSTS or bare in _SCHEMA_HOSTS


@dataclass
class Finding:
    """One piece of PII that survived, and the layer it was found in."""

    entity: Entity
    layer: str

    def __str__(self) -> str:
        return f"{self.entity.entity_type} in {self.layer}"


@dataclass
class VerificationReport:
    """What re-scanning one artifact turned up."""

    path: Path
    media_type: MediaType
    findings: List[Finding] = field(default_factory=list)
    layers_scanned: int = 0
    #: Set when ``--original`` was given: entities found in the SOURCE file.
    control_entities: Optional[int] = None
    note: str = ""
    coverage_incomplete: bool = False

    @property
    def clean(self) -> bool:
        return not self.findings and not self.inconclusive

    @property
    def inconclusive(self) -> bool:
        """Verification could not inspect all required content or establish a control."""
        return self.coverage_incomplete or self.control_entities == 0

    @property
    def status(self) -> str:
        if self.findings:
            return "LEAKING"
        return "INCONCLUSIVE" if self.inconclusive else "clean"

    def summary(self) -> str:
        head = f"[{self.status}] {self.path.name} ({self.media_type}) — {self.layers_scanned} layer(s)"
        if self.findings:
            shown = "; ".join(str(f) for f in self.findings[:6])
            more = f" (+{len(self.findings) - 6} more)" if len(self.findings) > 6 else ""
            head += f" | {len(self.findings)} finding(s): {shown}{more}"
        if self.note:
            head += f" | {self.note}"
        return head


# -- extraction ---------------------------------------------------------------

def extract_layers(path: Path, media_type: MediaType) -> Dict[str, str]:
    """Every recoverable text view of ``path``, keyed by where it came from.

    Deliberately broader than the redactors: a container is unpacked whole, so
    a leak in a part no backend edits is still caught.
    """
    layers: Dict[str, str] = {}
    try:
        raw = path.read_bytes()
    except OSError:
        return {"<unreadable>": ""}

    # Specific layers are collected first and "raw" is added last, because a
    # finding is attributed to the first layer it appears in. An uncompressed
    # zip entry shows up in the raw bytes too, and "US_SSN in
    # zip:customXml/item1.xml" tells the user what to fix where "US_SSN in raw"
    # does not.

    # 1. Zip containers: docx/xlsx/pptx, and anything else zip-shaped.
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    try:
                        part = zf.read(name).decode("utf-8", "replace")
                        if name.endswith((".xml", ".rels")):
                            part = _strip_ooxml_structure(part)
                        layers[f"zip:{name}"] = part
                    except (KeyError, OSError, zipfile.BadZipFile):
                        continue
        except zipfile.BadZipFile:
            pass

    # 2. Base64 payloads — the encoding that defeats a naive grep.
    for index, blob in enumerate(_B64.findall(raw)):
        joined = b"".join(blob.split())  # unwrap the 76-char MIME lines
        if len(joined) < _B64_MIN:
            continue
        try:
            decoded = base64.b64decode(joined, validate=True)
        except Exception:
            continue
        if decoded:
            layers[f"base64[{index}]"] = decoded.decode("utf-8", "replace")

    # 3. Format-aware extraction, which reaches text the raw bytes compress away.
    layers.update(_structured_text(path, media_type))

    # 4. Last: the bytes as they sit on disk, as the catch-all for anything the
    # layers above could not reach.
    text = raw.decode("utf-8", "replace")
    if media_type is MediaType.PDF:
        text = _strip_pdf_structure(text)
    elif zipfile.is_zipfile(path):
        text = _strip_ooxml_structure(text)
    layers["raw"] = text
    return layers


def _structured_text(path: Path, media_type: MediaType) -> Dict[str, str]:
    """Format-specific extraction; never raises, a failure just yields nothing."""
    out: Dict[str, str] = {}
    try:
        if media_type is MediaType.DOCX:
            from .docx import extract_text

            out["docx:text"] = extract_text(path)
        elif media_type is MediaType.XLSX:
            from .xlsx import extract_text

            out["xlsx:text"] = extract_text(path)
        elif media_type is MediaType.PPTX:
            from .pptx import extract_text

            out["pptx:text"] = extract_text(path)
        elif media_type is MediaType.EMAIL:
            from .eml import extract_text

            out["eml:text"] = extract_text(path)
        elif media_type is MediaType.PDF:
            out.update(_pdf_text(path))
        elif media_type is MediaType.IMAGE:
            out.update(_image_text(path))
    except Exception:
        # A malformed file must not abort verification — but silence here used
        # to mean no layer AND no marker, so verify printed [clean] having
        # searched only compressed raw bytes. Leave a marker so the caller
        # reports INCONCLUSIVE instead of a reassurance it cannot support.
        if media_type is MediaType.PDF:
            out.setdefault("pdf:<no engine>", "")
        elif media_type is MediaType.IMAGE:
            out.setdefault("image:<no ocr>", "")
    return out


def _image_text(path: Path) -> Dict[str, str]:
    """Text rendered as pixels — the layer a text search cannot reach.

    Without this, verifying a screenshot searched the PNG's compressed bytes,
    found nothing, and reported ``clean`` over a legible SSN. A verifier that
    cannot read the format it was handed must say so, not pass it.
    """
    from . import ocr

    if not ocr.available():
        return {"image:<no ocr>": ""}
    try:
        readings = [t for t, _ in ocr.read_regions(path)]
    except Exception:
        return {"image:<no ocr>": ""}
    # Both the raw reading and the boundary-restored one, for the same reason
    # the OCR backend scans both: the engine runs "SSN 078-05-1120" together as
    # "SSN078-05-1120", and \b-anchored patterns then miss it.
    lines = readings + [ocr.boundary_restored(t) for t in readings]
    return {"image:ocr": "\n".join(lines)}


def _pdf_text(path: Path) -> Dict[str, str]:
    """PDF text, plus a warning for pages that carry no text layer."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            # Without a PDF engine the raw-bytes layer is all we have, and a
            # compressed stream will hide text from it. Say so, don't imply
            # the file was checked properly.
            return {"pdf:<no engine>": ""}
    text = []
    layers = {}
    with pymupdf.open(str(path)) as doc:
        for number, page in enumerate(doc, start=1):
            text.append(page.get_text())
            if page.get_image_info():
                layers[f"pdf:<unexamined image> page {number}"] = ""
    layers["pdf:text"] = "\n".join(text)
    return layers


# -- verification -------------------------------------------------------------

def ocr_hint() -> str:
    from . import ocr

    return ocr.INSTALL_HINT


def verify_path(
    path: Path,
    entities: Optional[Sequence[str]] = None,
    threshold: float = 0.35,
    original: Optional[Path] = None,
    *,
    require_original: bool = False,
) -> VerificationReport:
    """Re-scan ``path``; with ``original``, run the positive control first."""
    media_type = detect_media_type(path)
    report = VerificationReport(path=path, media_type=media_type)

    notes = []
    if original is not None and original.is_file():
        control = _scan(original, detect_media_type(original), entities, threshold)
        report.control_entities = len(control)
        if not control:
            notes.append("positive control found no PII in the original, so a clean result proves nothing")
    elif original is not None or require_original:
        report.coverage_incomplete = True
        notes.append("original file is missing or ambiguous")

    layers = extract_layers(path, media_type)
    report.layers_scanned = len(layers)
    if "<unreadable>" in layers:
        report.coverage_incomplete = True
        notes.append("could not read the output file")
    if any(k.startswith("pdf:<no engine>") for k in layers):
        # Same reasoning as the image case below: a PDF's text lives in
        # compressed streams, so searching raw bytes finds nothing whether or
        # not the file is clean. Reporting "clean" from that is the base64
        # failure again, so force INCONCLUSIVE rather than merely noting it.
        report.coverage_incomplete = True
        notes.append(
            "no usable PDF engine — only raw bytes were searched, which a "
            'compressed stream hides. pip install "redact-suite[pymupdf]"'
        )
    if any(k.startswith("image:<no ocr>") for k in layers):
        # An image's text is pixels. Searching the compressed bytes finds
        # nothing whether or not an SSN is legible on screen, so "clean" here
        # would be the same empty reassurance as a grep over base64.
        report.coverage_incomplete = True
        notes.append(
            "text inside the image was NOT examined (no OCR engine) — a "
            f'screenshot or scan can carry PII as pixels. {ocr_hint()}'
        )

    image_pages = [layer.rsplit(" ", 1)[-1] for layer in layers if layer.startswith("pdf:<unexamined image>")]
    if image_pages:
        report.coverage_incomplete = True
        notes.append(f"PDF images were not examined on page(s) {', '.join(image_pages)}")
    report.note = ". ".join(notes)

    seen = set()
    for layer, text in layers.items():
        if not text:
            continue
        for entity in detect_entities(text, entities, threshold):
            if _is_structural(entity):
                continue
            key = (entity.entity_type, entity.text)
            if key in seen:  # the same value shows up in several layers
                continue
            seen.add(key)
            report.findings.append(Finding(entity=entity, layer=layer))
    return report


def _scan(
    path: Path,
    media_type: MediaType,
    entities: Optional[Sequence[str]],
    threshold: float,
) -> List[Entity]:
    found: List[Entity] = []
    for text in extract_layers(path, media_type).values():
        if text:
            found.extend(
                e for e in detect_entities(text, entities, threshold)
                if not _is_structural(e)
            )
    return found


def pair_with_originals(
    outputs: Sequence[Document], source_dir: Optional[Path]
) -> List[Tuple[Document, Optional[Path]]]:
    """Pair mirrored outputs by relative path, without searching by basename."""
    pairs = []
    for doc in outputs:
        origin = None
        if source_dir is not None:
            parent = doc.path.relative_to(doc.root).parent if doc.root is not None else Path()
            stem = doc.path.stem
            if stem.endswith(".redacted"):
                stem = stem[:-len(".redacted")]
            candidate = source_dir / parent / (stem + doc.path.suffix)
            # A text extract can share a destination name with a redacted .txt.
            # Require an unambiguous exact match instead of guessing its source.
            if candidate.is_file() and not (
                doc.path.suffix.lower() == ".txt"
                and any(
                    sibling != candidate and sibling.is_file()
                    and sibling.stem == stem
                    and detect_media_type(sibling) in (MediaType.PDF, MediaType.TEXT)
                    for sibling in candidate.parent.iterdir()
                )
            ):
                origin = candidate
        pairs.append((doc, origin))
    return pairs
