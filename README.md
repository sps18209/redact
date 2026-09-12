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
| **presidio** | text, structured, docx | [Microsoft Presidio](https://microsoft.github.io/presidio/) — NLP + rules; detects names/locations too. | `pip install "redact-suite[presidio]"` + a spaCy model |
| **philter** | text, structured | [Philter](https://philterd.ai/) self-hosted PII/PHI service (healthcare/legal/finance). | a running Philter service (`PHILTER_ENDPOINT`) |
| **redactai** | pdf, text | [RedactAI](https://github.com/AtharvSabde/RedactAI)-style contextual redaction via local Ollama models. | `pip install "redact-suite[pdf]"` + a running Ollama |
| **pdf-redact-tools** | pdf | Flattens PDFs to images, stripping the text layer & hidden metadata. | `pdf-redact-tools` on `PATH` |
| **deface** | image, video | [deface](https://github.com/ORB-HD/deface) — CNN face blurring. Model ships in the wheel, so detection is fully offline; video needs no system ffmpeg. **Faces only.** | `pip install "redact-suite[deface]"` |
| **anonymizer** | image, video | [understand.ai Anonymizer](https://github.com/understand-ai/anonymizer) — faces **and license plates**. ⚠️ Unmaintained since 2019 and pins `tensorflow-gpu==1.11.0` (Python ≤3.6), so it will not install on a current interpreter. | a git checkout (`ANONYMIZER_HOME`) or compatible CLI (`ANONYMIZER_BIN`) |

Backends report their own availability, so `redact list` always tells you what
can run right now and exactly what each missing one needs.

## Install

```bash
pip install -e .                 # core suite (builtin backend only)
pip install -e ".[presidio]"     # add Presidio
pip install -e ".[all]"          # add every pip-installable backend
```

### Faces and license plates

For **faces** in images and video, install `deface` — it is one pip command, its
CenterFace model ships inside the wheel (no download, works air-gapped), and it
brings a static ffmpeg so video needs nothing from the system:

```bash
pip install "redact-suite[deface]"
redact run ./footage -o ./clean          # auto-routes images and video to deface
```

**License plates are not covered by deface.** The only backend here that blurs
plates is understand.ai's Anonymizer, which is unmaintained and pins
`tensorflow-gpu==1.11.0` — it cannot be installed on Python 3.7+. It is kept
wired up for anyone who can run it (an old interpreter, a container, or any
CLI exposing the same interface via `ANONYMIZER_BIN`), but on a modern install
plate blurring is an open gap rather than something the suite quietly pretends
to handle:

```bash
git clone https://github.com/understand-ai/anonymizer     # needs Python <=3.6
export ANONYMIZER_HOME=$PWD/anonymizer
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
back with formatting and layout intact, not a text dump. Word often splits one
word across several runs (after spell-check or formatting), so detection runs on
each paragraph's full text and the placeholder is written into the run where the
match started.

Crucially, redaction covers content that is **invisible on screen but still
shipped in the file** — the usual way a "redacted" document leaks:

| Hiding place | What lives there |
|---|---|
| Tracked deletions (`w:delText`) | text someone deleted with Track Changes on; survives *reject all changes* |
| Field codes (`w:instrText`, `w:fldSimple`) | e.g. `HYPERLINK "mailto:jane@example.com"` |
| Revision & comment authors | `w:author` / `w:initials` on every edit and comment |
| Document properties | `dc:creator`, `cp:lastModifiedBy`, `Manager` |
| Relationship targets | a `mailto:` address lives in `.rels`, not the body |
| Embedded images | see `--docx-images` below |

Body, headers, footers, footnotes, endnotes and comments are all processed.
Author names are reported as `DOCUMENT_AUTHOR` (pass `-e` to opt out). Findings
in non-visible content are reported without offsets, since they have no position
in the rendered page.

Embedded images are governed by `--docx-images`:

```bash
redact run memo.docx --docx-images keep    # default: leave them, report the count
redact run memo.docx --docx-images strip   # replace each with a blank PNG
redact run memo.docx --docx-images blur    # blur faces/plates via an image backend
```

`strip` renames parts to `.png` and rewrites the referencing relationships and
content types, so the document stays valid. `blur` routes each image through the
best available image backend (Anonymizer); any image it declines is stripped
instead, so the policy never silently leaves data behind.

Stdlib only — no `python-docx` needed. Both the builtin engine and Presidio can
drive Word redaction; with Presidio installed it wins on priority and you get
name/location detection inside `.docx` too.

### `run` options

| Flag | Meaning |
|---|---|
| `-b, --backend` | backend name, or `auto` (default) to let the suite choose |
| `-m, --mode` | `replace` (default), `mask`, `hash`, `redact`, `blur` |
| `-e, --entities` | restrict detection to these labels (comma-separated or repeated) |
| `-o, --out` | output directory (default: alongside each source). The source tree is mirrored beneath it, so `inbox/a/x.txt` and `inbox/b/x.txt` never collide. |
| `--threshold` | minimum confidence to act on a detection (default `0.35`) |
| `--dry-run` | detect and report only |
| `--docx-images` | embedded images in a `.docx`: `keep` (default), `strip`, `blur` |
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
