# Changelog

All notable changes to this project are recorded here.

This project is **pre-release software**. The version numbers stay in `0.0.x` on
purpose: what exists today is the Python validation prototype, not the C++ product
described in `Project Watashi.md`. It is usable for testing and it is honest about
what it does not do yet -- see "Known limitations" below, which is part of the
release rather than a footnote.

## [0.0.6a] -- 2026-09-13 -- 测试版 (pre-release)

Labelled `0.0.6a` because that is what it is from the outside: the first release after
0.0.6, and a patch to it in the only sense that matters here — the things the user reported
after using 0.0.6. The work in between was developed as 0.0.7 and 0.0.8 and is folded in
rather than published as separate versions, since none of it was ever released.

The three reported defects, all of them real, and the third one reported against this very
release before it was pushed:

- **「打开设置面板」 was broken outright** — see *Fixed*. It is written up there rather than
  here because the interesting part is not the two wrong lines but that a check built on a
  stand-in had been certifying them.

The other two, neither of which any check could have caught as written:

- **The pause button never changed, so there was no way to tell whether it was
  recognising.** Root cause: `_emit_stats()` is called at the end of a processed frame,
  and a paused pipeline processes none — so the last stats event stayed `paused: False`
  forever, and the window redrew its button and status line from that stale object eight
  times a second. Pressing pause stopped the engine and the interface went on saying
  "recognising", while the one message that did say 已暂停 was overwritten a moment later.
  Fixed at three levels: pausing and resuming now publish a stats event of their own, the
  button is set from the command result without waiting for any event, and the state is
  shown next to the button rather than only inside a long counter line.
- **The engine read its own windows.** "Screen recognition must exclude itself" is two
  problems, and they needed different answers. A window this program owns — the overlay,
  the control window — can be excluded from capture by Windows, and the control window now
  asks for exactly that (`WDA_EXCLUDEFROMCAPTURE`, verified as `0x11`). A **browser showing
  the web panel is not our window**, so there is nothing to ask: the engine now detects it,
  refuses to start, and says which window is in the way and how much of the region it
  covers. The user's stutter was change detection never settling on a window that repaints
  its own counters.

Both are covered by checks that would have caught them: `selfcheck_session` asserts that
pausing publishes a stats event, `selfcheck_desktop` asserts the button flips on the click
(and does not flip when the command is refused), and `selfcheck_selfcapture` covers the
geometry, the window filter, the hold and the two switches that turn the guards off.
`selfcheck_desktop` also asserts the exclusion really is applied, with the setting off as
a control — `0x11` against `0x0`.

### Added

- **The 语料库 tab in the web panel.** One table of every entry from every layer, with the
  three states an editable corpus actually has and the action each one needs: yours
  (edit, delete), an override of a shipped entry (edit, revert), a shipped entry (edit →
  becomes an override, or 停用). Plus a filter, an add form, and import/export.
- **Editing a shipped entry never rewrites the shipped file.** It writes an override into
  the user layer, which wins by layer precedence, and the UI says what it overrode and
  what that entry used to say. The shipped corpora are demo data tracked by git;
  rewriting them would dirty a working tree, conflict with the next pull, and destroy the
  difference between "what shipped" and "what I changed". `selfcheck_library` asserts the
  bytes are unchanged after an edit through the same code path the UI uses.
- **Suppression**, for the other half of editing a shipped library: an entry can be turned
  off without pretending to translate it (`_suppress` in the user layer). A row that
  merely *vanished* would leave the user with no way to ask why or to undo it, so hidden
  entries stay listed, marked, and one click from coming back.
- **Import and export.** JSON in the corpus's own shapes, plus CSV and TSV with headers
  (`source,target,lang,pos,domain,note`, Chinese headers understood; no header means two
  columns). Text in, text out, so the browser does the file handling and the panel needs
  no access to the machine it runs on. CSV downloads are written `utf-8-sig` so a
  spreadsheet opens Chinese text instead of mojibake. Export can be the user's own entries
  or everything in effect, the latter shaped so it can be dropped into another install.
- **The `corpus_loader` plugin point is wired.** It has been reserved in the plugin API
  since the first version, described as "read a corpus format other than JSON", and had no
  caller until an editor needed to import a format it does not know. Reserved extension
  points that nothing calls are indistinguishable from broken ones.
- **`library_list` / `library_put` / `library_delete` / `library_suppress` /
  `library_restore` / `library_import` / `library_export`**, plus a `library` event so a
  second surface repaints its table instead of showing a stale one.

### Changed

- **The desktop window lost four tabs.** 设置, 呈现, 诊断 and 翻译 each duplicated something
  the web panel already did better — one of them existed only to say "adjust this in the
  web panel" — and two editors of one file is how two surfaces start disagreeing. What
  remains is what a browser cannot do: the live view with its correction editor, the
  region and window controls, plugins, and the target language in the top bar, which is
  the one setting a user changes while watching. 「打开设置面板」 starts the panel
  in-process and opens it, so the editing surface is one click away rather than a sentence
  in a tooltip.
- **The panel's command allowlist grew on purpose.** It was "settings and viewing only";
  a vocabulary table, a file picker and a fifty-field form are things a browser does well
  and tkinter does poorly. What is still refused is runtime control (`pause`, `resume`,
  `shutdown`, `reload_corpus`), because a stray click in a browser tab must not stop the
  subtitles.
- A corpus entry's value may now be a **list**, which is how one file answers the same term
  in two languages. A JSON object cannot hold two identical keys, so the alternative would
  have been a file that looks correct and silently keeps one of them.

### Fixed

- **An untagged user override was defeated by a language-tagged shipped entry.** Entries
  are keyed by (language, source), and the language view was merged with a plain dict
  update, so the language-specific entry won *regardless of layer* — and the editor writes
  overrides without a language by default, which made the default case the broken one.
  Merging now goes through the same layer-and-priority rule as everything else. Found in
  two places, because `lookup_exact` had it too, which means rule-based lookups did as
  well.
- **`selfcheck_web` wrote into the repository's own corpus layer.** Testing the editor
  against the real config would have left `library.json` in someone's working tree; the
  check now points the user layer at a scratch directory *before* the engine is built, and
  asserts that it did.
- **Hot reload could miss an edit made in the same 15.6 ms as the previous write.** NTFS
  timestamps come from a clock that ticks at about that rate, so two writes inside one
  tick get an *identical* mtime — and mtime was the only thing compared. It showed up as
  `selfcheck_correct` failing roughly one run in ten with a reload counter of zero, on an
  edit that had plainly happened. The snapshot now records size as well as time, which
  catches the common case; the blind spot that remains (same tick, same length) is why an
  explicit reload exists and why a correction forces one. The flake is now a
  deterministic assertion: the check stamps an edit with the previous write's own mtime
  and requires it to be seen, and removing the size from the snapshot makes that assertion
  fail.
- **「打开设置面板」 raised `AttributeError` on every click, so the desktop window never
  reached the web panel at all.** `open_panel` read `panel.running`, which `WebPanel` did
  not have, and then called `panel.url()` — a property, so the line after the crash would
  have raised `TypeError: 'str' object is not callable`. Both were repairs to a function
  written from memory of a class rather than from the class. `WebPanel` now exposes
  `running` (a property, distinct from readiness, and asserted in `selfcheck_web` so it
  cannot quietly disappear), the button keys off it rather than off `start()`'s return
  value — which is `False` both for "already running" and for "cannot bind", so a second
  click used to report a port problem to a user whose server was fine — and it waits for
  the socket to accept before handing the URL to the browser, because opening a browser
  first shows a connection error. Closing the window now stops the panel too; it used to
  keep port 8765 bound inside a process showing nothing, so the next launch reported the
  port in use.
- **The check that covered that button could not have caught it, and that is the deeper
  fix.** `selfcheck_desktop` replaced `WebPanel` with a hand-written `_Panel` class that
  defined `running` and made `url` a method — it was written from the same wrong idea as
  the code, so it agreed with the bug on every run. It now instantiates the **real**
  `WebPanel` on a free port, stubs only the browser, and then fetches the page over HTTP
  to assert it is served. Three mutations confirm it has teeth: calling `url()`, trusting
  `start()`'s return value, and dropping `stop()` from `close()` each fail it. It also
  asserts that the panel the button builds is bound to *this window's own session* rather
  than a copy — "linked up" is the whole point of the button, and a panel built around a
  duplicate session would serve a page that looks right and shows nothing happening.
- `selfcheck_web` now also verifies that the panel's page is *alive* — the inline script
  parses (via Node, skipped when Node is absent) and every element the script looks up
  exists in the markup. Every previous assertion read the page as text, so all of them
  passed on a page whose script had a syntax error: served fine, endpoints fine, and not a
  single tab or table in the browser.

### Testing

- New `selfcheck_library` (112 checks) asserts the safety properties rather than the
  features: the shipped file is unchanged byte for byte after an edit through the same path
  the UI uses, a hand-written corpus file beside it is untouched, override and suppress and
  revert each mean exactly one thing, and every export format imports back to the same
  entries and translations.
- `selfcheck_web` grew from 64 to 97 checks: the editor's endpoints end to end (edit,
  override, suppress, restore, import, export-as-a-real-download, and a refused byte
  sequence), the panel's `running` contract, and the page-liveness assertions above.
- `selfcheck_desktop` was rewritten for the slimmed window: it now asserts the four
  duplicated tabs stay gone and that their widgets went with them, that the top bar holds
  the target language and the panel button, and that the button really starts the panel and
  opens its URL. That last part runs the real `WebPanel` and fetches the served page — it
  is the one effect this check cannot stub, because stubbing it is what let a broken button
  ship — and it prints how long the click blocks (`panel up in N ms`), which is the number
  behind the 3 s readiness timeout.
- New `selfcheck_selfcapture` (40 checks): the rectangle geometry, which windows count as
  ours (by process and by title), that a window already excluded from capture is not
  treated as a problem, the hold itself, and both switches.

### Verified for the first time

- The desktop window's text reaches the screen. A screenshot of the window was read back
  by the project's OCR: the current translation, the source line, the 对照历史 label, the
  实时纠正 frame title, the 原文 / 译文 field labels, the 保存纠正 button, a history
  entry's provenance line, and the settings page's category headings, at 0.95-1.00
  confidence. The negative control held: a nonce string that is not on screen was not
  recognised.
- The overlay's text reaches the screen, at 1.00 confidence, and is absent once the
  overlay is hidden.
- The exclusion asked for and granted: `GetWindowDisplayAffinity` returns
  `WDA_EXCLUDEFROMCAPTURE` (`0x11`) for the control window, and `0x0` when the setting is
  off — asserted both ways, so the first is not a coincidence.

### Added: checking that the interface is alive

- **`selfcheck_uirender`** (display only, 11 checks). Every other UI check asserts
  *structure* -- a widget exists, its text variable is set, a canvas item sits inside the
  canvas -- and none of them can notice a window that paints nothing, text the same
  colour as its plate, or a control covering the label it belongs to. This one
  photographs the real desktop window and the real overlay and reads them back **with the
  project's own OCR**, which is also a fair test of the interface: the engine has to be
  able to read it. The overlay's assertion is differential -- a probe string that exists
  nowhere else must be read off a screenshot while the overlay is up and be gone once it
  is hidden -- so it cannot be satisfied by another window's text.
- **`Checker.skip`**. One class of check cannot run on every desktop, and the honest
  outcome is neither pass nor failure. Skips are printed in `--summary` too, because a
  check that quietly verified nothing is how a green run starts meaning less than it
  appears to.
- `selfcheck_deps` now walks the display checks as well, and follows sibling
  `selfcheck_*.py` scripts instead of counting them as distributions -- which it did, on
  its first run after the change.

### Fixed: found while looking at the interface

- **Closing the desktop window printed 13 Tcl errors to stderr**
  (`invalid command name ..._pump`). `_pump` scheduled its next tick without cancelling
  the previous one, so a driver that calls it directly -- which the self checks do, to
  run the widget without a main loop -- queued a tick per call and only the last was
  cancelled at teardown. Rescheduling is now idempotent: at most one tick is ever
  pending.

### Measured, and worth knowing before trusting a screenshot again

- **Being the foreground window is not the same as being on top.** `GetForegroundWindow`
  reported this project's own window while another application was painted over 70% of
  it, and a check that trusted the API would have reported the resulting missing text as
  a rendering bug. Visibility is now *measured*: hide the window, diff the screen, and
  the share that changes is the share that was visible.
- **A layered window is not what a screen capture returns.** At the bar preset's 0.72
  alpha (`WS_EX_LAYERED` with per-window alpha) the overlay could not be photographed at
  all — three attempts captured other applications and one of them nearly became a bug
  report about a plate drawn without text. With the window forced opaque its text read
  back at 1.00. `PrintWindow`, the usual answer to occlusion, returns solid black for a
  layered window.
- **Tk's window coordinates are correct.** An intermediate conclusion that they disagreed
  with Win32 was my own arithmetic: `GetWindowRect` reports the same DPI-virtualised
  space Tk uses for a DPI-unaware process, and dividing it by the display scale was
  wrong.

## [0.0.6] -- 2026-09-13 -- 测试版 (pre-release)

The first CI run would have failed on both platforms, at the same line, for a reason
no local check could see.

### Fixed

- **`fastapi` was not in `requirements.txt`.** `watashi/web.py` imports it when the
  module loads, `selfcheck_web` imports that module, and `selfcheck_web` is in
  `checks.txt` -- so CI would have failed at `ModuleNotFoundError: No module named
  'fastapi'` on Linux *and* Windows. It passed locally because the virtual environment
  had it installed by hand. `uvicorn` was missing for the same reason and is worse in
  kind: its import is guarded, so the module loads fine and the check fails later when
  the server refuses to start. Both are now declared, along with
  `python-xlib; sys_platform == "linux"` -- `mss` declares no runtime dependencies of
  its own but its Linux backend imports `Xlib`, so a Linux user's first capture would
  otherwise fail on a package nothing told them to install.
- **A headless Linux runner has no `libGL.so.1`, and `opencv-python` needs it at
  import.** Added as one guarded apt step rather than switching to
  `opencv-python-headless`, which `rapidocr-onnxruntime` also depends on -- two
  distributions both providing `cv2` is a worse problem than a missing system library.

### Added

- **`selfcheck_deps`**, 17 checks: for every check in `checks.txt`, walk the real
  import closure and require each third-party module to be declared in
  `requirements.txt` or exempted with a written reason. Guarded imports count too --
  excluding them is exactly how `uvicorn` was missed. It carries a vacuity guard (the
  walker is asserted to find known dependencies, so a broken walker cannot report "all
  clear" forever) and a negative control (the one deliberate exemption, the optional
  LLM backend, is asserted to be found *and* exempted, so that path is not dead).
  Verified by mutation: deleting `fastapi` from `requirements.txt` fails it.
- The README now documents **how to run CI**, how to trigger it by hand
  (`workflow_dispatch`), and the exact shell loop CI executes.
- **`check_all`** (`prototype/check_all.py`), so the same 12 checks can be run locally
  before pushing (34 s) instead of finding out from a red badge. It reads `checks.txt`,
  the file the workflow itself reads, so the two lists cannot drift; `--display` adds
  the four checks that need a screen and `--only` re-runs a subset. The first version of
  this was a batch file, and it parsed the comment lines of `checks.txt` as script
  names -- `delims=#` skips *leading* delimiters, so `# This file...` contributed `This`
  as a check to run. One Python implementation for both platforms replaced a `.cmd` and
  a `.sh`, which is also one parser instead of two.

## [0.0.5] -- 2026-09-13 -- 测试版 (pre-release)

The corpus engine had no self check of its own, and two real defects were sitting in
it. Both are about the same thing: the engine did not know what language it was
translating *into*.

### Fixed

- **A corpus had no target-language dimension.** An English->Chinese vocabulary
  answered a request for Japanese **with Chinese, at full confidence** — `coverage`
  1.0, nothing dimmed, nothing counted. This is the failure the 0.0.3 echo fix
  addressed for source == target, and it is worse, because the text is foreign either
  way so nothing looks wrong. An entry now declares its language (once per file with
  `"lang"`, or per entry), the engine uses only entries whose language matches the
  target, and the shipped corpora declare `zh-CN`. A corpus written before this
  existed declares nothing and keeps working unchanged, because an untagged entry is
  treated as usable for any target.
  Measured end to end, `--selftest --target ja` before/after: the corpus tier used to
  produce 「剑意」/「虚空」/「境界」 for English input; it now leaves the line
  untranslated (reported as such) and the model answers 「この宗派の剣の意図は神話だ」.
- **Rule language matching was string equality.** `target: "zh-CN"` did not match a
  request for `zh`, `zho_Hans`, `zh_CN` or `ZH-cn` — every spelling this project
  accepts elsewhere. Typing `zh` instead of `zh-CN` silently switched off all six
  Chinese rules in the shipped set, and the rule engine simply looked broken. It is
  now a language comparison (`lang.same_language`), and a rule's own corpus lookups
  respect the rule's language, so a Chinese rule cannot assemble an answer out of
  Japanese entries.
- **One source term could only exist in one language.** Entries were deduplicated on
  the source alone, so a `ja` entry for a term the `zh` corpus already had was
  discarded as a duplicate — which made a Japanese corpus impossible to express at
  all. They are now keyed by (language, source).
- **A rule whose regex failed to compile was still counted as loaded.** It could
  never fire, but it appeared in the rule count a user reads as "these rules are
  working". It is now disabled and reported.

### Added

- **`selfcheck_corpus`**, 79 checks: the corpus engine is the core of this project and
  had no dedicated check — it was verified indirectly through the session boundary and
  through benchmark numbers, which is exactly how these two defects stayed invisible.
  It covers layering and priority, longest match and the word-boundary rule, every
  accepted entry form (and the ones that must be refused), the target-language
  dimension, rule language identity, the explainability contract, R3's write-back
  loop, hot reload, and damaged files. Verified by mutation: making the language
  dimension language-blind again (one line) fails 20 of its checks and reproduces the
  original bug in the failure output.
- **R3's write-back loop, asserted.** The requirement that a rule's guess can be
  written into the corpus in one step is now implemented *and* verified: the rule
  answers `antidragon` -> `反龙` at confidence 0.55, accepting it as a correction
  makes the same word come from the corpus at confidence 1.0, with the same answer.
- **The corpus's languages are published** in the `ready` event as
  `corpus_languages`, in the desktop 翻译 tab, in the web panel's corpus card (with a
  per-entry language column), and in `/api/corpus`. "Nothing is being translated" is
  almost always "the vocabulary is for another language", and a user staring at
  untranslated subtitles had no way to discover that.
- **A warning for the one ambiguous spelling.** A file-level `"lang"` needs the
  `{"lang": ..., "entries": {...}}` form, where the top level is metadata. In the bare
  form a key is an entry, so a bare `"lang"` would silently become a term named
  `lang`; the engine now says so instead of guessing.

## [0.0.4] -- 2026-09-13 -- 测试版 (pre-release)

Two things: the correction loop, and the hot reload it depends on -- which turned out
not to exist.

### Fixed

- **A refinement already in flight could undo a correction.** The two-tier display
  means the model is routinely refining the very line the user is about to correct,
  and its answer arrives a few hundred milliseconds later — over the correction, and
  into the refinement cache, where it kept being served to every later frame reading
  that line. Whether a correction stuck therefore depended on which thread finished
  first. `CorpusStore.load()` now bumps a vocabulary revision, a refinement is queued
  with the revision it was computed under, and a batch whose revision has moved is
  discarded and counted (`refinements_stale`) instead of published. The same check
  invalidates the refinement cache, which also fixes a narrower version of the same
  bug: a corpus file edited by hand mid-run was hidden by the model's older answer
  for the same line. Both are asserted, and both assertions were verified by mutation.
- **Corpus hot reload never happened.** `CorpusStore` documented "mtime based hot
  reload", accepted `auto_reload=True`, and implemented `reload_if_changed()` --
  which nothing in the project ever called. Every "hot reload" claim in the docs was
  false: an edit to a corpus file reached the engine only through the explicit
  `reload_corpus` command or a restart. The check is now made from the translate
  path, throttled to one mtime sweep per `corpus.reload_interval_ms` (500 ms), which
  is where the vocabulary is about to be used. Verified by mutation: removing that
  one call makes three assertions in `selfcheck_correct` fail.
- **The shipped user corpus layer resolves to nothing.** The default user layer
  `../plugins/user/custom_rules` holds no files, and in a fresh clone the directory
  does not exist at all (git cannot store an empty directory), so the highest-priority
  layer silently loaded zero entries. Corrections now create it on first use, and
  `selfcheck_correct` asserts that a layer which does not exist yet is created and
  then loaded like any other corpus file.
- **An intermittent web self check failure, caused by a rejected request's body.**
  A route that rejects a request without reading its body leaves the body in flight
  when the response goes out, and uvicorn resets the connection. In this project's own
  self check that surfaced as a `ConnectionReset` on `POST /api/corpus` alone, roughly
  one run in five to eight, never reproducible on demand -- and three earlier diagnoses
  (a zombie server, a port race, a stop that did not stop) were all wrong, which the
  0.0.3 `stop()` fix above shows were real bugs but not this one. Reading the body first
  removed the race for every client. Ten consecutive runs passed afterwards, against a
  failure rate that made ten clean runs unlikely before.

### Added

- **Real time correction** (`watashi/correct.py`). A human correction outranks
  everything, because when the corpus itself is wrong the wrong answer is a confident
  hit rather than a gap, and no amount of extra vocabulary fixes it. `correct` writes
  `corrections.json` into the user corpus layer, atomically and whole, never merging
  into a file the user maintains by hand; the engine reloads it by force rather than
  waiting out the throttle; the reuse memory for that text is dropped, because
  otherwise the rejected translation is served from a ten-second cache and the fix is
  invisible for exactly as long as the user is watching; and the current frame is
  repainted, because a still screen produces no new frame at all.
  Two scopes: `line` is keyed on the whole sentence and matched loosely (whitespace
  and edge punctuation ignored, which is what makes a correction apply to the *next*
  frame -- OCR never reads the same string twice), and `term` is keyed exactly and
  matched wherever the term appears, which is the robust one for a name that shows up
  on every screen. `list_corrections` and `remove_correction` make a mistake about a
  mistake reversible. A whole-line correction is protected as one term, so the model
  is not asked to improve a sentence a person has already settled.
- **Correction editor in the desktop 字幕 tab.** Click a line in the history, its
  source and current translation are filled in, edit the translation, save. Clicking
  fills the source rather than asking the user to retype it, because a line correction
  only matches text that is byte-identical to what OCR produced. A correction made in
  another surface updates this window through the event stream, and the repainted
  frame replaces its own row instead of appearing as a second subtitle for the same
  sentence.
- **`corpus.auto_reload` and `corpus.reload_interval_ms`** settings, both immediate,
  with the schema naming the `set_corpus_reload` command that applies them -- and the
  session pushing both into the running engine, because a setting that only reaches
  `config` looks applied and does nothing until the next start.
- **`selfcheck_correct`**, 138 checks. It contains the assertions that would have
  caught the dead hot reload, and its discriminating power was verified by mutation
  rather than assumed: disabling the reload call fails three checks, disabling the
  cache invalidation fails one, making the model-skip impossible fails two, and
  removing either of the two anti-race guards fails four.

## [0.0.3] -- 2026-09-13 -- 测试版 (pre-release)

Everything here came out of using 0.0.2 for real, which is the only way most of it
would have been found. Three of the five fixes are for things that looked like they
worked.

### Fixed

- **Text in the target language was shown as if it were a translation.** Asking for
  zh->ja through the corpus path returns the Chinese unchanged, because the shipped
  corpus and rules are en<->zh only and the transliterate fallback echoes its input.
  That echo was displayed as the result, so the user read their own language back and
  concluded the translator was broken. An echo is now reported with zero coverage,
  counted as `untranslated_lines`, and drawn as the uncertain result it is -- which is
  what `overlay.dim_low_confidence` now does.
- **`overlay.dim_low_confidence` and per-element opacity did nothing.** The layout
  computed the opacity and the painter never read it, so a guess was drawn exactly as
  confidently as a certainty. tkinter canvas text has no alpha channel -- only a whole
  window does -- so opacity is now pre-blended against the plate colour. Over a
  transparent background the colour is deliberately left alone: darkening text over
  video reads as a rendering fault, not as uncertainty.
- **Six settings were stored, displayed, and read by nobody.** `overlay.subtitle_size`,
  `overlay.bar_alpha`, `overlay.bar_bottom_margin`, `capture.region_ratio`,
  `overlay.dim_low_confidence` and `overlay.bar_height`. The first three now drive the
  presentation spec, which is what actually renders -- and which is why the command
  line flags for the same three worked while the config keys did not. `region_ratio`
  now reaches `bottom_strip` (which had accepted the parameter all along, with nothing
  passing it). `bar_height` was removed from the settings because the bar is sized to
  its content; the key is still accepted so existing config files stay valid.
- **A detection-only OCR call crashed the recogniser.** `_unpack` assumed
  `(box, text, score)`, but `use_rec=False` -- which the constructor supports --
  returns bare quads, so `float(point)` raised `TypeError` and took the call with it.
  Entries that are not that shape are now skipped rather than coerced.
- **The web self check failed two runs in seven, at a different line each time.**
  `WebPanel.stop()` joined once, gave up, and cleared its handles anyway, leaving a
  server thread holding the port; the next `start()` failed to bind and requests went
  to that zombie, whose session was a different one. `stop()` now escalates to
  `force_exit` and reports whether it actually stopped, the self check picks a free
  port at run time, and a new assertion reproduces the original failure -- an open SSE
  stream, then a stop.

### Added

- **`ocr.max_boxes`** -- translate and draw only the largest N recognised boxes.
  Measured, because the obvious reading is wrong: one box costs about 20 ms to
  recognise, but one sentence costs 71-350 ms to translate with the local model, so a
  cap applied after recognition saves the expensive half and cannot save the cheap one.
  Largest-first, because small boxes on a real screen are mostly noise. The setting
  says all of this on its own row rather than in a README.
- **A scrolling line of the previous subtitle** (`previous` element role), enabled in
  `bar` and `bare`. Reading along with a subtitle that replaces itself loses the thread
  of the conversation. `inplace` does not need it (the old text is still under the
  plate) and `panel` already is a history, which is why it is a role a spec opts into.
- **Text reuse** (`watashi/recent.py`): a line translated a moment ago is reused
  instead of paying the model again. OCR is not deterministic, so the same sentence
  returns with a different trailing punctuation and misses the translation cache every
  time. The key is a normalised form of the source, the window slides while the line
  keeps appearing, and boxes are taken from the current frame so a moving plate still
  follows. Short lines are never remembered, because a collision there would be a
  wrong answer that is very hard to notice.
- **`selfcheck_ocr`**, and `selfcheck_dedup` was registered with CI -- the CI coverage
  assertion caught that one had been written and never added, so it would never have
  run.

### Changed

- The `bar` preset is one line taller: previous, source, target.
- The settings schema is 50 fields in 9 categories, none of them labelled "does
  nothing" any more.

### What the measurements say

Interleaved sampling, seven rounds each, because sequential comparison was swamped by
a 3x drift in the same frame between runs:

| | median | share |
| --- | --- | --- |
| OCR, detection + recognition | 198 ms | 100% |
| OCR, detection only | 43 ms | 22% |
| recognition | 155 ms | 78% |

And on translation quality, ten lines, the reason the hybrid design exists:

| path | term accuracy | lines fully correct | mean chrF |
| --- | --- | --- | --- |
| corpus + rules | 53.8% | 40% | 0.139 |
| local model alone | 20.0% | 40% | 0.340 |

Neither alone is usable: the model produces sentences but loses the terminology, the
corpus keeps the terminology but does not produce sentences. Reproduce with
`run.cmd bench_translate --backend corpus|model`.

## [0.0.2] -- 2026-09-13 -- 测试版 (pre-release)

The first tagged build. Everything below is verified by 10 self-check scripts
(`prototype/selfcheck_*.py`), which between them assert a few hundred properties;
run them with `prototype\run.cmd selfcheck_<name> --summary`.

### Added

- **Stability gate** (`capture.settle_ms`). OCR used to fire on the very frame that
  changed, which for animated or scrolling text means recognising half-drawn moving
  glyphs. A change now only arms the trigger; OCR is released once the frame has held
  still for the configured time, and only once. `0` reproduces the previous behaviour
  exactly, so the gate is opt-in. Counters `settle_waits` / `stability_s` make it
  visible whether the gate is doing anything, because without them a gate that never
  fires looks identical to one that works.
- **Settings schema** (`watashi/settings_schema.py`): 49 settings in 9 categories,
  each with a Chinese label, a description, a type, a range, and whether it applies
  live or needs a restart. Costs and measured traps are stated next to the control
  rather than only in a README.
- **Schema-driven settings page** in the web panel. It contains no hardcoded setting
  key: adding a field to the schema makes it appear with its explanation. The desktop
  window shows the same reference, read-only, refreshed whenever any surface changes
  a setting.
- **`config.user.yaml` override layer.** Settings changed in a UI are written here
  and merged over `config.yaml`, which is never rewritten in place because its
  comments are the documentation.
- **Panel window resizing** by dragging the bottom-right corner, with a minimum size,
  remembered across rebuilds. `overlay.panel_width` had been accepted and never read.
- **`--version`**, and the version is reported through the session so every surface
  can display it.
- **MIT licence** (`LICENSE`). Without a licence file a public repository is "all
  rights reserved" by default, whatever the hosting service's sidebar claims, so
  nobody could legally use, modify or redistribute this.
- **Continuous integration** (`.github/workflows/checks.yml`) running the seven
  display-free self checks on Linux and Windows, Python 3.12. The list of checks
  lives in `prototype/checks.txt` rather than in the workflow, and
  `selfcheck_ci.py` asserts that it and `checks_display.txt` between them account for
  every `selfcheck_*.py` on disk — so a new check cannot be added and then silently
  never run, which is how a green badge starts meaning less than it appears to.

### Fixed

- **Every group-painted preset drew its text outside its own window.** `bar`, `bare`,
  `minimal` and `lines` subtracted the window origin twice, so the plate painted and
  the text did not: a black rectangle with nothing in it. Found because the report
  was "no white text appears at all", while 25 passing assertions had only counted
  canvas items and never checked where they landed.
- **A single event queue shared by two surfaces.** A queue delivers each item to
  exactly one consumer, so `ready` -- which carries the capture region -- could reach
  the console instead of the overlay, leaving in-place layout drawing at the screen
  origin. Subtitles were split between the two surfaces in the same way.
- **The language gate ignored a declared `--source`.** With `--source en --target
  zh-CN` every frame of Chinese text was sent to the model. The gate now trusts a
  decisive script verdict first, then the declaration, then a script guess.
- **Letterless lines reached the model.** A clock, a countdown, a progress counter or
  a row of symbols produced a fresh "sentence" on every tick, each missing the
  translation cache, so the model ran continuously on text with no language in it.
- **`Server.stop()` did not verify that the server stopped.** It joined once, and
  cleared its handles even on timeout -- which an open SSE stream reliably causes,
  since graceful shutdown waits for in-flight connections. The zombie kept the port,
  the next start failed to bind, and requests went to a server with a different
  session. This is why the web self check failed intermittently at different lines.
  `stop()` now escalates to `force_exit` and reports whether it actually stopped.
- **`Source` was silently overridden in the language gate**, and `panel_width` was
  stored and never read; both are described above but are listed here as user-visible
  behaviour changes.

### Known limitations

- **The C++ mainline in `src/` is still placeholder scaffolding.** All working code
  is the Python prototype. Milestones M5 (cross-platform) and the port itself have
  not started.
- **R4's latency budget is only partly met.** A subtitle strip runs 89-230 ms against
  a 150 ms budget, and a full window of dense text costs 1-4 s per frame.
- **`overlay.dim_low_confidence` and per-element opacity do nothing.** The values are
  carried in the presentation spec and ignored by the painter. The settings page says
  so on the field itself rather than pretending the control works.
- **Already-translated sentences are not de-duplicated.** A line whose OCR reading
  changes by a punctuation mark is a new cache key, so the model re-runs.
- **`inplace` cannot follow moving text.** The plate is drawn from the previous OCR
  result, so it lags anything that moves; following it smoothly would need a cheap
  correlation tracker between OCR runs, which is not implemented.
- **Language detection is script-based.** Any Latin-script language other than
  English is guessed as English, so `--source` must be given explicitly; kanji-only
  Japanese reads as Chinese.
- **Python plugins cannot be sandboxed.** They run in the engine's process, so they
  are for the machine's owner, not for distribution. The `corpus_loader`, `renderer`
  and `translator` extension points are reserved but not wired.
- **Five self checks need a display** (`overlay`, `window`, `selector`, `desktop`),
  so they cannot run in headless CI.
- **`run.sh` has never been executed.** Bash cannot start in the development sandbox,
  so the Linux and macOS paths are written but unverified.
- **The web panel page and the desktop window have never been seen by the author of
  the code.** They are verified by parsing the page's JavaScript, by the API payloads,
  and by assertions on the served HTML -- not by looking at them.

## [0.0.1] -- unreleased

The initial import: capture, change detection, RapidOCR, the layered corpus and rule
engine, the local NLLB-200 model with term protection, the floating overlay, and the
engine/UI event contract. Never tagged.
