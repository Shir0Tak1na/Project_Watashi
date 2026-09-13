#!/usr/bin/env python3
"""Plugin skeleton verification. No screen, no models, no display.

A plugin system is mostly about what happens when things go *wrong*, so most of
these checks are about failure handling: a module that raises on import, one that
declares an incompatible API version, one that returns the wrong type, one whose
function explodes at call time. Each must be reported and skipped, never fatal,
and never silently swallowed.

    python selfcheck_plugins.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker
from watashi.plugins import (
    EXTENSION_POINTS,
    PLUGIN_API_VERSION,
    PluginRegistry,
    plugin_directories,
)

ROOT = Path(__file__).resolve().parent.parent


def write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Plugin skeleton self check (no screen, no models)")
    print("=" * 78)

    # ---- the shipped plugins -------------------------------------------- #
    print("")
    print("-- the shipped example plugins --")
    registry = PluginRegistry()
    registry.load_all([ROOT / "plugins" / "builtin"])
    names = [p.name for p in registry.plugins if p.ok]
    check.check("both shipped plugins load", len(names) == 2, f"{names}")
    check.check("postprocess has an implementation",
                len(registry.implementations("postprocess")) == 1)
    check.check("export has two implementations from one module",
                len(registry.implementations("export")) == 2,
                f"formats={registry.export_formats()}")
    check.check("export formats are discoverable",
                registry.export_formats() == ["glossary", "srt"],
                f"{registry.export_formats()}")

    # ---- postprocess behaviour ------------------------------------------ #
    print("")
    print("-- postprocess --")
    keeps = [
        ("打得好，打得漂亮 新手", "space between two translated words"),
        ("反龙 超灵力 虚空剑", "separations between three terms"),
        ("the 剑意 of this 宗门 is a myth", "terms inside an English frame"),
    ]
    for text, why in keeps:
        check.check(f"keeps {why}", registry.postprocess(text) == text, repr(text))
    rejoins = [
        ("剑 意 是 个 神 话", "剑意是个神话"),
        ("虚 空 境 界", "虚空境界"),
    ]
    for text, want in rejoins:
        check.check(f"rejoins OCR-split glyphs in {text!r}",
                    registry.postprocess(text) == want,
                    f"-> {registry.postprocess(text)!r}")
    check.check("strips a space before CJK punctuation",
                registry.postprocess("他突破了 。") == "他突破了。")
    check.check("returns None for text it does not change",
                registry.postprocess("already clean") == "already clean")

    # ---- export behaviour ----------------------------------------------- #
    print("")
    print("-- export --")
    payload = {
        "entries": [
            {"start": 0.0, "end": 1.5, "source": "a", "target": "甲"},
            {"source": "b", "target": "乙"},
            {"source": "dup", "target": "丙"},
            {"source": "dup", "target": "丙"},
            {"source": "same", "target": "same"},
        ]
    }
    srt = registry.export("srt", payload)
    check.check("srt renders", bool(srt) and "-->" in (srt or ""))
    check.check("srt invents timings when an entry has none",
                (srt or "").count("-->") == 5, f"{(srt or '').count('-->')} cues")
    check.check("srt keeps entries with no translation rather than dropping them",
                "same" in (srt or ""))
    bilingual = registry.export("srt", payload, {"bilingual": True})
    check.check("srt bilingual mode emits both languages",
                bilingual is not None and "a\n甲" in bilingual, repr(bilingual)[:60])
    glossary = registry.export("glossary", payload)
    check.check("glossary dedupes identical pairs",
                (glossary or "").count("dup") == 1, repr(glossary))
    check.check("glossary skips pairs where source equals target",
                "same" not in (glossary or ""))
    check.check("an unknown format returns None instead of raising",
                registry.export("no-such-format", payload) is None)

    # ---- failure handling ------------------------------------------------ #
    print("")
    print("-- failure handling (the point of a plugin system) --")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)

        write(directory, "broken_import.py", "raise RuntimeError('boom at import')\n")
        write(directory, "wrong_api.py", (
            "PLUGIN = {'name': 'wrong-api', 'api': 99, 'points': ['postprocess']}\n"
            "def register():\n"
            "    return {'postprocess': lambda t, c: t}\n"
        ))
        write(directory, "wrong_type.py", (
            "PLUGIN = {'name': 'wrong-type', 'api': 1, 'points': ['postprocess']}\n"
            "def register():\n"
            "    return {'postprocess': lambda t, c: 12345}\n"
        ))
        write(directory, "raises_at_call.py", (
            "PLUGIN = {'name': 'raises-at-call', 'api': 1, 'points': ['postprocess']}\n"
            "def boom(text, context):\n"
            "    raise ValueError('boom at call')\n"
            "def register():\n"
            "    return {'postprocess': boom}\n"
        ))
        write(directory, "unknown_point.py", (
            "PLUGIN = {'name': 'unknown-point', 'api': 1}\n"
            "def register():\n"
            "    return {'telepathy': object()}\n"
        ))
        write(directory, "no_register.py", "VALUE = 1\n")
        write(directory, "legacy.py", (
            "class Old:\n"
            "    def translate(self, text, src, dst):\n"
            "        return text\n"
            "def register():\n"
            "    return Old()\n"
        ))
        write(directory, "_private.py", (
            "PLUGIN = {'name': 'should-not-load', 'api': 1}\n"
            "def register():\n"
            "    return {'postprocess': lambda t, c: 'PRIVATE'}\n"
        ))
        write(directory, "good.py", (
            "PLUGIN = {'name': 'good', 'api': 1, 'points': ['postprocess']}\n"
            "def upper(text, context):\n"
            "    return text.upper()\n"
            "def register():\n"
            "    return {'postprocess': upper}\n"
        ))

        failing = PluginRegistry()
        failing.load_all([directory])
        by_name = {p.name: p for p in failing.plugins}

        check.check("a module that raises on import is reported, not fatal",
                    "broken_import" in by_name
                    and "import failed" in by_name["broken_import"].error,
                    by_name.get("broken_import", by_name.get("none")).error[:52])
        check.check("an incompatible API version is refused with an explanation",
                    "wrong-api" in by_name and "speaks" in by_name["wrong-api"].error,
                    by_name.get("wrong-api", by_name.get("none")).error[:60])
        check.check("a non-dict register() names the legacy convention",
                    "legacy" in by_name and "legacy" in by_name["legacy"].error.lower(),
                    by_name.get("legacy", by_name.get("none")).error[:60])
        check.check("an unknown extension point is refused",
                    "unknown-point" in by_name
                    and "unknown extension point" in by_name["unknown-point"].error,
                    by_name.get("unknown-point", by_name.get("none")).error[:56])
        check.check("a module without register() is reported",
                    "no_register" in by_name
                    and "no register()" in by_name["no_register"].error)
        check.check("an underscore-prefixed file is not treated as a plugin",
                    "_private" not in by_name)
        check.check("the good plugin still loads alongside seven bad ones",
                    "good" in by_name and by_name["good"].ok)

        # a postprocessor that raises must not stop the chain
        chained = failing.postprocess("hello")
        check.check("a postprocessor that raises is skipped, not propagated",
                    chained == "HELLO", f"got {chained!r}")
        check.check("the failure is recorded rather than swallowed",
                    any("raises-at-call" in key for key in failing.failures),
                    f"{list(failing.failures)[:2]}")
        check.check("a postprocessor returning a non-string is rejected",
                    any("wrong-type" in key for key in failing.failures),
                    f"{[k for k in failing.failures if 'wrong' in k][:1]}")
        check.check("a rejected plugin does not corrupt the text",
                    "12345" not in chained)

    # ---- versioning and reporting ---------------------------------------- #
    print("")
    print("-- versioning and reporting --")
    check.check("the API version is declared", PLUGIN_API_VERSION >= 1,
                f"v{PLUGIN_API_VERSION}")
    check.check("every contract is documented with a signature",
                all(isinstance(v, tuple) and len(v) == 2 for v in EXTENSION_POINTS.values()))
    check.check("connected and reserved points are distinguished",
                EXTENSION_POINTS["postprocess"][1] is True
                and EXTENSION_POINTS["renderer"][1] is False,
                "postprocess and export are called; corpus_loader/renderer/translator "
                "are reserved")
    status = registry.status()
    check.check("status reports counts and formats",
                status["loaded"] == 2 and status["export_formats"] == ["glossary", "srt"],
                f"{status['loaded']} loaded")
    check.check("status is JSON serialisable",
                _json_ok(status))
    check.check("plugin directories resolve without a config entry",
                len(plugin_directories(type("C", (), {"base_dir": ROOT / "prototype"})())) == 2)

    return check.report()


def _json_ok(value) -> bool:
    import json

    try:
        json.dumps(value)
    except TypeError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
