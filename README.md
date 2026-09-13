# Project Watashi

English | [简体中文](README_zh.md)

*Real-time screen region translation for slang and fictional vocabulary — fully local.*

Project Watashi (渡) is a desktop tool that continuously recognizes text in a
user-selected screen region and translates it into a target language. It is built
for the cases general-purpose translators handle badly: internet slang,
cultivation/fantasy jargon, and invented sci-fi terms.

Everything on the main path runs on the local machine: no cloud API is required,
and the app works offline.

## Core features

- **Real-time region translation** — pick a screen region and a language pair;
  the floating overlay shows the original and translated text side by side, with
  no copy-paste required.
- **Fully local** — screen capture, OCR, corpus lookup, rule inference, and the
  optional LLM all run on-device. Cloud LLM APIs exist only as an optional,
  off-by-default plugin.
- **Custom corpus** — extend coverage for slang and fictional words by writing
  corpus files. No engine changes, no recompilation.
- **Custom rules** — words missing from the corpus are handled by user-defined
  rules (word-root decomposition, naming templates, transliteration, domain
  mapping, morpheme composition). Every rule hit reports the rule id and a
  confidence score so results stay auditable.
- **Optional local LLM** — a local model (e.g. a quantized GGUF model via
  llama.cpp) can assist only when corpus and rules both miss.
- **Low latency first** — capture, OCR, and inference stay off the UI thread,
  with buffer reuse, incremental recognition, and result caching.

## Design principles

1. **Local-first** — the main path never requires network access.
2. **Externally extensible** — corpora, rules, and plugins are plain files; the
   engine itself does not change.
3. **Explainable** — every translation records whether it came from the corpus,
   a rule, or the LLM, plus its confidence.
4. **Thread isolation** — OCR and LLM inference never block the UI.

## Translation pipeline

```
hotkey / region select
  -> screen capture
  -> image preprocessing
  -> OCR
  -> text normalization
  -> corpus lookup
  -> rule inference
  -> (optional) local LLM
  -> result cache
  -> bilingual overlay
```

## Status

The **Python prototype in `prototype/`** implements M1-M3 and runs today: region
capture, local OCR, a file-driven corpus and rule engine, a local NMT model with
term protection, and a click-through subtitle overlay. See
[`prototype/README.md`](prototype/README.md) for measured latency numbers and
known limitations.

The **C++ tree in `src/`** is still a skeleton: it has not yet been ported, and
every layer below is a placeholder.

| Area | C++ tree (`src/`) | Python prototype (`prototype/`) |
| --- | --- | --- |
| Screen capture / region selection | Stub — returns a blank image | Working (`mss`, plus drag-to-select) |
| OCR | Stub — returns hardcoded text | Working (RapidOCR ONNX, offline) |
| Corpus loader | Static JSON, 5 entries each | Working, layered + hot reload |
| Rule engine | Not implemented | Working (affix / morpheme / template / transliterate) |
| Local translation model | Not implemented (`src/llm/` empty) | Working (NLLB-200 int8 via CTranslate2) |
| Term protection (corpus-authoritative translation) | Not implemented | Working, with fallback reporting |
| Storage / cache | Not implemented | In-memory only |
| UI, floating overlay | Not implemented | Working (subtitle bar + panel) |
| Desktop control window | Not implemented | Working (`--desktop`: six tabs, shares one Tk root with the overlay) |
| Plugins | Not implemented; Python bridge prototype | Extension points v1 with failure isolation, 2 example plugins |
| UI surface architecture | Not implemented | Working: engine/UI event-contract boundary, CLI, overlay, desktop window and in-process web panel all share one schema |
| Customization (appearance, layout, profiles) | Not implemented | Working: declarative presentation spec, 7 presets, live switching, memory tiers |
| Tests | Directories only | `--selftest` plus 8 self-check scripts, 298 assertions |

## Runtime requirements

Python prototype (runs today):

- Python 3.12; see `prototype/requirements.txt`
- tkinter (standard library) for the overlay
- Optional: a local NLLB model, fetched once by `prototype/fetch_model.py`
  (~617 MiB). Without it the prototype runs on corpora and rules alone.

Native build (not yet functional):

- CMake >= 3.16 and a C++17 compiler
- SQLite3, OpenCV (required by `CMakeLists.txt`)
- Optional: Qt 6 (UI), ONNX Runtime, llama.cpp, PaddleOCR

## Build

The prototype needs no build step — run it directly:

```bash
prototype\run.cmd --selftest
```

> **Use the launcher, not bare `python`.** On many Windows machines `python` on
> PATH is the Microsoft Store *app execution alias* — a zero-byte reparse point
> that opens an "application cannot be opened" dialog or the Store instead of
> running Python. `prototype\run.cmd` (and `prototype/run.sh` on Bash) resolve
> the project's own interpreter, so the documented commands work regardless of
> what `python` means locally. The launcher also picks the script for you:
>
> ```bash
> prototype\run.cmd --selftest            # watashi_proto.py --selftest
> prototype\run.cmd fetch_model --check   # fetch_model.py --check
> prototype\run.cmd bench_nmt --threads 4 # bench_nmt.py --threads 4
> ```
>
> To stop those dialogs entirely, turn off the aliases in
> **Settings → Apps → Advanced app settings → App execution aliases**
> (`python.exe`, `python3.exe`).

The native target does not build yet:

```bash
cmake -S . -B build
cmake --build build
```

> `CMakeLists.txt` calls `add_subdirectory(tests)` with `BUILD_TESTS=ON` by
> default, but `tests/` does not yet contain a `CMakeLists.txt`. Configure with
> `-DBUILD_TESTS=OFF` until that file is added.

Dependency switches:

```bash
cmake -S . -B build -DUSE_QT=OFF -DUSE_ONNX=OFF -DUSE_PADDLEOCR=OFF -DBUILD_TESTS=OFF
```

## Run

The working prototype:

```bash
prototype\run.cmd --select --mode both   # drag a region, then run
prototype\run.cmd --list-monitors
prototype\run.cmd --mode none --duration 20 --print   # headless
```

The native target (`./build/ProjectWatashi`) only prints a startup line; it is
not implemented yet.

## Configuration

Settings live in `config/app.yaml`:

```yaml
translation:
  default_source: auto      # source language, or auto-detect
  default_target: zh-CN     # target language
  cache_enabled: true
  use_llm: true             # local LLM, not a cloud API
  llm_backend: llama_cpp

ocr:
  provider: paddleocr
  capture_area_enabled: true

rules:
  slang_file: rules/slang/internet_slang.json
  fiction_file: rules/fiction/fantasy_terms.json

storage:
  db_path: data/project_watashi.db
```

## Custom corpus format

Corpus files are JSON, keyed by source term. Directories by layer:

| Layer | Path | Priority |
| --- | --- | --- |
| User private | `plugins/user/custom_rules/` | Highest |
| Internet slang | `rules/slang/` | Medium |
| Fictional vocabulary | `rules/fiction/` | Medium |
| General dictionary | `rules/dictionaries/` | Lowest |

Example (`rules/fiction/fantasy_terms.json`):

```json
{
  "sword": "剑",
  "dragon": "龙",
  "spirit": "灵力",
  "void": "虚空",
  "realm": "境界"
}
```

Entries may be extended with part of speech, domain tag, priority, source, and
update time. Lookup order is user private > domain > general, with longest match
winning inside a layer.

## Custom rules

Rules cover words that the corpus does not contain:

| Type | Purpose |
| --- | --- |
| Word-root / affix split | Combine known morphemes |
| Naming template | Apply a work's naming convention |
| Transliteration | Phonetic render into the target language |
| Domain mapping | Map to established terminology |
| Morpheme composition | Join known terms into new ones |

Rules are external files, same as corpora. Corpus hits always beat rule
inference, and a rule result can be saved into the corpus with one action.

## Plugins

Extension points: translation post-processing, corpus loaders, rule sets, OCR
pre/post-processing, overlay rendering, and export formats.

- `plugins/builtin/` — shipped with the app
- `plugins/user/` — user-installed

- C++ — `include/plugins/plugin_api.h` (`IPlugin`), still a draft
- Python — `prototype/watashi/plugins.py` (`PLUGIN_API_VERSION = 1`), **working**

On the Python side, `postprocess` and `export` are connected and
`corpus_loader` / `renderer` / `translator` are reserved but not yet called.
Four failure modes — an incompatible API version, a raised exception, a wrong
return type, and an unknown extension point — are all caught, reported and
skipped rather than taking down the host (`selfcheck_plugins`, 35 assertions).

Plugins are expected to be versioned, isolated, and sandboxed: one failing
plugin must never take down the host, and network access is denied by default.

> **Python cannot deliver the sandboxing half of that**, and the prototype does
> not pretend otherwise: a plugin runs in the engine's own process, so there is no
> privilege drop and no network interception. They are therefore trusted plugins
> for the machine's owner, not something to distribute. Real isolation needs a
> child process with a restricted token, which is work for the C++ core.

## Repository layout

```
ProjectWatashi/
├── CMakeLists.txt
├── .gitignore       # excludes .venv/ and models/ (617 MiB of weights)
├── config/          # app.yaml and user preferences
├── docs/            # architecture, OCR design, plugin API, presentation spec
├── include/         # public headers (app, core, db, llm, ocr, plugins)
├── src/
│   ├── app/         # entry point and wiring
│   ├── core/        # translation routing (fiction_engine, network_slang planned)
│   ├── ocr/         # capture, preprocessing, OCR scheduling
│   ├── db/          # local SQLite corpus and cache (planned)
│   ├── llm/         # local LLM backend (planned)
│   ├── rules/       # rule engine (planned)
│   ├── ui/          # overlay and main window (planned)
│   ├── plugins/     # plugin loading and isolation (planned)
│   └── utils/       # logging, strings, crypto (planned)
├── models/          # local models (ocr, llm, embedding) — gitignored
├── plugins/         # builtin and user plugins
├── prototype/       # WORKING Python prototype: M1-M6, runs today
├── rules/           # editable corpora (slang, fiction, dictionaries)
└── tests/           # C++ tests; CMakeLists.txt only, no sources yet
```

The empty `ui/` and `scripts/` directories from the original scaffold were
removed: empty directories cannot be committed, nothing referenced them, and the
`ui/` role is served by `src/ui/` in the C++ tree and `prototype/watashi/` today.

## Roadmap

| Stage | Goal |
| --- | --- |
| M1 | Minimal loop: hotkey region select -> capture -> OCR -> corpus lookup -> bilingual overlay (Windows) — **done in `prototype/`** |
| M2 | Corpus and rule engine with user authoring and hot reload — **done in `prototype/`** |
| M3 | Local model fallback with streaming output and caching — **done in `prototype/`** (local NMT, asynchronous refinement rather than streaming) |
| M4 | Latency budget met: incremental recognition, caching, thread model tuning — **partially measured** |
| M5 | Cross-platform: Linux and macOS capture and UI adapters |
| M6 | Stable plugin API; experimental Android target — **plugin API v1 done in `prototype/`**, Android not started |

## Portability targets

| Platform | Level | Key dependency |
| --- | --- | --- |
| Windows | Primary | Desktop capture API, global hotkey |
| Linux | Primary | X11 / Wayland capture, XTest |
| macOS | Primary | ScreenCaptureKit / CGWindowList |
| Android | Secondary | MediaProjection, overlay and accessibility permissions |
| Small devices | Tertiary | Trimmed UI and models, core path only |

## Notes

- The app performs no cloud calls by default and works offline.
- Local processing keeps documents, glossaries, and translation history on the
  user's machine.
- The previous README described an unrelated Tkinter + Google Translate
  prototype (`app.py`) that is not part of this repository; it has been replaced
  by this document.
