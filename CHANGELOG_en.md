[繁體中文](CHANGELOG.md) ｜ **English** ｜ [日本語](CHANGELOG_ja.md)

# Change log (English)

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

> **Scope.** Traditional Chinese is this project's primary language, and
> **[CHANGELOG.md](CHANGELOG.md) is the complete history** (822 releases).
> This English file summarises **recent releases** — enough to see what changed
> and decide whether to upgrade. For anything older, read the Chinese file.

---

## [1.15.93] - 2026-09-21

### Short-lived signed URLs: let an external service fetch one file, without handing over a credential

Some external services only accept a **URL** — the file is not uploaded to them,
they fetch it themselves from an address we provide.

The obvious approach is to issue them an API token. **That road is closed**:
`api_tokens` has no concept of scope, so one token unlocks every `/api/*`
endpoint (jobs, notifications, workspace, every tool). Granting all of that so
someone can fetch one file is not acceptable by this project's own standards.

A signed URL is the other way round: **nothing is handed over**. The URL is the
authorisation, it expires on its own, nothing needs revoking, and a leak is
bounded to that one file until it expires.

Three decisions:

* **The expiry has to be inside the signature** — sign only the file id and
  anyone can extend it indefinitely by editing `exp`.
* **Anything that fails to verify returns 404, not 403** — a 403 tells the caller
  "that id exists", and the id is the thing we did not want to leak. Malformed
  id, wrong signature, expired, file missing: all four must look identical from
  outside.
* **The URL is built from a configured address, never the request's Host** — the
  same service is reachable three ways (direct on the internal network, via the
  reverse-proxied domain, on the test box). Building it from the request Host
  means anyone arriving via the public domain submits a job carrying that domain,
  which the other side's source allowlist correctly rejects — and the symptom is
  "it works for some people and not others". A guard checks the source of that
  function for `request` / `headers` / `url.hostname`.

> This path **needs no change to any gate** (measured): a GET without a bearer
> falls back to session auth, and `/api/` is in the public prefix list; CSRF only
> guards unsafe methods. **POST is not like this** — CSRF is a pure-ASGI
> middleware that runs before route matching, so any POST without a token gets
> 403, including to paths that do not exist. The two paths sit behind completely
> different gates; a conclusion about one does not carry to the other.

## [1.15.92] - 2026-09-21

### Meeting summary gains a fifth category: events and impact

An external review noted that the summary never states the incident itself —
what happened and how much it affected. **That was not a model limitation, it
was our design**: the summary is written **only from verified items** and never
sees the transcript, and "what happened" was not one of the four categories, so
it structurally could not appear.

### How you add it turns out to matter a great deal

| Approach | Recall (4 kinds) | Fabrication (4 kinds) | Events and impact |
|---|---|---|---|
| Baseline (four kinds) | 100% | **5%** | — |
| Five kinds in one call | 88-100% | **16-19%** | 2 (duplicates) |
| Five kinds + explicit routing rules | 100% | **20%** (worse) | — |
| Its own pass | 100% | 5-6% | 19 (half were progress reports) |
| **Its own pass + mechanical boundary** | **100%** | **6%** | **7, every one correct** |

**More rules produced more output, and more noise.** Reading the items showed
why: the model did start noticing facts, but **filed them under decisions**
("the API rate limit is sixty a minute", "they quoted 320,000 a year for
maintenance"). The problem was never that the boundary was unclear — it was
**judging five categories in one call**.

So events and impact **runs as its own pass**: its own prompt, its own review,
while the four-kind prompt and rules are **unchanged to the character**.
**The zero regression is guaranteed by construction, not by tuning.**

The cost was measured: extraction calls double (29 to 61, 147s to 303s for a
160-minute meeting). So there is a switch, and **the trade-off is stated on the
page** — leave it on for incident and status meetings, turn it off for purely
forward-looking planning and the analysis takes about half as long.

### The boundary has to be mechanical, not a matter of feel

The first version said "personal progress reports do not count". **The model
could not tell**, because "the config file has been sent out" genuinely is
something that already happened. It became two mechanical rules:

* Never write "done / sent / reviewed", nor "stuck / waiting / not ready yet".
* Every entry must state **what was affected** or **a specific number** —
  if it can state neither, do not write it.

> **⚠ I opened a hole while closing one**: rewriting the rule replaced the
> example "stuck because permissions were not granted" along with the paragraph
> around it, and that whole class promptly reappeared (4 entries, with
> duplicates). **Do not open one hole to close another** — the restored rule
> sits alongside the new ones rather than replacing them.

> **The first guard had no teeth**: `analyse` has two per-kind loops, and I only
> checked that `MAIN_KINDS` appeared somewhere — changing just one of them back
> to `KINDS` stayed green. The criterion had to become "**no loop may use
> `KINDS`**".

## [1.15.91] - 2026-09-20

### Fixed: mindmap node text was cut off, so it read as if the analysis had lost content

The node label was `text[:60]` — **60 characters, no marker of any kind**. On
screen that produced "…set the environment variable (Environment Variable) to L"
and "…deny them all de", while **the cards and the transcript held the full text
all along**.

The user cannot tell "shortened for display" from "the analysis lost the rest",
and those two are worlds apart in severity.

Three parts to the fix: **an ellipsis** so shortening is visible, **never cut a
Latin identifier in half** (`LOG4J_FORMAT…`, `deployment.yaml`,
`X-Forwarded-Proto` each count as one word), and **the tooltip now shows the full
text** (it used to show the same truncated string).

### The 60-character limit came from a corpus that could never reach it

Of **868** items from earlier runs: median **18** characters, **longest 56** —
**the limit was never once hit**, so the truncation never appeared in any
measurement I had taken.

Running a real committee transcript (54 items): **median 39, p90 = 101, longest
379**.

| Limit | Shown in full |
|---:|---:|
| 60 (before) | **77%** — roughly one in four cut |
| 90 | 87% |
| **120 (now)** | **94%** |
| 160 | 98% |

> **A corpus can be large and still the wrong shape.** The evaluation corpus is
> synthetic short meetings; real items carry English terms and parenthetical
> notes. Same lesson as "the class synthetic samples cannot reach".

> **A guard must not share a definition with the code it guards**: my first
> version used the product's own `_is_token_char` to check "did it cut an
> identifier in half", so a mutation that emptied that definition **was not
> caught at all** — the criterion moved with the mutation and stayed
> self-consistent. The test now carries its own.
>
> The first version of that same test also used
> `original.startswith(what_was_kept)`, which is **toothless**: `LOG4J` is a
> prefix of `LOG4J_FORMAT…`, so **exactly the broken cut would be judged
> correct**.

## [1.15.90] - 2026-09-20

### Fixed: half the meeting summary's "speaking time" figures are estimates, and the page did not say so

Subtitle files (.vtt / .srt) and JSON carry an end time on **every** cue — those
are measured. A plain-text transcript only records when each person *started*,
so the end time is borrowed from the **next** segment's start, which means the
speaking time **includes the pauses in between**.

The two look identical on screen. The estimated kind now says so: the column
header becomes "Speaking time (estimated)" and a line under the table explains
how the figure is derived.

> **The server decides** (from which parser ran); the front end must not guess
> from the segments — "every end time equals the next start" can also be true of
> a genuine subtitle file, so a guessed criterion would lie on some files.
>
> The guard **first proves the premise holds** (plain text really does borrow the
> next start). If the parser ever stops filling it in, the expectation above
> becomes a lie while staying green.

### Empty cards no longer draw a conclusion about the meeting

"This meeting had nothing of this kind" became "**the analysis did not find**
anything of this kind **in the transcript**". Measured recall is 94-100%, not a
guarantee of 100% — the first phrasing draws a conclusion about the meeting, the
second states what we actually know.

## [1.15.89] - 2026-09-20

### Fixed: the submission-check row in "My jobs" had nothing to click

That row has exactly two exits: **download** (`result_path`) and **open**
(`view_url`). Submission check does not produce a file — it produces **a report
filed under a case** — so it legitimately has no `result_path`; but it had no
`view_url` either, so the row said "done" with nothing to click and the user had
to work out for themselves to go back into the tool and find the case.

The case page is already addressed by case id, already polls progress and
already has its own access check, so pointing at it is enough — **no `?job=`
restore work was needed**.

**All 29 job-submitting tools were surveyed**: only three actually offer "open"
(sentence translation, document translation, meeting summary) and all three
restore correctly, so the "open lands on a blank page" bug fixed earlier does
not exist anywhere else. This was the only tool with neither exit.

> **⚠ My first survey was wrong.** I scanned for `"view_url" in source`, and
> **`preview_url` contains `view_url`** — seven tools that merely show a preview
> image were counted as having "open", three of which I briefly took for a bug.
> Substring matching again (this project has been bitten by prefix and substring
> matching several times). The guard now works on the AST, and **one test exists
> purely to check it does not mistake `preview_url` for `view_url`**.

> **The "prove the scan reaches something" check caught me too**: the first AST
> matcher only recognised `_jm.job_manager.submit` (an `Attribute`), while most
> tools do `from … import job_manager` and call it directly (a `Name`) — **it
> reached 3 tools out of 29**. Without that check the guard would have quietly
> examined three tools and stayed green.

> **The criterion has to land on what "My jobs" actually receives**: `view_url`
> is set *after* `submit()`, with a database write between it and the list. So
> alongside the static guard there is one that really submits a job, waits for
> it to land in the database, and confirms the list side can read it.

## [1.15.88] - 2026-09-20

### The workspace now takes plain text (.txt / .md)

Transcripts from the meeting summary tool and output from the list tool could
not be kept in the workspace.

**The test is the content, not the file name.** Every other type the workspace
takes has a clear signal (PDF and PNG by magic bytes, Office files by opening
the zip and inspecting its structure); plain text has neither. Accepting by
extension would let anything through by renaming it to `.txt`, which is exactly
what the type check exists to prevent. The test is now "the whole file decodes
as UTF-8 and contains no control characters other than tab, CR and LF", and the
name only chooses **between .txt and .md**. A PNG renamed to `.txt` is still
detected as a PNG.

**The whole file is checked, not a sample of the first few KB** — a file whose
first 8 KB are clean and whose tail is binary is easy to produce, and sampling
would let it straight in.

> Plain text has no first page to draw, so its thumbnail is the blank
> placeholder — but it has to say so. Falling through to the "treat it as a PDF"
> path makes it look for a `file.pdf` that does not exist, and the error becomes
> "file not found", which reads as if the user's file had been lost.

### Fixed: one more list written twice — the upload accept attribute

The file picker in `my_workspace.html` hardcoded
`application/pdf,image/png,.docx,…` in two places, while the server-side list
gained spreadsheets and presentations back in v1.14.6. The symptom is that
**the file picker filters out files the server would happily accept**: the user
just finds a file "cannot be uploaded", with no error message to go on.

The list is now computed on the server and passed into the template, the same
way `data-ws-exts` already worked. The "PDF / PNG" wording on the page was
reworded so it will not drift again either.

## [1.15.87] - 2026-09-20

### Fixed: the macro hardening around conversions had never taken effect

Conversions handle files uploaded by users, so each one seeds a throwaway
profile with `DisableMacrosExecution`. Measuring it showed every converter also
passed `--safe-mode` — and that flag **resets the user profile at startup**, so
the setting was gone by the time the run finished.

The old comment said "`--safe-mode` has nothing to do with macros". That
sentence is true; what it missed is that safe mode has everything to do with
**the settings we seed**: it wipes the lot.

> The criterion is "is the setting still there after the run", not "did we write
> it out". The file was written every time, so checking the write would never
> have shown this.

Severity, stated plainly: LibreOffice ships with macro security set to High and
headless conversion does not auto-execute macros, so this was **a layer of
defence in depth that was not working**, not an exploitable hole.

The two things `--safe-mode` guarded against are already covered: user
customisations (every call gets a fresh throwaway profile) and the crash
recovery prompt (`--norestore`). With it removed, three real documents (docx and
xlsx) produce identical page counts, page sizes, character counts and per-page
rendered pixel hashes.

### Fixed: exported PDFs were Letter, not A4

Taiwan prints A4. Letter is 18mm shorter and 6mm wider, so content laid out for
A4 shifts. This affected every conversion whose source carries no page size:
Markdown to office document, the meeting summary exports, plain text and CSV.

* **CSS `@page { size: A4 }` is ignored** by LibreOffice's HTML import.
* **`LANG` and `LC_PAPER` do nothing** — `LANG=zh_TW.UTF-8` only changes the
  length unit (in to cm), the paper stays Letter, and most servers have no
  zh_TW locale generated at all.

What works is seeding `ooSetupSystemLocale` in the throwaway profile. **It must
not be applied unconditionally**: it is also the system locale, so it changes
CJK font fallback. On a real vendor form in docx, the leading spaces were then
measured with a different font and the whole label **shifted about 10pt left**
(identical ink and identical glyphs — purely the width of the whitespace). The
form-filling tools are acutely sensitive to coordinate shifts, and their sources
carry a page size already, so they do not need this at all.

So the test is whether the source declares a page size: those that do not (HTML,
plain text, CSV) get ours; those that do keep theirs — an .odt declaring Letter
still comes out Letter, and a real docx renders to the same per-page pixel hash
as before the change.

## [1.15.86] - 2026-09-20

### Fixed: a term that is not Taiwanese usage

`估計` is Mainland-flavoured. Taiwan writes `預估` (an estimated reading time),
`推估` (derived from another number) or `約` (about 800 MB). It is now on the
banned-term list, scoped to text the user can see — `背景估計` in a comment is a
technical term and stays.

Adding the rule immediately found two places: the word-count tool's reading-time
label (**the panel right next to it already used the correct form**, so both
spellings sat on the same screen), and one sentence in the README about how
memory is measured. The job list's `估 800 MB` became `約 800 MB` at the same time —
**its English translation was already "about"**; only the Chinese half was the
exception.

> The general rule: when you ban a term, sweep the **synonyms already in the
> code** in the same pass. Otherwise half the screen is new and half is old, and
> nothing goes red.

## [1.15.85] - 2026-09-20

### Fixed: headings were invisible white text in exported .odt / .docx

The business report theme draws its heading as white text on a dark banner, and
the Office engine's HTML import **keeps the text colour but drops the paragraph
background** — leaving white on white, so the whole line disappears. PDF is
unaffected (it can paint the background).

**"Markdown to office document" had the same bug**, just unreported. Both tools
now share one override: document formats get dark text with a rule under it,
while the PDF keeps its banner.

> The general rule: **never let legibility depend on a background**. A background
> is the first thing lost in a conversion, and when it goes the symptom is
> "nothing is there", not "the colour looks off".

### Meeting summary: a pasted transcript now takes its title from the background

When text is pasted, the filename is one we invent ("pasted transcript.txt"), so
the exported document was titled "pasted transcript — meeting record" — internal
wording on a document meant to be sent out.

The order is now: **the topic written in the meeting background → the filename →
a plain "Meeting record"**. The download filename follows the same source.

**The title is not guessed from the summary**: the summary is a paragraph, and
truncating it cuts mid-sentence and changes with every run. The background field
is where "Topic: …" belongs.

### Other

- The handoff button to "Markdown to office document" was removed (the result
  panel already offers Word and ODF)
- The LLM settings description for Meeting summary now recommends gemma4:26b or
  larger, with qwen3.8:27b as a measured option when video memory is limited

## [1.15.84] - 2026-09-19

### Meeting summary: clicking a block in the distribution jumps to that moment

The whole row shared one segment number (the speaker's first turn), so every
block jumped to the same place — while what the reader sees is a block at a
particular moment. Each block now carries its own segment number; clicking the
empty track still falls back to the row's first. Hovering shows the time and
segment for that block.

### Meeting summary: the longest topic row no longer wraps its figures

The bar competed with the text for width, so on the longest row the bar filled
the space and the percentage and duration wrapped to a second line. The bar is
now drawn inside a fixed-width track — the text always has room, and every row's
proportion is measured against the same track rather than varying with the
length of its label.

## [1.15.83] - 2026-09-19

### Meeting summary: exports to Word and ODF, with a choice of layout theme

Besides PDF, the record now exports as **Word (.docx)** and **ODF (.odt)**, so it
can be edited further or dropped into a company template.

The download row gained a **layout theme** selector with six options (clean,
GitHub style, academic, book, business report, minimal), defaulting to business
report.

All three formats and all six themes come straight from "Markdown to office
document" — the renderer and the theme definitions are shared. Writing a second
layout engine here would produce something worse, and themes added there would
not appear here.

## [1.15.82] - 2026-09-19

### Fixed: the exported charts had not followed the screen

The previous version merged the speaker bar chart into the table and replaced it
with a "speaking distribution" column, **but exports use a separate set of charts
rendered on the server** — so the page showed the distribution while the PDF still
had the old bar chart, with unlabelled speakers still shown as `unknown`. **The
same thing written in two places always drifts**; this time it drifted between
screen and export.

The server now draws "who spoke when" as well (one row per person, a block
wherever they spoke), and the exported table matches the page's ordering and
column names. When only the analysis is available and there are no segments (the
public API), it still falls back to the bar chart.

### Meeting summary: citations show just the number

The cards are narrow and an item often carries three or four citations — in
"segment 10", two of the three words are decoration repeated on every chip. The
full wording stays in the tooltip and the screen-reader label.

## [1.15.81] - 2026-09-19

### Meeting summary: the length bar now sits close to its topic title

A title and the bar beneath it are one thing; the gap between them made them read
as two rows. The gap came from two places — the row's line height (1.5, meant for
body text, which leaves three or four pixels under the title) and the bar's own
top margin. **Adjusting only one of them is not enough.**

## [1.15.80] - 2026-09-19

### Meeting summary: charts no longer run off the page in the exported PDF

Images were placed at their natural pixel size, and the discussion map is 980px
wide — **the right-hand side was cut off**. `max-width: 100%` does nothing (the
Office engine's HTML import ignores that CSS rule; measured, it still used 980),
so the HTML `width` attribute is used instead.

**Constraining the width alone is not enough**: scaled to the page width, the
discussion map is still taller than one page, and the engine does not paginate an
image — it places it and clips whatever does not fit (measured: the image bottom
was 57pt past the page). Tall charts are now sliced so each piece fits a page.

### Meeting summary: chart headings no longer appear twice

The chart draws its own title, and the surrounding Markdown added a heading with
the same text.

## [1.15.79] - 2026-09-19

### Fixed: the meeting summary PDF export had never once worked

Reported as "it looks great on screen, why is the exported PDF so bad". What came
out was three pages of charts with **not a single line of text**, on pages sized
980×2640 — the charts' own dimensions, not paper.

The conversion helper takes the **destination file** as its second argument and
returns `None`. The old code passed a directory and treated the return value as
the produced file, so that check **never once succeeded** and every export fell
back to charts-only. And since returning `None` is not an exception, the `except`
never fired — **nothing was logged, and the download was always 200**.

The Markdown was also being handed straight to the Office engine, which **does not
read Markdown**. It is now rendered to HTML first (sharing the renderer with
"Markdown to office document"), and the export is a document with a title,
summary, decisions, actions, risks, topics and the speaker table.

**Falling back now always leaves a warning** saying which step failed — a
charts-only PDF and a complete one look identical from the outside: a file
downloaded.

### Meeting summary: the document title no longer carries the file extension

### Topics timeline: "chapters" renamed to "topics"

"Chapter" is borrowed from video and books; nobody says it about a meeting. The
card is now "Topics over time" and the chart below it "How long each topic took".

That chart's heading had also **never been translated**: it was written as
`tr(cond ? 'A' : 'B')`, so the key is computed at runtime and the extractor can
never see it. The heading moved to HTML with each branch translated separately —
which is also where a section icon fits.

## [1.15.78] - 2026-09-19

### Fixed: opening a meeting summary from "My jobs" showed nothing

Coming back with a job id in the URL, the job had already finished — the progress
bar said "done" but **everything below it was empty**, as if the result had been
cleared.

Reading the result needs the upload id, which only exists during the upload
itself; on a fresh page it is empty, so that code **silently did nothing**. The
job records that id, and it is now retrieved before reading the result.

### Meeting summary: the speaking distribution is now part of the table

It used to be a chart on the left and a table on the right, which had to line up
row by row — same order, same row height, same starting point. Row height depends
on the font and the browser, so even measured alignment was off by a few pixels,
and "off by a little" is exactly what misalignment looks like.

Merged, the alignment is guaranteed by structure: a row is a row. The
distribution column is positioned in percentages, so it follows the column width
on its own. Clicking a row jumps to where that person first spoke.

## [1.15.77] - 2026-09-19

### Meeting summary: the speaker chart is now "who spoke when"

It used to be a bar chart of who talked most — which **the table beside it
already says**, so drawing it again was the same numbers in another shape.

It is now a timeline with one row per person: wherever they spoke, there is a
block. Three things the table cannot show are visible at a glance — **who
dominated** (most ink in that row; the share is still readable), **the rhythm**
(spread through the meeting or concentrated in one stretch), and **who was absent
from which part** (the gaps).

It works without timestamps too: the horizontal axis becomes position in the
transcript, which is still the progress of the meeting, just a different scale —
and the chart says which one it is using.

### Meeting summary: the numbers to the right of the charts are now aligned

Percentage, time and turn count were one right-aligned string, so the first
column was pushed around by the width of the last (`9.0%` and `13.8%` differ by a
character, which skewed the whole column). Each is now its own right-aligned
column, in tabular figures.

### Meeting summary: the background field is styled

It only had width, height and font size — no border, corners or padding, so it
looked like a raw browser box.

### Meeting summary: unlabelled speakers no longer show as `unknown`

`unknown` is a placeholder the analysis inserts, not a name. The data keeps it
(statistics, citations and exports all key off it); the screen now says
"Unlabelled speaker".

## [1.15.76] - 2026-09-19

### Meeting summary: the four statistics now fill the row evenly

Their width followed their contents, so the four boxes were different sizes,
bunched to the left and did not line up with the full-width fields below them.
They are now an even grid that fills the row and wraps when narrow.

### Meeting summary: the layout hint no longer has nested parentheses

The layout names already contain parentheses, so wrapping them added a second
level. The hint now reads "Detected: …" without the outer pair.

## [1.15.75] - 2026-09-19

### Meeting summary: the speaker table now has grid lines

Each row carries five numbers, and with only a very faint bottom rule and no
vertical lines the eye could not follow across. Horizontal and vertical rules,
alternating row tint, and a row highlight on hover were added.

### Meeting summary: the table's "share" column is now "share by characters"

The chart beside it is based on **speaking time**, while the table's share has
always been based on **character count** — seeing 23.1% and 21.8% next to each
other for the same person looked like one of them was wrong. The column heading
now says which it is.

## [1.15.74] - 2026-09-19

### Fixed: downloads in Meeting summary blocked the whole site

PDF / chart PNG / ZIP are **generated when you press the button** (rasterising
the charts, zipping), and that CPU work ran directly on the web server's event
loop — while one person downloaded, **nobody else could even open the home
page**.

It now runs on a worker thread. The existing "endpoints must not block the event
loop" check cannot see this shape (the heavy work is inside called functions), so
this endpoint is pinned in that test's explicit list.

### Meeting summary: download buttons now say "Generating…"

They were plain links, so nothing on screen changed until the file arrived — it
looked like nothing happened and people pressed again. The button now switches to
"Generating…" with a spinner and blocks repeat clicks until the file is ready.

They also fetch the file themselves now, so server errors are shown as a message
— previously the error page was saved as if it were the file.

### Meeting summary: the chapter timeline spine was broken into segments

The spine is drawn per row and only stretched to the row's *content* box, so the
row's vertical padding left a gap between every pair of rows. The overhang is now
tied to the same variable as the padding, so changing one cannot break the other.

## [1.15.73] - 2026-09-19

### Meeting summary: the upload card now follows the actual flow

The background field used to sit **below** the submit button, so you only saw it
after pressing. The order is now "paste the transcript → background → button",
with the button last.

The button is renamed from "Use this text" to "Parse transcript": what it does is
parse the content into segments and speakers (pressing it shows the segment
count, speaker count and the first few lines), and the label should say so.

## [1.15.72] - 2026-09-19

### Meeting summary: the background field looked like a separate block

It was already inside the "1. Upload transcript" card, but it had its own border
and background, so it read as **a card inside a card**. The box is gone; a single
divider line now separates it — the same visual weight as "or paste the
transcript directly", so it clearly belongs to the same card.

## [1.15.71] - 2026-09-19

### Meeting summary: you can now supply meeting background

There is a new optional field below the upload area (collapsed by default) for
**information that is not in the transcript** — the topic, date and place,
attendees and their roles, terminology, or anything else you want to say.

It makes the analysis more accurate: knowing who manages and who does the work is
what lets "please ask X to handle it" resolve to an owner, and knowing your system
codenames and abbreviations keeps the wording intact.

**The background never becomes a source of items.** What you write there did not
happen in the meeting; if it could produce a decision, this tool's guarantee that
every item traces back to the transcript would be broken — invisibly. Three
defences:

1. The prompt says so, and **our rules bracket the user text on both sides** —
   what you type could read like an instruction, so we must have the last word.
2. **Citation verification**: an item must point back to transcript segments, and
   the background is not part of what is compared.
3. **Anything that closely matches the background and matches it noticeably
   better than the transcript is dropped.**

The third was added while writing the tests — the second turned out not to be
enough on its own. What people put in the background (an agenda, a topic list) is
exactly what the meeting was about, so a decision copied from the agenda still
overlaps the transcript enough to pass the threshold.

> This does not catch a sentence the model builds by blending background and
> transcript wording. The background is still an input to treat with care.

The public API accepts it too (`context`, up to 4000 characters).

## [1.15.70] - 2026-09-19

### Meeting summary: a pass over the charts and layout

- **Chapters are now a timeline**: a spine down the left, a dot per chapter, and
  a length bar next to each title. The old two-column "time | title" table left
  most of the width empty; the same data now also shows which part ran longest.
- **Hovering a bar lights up its legend row and dims the others.**
- **The speaker chart and its table now sit side by side**, stacking when narrow.
- **The post-upload statistics are now tinted blocks** (segments / speakers /
  characters / duration).
- Legend text is one size larger.

### Fixed: chart text was sometimes too large, sometimes too small

Both reports arrived the same day, and **they had the same cause**: the width a
chart was drawn at did not match the width it actually occupied, so CSS scaled
the whole chart — text included.

Too large came from the discussion map being **still hidden when its width was
measured** (measured 0, fell back to a default, then got stretched 1.7x); too
small was the reverse.

Charts are now drawn in real pixels, and **measured again after drawing**: if the
actual width differs by more than 2%, the chart is redrawn once at the real
width. There are many ways to mismeasure (the container still opening, fonts
still loading, a scrollbar taking a few pixels), so rather than plugging each
one, the chart checks itself in the mirror.

### Fixed: hovering the items on the right of the discussion map did nothing

The chapters on the left highlighted; the items on the right did not. Those boxes
have a white background, and the effect was an opacity change — **which is
invisible on a white page**. It now changes brightness, which shows on both white
boxes and coloured bars.

### Fixed: an extra "Download .json" button on the progress bar

The meeting summary result panel already has a full row of download buttons (PDF
/ Markdown / chart PNG / ZIP / JSON / hand off), and the shared progress bar
added another one labelled "Download .json", which looked like something else.
Tools can now tell the shared progress bar not to show that button.

Spacing was also added between the progress bar buttons and the card below them
(this applies to every tool with background jobs).

## [1.15.69] - 2026-09-19

### Fixed: pressing "Stop" did not look like it stopped

After pressing stop the timer did freeze, but **the progress bar stayed where it
was, the status still read "extracting 4/5", and the "you can close this page"
note was still showing** — indistinguishable from a hung job.

Stopping only did two things: stop polling and tell the server. The code that
paints the "stopped" state was **on the polling path** (it ran when the server
reported the job as cancelled), so once polling stopped it could never run.

Pressing stop now immediately sets the status to "Stopped", greys out the bar and
takes down the "you can close this page" note. **The elapsed time is kept** — how
long it ran before stopping is useful information. This is the shared progress
bar, so every tool with background jobs benefits.

### Fixed: the paste box in Meeting summary printed `&#10;` in its hint text

The hint text of the "paste the transcript directly" box showed a literal
`&#10;` instead of a line break.

The hint goes through the translation helper, and **translated text is
auto-escaped** — the `&` became `&amp;`, so the browser displayed the six
characters. All three language catalogues had copied the same markup, so all
three were affected.

## [1.15.68] - 2026-09-19

### Meeting summary: charts are now drawn in the browser and are clickable

The three charts (discussion structure, chapter share, speaker share) used to be
images rendered on the server, so **nothing in them could be clicked** — even
though the whole point of this tool is that every item traces back to the
transcript. They are now drawn in the browser:

- **Click any part of a chart and the transcript jumps to that segment and
  highlights it** (the same logic the citation buttons use)
- They are drawn to the width of their container and redrawn when the window
  resizes, so there is no large empty gap on wide screens
- Exported PNG / PDF and the images embedded in the exported Markdown are still
  rendered on the server

### Meeting summary: the right-hand side of the discussion map is no longer empty

Decisions and action items cluster near the end of a meeting, so out of ten
chapters only three typically have anything attached — leaving the right half of
seven rows blank, which looked like a broken chart. Those chapters genuinely
produced nothing, so rather than hiding them or inventing a node, they are now
drawn as a full-width bar with "no decisions or actions" noted on the right. You
can tell at a glance which topics were discussed without producing an outcome.

### Fixed: chapter share said there were no timestamps when there were

In a plain-text transcript, timestamps are `16:19`-style markers at the start of
a line, and **not every line has one** (the header block, lines like
"(brief silence)", and the final segment never get one). Chapter times were
computed from "the start of the first segment" and "the end of the last segment",
so if a chapter happened to begin or end on such a line, that whole chapter had
no time — and a single chapter without a time made **the entire chart fall back
to segment counts**, displaying "this transcript has no timestamps".

Chapter times are now taken from the earliest and latest timestamps that are
actually present in the chapter.

### Meeting summary: the transcript is fully expanded, card headings stand out

The transcript used to be trapped in its own scrollbar, so jumping to a citation
meant scrolling inside a small window and the page scrollbar never reached the
later content. It is now fully expanded. The headings of the four cards
(decisions, actions, risks, open questions) are larger, have a background tint
and a coloured left edge, and the counts are shown as badges.

### Line endings in Windows batch files now have a guard

`setup-python.cmd` (the batch file that sets up the Python environment during
Windows installation) **must use CRLF line endings**. With LF endings, cmd.exe
fails token by token, so anyone installing from the tarball on a machine
without git gets a failed install. This was fixed in v1.12.8 / v1.12.10 with
three separate safeguards (normalise before running, convert the file to CRLF,
declare the rule in `.gitattributes`) — but **none of them had a test**.

`tests/test_script_line_endings.py` now checks that every line of `.cmd` /
`.bat` ends in CRLF, that `.sh` files contain no CR at all, and that the three
`.gitattributes` rules are still there (that is what protects people who clone).

People who clone never see this class of problem: checkout converts the file
back to CRLF, so it always looks correct on a development machine.

### Other

- The deployment tarball is now built from an allow-list and verified after packing
- Housekeeping in internal tools and guard tests

## [1.15.67] - 2026-09-18

### Fixed: all four download buttons in Extract text failed

If the uploaded filename had a space before the extension (`minutes .pdf`, which
is common when the name is copied from a web page), the TXT / Markdown / Word /
ODT downloads **all failed**, and "Save to workspace" broke with them. The
browser saved the error message as a file; the save dialog showed `txt.json`.

The cause was that the file was **written under the original name but read back
with the surrounding whitespace stripped**, so the two paths did not match. The
message said "expired", which looks like the file was cleaned up rather than a
filename mismatch.

Downloads now also carry a plain ASCII fallback name: when only the UTF-8 name is
sent, some browsers fall back to the last part of the URL.

### Extract text: one-click copy on the preview

It copies what the preview shows (the first 5000 characters); the button says how
much was copied and points to Download TXT for the whole thing.

### Meeting summary: a lot changed in this release

**Transcript layout is detected automatically, and you can override it.** Eight
common layouts are supported: a speaker header on its own line with the text
below, one utterance per line, times at the start or in brackets, bullet-led
lines, half-width colons and so on. The detected layout is shown, and you can
pick a different one when the guess is wrong.

**You can paste a transcript directly** instead of saving it to a file first.

**Very long turns are split.** In committee records one person often speaks for
minutes at a time, which used to be a single segment of twenty thousand
characters: every citation pointed at "segment 1", and clicking it showed a wall
of text, which is no citation at all. Segments are now capped at 400 characters
(measured: a 26,907-character record went from 4 segments to 78).

**Long meetings no longer go wrong.** What was sent to the model used to grow
with the length of a turn, and once it exceeded what the model can read it was
**silently truncated** — it looked successful while only part of the text had
been read. What is sent is now bounded, and the review pass runs in batches so a
failed batch costs only that batch.

**The mind map, speaking share and chapter share are now drawn on the server**,
so the page, the Markdown and the PDF all use the same image. The mind map is
**assembled, not generated**: every node comes from an entry whose citation was
verified, so each box points back to the transcript.

**"Who spoke how much" works without timestamps** (turns and characters); the
whole section used to disappear. Chapters likewise vanished without timings.

**New exports**: PDF (the whole minutes), charts as PNG, and a ZIP with the
Markdown plus images. The Markdown download embeds the images, so they survive
being handed to "Markdown to office document".

**Fixed**: the "Send to Markdown to office document" button did nothing. Every
section and card now has an icon, and the export buttons are spaced away from the
content below them.

### The home page says how many tools there are

"An integrated PDF / Office document platform, **49** tools" — the number is
computed, so it follows along when tools are added.

## [1.15.66] - 2026-09-18

### Fixed: turning on "enforce API tokens" froze the web interface (issue #52)

With enforcement on, the `/api/` requests the web interface makes for itself were
all rejected with 401: progress polling, cancelling a job, notifications, the
inbox, the workspace list. **The pages themselves still opened, and jobs really
did finish in the background**, so it looked like "the progress bar is stuck" or
"the button does nothing", nothing like a settings problem.

The check recognised only a **hard-coded list of paths** (the admin area and two
preview endpoints); everything else counted as an outside API call. It now looks
at whether the request carries a logged-in **session** instead: if it does, the
normal permission checks apply; only requests without one (scripts, `curl`) are
blocked. There is no list to maintain any more, so new endpoints cannot be
missed.

The setting's description was also corrected: **it has no effect while
authentication is off**, because with no identity required anywhere there is no
way to tell the web interface apart from a script, so both are let through. To
actually restrict outside callers, turn authentication on first. The old wording
said calls "are rejected with 401", which was not true in that mode.

## [1.15.65] - 2026-09-18

### New tool: meeting summary

Turns a meeting transcript into a **summary, decisions, action items, risks,
open questions and chapters**.

**Every entry points back to the segment it came from and who said it** — click
it and the transcript jumps to that line. This is not decoration: minutes get
used as the record of what was agreed, and **a decision with no source is worse
than no decision at all**. Anything that cannot be found in the segment it
claims is dropped, and the result page tells you how many were dropped.

Accepted transcripts: subtitles (`.vtt` / `.srt`), transcript JSON, plain text
(`.txt` / `.md`), Word and ODF (`.docx` / `.odt`). For plain text, one utterance
per line; a `Name:` prefix is understood, and so are leading timestamps.

**After the upload and before the analysis starts you see what was parsed** —
segment count, speakers, the first few lines. If the speakers came out wrong you
find out before spending the minutes, not after.

When timestamps are present it also computes the **speaking share** directly from
them (overlapping interjections counted once), not estimated. **Without
timestamps that chart and the chapter timeline simply do not appear**, and the
page says why — a guessed number would be worse than none.

Also: the analysis can be stopped; it finishes even if you close the tab, and
"My jobs" takes you back to it; the Markdown download can be handed straight to
"Markdown to office document" for layout. Requires LLM to be enabled in the
admin area.

Tool count 48 to 49.

## [1.15.64] - 2026-09-18

### New `/readyz`: "the service is alive" and "nothing is missing" are two questions

When a tool failed to load — usually a missing dependency — it left one line in
the log and then quietly disappeared: one fewer entry in the sidebar, 404 on its
URL, and `/healthz` still answering `{"ok":true}`. Administrators had nowhere to
see it.

`GET /readyz` is new (no login required, same as `/healthz`):

- Tools missing → **200 with `degraded: true`**; the remaining tools still work
- Data directory not writable, or the database unreachable → **503**, because
  that is a service that genuinely cannot do its job
- It never returns module names, exception text, or file paths — the endpoint is
  public

**Which tools failed and why** is shown at the top of the admin **System status**
page (administrators only), and takes up no space at all when nothing failed.
After installing the missing package, restart the service for the tool to load.

`/healthz` is unchanged and deliberately ignores tool loading: service managers
use it to decide whether to restart, and restarting in a loop because one tool is
missing is worse than the problem. For the same reason `/readyz` does not return
503 for missing tools — this product runs as a single web process, so marking the
only instance unhealthy would show visitors a site-wide error page.

Monitoring setup is documented in `OPS.md`. This came out of an external source
code audit.

## [1.15.63] - 2026-09-18

### Markdown to office: runs in the background and can be stopped

It used to be a synchronous request — all you could do was watch "converting…"
with no way to stop, and closing the tab threw the work away.

There is now a progress bar and a **Stop** button, the job finishes even if you
close the page, and "My jobs" → **Open** reconnects to it. Stopping takes down the
whole tree of conversion processes (the mechanism added in v1.15.62) rather than
just hiding the progress bar.

Finished jobs now also offer a download in "My jobs", named after the title you
entered.

## [1.15.62] - 2026-09-18

### Fixes a regression from v1.15.61: code blocks ran off the page

To put space between code and its frame, the previous release wrapped code blocks
in a single-cell table. The result was that **long commands stopped wrapping and
the whole block was pushed past the right edge of the page, cutting content off**.

Content being cut off is far worse than text sitting against a frame, so that
approach has been reverted. Three ways of getting the spacing were measured
(`padding`, an outer container, a single-cell table) and the conversion engine
honours none of them. Code blocks are now an indented tinted band with no frame —
with no frame there is nothing for the text to touch, and the indent separates
code from prose clearly.

Table borders are unaffected and remain as added in the previous release.

### Cancelling a job now actually stops the work

The stop button in "My jobs" and on tool pages only changed the status to
"stopped". A conversion spends its minutes waiting on an external program, so the
cancellation checkpoints in our own code were never reached: the screen said
stopped while **the server finished the conversion anyway**, still using CPU and
memory.

Cancelling now stops the entire tree of external programs that job started. This
applies to every tool that shells out (text recognition, all conversions).

Before stopping anything the process identity is verified — process numbers are
reused on a long-running service, and without the check there is a chance of
stopping something unrelated.

## [1.15.61] - 2026-09-18

### Markdown to office: tables now have visible borders, code no longer touches its frame

Converted tables had no borders and awkward column widths, and text in code blocks
sat right against the surrounding frame.

The conversion engine supports only a narrow slice of CSS: `border: 1px solid …`
draws **nothing at all**, and `padding` on a code block is treated as an indent
(frame and text both move right, with no gap between them). Switching to HTML
presentational attributes makes it reliable — table borders now measure 0.75pt
(clearly visible) and code blocks have roughly 6pt between the text and the frame.

The same document also lost a page (27 to 26), and the two overlapping sets of
borders — one from the attributes, one drawn as hairlines from CSS — no longer
fight each other.

## [1.15.60] - 2026-09-18

### Markdown to office: no more near-empty pages in the PDF

Converted PDFs contained pages holding only a line or two, with tables broken so
that a single row sat alone on a page. The cause: the HTML was converted using
**web-view layout** instead of document layout. On the same file that meant
36 pages with 3 near-empty ones; with document layout it is 25 pages with none,
and the median characters per page went from 687 to 1029. Table column widths
are better too.

The other two outputs (.docx / .odt) already used document layout — only the PDF
path had been missed.

### Markdown to office: choose which formats to produce

Every run used to produce PDF **and** DOCX **and** ODT. Each format runs the
conversion engine once, the engine is serialised, and each run is capped at
120 seconds — so on a busy host a few tens of KB of Markdown could take over two
minutes and then fail, while the screen promised "10-30 seconds".

There is now a format picker, defaulting to PDF only. The page also states that
previews are rendered from the PDF, so without PDF there is no preview. The
public API still produces all three when no format is given, so existing calls
are unaffected.

### Markdown to office: syntax highlighting in code blocks

Code blocks are coloured according to the language tag, and inline code keeps its
colour. The **Minimal black & white** theme deliberately stays uncoloured — a
completely neutral look is the whole point of that theme.

### A conversion that timed out did not actually stop

On timeout only the outer launcher was stopped; the process actually parsing the
file survived, kept burning CPU, and never exited on its own. Because conversion
processes are deliberately given low priority (to keep the web UI responsive),
each leftover process made every later conversion slower and more likely to time
out. The whole process tree is now stopped.

This affects all seven conversion paths (office to PDF, office to image, Markdown
to office, and so on).

### The timeout message no longer points in the wrong direction

When a Markdown conversion timed out, the message said the file might be damaged
and suggested asking the sender for a PDF — but the intermediate file on that
path is generated by this tool itself and has nothing to do with what was
uploaded. It now explains that a timeout usually means a busy host or a large
document.

## [1.15.59] - 2026-09-17

### The `jtdt-reform` engine now reports progress page by page, like the other two

In `pdf-to-office`, `pdf2docx-refine` and `jtdt-layout` had reported per page for
a while; **`jtdt-reform` only ever reported twice** — at the start and at the end.
Large files take minutes, during which the screen does not move at all, and
"nothing appears to be happening" is the hardest symptom to diagnose: users
assume it has hung.

Both phases (reading the PDF, writing the document) now report per page. Measured
on a 20-page file: 41 updates, progress 0.07 → 0.95.

> **Progress is a side channel, not the output**: if the reporting callback
> itself raises, the conversion must still succeed. There is a guard that
> deliberately makes the callback blow up.
>
> **Written-down status needs checking too.** Our own notes claimed all three
> engines reported per page; reading the code showed only two did. What is
> written down becomes what the next person believes.

## [1.15.58] - 2026-09-16

### ⚠⚠ Document diff: inserting one page made every page after it look different

Pages were paired **by index** — old page N against new page N. Measured on a
20-page document with one page inserted at position 3: **only 2 of 21 pages
paired up, the other 19 became "whole page removed plus whole page added"**.
The user sees "the entire document changed" when in fact one page was added.

Pairing now uses **the same structure as the line diff, one level up**: a
sequence comparison anchors the unchanged pages one to one, and changed pages
sitting between anchors line up by position. The same document now reports only
the inserted page.

> **No fuzzy similarity is needed** — unchanged pages are byte-identical, so
> they make perfectly good anchors. A similarity score would only add a
> threshold to tune.

> After an insert the two page numbers drift apart, so the heading now says so
> ("old page 4 ↔ new page 5"), and the page view fetches images by the **real**
> page number rather than the row number.

### Teaching a field name now tells you what else that name maps to

The "learn this" button in the form filler writes into the **site-wide** field
name map. It used to just say "learned", so the user had no idea what they had
affected.

> **It was not changed into "refuse on conflict".** Reading how the lookup index
> is built showed the premise was wrong: **one label mapping to several fields is
> deliberate**. Taiwanese forms often have a single cell covering two things at
> once, so two fields each tick their own options from it. Refusing would
> silently break ticking on those forms **and no test would go red**. So the
> consequence is reported, not blocked.

## [1.15.57] - 2026-09-16

### Japanese de-identification gained driver's licence and health insurance numbers

Japanese documents were already covered (My Number and corporate number with
their check digits, phone, postcode, address, name). This release adds the two
remaining categories from the original plan.

**Both are recognised only as "label plus value", and only the value is masked:**

* A **driver's licence number is 12 digits — exactly as long as a My Number** —
  and there is no published check-digit algorithm to validate it. Without the
  label, any 12-digit run would have to be guessed as one or the other and
  **both guesses would be wrong, while the screen says "processed"**.
* **Health insurance numbers vary by insurer**: no nationwide length, no check
  digit. That pattern additionally requires a `番号` field to follow, otherwise
  an ordinary `記号` field in a normal document would match.

> The hyphen inside the value has to use the "any kind of dash" character class:
> what PyMuPDF extracts is a non-breaking hyphen `U+2011`, not `-`. The test
> fixtures hard-code `\xa0` and `\u2011` rather than characters typed by hand,
> because otherwise the test passes while real files match nothing.

> The false-positive corpus is still part of the acceptance: a Japanese document
> full of part numbers, order numbers and ISBNs must produce **zero** sensitive
> hits. Checking only that something is detected would also pass a pattern
> loosened until it matches everything.

### Hidden-content scanning now parses in a separate process (external audit F04)

The audit said "PyMuPDF upstream does not support multithreading". **Taking that
literally and adding locks solves the wrong problem**: we do not share
`Document` objects (every job opens and closes its own), the report itself never
reproduced a crash, and locks scattered across tools can neither be shown to
cover every entry point nor contain a parser crash.

What is worth doing is the **blast radius**: a segfault inside MuPDF's C code
takes down the **whole service process**, and with it every job in flight and
everyone currently using the site. Isolated, only that one request fails.

**Only the hidden-content scanner for now.** The criterion is "least trusted
input, widest damage if it falls over", and that tool exists precisely to answer
"this file might be dangerous, check it". Everything else is unchanged until
this has run in production for a while.

> **`subprocess`, not `multiprocessing`**: spawn makes the child **re-import the
> parent's `__main__`**, and we start the service with `python -m app.main` — so
> every isolated call would rebuild the entire service.

> **⚠ The cost measured while planning was wrong.** It measured "spawn + import
> PyMuPDF + open the file" at 385 ms and **left out our own import chain**. The
> first real measurement was **1.6 seconds**, because importing
> `app.tools.…` triggers the tool package's `__init__.py`, which pulls in the
> web framework and the settings chain (1,375 ms for that line alone).
> Extracting the scanner into a module that **imports only PyMuPDF**, and
> loading it **by file path**, brought the fixed cost down to about **0.5 s**.
> A guard now watches that module's import list.

> **Isolation is a safeguard, not a feature**: if the subprocess cannot start,
> the tool falls back to doing the work in-process. The exception type for a
> broken file is preserved across the process boundary too — losing it would
> turn a 400 ("your file is broken") into a 500 ("the server is broken"), and
> users would retry forever.

> **Both directions are tested**: the same crashing code must kill the parent
> when not isolated and must not when it is. Checking only "it survived with
> isolation" proves nothing — the crash might not have happened at all. A
> further test **posts a real request** to confirm the endpoints actually go
> through isolation: testing only the internal helper stays green even if the
> endpoint is reverted (the same hole the audit's F06 recorded).

## [1.15.56] - 2026-09-16

### The document diff gained a page view that marks the changes on the page itself

Until now there was only the text view: two columns of text showing *what*
changed but not *where on the page* it changed. There is now a toggle:

* **Text view** — exactly as before, untouched.
* **Page view** — the original page rendered on both sides, with the
  differences outlined in place. Red = removed, green = added, yellow = changed.

Both views share the same comparison result, so switching costs nothing.
Office files work too: they are already converted to PDF before comparing, so
the page view shows the **converted** layout.

> **The text comparison itself did not change.** Lines still come from
> `get_text("text")`; coordinates are read separately from `rawdict` and matched
> by "the Nth non-empty line". Across 30 real samples that matched on
> **101 of 101 pages**; a page that does not match gets **no boxes at all** —
> missing boxes only lose a feature, boxes in the wrong place mislead.

> The per-character ranges inside a changed line were **already being computed
> and then thrown away**. Keeping them is what makes "these characters changed"
> markable. Chinese has to be character-level: `get_text("words")` returns a
> whole line as one "word" when there are no spaces, so a one-character edit
> would outline the entire line.

### Rotated pages nearly got every box in the wrong place

Text coordinates and the rendered page are not in the same space. Measuring the
ink coverage inside the box, on the same page at each rotation:

| Rotation | Raw coordinates | Times `page.rotation_matrix` |
|---:|---:|---:|
| 0 | 22.7% | 22.7% |
| 90 | **0.0%** | 22.7% |
| 180 | **0.0%** | 22.6% |
| 270 | 4.6% | 22.6% |

Without the matrix the boxes land on blank paper while the page itself looks
perfectly normal, so nobody would notice. The acceptance criterion is therefore
**ink coverage inside the box**, not "a box was drawn": all four rotations are
checked, and mutation-verified (drop that one line and 90/180/270 fail).

### When there is no text layer, it says so instead of implying "no differences"

Scans, text converted to outlines and PDFs with a broken character map cannot
give coordinates. The page view now says so on that page and points at OCR.
"No boxes" and "this page did not change" look identical, so the sentence
cannot be left out.

### One piece of setup copied eight times; the eighth copy broke a whole test

The headless-browser tests each had their own copy of "find the browser" and
"pick a directory the browser can read". The new copy only compared path
strings instead of reading the file, so it could not tell that Ubuntu's
`/usr/bin/chromium-browser` is a **shell wrapper** around the snap build. The
fixtures were written where the browser could not read them; the upload
"succeeded", the filename even showed up, and only the submit failed — so the
whole test **skipped**, which looks exactly like success in pytest output.

It is now one shared `tools/browser_probe.py`, used by all eight, with a guard
against a ninth copy.

## [1.15.55] - 2026-09-16

### Tool renamed: "Document straightening" is now **Scan cleanup**

The old name had two problems. It **collided conceptually with "Page rotation"**
(both sound like they straighten a page, but that one only turns the whole page
90/180 degrees), and it did not say what the tool actually does: **crop, deskew
and even out the background shading**. The Japanese name had the same fault.

The new name puts it in the same family as "Scan merge". Japanese: `スキャン補正`.

> **The tool id and API path `doc-straighten` did not change** — changing an id
> means moving built-in roles, a database migration and every existing install's
> permissions, whereas this is only a display name. The old name stays in the
> search keywords so anyone who types it still finds the tool.

### The "copy all" buttons in both diff tools were never translated

`Copy all (old)` / `Copy all (new)` and their confirmation toasts showed Chinese
in the English and Japanese UI. They were written as an interpolated template
literal starting with a Jinja icon call, so the sentence as a whole could never
be looked up and nobody had wrapped it. **They only appear once a comparison has
finished**, which is why page-by-page scanning never saw them.

### Missing Office engine returned 500 from the document diff; it is now 503

500 means "the server is broken": users retry and monitoring fills with false
alarms. A missing soffice is a **deployment** problem, and the message should say
what to install. The project already had `OfficeUnavailableError` and a global
handler, but this tool caught it with a bare `except Exception` and wrapped it in
a 500, so **the handler never saw it**.

> The guard actually posts an Office file with soffice made unavailable and
> checks the status code, rather than grepping the source for "503".
> Mutation-verified: revert the fix and it returns 500 and the guard fails.

## [1.15.54] - 2026-09-16

### Uninstalling could leave an undeletable Start Menu folder behind

The folder name is also a path, and the registry only remembers **the one the
last install created**. So "install under language A, install again under
language B, then uninstall" orphans A's folder: it survives the uninstall and
nothing will ever remove it.

**This actually happened on the test machine** (2026-09-16): after uninstalling,
`Jason Tools Document Toolbox` was still there, with two shortcuts pointing at
an install directory that no longer existed.

Both sides are fixed:

* **Uninstall** now tries **every language's folder name** after the one in the
  registry (plus the pre-v1.15.30 hard-coded Chinese name), each behind the same
  "must live under `$SMPROGRAMS\`" check — we do not touch what is not ours.
* **Install** reads back the previously recorded folder and removes it when it
  differs, so reinstalling under another language does not leave two entries
  with no way to tell which one is live.

Verified on the machine: 2 folders before → install the fixed build → uninstall
→ **0**, with all four SQLite files byte-identical in size (user data intact).

> The guard walks the **declared language list**: every declared language must
> have its `SM_FOLDER_xx` *and* appear in the cleanup list — adding a fourth
> language turns it red first. Mutation-verified in five directions.

### Ternary expressions were only half-wrapped: 16 spots showed Chinese in the English / Japanese UI

When wrapping JS strings for translation we deliberately **do not wrap a whole
ternary** (the lookup key would then be computed at run time and never match,
silently). The correct form is to wrap **each branch separately**. The tool
that wraps strings automatically skips ternaries entirely, so "only one half
got wrapped" was a state nobody was watching.

A scan found **16 of them**, every one of which shows Chinese in the English
and Japanese UI: admin save results where only the failure half was
translated, right-click menu headings in `pdf-fill` / `doc-deident`, the
word-count reading time once it goes over an hour, the "N failed" tail in font
management, and the queue message in OCR that only appears after three
seconds.

**Page-by-page scanning cannot see this class** — those strings only appear
after a click, a toggle, or a value crossing a threshold. New guard
`tests/test_ternary_branches_go_through_tr.py`, mutation-verified four ways.

> **A scanner must not pair quotes with a regular expression.** The first
> version used `'[^']*'`; on a line mixing quoted strings with a template
> literal it paired two unrelated quotes and swallowed everything between
> them, giving both a false positive and a miss. Walking the line character by
> character (and splitting template literals, because `${…}` is code, not
> text) found 3 more real misses and removed the false positive.

### Company-profile field labels were untranslated in the English / Japanese UI

The company card on the `pdf-fill` page has **50-odd field labels** that never
had translations. The section headings were worse: the translations were
already in the catalogue, the display side had simply forgotten to call
`tr()`.

The display side now translates; **labels the user renamed are untouched**
(a miss returns the string as-is), and the editable label input on the admin
page is deliberately left alone — translating it would rewrite the user's own
field names on the next save.

### The demo-data seeder kept a second copy of those labels, and it had drifted

`tools/seed_demo_data.py` — which produces the screenshots we publish — kept
its own field-label map. It had drifted from the shipped one in **6 fields**,
two of them into mainland Chinese wording, which is on our own banned list;
the terminology guard simply was not scanning that directory. Both are fixed:
one source of truth, and the guard now covers it.

## [1.15.53] - 2026-09-16

### Scan an instance that actually has data, and tell data from interface

Every per-page scan so far ran against an **empty** instance, so "tables that
only appear once there is data" (the third square in TEST_PLAN §0.6) had never
been looked at. This one seeds demo data first: 71 findings in English, 9 in
Japanese.

**Read one by one, not one of them is a missing translation** — they are all
the user's own data: the field names and values on the company card, group
names and descriptions, the names of stamps, signatures and watermarks, every
cell on the company settings page.

Those are now marked `data-i18n="skip"`. **The company card in the form filler
matters most**: those field names are editable by the administrator *and* are
what the matcher compares against the Chinese labels printed on Taiwanese
vendor forms — translating them would make the matching fail silently.

> **"Lots of findings" is not "lots of missing translations."** Without that
> distinction nobody reads the report next time — and the real misses get
> ignored along with the noise.

## [1.15.52] - 2026-09-16

### Display attributes set from JS need `tr()` too

`title` / `placeholder` / `aria-label` / `alt` come from two places: the
template (`title="…"`, translated at render time — fine) and **JS at runtime**
(`el.title = '…'`). The second is invisible to the template scan, and because
it is not a text node the per-page scan only sees it if you hover.

Four were left, all interpolated template literals (`` el.alt = `第 ${n} 頁` ``)
— the same shape as last version's dialogs. Guarded by
`tests/test_js_set_attributes_go_through_tr.py`.

> The floor is **measured** (10 in practice, half of that as the minimum) —
> a round number would either always hold or fail on every edit.

## [1.15.51] - 2026-09-16

### The result area after you submit was still Chinese in English and Japanese

TEST_PLAN §0.6 says in as many words that the per-page scan **cannot see the
result area after a submit** — and nobody had ever scanned it. This release has
the **screenshot tool** scan it on its way past: it already uploads a real file,
submits it and waits for the result before taking the picture, so **what is on
its screen is exactly the missing square**.

The first run found leftovers in **11 tools** (the watermark preview status
line, the PDF editor's loaded message, the OCR upload message, the per-sentence
translator's loaded message, redaction's summary chips, the before/after titles
and page numbers in PDF-to-Office, the per-page download tooltip…).

Every one of them was the same shape: **the sentence is interpolated**
(`` `第 ${n} 頁` ``), and an interpolated sentence never matches a catalogue
key. Some were not wrapped in `tr()` at all.

> **`tr()` gained a fallback**: on a miss it replaces runs of digits with `{0}`,
> `{1}`… and looks that up, then puts the numbers back. That also makes
> **server-produced job messages** translatable (`完成（3 份）` is built on a
> background thread, where there is no request and so no way to know the
> viewer's language). A second miss returns the string unchanged, so the worst
> case is exactly today's behaviour.
>
> **Its boundary is a test too**: it only handles sentences whose variables are
> all numbers. `已上傳 a.pdf（6379.1 KB）` starts with a filename — that one has
> to be wrapped where the sentence is built.

> **Redaction's pattern names were half-fixed**: v1.15.47 fixed the label on the
> result row and **missed the summary chips**. One family, one sweep — again.

### Some dropdown options must NOT be translated

Whatever you pick in the personal-data stamp's "purpose" list is **printed on
the stamp verbatim**. Translating it would mean picking one thing and printing
another — and it would look completely normal.

That list is marked `data-i18n="skip"`, and **the scanner now honours skip on
`<option>` too** — it did not before, so those entries would have sat in the
report forever as false positives, and a report full of false positives is a
report nobody reads.

> **Marking the `<select>` was not enough**: every dropdown on the site is a
> custom widget, and **the list you actually see is a separate set of nodes**
> outside the native `<select>` — `closest()` never found the marker, so only
> the hidden native options were skipped. The widget now passes `data-i18n`
> through to the wrapper it builds.

Account and group names in the permission matrix are marked as data too (they
are the user's data; Chinese is correct there), while tool names, role names
and the word "group" **should have been translated and were not**.

### Dialogs: a static scan found 15 calls with no `tr()`

TEST_PLAN §0.6 files dialogs under "manual pass only" — but **a static scan
sees every branch**, including the ones only an error reaches. Three shapes:
an interpolated template literal, a plain string nobody wrapped, and — the
nastiest — **a ternary with only one side wrapped**, which looks handled.

The guard's criterion is "strip `tr('…')` out of the first argument; no Chinese
may remain". Checking "does it start with `tr(`" would flag
`cond ? tr(a) : tr(b)` and `err.message || tr(c)`, which are correct.

> **My own comment fooled my own scanner** (again): the note I wrote next to
> the fix quoted the wrong form as an example, and the guard flagged it.
> Strip comments first — and for templates take only the `<script>` blocks,
> since handing a whole HTML file to a JS comment stripper treats prose as code.

## [1.15.50] - 2026-09-16

### Memory admission now books what it just dispatched (audit F06)

The dispatch loop can start several jobs in one pass, and each one reads the
**current** free memory — but the one before it **has not allocated yet**, so
the second sees a stale number and both are admitted. At the default
concurrency of 2 that is one extra job (800 MB); an administrator who raises it
to 4–6 is off by 2.4–4 GB, which is exactly the OOM budget.

A dispatched job now holds a reservation for its estimate, and the reservation
**expires after a settle window (30 s)** — by then its real memory shows up in
the free-memory reading, and counting both would be double counting.

> **"Skip the check when nothing is running" stays as it was**: that is a
> deliberate trade-off (otherwise the queue never unblocks when memory is
> tight), and the code already says so. This only adds the half that was
> missing.

### It now says so when you run it with multiple workers (audit F11)

`OPS.md` has always said not to, but **nothing enforced it** — and the symptoms
(the same job dispatched several times, running jobs turning into "interrupted")
look nothing like a configuration problem.

Startup now logs an ERROR saying **how it worked that out** and **what will go
wrong**. It logs and continues: refusing to start would be worse than the
problem.

> The test was derived from uvicorn's source (it spawns workers with
> `multiprocessing.get_context("spawn")`, so a worker's `parent_process()` is
> not `None`), not guessed. `WEB_CONCURRENCY` is checked too.

### Other

* No source file may contain an invalid escape sequence. Python 3.12 only warns;
  **3.14 makes it a `SyntaxError`**, which would break collection for the whole
  suite. Both real cases were inside explanatory docstrings.
* The two installer guards now share one rule for which languages may
  legitimately contain Han characters.
* The per-page i18n scan gained `--reveal`: it expands panels and sections that
  are **already in the DOM but not displayed** before scanning, covering the
  "you have to open it first" category. Measured: English and Japanese, 82
  pages each, **zero** extra findings. **Nodes created at runtime** (a dialog
  that only exists once you click) are still outside what it can see.
* One label mapping to several canonical keys is **intentional** — Taiwanese
  forms often have one cell covering two things — and now has a guard. I very
  nearly "fixed" it as a defect.

## [1.15.49] - 2026-09-15

### A red frame along all four edges of the corrected page (customer report)

It looked like the corner detection had swallowed some desk. **It was not the
desk — it was a colour we painted ourselves.** Rotation filled the newly
exposed edge with

    cv2.warpAffine(..., borderValue=255)

and OpenCV's `borderValue` is a four-number `Scalar`: writing `255` alone
expands to `(255, 0, 0, 0)` — white on a 1-channel image (correct), **pure red
on a 3-channel one**. When v1.15.47 moved the geometry from grayscale onto the
colour image, that line quietly changed meaning.

Measured on the reported business card: **5,628 pure-red pixels → 0**, and the
mean chroma of the outer two-pixel ring **43.8 → 2.8** (the paper itself is
4.2). The four corners were right all along (ink coverage 1.000, purity 1.000).

The same thread uncovered an older ordering mistake: **flatten the illumination
before rotating, not after.** Rotation pads the corners, and that padding is not
photographed content — yet the background estimator treats it as "this area is
genuinely bright", which drags the gain field down and darkens everything else:
**6.9% → 64.7% dark pixels** on the same photo. The padding used to be red
(luminance 54), which happened to be masked as a dark region, so the wrong order
never showed.

### Dragging the four corners: the image jumped after the first one

The status line sits directly above the "before" image and wraps onto a second
row when the text is long. Every recompute reset it to a short "rendering
preview…", so **the image jumped 40–50 px upwards after each corner**, landing
the remaining three 19% too low.

The resulting quadrilateral was then rejected as implausible (interior-angle
spread 40.4°, limit 40°) and **dropped entirely** — while the screen still said
"using the corners you dragged". The status row now only ever grows, and it
remembers the tallest it has been rather than hard-coding a height (Chinese,
English and Japanese differ, and so does the window width).

### The installer: Japanese, and no more mojibake

**Mojibake** (customer report, Win11 25H2): `winget` writes UTF-8 while NSIS's
`nsExec::ExecToLog` decodes with the **system ANSI code page** — CP950 for
Traditional Chinese, **CP932 for Japanese**, CP1252 for English. All three
garble. That output was never meaningful to the reader, so it now goes to
`installer.log` and the pane keeps only our own one-line status.

**Japanese**: only Traditional Chinese and English language tables were
declared, and NSIS falls back to the **first declared** one, so a Japanese
Windows got a Chinese installer. Japanese now covers the component list, the
finish page, the failure message and **the three uninstall prompts** (that path
returns before the language dialog, so it is the easiest one to miss).

### Other

* The **document straightening** tool is now called `文件擺正` in Chinese; its
  URL and API path (`doc-straighten`) are unchanged.
* The changelog no longer quotes what a person said. The symptom stays — the
  quotation marks and the attribution go.

### Redaction now handles **Japanese documents**

"Japanese" joins the document-language list, with six Japanese-only categories:
**My Number**, **Corporate Number**, **phone**, **postcode**, **address** and
**name**. A Japanese interface now defaults to Japanese documents.

* **Anything with a check digit is verified.** Both My Number and the Corporate
  Number have one — without it every 12- or 13-digit string matches, so part
  numbers and order numbers are flagged in bulk while the screen says "done".
* **A false-positive corpus is part of the acceptance.** A Japanese document
  full of part numbers, order numbers, ISBNs and version strings must produce
  **zero** sensitive hits. Testing only that detection *works* would pass even
  if the patterns matched everything.
* **Replacement values are Japanese and never valid.** A fake number that
  passes its own checksum may belong to a real person (the same reason SSNs use
  the unassigned `9xx` range and the fake IBAN deliberately fails mod-97).
* **Taiwan and English did not regress** — the real corpora were scanned before
  and after and compared.

> **⚠ The separators a PDF gives you are not the ones you typed.** PyMuPDF
> returned a non-breaking space `\xa0` and a **non-breaking hyphen `\u2011`**
> (not `-`) for the very same file — matching on `[ \-]` found 3 of the 7
> categories, **while the screen said "done"**. This project already recorded
> the space half of this ("the whitespace a PDF yields is not an ASCII space");
> this is the hyphen half.
>
> Loosening the separators then introduced one false positive
> (`社内コード：03-1234-5678-X-99` read as a phone number) — the
> "must not be adjacent to a hyphen" half cannot be dropped along with it.
> Both are pinned as acceptance checks, each mutation-verified.

### ⚠ A dozen scanners' `</script>` regexes could skip a whole file

`</script  >` is valid HTML, and a regex that hard-codes `</script>` treats it
as *not yet closed* — **swallowing the rest of the file as script content**, so
that scanner silently stops checking. That is what CodeQL's `py/bad-tag-filter`
was reporting (11 High alerts).

There is now one shared implementation in `tools/source_text` (`</tag\b[^>]*>`),
and all 14 sites use it.

> This project hit the same family in issue #15: a literal `</script>` inside a
> comment closed the tag early and turned a page of JavaScript into plain text.

### PDF editor: your work survives a disconnect or a closed tab

The edit state used to live only in that browser tab. What the server holds is
an **already-flattened PDF**, not the edit state — and temp cleanup removes it
after two hours anyway, so getting it back would not let you move a text box.

Edits are now kept in the browser, and reopening **the same file** offers to
pick up where you left off.

> **It does not follow you to another computer** — the notice says so, because
> everything else this tool produces does live on the server.
>
> **An empty edit state must never overwrite a real draft.** Reopening a file
> snapshots the still-empty canvas, and 1.5 seconds later that snapshot wiped
> the previous draft — **before the user could click "resume"** (measured: one
> object saved, zero read back). Only running it in a real browser shows this;
> static checks see the code and call it present.

### PDF to Word: per-page progress

`Converter.convert()` is a single opaque call, and this tool routinely takes
minutes — the bar simply did not move. It is now split into the four steps that
call already performs, with per-page granularity taken from pdf2docx's **own**
log line rather than its internals. A four-page file now reports ten times.

> If upstream changes that line we **silently fall back** to stage progress —
> which is exactly why there is a check watching the format.
>
> **Broken progress must never fail a conversion**: it is an accessory, not the
> output.

### The site's screenshots are no longer empty states

"User management" and the permission matrix showed "no users or groups yet".
The demo data now has 6 accounts, 3 groups, and **the same account name in both
the local and ldap realms** — which is the whole point of that screenshot. All
of it is invented.

---

## [1.15.48] - 2026-09-14

### The site's top-left title wrapped in English and Japanese

**Measured**: the title needs 166 px in Chinese, 225 in English and **285 in
Japanese** when it is not allowed to wrap; add the nav bar and English needs
**1186**, Japanese **1188** — while the container is **1180**. It was over by a
handful of pixels.

The brand block is now a fixed two-line lockup: the first line is always
`Jason Tools` (identical in every language) and the description sits on the
second line. The three languages now need 919 / 1052 / 1002 px, with well over a
hundred to spare.

> **Not a smaller font, and not a higher breakpoint** — either only pushes the
> problem to the next language. The part whose length varies moved to where
> length no longer matters.

---

## [1.15.47] - 2026-09-14

### ⚠ The site's screenshots had never actually had a document in them

Not because anyone forgot to add one:

* `/usr/bin/chromium-browser` here is the **snap** build, and **it cannot read
  `/opt`** — which is where the sample files lived.
* `DOM.setFileInputFiles` still "succeeds" and the file name still appears on
  screen. **Only the eventual XHR fails, with `network error`, and the server
  never sees a single request.**

So the uploads in that capture had never worked — the English set sitting on a
"Please upload a PDF first" dialog was the same cause. Staging the samples
somewhere the browser can read them fixed every page at once.

> **The family lesson**: "it looks like it worked" (the file name is showing)
> is not the same as "it worked". The check is now *did the server receive the
> request*, plus *is a dialog covering the screenshot* (that gets printed).

### Screenshots: entirely made-up demo data, and the tools are really run

`tools/seed_demo_data.py` creates a demo company (Example Technology Co., Ltd.,
VAT 12345675, 02-1234-5678 …, **not one real value**), a demo seal, and a
synthetic vendor form. The auto-fill screenshot is a form with **18 fields
actually filled in** — nothing has to be blurred out any more.

Each page now has a recipe (which sample, whether to press the button, where to
scroll) and **waits until the result is actually painted** rather than sleeping
for a fixed number of seconds.

### Taiwan-only tools are no longer shown on the English and Japanese site

They are greyed out in those interfaces anyway; showing their screenshots only
suggests they can be used. Which ones to drop comes from the registry's
`ToolMetadata.locales` — not a hand-kept list — and the remaining figures are
renumbered, because 01 / 03 / 04 looks worse than one fewer picture.

### The site's language picker is a dropdown now

The nav bar read `繁體中文English日本語` run together, and the whole bar wrapped
onto two lines. Languages only ever get added, so a dropdown (whose width does
not depend on how many there are) is the right shape.

Three things changed in the nav at once:

* **Items no longer wrap** — wrapping broke them mid-word
  ("Why self- / host").
* **Shorter Japanese labels** (`インストール` → `導入`, and so on).
* **The hamburger breakpoint is measured, not guessed** (`chromium
  --headless`, per language): the bar needs 805 px in Chinese, 875 in
  Japanese, 933 in English; English stops fitting at 1060 and still fits at
  1100 — so **1080 px**. The old 980 was set from Chinese lengths alone.

### Document straightening: output was black and white without asking for it

The photo was a colour business card. The whole pipeline ran on the greyscale
copy (`crop_page(gray)`, `warp_quad(gray, …)`), so colour never survived — while
the checkbox on screen says "convert to black and white (off by default)".
**The interface promised something the code did not do.**

It now **measures on grey and transforms the colour image**. That also exposed
an RGB/BGR mix-up (PyMuPDF hands back RGB, `imencode` wants BGR) which would
have swapped red and blue.

> The check is **saturation**, not channel count: three identical channels
> still look black and white.

### Document straightening: you can set the output size (pixels or mm)

Automatic (whatever the corrected page comes out as) / A4 / A4 landscape / A3 /
Letter / custom width × height with a unit. After a preview the boxes are
**pre-filled with the size actually produced**, and once you edit them they are
left alone.

> **When the ratio does not match, proportions are kept and the page is padded
> with white — never cropped, never stretched.** This tool has said from day
> one that cutting into content is unforgivable while an extra strip of desk is
> merely ugly. Padding is white, not black (black would soak the printer). A
> malformed size is treated as "not set", never a 500.

### Document straightening: a superseded re-render is aborted immediately

There was a token that ignored *late replies*, but the request itself ran to
completion — **the server rendered a page nobody wanted**. It is now aborted
with `AbortController`.

> Being superseded **is not a failure**: saying "preview failed" suggests
> something broke when the next one is simply on its way.

### ⚠ Redaction: a Japanese interface was treated as Taiwan

v1.15.46 fixed the document tool; **the text tool still had its own copy** of
the old "anything but `en` means `zh-Hant`" rule — a Japanese screenshot caught
`4111 1111 1111 1111` being reported as a Taiwanese landline. Both now share
`patterns.default_doc_lang`, with an AST check that they really delegate.

> "**Sweep the whole family at once**" — the same lesson, twice in one day.

Two more from the same round:

* **Category labels were never translated** — that row was Chinese in both the
  English and Japanese interfaces, in both tools.
* **Address masking was hard-coded to a Taiwanese shape** —
  `1842 Maple Street, Springfield, IL 62704` came out as `OO市OO區OO路OOO號`,
  which reads as though the document had a Taiwanese address to begin with.
  Masking is meant to *keep the shape and hide the content*; swapping in
  another country's shape breaks the first half. Chinese addresses keep the old
  form; everything else is masked character by character, separators and length
  intact.

### Also

* "PDF to Word" is no longer labelled **beta**.
* **The upgrade notice now says the `-C` is upper case** (reported by a customer
  on 2026-09-14): typing `git -c /opt/jt-doc-tools/ …` answers
  `fatal: not a git repository (or any of the parent directories): .git`,
  because a lower-case `-c` is the *config* flag and git never changes into that
  directory. **The message reads as though the install were not a git checkout**,
  which sent the customer looking in entirely the wrong place. The caveat is in
  the README, `OPS.md` and on the site, with a check that no copy-and-paste
  block ever contains a lower-case `git -c <path>`.

---

## [1.15.46] - 2026-09-14

### Japanese interface added (site, API guide, troubleshooting and README too)

"Interface language" in the sidebar now offers `日本語`. The catalogue holds
**4,891 entries**, and there are four public documents in Japanese
(`index-ja.html`, `api-ja.html`, `troubleshooting-ja.html`, `README_ja.md`).

**The language switch became a list, not a toggle.** One button was enough for
two languages; with three, a reader standing on the Japanese page had no way
out to the English one. Every page now links to **every language but its own**.

> **The generators and the guards all read the language list from
> `ui_locale.SUPPORTED`.** Eight places used to hard-code `en` — those guards
> would have gone on **quietly checking English only** after a third language
> arrived ("scanned nothing" and "scanned everything and it was clean" look
> identical in pytest output).

**Three things only Japanese exposed** (none of them happen with English):

* **The terminology guard flagged the whole Japanese file.** Japanese `保存`
  and `字体` are correct Japanese, and both are on the Chinese banned-words list
  (Chinese wants `儲存` and `字型`). Nearly every Japanese line has kanji, so
  without an exclusion the report is all false positives — and **once a check
  is noisy, people start ignoring it**. The exclusion keys off the **file
  name** (`README_ja.md`, `index-ja.html`), because the translated files sit
  beside the Chinese ones and a directory rule cannot tell them apart.
* **"A translation must not contain Han characters" does not hold for
  Japanese.** Every Japanese translation contains kanji. The check now looks
  for Chinese words modern Japanese does not use (`這` / `嗎` / `什麼` / `沒有` …),
  which is the signal for "this entry was never translated at all".
  **`的` must not be on that list**: `一般的` and `自動的` are correct Japanese
  (the first version included it and produced three false positives on the
  spot).
* **A Japanese interface does not mean redaction handles Japanese
  documents.** The document language defaulted to "Taiwan unless the interface
  is `en`", so Japanese users silently got the Taiwanese pattern set — and
  Taiwanese landline / address / VAT-number patterns applied to another
  language do not *miss*, they **match the wrong thing**, while the screen
  says "done". Languages with no pattern set of their own now fall back to the
  language-independent group.

### Fixed: dropdown labels never went through translation

Scanning the Japanese interface page by page in a real browser found five
places. **Every one of them was Chinese in the English interface too** — the
two dropdowns on the translation glossary page had been wrong since it shipped
in v1.15.19:

| Where | What |
|---|---|
| Translation glossary | 12 language options |
| System status | Database names (audit log, VAT database …) |
| Document redaction **and** text redaction | Document-language dropdown |
| Document straightening | The resolution hints |
| Login page | Authentication source ("Local accounts") |

> **What they share is that the text comes from server data** — a guard that
> greps templates for a literal `tr('…')` cannot see any of it. The new
> `test_option_labels_go_through_tr` checks the expression inside every
> `<option>` instead. Genuine data (user names, tool ids, a language's own
> name) is exempt **with the reason written down**.
>
> **Sweep the whole family at once**: the two redaction tools each carry a
> copy of the same template and I fixed only one of them first.

### Fixed: one sentence on the site had its clauses swapped (in English too)

The "tools that need an Office engine" paragraph is split by `<b>` tags and
translated segment by segment, which put the verbs the wrong way round:
*"These tools need Word / Excel / PowerPoint / ODF when handling OxOffice or
LibreOffice"*. **Each segment's translation has to read correctly in its own
position.**

Japanese also gained a typographic rule: when an inline tag has Japanese on
**both** sides, the space between them is removed (the Chinese source often
leaves one because the tag contains Latin text), otherwise you get things like
`不要 です`.

### Fixed: the language cookie had no `Secure` flag on HTTPS sites (found by ZAP)

`/ui-locale` used `request.url.scheme == "https"`, and this project turns
uvicorn's `proxy_headers` off — **behind a reverse proxy that value is always
http**, so `jtdt_locale` shipped without `Secure` on an HTTPS site. It now uses
the shared `is_https_request()` (and so does the SSO transaction cookie, which
was looking at the redirect URI we send the IdP, when `Secure` is about **the
browser's leg** of the connection).

> **That helper's docstring already said "every cookie's `secure` must go
> through here" — there was simply no guard.** Almost every regression in this
> project comes back that way. The new `tests/test_cookie_secure_flag.py` walks
> the AST and checks every `set_cookie` / `delete_cookie` (**deleting needs the
> flags too** — `Max-Age=0` does not inherit them), plus a second check that
> the one exempt local variable really is computed from that helper; without
> it, someone hard-coding `is_https = True` would stay green.
>
> **Nothing had ever scanned that path before**: with Japanese added, the
> language switch became a group of links, and ZAP's spider POSTed to
> `/ui-locale` for the first time. **"The scan found nothing" and "there is
> nothing" are different claims** — a scan only proves the parts it reached
> were clean.

### Also

* The screenshot tool and the page-by-page scanner both take `--locale` now,
  and the Japanese site uses screenshots of the **Japanese** interface
  (`screenshots/ja/`).
* `tests/test_i18n_catalog.py` strips comments before harvesting `tr()` keys
  from JavaScript — a comment explaining the rule contained an example call and
  was counted as a real key. That is this project's recurring "the scanner was
  fooled by the very name it checks for"; this time it caught me.

---

## [1.15.45] - 2026-09-14

### Document straightening: every page of every file is now visible

There was only a **number input**: you had to type a page number, could not see
what pages existed or which ones you had adjusted, and once several files were
merged into one PDF there was no way to tell which page came from which file.

There is now a per-page thumbnail strip: click to switch and re-run that page,
with the current page highlighted, "rotated N° / manual corners" marked, and a
separator plus file name at the first page of each source file.

> **Which page came from which file cannot be recovered from the merged PDF** —
> only the upload knows, so `/load` now returns `sources` (pages per input file).
> Thumbnails reuse the existing `/thumb` endpoint (70 dpi, cached on disk) with
> `loading="lazy"`, so a 50-page document does not fire 50 requests at once.

### Document straightening: after rotating, dragging the corners produced garbage

When the status line showed both "rotated 90°" and "using the corners you placed",
the corrected output was wrong. **Two mistakes stacked:**

* the corners the user drags are on the **rotated** image (since v1.15.44 the
  "before" view follows the rotation), but the caller converted 0–1 to pixels
  using the **unrotated** dimensions — which are exactly swapped at 90°;
* and the core then rotated those coordinates **a second time**.

Measured at 90°: output **834×358** (should be 471×629) and **41.2%** dark pixels
— i.e. mostly desk rather than paper (correct value: 1.9%).

> **The fix is not to repair the two conversions, it is to have one coordinate
> system.** `quad` is now always "**normalised 0–1 in the rotated frame**", and
> detection moved inside `straighten_page` (after the rotation), so nothing is
> converted in between; `_rotate_quad` is gone. **With two coordinate systems,
> sooner or later someone converts on the wrong side.**
>
> The same root cause had a second, unnoticed branch: the auto-detected corners
> **returned to the browser** were normalised against the unrotated dimensions
> too, so after rotating, switching to manual placed the handles somewhere
> unrelated — while the screen showed four handles and looked perfectly normal.

> **Two layers of guard:** at the core, the output from "corners in the rotated
> frame" is compared with "detect directly on the rotated image" (size **and**
> dark-pixel ratio); end-to-end, a real browser rotates 90°, drags the corners,
> then **draws the corrected image into a canvas and measures the dark ratio**.
> Checking only where the outline sits is not enough — mutation testing confirmed
> this bug stays green that way.

### Document straightening: switching back to "detect automatically" did not

After switching to "place the four corners yourself", adjusting them and
switching back, the corrected image and the outline both stayed on the manual
version. Two causes stacked:

* switching modes only called `renderQuad()`, which merely shows or hides the
  overlay — **the preview was never re-run**;
* and even re-running would have used the stored manual corners.

> **The mode is a switch, not a delete key.** Automatic mode no longer sends
> those coordinates (both the preview *and* the submit must filter them — filter
> only one and the screen says "automatic" while the delivered file is manual,
> with nothing to show for it), but the coordinates are **kept**, so switching
> back restores the user's work instead of throwing it away.
>
> The test is that the status line after switching back is **identical** to the
> one from the first automatic run; merely checking "did it recalculate" would
> pass even when recalculating with the manual corners.

---

## [1.15.44] - 2026-09-14

### Document straightening: the four corners were **never** joined up

v1.15.43 made the outline thicker and the user reported it still was not there.
The cause had nothing to do with thickness:

* **`SVGElement` has no `hidden` IDL attribute** — the spec defines it on
  `HTMLElement`. `svg.hidden = false` just sets a property nobody reads;
  **the `hidden` attribute itself is untouched.**
* And `platform.css` has `[hidden] { display: none !important; }` — an *author*
  stylesheet with no namespace. The browser's own `html.css` is namespaced to
  HTML, ours is not, so **it hides SVG as well.**

So that quadrilateral has been `display:none` since the first version, with
**no JavaScript error anywhere**: the four drag handles are `<div>`s and worked
fine, so the screen showed dots but never lines — it read as "not built yet"
rather than "broken". Now toggled with `toggleAttribute`, which works for both.

> Two guards: a static one that scans the whole tree for `.hidden` used on an
> `<svg>`, and an end-to-end one that uploads a synthetic photo in a real browser,
> switches to manual mode and **measures whether the outline occupies any space
> on screen**. Checking that the `points` attribute is set would have stayed green.

### Document straightening: new "clean up" option, on by default

A shadow across half the sheet is the most common problem with phone photos.
**The test is text recognition, not whether it looks cleaner:**

| Case | Untouched | Cleaned |
|---|---:|---:|
| Hard one-sided shadow | **0.472** | **0.982** |
| Corner vignetting | 0.884 | 0.986 |
| Diagonal shadow | 0.967 | 0.986 |

> **Binarising is not cleaning up.** On the same material, local thresholding drove
> recognition down to **0.108** — and the version that *looks* cleanest is the worst
> one. It stays a separate, off-by-default option for shrinking files. CLAHE was
> also consistently worse.

> **`divide(image, local max)` cannot be used**: recognition is just as good, but it
> washes large dark areas to pure white — a dark grey photo block went from mean
> 66.3 to **254.9**, i.e. it vanished. Instead a slowly varying, bounded gain field
> is estimated, with unreliable (large dark) regions masked out and filled by
> `inpaint` **from their boundary**. Filling with a wide blur instead smears away the
> shadow's step edge and recognition falls back to 0.472.

> **Perfectly even scans are left bit-for-bit identical** (maximum change: 0 levels),
> which is what makes it safe to default on. The white-point step must be clamped to
> brighten-only; without that clamp a pure white scan is pushed down to 245 and 97%
> of pixels change.

### Document straightening: multiple files at once; rotation applies to the "before" view

* Several uploads are merged into one PDF **in upload order**, each page processed.
* After pressing rotate, the "before" thumbnail on the left stayed unrotated and no
  longer matched the corrected view on the right.

### PDF editor: warns when the file carries a digital signature

Editing and saving a new file **always invalidates the signature** — it covers the
whole file, so any change (even just re-saving) makes readers report "signature
invalid / document has been altered". The editor now says so up front and explains
that keeping the signature means using the original file or having it re-signed.

### Seam stamp: transparent padding around the stamp shrank it

With "stamp width 40 mm" we scaled the *whole image* to 40 mm — and stamps cut out
from a photo usually keep a transparent margin, so the actual ink was only
**26.8 mm** (with 25% padding), and **the same setting produced different sizes for
different source images**. All the user sees is "the stamp got smaller".

Compositing and the reassembled preview now share one `load_stamp()` that trims the
transparent border first, with a stray-speck threshold — a plain alpha bounding box
is anchored by the few semi-transparent dots left behind by background removal and
trims nothing.

> Other points from the same external review already held: the complete stamp is
> rotated before slicing, slice widths use running rounding (no gaps or overlaps),
> there is a "reassembled stamp" preview, there is no artificial jagged/torn edge,
> and alignment uses the `CropBox` rather than the `MediaBox` (now pinned by a test).

### PDF editor: that signature warning had no styling at all

It used `class="warn-box"` and my own comment claimed the class already existed site-wide. It did not — I invented it and then vouched for it. The full suite's `test_template_css_is_effective` caught it. There is now a shared `.warn-box` (the amber warning twin of `.info-box`), both living in `platform.css` so the next tool does not invent a third name.

### Seam stamp: click a preview to see it full size

Both the per-page previews and the reassembled stamp open full size, with arrow-key
paging and Esc to close.

> **Opening is not the same as enlarging.** The first version measured 300×424 on
> screen — the same size as the thumbnail, because the per-page preview is a 78 dpi
> render and showing it at natural size enlarges nothing. The endpoint already had
> `large=1` (150 dpi), so the full-size view now fetches that **on click** (rendering
> one for every page up front brings back the old "90 seconds per preview request"
> problem on a 52-page file). Measured: 645px → **1240px**.

> **No fourteenth copy was written.** A sweep found **13 separate lightbox
> implementations**, each with slightly different keyboard and close behaviour.
> There is now one shared `static/js/lightbox.js` (declarative `data-lightbox`,
> event-delegated so thumbnails added later are covered too) and a guard that stops
> new private copies appearing; the existing 13 are an explicit exemption list which
> is itself checked for staleness.

---

## [1.15.43] - 2026-09-14

### New: an install & upgrade troubleshooting page, linked from every failure

**A single line of error text leaves people stuck.** Most failures here have a
known answer — missing git, corporate TLS, not enough disk, moved tags, a service
holding files open, a reverse proxy hard-coding the protocol — but nothing pointed
at it.

* New page `docs/troubleshooting.html` (in both languages, **every entry is
  something that actually happened**: symptom, cause, what to do) with a clickable
  index at the top.
* `install.sh`, `install.ps1`, the Windows installer's failure dialog and every
  failure path in `jtdt update` now print that page's address.
* **The address follows the operating system's language** — Chinese systems get
  the Chinese page, everything else the English one. CLI text itself stays English.

> The English pages previously linked to the **Chinese** API page — one click and
> the reader was back in Chinese. Internal links are now rewritten for the English
> build, except the language switch itself.

### Document straighten: the four corners are joined by a visible line

`stroke-width: .6` with `vector-effect: non-scaling-stroke` means **0.6 pixels** —
invisible over a photograph, leaving just four dots. It is now drawn twice, white
beneath and blue on top, so it shows on both dark desks and white walls.

---

## [1.15.42] - 2026-09-14

### Document straighten: button placement and the state while recalculating

* **"Straighten" moved below the before/after comparison** — it is pressed after
  looking at the result, so that is where it belongs. The options panel keeps only
  the preview button, which is what produces the comparison.
* **Dragging a corner now puts the right-hand pane into "Recalculating…" with a
  spinner straight away.** It previously kept showing the *previous* result until
  the server replied, which reads as "nothing happened" and invites a second drag.

---

## [1.15.41] - 2026-09-13

### `jtdt update` could be blocked by tags that had moved

Tags move — a release gets re-tagged, or history upstream is rewritten. When the
local tag points at one commit and the remote at another, `git fetch --tags`
without `--force` reports `would clobber existing tag` for each and **exits 1**.
`jtdt update` treated that as a failed fetch, aborted the upgrade and restored the
previous state, saying only `git fetch failed` — so **every git-based install
would stop updating**, with nothing to suggest tags were the cause.

Measured on an untouched machine: `git fetch --tags origin` → **1**;
`git fetch origin` → 0; `git fetch --tags --force origin` → 0.

Branches and tags are now fetched separately: **the branch is required, the tags
are a bonus** — an upgrade only needs `origin/main`. A failed tag fetch prints a
note instead of stopping the upgrade, and tags are always fetched with `--force`.

> An install that is already stuck recovers with one command, after which
> `jtdt update` works again:
>
> ```bash
> git -C <install directory> fetch --tags --force origin
> ```

---

## [1.15.40] - 2026-09-13

### Dragging the four corners: the handles used the box, but the picture is drawn inside it

A user reported that the magnifier's crosshair pointed somewhere other than where
the corner actually landed. The before/after images are sized `height: 46vh` with
`object-fit: contain` so the two columns match in height, which letterboxes a
photo whose aspect ratio differs: measured, a 541×972 box held a picture drawn at
541×766, with 103 px of blank above and below. The handles, the quadrilateral and
the magnifier all worked in fractions of the **box**.

| | before | after |
|---|---|---|
| A handle at 40% height landed at | 37.3% of the image (**23.65 px out**) | 40.0% (0.0 px) |
| Pixel under the magnifier's crosshair | a different point from the handle | exactly the expected one |

This was not only a display problem: the same numbers are what get sent to the
server, so the crop followed the wrong edges.

The overlay now lives in its own layer positioned over the drawn picture using the
`contain` maths, recomputed on load, page change, mode switch and resize;
everything inside keeps working in normalised 0–1 coordinates. The magnifier
samples at 62 px rather than 64 — it is a 128 px border-box with a 2 px border and
the background origin is the padding box, so the crosshair sits at 62.

> No existing gate could see this: the elements are all there, no exception, no
> untranslated text, and a screenshot looks right. A new test drives a real
> browser, drags a handle to (0.30, 0.40) and **measures** where it landed and
> which source pixel the crosshair covers, to the pixel. The sample image is
> deliberately a different aspect ratio from the box — with a matching one there
> would be no letterboxing and the test would pass while verifying nothing.

### Transit receipts: Uber support

**Trip receipts only.** An Uber ride produces two PDFs in Taiwan, and the
e-invoice covers only the booking fee — a few dollars — because taxi rides
themselves are not e-invoiced. Uploading the wrong one would put 10 dollars in
the table instead of the fare, so the page now says which to use.

Extracted: date, pickup and drop-off times, both addresses, the total, plus two
new columns — **plate** and **distance** (hidden by default; distance is a
required field for taxi expenses at many companies).

> **The times must come from the pickup and drop-off pair.** The receipt carries
> four times — request, pickup, drop-off, payment (16:58 / 17:01 / 17:27 / 17:28
> in the sample). Taking the first gives the request time: three minutes out,
> entirely plausible, and nobody would notice. The rule is "the line after the
> time is an address" — only the pickup and drop-off look like that.

### Uber receipts carry no ticket number — a second ride the same day was dropped

Deduplication keyed on the ticket number, which every rail ticket has. Uber
receipts do not, so it fell back to transport + date + route + fare — and a
second ride on the same route at the same price was silently treated as a
duplicate. That is an ordinary commute.

The receipt does carry a unique identifier: the `riders.uber.com/trips/<id>`
link on page one. It is invisible to text extraction — it exists only as a link
annotation — so the extractor now collects link targets as well. If the link is
missing (a reprinted receipt), the departure time separates the rides instead.

### Document straighten: flat scans are no longer "perspective corrected"

Testing had used two phone photographs and synthetic samples. Measuring against
82 real scans and photographs showed the quadrilateral **cutting the header off
scanned documents**. Rather than tune the mask again, the rule now recognises
that such images have no perspective to correct: when the chosen candidate is a
perfect rectangle covering most of the frame, warping can only crop. Genuine
perspective photographs are unaffected.

> **A measurement needs a control too.** An early "darkest N% of the image is
> ink" metric scored two perfectly correct crops at 0.015 and 0.036, because in
> those photographs the desk is darker than the paper. Changing the denominator
> was guesswork; drawing the mask is what located the real problem.

---

## [1.15.39] - 2026-09-13

### A strip of desk survived the straightening — the mask, not the quadrilateral

A user reported that the corrected page still had the desk around it. Drawing the
mask made it clear: **the half of the paper lying in shadow was classified as
desk** (Otsu covered 32% of the frame where the paper occupies 40%), so the
quadrilateral only enclosed the lit half — and the minimum-area rectangle, in
pulling that back in, swallowed a band of desk.

| | old (Otsu + min-area rect) | new (Otsu ∪ chroma + scored candidates) |
|---|---|---|
| photo A | ink 0.738 / purity 0.909 | ink 0.737 / **purity 1.000** |
| photo B | ink 0.747 / purity 0.925 | **ink 1.000 / purity 1.000** |

Three changes: the mask gains a **chroma** layer (paper is neutral in Lab, a
wooden or coloured desk is not — and shadow changes brightness, not hue);
several **candidate** quadrilaterals are generated rather than one; and the
choice is made on two measurable numbers — **ink coverage** (how much of the
writing is enclosed) and **purity** (how much of the enclosure is really paper).
Ink is the hard constraint; purity is maximised under it.

> **The brightness threshold has to be relative to what is definitely paper, not
> to a percentile of the whole frame.** A synthetic heavy-shadow sample exposed
> it: shadowed paper sits at L=118 while the frame's 25th percentile is 122 — a
> large shadow raises the percentile until the rule disqualifies itself. It is
> now 0.45 × the median brightness of the paper; sweeping 0.30/0.40/0.45/0.55/0.65
> shows everything at or below 0.45 scoring perfectly and 0.55 collapsing.

### Two job tests waited on the wrong thing (**test-only change**)

Two consecutive full-suite runs each failed one test that passed on its own, both
with the same shape: wait for `status == "done"`, then read something that only
happens afterwards. The finishing order is deliberate — status, then persist,
then autosave — so under load the read lands in that window. Both now wait for
what they actually verify; a scan found no third instance.

### Editing the public `.gitignore` does nothing — it is regenerated on every sync

While adjusting the sync settings: `github/.gitignore` is written from a heredoc
by the sync script every time, so an edit to the file itself is silently reverted
on the next sync — green before the sync, red after. The rule now lives in the
sync script, with a guard that checks it there rather than in the generated
output.

A related point: `git ls-files` only sees files that are already tracked, so
anything dropped in but not yet committed looks clean to it while
`rsync -a --delete` would carry it into the clone. Checking that a class of file
stays out of the public tree has to look at the filesystem.

### Document straighten interface (reported from screenshots)

The "place the four corners yourself" option was a small checkbox nobody would
notice; it is now a two-card choice using the same pattern as the rest of the
site, as is the resolution setting. Hint text no longer wraps while space
remains beside it.

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
