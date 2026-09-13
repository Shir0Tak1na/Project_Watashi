# Presentation spec

How customization is expressed, so that **one description drives every surface**.

## Why a declarative spec

Customization was requested across four axes, all of which must apply to the
floating overlay, the desktop window and the web panel alike:

| Axis | Example |
| --- | --- |
| **A. Appearance** | font, size, colour, opacity, position, theme |
| **B. Layout** | translation only / bilingual / multi-line / vertical / **in-place over the original text** |
| **C. Behaviour** | user code extending translation, corpora, export |
| **D. Profiles** | a named bundle of corpus + rules + presentation per work or project |

A code-based renderer interface could cover B, but it has two problems: it loads
user code into the engine process, and Python cannot be sandboxed. The security
section of the project requirements asks for least privilege and for one bad
plugin not to take down the host, and neither is achievable that way.

A **declarative spec** solves both, and adds a third benefit that matters more:
one spec is honoured by the tkinter overlay, the desktop window's preset buttons,
the web panel and any future surface, so customization is described once instead
of reimplemented per UI. Axis C is handled separately, through the plugin
extension points in `docs/plugin-api.md`, which carry no renderer code.

## Required schema change (prerequisite)

In-place and per-line layout are impossible without text geometry, and the
geometry is currently computed and then thrown away: `OcrLine.box` exists but
never leaves `ocr.py`. So subtitle events gain a `lines` array:

```json
{
  "source": "sword intent",
  "target": "剑意",
  "coverage": 1.0,
  "confidence": 0.95,
  "latency_ms": 88.5,
  "backend": "corpus+rules",
  "refined": false,
  "lines": [
    {
      "source": "the sword intent of this sect is a myth",
      "target": "剑意",
      "box": [24, 88, 640, 40],
      "confidence": 0.95,
      "coverage": 1.0
    }
  ]
}
```

`box` is `[x, y, width, height]` in **region-relative** coordinates. A renderer
maps it to the screen by adding the region origin, which clients already receive
in the `ready` event (`region`).

This has to land **now**, while the only consumers are surfaces written in this
repository. Adding it after Web, LAN and Android clients exist would be a
breaking change for all of them.

## The spec

```json
{
  "version": 1,
  "name": "subtitle-bar",
  "layout": {
    "mode": "bar",
    "anchor": "bottom-center",
    "offset": [0, -90],
    "max_width_ratio": 0.9,
    "line_gap": 6,
    "align": "center"
  },
  "elements": [
    {
      "role": "source",
      "order": 0,
      "font": { "family": "Microsoft YaHei UI", "size": 16, "bold": false },
      "color": "#c9d6e0",
      "opacity": 0.85
    },
    {
      "role": "target",
      "order": 1,
      "font": { "family": "Microsoft YaHei UI", "size": 24, "bold": true },
      "color": "#ffffff",
      "outline": { "color": "#000000", "width": 2 }
    }
  ],
  "background": { "kind": "plate", "color": "#000000", "opacity": 0.72, "radius": 8, "padding": [12, 8] },
  "confidence": { "dim_below": 0.5, "dim_opacity": 0.6 },
  "per_line": false
}
```

### layout.mode

| Mode | Meaning | Needs geometry |
| --- | --- | --- |
| `bar` | One block for the whole frame, anchored to an edge. Current default. | no |
| `lines` | One block per recognised line, stacked in reading order at the anchor. | no |
| `inplace` | One block per line, positioned at that line's original screen location. | **yes** |
| `panel` | Scrolling bilingual history with metadata. Debug / post-editing. | no |
| `hidden` | Recognise and translate, draw nothing (used for measurement and by the web panel). | no |

### elements

`role` selects which text is drawn: `source` (original), `target`
(translation), `trace` (provenance, for debugging), `previous` (the line before this
one, which is what keeps the thread of a conversation when a subtitle replaces
itself). `order` stacks them within a block.

`previous` is opt-in rather than present everywhere, and the presets that skip it do so
for a reason: `inplace` still has the old text on screen under its plate, `panel`
already is a history, `lines` already stacks one block per recognised line, and
`minimal` exists to show the translation and nothing else. `bar` and `bare` carry it,
dim and small, so it reads as context rather than competing with the current line.

### Presets

Named specs shipped with the app, overridable by a user file:

| Preset | Shape |
| --- | --- |
| `bar` | Current subtitle bar: source above, translation large below. |
| `bare` | Same, but transparent background with outlined text. |
| `inplace` | Translation drawn over the original text location. |
| `lines` | Per-line bilingual blocks stacked at the bottom. |
| `panel` | Bilingual history with latency and coverage metadata. |
| `minimal` | Translation only, no source line. |

## Live application

Appearance is read once in `Overlay.__init__`, so a change from any surface is
delivered through the same command channel as everything else:

```json
{ "cmd": "set_presentation", "spec": { ... } }
```

and the active spec is broadcast back as a `presentation` event so every surface
stays in sync — a preset clicked in the desktop window's 呈现 tab, or a look
edited in the web panel, updates the overlay immediately.

This is verified two ways: `selfcheck_overlay` switches every preset live and
asserts that the drawn blocks actually changed (not just the stored state), and
`selfcheck_desktop` asserts the tab's buttons issue `set_presentation` and that a
`presentation` event repaints the tab's own summary line.

## Plugins (axis C)

**Implemented** in `prototype/watashi/plugins.py`; see `docs/plugin-api.md` for the
contracts, the version rule and the failure handling.

| Point | Contract | State |
| --- | --- | --- |
| `postprocess` | Rewrites a finished translation (style, terminology cleanup) | connected |
| `export` | Subtitle / glossary / side-by-side export formats | connected |
| `translator` | An alternative `Translator` backend | reserved |
| `corpus_loader` | Reads a corpus format other than the built-in JSON | reserved |
| `renderer` | A code renderer, for layouts the declarative spec cannot express | reserved |

Code plugins run **in process** and are therefore trust-based: they are for the
machine's owner, not for untrusted third-party distribution. The declarative
spec covers the customization most users want without that caveat.

## Open questions

- Should `inplace` hide the original text, or draw beside it? (Hiding requires
  painting an opaque block over the source, which is only correct if the original
  is re-rendered or masked. Today it draws over the line's own position without
  masking, and per-block opacity is not available — tkinter can only set opacity
  per window, not per canvas item.)
- Profiles: a single file per profile, or a directory (`profiles/<name>/`)?
