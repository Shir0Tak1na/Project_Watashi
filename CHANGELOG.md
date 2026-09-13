# Changelog

All notable changes to this project are recorded here.

This project is **pre-release software**. The version numbers stay in `0.0.x` on
purpose: what exists today is the Python validation prototype, not the C++ product
described in `Project Watashi.md`. It is usable for testing and it is honest about
what it does not do yet -- see "Known limitations" below, which is part of the
release rather than a footnote.

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
