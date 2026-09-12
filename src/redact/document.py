"""Document ingestion — the "pull any document in" layer.

This module normalizes arbitrary inputs (single files, directories, glob
patterns) into :class:`Document` objects with a detected :class:`MediaType`, so
the rest of the suite never has to care where content came from or how to sniff
its type. It also owns :func:`output_path`, the single rule for where a backend
writes its artifact.
"""

from __future__ import annotations

import glob as _glob
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

from .types import MediaType, RedactionOptions

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
    # office
    ".docx": MediaType.DOCX,
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

# Directories that never hold user documents; skipped when walking a tree
# (hidden directories such as ``.git`` are skipped as well).
_SKIP_DIRS = {"node_modules", "__pycache__", "venv"}

_GLOB_CHARS = "*?["


def is_redaction_output(path: Path) -> bool:
    """True for artifacts the suite itself produces.

    Ingestion skips these so a second run over the same folder never re-redacts
    ``a.redacted.txt`` into ``a.redacted.redacted.txt``.
    """
    name = path.name
    return (
        ".redacted." in name
        or name.endswith(".redacted")
        or name.endswith("-final.pdf")  # pdf-redact-tools output
    )


@dataclass
class Document:
    """A single ingested document, ready to be routed to a backend."""

    path: Path
    media_type: MediaType
    #: The directory this document was discovered under (a directory or glob
    #: input). :func:`output_path` mirrors the tree beneath it so two files
    #: with the same name in different folders never collide in ``-o``.
    #: ``None`` for a document loaded directly by path.
    root: Optional[Path] = None

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


def _sniff_zip(path: Path) -> MediaType:
    """Distinguish Office containers from arbitrary zips."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return MediaType.UNKNOWN
    if "word/document.xml" in names:
        return MediaType.DOCX
    return MediaType.UNKNOWN


def _sniff_magic(path: Path) -> MediaType:
    """Best-effort content sniffing when the extension is unhelpful."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return MediaType.UNKNOWN
    if not head:
        return MediaType.UNKNOWN
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(path)
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
    ``include_unknown`` is set. The suite's own outputs (see
    :func:`is_redaction_output`) are always skipped.
    """
    for raw in inputs:
        root, paths = _expand_input(raw, recursive)
        for path in paths:
            if is_redaction_output(path):
                continue
            mt = detect_media_type(path)
            if mt is MediaType.UNKNOWN and not include_unknown:
                continue
            yield Document(path=path, media_type=mt, root=root)


def _expand_input(raw: str, recursive: bool) -> Tuple[Optional[Path], List[Path]]:
    """Expand one input into ``(root, files)``; ``root`` anchors tree mirroring."""
    p = Path(raw)
    if p.is_dir():
        globber = p.rglob("*") if recursive else p.glob("*")
        files = sorted(f for f in globber if f.is_file() and not _in_skipped_dir(f, p))
        return p, files
    if p.is_file():
        return None, [p]
    # Treat as a glob pattern. ``glob.glob`` handles absolute patterns, which
    # ``Path().glob`` rejects, and ``**`` when recursive.
    matches = sorted(Path(m) for m in _glob.glob(raw, recursive=recursive))
    return _glob_root(raw), [m for m in matches if m.is_file()]


def _glob_root(pattern: str) -> Path:
    """The leading wildcard-free directory of a pattern (``inbox/**/*.txt`` -> ``inbox``)."""
    fixed: List[str] = []
    for part in Path(pattern).parts:
        if any(c in part for c in _GLOB_CHARS):
            break
        fixed.append(part)
    return Path(*fixed) if fixed else Path()


def _in_skipped_dir(path: Path, root: Path) -> bool:
    """True if any directory between ``root`` and ``path`` is hidden or junk."""
    for part in path.relative_to(root).parts[:-1]:
        if part.startswith(".") or part in _SKIP_DIRS:
            return True
    return False


def output_path(
    document: Document, options: RedactionOptions, suffix: Optional[str] = None
) -> Path:
    """Where a backend should write its artifact for ``document``.

    The artifact is always named ``<stem>.redacted<suffix>`` (``suffix``
    defaults to the source's own, e.g. ``report.pdf`` -> ``report.redacted.pdf``;
    pass ``".txt"`` for a text sidecar). Without an output directory it sits
    beside the source. With one, the document's path relative to its ingestion
    :attr:`Document.root` is mirrored beneath it, so ``inbox/a/x.txt`` and
    ``inbox/b/x.txt`` become ``out/a/x.redacted.txt`` and ``out/b/x.redacted.txt``
    instead of overwriting each other.

    This is the one place output naming lives; backends must not roll their own.
    """
    src = document.path
    name = src.stem + ".redacted" + (src.suffix if suffix is None else suffix)
    if not options.output_dir:
        return src.parent / name
    return Path(options.output_dir) / _relative_dir(document) / name


def _relative_dir(document: Document) -> Path:
    if document.root is None:
        return Path()
    try:
        return document.path.relative_to(document.root).parent
    except ValueError:  # path not under root (shouldn't happen; stay flat)
        return Path()
