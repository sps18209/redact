"""Command-line interface for the redaction suite.

Subcommands
-----------
``redact list``               show every backend and whether it is available
``redact detect <inputs>``    show the detected media type for each input
``redact run <inputs>``       ingest and redact inputs (files, dirs, globs)
``redact search <q> <inputs>`` rank images/video by how well they match a phrase
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .document import iter_documents, unmatched_inputs
from .suite import RedactionSuite
from .types import RedactionMode, RedactionOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redact",
        description="Unified PII/PHI redaction suite — pull any document in, "
        "route it to the best available tool.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    # list
    sub.add_parser("list", help="list backends and their availability")

    # detect
    p_detect = sub.add_parser("detect", help="show detected media types for inputs")
    p_detect.add_argument("inputs", nargs="+", help="files, directories, or globs")
    p_detect.add_argument(
        "--no-recursive", action="store_true", help="do not walk directories recursively"
    )
    p_detect.add_argument(
        "--include-unknown", action="store_true", help="include files of unknown type"
    )

    # run
    p_run = sub.add_parser("run", help="redact inputs")
    p_run.add_argument("inputs", nargs="+", help="files, directories, or globs")
    p_run.add_argument(
        "-b", "--backend", default="auto",
        help="backend name, or 'auto' (default) to let the suite choose",
    )
    p_run.add_argument(
        "-m", "--mode", default="replace",
        choices=[m.value for m in RedactionMode],
        help="how to transform detected entities (default: replace)",
    )
    p_run.add_argument(
        "-e", "--entities", action="append", default=None, metavar="LABEL[,LABEL]",
        help="restrict detection to these entity labels; repeat the flag or "
        "comma-separate, e.g. -e EMAIL_ADDRESS,US_SSN (default: all)",
    )
    p_run.add_argument(
        "--hash-key", default=None, metavar="KEY",
        help="key for -m hash. Same key + same value = same pseudonym, so "
        "records stay correlatable across runs. Omitted, a random per-run key "
        "is used and pseudonyms cannot be linked between runs. Treat the key "
        "as a secret: anyone holding it can invert the tokens",
    )
    p_run.add_argument("-o", "--out", default=None, help="output directory")
    p_run.add_argument("--lang", default="en", help="language code (default: en)")
    p_run.add_argument(
        "--threshold", type=float, default=0.35,
        help="minimum confidence to act on a detection (default: 0.35)",
    )
    p_run.add_argument("--mask-char", default="*", help="character used in mask mode")
    p_run.add_argument(
        "--yolo-model", default=None, metavar="CKPT",
        help="YOLO checkpoint for the 'yolo' backend (default: an open-vocabulary "
        "yolov8s-worldv2.pt; try yolo26x.pt for COCO classes)",
    )
    p_run.add_argument(
        "--yolo-classes", default=None, metavar="A,B",
        help="what the 'yolo' backend should mask, as text prompts, e.g. "
        "\"license plate,human face\" (default: license plate, human face)",
    )
    p_run.add_argument(
        "--docx-images", default="keep", choices=["keep", "strip", "blur"],
        help="images embedded in a .docx/.xlsx: keep them (default), strip them "
        "to a blank placeholder, or blur faces with an image backend",
    )
    p_run.add_argument(
        "--eml-attachments", default="keep", choices=["keep", "strip"],
        help="email attachments that cannot be redacted in place: keep them "
        "(default — reported loudly, never silently) or strip them out",
    )
    p_run.add_argument(
        "--dry-run", action="store_true",
        help="detect and report, but write nothing",
    )
    p_run.add_argument(
        "--no-recursive", action="store_true", help="do not walk directories recursively"
    )
    p_run.add_argument(
        "--include-unknown", action="store_true", help="attempt files of unknown type"
    )
    p_run.add_argument(
        "--match", default=None, metavar="PHRASE",
        help="only redact visual files matching this description (semantic search); "
        "text and PDFs are always kept",
    )
    p_run.add_argument(
        "--match-threshold", type=float, default=0.05, metavar="T",
        help="calibrated score a file must reach to count as a --match "
        "(0-1, default: 0.05); see 'redact search' to pick one",
    )

    # verify
    p_verify = sub.add_parser(
        "verify",
        help="re-scan redacted output for PII that survived (decodes every layer)",
    )
    p_verify.add_argument("inputs", nargs="+", help="redacted files, directories, or globs")
    p_verify.add_argument(
        "--original", default=None, metavar="DIR",
        help="directory holding the SOURCE files. Enables the positive control: "
        "if the same scan finds no PII in the original, a clean result is "
        "reported INCONCLUSIVE rather than clean, because the check is blind "
        "to that file",
    )
    p_verify.add_argument(
        "-e", "--entities", action="append", default=None, metavar="LABEL[,LABEL]",
        help="restrict the scan to these entity labels",
    )
    p_verify.add_argument(
        "--threshold", type=float, default=0.35,
        help="minimum confidence to report (default: 0.35)",
    )
    p_verify.add_argument(
        "--no-recursive", action="store_true", help="do not walk directories recursively"
    )

    # search
    p_search = sub.add_parser(
        "search", help="rank images/video by how well they match a description"
    )
    p_search.add_argument("query", help="what to look for, in plain language")
    p_search.add_argument("inputs", nargs="+", help="files, directories, or globs")
    p_search.add_argument("--top", type=int, default=10, help="results to show (default: 10)")
    p_search.add_argument(
        "--threshold", type=float, default=0.0,
        help="minimum score to report (default: 0.0)",
    )
    p_search.add_argument(
        "--raw-scores", action="store_true",
        help="report uncalibrated cosine similarity instead of the calibrated "
        "score (raw similarity is not comparable across images)",
    )
    p_search.add_argument(
        "--index", default=None, metavar="FILE",
        help="reuse/write an index here instead of embedding every time",
    )
    p_search.add_argument(
        "--frames", type=int, default=8, metavar="N",
        help="frames sampled per video (default: 8)",
    )
    p_search.add_argument(
        "--all-frames", action="store_true",
        help="report every matching frame, not just each file's best",
    )
    p_search.add_argument(
        "--no-recursive", action="store_true", help="do not walk directories recursively"
    )
    return parser


def _cmd_list(suite: RedactionSuite) -> int:
    rows = suite.describe_backends()
    print(f"{'BACKEND':<18} {'AVAIL':<6} {'PRIO':<5} {'MEDIA TYPES':<28} DESCRIPTION")
    print("-" * 100)
    for r in rows:
        avail = "yes" if r["available"] else "no"
        media = ",".join(r["media_types"])
        print(f"{r['name']:<18} {avail:<6} {r['priority']:<5} {media:<28} {r['description']}")
        if not r["available"] and r["missing"]:
            print(f"{'':<18} ├─ needs: {', '.join(r['missing'])}")
            if r.get("install_hint"):
                print(f"{'':<18} └─ {r['install_hint']}")
    return 0


def _report_inputs(inputs: List[str], recursive: bool) -> None:
    """Name inputs that match nothing, so a typo does not look like an empty run."""
    for raw in unmatched_inputs(inputs, recursive):
        print(f"no such file, directory, or glob match: {raw}", file=sys.stderr)


def _report_skipped(skipped: List, include_unknown: bool) -> None:
    if skipped and not include_unknown:
        names = ", ".join(p.name for p in skipped[:3])
        more = f" (+{len(skipped) - 3} more)" if len(skipped) > 3 else ""
        print(
            f"skipped {len(skipped)} file(s) of unrecognised type: {names}{more}"
            " — pass --include-unknown to attempt them",
            file=sys.stderr,
        )


def _cmd_detect(inputs: List[str], recursive: bool, include_unknown: bool) -> int:
    _report_inputs(inputs, recursive)
    skipped: List = []
    any_found = False
    for doc in iter_documents(inputs, recursive, include_unknown, skipped=skipped):
        any_found = True
        print(f"{doc.media_type:<12} {doc.path}")
    _report_skipped(skipped, include_unknown)
    if not any_found:
        print("no matching documents found", file=sys.stderr)
        return 1
    return 0


def _parse_entities(raw: Optional[List[str]]) -> Optional[List[str]]:
    """Flatten repeated/comma-separated ``-e`` values; empty means "all"."""
    if not raw:
        return None
    labels = [
        label.strip().upper()
        for chunk in raw
        for label in chunk.split(",")
        if label.strip()
    ]
    return labels or None


def _yolo_extra(args: argparse.Namespace) -> dict:
    """Backend-specific knobs travel in RedactionOptions.extra."""
    extra = {}
    if getattr(args, "yolo_model", None):
        extra["yolo_model"] = args.yolo_model
    if getattr(args, "yolo_classes", None):
        extra["yolo_classes"] = args.yolo_classes
    return extra


def _cmd_run(suite: RedactionSuite, args: argparse.Namespace) -> int:
    options = RedactionOptions(
        backend=args.backend,
        mode=RedactionMode(args.mode),
        entities=_parse_entities(args.entities),
        language=args.lang,
        threshold=args.threshold,
        mask_char=args.mask_char,
        hash_key=args.hash_key,
        docx_images=args.docx_images,
        eml_attachments=args.eml_attachments,
        extra=_yolo_extra(args),
        output_dir=Path(args.out) if args.out else None,
        dry_run=args.dry_run,
    )

    _report_inputs(args.inputs, not args.no_recursive)
    skipped: List = []
    documents = iter_documents(
        args.inputs, not args.no_recursive, args.include_unknown, skipped=skipped
    )
    if getattr(args, "match", None):
        from .semantic import SemanticError, filter_documents

        try:
            documents = filter_documents(documents, args.match, args.match_threshold)
        except SemanticError as exc:
            print(f"--match unavailable: {exc}", file=sys.stderr)
            return 2
        print(
            f"--match '{args.match}': {len(documents)} file(s) selected",
            file=sys.stderr,
        )

    total = 0
    failures = 0
    incomplete = 0
    for result in (suite.redact_document(doc, options) for doc in documents):
        total += 1
        if not result.success:
            failures += 1
        elif result.unredacted:
            incomplete += 1
        print(result.summary())

    _report_skipped(skipped, args.include_unknown)
    if total == 0:
        print("no matching documents found", file=sys.stderr)
        return 1

    summary = f"\n{total} document(s) processed, {failures} failed"
    if incomplete:
        summary += f", {incomplete} left content unredacted"
    print(summary + ".", file=sys.stderr)
    if incomplete:
        # Exit 0 from a redaction tool means "the output is safe to share".
        # Saying that while content is knowingly unredacted would mislead any
        # script that checks the exit code instead of reading the log.
        print(
            f"{incomplete} document(s) still contain unredacted content — "
            "see the warnings above; exiting non-zero so automation does not "
            "treat this as a clean run.",
            file=sys.stderr,
        )
    return 1 if (failures or incomplete) else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Re-scan finished artifacts. Independent of whatever produced them."""
    from .verify import verify_path

    _report_inputs(args.inputs, not args.no_recursive)
    skipped: List = []
    # include_unknown: verification must look at everything handed to it. A
    # file whose type we cannot name is exactly the one worth scanning.
    documents = list(
        iter_documents(
            args.inputs, not args.no_recursive, True,
            skipped=skipped, include_outputs=True,
        )
    )
    if not documents:
        print("no files to verify", file=sys.stderr)
        return 1

    source_dir = Path(args.original) if args.original else None
    if source_dir and not source_dir.is_dir():
        print(f"--original: not a directory: {source_dir}", file=sys.stderr)
        return 2

    entities = _parse_entities(args.entities)
    leaking = inconclusive = 0
    for doc in documents:
        origin = None
        if source_dir is not None:
            candidate = source_dir / doc.path.name.replace(".redacted", "", 1)
            origin = candidate if candidate.exists() else None
        report = verify_path(
            doc.path, entities=entities, threshold=args.threshold, original=origin
        )
        if not report.clean:
            leaking += 1
        elif report.inconclusive:
            inconclusive += 1
        print(report.summary())

    _report_skipped(skipped, True)
    total = len(documents)
    print(
        f"\n{total} artifact(s) verified, {leaking} leaking, {inconclusive} inconclusive.",
        file=sys.stderr,
    )
    if leaking:
        # The whole point of the verb: a leak must be impossible to miss in a
        # script that only checks the exit code.
        print(
            f"{leaking} artifact(s) still contain detectable PII — they are NOT "
            "safe to share.",
            file=sys.stderr,
        )
        return 1
    if inconclusive:
        print(
            f"{inconclusive} artifact(s) could not be meaningfully checked (the "
            "positive control found nothing in the original). Treat as unverified.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from .semantic import ClipEmbedder, SemanticError, SemanticIndex

    try:
        embedder = ClipEmbedder()
        if args.index and Path(args.index).is_file():
            index = SemanticIndex.load(args.index)
        else:
            _report_inputs(args.inputs, not args.no_recursive)
            docs = iter_documents(args.inputs, not args.no_recursive)
            index = SemanticIndex.build(docs, embedder, frames_per_video=args.frames)
            if args.index:
                index.save(args.index)
        if not len(index):
            print("no images or video found to search", file=sys.stderr)
            return 1
        matches = index.query(
            args.query, embedder, top_k=args.top,
            threshold=args.threshold, per_file=not args.all_frames,
            calibrate=not args.raw_scores,
        )
    except SemanticError as exc:
        print(f"semantic search unavailable: {exc}", file=sys.stderr)
        return 2

    if not matches:
        print("no matches above the threshold", file=sys.stderr)
        return 1
    print(f"{len(matches)} match(es) for {args.query!r} "
          f"across {len(index)} embedded frame(s):")
    for m in matches:
        print("  " + m.describe())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"redact-suite {__version__}")
        return 0

    if not args.command:
        parser.print_help()
        return 1

    suite = RedactionSuite()

    if args.command == "list":
        return _cmd_list(suite)
    if args.command == "detect":
        return _cmd_detect(
            args.inputs, not args.no_recursive, args.include_unknown
        )
    if args.command == "run":
        return _cmd_run(suite, args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "search":
        return _cmd_search(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
