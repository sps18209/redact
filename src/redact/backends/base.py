"""The backend contract every redaction tool adapter implements.

A *backend* wraps one underlying redaction tool (Presidio, Philter, Anonymizer,
…). Adapters are deliberately thin: they declare what they can do and whether
their dependencies are present, and they translate the suite's neutral
:class:`RedactionOptions` into a call against the real tool.
"""

from __future__ import annotations

import abc
from typing import List, Sequence

from ..document import Document
from ..types import MediaType, RedactionOptions, RedactionResult


class Backend(abc.ABC):
    """Abstract base class for a redaction backend adapter."""

    #: Stable, lowercase identifier used on the CLI (``--backend NAME``).
    name: str = "base"

    #: One-line human description shown by ``redact list``.
    description: str = ""

    #: Media types this backend can process.
    supported_media_types: Sequence[MediaType] = ()

    #: Router priority when several backends can handle a document. Higher wins.
    #: Purpose-built tools should outrank general fallbacks.
    priority: int = 0

    def supports(self, media_type: MediaType) -> bool:
        return media_type in self.supported_media_types

    @abc.abstractmethod
    def missing_dependencies(self) -> List[str]:
        """Return the names of anything required but not currently available.

        An empty list means the backend is ready to run. Adapters must not raise
        here — this is called during discovery and must be cheap and safe.
        """

    def is_available(self) -> bool:
        """True when the backend can actually run right now."""
        try:
            return not self.missing_dependencies()
        except Exception:  # defensive: discovery must never crash the suite
            return False

    @abc.abstractmethod
    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        """Detect and redact sensitive content in ``document``.

        Implementations should return a :class:`RedactionResult` with
        ``success=False`` and a helpful ``message`` rather than raising for
        expected failure modes (missing dep, unreadable file).
        """
