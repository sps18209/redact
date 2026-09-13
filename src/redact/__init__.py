"""redact-suite — a unified PII/PHI redaction suite.

Ingest any document (text, structured data, PDF, image, video) and route it to
the best available redaction backend — Microsoft Presidio, Philter, RedactAI
(Ollama), pdf-redact-tools, understand.ai Anonymizer — or a built-in, offline
regex engine that always works.
"""

from .document import Document, detect_media_type, iter_documents, load_document
from .registry import BackendRegistry
from .router import RoutingError, candidates, select_backend
from .suite import RedactionSuite
from .types import (
    Entity,
    MediaType,
    RedactionMode,
    RedactionOptions,
    RedactionResult,
)

__version__ = "0.2.0"

__all__ = [
    "RedactionSuite",
    "BackendRegistry",
    "Document",
    "MediaType",
    "Entity",
    "RedactionMode",
    "RedactionOptions",
    "RedactionResult",
    "RoutingError",
    "candidates",
    "select_backend",
    "load_document",
    "iter_documents",
    "detect_media_type",
    "__version__",
]
