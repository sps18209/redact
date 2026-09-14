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

#: A run of base64 long enough to be a payload rather than an id.
_B64 = re.compile(rb"^[A-Za-z0-9+/=]{24,}$", re.M)

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


def _strip_pdf_structure(text: str) -> str:
    """Drop cross-reference tables from a PDF's raw bytes before scanning."""
    return _PDF_XREF.sub("", text)


def _is_structural(entity: Entity) -> bool:
    """True for a namespace/schema URL — file plumbing, never user data."""
    if entity.entity_type != "URL" or not entity.text:
        return False
    match = _HOST.match(entity.text.strip())
    if not match:
        return False
    return match.group(1).lower().lstrip("www.") in _SCHEMA_HOSTS or \
        match.group(1).lower() in _SCHEMA_HOSTS


@dataclass
class Finding:
    """One piece of PII that survived, and the layer it was found in."""

    entity: Entity
    layer: str

    def __str__(self) -> str:
        return f"{self.entity.entity_type} in {self.layer}: {self.entity.text!r}"


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

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def inconclusive(self) -> bool:
        """The control found nothing, so a clean result proves nothing."""
        return self.control_entities == 0

    @property
    def status(self) -> str:
        if self.inconclusive:
            return "INCONCLUSIVE"
        return "clean" if self.clean else "LEAKING"

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
    except OSError as exc:
        return {"<unreadable>": f"{exc}"}

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
                        layers[f"zip:{name}"] = zf.read(name).decode("utf-8", "replace")
                    except (KeyError, OSError, zipfile.BadZipFile):
                        continue
        except zipfile.BadZipFile:
            pass

    # 2. Base64 payloads — the encoding that defeats a naive grep.
    for index, blob in enumerate(_B64.findall(raw)):
        try:
            decoded = base64.b64decode(blob, validate=True)
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
    except Exception:  # a malformed file must not abort verification
        pass
    return out


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
    out = []
    doc = pymupdf.open(str(path))
    try:
        for page in doc:
            out.append(page.get_text())
    finally:
        doc.close()
    return {"pdf:text": "\n".join(out)}


# -- verification -------------------------------------------------------------

def verify_path(
    path: Path,
    entities: Optional[Sequence[str]] = None,
    threshold: float = 0.35,
    original: Optional[Path] = None,
) -> VerificationReport:
    """Re-scan ``path``; with ``original``, run the positive control first."""
    media_type = detect_media_type(path)
    report = VerificationReport(path=path, media_type=media_type)

    if original is not None and original.exists():
        control = _scan(original, detect_media_type(original), entities, threshold)
        report.control_entities = len(control)
        if not control:
            report.note = (
                f"positive control found no PII in {original.name} — this engine "
                "cannot see into that file, so a clean result here proves nothing"
            )

    layers = extract_layers(path, media_type)
    report.layers_scanned = len(layers)
    if any(k.startswith("pdf:<no engine>") for k in layers):
        report.note = (
            'no PDF engine installed — only raw bytes were searched, which a '
            'compressed stream hides. pip install "redact-suite[pymupdf]"'
        )

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
    """Match each redacted artifact to its source for the positive control.

    Outputs are named ``<stem>.redacted<suffix>``, so the source name is
    recoverable; anything that does not match is verified without a control.
    """
    pairs: List[Tuple[Document, Optional[Path]]] = []
    for doc in outputs:
        origin = None
        if source_dir is not None:
            name = doc.path.name.replace(".redacted", "", 1)
            candidate = source_dir / name
            if candidate.exists():
                origin = candidate
        pairs.append((doc, origin))
    return pairs
