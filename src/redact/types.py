"""Core data types shared across the redaction suite.

These types are intentionally dependency-free so that every backend adapter,
the router, and the CLI can speak the same vocabulary regardless of which
heavy tools happen to be installed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


class MediaType(str, enum.Enum):
    """The broad category of content a document holds.

    Backends advertise which media types they can handle, and the router uses
    a document's media type as the primary key when choosing a backend.
    """

    TEXT = "text"           # plain text, .txt, .md, source code
    STRUCTURED = "structured"  # csv, json, tsv, log, xml, yaml
    DOCX = "docx"          # Word documents (zip of XML parts)
    XLSX = "xlsx"          # Excel workbooks (same OPC zip family)
    PPTX = "pptx"          # PowerPoint decks (same OPC zip family)
    EMAIL = "email"        # RFC 5322 messages (.eml), headers + MIME parts
    PDF = "pdf"
    IMAGE = "image"
    VIDEO = "video"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # nicer CLI output
        return self.value


class RedactionMode(str, enum.Enum):
    """How a detected entity should be transformed."""

    MASK = "mask"        # replace characters with a mask char (e.g. ****)
    REPLACE = "replace"  # replace with a typed placeholder (e.g. <EMAIL>)
    HASH = "hash"        # replace with a stable hash of the value
    REDACT = "redact"    # remove entirely / black out
    BLUR = "blur"        # visual blur (images/video)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Entity:
    """A single piece of detected sensitive information within a document.

    ``start``/``end`` are character offsets for text-like content and are left
    as ``None`` for visual detections (faces, plates) where a bounding box is
    the natural unit. ``bbox`` carries ``(x, y, w, h)`` for those cases.
    """

    entity_type: str
    score: float = 1.0
    start: Optional[int] = None
    end: Optional[int] = None
    text: Optional[str] = None
    bbox: Optional[tuple] = None

    def __str__(self) -> str:
        loc = ""
        if self.start is not None:
            loc = f" @ {self.start}:{self.end}"
        elif self.bbox is not None:
            loc = f" @ bbox{self.bbox}"
        return f"{self.entity_type}({self.score:.2f}){loc}"


@dataclass
class RedactionOptions:
    """User-facing knobs that steer a redaction run."""

    backend: str = "auto"  # "auto" lets the router choose; else a backend name
    mode: RedactionMode = RedactionMode.REPLACE
    entities: Optional[List[str]] = None  # None = detect everything the backend knows
    language: str = "en"
    threshold: float = 0.35  # minimum confidence to act on a detection
    mask_char: str = "*"
    output_dir: Optional[Path] = None  # where redacted artifacts are written
    dry_run: bool = False  # detect + report, but do not write redacted output
    #: What to do with images embedded in a .docx: keep | strip | blur.
    docx_images: str = "keep"
    #: What to do with email attachments that cannot be redacted: keep | strip.
    eml_attachments: str = "keep"
    extra: dict = field(default_factory=dict)  # backend-specific escape hatch


@dataclass
class RedactionResult:
    """The outcome of running one backend over one document."""

    source: Path
    backend: str
    media_type: MediaType
    success: bool = True
    entities: List[Entity] = field(default_factory=list)
    output_path: Optional[Path] = None
    redacted_text: Optional[str] = None
    message: str = ""
    #: Content this run knowingly did NOT redact (e.g. a binary email
    #: attachment). A run that leaves any of this is not a clean redaction, and
    #: the CLI exits non-zero for it — "exit 0" from a redaction tool has to
    #: mean the output is safe, or automation is being told a comfortable lie.
    unredacted: List[str] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        return len(self.entities)

    @property
    def fully_redacted(self) -> bool:
        """True when nothing was knowingly left behind."""
        return self.success and not self.unredacted

    def summary(self) -> str:
        status = "ok" if self.success else "FAILED"
        out = f" -> {self.output_path}" if self.output_path else ""
        return (
            f"[{status}] {self.source.name} via {self.backend} "
            f"({self.media_type}): {self.entity_count} entities{out}"
            + (f" | {self.message}" if self.message else "")
        )
