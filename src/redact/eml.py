"""Email (.eml) support — standard library only.

Email is a primary carrier of PII, and it is the format where a redaction tool
is most likely to *lie*. An ``.eml`` file is text, so the suite used to route it
to the plain-text engine, which redacted the headers and body and reported
success — while any attachment, being base64, sailed through untouched. Grepping
the "redacted" output for the SSN found nothing, because the SSN was encoded.
The user's own verification confirmed a redaction that had not happened.

So this module treats a message as a structure, not a blob:

* **Headers** that carry identity (``From``, ``To``, ``Cc``, ``Bcc``,
  ``Reply-To``, ``Subject``) are redacted. Routing headers (``Received``,
  ``Message-ID``) carry host and address data too and are scanned as well.
* **Text parts** are decoded from their transfer encoding and charset, redacted,
  and written back — so base64-encoded *text* is no longer a blind spot.
* **Binary attachments** (a PDF, an image, a zip) cannot be redacted here. They
  are never silently retained: each one is reported, and ``strip`` replaces it
  with a short notice. Refusing loudly is safe; passing silently is not.

Nested ``message/rfc822`` parts are walked too — a forwarded email is a common
way for the original addresses to survive.

``.mbox`` archives are split into their individual messages first. Handing a
mailbox to a single-message parser folds messages 2..N into the *body* of
message 1: the text happens to still get redacted, but the archive is destroyed
and the tool reports a clean run over a file no mail client can read again.
"""

from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import List, Optional, Tuple

from .opc import Detect, Replace, apply_text, unpositioned
from .types import Entity

#: Headers whose values are redacted (identity and routing).
REDACTED_HEADERS = (
    "from", "to", "cc", "bcc", "reply-to", "sender", "return-path",
    "subject", "received", "message-id", "in-reply-to", "references",
    "x-originating-ip", "delivered-to",
)

#: Entity label reported for an attachment that could not be redacted.
ATTACHMENT_ENTITY = "EMAIL_ATTACHMENT"

#: What to do with attachments this module cannot redact.
ATTACHMENT_POLICIES = ("keep", "strip")

#: An mbox message begins at a line starting with ``From `` — the same rule the
#: stdlib :mod:`mailbox` applies. Writers escape a body line that would collide
#: as ``>From ``, so this is the conventional boundary, not a guess.
_MBOX_START = b"From "

_STRIPPED_NOTICE = (
    "[attachment removed by redact-suite: could not be redacted in place]"
)


class EmlError(Exception):
    """Raised when a message cannot be parsed."""


@dataclass
class EmlRedaction:
    """What :func:`redact_eml` found.

    Entities carry no offsets: a message has no single linear text flow, and a
    finding in a base64 attachment has no position in the file as stored.
    """

    entities: List[Entity] = field(default_factory=list)
    redacted_text: str = ""
    notes: List[str] = field(default_factory=list)
    #: Attachments left in place that this module could not redact.
    unredacted_attachments: List[str] = field(default_factory=list)


def extract_text(path) -> str:
    """Header values and decoded text parts, one per line.

    Covers every message in the file, so an mbox reports all of its mail.
    """
    lines: List[str] = []
    for separator, msg in _messages(path):
        if separator:
            lines.append(separator)
        for name, value in msg.items():
            if name.lower() in REDACTED_HEADERS and value:
                lines.append(f"{name}: {value}")
        for part in _walk(msg):
            if part.get_content_maintype() == "text":
                text = _part_text(part)
                if text:
                    lines.append(text)
    return "\n".join(lines)


def redact_eml(
    source: Path,
    out: Optional[Path],
    detect: Detect,
    replace: Replace,
    scrub_authors: bool = True,
    attachment_policy: str = "keep",
) -> EmlRedaction:
    """Redact ``source`` into ``out`` (``None`` = detect only, write nothing).

    Handles both a single message and an mbox archive; the archive keeps its
    ``From `` separators so the output is still a mailbox.
    """
    if attachment_policy not in ATTACHMENT_POLICIES:
        raise ValueError(
            f"attachment_policy must be one of {ATTACHMENT_POLICIES}, got {attachment_policy!r}"
        )

    result = EmlRedaction()
    chunks: List[str] = []
    stripped = 0
    rendered: List[bytes] = []

    for separator, msg in _messages(source):
        if separator is not None:
            # The mbox separator line carries the envelope sender.
            new_sep, sep_ents = apply_text(separator, detect, replace)
            result.entities.extend(unpositioned(sep_ents))
            if sep_ents:
                chunks.append(new_sep)
            rendered.append(new_sep.encode("utf-8", "replace") + b"\n")
        entities, part_chunks, unredacted, n = _redact_message(
            msg, detect, replace, attachment_policy
        )
        result.entities.extend(entities)
        chunks.extend(part_chunks)
        result.unredacted_attachments.extend(unredacted)
        stripped += n
        rendered.append(msg.as_bytes())

    result.redacted_text = "\n".join(chunks)
    if stripped:
        result.notes.append(f"{stripped} attachment(s) removed (not redactable in place)")
    if result.unredacted_attachments:
        names = ", ".join(result.unredacted_attachments)
        result.notes.append(
            f"WARNING: {len(result.unredacted_attachments)} attachment(s) NOT redacted: "
            f"{names} — their contents are unchanged; re-run with "
            "--eml-attachments strip, or extract and redact them separately"
        )

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"".join(rendered))
    return result


def _redact_message(
    msg: Message,
    detect: Detect,
    replace: Replace,
    attachment_policy: str,
) -> Tuple[List[Entity], List[str], List[str], int]:
    """Redact one message in place. Returns (entities, text chunks,
    attachments left unredacted, attachments stripped)."""
    entities: List[Entity] = []
    chunks: List[str] = []
    unredacted: List[str] = []
    stripped = 0

    # 1. Headers.
    for name in list(msg.keys()):
        if name.lower() not in REDACTED_HEADERS:
            continue
        value = msg.get(name)
        if not value:
            continue
        new, ents = apply_text(str(value), detect, replace)
        if ents:
            _replace_header(msg, name, new)
            entities.extend(unpositioned(ents))
            chunks.append(new)

    # 2. Body parts and attachments.
    for part in _walk(msg):
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()

        if part.get_content_maintype() == "text":
            text = _part_text(part)
            if not text:
                continue
            new, ents = apply_text(text, detect, replace)
            if ents:
                _set_part_text(part, new)
                entities.extend(unpositioned(ents))
            chunks.append(new)
            continue

        # A binary attachment: this module cannot redact its interior.
        label = filename or f"<{part.get_content_type()}>"
        entities.append(Entity(entity_type=ATTACHMENT_ENTITY, score=1.0, text=label))
        if attachment_policy == "strip":
            _strip_part(part)
            stripped += 1
        else:
            unredacted.append(label)

    return entities, chunks, unredacted, stripped


# -- internals -----------------------------------------------------------------

def _messages(path) -> List[Tuple[Optional[str], Message]]:
    """Every message in the file, paired with its mbox separator line.

    A plain ``.eml`` yields one ``(None, message)``. An mbox yields one entry
    per message, each carrying the ``From `` line that introduced it so the
    archive can be rebuilt.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise EmlError(f"could not read message: {exc}") from exc

    if not raw.startswith(_MBOX_START):
        return [(None, _parse_bytes(raw))]
    return [(sep, _parse_bytes(body)) for sep, body in _split_mbox(raw)]


def _split_mbox(raw: bytes) -> List[Tuple[str, bytes]]:
    """Split an mbox into ``(separator line, message bytes)`` pairs."""
    entries: List[Tuple[str, bytes]] = []
    separator: Optional[str] = None
    body: List[bytes] = []
    for line in raw.splitlines(keepends=True):
        if line.startswith(_MBOX_START):
            if separator is not None:
                entries.append((separator, b"".join(body)))
            separator = line.rstrip(b"\r\n").decode("utf-8", "replace")
            body = []
            continue
        body.append(line)
    if separator is not None:
        entries.append((separator, b"".join(body)))
    return entries


def _parse_bytes(raw: bytes) -> Message:
    try:
        return email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception as exc:  # malformed MIME
        raise EmlError(f"could not parse message: {exc}") from exc


def _walk(msg: Message):
    """Every part, descending into forwarded ``message/rfc822`` payloads."""
    for part in msg.walk():
        yield part
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                for inner in payload:
                    if isinstance(inner, Message):
                        yield from _walk(inner)


def _part_text(part: Message) -> str:
    """Decode a text part through its transfer encoding and charset."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _set_part_text(part: Message, text: str) -> None:
    """Write text back, re-encoding so the stored bytes really change."""
    charset = part.get_content_charset() or "utf-8"
    try:
        encoded = text.encode(charset)
    except (LookupError, UnicodeEncodeError):
        charset, encoded = "utf-8", text.encode("utf-8")
    del part["Content-Transfer-Encoding"]
    part.set_payload(encoded.decode(charset), charset=charset)


def _replace_header(msg: Message, name: str, value: str) -> None:
    del msg[name]
    msg[name] = value


def _strip_part(part: Message) -> None:
    """Replace an attachment's payload with a notice, keeping the structure valid."""
    for header in ("Content-Transfer-Encoding", "Content-Type", "Content-Disposition"):
        del part[header]
    part["Content-Type"] = 'text/plain; charset="utf-8"'
    part["Content-Disposition"] = "inline"
    part.set_payload(_STRIPPED_NOTICE, charset="utf-8")
