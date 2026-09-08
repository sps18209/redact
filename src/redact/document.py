"""Document ingestion — the "pull any document in" layer.

This module normalizes arbitrary inputs (single files, directories, glob
patterns) into :class:`Document` objects with a detected :class:`MediaType`, so
the rest of the suite never has to care where content came from or how to sniff
its type.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List

from .types import MediaType

# Extension -> MediaType. Kept deliberately broad; unknown extensions fall back
# to magic-byte sniffing and finally to MediaType.UNKNOWN.
_EXTENSION_MAP = {
    # text
    ".txt": MediaType.TEXT, ".md": MediaType.TEXT, ".rst": MediaType.TEXT,
    ".py": MediaType.TEXT, ".js": MediaType.TEXT, ".ts": MediaType.TEXT,
    ".java": MediaType.TEXT, ".go": MediaType.TEXT, ".rb": MediaType.TEXT,
    ".c": MediaType.TEXT, ".cpp": MediaType.TEXT, ".h": MediaType.TEXT,
    ".sql": MediaType.TEXT, ".sh": MediaType.TEXT, ".html": MediaType.TEXT,
    # structured
    ".csv": MediaType.STRUCTURED, ".tsv": MediaType.STRUCTURED,
    ".json": MediaType.STRUCTURED, ".jsonl": MediaType.STRUCTURED,
    ".ndjson": MediaType.STRUCTURED, ".xml": MediaType.STRUCTURED,
    ".yaml": MediaType.STRUCTURED, ".yml": MediaType.STRUCTURED,
    ".log": MediaType.STRUCTURED,
    # pdf
    ".pdf": MediaType.PDF,
    # images
    ".png": MediaType.IMAGE, ".jpg": MediaType.IMAGE, ".jpeg": MediaType.IMAGE,
    ".gif": MediaType.IMAGE, ".bmp": MediaType.IMAGE, ".tif": MediaType.IMAGE,
    ".tiff": MediaType.IMAGE, ".webp": MediaType.IMAGE,
    # video
    ".mp4": MediaType.VIDEO, ".mov": MediaType.VIDEO, ".avi": MediaType.VIDEO,
    ".mkv": MediaType.VIDEO, ".webm": MediaType.VIDEO, ".m4v": MediaType.VIDEO,
}

# Leading magic bytes -> MediaType, for extension-less or misnamed files.
_MAGIC = [
    (b"%PDF-", MediaType.PDF),
    (b"\x89PNG\r\n\x1a\n", MediaType.IMAGE),
    (b"\xff\xd8\xff", MediaType.IMAGE),          # JPEG
    (b"GIF87a", MediaType.IMAGE),
    (b"GIF89a", MediaType.IMAGE),
    (b"BM", MediaType.IMAGE),                     # BMP
    (b"RIFF", MediaType.IMAGE),                   # WEBP/AVI share RIFF; refined below
]


@dataclass
class Document:
    """A single ingested document, ready to be routed to a backend."""

    path: Path
    media_type: MediaType

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def read_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.path.read_text(encoding=encoding, errors=errors)


def _sniff_magic(path: Path) -> MediaType:
    """Best-effort content sniffing when the extension is unhelpful."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return MediaType.UNKNOWN
    if not head:
        return MediaType.UNKNOWN
    for sig, mt in _MAGIC:
        if head.startswith(sig):
            # Disambiguate RIFF containers (WEBP image vs AVI video).
            if sig == b"RIFF" and len(head) >= 12:
                fourcc = head[8:12]
                if fourcc == b"WEBP":
                    return MediaType.IMAGE
                if fourcc in (b"AVI ", b"AVI\x20"):
                    return MediaType.VIDEO
            return mt
    # Heuristic: treat as TEXT only if it decodes as UTF-8 *and* looks textual.
    # Binary control bytes (e.g. \x00-\x08) are valid UTF-8 but are not text, so
    # reject content carrying NULs or a run of non-printable control characters.
    try:
        decoded = head.decode("utf-8")
    except UnicodeDecodeError:
        return MediaType.UNKNOWN
    if "\x00" in decoded:
        return MediaType.UNKNOWN
    control = sum(1 for c in decoded if ord(c) < 32 and c not in "\t\n\r\f\v")
    if control:
        return MediaType.UNKNOWN
    return MediaType.TEXT


def detect_media_type(path: Path) -> MediaType:
    """Detect a file's media type from its extension, then its magic bytes."""
    mt = _EXTENSION_MAP.get(path.suffix.lower())
    if mt is not None:
        return mt
    return _sniff_magic(path)


def load_document(path) -> Document:
    """Load a single path into a :class:`Document`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No such file: {p}")
    if not p.is_file():
        raise IsADirectoryError(f"Not a file: {p} (use iter_documents for directories)")
    return Document(path=p, media_type=detect_media_type(p))


def iter_documents(
    inputs: Iterable[str],
    recursive: bool = True,
    include_unknown: bool = False,
) -> Iterator[Document]:
    """Yield :class:`Document` objects for every input.

    Each input may be a file, a directory (walked, recursively by default), or a
    glob pattern. This is the batch face of "pull any document in": point it at
    a folder and it surfaces every redactable file it can identify.

    Files whose type resolves to :data:`MediaType.UNKNOWN` are skipped unless
    ``include_unknown`` is set.
    """
    for raw in inputs:
        for path in _expand_input(raw, recursive):
            mt = detect_media_type(path)
            if mt is MediaType.UNKNOWN and not include_unknown:
                continue
            yield Document(path=path, media_type=mt)


def _expand_input(raw: str, recursive: bool) -> List[Path]:
    p = Path(raw)
    if p.is_dir():
        globber = p.rglob("*") if recursive else p.glob("*")
        return sorted(f for f in globber if f.is_file())
    if p.is_file():
        return [p]
    # Treat as a glob pattern relative to cwd.
    matches = sorted(Path().glob(raw))
    return [m for m in matches if m.is_file()]
