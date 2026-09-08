"""A registry of backend instances the suite can select among."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Type

from .backends import DEFAULT_BACKENDS
from .backends.base import Backend
from .types import MediaType


class BackendRegistry:
    """Holds one instance of each known backend, keyed by name."""

    def __init__(self, backends: Optional[Iterable[Backend]] = None):
        self._backends: Dict[str, Backend] = {}
        for backend in backends or []:
            self.register(backend)

    def register(self, backend: Backend) -> None:
        self._backends[backend.name] = backend

    def get(self, name: str) -> Optional[Backend]:
        return self._backends.get(name)

    def all(self) -> List[Backend]:
        return list(self._backends.values())

    def names(self) -> List[str]:
        return sorted(self._backends)

    def available(self) -> List[Backend]:
        return [b for b in self._backends.values() if b.is_available()]

    def for_media_type(self, media_type: MediaType) -> List[Backend]:
        """All registered backends that support ``media_type`` (any availability)."""
        return [b for b in self._backends.values() if b.supports(media_type)]

    @classmethod
    def with_defaults(cls) -> "BackendRegistry":
        """Build a registry populated with one instance of every default backend."""
        return cls(backend_cls() for backend_cls in DEFAULT_BACKENDS)
