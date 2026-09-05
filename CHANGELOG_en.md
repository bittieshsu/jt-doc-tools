[繁體中文](CHANGELOG.md) ｜ **English**

# Change log (English)

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

> **Scope.** Traditional Chinese is this project's primary language, and
> **[CHANGELOG.md](CHANGELOG.md) is the complete history** (724 releases).
> This English file summarises **recent releases** — enough to see what changed
> and decide whether to upgrade. For anything older, read the Chinese file.

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
**summary**: the Chinese one covers 724 releases over six thousand lines, which is
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
