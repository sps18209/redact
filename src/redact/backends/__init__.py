"""Backend adapters for each redaction tool the suite can drive."""

from .anonymizer import AnonymizerBackend
from .base import Backend
from .builtin import BuiltinBackend
from .deface import DefaceBackend
from .pdf_redact_tools import PdfRedactToolsBackend
from .philter import PhilterBackend
from .presidio import PresidioBackend
from .redactai import RedactAIBackend

#: The backend classes registered by default, in no particular order (the
#: router orders by media type + priority + availability at selection time).
DEFAULT_BACKENDS = [
    BuiltinBackend,
    PresidioBackend,
    DefaceBackend,
    PhilterBackend,
    RedactAIBackend,
    PdfRedactToolsBackend,
    AnonymizerBackend,
]

__all__ = [
    "Backend",
    "BuiltinBackend",
    "PresidioBackend",
    "DefaceBackend",
    "PhilterBackend",
    "RedactAIBackend",
    "PdfRedactToolsBackend",
    "AnonymizerBackend",
    "DEFAULT_BACKENDS",
]
