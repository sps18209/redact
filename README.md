# redact-suite

A unified **PII/PHI redaction suite**. Point it at *any* document — text, CSV/JSON,
Word (.docx), Excel (.xlsx), PowerPoint (.pptx), email (.eml/.mbox), PDF, image, or video — and it routes each file to the best available redaction
tool, or one you pick by hand. A dependency-free rule engine ships built in, so
the suite works out of the box and every heavy tool is opt-in.

```
┌──────────┐   ingest      ┌────────┐   choose        ┌──────────────────────────────┐
│  inputs  │ ────────────▶ │ router │ ──────────────▶ │  backend (the right tool)    │
│ files /  │  detect type  │ picks  │  by media type  │  presidio · philter ·        │
│ dirs /   │               │ a tool │  + availability │  redactai · pdf-redact-tools │
│ globs    │               └────────┘  + priority     │  · deface · builtin          │
└──────────┘                                          └──────────────────────────────┘
```

## Why

The redaction ecosystem is fragmented: Presidio is great for text, deface blurs
faces in video, pdf-redact-tools sanitises PDFs, and so on.
`redact-suite` is the layer on top: one ingestion path, one selection policy,
one result shape — so you can throw a mixed folder at it and let it dispatch
each document to the tool that fits.

## Supported backends

| Backend | Handles | What it does | Requires |
|---|---|---|---|
| **builtin** | text, structured, docx, xlsx, pptx, email | Offline regex/rule engine (email, phone, SSN, card w/ Luhn, IBAN, IP, URL). Redacts Office documents and email in place, formatting intact. Always available. | nothing (stdlib) |
| **presidio** | text, structured, docx, xlsx, pptx, email | [Microsoft Presidio](https://microsoft.github.io/presidio/) — NLP + rules; detects names/locations too. | `pip install "redact-suite[presidio]"` + a spaCy model |
| **philter** | text, structured | [Philter](https://philterd.ai/) self-hosted PII/PHI service (healthcare/legal/finance). | a running Philter service (`PHILTER_ENDPOINT`) |
| **pymupdf** | pdf | **True in-place PDF redaction** — removes text from the content stream (not a box drawn over it) and scrubs metadata. One pip install, no system dependencies. AGPL-3.0, opt-in. | `pip install "redact-suite[pymupdf]"` |
| **redactai** | pdf, text | [RedactAI](https://github.com/AtharvSabde/RedactAI)-style contextual detection via local Ollama models. Redacts `.txt` outright; for a **PDF it writes a redacted text extract and leaves the PDF itself untouched**, reporting it as unredacted (non-zero exit). | `pip install "redact-suite[pdf]"` + a running Ollama |
| **pdf-redact-tools** | pdf | Flattens PDFs to images, stripping the text layer & hidden metadata. | `pdf-redact-tools` on `PATH` |
| **yolo** | image, video | [Ultralytics YOLO](https://docs.ultralytics.com/) — **open-vocabulary** masking from text prompts, so **license plates** (and anything else you can name) are covered. | `pip install "redact-suite[yolo]"` |
| **deface** | image, video | [deface](https://github.com/ORB-HD/deface) — CNN face blurring. Model ships in the wheel, so detection is fully offline; video needs no system ffmpeg. **Faces only.** | `pip install "redact-suite[deface]"` |
| **anonymizer** | image, video | [understand.ai Anonymizer](https://github.com/understand-ai/anonymizer) — faces **and license plates**. ⚠️ Unmaintained since 2019 and pins `tensorflow-gpu==1.11.0` (Python ≤3.6), so it will not install on a current interpreter. | a git checkout (`ANONYMIZER_HOME`) or compatible CLI (`ANONYMIZER_BIN`) |

Backends report their own availability, so `redact list` always tells you what
can run right now and exactly what each missing one needs.

## Install

```bash
pip install -e .                 # core suite (builtin backend only, zero deps)
pip install -e ".[presidio]"     # add Presidio       (names, locations)
pip install -e ".[deface]"       # add deface         (faces in images/video)
pip install -e ".[yolo]"         # add YOLO           (plates, prompt-driven)
pip install -e ".[semantic]"     # add CLIP search
pip install -e ".[all]"          # every pip-installable backend
```

Not sure what you have? `redact list` shows every backend, whether it can run
right now, what it is missing, and the exact command to fix it:

```
BACKEND            AVAIL  PRIO  MEDIA TYPES         DESCRIPTION
builtin            yes    10    text,structured,…   Dependency-free regex/rule engine…
redactai           no     60    pdf,text            RedactAI-style contextual PDF redaction…
                   ├─ needs: pypdf, ollama server at http://localhost:11434 (unreachable)
                   └─ pip install "redact-suite[pdf]" and run a local Ollama server
```

### Faces and license plates

For **faces** in images and video, install `deface` — it is one pip command, its
CenterFace model ships inside the wheel (no download, works air-gapped), and it
brings a static ffmpeg so video needs nothing from the system:

```bash
pip install "redact-suite[deface]"
redact run ./footage -o ./clean          # auto-routes images and video to deface
```

**deface does not cover license plates.** For those — and for anything else you
can describe — use the `yolo` backend:

```bash
pip install "redact-suite[yolo]"
redact run ./footage -b yolo                       # plates + faces, the defaults
redact run ./footage -b yolo --yolo-classes "license plate,ID card,tattoo"
```

It runs an **open-vocabulary** YOLO-World model, so its classes are *text
prompts*: name the thing and it is detected and masked, with no fine-tuning and
no fixed class list. Each prompt gets its own entity label (`LICENSE_PLATE`,
`ID_CARD`), never folded into `FACE`.

> **The trap this avoids:** every stock YOLO checkpoint — **YOLO26 included** —
> is COCO-trained with 80 classes, and *none of them is a license plate*. Asking
> `yolo26x.pt` for plates would quietly find nothing and report a clean run. This
> backend refuses instead, telling you what the checkpoint actually knows and
> pointing at an open-vocabulary model. Closed-vocabulary checkpoints are still
> useful for classes they do have:
>
> ```bash
> redact run ./photos -b yolo --yolo-model yolo26x.pt --yolo-classes person
> ```

`yolo` sits *below* `deface` in priority on purpose: deface is a purpose-built
face detector and is better at faces, so `auto` will not silently swap a
specialist for a generalist. Ask for `-b yolo` when you need plates.

The legacy `anonymizer` backend (faces *and* plates) is still wired up, but it
pins `tensorflow-gpu==1.11.0` and installs only on Python ≤3.6:

```bash
git clone https://github.com/understand-ai/anonymizer     # needs Python <=3.6
export ANONYMIZER_HOME=$PWD/anonymizer
```

## Semantic search

Redaction starts with a question a filename cannot answer: *which* of these ten
thousand frames shows a whiteboard, a badge, a screen full of records? Install
the `semantic` extra and describe it:

```bash
pip install "redact-suite[semantic]"

redact search "a photo of an ID card" ./footage --top 10
redact search "a whiteboard with writing" ./footage --index idx.npz   # cache it
```

Images and sampled video frames are embedded with CLIP into the same space as
English text, so video hits report the moment they occur:

```
3 match(es) for 'a bus on a city street' across 11 embedded frame(s):
  +0.9994  bus.jpg
  +0.2963  clip.mp4 @ 0.6s (frame 3)
  +0.0000  noise.png
```

You can also let a search decide *what gets redacted*:

```bash
redact run ./footage --match "a whiteboard with writing" --match-threshold 0.05
```

Text files and PDFs are always passed through — a visual filter must never
silently drop the documents in a mixed folder.

> **Why the scores are calibrated.** Raw CLIP cosine similarity is meaningful
> only *between texts for one image*, never as an absolute number across images.
> Measured on this corpus, random noise scored **0.2099** against "a football
> player" while a real photo of footballers scored **0.1972** — thresholding raw
> similarity ranks noise above the real thing. Every score above is instead the
> query's softmax share against a set of generic background prompts, which puts
> the footballers on top and drops the noise to **0.0001**. Pass `--raw-scores`
> for the uncalibrated numbers.

## CLI

```bash
redact list                          # show backends and availability
redact search "an ID card" ./inbox   # rank images/video by a description
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
redact run memo.docx --docx-images blur    # blur faces via an image backend
```

`strip` renames parts to `.png` and rewrites the referencing relationships and
content types, so the document stays valid. `blur` routes each image through the
highest-priority available image backend (`deface`, if installed); any image it
declines or fails on is stripped instead, so the policy never silently leaves
data behind — and the result message reports the real split, e.g.
`1 embedded image(s) blurred, 1 stripped (could not be processed)`.

Stdlib only — no `python-docx` needed. Both the builtin engine and Presidio can
drive Word redaction; with Presidio installed it wins on priority and you get
name/location detection inside `.docx` too.

### Spreadsheets

`.xlsx`/`.xlsm` workbooks are redacted in place, and Excel's storage model has
traps a naive find-and-replace misses:

| Trap | What the suite does |
|---|---|
| Text is **deduplicated** into `sharedStrings.xml` | Redacting once fixes every referencing cell; the report names them (`US_SSN at People!A2, Notes!C7`) |
| Formatted cells **split runs** (`jane` + `.doe@exa` + `mple.com`) | Detection runs on the joined string, same engine as Word |
| Formula results are **cached** in `<v>` | Rewriting the cache alone is undone by recalculation, so the *formula is removed* and the cell becomes a static redacted string |
| Comments, headers/footers, hyperlink `display`, text boxes, `docProps` | All scanned; comment authors scrubbed |

Numeric cells are deliberately **not** scanned — an SSN stored as the number
`123456789` is indistinguishable from any other identifier without column
context, and guessing there would do more harm than good.

### Presentations

`.pptx`/`.pptm` decks are redacted in place. A deck hides text in more places
than it shows:

| Where | What the suite does |
|---|---|
| Slide shapes and tables | Redacted; split runs joined first, same engine as Word |
| **Speaker notes** | Redacted — the single most-forgotten leak in a shared deck |
| Slide layouts & masters | Scanned; a name typed into a master shows on every slide |
| Comments and their authors | Text redacted, authors scrubbed |
| **Chart value caches** (`c:v`) | Scanned — a chart keeps its own copy of the source data |
| `docProps`, embedded media | Metadata scrubbed; media governed by `--docx-images` |

A deck has no single linear text flow, so findings are reported by part
(`US_SSN in notesSlide2`) rather than by character offset.

### Email

`.eml` and `.mbox` messages are redacted in place and stay valid RFC 5322 —
they still open in a mail client, with all headers present.

| Where | What the suite does |
|---|---|
| Headers (`From`, `To`, `Cc`, `Bcc`, `Subject`, `Received`, `Message-ID`, …) | Redacted |
| Text and HTML parts | Decoded through their transfer encoding, redacted, **re-encoded** |
| Forwarded `message/rfc822` parts | Walked recursively; a quoted thread is redacted too |
| `.mbox` archives | Split into individual messages first (including the envelope-sender `From ` line), redacted one by one, and rejoined — the archive still opens as a mailbox |
| Binary attachments (PDF, image, …) | See below |

**The base64 trap.** A PII-laden attachment is stored base64-encoded, so it is
invisible to any tool that greps the raw file — including the obvious way a user
verifies the output. The suite decodes text parts before scanning so they cannot
hide that way, and for *binary* attachments it refuses to pretend:

```bash
redact run msg.eml                              # default: keep
# WARNING: r.pdf left unredacted (binary attachment)
# echo $? → 1

redact run msg.eml --eml-attachments strip      # replace payload with a notice
# echo $? → 0
```

### Exit codes

`0` means the output is safe. The CLI exits **non-zero** when a run knowingly
left content unredacted (today: kept binary attachments), separately from
`success=False` failures — because `exit 0` from a redaction tool is a promise
automation will act on.

### PDFs

```bash
pip install "redact-suite[pymupdf]"
redact run chart.pdf
```

| Backend | What you actually get |
|---|---|
| **`pymupdf`** *(default for PDF)* | **True redaction.** The text is removed from the page's content stream and the metadata scrubbed. The document stays a document — still selectable, searchable, accessible. |
| `pdf-redact-tools` | Flattens every page to an image. More thorough (it destroys content you never detected) but the result is a picture of a document: no text, no search, no screen reader. An explicit choice, not the automatic one. |
| `redactai` | Detection over the text layer plus a redacted `.txt` *extract*. The PDF is **not** modified, so the run reports content left unredacted and exits non-zero. |

Drawing black rectangles over text is not redaction — the characters stay in the
content stream and any `pdftotext` recovers them. So the test for `pymupdf` is
that the value is gone from the **raw bytes**, not that the page looks right.

**Scanned PDFs.** A scan is an image of text: there is no text layer, so
detection finds nothing and a naive tool reports a spotless run over a document
full of PII. Pages carrying images but no text are reported and the run exits
non-zero:

```
[ok] scanned.pdf via pymupdf (pdf): 0 entities -> scanned.redacted.pdf | WARNING:
page(s) 1 carry images but no text layer — a scan reads as 'nothing found'.
OCR it, or flatten with -b pdf-redact-tools
```

A detected entity whose position can't be resolved on the page (ligatures,
hyphenation, text split across spans) is reported the same way rather than
silently skipped.

### Redaction modes

| Mode | Output | What it guarantees |
|---|---|---|
| `replace` *(default)* | `<US_SSN>` | The value is gone. No linkage, no length, nothing recoverable. |
| `redact` | *(removed)* | As above, without even the type label. |
| `mask` | `********` | The value is gone. **Fixed width** — the mask never reveals how long the original was. |
| `hash` | `<US_SSN:e9e7e9a44c8e>` | **Pseudonymisation, not anonymisation.** Equal values get equal tokens, so records stay correlatable. |
| `blur` | *(pixels)* | Visual modes only (image/video). |

**On `hash`.** The token is a keyed HMAC, not a bare digest. That distinction is
the whole ballgame: PII comes from small spaces — an SSN is 10⁹ values, a phone
~10¹⁰ — so an unkeyed `sha256(value)` is invertible by simply enumerating the
space and comparing digests, in seconds, no matter how far the digest is
truncated. A key makes the candidate set uncomputable.

```bash
redact run notes.txt -m hash                     # random per-run key: unlinkable between runs
redact run ./inbox  -m hash --hash-key "$KEY"    # same key = same pseudonym, on purpose
```

Two limits worth stating plainly:

- **The key is a credential.** Anyone holding it can run the enumeration above
  and invert every token. Don't publish it alongside the output.
- **Equality survives by design** — that is what a pseudonym is *for* — so token
  frequency mirrors value frequency. If you don't need linkage, use `replace`
  or `redact`, which leak neither.

### `run` options

| Flag | Meaning |
|---|---|
| `-b, --backend` | backend name, or `auto` (default) to let the suite choose |
| `-m, --mode` | `replace` (default), `mask`, `hash`, `redact`, `blur` — see [Redaction modes](#redaction-modes) |
| `--hash-key` | key for `-m hash`; same key = same pseudonym across runs (omit for a random per-run key) |
| `-e, --entities` | restrict detection to these labels (comma-separated or repeated) |
| `-o, --out` | output directory (default: alongside each source). The source tree is mirrored beneath it, so `inbox/a/x.txt` and `inbox/b/x.txt` never collide. |
| `--threshold` | minimum confidence to act on a detection (default `0.35`) |
| `--dry-run` | detect and report only |
| `--yolo-model` | checkpoint for the `yolo` backend (default open-vocabulary `yolov8s-worldv2.pt`) |
| `--yolo-classes` | text prompts for the `yolo` backend, e.g. `"license plate,ID card"` |
| `--docx-images` | embedded images in a `.docx`/`.pptx`: `keep` (default), `strip`, `blur` |
| `--eml-attachments` | binary attachments in an email: `keep` (default, warns and exits non-zero), `strip` |
| `--match` | only redact visual files matching this description |
| `--match-threshold` | calibrated score a `--match` must reach (0-1, default `0.05`) |
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
