"""Machine-readable records of a run, for automation and for models.

The CLI's human output is a line per document. That is unusable for a pipeline
that has to decide whether to release a batch, and for a model asked "what did
this run find?" — both end up parsing prose. This module emits the same facts as
JSON with a stable schema.

**The report must not become the leak.** ``Entity.text`` holds the value that was
found: the SSN, the email, the card number. A report that lists them is a fresh
copy of exactly the data the run existed to remove — and unlike the redacted
artifact, nobody thinks of a JSON log as sensitive. So values are omitted by
default and every payload says which mode produced it
(``contains_pii_values``). ``--json-include-values`` opts in, for an
investigation where you need to see what was hit, and the file is then as
sensitive as the source documents.

The schema is versioned. A consumer should check ``schema`` and
``schema_version`` and refuse what it does not understand, rather than guessing
at fields.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .types import Entity, RedactionResult

#: Bumped when a field changes meaning or disappears. Additions are minor.
SCHEMA_VERSION = "1.0"

RUN_SCHEMA = "redact-suite/run-report"
VERIFY_SCHEMA = "redact-suite/verify-report"

__all__ = [
    "SCHEMA_VERSION", "RUN_SCHEMA", "VERIFY_SCHEMA",
    "run_report", "verify_report", "write",
]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _version() -> str:
    from . import __version__

    return __version__


def _entity(entity: Entity, include_values: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "type": entity.entity_type,
        "score": round(float(entity.score), 4),
        "start": entity.start,
        "end": entity.end,
        "bbox": list(entity.bbox) if entity.bbox else None,
    }
    if include_values:
        out["text"] = entity.text
    return out


def _counts(entities: Sequence[Entity]) -> Dict[str, int]:
    """Per-label totals — the useful summary when values are withheld."""
    counts: Dict[str, int] = {}
    for entity in entities:
        counts[entity.entity_type] = counts.get(entity.entity_type, 0) + 1
    return dict(sorted(counts.items()))


def _document(result: RedactionResult, include_values: bool) -> Dict[str, Any]:
    return {
        "source": str(result.source),
        "output": str(result.output_path) if result.output_path else None,
        "backend": result.backend,
        "media_type": str(result.media_type),
        "success": result.success,
        # The field automation should branch on: success alone can be True while
        # content was knowingly left behind.
        "fully_redacted": result.fully_redacted,
        "entity_count": result.entity_count,
        "entity_counts": _counts(result.entities),
        "entities": [_entity(e, include_values) for e in result.entities],
        "unredacted": list(result.unredacted),
        "message": result.message,
    }


def run_report(
    results: Sequence[RedactionResult],
    include_values: bool = False,
    exit_code: Optional[int] = None,
) -> Dict[str, Any]:
    """The JSON payload describing one ``redact run``."""
    documents = [_document(r, include_values) for r in results]
    failed = [d for d in documents if not d["success"]]
    incomplete = [d for d in documents if d["success"] and not d["fully_redacted"]]
    return {
        "schema": RUN_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tool_version": _version(),
        "generated_at": _now(),
        # Say plainly whether this file is itself sensitive.
        "contains_pii_values": bool(include_values),
        "summary": {
            "documents": len(documents),
            "failed": len(failed),
            "incomplete": len(incomplete),
            "entities": sum(d["entity_count"] for d in documents),
            # One boolean for "is every output safe to release?".
            "all_safe": not failed and not incomplete,
            "exit_code": exit_code,
        },
        "documents": documents,
    }


def verify_report(
    reports: Sequence[Any],
    include_values: bool = False,
    exit_code: Optional[int] = None,
) -> Dict[str, Any]:
    """The JSON payload describing one ``redact verify``."""
    artifacts: List[Dict[str, Any]] = []
    for report in reports:
        findings = []
        for finding in report.findings:
            item = {
                "type": finding.entity.entity_type,
                "layer": finding.layer,
            }
            if include_values:
                item["text"] = finding.entity.text
            findings.append(item)
        artifacts.append({
            "path": str(report.path),
            "media_type": str(report.media_type),
            "status": report.status,
            "clean": report.clean,
            "inconclusive": report.inconclusive,
            "layers_scanned": report.layers_scanned,
            "finding_count": len(report.findings),
            "findings": findings,
            "control_entities": report.control_entities,
            "note": report.note,
        })
    leaking = [a for a in artifacts if not a["clean"]]
    inconclusive = [a for a in artifacts if a["clean"] and a["inconclusive"]]
    return {
        "schema": VERIFY_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tool_version": _version(),
        "generated_at": _now(),
        "contains_pii_values": bool(include_values),
        "summary": {
            "artifacts": len(artifacts),
            "leaking": len(leaking),
            "inconclusive": len(inconclusive),
            # "Verified clean" means neither leaking NOR unverifiable. An
            # inconclusive result is not a pass.
            "all_verified_clean": not leaking and not inconclusive,
            "exit_code": exit_code,
        },
        "artifacts": artifacts,
    }


def write(payload: Dict[str, Any], destination: str) -> None:
    """Write ``payload`` to a file, or to stdout when ``destination`` is ``-``."""
    text = json.dumps(payload, indent=2, sort_keys=False)
    if destination == "-":
        print(text)
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    if payload.get("contains_pii_values"):
        # This file now holds the values the run removed from the documents.
        print(
            f"WARNING: {path} contains the detected PII values themselves — "
            "treat it as sensitive as the source documents.",
            file=sys.stderr,
        )
