# Changelog

All notable changes to this project are recorded here.

This project is **pre-release software**. The version numbers stay in `0.0.x` on
purpose: what exists today is the Python validation prototype, not the C++ product
described in `Project Watashi.md`. It is usable for testing and it is honest about
what it does not do yet -- see "Known limitations" below, which is part of the
release rather than a footnote.

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
