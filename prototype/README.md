# Project Watashi -- working prototype (M1 + M3 + UI architecture + desktop UI)

A runnable Python prototype of the pipeline the project is built around:

```
capture region -> change detection -> local OCR -> corpus + rules -> on-screen overlay
                                                          |
                                                          +-> local NMT model (async refinement)
```

It exists to answer four questions with measurements instead of assumptions:

1. **Does the whole path work locally, with no cloud API?** (requirement R1)
2. **Are corpora and rules enough to extend translation, edited live as files?** (R2, R3)
3. **Is the R4 latency budget actually reachable on real hardware?** (R4)
4. **Can a local translation model raise quality without breaking terminology,
   and without breaking the latency budget?** (R1, M3)

Answers: 1, 2 and 4 yes with documented caveats; 3 yes for realistically sized
regions — the numbers and the failure modes are spelled out below.

---

## Architecture: one engine, several surfaces

Everything is built on a single boundary, defined in `watashi/events.py` and
owned by `watashi/session.py`:

```
                    ┌─────────────────────────────────────┐
                    │  Session  (engine, no UI imports)    │
                    │  pipeline · translator · corpus ·    │
                    │  OCR · capturer · memory sampler     │
                    └──────────────┬──────────────────────┘
                                   │
                  events out  ─────┴─────  commands in
                  ready/subtitle/          pause, resume, set_region,
                  refinement/stats/        set_target_lang, set_fps,
                  status/presentation/     reload_corpus, set_presentation,
                  error/stopped            load_profile, shutdown
                                   │
        ┌──────────────┬───────────┴────────┬──────────────────┐
        │              │                    │                  │
   CLI / PowerShell  floating overlay   local web panel   (future) LAN
   --json-lines      in-process queue   SSE + POST        same schema
   ConsoleAdapter    OverlayAdapter     web.py            any transport
```

Why this shape:

- **Every surface is an adapter, not a second application.** The CLI, the
  overlay and the web panel consume identical events, so a new surface is an
  adapter (~100 lines), not a rewrite.
- **`events.py` imports nothing**: no tkinter, no framework, no model. The
  engine can run with no UI toolkit present at all.
- **The schema is versioned and sequenced.** Every envelope carries `v`, `seq`
  and `ts`, so a client can detect an incompatible stream instead of silently
  misreading subtitles, and can rely on ordering.
- **The capturer is injectable**, which is what makes the engine testable
  headlessly (`SyntheticCapturer`). Only the overlay window itself needs a
  display, and it has its own self check.

### Surfaces

| Surface | How to run | Notes |
| --- | --- | --- |
| CLI (text) | `watashi_proto.py --mode none --print` | Human readable |
| CLI (JSON lines) | `watashi_proto.py --json-lines` | One envelope per line, for scripts |
| Floating overlay | `watashi_proto.py --mode bar\|panel\|both` | Native, click-through, 6 MiB |
| Desktop window | `watashi_proto.py --desktop` | The live view plus the controls that need a native window; shares one Tk root with the overlay |
| Local web panel | `watashi_proto.py --serve`, or 「打开设置面板」 in the desktop window | Settings and the corpus editor; in-process, loopback only by default |
| No screen at all | `--synthetic` | Drives the engine with rendered frames |

The web panel runs **inside the engine process**, so it shares the loaded OCR
engine, corpus and model. A second process would duplicate 715 MiB of weights
for nothing. It uses SSE rather than WebSockets because subtitle delivery is
one-directional and SSE needs no extra dependency, and the page inlines all CSS
and JS so it cannot reach the network — requirement R1 is about running offline,
and one stray CDN link would quietly break that.

**The division of labour between the two windows**, settled by removing what was
duplicated rather than by documenting it:

| | Desktop window | Web panel |
| --- | --- | --- |
| Live subtitles | yes, with the correction editor | yes, rendered with the same spec |
| Pause, region drag, window pick, target language | yes — these need a native window or a hotkey | no |
| Plugins, export formats | yes | — |
| The 50-field settings form | no | yes, editable, each field explained |
| Presentation spec editor | no | yes, full editor |
| Counters | one status line | full JSON plus the event log |
| **Corpus: entries, override, suppress, import/export** | read-only notice | **yes** |

Four tabs used to exist on the desktop window for the settings form, the
presentation spec, the raw counters and the memory tiers. They were removed: each
one was a second editor of state the panel already edited better, and two surfaces
editing one file is how they start disagreeing. The desktop window keeps what a
browser cannot do — dragging a region on the real screen, picking a window,
`Escape`-able pause, and the target language, which is the one setting a user
changes while watching — and 「打开设置面板」 starts the panel in-process and
opens it, so the surface that edits is one click away rather than a sentence in a
tooltip.

The panel's command allowlist grew for the same reason: it was "settings and
viewing only", and it now includes the corpus editor, because a vocabulary table,
a file picker and a fifty-field form are things a browser does well and tkinter
does poorly. What is still refused server side is runtime control — `pause`,
`resume`, `shutdown`, `reload_corpus` — because a stray click in a browser tab must
not be able to stop the subtitles. The allowlist is enforced in `/api/command`,
which answers 403 with an explanation, so posting `pause` directly is refused too.

### Commands

Commands are validated, not silently ignored, and every one returns a result:

| Command | Effect |
| --- | --- |
| `pause` / `resume` / `toggle_pause` | Deterministic: a frame in flight is dropped, so pause really stops output |
| `set_region` | `"x,y,w,h"`; resets change detection. Also **leaves window capture** — see below |
| `use_window` | `{"spec": "<index\|title>", "hwnd": 123, "sub_region": "0,0.6,1,1"}`; captures a window and follows it |
| `set_target_lang` | Switches target and clears stale refinements |
| `set_fps`, `set_diff_threshold` | Live tuning |
| `reload_corpus` | Re-reads corpora and rules from disk. Corrections are picked up either way: the engine also reloads by itself, see below |
| `correct` | `{"source", "target", "scope": "line\|term", "note"}` — records a human correction into the user corpus layer and applies it to the frame on screen now. See [Real time correction](#real-time-correction) |
| `list_corrections` / `remove_correction` | Read the correction list, or undo one by its source text |
| `set_corpus_reload` | `{"auto_reload": bool, "reload_interval_ms": int}` — the runtime form of two settings |
| `library_list` | Every entry the editor shows, from every layer, with what is hidden |
| `library_put` | `{"source", "target", "lang", "pos", "note"}` — add an entry, or override a shipped one |
| `library_delete` | `{"source", "lang"}` — remove one of your entries; for an override this *is* the revert |
| `library_suppress` / `library_restore` | Hide a shipped entry, or bring it back |
| `library_import` | `{"text", "format", "replace"}` — apply an uploaded file |
| `library_export` | `{"format", "scope", "path"}` — write the user's entries or everything in effect |
| `set_presentation` | A preset name, or a partial spec merged over the current one |
| `load_profile` | Applies a profile, including the memory tiers |
| `status`, `shutdown` | Introspection and lifecycle |

The web panel may only issue the **settings** subset of these
(`set_presentation`, `load_profile`, `set_target_lang`, `set_fps`,
`set_diff_threshold`, `set_region`, `status`). Everything else — `pause`,
`reload_corpus`, `shutdown` — is refused with 403 and a pointer to the CLI.

### Desktop UI (`--desktop`)

A single window with seven tabs over the same `Session` the CLI uses:

| Tab | What it does |
| --- | --- |
| 字幕 | Current translation large, source line under it, a scrolling bilingual history with provenance (`语料库` vs `模型`) and per-line latency, and the **correction editor**: click a history line, fix the translation, save |
| 采集 | The current region, capture fps, change threshold — each applied through `session.command()` |
| 翻译 | Target language, the memory-tier profiles (`lean` / `balanced` / `full`), corpus size |
| 呈现 | One button per presentation preset; applies to the overlay live |
| 插件 | Loaded plugins, their extension points, failures, and the export formats they register |
| 设置 | The whole settings schema, read-only, with every label and description |
| 诊断 | Raw counters as JSON — the same numbers a `stats` event carries |

Controls in the top bar: pause/resume, drag a new region, pick a window from a
list, export, reload the corpus, the target language, and 「打开设置面板」.

The pause button changes the moment you click it, and says what it means next to itself
(`已暂停：不再识别屏幕`). That sounds like nothing until you know the bug it replaced: stats
were only published at the end of a processed frame, a paused pipeline processes none, so
the last stats event stayed `paused: False` forever and every surface kept saying
"recognising" — pressing stop changed nothing you could see. Now pausing and resuming
publish a stats event of their own, and the window sets the button from the command result
rather than waiting for any event at all.

Three things worth knowing:

- **It is an adapter, not a second application.** Every button issues a command
  through `session.command()` and every view is fed by the event stream, so
  nothing works here that does not work through the boundary. A refused command
  is shown in the history instead of being swallowed.
- **It shares one Tk root with the overlay.** Two `Tk()` instances in one process
  have separate interpreters and widgets cannot cross between them, so
  `overlay.start(root=...)` takes a host root and `close()` only destroys a root
  it created itself. Closing the window takes the overlay down too.
- **Its widgets are driven from a queue, never from the worker threads.** The
  window pumps a `queue.Queue` from an `after` tick, so OCR and the model can
  publish freely without touching Tk.

The region selector is non-blocking for the same reason: a blocking nested loop
would stop the pump, so the panel would freeze for as long as the selector is
open. With an overlay present, the desktop button reuses the overlay's own
reselect path, so both surfaces share one implementation.

### Window capture, and why `set_region` also has to leave it

`--window` and the desktop window picker bind capture to a window instead of a
rectangle, and the client area is re-resolved on every grab, so moving or
resizing the target keeps the capture on it rather than on whatever now occupies
the old coordinates.

That makes the two modes mutually exclusive, and this is not academic: a
`WindowCapturer` *also* has a `set_region` method — a documented no-op, because a
window owns its own rectangle. A session that duck-typed `set_region` therefore
accepted a dragged region, reported success, and kept following the window. The
capturers now declare `KIND = "region" | "window"` and `set_region` replaces the
capture source when the current one cannot honour a rectangle. `selfcheck_desktop`
asserts exactly this: switch to a window, drag a region, and the capturer and the
reported box must both change back.

### Memory profile

Measured with `watashi/memory.py`, which is dependency free (no psutil):

| Configuration | Resident |
| --- | --- |
| Python + modules | 31 MiB |
| OCR only (`--no-nmt`) | 175 MiB |
| OCR + local model + overlay | **890 MiB** (peak 1123 MiB during load) |
| tkinter overlay alone | **6 MiB** |

Two conclusions that shaped the design:

- **The model, not the UI toolkit, is the memory cost.** A browser tab costs
  150-400 MiB, which is 25-65x the overlay, so a web UI is *not* how you serve a
  memory-limited machine on the same box — the CLI and overlay are.
- The overlay being 6 MiB is why it stays native tkinter.

The tiers are exposed as profiles so this is a one-word switch rather than a
mental calculation:

```bash
prototype\run.cmd --list-profiles
prototype\run.cmd --profile lean     # ~175 MiB, no model
prototype\run.cmd --profile balanced # ~890 MiB, normal desktop
```

---

## Customization

Four kinds of customization share one mechanism: a **declarative presentation
spec** (`watashi/presentation.py`) that every surface renders. Full schema in
[docs/presentation-spec.md](../docs/presentation-spec.md).

| Kind | How |
| --- | --- |
| **Appearance** | Fonts, sizes, colours, opacity, outlines, plate vs transparent |
| **Layout** | Anchors, offsets, alignment, per-line blocks, **in-place over the original text** |
| **Behaviour** | Corpora, rules, translation backends — edited as files, hot reloaded |
| **Profiles** | A named bundle of the above per work or project |

The web panel edits appearance, layout and profiles, and shows behaviour
(corpora, rules, stats) read-only. Behaviour changes are made by editing files.

### Presets

```bash
prototype\run.cmd --list-presentations
prototype\run.cmd --presentation inplace
```

| Preset | What it does |
| --- | --- |
| `bar` | Source line above, translation large below, translucent plate |
| `bare` | Same but transparent with outlined text — the burned-in subtitle look |
| `minimal` | Translation only |
| `lines` | One bilingual block per recognised line, stacked |
| `inplace` | **Translation drawn over the original text's position** |
| `panel` | Bilingual history with latency and coverage |
| `hidden` | Recognise and translate, draw nothing |

### Why declarative rather than a code renderer

A code renderer interface would cover layout too, but it means loading user code
into the engine process and Python cannot be sandboxed — which conflicts with the
requirement that a bad plugin must not take down the host and that plugins get
least privilege. A declarative spec avoids that *and* gives the bigger win: one
description, honoured by the tkinter overlay, the web panel and any future
client, so a custom look is written once.

### Live editing

The spec travels the same command channel as everything else, so **any** surface
can change the look and the others follow:

```json
{ "cmd": "set_presentation", "spec": { "layout": { "mode": "inplace" } } }
```

The partial spec is deep-merged over the current one, so a UI changing only the
background need not resend everything. The active spec is then broadcast as a
`presentation` event, which is why the web panel's appearance editor updates the
floating overlay immediately.

### In-place layout and display scaling

`inplace` needs per-line geometry, which is why subtitle events carry a `lines`
array with each line's box:

```json
{ "source": "...", "target": "...", "box": [24, 88, 640, 40], "confidence": 0.95 }
```

Boxes are **region relative**; clients add the origin from the `ready` event.

One subtlety worth knowing: Tk positions windows in **logical** pixels while
capture and OCR report **physical** ones. On this machine at 150% display scaling
that is 1707x1067 versus 2560x1600, a 1.5x difference — enough to put the
translation somewhere unrelated to the text it belongs to. The overlay detects
the ratio, converts, and says so on startup.

### Writing a profile

Drop a YAML file in `prototype/profiles/` (or `config/profiles/`):

```yaml
name: my-novel
description: glossary and layout for one series
memory: balanced
presentation: lines
corpus:
  domain: [glossaries/my-novel]
translation:
  target: zh-CN
capture:
  fps: 8
```

Corpus directories are **added** to the layer lists, never replacing them, so a
profile cannot accidentally hide the user's own vocabulary.

---

## Quick start

```bash
prototype\run.cmd --selftest          # Bash: prototype/run.sh --selftest
```

> **Use the launcher, not bare `python`.** On many Windows machines `python` on
> PATH is the Microsoft Store *app execution alias* — a zero-byte reparse point
> that opens an "application cannot be opened" dialog or the Store instead of
> running Python. `run.cmd` / `run.sh` resolve the project's venv, fall back to
> the `py` launcher, and never rely on what `python` happens to mean locally.
> They also choose the script, so `run.cmd fetch_model --check` runs
> `fetch_model.py --check` and a bare flag runs `watashi_proto.py`.

The self test renders a synthetic subtitle image, runs the real OCR engine, the
real corpus/rule engine and the real translation model over it, and prints
timings. **No screen access is needed**, so it works headless and in CI.

The translation model is a one-time 617 MiB download:

```bash
prototype\run.cmd fetch_model          # resume-safe
prototype\run.cmd fetch_model --check  # report what is present
```

Without it everything still runs, on the corpus path alone. Nothing is ever sent
to a cloud service.

Then, against your real screen:

```bash
# see what monitors are available
prototype\run.cmd --list-monitors

# drag to pick a region, then run with the subtitle bar and the panel
prototype\run.cmd --select --mode both

# or capture a whole window, and follow it as it moves
prototype\run.cmd --list-windows
prototype\run.cmd --window "Edge" --mode both

# a dense window is slow to read; capture just its lower part
prototype\run.cmd --window "Edge" --window-subregion 0,0.6,1,1

# the desktop window: controls, settings and history, with the overlay on top
prototype\run.cmd --desktop
prototype\run.cmd --desktop --no-overlay-with-desktop   # window only, no overlay

# or start immediately on a fixed region
prototype\run.cmd --mode both --region 400,1150,1200,200

# headless: no overlay, print recognised lines and model refinements
prototype\run.cmd --mode none --duration 20 --print

# the settings panel, loopback only
prototype\run.cmd --serve --mode both

# corpus + rules only: no model, lower latency, no sentence-level fluency
prototype\run.cmd --profile lean
```

Press Ctrl+C to stop, or use the hotkeys below.

### Controlling a click-through overlay

The overlay deliberately passes mouse input straight through, so it can never
receive a click of its own — there is no button to press and no window to focus.
Global hotkeys are therefore the only control that works while another
application has focus, which is exactly when the overlay is being used.

| Hotkey | Action |
| --- | --- |
| `Ctrl+Alt+P` | Pause / resume recognition |
| `Ctrl+Alt+H` | Hide / show the overlay (recognition keeps running) |
| `Ctrl+Alt+R` | Drag a new capture region, without restarting |
| `Ctrl+Alt+Q` | Quit |

Reassign or disable them:

```bash
prototype\run.cmd --hotkey-pause ctrl+shift+F9
prototype\run.cmd --hotkey-reselect ctrl+shift+F10
prototype\run.cmd --no-hotkeys --stdin-controls   # p / h / s / q in the terminal
```

If a combination is already owned by another application, registration fails and
the program says so rather than silently doing nothing.

### Choosing what to capture

Three ways, in rough order of how well they survive being moved:

| Mode | How | Notes |
| --- | --- | --- |
| Window | `--window <index\|title>` | Follows the window as it moves or resizes; `--list-windows` to choose |
| Window part | `--window ... --window-subregion 0,0.6,1,1` | Captures a fraction of the window. **Use this for anything text-dense**: recognition cost scales with the number of text boxes, so a full window of text is 1-4 s per frame while a strip is ~100 ms |
| Fixed region | `--region x,y,w,h` or `--select` | `--select` drags a box on screen; `Ctrl+Alt+R` re-draws it while running |

All three are also reachable at runtime: the desktop UI's 选择窗口 button issues
`use_window`, and its 框选区域 button issues `set_region` (which leaves window
capture, as described above).

The selector converts the drag from Tk's **logical** coordinates into the
**physical** pixels the capturer needs. At 150% display scaling those differ by
1.5x, and getting it wrong put the box ~199 px off and 399x149 px too small --
which is exactly what the old selector did. It is also driven in tests by
synthetic drags, because the only other way to notice that class of bug is to
drag by hand and look at where the box landed.

Our own windows are excluded from both the window list and the capture itself, so
neither can become a capture target.

### Two things that would otherwise make it unpleasant

**The overlay is excluded from its own captures.** It draws subtitles on screen;
if the capture region contains them, OCR reads our own output, the pixels change,
change detection fires again — a feedback loop that stutters. Windows'
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` removes the window from
capture while leaving it visible on screen, *and* the content behind it becomes
capturable again, so OCR reads the real screen rather than a black rectangle.
Measured: an overlay occupying 98.9% of a capture over its own box drops to 0.0%,
with the underlying content reappearing at 98.7%.

**Text already in the target language is passed through.** Without this, Chinese
on screen is "translated" into Chinese: your own language comes back rewritten,
the local model spends 300-600 ms per line rephrasing it, and the subtitle keeps
changing for nothing. Recognising source == target costs nothing and skips both
the translation and the refinement. The engine counts it:
`passthrough_lines` in the stats.

The detector is a script heuristic (kana ⇒ Japanese, Hangul ⇒ Korean, Han ⇒
Chinese, Latin ⇒ English), which is enough to answer "is this already the
language we want?" for the pairs this project targets. Its known blind spot is
Japanese written entirely in kanji, which reads as Chinese.

---

## Plugins (customization axis C)

User code extending the engine, discovered from `plugins/builtin/` and
`plugins/user/`. Full contracts in [docs/plugin-api.md](../docs/plugin-api.md).

```bash
prototype\run.cmd --list-plugins
prototype\run.cmd --export srt --export-out subtitles.srt
prototype\run.cmd --no-plugins              # skip discovery entirely
```

| Point | Contract | Connected |
| --- | --- | --- |
| `postprocess` | `(text, context) -> str \| None` | yes — chained over each translated line |
| `export` | `(payload, options) -> str` | yes — `export` command and `--export` |
| `corpus_loader` | `(path) -> dict` | reserved |
| `renderer` | see the presentation spec | reserved |
| `translator` | a `Translator` subclass | reserved |

Reserved points are listed by `--list-plugins`, so an author can see what is not
yet connected rather than having a plugin silently ignored.

Two shipped examples double as executable documentation:
`plugins/builtin/postprocess_tidy.py`, and `plugins/builtin/export_srt.py`
(which provides two formats from one module).

**The plugin model is trust based.** Plugins run in this process and Python
cannot sandbox them, so they can do anything the application can. They are for
your own machine, not for distribution. What *is* guaranteed is narrower but real:
a plugin that raises, returns the wrong type, declares an incompatible API version
or names an unknown point is reported and skipped — never fatal, never silent.

---

## Verifying

Thirteen self checks run without a display, and five more with one:

```bash
prototype\run.cmd selfcheck_presentation --summary   # spec and layout maths
prototype\run.cmd selfcheck_session --summary        # engine boundary + pause reporting
prototype\run.cmd selfcheck_web --summary            # the panel, its editor and its limits
prototype\run.cmd selfcheck_correct --summary        # hot reload + real time correction
prototype\run.cmd selfcheck_corpus --summary         # layering, language, rules, write-back
prototype\run.cmd selfcheck_library --summary        # the corpus editor: override, suppress, import/export
prototype\run.cmd selfcheck_selfcapture --summary    # not reading our own windows
prototype\run.cmd selfcheck_deps --summary           # can a clean environment run these?
prototype\run.cmd selfcheck_plugins --summary        # plugin contracts + failure handling
prototype\run.cmd selfcheck_overlay --summary        # overlay, capture exclusion, hotkeys
prototype\run.cmd selfcheck_window --summary         # window selection and following
prototype\run.cmd selfcheck_selector --summary       # drag-to-select, driven synthetically
prototype\run.cmd selfcheck_desktop --summary        # the desktop window, its tabs and commands
prototype\run.cmd selfcheck_uirender --summary       # photographs the windows, reads them with OCR
```

Counts as of the last full run: 45 / 17 / 29 / 141 / 84 / 112 / 40 / 35 / 49 / 21 / 64 /
94 / 17 / 13 headless and 46 / 22 / 20 / 96 / 7 with a display — 952 checks, all
passing. `selfcheck_uirender` reports 7 with one skip on this machine: its window is 70%
covered by other applications, so the pixel section under that guard never runs. On a
clear desktop it is 11.

### Not reading our own output

The engine photographs a rectangle of the screen. If one of our own windows is inside it,
it reads its own output, and change detection never settles on a window that repaints its
own counters — which is what "it stutters from the moment it starts" looks like. Two
mechanisms, because the two cases are genuinely different:

| Case | What happens |
| --- | --- |
| A window **we own** — the overlay, the control window | It asks Windows to keep it out of capture (`WDA_EXCLUDEFROMCAPTURE`). The overlay has always done this; the control window now does too, and `selfcheck_desktop` asserts the flag is really applied (`0x11`), with the setting off as a control (`0x0`). |
| A window **we do not own** — a browser showing the panel | There is nothing to ask: the window belongs to another process. So the engine *detects* it, refuses to start, and names the window and how much of the region it covers. Resume is never blocked, just informed. |

Two settings control this, both described in the panel: `capture.exclude_self` (the
exclusion above — its cost is real, since an excluded window is invisible to *your* screen
recordings too, hence the switch) and `capture.hold_if_self_visible` (the detection).
`selfcheck_selfcapture` verifies the geometry, the window filter, the hold and both
switches; it takes the window list as an argument, so it does not depend on what happens
to be on the desktop.

The one thing worth knowing when editing `selfcheck_uirender`: it **must** set
`capture.exclude_self` to false, because a window that is excluded from capture cannot be
photographed. That is not a workaround; it is the same fact from the other side.

### Checks can skip, and a skip is not a pass

`Checker.skip` exists because one class of check cannot run on every desktop:
`selfcheck_uirender` photographs real windows, and a window that another application is
painted over cannot be told apart from a window that renders nothing. It measures how
much of its own window is actually uncovered and, below a threshold, records a **skip**
that is printed in the summary — never a pass, because a check that silently verified
nothing is how a green run starts meaning less than it appears to. On a clear desktop it
runs in full.

### What the UI checks can and cannot see

Structural checks (`selfcheck_desktop`, and the overlay's canvas assertions) prove that
widgets exist, that their text variables are set, and that canvas items sit inside the
canvas. They cannot notice a window that paints nothing, text the same colour as its
plate, or one control covering another. `selfcheck_uirender` covers that gap by
photographing the window and reading it back **with the project's own OCR** — which is
also a fair test of the interface: the engine has to be able to read it.

Two things learned the hard way, both of which cost a wrong conclusion first:

- **Being the foreground window is not the same as being on top.** `GetForegroundWindow`
  returned this project's own window while another application was painted over 70% of
  it. So visibility is *measured* (hide the window, diff the screen) rather than asked
  for.
- **A layered window is not what a screen capture returns.** At the bar preset's 0.72
  alpha the overlay could not be photographed at all; with the window forced opaque its
  text came back at 1.00 OCR confidence. `PrintWindow`, the usual way around occlusion,
  returns solid black for a layered window. This is why the render check makes the
  window opaque before photographing it, and why the overlay's *configured* opacity is
  verified by the presentation checks instead.

### Running this in CI

`.github/workflows/checks.yml` runs the headless list on `ubuntu-latest` and
`windows-latest`, Python 3.12. It is triggered by **any push to any branch**, by a
pull request, or by hand from **Actions → checks → Run workflow**
(`workflow_dispatch`). A workflow only executes where the repository is, so it starts
when the commits are pushed to GitHub, not when they are committed locally — which is
why there is also a local runner:

```bash
prototype\run.cmd check_all              # the same 12 checks CI runs, ~35 s
prototype\run.cmd check_all --display    # plus the 4 that need a display
prototype\run.cmd check_all --only selfcheck_corpus,selfcheck_deps
```

`check_all` reads `checks.txt` — the same file the workflow reads, so the two cannot
disagree — and exits non-zero if anything failed, so it works as a pre-push gate.

What CI itself runs, for reference:

```bash
python -m pip install -r prototype/requirements.txt
while IFS= read -r line; do
  name="$(echo "$line" | sed 's/#.*//' | tr -d '[:space:]')"
  [ -z "$name" ] && continue
  python "prototype/${name}.py" --summary || echo "FAILED: $name"
done < prototype/checks.txt
```

Two checks protect that arrangement, from opposite sides. `selfcheck_deps` asserts the
`pip install` above provides every package the checks import, so "passes locally, fails
on the runner" is caught locally — it found `fastapi` and `uvicorn` missing on its first
run, which would have failed both platforms. `selfcheck_ci` asserts that a new check
cannot be added without being run here, by checking the two list files against the
scripts on disk. `watashi_proto.py --selftest` covers the OCR + corpus + model path end
to end with no screen.

The correction check contains deliberate **mutation tests** worth knowing about:
comment out the `reload_if_changed()` call in `CorpusStore.translate`, the cache
invalidation in `Session._forget_correction`, either of the two revision guards in
`local_nmt.py`, or make the model-skip impossible, and it fails. That was verified,
not assumed — the first version of the check passed while the hot reload it claimed
to test had no caller at all, and an earlier draft of the race test never reached
the model because it used the wrong method name and a line the model is never asked
about.

## Overlay modes

Both presentation modes from the design discussion are implemented.

| Mode | What it is | Click-through |
| --- | --- | --- |
| `bar` | Translucent, always-on-top strip across the bottom of the screen. The "cover the screen" mode: mouse clicks pass straight through it. | yes |
| `panel` | Draggable bilingual panel listing recent original/translation pairs plus live latency stats and a freeze control. | no (it is interactive) |
| `both` | Both at once. | — |
| `none` | Nothing drawn; useful for measuring latency. | — |

Subtitle bar styles:

- `plate` (default) — semi-transparent dark plate with white text. Robust everywhere.
- `bare` — true per-pixel transparency (`-transparentcolor`) with outlined text. Looks like real burned-in subtitles; slightly more fragile.

Click-through uses Win32 extended styles
(`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`). On non-Windows
platforms the overlay still renders, it just also receives clicks.

---

## Local translation model (M3)

A general NMT model is fluent but wrong about invented terminology. Measured on
NLLB-200-distilled-600M:

| source | raw model | with the corpus as authority |
| --- | --- | --- |
| `he broke through to the void realm` | 他突破了空虚的世界 | 断到虚空境界 |
| `the sword intent of this sect is a myth` | 这一宗派的剑意是个神话 | 对于此宗门的剑意是个神话 |
| `voidsword` (standalone) | 无效的字符 ("invalid character") | 虚空剑 |

So the corpus stays authoritative and the model only supplies grammar. The
mechanism is **term protection**:

```
1. corpus + rules scan                  -> instant result, shown immediately
2. if masking the terms leaves no context, stop and keep the corpus result
3. replace trusted terms with placeholders   "he broke through to the <=0>= <=1>="
4. model translates the remainder
5. verify every placeholder survived; put the corpus terms back
6. on any failure (lost placeholder, repetition loop) keep the corpus result
```

Each step of that was chosen from measurement, not taste:

- **Placeholder scheme.** Survival of 9 protected terms through NLLB int8:
  `≤i≥` **7/9**, `@i@` 4/9, `[i]` 4/9, `#i#` 3/9, CJK brackets / fullwidth `@` /
  guillemets 0/9. `≤i≥` is the default because the model copies it verbatim.
- **Step 2 exists because** a line like `antidragon superspirit voidsword` masks
  to an empty string, and NLLB then answers `其他国家` ("other countries").
- **Step 6 exists because** NLLB occasionally drops a placeholder or emits a
  repetition loop (`子子子子…`). Terminology correctness outranks fluency here,
  and every fallback is reported with its reason rather than hidden.
- **Rule confidence gates protection.** The rule set ends with a `transliterate`
  fallback that "matches" every unknown word at confidence 0.10. Treating that
  as authoritative masked *entire sentences* and left the model nothing to do,
  so protection requires confidence >= 0.4 (`protect_min_confidence`).

### `ocr.max_boxes`, and what it does not do

The obvious reading is wrong, so it is worth stating plainly: **this does not make OCR
faster.** RapidOCR detects and recognises in a single call, so a cap applied afterwards
cannot un-recognise anything. Measured, with interleaved sampling (sequential
comparison was swamped by a 3× drift in the same frame between runs):

| | median | share |
| --- | --- | --- |
| OCR, detection + recognition | 198 ms | 100% |
| OCR, detection only | 43 ms | 22% |
| recognition | 155 ms | 78% |

What the cap does save is the other expensive half. One box costs about 20 ms to
recognise; one sentence costs **71–350 ms** to translate with the local model. On a
dense screen, refusing to translate 40 of 50 boxes saves far more than the recognition
it cannot avoid — and it keeps the overlay and the history readable instead of flooded.

Largest boxes win, because the small ones on a real screen are mostly noise: a
fragment of texture or a UI ornament read as a single character. Dropping them is a
quality improvement as well as a cost one.

The real lever on OCR latency remains the **region size**: pick the smallest area that
still contains the text you want.

### Text reuse, and why it is not just cosmetic

OCR is not deterministic. The same on-screen sentence comes back as `他突破到了虚空境界`,
then `他突破到了虚空境界。`, then with a trailing space — three different cache keys, each
missing the translation cache, each paying the model again for text already translated.
`watashi/recent.py` remembers the last few seconds of translations under a normalised
key (whitespace collapsed, edge punctuation trimmed, Latin case folded) and reuses them.

Three details that make it work rather than misfire:

- **The window slides.** Every reuse refreshes it, so a line that is still on screen is
  still considered valid, however long it has been there.
- **Boxes come from the current frame, not the remembered one**, so a plate still
  follows text that has moved. Suppressing the cost and suppressing the position are
  different things, and only the first is wanted.
- **Short lines are never remembered.** `是`, `OK` and the like collide constantly, and
  a reused collision would be a wrong answer that is very hard to notice.

Counters: `reused_lines`, `dedup_hits`, `dedup_misses`, `dedup_hit_rate`, `dedup_size`.

### Two-tier display

NMT costs ~200-350 ms per sentence on CPU, against a 150 ms frame budget, so it
never runs inline. The subtitle bar shows the corpus result instantly and the
model's version replaces it when ready, both cached by source text. The panel
labels each entry `语料库` (corpus) or `模型` (model).

Model latency, NLLB-200-distilled-600M int8 on CPU:

| Configuration | Per sentence |
| --- | --- |
| one at a time, 4 threads | 240 ms |
| one at a time, 2 threads | 266 ms |
| **batched (6 at once)** | **73-86 ms (2.8-3.4x faster)** |
| model load | ~1.4 s (one time) |
| beam 4 instead of 1 | slower *and* measured worse output |

Batching is the biggest single win, which matters if the pipeline is ever
changed to refine a whole frame's lines at once instead of one at a time.

Reproduce with `prototype\run.cmd bench_nmt`.

### The model competes with OCR

Both saturate memory bandwidth, and running them together costs real latency:

| | OCR latency |
| --- | --- |
| model idle | 73 ms |
| model translating concurrently | 266 ms (**3.76x**) |

Thread tuning does **not** fix this — `OMP_WAIT_POLICY=PASSIVE` was worse
(352 ms), 2 threads 245 ms, 1 thread 283 ms, 4 threads 266 ms. It is bandwidth
bound, so the fix is scheduling: `HybridTranslator` waits for a quiet period
while OCR is busy (`_wait_for_quiet_period`), bounded at 4 s so a
constantly-changing screen still gets refinements. Subtitles hold still for
seconds, so waiting is nearly free.

Reproduce all of it with `prototype\run.cmd bench_nmt --contention`.

---

## Measured results

Machine: 20 logical CPUs, Windows, `onnxruntime` 1.30.0, RapidOCR PP-OCRv4 models on CPU.

### Live run against the real screen

Region 1200x200 at 10 FPS target, 8 second run:

| Metric | Value |
| --- | --- |
| OCR | 92.9 ms |
| Translation (corpus + rules) | 0.2 ms |
| **Total per frame** | **93.1 ms** |
| Frames OCR'd | 5 |
| Frames skipped as unchanged | 76 |
| Errors | 0 |

94% of captured frames were skipped by change detection, which is the whole
point: a "continuously monitoring" overlay must not OCR an unchanging screen.

### OCR latency by region width

Short subtitle lines, native resolution:

| Region width | Median OCR | Est. FPS | R4 budget (150 ms) |
| --- | --- | --- | --- |
| 640 px | 89 ms | 11.2 | PASS |
| 960 px | 104 ms | 9.7 | PASS |
| 1280 px | 117 ms | 8.6 | PASS |

Reproduce with `prototype\run.cmd bench_ocr`.

### Three settings that cost ~50x, and how they were found

RapidOCR's stock configuration is tuned for full-page document scans and is
actively harmful for a wide, short subtitle strip. Out of the box a 1280x176
strip took **3577 ms per frame** (~0.3 FPS). Three fixable causes:

| # | Problem | Effect | Fix |
| --- | --- | --- | --- |
| 1 | `Det.limit_type` defaults to `"min"` with `limit_side_len: 736`. It scales the image until the **short** side reaches 736, turning a 1280x176 strip into **5344x736** — a 17x pixel blowup. | ~2300 ms of det | Force `limit_type: "max"`, which only ever downscales. |
| 2 | `enable_cpu_mem_arena` is set to `False`, so every inference reallocates its buffers instead of reusing a pool. | allocation churn | Enable the arena. |
| 3 | `intra_op_num_threads` is left at the ORT default (all cores). On 20 cores that is ~20 threads thrashing over a small model — **slower than 4**. | ~5x | Pin to 4. Measured: 1 -> 133 ms, 2 -> 80 ms, 3 -> 61 ms, **4 -> 168 ms**, 6 -> 261 ms, 8 -> 384 ms. |

Result: 3577 ms -> 89 ms.

Also worth knowing:

- **Disabling the direction classifier (`use_cls`)** is a free win; subtitles are horizontal.
- **The first inference at a new input shape is much slower** (~4.4 s vs 3.5 s) because ONNX Runtime pays kernel selection per shape. The engine warms up on load, and `--selftest` reports first-call and steady-state separately.
- **ONNX Runtime itself is not the bottleneck.** Raw det inference on a 640x640 tensor is 29 ms; run `prototype\run.cmd diagnose_ort` to confirm this on any machine.
- **Downscaling before OCR is a trap.** It does not reliably save time and it destroys accuracy: at `max_width: 640` the detector merged words (`the sword intent` became `theswordintent`), and corpus lookup then failed. Native resolution plus a tighter region is the correct lever.
- **Long lines are the expensive case** in the recognition stage (~75-100 ms per text box), so a frame with one very wide subtitle line costs more than several short ones.

---

## Configuration

See `config.yaml`. Every path is resolved relative to that file, so the
prototype can be launched from anywhere. Notable knobs:

| Key | Meaning |
| --- | --- |
| `capture.region` | `"x,y,w,h"`, or `null` for an automatic bottom strip |
| `capture.fps` | Target capture rate |
| `capture.diff_threshold` | How much a frame must change before OCR runs. Raise if the overlay redraws noise; lower if brief subtitles are missed. |
| `capture.max_width` | `0` = native. Downscaling is a documented trap; leave at 0. |
| `capture.settle_ms` | Hold OCR back until the frame has been still this long. `0` recognises the changing frame, which captures moving glyphs. 150–250 for anything animated. |
| `ocr.intra_op_threads` | 4 is the measured optimum on a 20 core machine |
| `ocr.det_limit_type` | Keep `max` |
| `ocr.max_boxes` | Translate and draw only the largest N boxes; `0` = no cap. Saves the model, **not** OCR — see below. |
| `overlay.mode` | `bar` / `panel` / `both` / `none` |
| `translation.nmt_model` | Path to the local CTranslate2 model dir, or `null` for corpus + rules only |
| `translation.nmt_intra_threads` | 4 measured fastest; more threads made it slower |
| `translation.nmt_beam_size` | Keep 1; beam 4 was slower and gave worse output here |
| `translation.protect_terms` | Keep `true` — this is what keeps terminology consistent |
| `translation.protect_min_confidence` | 0.4. Rule confidence below this is not treated as authoritative, which stops the `transliterate` fallback from masking whole sentences. |

---

## Corpora and rules

Both are plain JSON, loaded at startup and **hot reloaded** when their mtime
changes, so you can add vocabulary while the overlay is running and see the
next subtitle pick it up.

That reload is checked from the translate path (`reload_if_changed`), throttled to
one mtime sweep per `corpus.reload_interval_ms` (500 ms by default), because the
moment to notice that the vocabulary changed is the moment it is about to be used.
Turn it off with `corpus.auto_reload` if the corpus lives on a slow network share;
`reload_corpus` still forces one, and so does a correction.

### Corpus (R2)

Any `.json` file under the configured directories. Layered lookup, highest
priority first: `user` > `domain` > `general`. Longest match wins within a
layer, so `"gg wp"` beats `"gg"`, and a match is never made inside a longer word
(`gg` does not fire inside `eggs`).

```json
{
  "sword intent": "剑意",
  "void": "虚空",
  "heavenly dao": { "target": "天道", "pos": "noun", "domain": "cultivation", "priority": 10 }
}
```

Both the short string form and the richer object form are accepted, as is the
`{"entries": {...}}` wrapper.

#### Every entry has a target language, and it is not optional in practice

A corpus is **not** language-neutral. An English→Chinese vocabulary asked for
Japanese used to answer *with Chinese, at full confidence* — the same failure the
language gate catches when source == target, and harder to notice, because the text
is foreign either way and nothing looks wrong. The engine cannot infer the language
of a translation it is handed; only whoever wrote the entry knows.

So an entry says. Either once per file:

```json
{
  "lang": "zh-CN",
  "entries": {
    "sword intent": "剑意"
  }
}
```

or per entry, which wins over the file and is what makes a mixed file possible:

```json
{
  "sword intent": { "target": "剣意", "lang": "ja" }
}
```

- A file that declares nothing is treated as **usable for any target**, so every
  corpus written before this existed keeps working unchanged.
- The same source term can appear once per language. Both are loaded; each answers
  only for its own target.
- `lang` is compared as a *language*, not as a string: `zh`, `zh-CN`, `zh-TW`,
  `zho_Hans` and `zh_CN` all reach an entry tagged `zh-CN`. Script is deliberately
  ignored — refusing Simplified data for a Traditional target would be a judgement
  call the entry author has not made, and some Chinese beats none.
- An unknown tag matches only itself, so it cannot leak into every target.
- The shipped corpora declare `"lang": "zh-CN"`. Asking for Japanese therefore gets
  *nothing* from them (the line is reported as untranslated, and the model answers),
  instead of Chinese presented as Japanese. Add a second file with `"lang": "ja"`
  for the same terms and both languages work.
- Which languages the loadable corpus can answer for is published in the `ready`
  event as `corpus_languages`, shown in the desktop 翻译 tab and the web panel, and
  returned by `/api/corpus` — because "nothing is being translated" is almost always
  "the vocabulary is for another language", and a user staring at untranslated
  subtitles has no other way to find that out.

A file-level `lang` needs the `{"entries": {...}}` form, where the top level is
metadata. In the bare form a key *is* an entry, so a bare `"lang"` would become a
term named `lang`; the engine says so out loud rather than guessing which was meant.

One more shape, used by the editor: a value may be a **list** of entries, which is how a
single file answers the same term in two languages.

```json
{
  "entries": {
    "gate": [
      { "target": "门", "lang": "zh-CN" },
      { "target": "ゲート", "lang": "ja" }
    ]
  }
}
```

It has to be a list rather than the same key twice, because a JSON object cannot hold two
identical keys: whichever parser reads it silently keeps one of them, so the file would
look correct and be missing half its content.

### Editing the corpus in the UI

The web panel's 语料库 tab is the editor. Three things a user does there, and each maps to
exactly one operation on disk:

| The user | What happens | What is written |
| --- | --- | --- |
| Changes a translation | the entry is added or updated in the user layer | `library.json` |
| Edits a **shipped** entry | an override with the same source, which wins by layer | `library.json`; the shipped file is untouched |
| Turns a shipped entry off | the source goes on the suppression list | `library.json`: `_suppress` |
| Reverts either | the user-layer row or the suppression is removed | the shipped entry is what you see again |

The shipped corpora are demo data tracked by git. **Nothing in this application writes to
them** — an edit becomes an override in the user layer, and `selfcheck_library` asserts
that byte for byte by reading the file before and after an edit through the same code path
the UI uses. A user's own hand-written corpus file in the same directory is untouched for
the same reason: this application writes exactly one file, and only one.

Every edit takes effect immediately: the file is written, the corpus is force-reloaded
(the mtime throttle is for changes noticed in passing, not for the one the user is
watching for), and a `library` event tells the other surfaces to repaint their tables.

**Import and export** are text in, text out, so the browser does the file handling and the
panel needs no access to the machine it is running on — which matters, because this panel
can be reached over the LAN.

| | Formats | Notes |
| --- | --- | --- |
| Import | JSON, CSV, TSV | JSON accepts the corpus's own shapes, so any file that works as a corpus imports as one. CSV/TSV are header-driven (`source,target,lang,pos,domain,note`, Chinese headers understood); with no header they are read as two columns, `原文,译文` |
| Import (other) | anything a plugin handles | the `corpus_loader` extension point, reserved in the plugin API from the first version and wired now that there is a reason to |
| Export | JSON, CSV, TSV × `user` or `effective` | CSV/TSV downloads are written `utf-8-sig`, so a spreadsheet opens Chinese text instead of mojibake |
| | | `user` is what you wrote; `effective` is everything the engine will actually use, as a corpus file you could drop into another install |

A round trip is asserted, not assumed: `selfcheck_library` exports each format and imports
it back, and checks the entries and their translations survive.

Layer directories (`config.yaml`):

| Layer | Paths |
| --- | --- |
| user | `../plugins/user/custom_rules` |
| domain | `corpus/` (demo data), `../rules/slang`, `../rules/fiction` |
| general | `../rules/dictionaries` |

`corpus/demo_terms.json` is 84 entries of demo vocabulary so the prototype
visibly translates something. It is not shipped data — replace it.

### Real time correction

A wrong answer is not a gap, it is a confident hit: when the corpus itself is
wrong — a name romanised the wrong way, a skill mistranslated — more vocabulary
does not help, because the wrong translation is already winning. Something has to
outrank an authoritative wrong answer, and only a human can.

So: the translation is on screen, you correct it, and it sticks. In the desktop
window, click the line in 字幕, edit 译文, press 保存纠正. Or through the boundary:

```
{"type": "command", "data": {"cmd": "correct",
  "source": "他突破到了虚空境界", "target": "He has broken through into the Void Realm",
  "scope": "line"}}
```

Four things happen, and each of them is a way the feature can appear to work and
not work:

1. **Written to a file.** `corrections.json` in the user corpus layer — the
   highest-priority layer, created on first use because a fresh checkout has no
   such directory (git cannot store an empty one). Written atomically, whole, and
   never merged into a corpus file you maintain by hand.
2. **Loaded immediately,** forcing the reload rather than waiting out the throttle.
   You are looking at the screen to see whether your fix took.
3. **The reuse memory is cleared** for that text. Otherwise the rejected
   translation is served from a ten-second cache and the correction is invisible
   for exactly as long as you are watching it.
4. **The frame on screen is repainted.** A still screen produces no new frame at
   all, so without this a correction made on a paused or static screen would sit in
   the corpus and never be shown.
5. **Anything already in flight loses.** The model is often already refining the
   very line being corrected — that is what the two-tier display does — and its
   answer comes back a few hundred milliseconds later. Both that in-flight answer
   and any *cached* refinement for the line are answers to the question the human
   just answered differently, so they are discarded rather than published. Without
   this, whether a correction stuck would depend on which thread finished first.

The mechanism for 5 is a **vocabulary revision**: `CorpusStore.load()` bumps a
counter, a refinement is queued with the revision it was computed under, and it is
dropped if that counter moved. The same check invalidates the refinement cache, so
a corpus file you edit by hand mid-run also takes effect instead of being hidden by
the model's older answer for the same line. Dropped refinements are counted
(`refinements_stale`) rather than discarded silently — a correction that keeps being
thrown away would otherwise look exactly like one that does not work.

Two scopes, and the difference matters:

| Scope | Keyed on | Use it for |
| --- | --- | --- |
| `line` (default) | The whole sentence, matched **loosely** — whitespace and edge punctuation ignored | One sentence that came out wrong. OCR never reads the same string twice, so the loose key is what makes a correction apply to the next frame rather than only to the one it was typed from |
| `term` | The exact term, matched by the ordinary corpus scan wherever it appears | A name that shows up on every screen. It survives the sentence around it being read differently, which the line scope cannot |

A term correction rewrites the word and leaves the sentence alone; a line
correction replaces the whole line and, being fully protected terminology, is not
second-guessed by the model afterwards. Both are listed (read-only) in the web
panel, and `remove_correction` undoes one.

### Rules (R3)

For words the corpus does not contain. Rules are **data, not code**
(`rules/engine_rules.json`), so editing them needs no restart or rebuild.

| Type | Purpose | Example |
| --- | --- | --- |
| `affix` | Strip a known prefix/suffix, translate the stem, re-attach | `antidragon` -> `反龙` |
| `morpheme` | Split an unknown word into known corpus morphemes | `voidsword` -> `虚空剑` |
| `template` | Apply a naming-convention regex | `(.+)境` -> `{base} Realm` |
| `transliterate` | Last-resort fallback, flagged low confidence | `warpdrive` -> `warpdrive` |

Every span records where it came from and how confident it was, and the panel
dims output below 50% coverage. `--print-trace` shows the full provenance:

```
'sword intent'->'剑意'[corpus:demo_terms 0.95]  'antidragon'->'反龙'[affix-en-to-zh 0.55]
```

A rule's `target` is compared as a **language, not a string**: `zh`, `zh-CN`,
`zh_CN` and `zho_Hans` all activate a rule tagged `zh-CN`. Equality there meant
typing `zh` instead of `zh-CN` switched off every Chinese rule in the shipped set,
with no diagnostic — the rule engine just looked broken. A rule's own corpus
lookups respect its language too, so a Chinese rule cannot assemble its answer out
of Japanese entries. A rule that omits `target` applies to every target, which is
what the transliterate fallback does.

A rule with a pattern that cannot compile is disabled and reported, rather than
counted among the loaded rules: a rule that can never fire should not appear in a
number a user reads as "these rules are working".

**R3's write-back loop is implemented** — the requirement that a rule's guess can be
written into the corpus in one step. A rule answers at its declared confidence
(`antidragon` → `反龙` at 0.55); accept it as a correction and the same word is then
answered by the corpus at confidence 1.0, with the same translation:

```
{"cmd": "correct", "source": "antidragon", "target": "反龙", "scope": "term"}
```

`selfcheck_corpus` asserts the whole loop: the confidence rises, the provenance
changes from `rule:affix` to `corpus:`, and the answer itself does not change.

---

## Tools

| Tool | Purpose |
| --- | --- |
| `watashi_proto.py --selftest` | Headless end-to-end check of OCR + corpus + rules + model. No screen needed. |
| `check_all.py` | Runs every check CI runs (from `checks.txt`, the same file the workflow reads) on this machine, as a pre-push gate. `--display` adds the four that need a screen. |
| `selfcheck_session.py` | **Headless verification of the engine boundary**: events, envelope integrity, every command, adapter pass-through. No screen needed. |
| `selfcheck_correct.py` | **Headless verification of corpus hot reload and real time correction**: the file format, loose matching, both scopes, cache invalidation, the repaint, and mutation-tested assertions. No screen needed. |
| `selfcheck_corpus.py` | **Headless verification of the corpus and rule engine** (R2 / R3), which had no check of its own: layering and priority, longest match, entry forms, the target-language dimension, rule language identity, explainability, R3's write-back loop, hot reload and damaged files. No screen needed. |
| `selfcheck_deps.py` | **Headless verification that a clean environment can run the other checks**: walks their import closures and requires every package to be declared in `requirements.txt` or exempted with a reason. Found `fastapi` and `uvicorn` missing, which would have failed the first CI run on both platforms. No screen needed. |
| `selfcheck_library.py` | **Headless verification of the corpus editor**: that editing a shipped entry leaves the shipped file byte for byte unchanged, that a hand-written corpus beside it is untouched, that override/suppress/revert each mean one thing, and that every export format imports back to the same entries. No screen needed. |
| `selfcheck_selfcapture.py` | **Headless verification that the engine does not read its own windows**: rectangle geometry, which windows count as ours, that a window already excluded from capture is not treated as a problem, the hold at startup, and the two switches that turn the guards off. It takes the window list as an argument, so it runs on a machine where nothing of ours is on screen. No screen needed. |
| `selfcheck_uirender.py` | **Does the UI actually draw?** Photographs the real desktop window and the real overlay, and reads them back with the project's own OCR: the translation on screen, the history label, the correction editor's fields and button, the settings schema. Skips (never passes) when another window covers what it needs to photograph. Needs a display. |
| `watashi_proto.py --list-monitors` | Enumerate monitors. |
| `watashi_proto.py --select` | Drag to choose a region. |
| `watashi_proto.py --print` | Echo recognised lines, translations and refinements to the console. |
| `watashi_proto.py --json-lines` | Machine readable event stream (one envelope per line). |
| `watashi_proto.py --synthetic` | Drive the engine with rendered frames instead of the screen. |
| `watashi_proto.py --no-nmt` | Run corpus + rules only, without loading the model. |
| `watashi_proto.py --stats-json FILE` | Write final latency stats as JSON. |
| `fetch_model.py` | One-time, resume-safe model download. `--check` reports state. |
| `bench_ocr.py` | Latency sweep by region width, plus the downscale penalty. |
| `bench_nmt.py` | Model latency, batching, term-protection on/off, OCR contention. |
| `diagnose_ort.py` | Isolate ONNX Runtime cost from pipeline cost; sweep thread counts. |

---

## Known limitations

Be aware of these before judging the prototype.

1. **The model is a 600M distilled model at int8.** It is fluent but not
   accurate: `he broke through to the void realm` becomes `断到虚空境界` — the
   terms are right because the corpus enforced them, but `断到` is a poor
   rendering of "broke through to". Masking terms also removes context, so
   protection costs some fluency. A larger model would help; that is a size and
   latency tradeoff, not a design flaw.
2. **Roughly half of term-dense lines fall back to the corpus.** Deliberately:
   `antidragon superspirit voidsword` masks to nothing, and NLLB answers
   `其他国家`. Fallback reasons are reported, never hidden.
3. **Terminology still depends on corpus size.** The demo corpus is 84 entries.
   Anything absent gets the model's guess, which for invented words is often
   nonsense (`voidsword` -> `无效的字符`, "invalid character"). That is precisely
   why corpora and rules are the project's core, not the model.
4. **Latency drifts a lot between runs on this machine.** The same 1280x304 OCR
   frame measured 157 ms, 170 ms and 218 ms in different sessions; the same self
   test reported a 156.9 ms median, then three consecutive runs around 400-430 ms
   while test suites were running, then 156.9 ms again once the machine was quiet.
   Treat the budget numbers as best-case under a quiet machine, and re-measure with
   `bench_ocr.py` before concluding that a change made anything slower — that
   experiment cost an afternoon of suspicion and turned out to be load.
5. **Latency is region dependent.** ~89-120 ms for realistic short subtitle
   lines, but a large region or a frame with several long lines costs 200-350 ms
   and misses the R4 budget. Region size is the primary control.
6. **Change detection is a whole-frame mean.** It cannot notice a small text
   change in one corner of a large region; subtle changes may be missed.
   `diff_threshold` is the tradeoff knob.
7. **The two-tier display means the visible text changes twice** for a new line:
   corpus result, then the refined version ~0.2-0.7 s later. That is the price
   of keeping OCR inside the latency budget.
8. **Source language detection is a heuristic** (CJK ratio, else Latin). It
   handles the en/zh case this project targets and nothing more.
9. **Click-through is Windows only** (Win32 extended styles).
10. **`bare` subtitle style** depends on `-transparentcolor` and can show
    fringing around glyph edges on some setups; `plate` is the safe default.
11. **Only one capture source at a time**, and it is either a window or a
    rectangle, never both. In-place layout (`--presentation inplace`, and the
    呈现 tab's `inplace` button) *is* implemented and draws over each recognised
    line's own position; what is still missing is per-block opacity, so an
    in-place translation cannot be faded the way a whole window can.
12. **The desktop UI is one process and one window.** It needs a display, so it
    cannot be verified in CI the way the engine can — `selfcheck_desktop` drives
    its real widgets, but that still requires a screen. It also has no
    per-element style editor: appearance is edited as files or via the web panel,
    and the desktop window picks from presets.
13. **Window enumeration excludes our own windows** and offers only the primary
    monitor to the drag-to-select path; a region on a second monitor needs
    `--region`. UWP apps and overlay hosts can appear twice in the picker, which
    is why each row shows its size and process.
14. **A correction matches by text, so it only fires when OCR reads the same
    words.** A line scope tolerates the whitespace and punctuation OCR varies
    between frames, and nothing more: one mis-recognised character and neither the
    loose line key nor the exact corpus key hits, and the line goes back to the
    translation you rejected. The fix is to correct the *term* instead, which is
    why the scope exists. The web panel's 本次命中 column is the honest answer to
    "is my correction doing anything" — a stored row that never matches looks
    exactly like a working one in the file.
15. **A correction does not re-read the screen.** It repaints the frame it already
    has, so if the text has scrolled away the corrected line still applies to the
    next frame that contains it, not to what is in front of you now.
16. **A correction is not re-checked by the model.** A whole-line correction is
    protected as one term, so the refinement path declines it and the line stays at
    corpus quality until the next frame brings a fresh refinement of the sentence
    around it. That is the intended trade: the human's answer outranks fluency.
17. **A corpus must declare its language to be usable for a non-Chinese target.**
    Untagged entries are used for every target (that is what keeps every existing
    corpus file working), so leaving them untagged means a Japanese target will be
    answered with Chinese again. The shipped demo data is tagged `zh-CN` for exactly
    that reason; a Japanese corpus is a file the user writes, and
    `"lang": "ja"` is the whole of the syntax.
18. **Japanese written only in kanji still reads as Chinese** (`lang.py`'s script
    heuristic), because the scripts are genuinely identical. Kana anywhere in the
    line settles it; a kanji-only title does not. Declaring the source language with
    `--source ja` is the workaround.

---

## Files

```
prototype/
├── watashi_proto.py      CLI entry point (thin client of Session)
├── selfcheck_session.py  headless engine boundary verification
├── selfcheck_correct.py  headless corpus hot reload + correction verification
├── selfcheck_corpus.py   headless corpus/rule engine verification (R2 / R3)
├── selfcheck_library.py  headless corpus editor verification (override, import, export)
├── selfcheck_selfcapture.py  headless verification that we do not read our own windows
├── selfcheck_deps.py     headless check that a clean install can run the others
├── selfcheck_uirender.py photographs the real windows and reads them with OCR
├── check_all.py          runs every check CI runs, locally (pre-push gate)
├── config.yaml           configuration (paths relative to this file)
├── requirements.txt      local-only dependencies
├── fetch_model.py        one-time, resume-safe model download
├── bench_ocr.py          OCR latency benchmark
├── bench_nmt.py          model benchmark: batching, term protection, contention
├── diagnose_ort.py       ONNX Runtime isolation / thread sweep
├── corpus/
│   └── demo_terms.json   demo corpus, 84 entries (replace it)
├── rules/
│   └── engine_rules.json rule definitions (R3)
└── watashi/
    ├── events.py         THE BOUNDARY: event/command schema, no UI imports
    ├── session.py        engine facade: events out, commands in
    ├── correct.py        real time correction: the user-corpus file, atomic writes
    ├── library.py        the corpus editor: entries, suppression, import/export
    ├── adapters.py       OverlayAdapter, ConsoleAdapter (in-process surfaces)
    ├── capture.py        mss region capture, window capture + change detection
    ├── ocr.py            RapidOCR wrapper, with the measured tuning
    ├── translate.py      corpus + rule engine, explainable spans, target languages
    ├── local_nmt.py      CTranslate2 model, term protection, hybrid translator
    ├── overlay.py        subtitle bar + bilingual panel (tkinter)
    ├── desktop.py        desktop control window (tkinter, shares the Tk root)
    ├── selector.py       drag-to-select, with logical->physical conversion
    ├── winutil.py        window enumeration and client-area geometry
    ├── hotkeys.py        global hotkeys (RegisterHotKey)
    ├── plugins.py        extension points, discovery, failure isolation
    ├── presentation.py   declarative spec, presets, pure layout maths
    ├── pipeline.py       threaded capture/OCR pipeline, latest-frame backpressure
    ├── lang.py           language detection and the target-language gate
    ├── profiles.py       memory tiers (lean / balanced / full)
    ├── web.py            in-process settings panel (FastAPI + SSE)
    ├── synth.py          synthetic frames + capturer (headless testing)
    ├── memory.py         resident memory reporting (no psutil)
    ├── config.py         config loading
    └── selftest.py       headless OCR/corpus/model verification
```
