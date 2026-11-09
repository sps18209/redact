"""Backend selection — the logic that "chooses between any of these tools".

Given a document and the user's options, the router decides which backend runs:

* If the user names a backend (``options.backend != "auto"``), that backend is
  used — but only if it both supports the document's media type and is available.
* Otherwise the router picks the highest-priority available backend for the
  media type that processes content without a service endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .backends.base import Backend
from .document import Document
from .registry import BackendRegistry
from .types import RedactionOptions


class RoutingError(Exception):
    """Raised when no suitable backend can be selected for a document."""


@dataclass
class Candidate:
    backend: Backend
    available: bool
    priority: int


def candidates(
    document: Document, registry: BackendRegistry
) -> List[Candidate]:
    """Local automatic routing candidates for the media type, best first."""
    supporting = registry.for_media_type(document.media_type)
    cands = [
        Candidate(backend=b, available=b.is_available(), priority=b.priority)
        for b in supporting
        if not b.uses_service
    ]
    # Available backends first, then by descending priority, then name for stability.
    cands.sort(key=lambda c: (not c.available, -c.priority, c.backend.name))
    return cands


def select_backend(
    document: Document,
    options: RedactionOptions,
    registry: BackendRegistry,
) -> Backend:
    """Return the backend that should redact ``document``.

    Raises :class:`RoutingError` with an actionable message when nothing fits.
    """
    if options.backend and options.backend != "auto":
        backend = registry.get(options.backend)
        if backend is None:
            raise RoutingError(
                f"unknown backend '{options.backend}'. "
                f"Known: {', '.join(registry.names())}"
            )
        if not backend.supports(document.media_type):
            raise RoutingError(
                f"backend '{backend.name}' does not support {document.media_type} "
                f"files (needed for {document.name})"
            )
        if not backend.is_available():
            missing = ", ".join(backend.missing_dependencies())
            hint = f". To enable it: {backend.install_hint}" if backend.install_hint else ""
            raise RoutingError(
                f"backend '{backend.name}' is not available: missing {missing}{hint}"
            )
        return backend

    # Auto mode.
    local = candidates(document, registry)
    for cand in local:
        if cand.available:
            return cand.backend

    if not local:
        raise RoutingError(
            f"no local backend supports {document.media_type} files ({document.name})"
        )
    hints = "; ".join(
        f"{cand.backend.name}: {cand.backend.install_hint or 'install its dependencies'}"
        for cand in local
    )
    raise RoutingError(
        f"no available local backend for {document.media_type} ({document.name}); {hints}"
    )
