"""True in-place PDF redaction via PyMuPDF.

This is the suite's answer to its largest field gap. Before it, redacting a PDF
meant either a running Ollama server (``redactai``, which does not even modify
the PDF — it writes a text extract) or the ``pdf-redact-tools`` CLI and its
ImageMagick/exiftool/poppler stack. Out of the box, the most common document
type in legal and medical work could not be redacted at all.

PyMuPDF's ``apply_redactions`` genuinely *removes* content from the page's
content stream rather than drawing a rectangle over it. Verified here: after a
run, the value is absent from the extracted text **and** from the file's raw
bytes. Black rectangles are not redaction — the characters survive underneath
for any ``pdftotext`` to recover — so a box-drawing implementation would have
been worse than none.

Two failure modes get explicit handling, because both would otherwise report a
clean run over a document that still carries PII:

* **A detected entity whose rectangle cannot be located.** Detection runs on
  extracted text; the redaction needs coordinates, which come from
  ``page.search_for``. Ligatures, hyphenation and text split across spans can
  make a string that exists in the extraction unfindable on the page. Such an
  entity is reported in ``unredacted`` instead of being quietly dropped.

* **A page with no text layer.** A scanned document is an image of text: the
  extraction is empty, so detection finds nothing and the run would otherwise
  look spotless. Those pages are reported too — "no text layer" means "this
  tool could not read it", never "there is nothing here". Flatten such a file
  by OCRing it first, or with ``-b pdf-redact-tools`` (unmaintained
  Python 2, so it needs patching before it runs at all).

Licence note: PyMuPDF is AGPL-3.0 (or commercial), like ``ultralytics``. It is
an opt-in extra, never a hard dependency of this MIT-licensed suite.
"""

from __future__ import annotations

import importlib.util
from typing import List, Tuple

from ..document import Document, output_path
from ..opc import unpositioned
from ..types import Entity, MediaType, RedactionOptions, RedactionResult
from .base import Backend
from .builtin import detect_entities, replacement_for

#: Fill colour for a redaction box (black).
_FILL = (0, 0, 0)

#: Point size for replacement text drawn into the cleared box. Small enough
#: that a placeholder like ``<EMAIL_ADDRESS>`` fits where the original sat.
_OVERLAY_FONTSIZE = 7


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class PyMuPDFBackend(Backend):
    name = "pymupdf"
    description = (
        "True in-place PDF redaction via PyMuPDF — removes text from the content "
        "stream (not a black box over it) and scrubs metadata."
    )
    supported_media_types = (MediaType.PDF,)
    install_hint = 'pip install "redact-suite[pymupdf]"'
    # Above redactai (60) and pdf-redact-tools (40): it is the only backend that
    # redacts the PDF itself while keeping it a usable document. Flattening to
    # images is more thorough but destroys text, search and accessibility, so it
    # stays an explicit choice rather than the automatic one.
    priority = 75

    def missing_dependencies(self) -> List[str]:
        return [] if _module_present("pymupdf") or _module_present("fitz") else ["pymupdf"]

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
            import pymupdf  # noqa: PLC0415 - heavy import stays lazy
        except ImportError:  # pragma: no cover - older wheels expose `fitz`
            import fitz as pymupdf

        try:
            doc = pymupdf.open(str(document.path))
        except Exception as exc:  # unreadable / encrypted / not a PDF
            result.success = False
            result.message = f"could not open PDF: {exc}"
            return result

        try:
            entities, unlocatable, imageonly = self._plan(doc, options)
            result.entities = entities

            if options.dry_run:
                doc.close()
                result.message = _summary(entities, unlocatable, imageonly, applied=False)
                result.unredacted = _unredacted(unlocatable, imageonly)
                return result

            out = output_path(document, options)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                # garbage=4 + clean rewrites the xref, so removed content does
                # not linger as an orphaned object in the saved file.
                doc.set_metadata({})
                doc.save(str(out), garbage=4, deflate=True, clean=True)
            except Exception as exc:
                result.success = False
                result.message = f"could not write output: {exc}"
                return result
            finally:
                doc.close()

            result.output_path = out
            result.unredacted = _unredacted(unlocatable, imageonly)
            result.message = _summary(entities, unlocatable, imageonly, applied=True)
            return result
        except Exception as exc:  # defensive: a batch must survive one bad file
            try:
                doc.close()
            except Exception:
                pass
            result.success = False
            result.message = f"redaction failed: {exc}"
            return result

    # -- planning -----------------------------------------------------------
    def _plan(self, doc, options: RedactionOptions) -> Tuple[List[Entity], List[str], List[int]]:
        """Mark every locatable entity for removal; report what could not be."""
        found: List[Entity] = []
        unlocatable: List[str] = []
        imageonly: List[int] = []

        for number, page in enumerate(doc, start=1):
            text = page.get_text()
            if not text.strip():
                # No text layer. Either a genuinely blank page or — the case
                # that matters — a scan, where every word is pixels.
                if page.get_images(full=True):
                    imageonly.append(number)
                continue

            for entity in detect_entities(text, options.entities, options.threshold):
                value = entity.text or ""
                if not value.strip():
                    continue
                rects = page.search_for(value)
                if not rects:
                    unlocatable.append(f"{entity.entity_type} on page {number}")
                    continue
                # A page has no single linear text flow, so offsets into the
                # per-page extraction would not index anything a caller holds.
                found.extend(unpositioned([entity]))
                overlay = replacement_for(entity, options)
                for rect in rects:
                    page.add_redact_annot(
                        rect, text=overlay or None, fill=_FILL,
                        fontsize=_OVERLAY_FONTSIZE, text_color=(1, 1, 1),
                    )
            # Removes the marked content from this page's content stream.
            page.apply_redactions()

        return found, unlocatable, imageonly


def _unredacted(unlocatable: List[str], imageonly: List[int]) -> List[str]:
    """Content this run knowingly did not remove (drives the non-zero exit)."""
    out = list(unlocatable)
    if imageonly:
        pages = ", ".join(str(p) for p in imageonly)
        out.append(f"page(s) {pages} have no text layer (scanned?)")
    return out


def _summary(
    entities: List[Entity],
    unlocatable: List[str],
    imageonly: List[int],
    applied: bool,
) -> str:
    head = (
        f"{len(entities)} entit{'y' if len(entities) == 1 else 'ies'} "
        + ("removed from the content stream" if applied else "detected (dry-run)")
    )
    notes: List[str] = []
    if unlocatable:
        notes.append(
            f"WARNING: {len(unlocatable)} detected item(s) could not be located on the "
            f"page and are UNCHANGED ({'; '.join(unlocatable)})"
        )
    if imageonly:
        pages = ", ".join(str(p) for p in imageonly)
        notes.append(
            f"WARNING: page(s) {pages} carry images but no text layer — a scan reads as "
            "'nothing found'. OCR the file first, or flatten it with -b "
            "pdf-redact-tools (unmaintained Python 2 — see its install hint)"
        )
    return " | ".join([head] + notes)
