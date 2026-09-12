"""Command-line interface for the redaction suite.

Subcommands
-----------
``redact list``               show every backend and whether it is available
``redact detect <inputs>``    show the detected media type for each input
``redact run <inputs>``       ingest and redact inputs (files, dirs, globs)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .document import iter_documents
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
        help="images embedded in a .docx: keep them (default), strip them to a "
        "blank placeholder, or blur faces/plates with an image backend",
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
            print(f"{'':<18} └─ needs: {', '.join(r['missing'])}")
    return 0


def _cmd_detect(inputs: List[str], recursive: bool, include_unknown: bool) -> int:
    any_found = False
    for doc in iter_documents(inputs, recursive, include_unknown):
        any_found = True
        print(f"{doc.media_type:<12} {doc.path}")
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
        docx_images=args.docx_images,
        extra=_yolo_extra(args),
        output_dir=Path(args.out) if args.out else None,
        dry_run=args.dry_run,
    )

    total = 0
    failures = 0
    for result in suite.redact_paths(
        args.inputs,
        options,
        recursive=not args.no_recursive,
        include_unknown=args.include_unknown,
    ):
        total += 1
        if not result.success:
            failures += 1
        print(result.summary())

    if total == 0:
        print("no matching documents found", file=sys.stderr)
        return 1
    print(f"\n{total} document(s) processed, {failures} failed.", file=sys.stderr)
    return 1 if failures else 0


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

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
