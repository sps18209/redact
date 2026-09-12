# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`redact-suite` is a **unified PII/PHI redaction suite**. It ingests any document
(text, structured data, PDF, image, video), detects its media type, and routes
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
| `src/redact/document.py` | Ingestion: `MediaType` detection, `load_document`, `iter_documents` (files/dirs/globs). |
| `src/redact/backends/base.py` | `Backend` ABC — the adapter contract. |
| `src/redact/backends/builtin.py` | Offline regex/rule engine. Also exports reusable `detect_entities` / `apply_redactions`. Always available. |
| `src/redact/backends/presidio.py` | Microsoft Presidio (text/structured), optional import. |
| `src/redact/backends/philter.py` | Philter service over HTTP (stdlib urllib). |
| `src/redact/backends/redactai.py` | RedactAI-style contextual PDF redaction via local Ollama. |
| `src/redact/backends/pdf_redact_tools.py` | Shells out to `pdf-redact-tools` CLI. |
| `src/redact/backends/anonymizer.py` | understand.ai Anonymizer (image/video), CLI/package. |
| `src/redact/registry.py` | `BackendRegistry` — holds backend instances, lookups, availability. |
| `src/redact/router.py` | `select_backend` / `candidates` — the selection policy. |
| `src/redact/suite.py` | `RedactionSuite` — high-level entry point + batch. |
| `src/redact/cli.py` | `redact` CLI: `list` / `detect` / `run`. |
| `tests/` | pytest suite — ingestion, detection, routing, builtin, CLI, discovery, and audit regressions. |
| `.github/workflows/ci.yml` | CI: pytest on 3.9/3.11/3.12 + CLI smoke test. |

## Backends

| Name | Media types | Priority | Requires |
|---|---|---|---|
| `builtin` | text, structured | 10 | nothing (stdlib) |
| `presidio` | text, structured | 80 | `presidio-analyzer`, `presidio-anonymizer` + spaCy model |
| `philter` | text, structured | 70 | running Philter service (`PHILTER_ENDPOINT`) |
| `redactai` | pdf, text | 60 | `pypdf` + running Ollama (`OLLAMA_HOST`) |
| `anonymizer` | image, video | 60 | git checkout via `ANONYMIZER_HOME` (or `ANONYMIZER_BIN`); `ffmpeg`+`ffprobe` for video. **Not** the PyPI `anonymizer` package — that's unrelated. |
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
- Output files always go to `<stem>.redacted<suffix>` — never `.with_suffix()`
  on an already-suffixed name (that's how `.redacted.redacted` happened).

## Adding a new backend

1. Subclass `redact.backends.base.Backend`; set `name`, `description`,
   `supported_media_types`, `priority`.
2. Implement `missing_dependencies()` (cheap, no raise) and `redact()`.
3. Add it to `DEFAULT_BACKENDS` in `src/redact/backends/__init__.py`.
4. Add tests mirroring `tests/test_backends_availability.py` patterns.

## Notes for future sessions

- Person-name / free-text NER is **Presidio's** job, not the builtin engine —
  the builtin engine only catches pattern-based PII (email, phone, SSN, card w/
  Luhn, IBAN, IP, URL). Don't "fix" the builtin engine to chase names; install
  Presidio instead.
- Default branch is `main`. (A duplicate branch
  `claude/pii-redaction-tools-13vhcw` may still exist — it's an identical copy of
  `main` and safe to delete.)
- Never commit redaction outputs; `.gitignore` already excludes
  `*.redacted.*` and `*-final.pdf`.
