import pytest

from redact.backends.base import Backend
from redact.document import Document
from redact.registry import BackendRegistry
from redact.router import RoutingError, candidates, select_backend
from redact.types import MediaType, RedactionOptions


class _FakeBackend(Backend):
    def __init__(self, name, media_types, priority, available):
        self.name = name
        self.supported_media_types = media_types
        self.priority = priority
        self._available = available

    def missing_dependencies(self):
        return [] if self._available else ["something"]

    def redact(self, document, options):  # pragma: no cover - not exercised
        raise NotImplementedError


def _doc(media_type=MediaType.TEXT):
    return Document(path=__import__("pathlib").Path("x.txt"), media_type=media_type)


def test_auto_picks_highest_priority_available():
    reg = BackendRegistry([
        _FakeBackend("low", (MediaType.TEXT,), 10, True),
        _FakeBackend("high", (MediaType.TEXT,), 90, True),
        _FakeBackend("higher_unavailable", (MediaType.TEXT,), 99, False),
    ])
    backend = select_backend(_doc(), RedactionOptions(backend="auto"), reg)
    assert backend.name == "high"


def test_auto_raises_when_none_available():
    reg = BackendRegistry([
        _FakeBackend("x", (MediaType.PDF,), 10, False),
    ])
    with pytest.raises(RoutingError):
        select_backend(_doc(MediaType.PDF), RedactionOptions(), reg)


def test_auto_raises_when_media_unsupported():
    reg = BackendRegistry([_FakeBackend("x", (MediaType.TEXT,), 10, True)])
    with pytest.raises(RoutingError):
        select_backend(_doc(MediaType.VIDEO), RedactionOptions(), reg)


def test_explicit_backend_selected():
    reg = BackendRegistry([
        _FakeBackend("a", (MediaType.TEXT,), 10, True),
        _FakeBackend("b", (MediaType.TEXT,), 90, True),
    ])
    backend = select_backend(_doc(), RedactionOptions(backend="a"), reg)
    assert backend.name == "a"


def test_explicit_unknown_backend_errors():
    reg = BackendRegistry([_FakeBackend("a", (MediaType.TEXT,), 10, True)])
    with pytest.raises(RoutingError):
        select_backend(_doc(), RedactionOptions(backend="ghost"), reg)


def test_explicit_backend_wrong_media_errors():
    reg = BackendRegistry([_FakeBackend("a", (MediaType.TEXT,), 10, True)])
    with pytest.raises(RoutingError):
        select_backend(_doc(MediaType.PDF), RedactionOptions(backend="a"), reg)


def test_explicit_unavailable_backend_errors():
    reg = BackendRegistry([_FakeBackend("a", (MediaType.TEXT,), 10, False)])
    with pytest.raises(RoutingError):
        select_backend(_doc(), RedactionOptions(backend="a"), reg)


def test_candidates_orders_available_first():
    reg = BackendRegistry([
        _FakeBackend("unavail_high", (MediaType.TEXT,), 99, False),
        _FakeBackend("avail_low", (MediaType.TEXT,), 5, True),
    ])
    cands = candidates(_doc(), reg)
    assert cands[0].backend.name == "avail_low"
    assert cands[0].available is True
