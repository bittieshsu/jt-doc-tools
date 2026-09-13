[繁體中文](CHANGELOG.md) ｜ **English**

# Change log (English)

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

> **Scope.** Traditional Chinese is this project's primary language, and
> **[CHANGELOG.md](CHANGELOG.md) is the complete history** (767 releases).
> This English file summarises **recent releases** — enough to see what changed
> and decide whether to upgrade. For anything older, read the Chinese file.

---

## [1.15.38] - 2026-09-13

### The same root cause, four times: classes that do nothing where they are used

`class="notice"`, `af-field` / `af-note`, `jt-select`, `btn-secondary` — four
separate places where markup referenced a class that **has no effect in that
context**. `.af-field` is only styled inside `.auth-form`; `jt-select` is a hook
for `custom_select.js` and is inert without that script. Nothing errors, nothing
logs, the element is there — the page just looks unstyled.

A new guard, `tests/test_template_css_is_effective.py`, resolves every class
used in a template against the stylesheets **and against the scope it is used
in**, so a selector that can never match is now a failing test rather than
something only a screenshot would reveal.

### A job-autosave test waited on the wrong thing (**test-only change**)

One test failed at the end of the full suite and passed on its own. Reproduced
by running it alongside the browser tests, which load the machine: the output
file existed, `result_path` resolved, the workspace was enabled — and `meta` was
empty. Not a timeout: the read happened too early.

The finishing order in `_run()` is deliberate — status goes to `done` and the
final state is persisted **before** the autosave copies the file and fills in
`meta`. The test waited for the status and read `meta` immediately, landing in
that window; under load the copy takes long enough to hit it every time. It now
waits for `meta["workspace"]` to appear.

### Windows installer: a successful uninstall reported a non-zero exit code

Found while testing the uninstall → fresh install path on a real machine. The
silent uninstall **succeeded completely** — service, registry entry and install
directory removed, firewall rule gone, **user data and all four SQLite files
preserved** — and still exited with **2**, because NSIS's `Quit` defaults to
"aborted by script" after handing off to the copy in `%TEMP%`. A scripted
uninstall (MDM, `Start-Process -Wait`) would call that a failure. Fixed with an
explicit `SetErrorLevel 0`.

> This release is **not tagged**, so the fix ships with the next installer.

### Both installer paths are now verified

Only the upgrade path had been tested before. The other half is now covered:
uninstall (user data verified intact) → **fresh install** — 131 seconds, exit 0,
signature `Valid` with `CN=SignPath Foundation`, service running and set to
automatic, health check `{"ok":true}`, correct version on the page, and the
**existing user data picked up** by the new installation.

### Buttons in one row now agree on their icons

A user pointed out two buttons in a four-button row had no icon. Fifteen more
rows across the site had the same mix. Both the inconsistency and a second,
invisible variant — a button whose icon is silently wiped because JavaScript
overwrites the whole button with `textContent` — are now guarded by
`tests/test_button_icons_are_consistent.py`.

### English documentation had fallen 34 releases behind

`CHANGELOG_en.md` stopped at 1.15.4 — and the three existing guards (file
exists, no Chinese left, language links point both ways) were **all green** for
a document that was a month out of date. Entries for 1.15.7 through 1.15.38 are
now written, and three new guards make it impossible to repeat: the newest
English entry must match the newest Chinese one, `README_en.md` must carry the
same version as `README.md`, and the English site pages are **regenerated and
compared byte for byte** — a criterion that computes itself rather than relying
on somebody remembering to run the generator.

### Document straighten: layout and loading polish

Before/after images no longer flash a broken-image icon while they are being
rendered; the cards use the same field layout as the rest of the site.

---

## [1.15.37] - 2026-09-13

### Windows installer: upgrading an existing installation always failed

The installer ran `uv venv --clear` against a virtual environment that was still
in use by the running service. It **deleted the environment and then failed**,
leaving a machine that could not start (`ModuleNotFoundError: jinja2`), and a
`MessageBox` without `/SD` meant the silent installer then **waited forever for a
click nobody could give**. The service is now stopped and its handles released
before the environment is touched, and every dialog has a silent-mode default.

Verified end to end on a real Windows machine, upgrading an existing install.

### Automatic page-edge detection failed on both real photos

Tested with actual phone photographs: `approxPolyDP` returned five and six
points, so no quadrilateral was found. Replaced with an Otsu brightness mask,
morphological closing, a convex hull and `minAreaRect`, plus a sanity check that
rejects wildly skewed corner sets.

> Two intermediate attempts scored **perfectly on residual angle** while
> cropping away content or framing the desk instead of the paper. A residual
> angle near zero does not mean the right sheet was found — the output has to be
> looked at.

### Document straighten: drag the four corners, rotate individual pages

Manual mode (phase 2): drag each corner with a magnifier under the cursor,
rotate a single page 90°/180°, reset to the original orientation, and apply a
correction to one page, all pages, or all following pages.

---

## [1.15.36] - 2026-09-13

### The new tool's page was dead JavaScript — and every gate was green

The document-straighten template never loaded its `<script src>` dependencies,
so the page threw `ReferenceError` on load: no drag and drop, no file picker,
nothing. Syntax checks, i18n scans and the API tests were all green, because
none of them **opens the page**.

Two new gates close that hole for every current and future tool:

* `tests/test_pages_boot_in_a_browser.py` — loads every page in a real browser
  and fails on any console error or CSP violation. It found a second instance of
  the same bug by itself.
* `tests/test_template_script_deps.py` — every global a template uses must be
  provided by a script that template actually includes.

### Fixes

* The sidebar highlighted two tools at once: the match was a prefix match, so
  `/tools/pdf-annotations` also lit up `/tools/pdf-annotations-flatten`.
* `class="notice"` was not defined anywhere in the stylesheets.
* Terminology: `在線` → `線上` in the Traditional Chinese interface.

---

## [1.15.35] - 2026-09-13

### History ids came straight from the URL without a format check

`/history/<id>` passed the id through to the filesystem layer, where a malformed
value produced a 500 instead of a 404. Ids are now validated against their
actual shape (12 hex characters) before anything is opened.

The test plan gained §4.9 for read-only admin endpoints that still return data.

---

## [1.15.34] - 2026-09-13

### Settings files interrupted mid-write turned silently into defaults

A note in the project file listed "six modules still writing settings
non-atomically". Counting them properly — with an AST pass rather than from
memory — produced **eighteen**, and three of them mattered a great deal:

| File | What a truncated write meant |
|---|---|
| `auth_settings` | zero bytes used to read as "authentication off" |
| **`api_tokens`** | every token gone **and `enforce` back to false — API authentication silently disabled** |
| `asset_manager` | the whole stamp / signature / watermark index disappears |

All thirty call sites now go through one helper (`app/core/atomic_json.py`):
same-directory temporary file → `fsync` → `os.replace` → `fsync` of the
directory. Four deliberate exceptions are documented, with a guard that checks
the exception list has not gone stale.

### Converted files were thrown away, with a message pointing at Java

`soffice` prints warnings (`failed to launch javaldx`) while converting
perfectly well, so its exit code is not a verdict. Only one of seven conversion
paths judged by the output file; the other six checked the return code first and
**discarded a good file**. All seven now require a usable output, and the three
failure modes are reported distinctly: empty output (source may be damaged),
killed by a signal (memory or concurrency, nothing to do with the file), and
everything else (with what soffice actually said).

### Calling the API exactly as documented could still fail

All 84 `curl` examples in `API.md` are now executed by
`tools/api_doc_example_audit.py`. One was genuinely broken:
`/admin/api/llm/test-connection` had no body in the example and no parameter
table, and the endpoint raised on an empty body. Both sides fixed — the endpoint
now falls back to the saved settings.

---

## [1.15.33] - 2026-09-13

### New tool: Document straighten (`doc-straighten`) — 47 tools → 48

Straightens skewed scans and phone photographs, trims black edges and evens out
background shading. **No AI and no GPU**: measured 0.83 s/page at 200 dpi.
A scan tilted 2.3° was estimated at −2.30° (error 0.00°) with 0.10° residual.

> **Pages that already have a text layer and are already straight are copied
> through untouched.** Re-rendering a born-digital PDF would turn selectable
> text into an image that merely looks the same — the document would stop being
> searchable and nobody would notice. Across eight real files, **100% of text
> survived**, and the completion message says how many pages were preserved.

"Convert to black and white" is off by default, and the interface says what it
is for: **smaller files, not better recognition**. Measured: local thresholding
drops OCR similarity from 0.775 to 0.108 on Chinese text.

### CI caught two problems that only exist in the published tree

A test hard-coded `github/OPS.md`, a path that only exists in the development
tree; and the SignPath signing step waited only ten minutes for an approval that
is manual by policy. Both fixed, and a guard now rejects literal `github/` paths.

---

## [1.15.32] - 2026-09-13

### De-identification now supports English documents

Adding English patterns was only half the work. **The Taiwanese patterns applied
to an English document do not miss things — they match the wrong things**:
passport numbers, IBAN fragments and card fragments were all matched as
telephone numbers, and a flight number matched a UK postcode. A false positive
is more dangerous than a miss, because the screen says "done".

Patterns now carry a locale and are selected by the **document's** language
(which is not the interface language — an English contract with a Chinese
interface is common, so the choice is on the page).

Everything with a check digit is verified — IBAN mod-97 above all — and the
replacement values are drawn from ranges that are **never assigned** (SSN 9xx,
555-01xx numbers, IBANs that deliberately fail their checksum), because a fake
number that validates may belong to a real person.

Both de-identification tools are no longer greyed out in the English interface.
The Taiwanese patterns were re-checked against real samples: no regressions.

---

## [1.15.31] - 2026-09-13

### The Windows installer is now English on English Windows

The product name is also a **path** — the Start menu folder and two shortcut
file names. Translating it naively means an installation made in one language
cannot be uninstalled in another: uninstall "succeeds" and leaves a folder
behind. The actual paths created are now recorded in the registry and read back
at uninstall time, with the old Chinese name kept as a fallback for upgrades.

### Services installed by the one-line Linux installer are now hardened

`packaging/jt-doc-tools.service` had five hardening settings; the unit the
installer generated itself had only `User=`. Anyone who installed with the
one-liner never had that protection. Verified with `systemd-run` using the same
settings — `sudo -u` proves nothing here, as it runs outside the namespace.

---

## [1.15.30] - 2026-09-13

### A coverage gate that compared the last path segment was not checking anything

It matched `/api/` endpoints by their final segment, so `list`, `count`,
`assets` and `history` matched something in a four-thousand-line document no
matter what. Eight of 84 endpoints passed without being covered at all; seven of
them appeared nowhere. Full-path matching now, with the mutation verified in
**both** directions — reverting to the old rule has to pass, or the change only
proves the wording moved.

### 79 state-changing admin endpoints had no acceptance criteria

The gate skipped the whole `/admin` prefix. Writes now require acceptance items
(§4.8, grouped by page); reads stay covered by page-level acceptance, and that
trade-off is written into the test itself.

### Further

* `API.md` was missing 16 endpoints.
* CI installed from `requirements.txt` while production uses the lockfile; a new
  test checks the three dependency declarations agree.

---

## [1.15.29] - 2026-09-13

* **Audit forwarding**: one failing destination no longer blocks the others —
  each destination keeps its own cursor and bounded retry queue.
* **PNG export**: pages are written to disk instead of accumulating in memory,
  and the temporary directory is cleaned up after the response.
* **Administrator privacy boundaries** are now one written policy rather than
  two endpoints disagreeing about what an administrator may open.

---

## [1.15.28] - 2026-09-13

### De-identification did not actually remove personal data from scans

Output looked correct in every visible way — text could not be extracted, black
boxes were on the page — but **extracting the page image and running OCR on it
recovered the data in full**.

The cause was `apply_redactions(images=PDF_REDACT_IMAGE_NONE)`: clearing pixels
inside the box is PyMuPDF's *default*, and that line deliberately turned the safe
default off. The pattern had been copied from the PDF editor, where the goal is
the opposite (move text, keep the logo underneath).

> Copying code means asking whether the source tool had the same goal. Here the
> correct value is the exact opposite, and no test went red.

The affected shape — a scanned image with an invisible text layer — is what this
product's own OCR tool produces, so it is a primary case, not an edge case.
Acceptance now inspects the images inside the output and re-runs OCR on them.

Clearing pixels re-encodes images as PNG: a real scan went from 2.3 MB to 5.5 MB.
Re-compressing to JPEG cost 3–4 s per page for 20–30% and a second lossy pass, so
the result page says so plainly and points at the compression tool instead.

### "Restoring previous state" was not true

When `uv sync` failed during an upgrade, that message was printed while the
working tree stayed on the **new** code with partially synced dependencies — and
the service was then started. Recovery now resets the code *and* re-syncs
dependencies, and the message distinguishes three outcomes: fully restored, code
restored but dependencies not synced, and could not restore.

### Further

* GELF over TCP is framed with a null byte, as Graylog requires; syslog and CEF
  keep RFC 6587 framing, pinned by tests.
* Cancelling a queued job released its row but kept its callable alive; one
  place now forgets a job, and a guard stops a fourth cleanup path from
  reintroducing the leak.

---

## [1.15.27] - 2026-09-10

### Parts of the Windows installer stayed Chinese on English Windows

Component names, failure messages and the three uninstall dialogs were
hard-coded. Three things were established by running the executable on real
Windows rather than by reading documentation: NSIS picks the language table from
the **system** locale, not from declaration order; `StrCpy $LANGUAGE` at runtime
cannot change an already-loaded table; and `makensis` does **not** warn when a
string is missing a language — "zero warnings" proves nothing.

---

## [1.15.26] - 2026-09-10

### Our own reverse-proxy example broke a customer: a hard-coded `X-Forwarded-Proto: https`

A customer reported "CSRF token missing or incorrect" as soon as a **remote**
machine uploaded a file, while the server itself was fine. The IIS `web.config`
example in `OPS.md` set the header to a fixed `https`; on an http-only site the
backend then marked cookies `Secure` and the browser dropped them over plain
http. Sign-in broke the same way, with no error shown.

> `http://localhost` is a secure-origin exception, so it accepts `Secure`
> cookies. **Testing a remote user's problem locally cannot reproduce it.**

The example now maps `{HTTPS}` properly, the documentation says the symptom
cannot be reproduced on the server itself, and the backend can explain the
mismatch. It **reports** it and does not relax anything automatically — the
detection headers are attacker-controlled.

---

## [1.15.25] - 2026-09-10

* **A saved SMTP port was overwritten every time the page loaded** (customer
  report). The convenience "fill in the usual port" logic also ran on load, and
  the flag meant to prevent that reset on every page view. The initial call now
  touches nothing, and switching mode only fills a field that is empty or still
  holds another convention value.
* The site-URL field was sized by a rule written for numeric fields.

---

## [1.15.24] - 2026-09-10

### The IIS reverse-proxy prerequisites were in the wrong order (customer report)

`OPS.md` said to install ARR and then URL Rewrite; **ARR depends on URL
Rewrite**. The instructions were correct when written — the Web Platform
Installer used to resolve that automatically, and upstream has since removed it.
The order is fixed, the reason is written down so it does not get "tidied" back,
and a literal test pins it.

---

## [1.15.23] - 2026-09-09

### A customer thought translation stopped at page six

It did not: 179 of 182 segments were translated and all 11 pages of output had
content. The side-by-side preview only renders the first six pages, and that was
said in small grey text nobody reads. The clue was in the customer's own words —
"the total word count is close to the original".

Anything that shows only part of a result now says how much the whole is, **at
the point where scrolling stops**, in a bordered box, with the download button
right there. A guard pins this for both tools that preview partially.

---

## [1.15.22] - 2026-09-08

### One table cell froze an entire translation, forever

A fill-in-the-blank line with sixteen non-breaking spaces made the model unable
to stop. With `stream=True`, an httpx `timeout` applies **per chunk**, so tokens
kept arriving and it never fired — the progress display simply stopped moving,
with no error and no failure. Streaming now has a separate wall-clock limit in
both loops, and the message says the model may be unable to stop rather than
blaming the network.

Documents that came from a PDF now say which engine was used, because the engine
with the best visual fidelity is the worst one to translate from: it pins each
line in place, which splits sentences across lines.

---

## [1.15.21] - 2026-09-08

### Word files containing text boxes were wrecked by translation

The same text was collected **four times** (a 44,900-word document extracted as
180,532 words): paragraphs that contain text boxes also iterate their contents,
and `mc:AlternateContent` stores the same content twice. Writing back put a whole
page into the first text box.

> When walking paragraphs, ask whether a node contains more nodes of its own
> kind — if it does, it is a container, not content. **A word count that does not
> match the original is the signal.**

The legacy VML copy is mirrored after translation, so the delivered file does not
carry a hidden full copy of the original text.

---

## [1.15.20] - 2026-09-07

### The glossary's placeholders were destroyed by the real model

Every test passed — a fake model naturally preserves whatever it is given. On
the production model, **all five sentences fell back** and the glossary did
nothing. The raw reply showed `<0xE2><0x9F><0xAA>1⟫`: a tokenizer that meets a
character outside its vocabulary emits the **literal text of the byte tokens**.

> Anything that depends on a model following instructions has to be tested
> against a real model. **Markers sent to a model must be ASCII** — seven were
> measured; `[[T1]]` was chosen.

The safety net worked so well that the output looked perfect while the feature
was not working at all; only the fallback counter could tell.

---

## [1.15.19] - 2026-09-07

### New: a translation glossary (shared by sentence and document translation)

Company-specific terms need one consistent translation. Putting a table in the
prompt is only a request the model may ignore, and the Traditional Chinese
instructions are already 1,179 characters against a 1,200-character batch limit.

This uses **term protection**, the standard approach in translation tools: the
terms are replaced with placeholders before the request, so the model never sees
them, and the required translation is substituted back afterwards. Deterministic,
and **not one character is added to the prompt**.

---

## [1.15.18] - 2026-09-07

### A scheduled CI run went red where the push run was green (**test-only change**)

The test waited on in-memory job state and then read the database, which is
written afterwards. Reproduced first by delaying the write, which made the old
test fail with exactly the CI message, then fixed to wait for what it actually
verifies.

---

## [1.15.17] - 2026-09-07

* **The pre-upgrade backup could fill the disk — and failed after the service
  was already stopped.** On production, 1.4 GB of a 2.0 GB data directory is a
  government dataset that re-downloads itself. Free space is now checked before
  anything stops, and the skip list has one rule: it must be able to rebuild
  itself. Measured: 1.93 → 0.56 GB per backup.
* **Every HTTP request read a settings file from disk**, on the event loop,
  including static files and health checks. Now cached by mtime and size:
  104 µs → 32 µs, with no restart needed for changes to take effect.

---

## [1.15.16] - 2026-09-07

### `sudo jtdt reset-password` could leave the service unable to write

Anything the CLI creates while running as root is owned by root, and the service
runs as its own account: `attempt to write a readonly database`. The worst case
is the rescue command itself — recovery would lock you out. Ownership is now
restored in one place in the dispatcher rather than at each return point.

### Files the public instructions tell you to run were not in the public tree

The screenshot script and the penetration-test script were missing, so the whole
procedure could not run from a clone. The existing gate only recognised commands
starting with `python …`, and half the document uses `.venv/bin/python …` — nine
of seventeen command lines had never been checked.

---

## [1.15.15] - 2026-09-07

* **`jtdt update` reported "Health check timed out" while the service was fine.**
  `jtdt bind` writes the listen address into the service manager's own
  configuration, and the health check read it from the shell's environment. Any
  installation with a changed port probed the wrong address forever. It now
  reads where the setting actually lives, probes loopback as well, bypasses any
  proxy, and prints what it probed plus the last 20 log lines instead of one
  unhelpful word.
* **`defusedxml` was imported by four modules but never declared.** On a machine
  without it, the tools are skipped silently: the service starts and health
  checks pass, and four tools simply are not there. A new test compares imports
  against the declared dependencies.

---

## [1.15.14] - 2026-09-06

* **Spreadsheet translation previews were blank.** The "fit to one page wide"
  step wrote new attributes after the tag name instead of replacing existing
  ones, producing duplicate attributes — invalid XML, which LibreOffice turns
  into an empty sheet with a **zero exit code**. Modified XML is now re-parsed
  before use, and falls back to the original if it does not load.
* **Translated spreadsheets opened on a blank area**, because the scroll
  position is stored in the file. The view is reset without touching frozen
  panes or a single cell of content.

---

## [1.15.13] - 2026-09-06

Every path that reads a user-supplied zip is now covered by one zip-bomb guard.

> ⚠ The first version of that guard was fake: it looked for the guard's *name* in
> the source, and every place that called it also had a comment mentioning it —
> so removing the import and the call left the test green. It now matches AST
> call nodes.

---

## [1.15.12] - 2026-09-06

* Without CJK fonts, ten tests failed with `TypeError: cannot unpack
  non-iterable NoneType` — nothing that suggests fonts. They now skip honestly,
  and the fonts are installed in CI so the "Chinese really renders" checks still
  run there.
* CI failures now name the failing test.

> ⚠ Hiding half the environment is the same as hiding none: a plugin that removed
> only LibreOffice reported "all green" while CI stayed red, because the runner
> has no CJK fonts either.

---

## [1.15.11] - 2026-09-06

* **Transit certificates keep the original file**, reachable from the record.
* High-speed-rail certificates that carry no train number no longer display `--`.
* Security: uploaded XML is parsed defensively and outbound downloads validated.

---

## [1.15.10] - 2026-09-06

Missing Office engine now returns **503**, not 500 — that is a deployment
problem, not a malformed request, and a 500 makes people retry forever while
monitoring fills with false alarms. One global handler covers every tool.

Tests that need system dependencies now carry skip gates, checked by AST rather
than a regular expression.

---

## [1.15.9] - 2026-09-06

* The first real CI run went red; the dangerous part was **silent loss of
  coverage** rather than the failures themselves.
* Damaged or hostile office documents are rejected **before** they reach soffice.
* Error messages no longer hand raw upstream responses to the user.
* English interface fixes from a page-by-page user review.

---

## [1.15.8] - 2026-09-06

### `tr` shadowed by a variable of the same name — three tools completely broken

A user reported `Upload error: tr is not a function`. `tr` is the front-end
translation function and also the most natural name for a table row:

```js
const tr = document.createElement('tr');   // tr is a DOM element here
inp.placeholder = tr('Subject');            // so this calls an element
```

Sixteen occurrences across three tools and nine admin pages. The worst was
`const tr = { 'host required': tr('Host is required') }` — calling itself from
its own initialiser.

Both existing defences were green: `node --check` only validates syntax, and this
shadowing **is** valid syntax; the i18n scanner checks whether strings are
wrapped, not what `tr` refers to where they are wrapped.

### Further

* Built-in role descriptions were wrong on existing installations.
* A batch of English interface fixes from a user's page-by-page review.

---

## [1.15.7] - 2026-09-05

### English interface: 986 untranslated strings → 0, verified in a real browser

Static scanning cannot see three sources of leftover Chinese: nodes built by
JavaScript, attributes (`title` / `placeholder` / `aria-label`), and server data
inserted into the DOM. A real browser walking every page found 986 strings, over
60% of them in the first two categories. The catalogue went from 3,286 to 4,698
entries.

> ⚠ "The scan found zero" is not "the translation is finished". A user replied
> with a dozen screenshots: dialogs, property panels, job lists, expanded
> dropdowns, error messages, tables that only exist once there is data — **none
> of that exists when the page first loads**. Three methods are needed, and the
> test plan now says so.

### Further

* **Windows: a machine whose installation was interrupted could never install
  again** (`uv venv --clear` added).
* CodeQL: an open redirect in `/ui-locale`, a `</script>` pattern that ignored
  whitespace, an unpinned minimum TLS version, and an unencoded job id.
* CI exists for the first time (`.github/workflows/tests.yml`).
* The English introduction site now uses **screenshots of the English
  interface** — the most direct evidence that English is really supported.

---

## [1.15.4] - 2026-09-05

### torch 2.11 → 2.14 (the setuptools alert can finally move)

The comment in `pyproject.toml` said the setuptools CVE needed torch 2.13 —
**torch 2.13 did drop the `setuptools<82` cap** (2.11 and 2.12 pin `<82`; 2.13
and later ask for `>=77.0.3`). With that gone, setuptools moves to **84** and the
moderate alert goes with it.

**Recognition was verified as identical on two real machines**, not assumed:

| Machine | Configuration | Result |
|---|---|---|
| `.30` | Linux / Python 3.10 / `+cu130` (the production configuration) | **line for line identical** to 2.11 |
| `.154` | Windows / Python 3.12 / `+cpu` | **line for line identical** to 2.11 |

The test image has six mixed Chinese/English/numeric lines (company ID, invoice
number, amount, address, email, restricted-use wording) — and **even the mistakes
OCR makes are the same** (`AB-` read as `1B-`, `NT$` as `1TT$`, `Xinyi` as
`Yinyi`). That is what shows the model behaviour has not changed; "it runs" would
not.

> ⚠ **torchvision must come from the same index as torch.** PyPI's torchvision
> with a torch from `download.pytorch.org/whl/cpu` gives `RuntimeError: operator
> torchvision::nms does not exist` — **it fails at import, so OCR is completely
> dead**. This project uses the default PyPI index, where both resolve together,
> but anyone installing by hand can hit it.

> ⚠ **OCR cannot be verified on dev1**: that machine is a QEMU VM whose **CPU has
> no AVX2**, so `readtext` dumps core. A control run showed **the current 2.11
> does exactly the same there** — it is the CPU, not the version. Without that
> control I would have blamed the upgrade.

**Production's torch changes on the next `jtdt update`** (that step runs uv sync:
about 5 GB of downloads and a restart). This release only updates the declaration
and the lockfile.

---

## [1.15.3] - 2026-09-05

### Stamp and seam stamp had identical icons (spotted by a user)

Both sit in the "forms and stamps" group, both used `stamp`, and they are next to
each other in the sidebar — **you had to read the label to tell them apart**. The
seam stamp now has its own icon: **two sheets side by side with a round stamp
straddling the seam between them**, which is exactly what the tool does.

Three more same-group duplicates were cleared at the same time:
`image-to-pdf` / `pdf-to-image` (both `image`), `doc-deident` / `text-deident`
(both `shield`) and `doc-diff` / `text-diff` (both `diff`). The plain-text
variants now use `text` and `columns`, and images-to-PDF uses `layers` (several
stacked into one). **Duplication across groups is left alone** — those are two
different lists and never sit side by side.

Two gates: no two tools in one group may share an icon (mutation test: putting
`stamp` back on the seam stamp turns it red), and every icon name must actually
exist in `icons.html` (**a typo raises no error, it simply shows no icon**, and
only eyes catch that).

### Dialog titles and buttons were still in Chinese

`showConfirm(message, { title: '清空工作區', okText: '清空' })` — the message was
already translated, but **the strings in the options object were not**, so in
English the dialog's title and buttons stayed Chinese. 175 of them are fixed.

The rewrite only touches text **inside a `showConfirm` / `showToast` /
`showModal` call** — `title:` elsewhere is **data, not display text** (a bookmark
is `{title, page, level}`), and translating that would rewrite the user's bookmark
titles, which is corrupting data rather than translating it.

Tooltips (`title=` attributes) were already covered by the 174 display-only
attributes in v1.15.2. The catalogue now holds **3,286 entries**.

---

## [1.15.2] - 2026-09-05

### Stamp and sign / seam stamp are no longer restricted to Chinese

These two were shown only in a Chinese interface, on the grounds that "stamps are
a Chinese documentary convention". **That judgement was wrong**: applying a company
stamp, a signature image or a logo, and stamping across pages so a swap shows, are
done everywhere. The bar for greying a tool out should be "**an English document
goes in, it succeeds, and nothing is found**" (the company ID database, e-invoice
QR codes, the ID-number and Taiwanese address patterns, the Chinese field-label
dictionary) — stamping is not one of those. Tools greyed out in English go from 9
to **7**.

The 246 strings on those two pages are translated as well (the catalogue now holds
**3,208 entries**).

### In English, field labels covered the checkboxes and inputs (reported from two screenshots)

`.form-row label` is a **fixed 96px with nowrap** — sized for **Chinese** (four to
six characters). English runs about 1.7× wider, so "Enable the workspace" and
"Remove the background" **overflowed straight over the controls beside them**.

The fix is scoped with `html[lang]:not([lang="zh-Hant"])`, so **the Chinese layout
does not move by a single pixel**. That is safer than widening the 96px, which
would give Chinese a wider label column for no reason.

60 over-long English strings were shortened at the same time (buttons and field
labels): `Make it the default` → `Set default`; `Counting window (minutes) — only
failures inside it count` → `Counting window (minutes)` (the explanation is
already on the hint line below).

> **No automated test catches this class of problem** — the elements are all there,
> there is no JavaScript error, and no Chinese is left. Only looking at a
> screenshot shows it. That is what `scripts/page_screenshots.py --locale en` is
> for.

### Sentences with variables in JavaScript are translated too

`` `已選：${file.name}` `` cannot simply be wrapped — **once interpolated the key
no longer matches**, so the lookup silently fails. They are parameterised instead:
`tr('…{0}…').replace('{0}', expr)`, and only when the interpolated expression is
simple enough (no quotes, backticks, newlines or nested templates); anything else
is skipped entirely. 97 in this batch.

---

## [1.15.1] - 2026-09-05

### An out-of-range thumbnail page returned 500

`/tools/pdf-rotate/thumb/<id>/0` and `/99` both returned **500**. The page number
is **in the path**, so that is a user asking for a URL that does not exist, not a
broken server — **a 500 makes people think the service is down and keep retrying,
and fills the monitoring with false alarms** (the same principle as v1.14.37's
"a corrupt file is always 400, never 500").

The interesting part: **the range was already checked**. `render_page_png` was
fixed in v1.14.31 for the nastier bug where `page_no=0` used a negative index,
returned the last page, and answered 200 OK. But nothing caught the `ValueError`
it raised, so it surfaced as a 500.

The fix is one **global handler** (a dedicated `PageOutOfRange` → 404), as with
corrupt files — the thirty-odd thumbnail and preview endpoints all have the same
shape, and patching them one at a time means the next new tool is missed again.

The gate (`tests/test_preview_page_range.py`) judges **"not a 5xx" rather than
"must be 404"** (a tool that stops it earlier with a 400 is also right), and it
has **a reverse check**: without one, making the endpoint always return 404 would
pass. Mutation test: removing the handler turns 12 cases red.

### Running a tool for real in the English interface

`temp/i18n-cdp/cdp_en_e2e.py` sends a PDF through the tools **in English** and
looks at the output. Wrapping JS strings deliberately skipped ternaries and string
concatenation (those may hold values that are compared or sent to the server), but
skipping is only an attempt to avoid the problem, not proof of it — a translated
value looks **perfectly normal on screen while the logic quietly breaks, and only
in English**. Measured: word count reports 3 pages / 21 words, page rotation
uploads and returns a 5,132-byte thumbnail, and not one drop-down `value` has
turned into Chinese.

The workspace drop area hint — the last leftover — is translated too.

---

## [1.15.0] - 2026-09-05

> The patch number rolls into a minor at 99 (a project convention, so no 1.14.100).

### Finishing the interface language work

Chinese left across the site in English went from thousands of strings to **202**,
and nearly all of what remains **should not be translated**:

- **The product name** (`Jason Tools 文件工具箱`, 74 occurrences) — it is a brand,
  and an administrator can replace it; translating it automatically would also
  replace a company name somebody had set.
- **Domain data** — the field synonym dictionary (`付款方式, 匯款方式, Style of
  Payment…`), accounting categories, the redaction patterns. **Not one word of
  this may enter the catalogue**: translating it makes auto-fill forms **silently
  stop finding fields**.

What was filled in this round were the sidebar items that kept being missed: the
`aria-label` and button text for **"notifications" and "home"** (71 occurrences
each, the most frequent leftovers on the site), the retention item names, and the
history page title.

### Display-only attributes and the LLM tool labels

`placeholder`, `title`, `alt` and `aria-label` — attributes that are **display
only** — had been missed by the first two passes (which covered text nodes and
`{% with %}` parameters). 174 of them are now translated, so input hints and
button tooltips follow the interface language. The per-tool descriptions in the
administration LLM settings (29 more) are covered too. The catalogue holds
**2,917 entries**.

### Two things fixed along the way

- **`current_locale()`'s documentation was wrong** (it still said "switching is not
  available yet"). It is really the fallback for when there is no request to ask —
  the language lives in a cookie and can only be determined with a `Request`.
  `tool_visible()` must always be given the locale explicitly; falling back means
  treating everything as Chinese, and the nine Chinese-only tools would appear
  usable in English.
- **`test_admin_users_table` did not recognise the `{{ tr('…') }}` wrapper** and
  went red the moment i18n was added, which has nothing to do with the column
  order it exists to protect. **A gate that fails on unrelated changes is as bad as
  one that misses real problems** — nobody believes it the next time it goes red.

---

## [1.14.99] - 2026-09-05

### Interface language, stage B finished: the administration area

Every visible string on the 30 administration pages (1,048 of them) and the
messages inside their `<script>` blocks (118) now go through the translation
layer, along with the 34 descriptions in the "settings" menu. The catalogue holds
**2,711 entries**. Chinese left on the administration pages in English went from
**1,497 to about 200** (what remains comes from Python data — LLM tool names, OCR
language labels — and the product name in the page title).

### ⚠ The safety net could not see the administration area

`tools/i18n_zh_baseline.py` originally covered only tool pages and general pages,
so **a broken administration page was invisible to it**. Extending it ran into two
things that differ on every run, either of which would have made the comparison
permanently red (and therefore worthless):

- **JSON APIs share the administration GET routes** (system status, job queue …)
  and their responses carry timestamps → only `content-type: text/html` is kept.
- **The data directory path is printed on the pages** (export directory, font
  directory), and the baseline script used a fresh `mkdtemp` each time → it now
  uses a fixed `temp/i18n-baseline-data`.

It now covers **81 pages**, including every administration page, and this round
was byte-for-byte identical throughout.

### Chinese keywords still find administration pages in English

The sidebar search matches against `data-name`; replacing it wholesale with
English would mean **a Chinese search no longer finds anything**. Both languages
now go into `data-name` in English, while **the Chinese interface is left exactly
as it was** (otherwise the same string appears twice and the bytes change — the
safety net caught that immediately).

---

## [1.14.98] - 2026-09-05

### Interface language, stage B (part 2): strings inside `<script>` too

The template helper `{{ tr('…') }}` is evaluated **while the server renders the
page**, so button labels, error messages and text inserted into the DOM at runtime
could not use it — 1,311 strings, the bulk of a tool page.

The answer is a `tr()` of the same name on the front end
(`static/js/i18n.js`), with the dictionary served from `GET /i18n/<locale>.js`:

- **Traditional Chinese never loads a dictionary at all** (the template only emits
  that `<script src>` for other languages) and `tr()` returns its argument — no
  cost, no risk.
- The dictionary is around 100 KB and only changes on upgrade, so it carries an
  **ETag**: moving between pages sends one `If-None-Match` and gets a 304
  (`no-cache` does not mean "do not cache", it means "ask before using").
- **Sentences with variables are parameterised** (`tr('Selected: {0}').replace(...)`);
  an interpolated sentence must never be the key, because the key changes with the
  value.

**Only positions that cannot be used as values are wrapped**: `.textContent =`,
`.innerHTML =`, `.title =`, `.placeholder =`, `alert(`, `showToast(`,
`showConfirm(`, `friendlyServerError(…,`. Ternary results and string
concatenation are deliberately left alone — translating a string that is compared
against something, or sent to the server, looks perfectly fine on screen while the
logic quietly breaks, **and only in English**. 356 strings in this batch; the
catalogue now holds **1,507 entries**.

Verification uses a real browser (`temp/i18n-cdp/cdp_i18n_test.py`): in English the
button raises an English message, and in Chinese not one character changed. The
signal has to be something that only appears when the translation really happened —
an untranslated string comes back as Chinese with **no JavaScript error at all**.

### I made the "match translations by index" mistake again

The batch process is: print the untranslated list → write translations in order →
merge back by index. Between those steps I removed two keys that contained Jinja
syntax (`tr('{{ icon(...) }} …')` — the template renders first, so the runtime key
is rendered HTML and never matches), the list was regenerated, the order changed,
and **everything from the 8th entry on was two places out**. Spot-checking caught
it before it shipped.

This is the same fault fixed in v1.14.97 on the introduction site. So
`tools/i18n_merge.py` now exists: **translation batches must be keyed by the
Chinese source string, and a batch whose keys look like indices is refused.**

Three more gates: every JS `tr()` key must be translated; keys must not contain
template syntax; and a translation must keep a trailing colon or ellipsis (`tr('Analysis failed: ') + err`
loses the separator otherwise — and that check also catches whole-batch misalignment).

### Site-wide screenshots can now be taken in English

`scripts/page_screenshots.py --locale en`. English runs about 1.7× wider than
Chinese, and **no automated test catches a broken layout** — only eyes do. All 80
pages were reviewed this round; nothing overflowed or was cut off.

---

## [1.14.97] - 2026-09-04

### Sentences chopped up and headings pasted onto the wrong section (reported from a screenshot)

Every clause of the disclaimer began with a comma, the text under "Terms of use"
belonged to another paragraph, and a table cell read `; JSON:`. A user spotted it
at a glance while every existing gate stayed **green**. Two causes:

**① Sentences split by inline markup were translated piece by piece.** Extraction
worked on text nodes, so `<b>This software is provided AS IS</b>, including but
not limited to…` was two pieces. Chinese reads correctly when the pieces are
concatenated in the original order; **English word order differs**, so the result
was fragments like ", including but not limited to…". Extraction now takes **the
whole block, inline tags included**, so the translation decides where `<b>` goes.

**② Translations were attached to the wrong keys.** An earlier merge matched
translations to keys **by index**, so a change in list order shifted whole runs.
**The existing gate only checked for leftover Chinese — after a shift there is no
Chinese at all, so it passed.**

A nastier variant: **fragments consisting only of punctuation were treated as
translatable**. A lone `—` in a table became a key that matches everywhere, and
another string's translation was pasted where that dash belonged. That is where
`; JSON:` came from.

### Three new gates, all decidable from the text itself

- **Inline tags and links must match exactly** — a translation cut short, or
  pasted onto the wrong key, no longer has the same tags. This caught 8 of my own
  translations that were missing their tails.
- **Compare the Chinese and English pages block by block**: if an English block
  starts with punctuation where the Chinese one does not, fail. The judgement
  lives **on the rendered pages, not in the catalogue** — "starts with
  punctuation" is sometimes correct in the catalogue (the source really is the
  middle of a sentence split by `<code>`), so judging entries individually gives
  false positives, while comparing pages is position against position. Mutation
  test: replacing the translation of one heading turns it red (**the first version
  missed `<div>` and stayed green** until that was added).
- **Pure-punctuation keys are not allowed.**

### Interface language, stage B (continued)

Option labels that come from Python data — fonts, themes, languages, formats — are
now translated too. The catalogue holds **1,284 entries**. The Chinese output is
still byte-for-byte identical.

---

## [1.14.96] - 2026-09-04

### Interface language, stage B (part 1): 38 tool pages in English

After the shell, the inside of the tools. The catalogue grew from 313 to
**1,205 entries**, covering the 38 tools that are usable in an English
interface (the nine Chinese-only tools are greyed out in English, so they are
left for later).

**The Chinese output had to be proved unchanged first.** Wrapping several
hundred lines of templates in `{{ tr('…') }}` is too much to check by eye, and
when it breaks it usually still *looks* right — a lost space, or one extra layer
of escaping. Pixel comparison is both slow and blind to that. So
`tools/i18n_zh_baseline.py` compares the **rendered HTML byte for byte** across
52 pages after every batch (the CSRF token and CSP nonce are normalised first,
otherwise the comparison is always red and the safety net is worthless).

The wrapper is deliberately timid: anything containing `&` (an HTML entity would
be double-escaped), a quote, or template syntax is skipped — better to miss a
string than to break one. A second pass covered `{% block title %}` and
`{% with hint='…' %}`, which are not text nodes.

Chinese left on the tool pages themselves went from **1,555 to about 90**.

---

## [1.14.95] - 2026-09-04

### Test-plan coverage gaps

Comparing the route table, the tool registry and the `tests/` directory against
the plan by program turned up four areas with no acceptance criteria at all.
Each now has a gate, so anything new that is not written into the plan turns red:

| Gap | Size | Why it matters |
|---|---|---|
| **Non-API endpoints** (§4.7) | **267** | every button on screen calls this layer |
| **Test-file index** (§1.99) | 96 of 212 listed | the other 116 run, but what they guard is invisible |
| **CLI commands** (§3.5) | 8 of 26 listed | when the web UI is unreachable, this is the only way in |
| **Schema migrations** (§1.98) | **29**, none listed | losing data on upgrade is the least reversible failure |

The non-API layer matters most: §4 only guaranteed that each tool had one
`/api/` endpoint with acceptance criteria, yet the worst bugs this project has
had all happened in the non-API layer while the `/api/` route was fine —
horizontal privilege escalation in the N-up preview (B could download A's PDF),
90-second per-page previews in the seam stamp, permanently blank workspace
thumbnails, and `/AF` left behind in the "copy without attachments".

### Two gates that were themselves broken

- The published test plan told people to run `python tools/check_*.py`, but
  **`tools/` had never been synced into the repository** — copying the command
  gave "file not found", so those checks were never run. A new gate checks the
  published tree as well as the working tree.
- `check_version_consistency.py` exited 0 when it could not read a source at
  all. Putting a language switch above the README title made the heading reader
  return `None`, and the check silently stopped verifying the README. "Cannot
  read it" now counts as a failure.

### English README and change log

`README_en.md` and this file, each with a language switch on the first line; the
Chinese files keep their names.

`README_en.md` is **generated** the same way the introduction site is
(`github/build-i18n-md.py`, line by line against `docs/i18n/readme.en.json`, code
blocks left untouched) — one document maintained by hand in two places always
drifts, and this project has paid for that several times. This change log is a
**summary**: the Chinese one covers 767 releases over six thousand lines, which is
neither useful nor maintainable to translate in full.

### The README's pytest badge had been stale for many releases

It read **470 passed**; the real figure is 5,951. A new gate
(`test_readme_pytest_badge_is_not_stale`) requires the badge to be **no lower than
the number of `def test_` definitions in `tests/`** — deliberately one-sided, since
parameterisation only ever adds cases, so a badge below the definition count is
certainly stale and can never be a false positive.

---

## [1.14.94] - 2026-09-05

### Word count now accepts office documents

`.doc` / `.docx` / `.odt`, `.xls` / `.xlsx` / `.ods`, `.ppt` / `.pptx` / `.odp` —
**converted to PDF first, then counted**. That way the page count and per-page
figures match what actually prints, and the existing PDF counting path is reused.
Reading the XML directly would be faster but gives no page count, and paragraphs
and line breaks would differ from the laid-out document.

A failed conversion returns **400** (corrupt file, or no Office engine), never 500;
the test is “did we get a usable file”, not soffice's exit code.

### Interface fixes

- The English label `Authentication realm` on the sign-in page was clipped by the
  fixed-width label column — shortened to `Realm`.
- Finished translating the sentences in shared components that inline markup had
  split apart (LLM service notice, missing-Chinese-font warning, the background-job
  “you can close this page” hint).

---

## [1.14.93] - 2026-09-04

### The introduction site and API manual are available in English

`docs/index-en.html` and `docs/api-en.html`, with a language link in the navigation
of each pointing at the other.

**They are generated, not maintained by hand.** The same document kept in two
places always drifts. The Chinese page stays the single source of truth, and
`github/build-i18n-page.py` extracts the translatable strings and produces the
English page from a catalogue.

### Locale-restricted tools are greyed out rather than hidden

Nine tools are built for Chinese / Taiwanese documents and conventions (company ID
lookup, e-invoice processing, travel receipts, pre-submission check, auto-fill
forms, both redaction tools, seam stamp, stamp and sign). In a non-Chinese
interface they are still listed, but greyed out, not clickable, and not pinnable,
with a tooltip explaining why. Seeing that a tool exists and why it cannot be used
is easier to understand than the tool disappearing.

### Interface language (i18n)

The shell is translated: sidebar, search, notifications, sign-in, two-step
verification, first-run setup, home page, my jobs, my workspace, and all 47 tool
names and descriptions. The language is chosen from the account menu or on the
sign-in page, and **only an explicit choice changes it** — browser language is
deliberately ignored, because switching to English also greys out those nine tools,
and nobody should lose access because of a browser setting they never chose.

---

## [1.14.87] - 2026-09-04

### Found the real cause of “20% of batches lose a segment”

Document translation batches were being judged as incomplete and retried — pure
waste, because the translation was there all along:

```
⟦<0xC2⟩5⟧5. New requirement - effective immediately   ← a stray <0xC2> inside the marker
```

`<0xC2>` is what a tokenizer emits, **as literal text**, for a byte that is not a
character. The marker no longer matched, that segment was not parsed, the whole
batch was judged incomplete, and a full generation was thrown away. Stripping
`<0x??>` before parsing fixed it: 3/3 parsed where it had been 0/3.

---

## [1.14.85] - 2026-09-03

### Document translation was dropping text colour

A spreadsheet cell containing “explanatory text + line break + a red italic note”
came back with the note in plain black. The translation had been written into the
paragraph's first text node, collapsing the whole cell to that node's style. There
was no error and the layout was unchanged — only a side-by-side comparison with the
original showed it, and that red “this is a new requirement” note was the point of
the document.

The translation is now written back **line by line where the runs allow it**, so
line-level colour, italics and bold survive.

---

## [1.14.67 – 1.14.83] - 2026-09-03

### New tool: document translation

Translate a whole office document into another language and get **the same format
and layout back** — only the text changes. Nine formats; the older binary formats
(.doc/.xls/.ppt) are converted to the modern one, translated, and converted back.
**PDF is not accepted**: a PDF has no paragraphs, its text is positioned fragments,
and replacing them with translations of a different length is bound to break the
layout.

---

For releases before this, see **[CHANGELOG.md](CHANGELOG.md)** (Traditional Chinese).
