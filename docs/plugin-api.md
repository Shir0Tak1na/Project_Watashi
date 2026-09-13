# Plugin API

Two surfaces, deliberately different in status:

| Surface | Status |
| --- | --- |
| **Python extension points** | **Working.** Implemented in `prototype/watashi/plugins.py`, with two shipped examples and a self check. |
| **C++ `IPlugin`** | Draft. Nothing in `src/` implements it yet; it is what the port should target. |

## The trust caveat, first

**Plugins run in this process.** Python cannot be sandboxed, so a plugin can do
anything the application can: read files, open sockets, spin forever, crash the
interpreter. That is not a gap waiting to be closed with more code — it is what
running user code in-process means. The model is therefore **trust based**:
plugins are for the machine's owner, not for distribution to strangers.

What the registry does guarantee is narrower and still worth having: a plugin
that raises, returns the wrong type, declares an incompatible API version, or
names an unknown extension point is **reported and skipped**, never fatal and
never silently swallowed.

## Python extension points

Version: `PLUGIN_API_VERSION = 1`. A plugin declaring a different value is refused
with an explanation rather than called with the wrong arguments.

| Point | Contract | Called by the engine today |
| --- | --- | --- |
| `postprocess` | `(text, context) -> str \| None` | **yes** — chained over each translated line |
| `export` | `(payload, options) -> str` | **yes** — via the `export` command and `--export` |
| `corpus_loader` | `(path) -> dict` | reserved |
| `renderer` | see [presentation-spec.md](presentation-spec.md) | reserved |
| `translator` | a `translate.Translator` subclass | reserved |

Reserved points are listed by `--list-plugins` so an author can see what is not
yet connected, instead of having a plugin silently ignored.

### Writing one

A plugin is any `.py` file under `plugins/builtin/` or `plugins/user/` (or the
directories in `config.yaml`). Files starting with `_` are treated as private
helpers, not plugins.

```python
PLUGIN = {
    "name": "my-plugin",
    "version": "0.1",
    "api": 1,
    "description": "one line",
    "points": ["postprocess"],
}


class MyPostprocessor:
    def postprocess(self, text, context):
        # context carries target_lang, source_lang, backend, refined
        return text.replace("colour", "color")


def register():
    return {"postprocess": MyPostprocessor()}
```

`register()` returns a dict mapping extension point names to implementations. A
value may also be a **list**, so one module can provide several implementations of
one point — the shipped export plugin provides both `srt` and `glossary` that way,
distinguished by a `format` attribute.

`PLUGIN` is optional but recommended: without it the filename becomes the name,
and a typo in `points` cannot be caught up front.

### postprocess

Runs over each translated line, in discovery order, after the corpus and the
model have had their turn. Returning `None` means "no change". Returning a
non-string, or raising, drops that plugin from the chain for that call and records
the failure.

Chained rather than first-wins, because a terminology plugin and a formatting
plugin both want a turn. Per line rather than over the joined frame, so the
per-line array and the aggregate text can never disagree.

### export

`payload` is `{"entries": [{"start", "end", "source", "target"}, ...]}`. Which
implementation runs is chosen by the `format` attribute:

```python
class SrtExporter:
    format = "srt"

    def export(self, payload, options):
        ...
```

`session.export(fmt, path, options)` returns the text; `--export <fmt>` writes it
on exit, and the desktop window's 插件 and top-bar 导出 buttons drive the same
command with a file chooser. `--list-plugins` reports the available formats, and
the formats shown in the desktop window are read from the registry rather than
hardcoded — an export plugin that is added or fails changes that list.

## What a plugin cannot do yet

Worth stating plainly, because the seams exist but are not wired:

- **Corpus loaders.** Corpora are still only JSON. The point is reserved.
- **Renderers.** Custom layouts go through the declarative presentation spec; a
  code renderer for something the spec cannot express is not implemented.
- **Translator backends.** `Translator` is a real protocol and `HybridTranslator`
  implements it, but the registry does not yet select a backend from a plugin.
- **Lifecycle hooks.** No `initialize`/`shutdown`; a module is imported and its
  `register()` called, nothing more.

## The C++ draft

What the port should target, roughly mirroring the Python points:

```cpp
namespace projectwatashi {

class IPlugin {
public:
    virtual ~IPlugin() = default;
    virtual std::string name() const = 0;
    virtual int api_version() const = 0;
    virtual bool initialize() = 0;
    virtual std::string process(const std::string& text,
                               const std::string& src_lang,
                               const std::string& dst_lang) = 0;
    virtual void shutdown() = 0;
};

} // namespace projectwatashi
```

`include/plugins/plugin_api.h` currently declares a version of this without
`api_version()`. Nothing implements it, and the C++ tree is still all
placeholders — see the status table in the repository README.

This allows custom local translation logic for:

- slang
- sci-fi terms
- fantasy words
- game-specific vocabulary
- personalised user terms

## Verifying

```bash
prototype\run.cmd selfcheck_plugins --summary   # no screen, no models
prototype\run.cmd --list-plugins
```

The self check is mostly about failure handling: a module that raises on import, a
wrong API version, a non-dict `register()`, an unknown point, a missing
`register()`, a postprocessor that raises, one that returns an `int`. Each must be
reported and skipped while a good plugin alongside seven bad ones still loads.
