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
| Corpus loader | Not implemented — `src/` has no corpus code at all | Working, layered + hot reload |
| Rule engine | Not implemented | Working (affix / morpheme / template / transliterate) |
| Local translation model | Not implemented (`src/llm/` empty) | Working (NLLB-200 int8 via CTranslate2) |
| Term protection (corpus-authoritative translation) | Not implemented | Working, with fallback reporting |
| Storage / cache | Not implemented | In-memory only |
| UI, floating overlay | Not implemented | Working (subtitle bar + panel) |
| Desktop control window | Not implemented | Working (`--desktop`: 字幕 / 采集 / 插件 tabs, sharing one Tk root with the overlay) |
| Plugins | Not implemented; `include/plugins/plugin_api.h` is still a draft | Extension points v1 with failure isolation, 2 example plugins |
| UI surface architecture | Not implemented | Working: engine/UI event-contract boundary, CLI, overlay, desktop window and in-process web panel all share one schema |
| Customization (appearance, layout, profiles) | Not implemented | Working: declarative presentation spec, 7 presets, live switching, memory tiers |
| Tests | Directories only | `--selftest` plus 20 self-check scripts (15 headless in CI, 5 that need a display); `prototype/README.md` records the current assertion counts |

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

Vocabulary can be managed without the UI, on a machine that has no browser:

```bash
prototype\run.cmd --list-scenes                       # scenes, current scene, conflicts
prototype\run.cmd --export-corpus corpus.json          # edit it, then write it back
prototype\run.cmd --import-corpus terms.csv --dry-run  # report, write nothing
prototype\run.cmd --promote-corrections --dry-run      # 实时纠正 -> corpus entries
```

The native target (`./build/ProjectWatashi`) only prints a startup line; it is
not implemented yet.

## Configuration

The working prototype reads `prototype/config.yaml`, and every path inside it is resolved
relative to that file, so the prototype can be launched from anywhere. Settings changed in
a UI are **not** written back there: they go to `prototype/config.user.yaml`, a thin
override layer merged over the file at load time. That is deliberate — the comments in
`config.yaml` are the documentation for these settings and a YAML round trip would delete
all of them — and it makes "put it back" a one-file delete. `--config PATH` points the
prototype at a different file.

The bad news is that `config/app.yaml` is still in the tree: it is a placeholder for the
C++ skeleton and **no code reads it**. The authoritative list of settings is
`prototype/watashi/settings_schema.py` — one declaration per setting, carrying a key, a
label and a description, which is what the desktop settings form and the web panel render
— and the shipped defaults are the `DEFAULTS` dict in `prototype/watashi/config.py`. An
unrecognised key in a config file is ignored in silence, so a typo looks exactly like a
setting that does nothing; the settings panel is the reliable place to see what exists.

A minimal file, in the shape the loader expects (every key and value below is real):

```yaml
capture:
  region: null          # "x,y,w,h" in physical pixels; null = a strip across the bottom
  fps: 10               # capture rate; OCR only runs on a frame that changed
  settle_ms: 0          # hold OCR back until the frame has been still this long

translation:
  source: auto          # auto-detect, or name it: auto cannot tell French from English
  target: zh-CN
  scene: ""             # which scene's term entries win; empty = no preference
  nmt_model: null       # null = corpus + rules only; the shipped file names the model

corpus:
  domain: [corpus, ../rules/slang, ../rules/fiction]
  auto_reload: true     # re-read corpus files when their modification time changes

overlay:
  mode: both            # bar | panel | both | none
```

The shipped `config.yaml` is much longer, and worth reading rather than copying: its
comments carry the measured reason behind each default (why `ocr.det_limit_type` is `max`
and not RapidOCR's `min`, why `max_width` stays `0`).

## Custom corpus format

Corpus files are JSON. An entry is keyed by *(target language, source term, scene,
conditions)* — the four things that decide which answer is right for the line in front of
you. Directories by layer:

| Layer | Path | Priority |
| --- | --- | --- |
| User private | `plugins/user/custom_rules/` | Highest |
| Internet slang | `rules/slang/` | Medium (domain layer) |
| Fictional vocabulary | `rules/fiction/` | Medium (domain layer) |
| General dictionary | `rules/dictionaries/` | Lowest |

Example — `rules/fiction/fantasy_terms.json` as it ships:

```json
{
  "lang": "zh-CN",
  "entries": {
    "sword": "剑",
    "dragon": "龙",
    "spirit": "灵力",
    "void": "虚空",
    "realm": "境界"
  }
}
```

An entry may also carry `pos`, `priority` and `note`. `pos` and `note` are carried through
and displayed, and never affect a lookup; `priority` breaks a tie between two entries in
the same layer, scene and condition set. Lookup order is user private > domain > general,
with longest match winning inside a layer.

**A corpus is not language-neutral.** An entry declares the language of its
translation — once per file with `"lang": "zh-CN"` plus an `"entries"` object, or
per entry with its own `lang` — because an English→Chinese vocabulary asked for
Japanese would otherwise answer *with Chinese, at full confidence*. An entry that
declares nothing is usable for any target, so every corpus written before this
existed keeps working. The same source term may appear once per language, scene and set of
conditions. Languages are compared as languages (`zh` = `zh-CN` = `zho_Hans`), and the
languages a corpus can answer for are reported in the `ready` event, the desktop window and
the web panel.

### One word, several meanings

Writing a second meaning used to overwrite the first **in silence**: both landed on the
same key, and nothing anywhere said so. The key now carries the scene and the conditions
as well, and because a JSON object cannot hold one key twice, a source with more than one
answer is written as a **list**:

```json
{
  "bank": [
    {"target": "银行", "domain": "finance"},
    {"target": "岸", "domain": "geography"}
  ]
}
```

There are two ways to say which meaning applies.

**Scene** (`domain` on the entry) is the one you pick. Set `translation.scene` in the
settings panel, type it in the 场景 box in the desktop top bar next to 目标语言, or pass
`--scene NAME`. Empty — the default — means no preference, which is exactly the behaviour
before scenes existed. Scene names are compared case-folded, so `Finance` and `finance` are
one scene.

**Conditions** are evidence the engine reads for itself, and *all* of them must hold:

| Key | Holds when |
| --- | --- |
| `when_line` | the regex matches anywhere in the whole recognised line |
| `when_near` | one of these words appears in the line **outside** the matched term |
| `when_window` | the regex matches the title of the window the text was captured from |

`when_near` looks at the line with the matched term cut out, so an entry for "bank" cannot
satisfy `when_near: ["bank"]` with itself. An entry that requires a window is not applied
when there is no window — a fixed screen region has no title — which is what keeps a
window-specific term out of a region capture.

When several entries could answer, the order is:

1. **layer** — the user's own file always beats a shipped file, even when the shipped
   entry names the scene you selected;
2. **scene** — exact match, then an entry that names no scene, then a different scene;
3. **`priority`**, then a deterministic tail, so the answer never depends on which file was
   read first.

Conditions are a **filter, not a weight**: they decide whether an entry is eligible at all.
An entry whose conditions do not hold is not ranked lower, it does not compete.

**Two entries that really are the same** — same term, language, scene and conditions — but
give *different* translations: one wins deterministically and the loser is now reported
rather than dropped in silence. It is printed when the corpus loads, listed by
`--list-scenes`, and shown in the web panel's 语料库 tab with the reason. Two entries that
agree on the translation are not reported: that is one statement written twice, and a
diagnostic that cries wolf is one nobody reads. The fix for a reported conflict is to give
the two entries different scenes or different conditions — the only way to say which one
applies when.

Corpus edits take effect while the program is running: the engine checks file
modification times before each translation and reloads on change, throttled to once
per 500 ms (`corpus.auto_reload`, `corpus.reload_interval_ms`).

**The caches key on the situation, not on the text.** The recent-translation memory (a
source seen again within 10 s is not translated twice) used to be keyed on the source text
alone, so switching the target language inside that window served the previous language's
text back. It is now keyed on the dimensions the loaded vocabulary can actually
distinguish: always the target language, plus the scene or the window only when some entry
depends on one. A corpus with no scenes and no window conditions therefore caches exactly
as it did before, and one that answers differently per scene cannot hand back the other
scene's answer. The translation cache and the model's refinement cache use the same key.

**Real time correction.** When the corpus itself is wrong, the wrong answer is a
confident hit rather than a gap, and more vocabulary cannot fix it. So the fix is
made where the mistake is seen: click the line in the desktop 字幕 tab, type the
right translation, save. It is written to `corrections.json` in the user private
layer, applied to the frame on screen immediately, and used for that sentence from
then on. A `line` correction matches the whole sentence while ignoring the
whitespace and edge punctuation OCR varies between frames; a `term` correction
replaces that word in any sentence. A correction records the target language it was
written for: a whole-line correction typed while translating into Chinese used to keep
winning after the target switched to Japanese, which reads as Chinese text labelled
Japanese. A correction with no language — every file written before this existed — still
applies to any target, and the engine reports those separately as **untagged** rather than
guessing a language for them. See `prototype/README.md` for the engine commands
(`correct`, `list_corrections`, `remove_correction`).

**Editing the corpus in the UI.** The web panel's 语料库 tab is the editor: a table of
every entry from every layer, with override, suppress, revert, import and export, the scene
each row declares, and the conflicts the loader found. **Editing a shipped entry does not
rewrite the shipped file** — it writes an override into the user layer, and the UI shows
what it overrode, what that entry used to say, and offers one-click revert. Import takes a
file or pasted text; export handles JSON (the corpus's own shapes) and CSV/TSV (Chinese
headers understood, downloads written for spreadsheets), with other formats going through
the plugin `corpus_loader` extension point. Every edit takes effect immediately.

A term answered more than once — two languages, or one language in two scenes — is stored
as a *list* of entries, and the editor writes and reads that form. It did not always: it
could write the list but not read it back, so such a term showed as no rows at all and the
next save wrote the empty table over the file. Reading, writing, importing and exporting
handle it now.

**Bulk vocabulary.** Three ways to add many entries already existed: the panel's file
import, dropping a file into the user corpus layer (`plugins/user/custom_rules/`), and the
`corpus_loader` plugin extension point. There are now four more:

- the panel takes **pasted text**, not only a file — one `source,target` line each, or a
  header-driven table;
- the panel can **preview** an import before writing it: 「预览」 and 「导入」 call the *same*
  code with one extra `dry_run` flag, so the preview's "added / updated / skipped" is the
  number the import will produce rather than a second guess at it;
- recorded corrections can be **promoted into entries** in bulk — 「提升为词条」 in the
  panel's 语料库 tab, under 实时纠正, or `--promote-corrections`. The corrections file and
  the corpus stay separate files; nothing is promoted unless you ask, and promotion never
  edits or deletes a correction;
- the **command line** can do all of it headlessly:

  | Command | Effect |
  | --- | --- |
  | `--import-corpus FILE` | add entries from a file |
  | `--export-corpus FILE` | write the corpus out as a file |
  | `--promote-corrections` | copy recorded corrections into the corpus |
  | `--list-scenes` | the scenes the corpus declares, the current one, and every conflict |
  | `--scene NAME` | select the active scene (empty clears it) |
  | `--corpus-format json\|csv\|tsv` | format for import/export; default from the extension, else json |
  | `--corpus-scope user\|effective` | export your own entries or everything in effect; for `--promote-corrections`, `line` / `term` instead |
  | `--keep-existing` | import/promote: skip rows that already exist instead of replacing them |
  | `--dry-run` | import/promote: report what would happen and write nothing |

CSV and TSV import/export understand
`source,target,lang,pos,domain,when_line,when_near,when_window,note`, and accept Chinese
header names too — the exact list is `_COLUMNS` in `prototype/watashi/library.py`. A
source answered more than once exports as the list form, because that is the only shape a
JSON object can carry two answers in.

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

Extension points: translation post-processing, corpus loaders, export formats, overlay
rendering, and a replacement translation backend — `postprocess`, `corpus_loader`,
`export`, `renderer` and `translator` in `prototype/watashi/plugins.py`.

- `plugins/builtin/` — shipped with the app
- `plugins/user/` — user-installed

- C++ — `include/plugins/plugin_api.h` (`IPlugin`), still a draft
- Python — `prototype/watashi/plugins.py` (`PLUGIN_API_VERSION = 1`), **working**

On the Python side, `postprocess`, `export` and `corpus_loader` are connected —
`corpus_loader` is what lets a plugin read a corpus format other than the built-in JSON —
and `renderer` / `translator` are reserved but not yet called; `--list-plugins` reports
which is which, so an author sees what is not connected rather than discovering it by
having their plugin ignored. Four failure modes — an incompatible API version, a raised
exception, a wrong return type, and an unknown extension point — are all caught, reported
and skipped rather than taking down the host (`selfcheck_plugins`, 35 assertions).

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
├── config/          # app.yaml — C++ skeleton placeholder, read by no code yet
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
├── prototype/       # WORKING Python prototype: M1-M3 plus plugin API v1, runs today
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
