"""Discovery must be cheap and crash-free even with no tools installed."""

from redact.backends import DEFAULT_BACKENDS
from redact.types import MediaType


def test_every_backend_reports_availability_safely():
    for cls in DEFAULT_BACKENDS:
        backend = cls()
        # Must not raise, must return a list, must return a bool.
        missing = backend.missing_dependencies()
        assert isinstance(missing, list)
        assert isinstance(backend.is_available(), bool)
        assert backend.name
        assert backend.supported_media_types


def test_backends_declare_supported_media_types():
    coverage = set()
    for cls in DEFAULT_BACKENDS:
        coverage.update(cls().supported_media_types)
    # The suite as a whole should cover every real media type.
    assert {
        MediaType.TEXT, MediaType.STRUCTURED, MediaType.DOCX, MediaType.XLSX,
        MediaType.PPTX, MediaType.EMAIL, MediaType.PDF,
        MediaType.IMAGE, MediaType.VIDEO,
    } <= coverage


def test_backend_failure_is_result_not_exception(tmp_path):
    """An unavailable backend returns a failed result rather than raising."""
    from redact.backends.presidio import PresidioBackend
    from redact.document import Document
    from redact.types import RedactionOptions

    src = tmp_path / "a.txt"
    src.write_text("a@b.com")
    doc = Document(path=src, media_type=MediaType.TEXT)
    backend = PresidioBackend()
    if not backend.is_available():
        res = backend.redact(doc, RedactionOptions())
        assert res.success is False
        assert "missing" in res.message.lower()


def test_every_optional_backend_explains_how_to_install_it():
    """A backend that can be unavailable must tell the user what to do."""
    from redact.backends.builtin import BuiltinBackend

    for cls in DEFAULT_BACKENDS:
        backend = cls()
        if isinstance(backend, BuiltinBackend):
            continue  # always available; nothing to install
        assert backend.install_hint, f"{backend.name} has no install_hint"


def test_all_extra_covers_every_pip_installable_backend():
    """README promises [all] installs them; an omission is a broken promise."""
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text()
    optional = re.search(r"\[project\.optional-dependencies\](.*?)\n\[", text, re.S).group(1)
    extras = dict(re.findall(r"^(\w+) = \[(.*?)\]", optional, re.S | re.M))
    all_pkgs = extras["all"]
    for name in ("presidio", "pdf", "deface", "yolo", "semantic"):
        for pkg in re.findall(r'"([^"]+)"', extras[name]):
            root = pkg.split()[0].split(">=")[0].split("==")[0]
            assert root in all_pkgs, f"[all] is missing {root} (from [{name}])"
