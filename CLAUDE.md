# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`redact-suite` is a **unified PII/PHI redaction suite**. It ingests any document
(text, structured data, Word .docx, Excel .xlsx, PowerPoint .pptx, email .eml,
PDF, image, video), detects its media type, and routes
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
| `src/redact/opc.py` | Shared Office Open XML plumbing for `.docx`/`.xlsx`/`.pptx`: namespace-safe parse/serialize, split-run rewriting, image policy, rels retargeting, package writing. |
| `src/redact/xlsx.py` | Excel: shared strings (deduped, located by cell), rich-text runs, cached formula results, comments, headers/footers, drawings. |
| `src/redact/docx.py` | Stdlib `.docx` support: paragraph-level detection mapped back onto `<w:t>` runs; also tracked deletions, field codes, revision/comment authors, docProps, `.rels` hyperlink targets and embedded images. Word-safe XML round-trip. |
| `src/redact/pptx.py` | Stdlib `.pptx`/`.pptm` support: slide shapes, **speaker notes**, layouts/masters, comments + authors, chart value caches, docProps, media. |
| `src/redact/eml.py` | Stdlib `.eml`/`.mbox` support: headers, transfer-decoded text/HTML parts, forwarded `message/rfc822` parts, and an explicit policy for binary attachments. |
| `src/redact/backends/base.py` | `Backend` ABC — the adapter contract. |
| `src/redact/backends/builtin.py` | Offline regex/rule engine. Also exports reusable `detect_entities` / `apply_redactions`. Always available. |
| `src/redact/backends/presidio.py` | Microsoft Presidio (text/structured), optional import. |
| `src/redact/backends/philter.py` | Philter service over HTTP (stdlib urllib). |
| `src/redact/backends/redactai.py` | RedactAI-style contextual PDF redaction via local Ollama. |
| `src/redact/backends/pymupdf.py` | True in-place PDF redaction: per-page detect, `search_for` to locate, `apply_redactions` to remove, metadata scrubbed. Declares scanned pages and unlocatable entities as `unredacted`. |
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
| `src/redact/verify.py` | `redact verify` — reopens a finished artifact, decodes every layer (zip parts, base64, PDF text), and re-scans for PII. Independent of the backend that wrote it. |
| `src/redact/cli.py` | `redact` CLI: `list` / `detect` / `run` / `verify` / `search`. |
| `tests/` | pytest suite — ingestion, detection, routing, builtin, CLI, discovery, and audit regressions. |
| `.github/workflows/ci.yml` | Fast CI: pytest on 3.9/3.11/3.12 + CLI smoke test. |
| `.github/workflows/heavy.yml` | Weekly/dispatch CI that actually downloads models: runs the `REDACT_TEST_YOLO_WEIGHTS`/`REDACT_TEST_CLIP` gated tests, and proves the real Presidio library reports itself available. |

## Backends

| Name | Media types | Priority | Requires |
|---|---|---|---|
| `builtin` | text, structured, docx, xlsx, pptx, eml | 10 | nothing (stdlib) |
| `presidio` | text, structured, docx, xlsx, pptx, eml | 80 | `presidio-analyzer` + a spaCy model (`en_core_web_lg`). **Not** `presidio-anonymizer`: this suite uses Presidio for detection only and does its own rewriting, so requiring it would be a lie — see the note below and the `[presidio]` extra. |
| `philter` | text, structured | 70 | running Philter service (`PHILTER_ENDPOINT`) |
| `pymupdf` | pdf | 75 | `pymupdf`. **The only backend that truly redacts a PDF while keeping it a document** — `apply_redactions` removes text from the content stream. AGPL-3.0 (like ultralytics), opt-in. |
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
redact run msg.eml --eml-attachments strip     # drop binary attachments we cannot redact
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
- **`exit 0` must mean the output is safe.** `RedactionResult.unredacted` lists
  content a run knowingly left behind (today: kept binary email attachments) and
  `fully_redacted` folds that into `success`; the CLI exits non-zero for it,
  separately from outright failures. A backend that cannot redact part of a
  document must populate `unredacted` rather than report a clean success —
  false assurance is worse than non-coverage, because it defeats the user's own
  verification of the output.
- Keep the CLI's verbs (`list`/`detect`/`run`/`verify`/`search`) thin — logic belongs in the
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
  `docx.redact_docx`, `xlsx.redact_xlsx`, `pptx.redact_pptx` or `eml.redact_eml`
  with `detect`/`replace` callbacks. A new text backend gets Word, Excel,
  PowerPoint *and* email support by calling it with its own `detect`; never
  reimplement the orchestration. (`redact_docx_document` remains
  as an alias.)
- **Format-agnostic OPC logic lives in `opc.py`**, not in `docx.py`/`xlsx.py`:
  parse/serialize, `rewrite_pieces` (split runs), image policy, rels, package
  writing. Put anything both formats need there.
- **Excel-specific hazards** (see `xlsx.py`): text is deduplicated into
  `sharedStrings.xml` so one edit covers many cells — findings are located by
  walking sheets; and a cached formula result must have its *formula removed*,
  because rewriting only `<v>` is undone the moment Excel recalculates.
- **PowerPoint-specific hazards** (see `pptx.py`): **speaker notes** are a
  separate part (`notesSlides/`) and are the most-forgotten leak in a shared
  deck; layouts and masters carry text onto every slide; and a chart keeps its
  own cache of the source data in `c:v`. A deck has no linear text flow, so
  findings carry `start`/`end` of `None` and name their part instead.
- **Email-specific hazards** (see `eml.py`): parts are transfer-encoded, so
  `_part_text` decodes before scanning and `_set_part_text` re-encodes after —
  the stored bytes really change. `_walk` descends into `message/rfc822` so a
  forwarded thread is redacted too. An `.mbox` is split on `From ` lines before
  parsing — handing a mailbox to a single-message parser folds messages 2..N
  into message 1's *body*, which still redacts the text but destroys the archive
  while reporting a clean run. Binary attachments cannot be redacted in place: they go to `unredacted` (warning + non-zero exit) unless
  `--eml-attachments strip` replaces the payload.
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
   `supported_media_types`, `priority`, and — unless it is always available —
   `install_hint`, the exact command that makes it work. `redact list` prints it
   under the "needs:" line; a test asserts every optional backend has one.
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
- **Deterministic labels win *exact* span collisions** in `_analyze` — a
  validated regex match is a fact, a model label is a guess. Know the limit:
  the rule keys on span equality, so it does nothing when a model span strictly
  *contains* a regex span. Re-measured against the real library (an earlier note
  here claimed `en_core_web_sm` returns EMAIL_ADDRESS for
  `maria.g@clinic.example` and only `en_core_web_lg` returns PERSON — that is
  **false**): for "Email maria.g@clinic.example today" *both* models return
  PERSON over (0,28), covering the address plus the literal word "Email", and
  neither ever emits EMAIL_ADDRESS. Longest-wins therefore keeps PERSON. The
  address is still redacted — over-redaction is the safe direction — but the
  reported label depends on surrounding context. `-e` is unaffected: both
  detectors are filtered by `options.entities` before the collision. Pinned by
  `tests/test_presidio_real.py`; don't restate model behaviour here without
  re-measuring.
- **Real-Presidio behaviour is pinned by `tests/test_presidio_real.py`**, gated
  behind `REDACT_TEST_PRESIDIO=1` and run by `heavy.yml`. The fakes prove the
  adapter's plumbing; only these prove the *model* claims this design rests on —
  that Presidio misses `SSN\t123-45-6789` (verified for both `sm` and `lg`, so
  the union is genuinely load-bearing) and that it finds PERSON names the regex
  engine structurally cannot. Those claims were one-off measurements that had
  never been re-run, and one had drifted. Measurements in comments rot; put them
  in a test.
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
- **`hash` mode is a keyed HMAC, and must stay keyed.** It was an unsalted
  `sha256(value)[:12]`, which is not redaction: PII spaces are tiny (an SSN is
  10^9 values), so enumerating the space and matching digests recovers the
  original in seconds regardless of truncation. The key comes from
  `RedactionOptions.hash_material()` — the explicit `hash_key` when set, else a
  random per-run key generated by `_run_key` (`init=False`, so it cannot be
  pinned by accident and runs stay unlinkable). Never "simplify" this back to a
  plain digest, and never derive the key from the data.
- **`MASK_WIDTH` is fixed at 8 on purpose.** `mask_char * len(original)`
  reproduces the length of what it removed, distinguishing a 7- from a 10-digit
  number. Four tests asserted that old behaviour; one more passed only because
  `len("new text") == 8`. Assert against `MASK_WIDTH`, never a literal length.
- **`hash` is pseudonymisation, not anonymisation.** Equal values map to equal
  tokens by design, so token frequency mirrors value frequency. That is a real
  leak and an intended feature — say so rather than implying `hash` is the
  safest mode. `replace`/`redact` leak neither length nor linkage.
- **`redactai` does not redact a PDF, and says so.** It has no PDF writer, so for
  a PDF it detects in the text layer and writes a redacted `.txt` *extract*
  while the source PDF stays untouched. It therefore appends the PDF to
  `result.unredacted`, which makes the CLI warn and exit non-zero. It used to
  return a bare success, so `redact run report.pdf -b redactai` exited 0 with
  every entity still in the PDF the user holds. A `.txt` input is unaffected —
  there the written file really is the redacted artifact. `pdf-redact-tools` is
  the only backend that redacts a PDF itself (by flattening it to images).
  Black rectangles drawn over text are not redaction: the characters remain in
  the content stream. Do not add a "PDF redaction" path that only draws boxes.
- **`pymupdf` is the PDF path that actually works.** `apply_redactions` removes
  content from the page's content stream; verified by asserting the value is
  absent from the **raw bytes**, not from the rendering. Never replace it with
  drawn rectangles — the characters survive underneath. Two cases must stay in
  `unredacted` or the backend starts lying: a **page with no text layer** (a
  scan — detection finds nothing and "0 entities" would read as clean) and a
  **detected entity `search_for` cannot locate** (ligatures, hyphenation, text
  split across spans). Both are regression-tested in `tests/test_pymupdf.py`.
- **Don't assert on failure *wording* in suite-level tests.** `test_suite.py`
  hard-coded "no pdf backend in CI"; installing one turned a passing test red
  for the wrong reason. Assert the contract — the batch continues, the failure
  comes back as a result with a message and no output path.
- **`verify` must decode before searching, and must not cry wolf.** It scans
  every zip part (a leak in `customXml/` counts, even though nothing edits it),
  every base64 payload and PDF text — the base64 case is the whole reason the
  verb exists, since a user's `grep` cannot see it. Equally important: XML
  namespace URLs are filtered (`_SCHEMA_HOSTS`), because flagging every Office
  document teaches users to ignore the tool, and an ignored verifier is worse
  than none. The filter matches *hosts*, so a real URL leak is still reported.
  The same class bit twice: a PDF's cross-reference table is rows of ten-digit
  zero-padded offsets, which read as phone numbers (two adjacent ones as a
  card), flagging every PDF. `_PDF_XREF` strips exactly that shape from the raw
  layer — never page content. Expect more of these: structural noise that looks
  like PII is the main cost of scanning every layer, and the fix is always a
  narrow structural filter, never loosening detection.
- **Layer order in `extract_layers` is load-bearing.** Specific layers are
  collected first and `raw` last, because a finding is attributed to the first
  layer it appears in; an uncompressed zip entry also appears in the raw bytes,
  and `US_SSN in zip:customXml/item1.xml` is actionable where `in raw` is not.
- **`iter_documents(..., include_outputs=True)` exists for `verify` alone.**
  Ingestion skips `.redacted.` files to keep batches idempotent; verification's
  entire input *is* those files. Never set it in a path that redacts.
- Person-name / free-text NER is **Presidio's** job, not the builtin engine —
  the builtin engine only catches pattern-based PII (email, phone, SSN, card w/
  Luhn, IBAN, IP, URL). Don't "fix" the builtin engine to chase names; install
  Presidio instead.
- Default branch is `main`. (A duplicate branch
  `claude/pii-redaction-tools-13vhcw` may still exist — it's an identical copy of
  `main` and safe to delete.)
- Never commit redaction outputs; `.gitignore` already excludes
  `*.redacted.*` and `*-final.pdf`.
- **The `[all]` extra must list every pip-installable backend.** The README
  promises it does, and a test in `test_backends_availability.py` enforces it —
  it was silently missing `yolo` and `semantic`.
- **Silence is a bug in a CLI.** A mistyped path must not read like an empty
  folder (`unmatched_inputs`), and files skipped as unrecognised are reported
  with a pointer to `--include-unknown` rather than vanishing.
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
