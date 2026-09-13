"""Built-in, dependency-free redaction engine.

This backend ships with the suite and needs nothing beyond the standard
library. It uses rule-based regular expressions (plus a Luhn check for card
numbers) to find common PII/PHI in text and structured files, and it is the
guaranteed fallback so the suite always does *something* useful even when no
heavy tool is installed.

The detection helpers here are also reused by other adapters (e.g. the RedactAI
adapter augments its model output with these deterministic patterns).
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Optional

from ..document import Document, output_path
from ..opc import resolve_overlaps
from ..types import (
    Entity,
    MediaType,
    RedactionMode,
    RedactionOptions,
    RedactionResult,
)
from .base import Backend

# --- Detection patterns -----------------------------------------------------
# Each recognizer is (entity_type, compiled_regex, optional validator).

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?\d{1,2}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}[-\s]?(?!00)\d{2}[-\s]?(?!0000)\d{4}\b")
# Starts and ends on a digit so a trailing separator is never swallowed.
_CREDIT_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_IPV4 = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
# At least four colon-separated groups: two-colon forms like ``10:23:45`` are
# almost always clock times, which flooded log files with false positives.
# (Compressed ``::`` notation is not handled; use Presidio for full IPv6.)
_IPV6 = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){3,7}[A-Fa-f0-9]{1,4}\b")
_URL = re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_US_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# (entity_type, regex, validator) — validator returns True to keep a match.
_RECOGNIZERS = [
    ("EMAIL_ADDRESS", _EMAIL, None),
    ("US_SSN", _SSN, None),
    ("CREDIT_CARD", _CREDIT_CARD, _luhn_ok),
    ("IBAN_CODE", _IBAN, None),
    ("PHONE_NUMBER", _PHONE, None),
    ("URL", _URL, None),
    ("IP_ADDRESS", _IPV4, None),
    ("IP_ADDRESS", _IPV6, None),
]

# Recognizers users often want to opt into but that are noisy by default.
_OPTIONAL_RECOGNIZERS = {
    "US_ZIP_CODE": (_US_ZIP, None),
}

#: The entity labels the built-in engine knows about.
KNOWN_ENTITIES = sorted(
    {name for name, _, _ in _RECOGNIZERS} | set(_OPTIONAL_RECOGNIZERS)
)


def detect_entities(
    text: str,
    entities: Optional[List[str]] = None,
    threshold: float = 0.0,
) -> List[Entity]:
    """Find sensitive spans in ``text`` and return them sorted by position.

    ``entities`` optionally restricts detection to a subset of labels; ``None``
    runs every default recognizer. Overlapping matches are de-duplicated in
    favour of the earliest, longest span.
    """
    wanted = set(entities) if entities else None
    active = list(_RECOGNIZERS)
    if wanted:
        for label in wanted:
            if label in _OPTIONAL_RECOGNIZERS:
                rx, val = _OPTIONAL_RECOGNIZERS[label]
                active.append((label, rx, val))

    found: List[Entity] = []
    for label, regex, validator in active:
        if wanted is not None and label not in wanted:
            continue
        for m in regex.finditer(text):
            value = m.group(0)
            if validator and not validator(value):
                continue
            # Deterministic recognizers get high confidence; validated ones max.
            score = 0.95 if validator else 0.85
            if score < threshold:
                continue
            found.append(
                Entity(
                    entity_type=label,
                    score=score,
                    start=m.start(),
                    end=m.end(),
                    text=value,
                )
            )

    return _dedupe_overlaps(found)


def _dedupe_overlaps(entities: List[Entity]) -> List[Entity]:
    """Drop entities overlapping an earlier, longer span (see opc.resolve_overlaps)."""
    return resolve_overlaps(entities)


def replacement_for(entity: Entity, options: RedactionOptions) -> str:
    original = entity.text or ""
    if options.mode is RedactionMode.MASK:
        return options.mask_char * max(len(original), 1)
    if options.mode is RedactionMode.HASH:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
        return f"<{entity.entity_type}:{digest}>"
    if options.mode is RedactionMode.REDACT:
        return ""
    # REPLACE (default) and any visual mode fall back to a typed placeholder.
    return f"<{entity.entity_type}>"


def apply_redactions(
    text: str,
    entities: List[Entity],
    options: RedactionOptions,
) -> str:
    """Return ``text`` with every entity rewritten per ``options.mode``."""
    result = text
    # Rewrite from the end so earlier offsets stay valid.
    for entity in sorted(entities, key=lambda e: e.start or 0, reverse=True):
        if entity.start is None or entity.end is None:
            continue
        result = result[: entity.start] + replacement_for(entity, options) + result[entity.end :]
    return result



class BuiltinBackend(Backend):
    """Rule-based, offline redactor for text and structured files."""

    name = "builtin"
    description = "Dependency-free regex/rule engine for text & structured data (always available)."
    supported_media_types = (
        MediaType.TEXT, MediaType.STRUCTURED, MediaType.DOCX, MediaType.XLSX,
    )
    priority = 10  # low: a safe fallback, beaten by purpose-built tools

    def missing_dependencies(self) -> List[str]:
        return []  # stdlib only

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        if document.media_type in (MediaType.DOCX, MediaType.XLSX):
            return self._redact_office(document, options)
        try:
            text = document.read_text()
        except OSError as exc:
            return RedactionResult(
                source=document.path,
                backend=self.name,
                media_type=document.media_type,
                success=False,
                message=f"could not read file: {exc}",
            )

        entities = detect_entities(text, options.entities, options.threshold)
        redacted = apply_redactions(text, entities, options)

        result = RedactionResult(
            source=document.path,
            backend=self.name,
            media_type=document.media_type,
            entities=entities,
            redacted_text=redacted,
        )

        if options.dry_run:
            result.message = "dry-run: detected only, nothing written"
            return result

        out = output_path(document, options)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(redacted, encoding="utf-8")
        except OSError as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result
        result.output_path = out
        return result

    def _redact_office(self, document: Document, options: RedactionOptions) -> RedactionResult:
        return redact_office_document(
            self.name,
            document,
            options,
            detect=lambda text: detect_entities(text, options.entities, options.threshold),
        )


def redact_office_document(
    backend_name: str,
    document: Document,
    options: RedactionOptions,
    detect,
) -> RedactionResult:
    """Drive a Word or Excel redaction with any detection function.

    Shared by every text backend that supports Office documents (the builtin
    engine and Presidio): the backend supplies ``detect``; replacement, image
    policy, output naming and error handling are identical for all of them.
    """
    from ..opc import AUTHOR_ENTITY, IMAGE_ENTITY, OpcError

    if document.media_type is MediaType.XLSX:
        from ..xlsx import redact_xlsx as redact_office
    else:
        from ..docx import redact_docx as redact_office

    result = RedactionResult(
        source=document.path, backend=backend_name, media_type=document.media_type,
    )
    wanted = options.entities
    out = None if options.dry_run else output_path(document, options)
    policy = options.docx_images
    if wanted is not None and IMAGE_ENTITY not in wanted:
        policy = "keep"  # an entity filter that excludes images means leave them
    try:
        office = redact_office(
            document.path,
            out,
            detect=detect,
            replace=lambda entity: replacement_for(entity, options),
            scrub_authors=(wanted is None or AUTHOR_ENTITY in wanted),
            image_policy=policy,
            image_redactor=options.extra.get("image_redactor"),
        )
    except (OpcError, OSError, ValueError) as exc:
        result.success = False
        result.message = f"could not redact .{document.media_type}: {exc}"
        return result

    result.entities = office.entities
    result.redacted_text = office.redacted_text
    notes = list(office.notes)
    if options.dry_run:
        notes.insert(0, "dry-run: detected only, nothing written")
    else:
        result.output_path = out
    result.message = "; ".join(notes)
    return result


#: Backwards-compatible alias for the pre-xlsx name.
redact_docx_document = redact_office_document
