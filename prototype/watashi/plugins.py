"""Plugin registry: user code extending the engine (customization axis C).

Skeleton for the extension points that were previously only *possible* rather
than *registered*. Each one is a small duck-typed contract, versioned, discovered
from disk, and isolated so one failure cannot take down the host.

The honest caveat, stated up front because it shapes everything here
--------------------------------------------------------------------
**Plugins run in this process.** Python cannot be sandboxed, so a plugin can do
anything the application can: read files, open sockets, crash the interpreter.
That is not a gap to be closed later with more code -- it is a property of
running user code in-process. So the model is *trust based*: plugins are for the
machine's owner, not for distributing to strangers. What this module can and does
guarantee is the weaker but still useful promise that a plugin which raises,
returns nonsense, or declares an incompatible API version is reported and
skipped rather than silently corrupting output.

Contract
--------
A plugin is a Python module anywhere under the configured plugin directories that
defines::

    PLUGIN = {
        "name": "terminology",
        "version": "0.1",
        "api": 1,                  # must equal PLUGIN_API_VERSION
        "description": "one line",
        "points": ["postprocess"], # which extension points it provides
    }

    def register() -> dict[str, object]:
        return {"postprocess": TerminologyPostprocessor()}

``PLUGIN`` is optional but recommended: without it the module still loads and a
name is derived from the filename, while ``points`` lets a typo be caught up
front instead of at first use.

Extension points
----------------
``postprocess``    ``(text, context) -> str | None``   rewrite a finished translation
``corpus_loader``  ``(path) -> dict``                  read a corpus format other than JSON
``export``         ``(payload, options) -> str``       emit a new output format
``renderer``       see docs/presentation-spec.md        layouts the spec cannot express
``translator``     subclass ``translate.Translator``    an alternative backend

Only ``postprocess`` and ``export`` are wired into the running engine today;
``corpus_loader``, ``renderer`` and ``translator`` are reserved and reported by
``--list-plugins`` so an author can see what is not yet connected rather than
discovering it by having their plugin ignored.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: Bump when a contract changes shape. A plugin declaring an older or newer value
#: is refused with an explanation instead of being called with the wrong arguments.
PLUGIN_API_VERSION = 1

#: Extension point name -> (contract signature, whether the engine calls it today)
EXTENSION_POINTS: dict[str, tuple[str, bool]] = {
    "postprocess": ("(text, context) -> str | None", True),
    "corpus_loader": ("(path) -> dict", False),
    "export": ("(payload, options) -> str", True),
    "renderer": ("see docs/presentation-spec.md", False),
    "translator": ("a translate.Translator subclass", False),
}

#: Directory names that never contain plugin sources.
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}


@dataclass
class PluginInfo:
    """What was found, and what happened when it was loaded."""

    name: str
    path: Path
    version: str = ""
    api_version: int = 0
    description: str = ""
    declared_points: list[str] = field(default_factory=list)
    provided_points: list[str] = field(default_factory=list)
    loaded: bool = False
    error: str = ""
    #: point name -> every object registered for it. A single module may provide
    #: several implementations of one point, e.g. two export formats.
    implementations: dict[str, list[Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.loaded and not self.error

    def label(self) -> str:
        if self.error:
            return f"{self.name}  [{self.error}]"
        if not self.provided_points:
            return f"{self.name}  (no known extension point)"
        return f"{self.name}  -> {', '.join(self.provided_points)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "version": self.version,
            "api_version": self.api_version,
            "description": self.description,
            "declared_points": list(self.declared_points),
            "provided_points": list(self.provided_points),
            "loaded": self.loaded,
            "error": self.error,
        }


class PluginRegistry:
    """Discovers, loads and isolates plugins from a set of directories."""

    def __init__(self, api_version: int = PLUGIN_API_VERSION) -> None:
        self.api_version = api_version
        self.plugins: list[PluginInfo] = []
        self._by_point: dict[str, list[tuple[PluginInfo, Any]]] = {}
        #: calls that raised, so a broken plugin is visible rather than merely quiet
        self.failures: dict[str, int] = {}

    # -- discovery --------------------------------------------------------- #

    def discover(self, directories: Iterable[Path]) -> list[Path]:
        """Find candidate plugin modules. Nothing is imported yet."""
        found: list[Path] = []
        seen: set[Path] = set()
        for directory in directories:
            directory = Path(directory)
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.py")):
                if any(part in _SKIP_DIRS for part in path.parts):
                    continue
                # a leading underscore marks a private helper, not a plugin
                if path.name.startswith("_"):
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append(path)
        return found

    def load_all(self, directories: Iterable[Path]) -> list[PluginInfo]:
        for path in self.discover(directories):
            self.plugins.append(self.load(path))
        return self.plugins

    # -- loading ----------------------------------------------------------- #

    def load(self, path: Path) -> PluginInfo:
        """Import one module and register what it provides.

        Every failure mode is reported on the returned ``PluginInfo`` rather than
        raised, because a bad plugin must not stop the application from starting.
        """
        info = PluginInfo(name=path.stem, path=path)
        info.implementations = {}
        try:
            module = self._import(path)
        except Exception as exc:
            info.error = f"import failed: {type(exc).__name__}: {exc}"
            return info

        manifest = getattr(module, "PLUGIN", None)
        if isinstance(manifest, dict):
            info.name = str(manifest.get("name") or info.name)
            info.version = str(manifest.get("version") or "")
            info.description = str(manifest.get("description") or "")
            declared = manifest.get("points") or []
            info.declared_points = [str(p) for p in declared] if isinstance(declared, list) else []
            try:
                info.api_version = int(manifest.get("api", self.api_version))
            except (TypeError, ValueError):
                info.error = f"PLUGIN['api'] is not a number: {manifest.get('api')!r}"
                return info
            if info.api_version != self.api_version:
                info.error = (
                    f"declares plugin API {info.api_version}, this build speaks "
                    f"{self.api_version}; not loading it rather than calling it with "
                    f"the wrong arguments"
                )
                return info

        register = getattr(module, "register", None)
        if not callable(register):
            info.error = "no register() function (see prototype/README.md)"
            return info

        try:
            provided = register()
        except Exception as exc:
            info.error = f"register() raised {type(exc).__name__}: {exc}"
            return info

        if not isinstance(provided, dict):
            # The legacy convention returned a bare object with a translate()
            # method. Say so plainly instead of pretending it loaded.
            info.error = (
                f"register() returned {type(provided).__name__}, expected a dict of "
                f"extension points; this looks like the legacy plugin convention"
            )
            return info

        unknown = [p for p in provided if p not in EXTENSION_POINTS]
        if unknown:
            info.error = (
                f"unknown extension point(s): {', '.join(sorted(unknown))}; "
                f"known: {', '.join(sorted(EXTENSION_POINTS))}"
            )
            return info

        for point, implementation in provided.items():
            if implementation is None:
                continue
            # a module may hand back one object or a list of them
            items = (
                list(implementation)
                if isinstance(implementation, (list, tuple))
                else [implementation]
            )
            for item in items:
                if item is None:
                    continue
                info.implementations.setdefault(point, []).append(item)
                self._by_point.setdefault(point, []).append((info, item))
            if info.implementations.get(point):
                info.provided_points.append(point)

        info.loaded = True
        return info

    @staticmethod
    def _import(path: Path) -> Any:
        """Import a module from an arbitrary path, without polluting sys.modules."""
        module_name = f"watashi_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build an import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        # the plugin may need its own directory on the path for sibling imports
        parent = str(path.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        return module

    # -- use --------------------------------------------------------------- #

    def implementations(self, point: str) -> list[tuple[PluginInfo, Any]]:
        return list(self._by_point.get(point, []))

    def postprocess(self, text: str, context: dict[str, Any] | None = None) -> str:
        """Run every postprocessor in turn, isolating failures.

        Chained rather than first-wins: a terminology plugin and a formatting
        plugin both want a turn. A plugin that raises or returns a non-string is
        skipped with its failure counted, and the text passes through unchanged.
        """
        if not text:
            return text
        context = context or {}
        result = text
        for info, implementation in self.implementations("postprocess"):
            function: Callable[[str, dict[str, Any]], Any] | None = None
            if callable(implementation):
                function = implementation
            else:
                candidate = getattr(implementation, "postprocess", None)
                if callable(candidate):
                    function = candidate
            if function is None:
                self._note_failure(info, "no callable postprocess(text, context)")
                continue
            try:
                produced = function(result, context)
            except Exception as exc:
                self._note_failure(info, f"{type(exc).__name__}: {exc}")
                continue
            if produced is None:
                continue
            if not isinstance(produced, str):
                self._note_failure(
                    info, f"returned {type(produced).__name__}, expected str"
                )
                continue
            result = produced
        return result

    def export(self, fmt: str, payload: Any, options: dict[str, Any] | None = None) -> str | None:
        """Render ``payload`` in the named format, or None when nobody provides it."""
        wanted = fmt.strip().lower()
        for info, implementation in self.implementations("export"):
            name = str(getattr(implementation, "format", "") or "").lower()
            if name != wanted:
                continue
            function = (
                implementation
                if callable(implementation)
                else getattr(implementation, "export", None)
            )
            if not callable(function):
                self._note_failure(info, "no callable export(payload, options)")
                continue
            try:
                produced = function(payload, options or {})
            except Exception as exc:
                self._note_failure(info, f"{type(exc).__name__}: {exc}")
                return None
            if not isinstance(produced, str):
                self._note_failure(info, f"returned {type(produced).__name__}, expected str")
                return None
            return produced
        return None

    def export_formats(self) -> list[str]:
        formats = []
        for _info, implementation in self.implementations("export"):
            name = str(getattr(implementation, "format", "") or "").lower()
            if name:
                formats.append(name)
        return sorted(set(formats))

    def plugin_names(self) -> list[str]:
        return [p.name for p in self.plugins]

    def connected_points(self) -> list[str]:
        """Points the engine actually calls, as opposed to reserved ones."""
        return sorted(name for name, (_sig, wired) in EXTENSION_POINTS.items() if wired)

    def _note_failure(self, info: PluginInfo, message: str) -> None:
        key = f"{info.name}: {message}"
        self.failures[key] = self.failures.get(key, 0) + 1
        print(f"[plugins] {info.name}: {message}", file=sys.stderr)

    # -- reporting --------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "loaded": sum(1 for p in self.plugins if p.ok),
            "failed": sum(1 for p in self.plugins if p.error),
            "points": sorted(EXTENSION_POINTS),
            "connected_points": sorted(
                name for name, (_sig, wired) in EXTENSION_POINTS.items() if wired
            ),
            "export_formats": self.export_formats(),
            "failures": dict(self.failures),
            "plugins": [p.to_dict() for p in self.plugins],
        }

    def describe(self) -> list[str]:
        lines: list[str] = []
        for info in self.plugins:
            mark = "ok " if info.ok else "!! "
            lines.append(f"{mark}{info.label()}")
            if info.description:
                lines.append(f"     {info.description}")
            if info.declared_points and sorted(info.declared_points) != sorted(info.provided_points):
                lines.append(
                    f"     declared {info.declared_points} but provided "
                    f"{info.provided_points}"
                )
        if not self.plugins:
            lines.append("(no plugins found)")
        return lines


def plugin_directories(config: Any) -> list[Path]:
    """Where plugins are looked up: shipped ones first, then the user's.

    User plugins load last so that, where two provide the same export format,
    the later registration is the one found first by a reverse scan. Ordering is
    otherwise not significant: postprocessors all run, in discovery order.
    """
    root = Path(getattr(config, "base_dir", ".")).parent
    configured = (getattr(config, "plugins", {}) or {})
    dirs: list[Path] = []

    for entry in configured.get("directories") or []:
        path = Path(entry)
        dirs.append(path if path.is_absolute() else root / path)
    if not dirs:
        dirs = [root / "plugins" / "builtin", root / "plugins" / "user"]
    return dirs
