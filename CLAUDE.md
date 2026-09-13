# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`redact-suite` is a **unified PII/PHI redaction suite**. It ingests any document
(text, structured data, Word .docx, PDF, image, video), detects its media type, and routes
it to the best *available* redaction backend — or one the user names explicitly.
A dependency-free rule engine ships built in, so the suite always works; every
heavy tool (Presidio, Philter, RedactAI/Ollama, pdf-redact-tools, Anonymizer) is
an optional, self-reporting backend.

## Architecture at a glance

```
inputs (files/dirs/globs)
        │  document.py         detect media type (extension → magic bytes)
        ▼
   RedactionSuite (suite.py)   orchestrates ingest → route → run
        │  router.py           pick backend: explicit choice, else
        │                      highest-priority AVAILABLE one for the media type
        ▼
   Backend (backends/*.py)     one adapter per tool; neutral options → real call
        ▼
   RedactionResult (types.py)  entities found, output path, status, message
```

Key idea: the suite speaks one neutral vocabulary (`types.py`) so the router,
CLI, and every adapter are decoupled from any specific tool.

## File map

| Path | Responsibility |
|---|---|
| `src/redact/types.py` | Core dataclasses/enums: `MediaType`, `RedactionMode`, `Entity`, `RedactionOptions`, `RedactionResult`. Dependency-free — the shared vocabulary. |
| `src/redact/document.py` | Ingestion: `MediaType` detection, `load_document`, `iter_documents` (files/dirs/globs), and **`output_path`** — the one rule for where artifacts go. |
| `src/redact/opc.py` | Shared Office Open XML plumbing for `.docx`/`.xlsx`: namespace-safe parse/serialize, split-run rewriting, image policy, rels retargeting, package writing. |
| `src/redact/xlsx.py` | Excel: shared strings (deduped, located by cell), rich-text runs, cached formula results, comments, headers/footers, drawings. |
| `src/redact/docx.py` | Stdlib `.docx` support: paragraph-level detection mapped back onto `<w:t>` runs; also tracked deletions, field codes, revision/comment authors, docProps, `.rels` hyperlink targets and embedded images. Word-safe XML round-trip. |
| `src/redact/backends/base.py` | `Backend` ABC — the adapter contract. |
| `src/redact/backends/builtin.py` | Offline regex/rule engine. Also exports reusable `detect_entities` / `apply_redactions`. Always available. |
| `src/redact/backends/presidio.py` | Microsoft Presidio (text/structured), optional import. |
| `src/redact/backends/philter.py` | Philter service over HTTP (stdlib urllib). |
| `src/redact/backends/redactai.py` | RedactAI-style contextual PDF redaction via local Ollama. |
| `src/redact/backends/pdf_redact_tools.py` | Shells out to `pdf-redact-tools` CLI. |
| `src/redact/semantic.py` | CLIP semantic search: embed images + sampled video frames, calibrated scoring, on-disk index, and `filter_documents` behind `run --match`. |
| `src/redact/media.py` | Shared ffmpeg discovery (system, else the static `imageio-ffmpeg` build), video fps, and audio muxing. |
| `src/redact/backends/yolo.py` | Ultralytics YOLO: open-vocabulary prompts (YOLO-World) or COCO classes; masks boxes with blur/mosaic/solid. Video runs go through `continuity.py` and are verified via `verification/`. |
| `src/redact/continuity.py` | `TemporalMaskTracker` — bounded temporal continuity for per-frame visual detections: IoU/centre-gate association, interpolation across healed gaps, padded propagation, and unresolved-gap (masked→exposed→masked) reporting. Dependency-free. |
| `src/redact/verification/` | Reopen-and-verify for video outputs: full decode + frame-count check, `passed`/`passed_with_warnings`/`failed` rollup, and the `<output>.verification.json` audit sidecar. |
| `src/redact/backends/deface.py` | deface face blurring (image/video) via its Python API. Note: `import deface` here resolves to the installed library, not this module (Python 3 absolute imports). |
| `src/redact/backends/anonymizer.py` | understand.ai Anonymizer (image/video), legacy CLI. |
| `src/redact/registry.py` | `BackendRegistry` — holds backend instances, lookups, availability. |
| `src/redact/router.py` | `select_backend` / `candidates` — the selection policy. |
| `src/redact/suite.py` | `RedactionSuite` — high-level entry point + batch. |
| `src/redact/cli.py` | `redact` CLI: `list` / `detect` / `run` / `search`. |
| `tests/` | pytest suite — ingestion, detection, routing, builtin, CLI, discovery, and audit regressions. |
| `.github/workflows/ci.yml` | Fast CI: pytest on 3.9/3.11/3.12 + CLI smoke test. |
| `.github/workflows/heavy.yml` | Weekly/dispatch CI that actually downloads models: runs the `REDACT_TEST_YOLO_WEIGHTS`/`REDACT_TEST_CLIP` gated tests, and proves the real Presidio library reports itself available. |

## Backends

| Name | Media types | Priority | Requires |
|---|---|---|---|
| `builtin` | text, structured, docx, xlsx | 10 | nothing (stdlib) |
| `presidio` | text, structured, docx, xlsx | 80 | `presidio-analyzer`, `presidio-anonymizer` + spaCy model |
| `philter` | text, structured | 70 | running Philter service (`PHILTER_ENDPOINT`) |
| `redactai` | pdf, text | 60 | `pypdf` + running Ollama (`OLLAMA_HOST`) |
| `yolo` | image, video | 65 | `ultralytics` + `opencv-python`. Open-vocabulary (YOLO-World) by default, so classes are text prompts — **the only working license-plate path**. Below deface on purpose: deface is the better *face* detector, so `auto` keeps it. |
| `deface` | image, video | 70 | `deface` (pip). Bundled CenterFace ONNX model + static ffmpeg, so fully offline. **Faces only — no license plates.** |
| `anonymizer` | image, video | 60 | git checkout via `ANONYMIZER_HOME` (or `ANONYMIZER_BIN`). **Legacy**: pins `tensorflow-gpu==1.11.0` (Python ≤3.6), so it does not install on current Python. Kept solely because it is the only backend covering **license plates**. Not the PyPI `anonymizer` package — that's unrelated. |
| `pdf-redact-tools` | pdf | 40 | `pdf-redact-tools` on PATH |

Priority orders auto-selection: purpose-built tools outrank the builtin fallback.
The router only ever picks a backend that is *available right now*.

## Common commands

```bash
pip install -e ".[dev]"     # core suite + pytest
pytest                      # run all tests (must stay green)

redact list                 # backends + availability (and what each missing one needs)
redact detect ./inbox       # show detected media type per file
redact run report.pdf                 # auto-route one file
redact run ./inbox -o ./clean         # batch a folder
redact run notes.txt -b presidio      # force a backend
redact run data.csv -m mask           # mask instead of <TYPE> placeholder
redact run notes.txt --dry-run        # detect only, write nothing
redact run notes.txt -e EMAIL_ADDRESS,US_SSN   # restrict entity types (comma or repeat -e)
python -m redact list                 # module entry point equivalent
```

## Conventions & invariants (please preserve)

- **`types.py` stays dependency-free.** No third-party imports there.
- **Adapters degrade gracefully.** `missing_dependencies()` must be cheap and
  never raise; `redact()` returns a `RedactionResult(success=False, message=...)`
  for expected failures (missing dep, unreadable file) instead of raising, so a
  batch never aborts on one bad document.
- **Heavy imports are lazy** — import spaCy/pypdf/etc. *inside* methods, never at
  module top level, so discovery and `redact list` stay fast.
- **The builtin backend must always be available** (stdlib only). It's the
  guaranteed fallback and the source of the shared `detect_entities` helper.
- **Redaction modes** live in `RedactionMode`; text rewriting is centralized in
  `builtin.apply_redactions`. Reuse it rather than re-implementing per backend.
- Keep the CLI's three verbs (`list`/`detect`/`run`) thin — logic belongs in the
  suite/router/backends, not `cli.py`.
- **Ingestion never re-reads the suite's own outputs** (`document.is_redaction_output`)
  and skips hidden/junk dirs, so a batch is idempotent. Any new backend that
  writes artifacts must name them `<stem>.redacted<suffix>` (or extend that
  predicate) or a second run will re-redact them.
- `Backend.is_available()` is memoised for 60s (some adapters probe a network
  service). Use `refresh_availability()` if a test or caller changes the
  environment mid-process.
- **`document.output_path(document, options, suffix=None)` is the only place
  output naming lives.** It yields `<stem>.redacted<suffix>` beside the source,
  or under `-o` with the path relative to `Document.root` mirrored (so equal
  filenames in different folders don't collide). Backends must call it — never
  compute paths themselves, and never `.with_suffix()` an already-suffixed name
  (that's how `.redacted.redacted` happened).
- `Document.root` is set by `iter_documents` (the directory walked, or a glob's
  wildcard-free prefix) and is `None` for a directly loaded file. Preserve it
  when constructing documents in new code paths.
- **Office formats go through `builtin.redact_office_document`** — the shared
  entry point both the builtin engine and Presidio use, dispatching to
  `docx.redact_docx` or `xlsx.redact_xlsx` with `detect`/`replace` callbacks. A
  new text backend gets Word *and* Excel support by calling it with its own
  `detect`; never reimplement the orchestration. (`redact_docx_document` remains
  as an alias.)
- **Format-agnostic OPC logic lives in `opc.py`**, not in `docx.py`/`xlsx.py`:
  parse/serialize, `rewrite_pieces` (split runs), image policy, rels, package
  writing. Put anything both formats need there.
- **Excel-specific hazards** (see `xlsx.py`): text is deduplicated into
  `sharedStrings.xml` so one edit covers many cells — findings are located by
  walking sheets; and a cached formula result must have its *formula removed*,
  because rewriting only `<v>` is undone the moment Excel recalculates.
- **`docx.py` serialization is fragile by nature.** Keep the root-tag
  preservation *and* the default-namespace registration in `_parse`: ElementTree
  drops unused `xmlns:` declarations (Word then rejects the file over
  `mc:Ignorable`) and mangles default namespaces into `ns0:` (which produces
  mismatched tags in `.rels` and `[Content_Types].xml`). Both are regression-tested.
- **Hidden text is in scope.** Tracked deletions, field codes, authors, `.rels`
  targets and images are redacted, not just visible runs — that is the whole
  point of document redaction. Findings outside the visible flow are reported
  with `start`/`end` of `None`; visible offsets index the *source* text, so
  `extract_text(doc)[e.start:e.end] == e.text` holds.

## Adding a new backend

1. Subclass `redact.backends.base.Backend`; set `name`, `description`,
   `supported_media_types`, `priority`.
2. Implement `missing_dependencies()` (cheap, no raise) and `redact()`.
3. Add it to `DEFAULT_BACKENDS` in `src/redact/backends/__init__.py`.
4. Add tests mirroring `tests/test_backends_availability.py` patterns.

## Notes for future sessions

- **Presidio supplies detection only, for every media type.** The rewrite uses
  the suite's own operators, so modes behave identically across backends. There
  is deliberately no separate text path: an earlier version let Presidio's
  `AnonymizerEngine` handle plain text, which bypassed the deterministic union
  below and shipped a `.txt` file with the SSN still in it while reporting
  success. One `detect` function, one rewrite path.
- **`presidio._analyze` unions the builtin recognizers in.** Presidio is a model
  and misses things a regex does not — with `en_core_web_sm` it returned
  *nothing* for `SSN\t123-45-6789`. Since Presidio outranks builtin in the
  router, installing it would otherwise find *less*. A redaction tool must never
  detect less because you installed something better; keep the union.
- **Every rewrite path resolves overlapping spans** (`opc.resolve_overlaps`).
  Presidio reports one email as an EMAIL_ADDRESS *and* two URLs inside it;
  rewriting naively interleaves them into `<EMAIL_ADDRESS><URL>e@<URL>`.
- **Deterministic labels win exact span collisions** in `_analyze`. NER labels
  are model-dependent in both directions — `en_core_web_lg` calls
  `maria.g@clinic.example` a PERSON while `en_core_web_sm` correctly calls it an
  EMAIL_ADDRESS — so a bigger model is not simply better. Both redact the span;
  pinning the label to the verified regex keeps reports and `-e` filtering
  stable across model choices.
- Presidio is not installed in CI. `tests/test_presidio_docx.py` injects fake
  `presidio_*` modules (with a `__spec__`, or `find_spec` won't see them) so the
  real adapter code is exercised; the fake detects `PERSON`, a label the builtin
  engine cannot produce, which is how those tests prove Presidio drove the run.
- **License plates go through the `yolo` backend**, which is open-vocabulary:
  its "classes" are text prompts, so plates need no fine-tuning. Each prompt
  gets its own entity label via `yolo.entity_label` — never fold a prompt into
  `FACE`.
- **No stock YOLO checkpoint has a license-plate class** — YOLO26 included; they
  are all COCO's 80 classes. `yolo._load_model` therefore *raises* when a
  requested class is not in a closed-vocabulary model's list, rather than
  returning zero detections that would read as a clean run. Preserve that: a
  redaction tool reporting "nothing found" when it structurally cannot find the
  thing is the worst possible failure mode.
- **CLIP scores must stay calibrated.** Raw cosine similarity is not comparable
   across images: measured here, random noise beat a real photo of footballers
   for the query "a football player" (0.2099 vs 0.1972). `SemanticIndex.query`
   therefore returns the query's softmax share against `BACKGROUND_PROMPTS`
   (noise drops to 0.0001). Do not "simplify" this back to a bare dot product,
   and keep `calibrate=False` available for callers who want the raw number.
- `filter_documents` (behind `run --match`) passes non-visual documents through
  untouched — a visual filter silently dropping the text files in a mixed folder
  would be a data-loss bug, not a feature.
- Semantic tests use a fake 3-d embedder so CI needs neither torch nor weights;
  the real-CLIP test is gated behind `REDACT_TEST_CLIP=1`.
- Real-model YOLO tests download weights and are gated behind
  `REDACT_TEST_YOLO_WEIGHTS=1`; the rest stub `_load_model`/`_detect` so CI stays
  fast and offline. **The gated tests are not optional** — `.github/workflows/heavy.yml`
  runs them weekly with the env vars set, so an upstream break in
  Ultralytics/CLIP/Presidio surfaces in CI rather than in a user's hands.
- Installing Presidio here needs `pip install --ignore-installed PyYAML ...`:
  pip cannot uninstall the Debian-provided PyYAML and aborts the whole install
  otherwise. Note that `pip ... | tail` hides this, because the pipeline's exit
  status is `tail`'s — check the log, not the exit code.
- **Checkpoints must never land in the user's CWD.** Ultralytics downloads a bare
  name (`yolo26x.pt`) into the working directory; `yolo.resolve_weights` turns
  bare names into paths under `~/.cache/redact-suite/yolo`
  (`REDACT_YOLO_WEIGHTS_DIR` overrides) so a full path is passed instead.
  Explicit paths are honoured untouched. `.gitignore` also excludes `*.pt`,
  `*.onnx` and `weights/` — a push of committed weights is rejected by the
  repo's pre-receive hook.
- `deface` drives the library's Python API, not its console script: `python -m
  deface` does not work (no `__main__`), and the API additionally yields per-face
  boxes, which is what populates `Entity.bbox`.
- Person-name / free-text NER is **Presidio's** job, not the builtin engine —
  the builtin engine only catches pattern-based PII (email, phone, SSN, card w/
  Luhn, IBAN, IP, URL). Don't "fix" the builtin engine to chase names; install
  Presidio instead.
- Default branch is `main`. (A duplicate branch
  `claude/pii-redaction-tools-13vhcw` may still exist — it's an identical copy of
  `main` and safe to delete.)
- Never commit redaction outputs; `.gitignore` already excludes
  `*.redacted.*` and `*-final.pdf`.
- **Video continuity is bounded and honest.** The YOLO video path bridges
  detector misses of up to `temporal_gap` frames (default 2) with
  interpolated/propagated masks — false-positive biased — and *refuses* to
  invent longer trajectories. A re-detection near a recently expired track
  instead records an `unresolved_gaps` entry (the masked→exposed→masked
  signature) which surfaces as a sidecar warning and
  `verification_status: passed_with_warnings`. Do not "fix" a warning by
  widening `temporal_gap` past jitter scale (the backend clamps it to 0–10
  anyway); a long gap is information for a human, not something to mask over.
  Known limits — a clean report means "no *detected* exposure signature",
  never "no exposure": invisible are a subject never re-detected, a
  re-detection beyond `gap_report_window` frames (frame-based, default 30,
  tunable via `extra`), one stolen by association to another live same-label
  track, and one re-detected under a different label.
- **The verification sidecar is an internal audit record — never deliver it
  with the redacted artifact.** It names the source path and, when warnings
  are present, the exact frames where a subject may be exposed.
- **Every YOLO video export is reopened and verified** (full decode, frame
  count) before the result is called a success — the other video backends
  (deface, anonymizer) do not verify yet; wire a new video path through
  `verification/` rather than duplicating it. A failed verification removes
  the unusable output but keeps the sidecar as the audit record. The sidecar name
  derives from the output (`<output>.verification.json`), so it contains
  `.redacted.` and ingestion idempotence holds — regression-tested.
