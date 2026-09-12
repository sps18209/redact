# redact-suite

A unified **PII/PHI redaction suite**. Point it at *any* document — text, CSV/JSON,
Word (.docx), PDF, image, or video — and it routes each file to the best available redaction
tool, or one you pick by hand. A dependency-free rule engine ships built in, so
the suite works out of the box and every heavy tool is opt-in.

```
┌──────────┐   ingest      ┌────────┐   choose        ┌──────────────────────────────┐
│  inputs  │ ────────────▶ │ router │ ──────────────▶ │  backend (the right tool)    │
│ files /  │  detect type  │ picks  │  by media type  │  presidio · philter ·        │
│ dirs /   │               │ a tool │  + availability │  redactai · pdf-redact-tools │
│ globs    │               └────────┘  + priority     │  · anonymizer · builtin      │
└──────────┘                                          └──────────────────────────────┘
```

## Why

The redaction ecosystem is fragmented: Presidio is great for text, Anonymizer
handles faces and license plates, pdf-redact-tools sanitises PDFs, and so on.
`redact-suite` is the layer on top: one ingestion path, one selection policy,
one result shape — so you can throw a mixed folder at it and let it dispatch
each document to the tool that fits.

## Supported backends

| Backend | Handles | What it does | Requires |
|---|---|---|---|
| **builtin** | text, structured, docx | Offline regex/rule engine (email, phone, SSN, card w/ Luhn, IBAN, IP, URL). Redacts Word documents in place, formatting intact. Always available. | nothing (stdlib) |
| **presidio** | text, structured | [Microsoft Presidio](https://microsoft.github.io/presidio/) — NLP + rules; detects names/locations too. | `pip install "redact-suite[presidio]"` + a spaCy model |
| **philter** | text, structured | [Philter](https://philterd.ai/) self-hosted PII/PHI service (healthcare/legal/finance). | a running Philter service (`PHILTER_ENDPOINT`) |
| **redactai** | pdf, text | [RedactAI](https://github.com/AtharvSabde/RedactAI)-style contextual redaction via local Ollama models. | `pip install "redact-suite[pdf]"` + a running Ollama |
| **pdf-redact-tools** | pdf | Flattens PDFs to images, stripping the text layer & hidden metadata. | `pdf-redact-tools` on `PATH` |
| **anonymizer** | image, video | [understand.ai Anonymizer](https://github.com/understand-ai/anonymizer) — blurs faces & license plates. Video via ffmpeg frame extraction. | a git checkout (`ANONYMIZER_HOME`) or compatible CLI (`ANONYMIZER_BIN`); `ffmpeg` for video |

Backends report their own availability, so `redact list` always tells you what
can run right now and exactly what each missing one needs.

## Install

```bash
pip install -e .                 # core suite (builtin backend only)
pip install -e ".[presidio]"     # add Presidio
pip install -e ".[all]"          # add every pip-installable backend
```

Anonymizer is not on PyPI (the PyPI `anonymizer` project is unrelated):

```bash
git clone https://github.com/understand-ai/anonymizer
pip install -r anonymizer/requirements.txt
export ANONYMIZER_HOME=$PWD/anonymizer   # weights auto-download on first run
```

## CLI

```bash
redact list                          # show backends and availability
redact detect ./inbox                # show detected media type per file
redact run report.pdf                # auto-route one file
redact run ./inbox -o ./clean        # ingest a whole folder, write to ./clean
redact run notes.txt -b presidio     # force a specific backend
redact run data.csv -m mask          # mask instead of the default <TYPE> placeholder
redact run notes.txt --dry-run       # detect & report, write nothing
redact run notes.txt -e EMAIL_ADDRESS,US_SSN   # only these entity types (or repeat -e)
```

Re-running over the same folder is safe: the suite never re-ingests its own
`*.redacted.*` outputs, and it skips hidden directories such as `.git`.

Outputs are always named `<stem>.redacted<ext>`. With `-o`, each file's path
relative to the folder (or glob prefix) you passed is mirrored under the output
directory: `redact run inbox -o clean` turns `inbox/hr/x.txt` into
`clean/hr/x.redacted.txt`.

### Word documents

`.docx` files are redacted **in place as Word documents** — you get a `.docx`
back with formatting, images and layout intact, not a text dump. Word often
splits one word across several runs (after spell-check or formatting), so
detection runs on each paragraph's full text and the placeholder is written into
the run where the match started. Body, headers, footers, footnotes, endnotes and
comments are all processed, and the author fields in the document properties are
scrubbed (reported as `DOCUMENT_AUTHOR`; pass `-e` to opt out). Text inside
tracked deletions, field codes and embedded images is not touched. Stdlib only —
no `python-docx` needed.

### `run` options

| Flag | Meaning |
|---|---|
| `-b, --backend` | backend name, or `auto` (default) to let the suite choose |
| `-m, --mode` | `replace` (default), `mask`, `hash`, `redact`, `blur` |
| `-e, --entities` | restrict detection to these labels (comma-separated or repeated) |
| `-o, --out` | output directory (default: alongside each source). The source tree is mirrored beneath it, so `inbox/a/x.txt` and `inbox/b/x.txt` never collide. |
| `--threshold` | minimum confidence to act on a detection (default `0.35`) |
| `--dry-run` | detect and report only |
| `--no-recursive` | do not walk directories recursively |

## Library

```python
from redact import RedactionSuite, RedactionOptions
from redact.types import RedactionMode

suite = RedactionSuite()

# one document, auto-routed
result = suite.redact_path("contract.pdf")
print(result.summary())

# a whole folder, masked, written to ./clean
opts = RedactionOptions(mode=RedactionMode.MASK, output_dir="clean")
for res in suite.redact_paths(["./inbox"], opts):
    print(res.summary())
```

## How routing works

1. **Ingest** — every input (file, directory, or glob) is expanded and each file
   gets a `MediaType` from its extension, then magic-byte sniffing.
2. **Choose** — if you named a backend it is used (validated for media type +
   availability); otherwise the router takes the highest-`priority` backend that
   supports the media type *and* is available right now.
3. **Run** — the backend translates the suite's neutral options into a call
   against the real tool and returns a `RedactionResult` (entities found, output
   path, status).

Failures are returned as `success=False` results with a helpful message rather
than raised, so a batch never aborts on one bad file.

## Extending

Add a new tool by subclassing `redact.backends.base.Backend` (declare `name`,
`supported_media_types`, `priority`, implement `missing_dependencies()` and
`redact()`), then register it:

```python
from redact import BackendRegistry, RedactionSuite
reg = BackendRegistry.with_defaults()
reg.register(MyBackend())
suite = RedactionSuite(registry=reg)
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT.
