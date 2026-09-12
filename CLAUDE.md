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
| `src/redact/docx.py` | Stdlib `.docx` support: paragraph-level detection mapped back onto `<w:t>` runs; also tracked deletions, field codes, revision/comment authors, docProps, `.rels` hyperlink targets and embedded images. Word-safe XML round-trip. |
| `src/redact/backends/base.py` | `Backend` ABC — the adapter contract. |
| `src/redact/backends/builtin.py` | Offline regex/rule engine. Also exports reusable `detect_entities` / `apply_redactions`. Always available. |
| `src/redact/backends/presidio.py` | Microsoft Presidio (text/structured), optional import. |
| `src/redact/backends/philter.py` | Philter service over HTTP (stdlib urllib). |
| `src/redact/backends/redactai.py` | RedactAI-style contextual PDF redaction via local Ollama. |
| `src/redact/backends/pdf_redact_tools.py` | Shells out to `pdf-redact-tools` CLI. |
| `src/redact/media.py` | Shared ffmpeg discovery (system, else the static `imageio-ffmpeg` build), video fps, and audio muxing. |
| `src/redact/backends/yolo.py` | Ultralytics YOLO: open-vocabulary prompts (YOLO-World) or COCO classes; masks boxes with blur/mosaic/solid. |
| `src/redact/backends/deface.py` | deface face blurring (image/video) via its Python API. Note: `import deface` here resolves to the installed library, not this module (Python 3 absolute imports). |
| `src/redact/backends/anonymizer.py` | understand.ai Anonymizer (image/video), legacy CLI. |
| `src/redact/registry.py` | `BackendRegistry` — holds backend instances, lookups, availability. |
| `src/redact/router.py` | `select_backend` / `candidates` — the selection policy. |
| `src/redact/suite.py` | `RedactionSuite` — high-level entry point + batch. |
| `src/redact/cli.py` | `redact` CLI: `list` / `detect` / `run`. |
| `tests/` | pytest suite — ingestion, detection, routing, builtin, CLI, discovery, and audit regressions. |
| `.github/workflows/ci.yml` | CI: pytest on 3.9/3.11/3.12 + CLI smoke test. |

## Backends

| Name | Media types | Priority | Requires |
|---|---|---|---|
| `builtin` | text, structured, docx | 10 | nothing (stdlib) |
| `presidio` | text, structured, docx | 80 | `presidio-analyzer`, `presidio-anonymizer` + spaCy model |
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
- **`.docx` goes through `docx.redact_docx`** with `detect`/`replace` callbacks,
  driven by `builtin.redact_docx_document` — the shared entry point both the
  builtin engine and Presidio use. A new text backend gets Word support by
  calling it with its own `detect`; never reimplement the orchestration.
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

- Presidio drives `.docx` for *detection*; the rewrite uses the suite's own
  `replacement_for`, because run-level editing needs a string per entity rather
  than one anonymized blob. Modes therefore behave identically across backends.
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
- Real-model YOLO tests download weights and are gated behind
  `REDACT_TEST_YOLO_WEIGHTS=1`; the rest stub `_load_model`/`_detect` so CI stays
  fast and offline.
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
