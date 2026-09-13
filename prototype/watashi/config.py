"""Configuration loading for the prototype.

Every path in the config file is resolved relative to the config file's own
directory, so the prototype can be launched from anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

from .capture import Region

#: Settings changed through the UI are written here, not into config.yaml.
#: See AppConfig.load for why the documented config is never rewritten in place.
OVERRIDES_NAME = "config.user.yaml"

DEFAULTS: dict[str, Any] = {
    "capture": {
        "monitor": 1,
        "region": None,
        "region_ratio": 0.18,
        "fps": 10.0,
        "diff_threshold": 2.0,
        "signature_width": 96,
        "max_width": 0,
        "min_ocr_interval": 0.0,
        #: hold OCR back until the frame has been still this long (ms). 0 = the
        #: original behaviour: recognise the changing frame itself.
        "settle_ms": 0,
        #: keep this program's own windows out of what it photographs
        "exclude_self": True,
        #: pause at startup when the capture region covers one of those windows
        "hold_if_self_visible": True,
    },
    "ocr": {
        "intra_op_threads": 4,
        "inter_op_threads": 1,
        "use_mem_arena": True,
        "det_limit_type": "max",
        "det_limit_side_len": None,
        "use_cls": False,
        #: translate and draw only the largest N recognised boxes; 0 = no cap
        "max_boxes": 0,
    },
    "translation": {
        "source": "auto",
        "target": "zh-CN",
        #: which scene's term entries win; "" = no preference, all equally eligible
        "scene": "",
        "min_confidence": 0.0,
        "nmt_model": None,
        "nmt_compute_type": "int8",
        "nmt_beam_size": 1,
        "nmt_intra_threads": 4,
        "protect_terms": True,
        "protect_min_confidence": 0.4,
    },
    "corpus": {
        "user": ["../plugins/user/custom_rules"],
        "domain": ["corpus", "../rules/slang", "../rules/fiction"],
        "general": ["../rules/dictionaries"],
        #: check corpus/rule mtimes before each translation and reload on change
        "auto_reload": True,
        #: at most one such check per interval; the check is a few stats, not a scan
        "reload_interval_ms": 500,
    },
    "rules": {"files": ["rules/engine_rules.json"]},
    "plugins": {
        "enabled": True,
        # empty = the shipped plugins plus the user's own
        "directories": [],
        # run the postprocess chain over finished translations
        "postprocess": True,
        # how many recognised frames to keep for export
        "history_limit": 2000,
    },
    #: a preset name ("bar", "inplace", ...) or a full spec object
    "presentation": "bar",
    "profile": None,
    "overlay": {
        "mode": "both",
        "subtitle_style": "plate",
        "font_family": None,
        "subtitle_size": 24,
        "panel_size": 13,
        "bar_height": 96,
        "bar_alpha": 0.72,
        "bar_bottom_margin": 90,
        "panel_width": 540,
        "panel_height": 320,
        "panel_history": 12,
        "dim_low_confidence": True,
    },
    "logging": {"print_lines": False, "print_trace": False, "print_refined": True},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class AppConfig:
    """Resolved configuration, with paths already made absolute."""

    base_dir: Path
    capture: dict[str, Any] = field(default_factory=dict)
    ocr: dict[str, Any] = field(default_factory=dict)
    translation: dict[str, Any] = field(default_factory=dict)
    corpus: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    plugins: dict[str, Any] = field(default_factory=dict)
    overlay: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=dict)
    #: presentation preset name or spec object
    presentation: Any = "bar"
    profile: str | None = None
    #: The model path as configured, remembered separately so a profile can turn
    #: the model off and a later profile can turn it back on. Without this,
    #: switching lean -> balanced would resolve "the configured model" to None,
    #: because lean already overwrote it.
    nmt_model_configured: str | None = None
    region: Region | None = None

    # -- accessors -------------------------------------------------------- #

    @property
    def monitor(self) -> int:
        return int(self.capture.get("monitor", 1))

    @property
    def fps(self) -> float:
        return float(self.capture.get("fps", 10.0))

    @property
    def target_lang(self) -> str:
        return str(self.translation.get("target", "zh-CN"))

    @property
    def source_lang(self) -> str:
        return str(self.translation.get("source", "auto"))

    def corpus_dirs(self, layer: str) -> list[Path]:
        raw = self.corpus.get(layer) or []
        if isinstance(raw, str):
            raw = [raw]
        return [self._resolve(p) for p in raw]

    def rule_files(self) -> list[Path]:
        raw = self.rules.get("files") or []
        if isinstance(raw, str):
            raw = [raw]
        return [self._resolve(p) for p in raw]

    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else (self.base_dir / path)

    # -- construction ----------------------------------------------------- #

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        if path is None:
            path = Path(__file__).resolve().parent.parent / "config.yaml"
        path = Path(path).expanduser().resolve()
        base_dir = path.parent

        raw: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError(f"{path}: expected a mapping at the top level")
            raw = loaded or {}

        merged = _deep_merge(DEFAULTS, raw)

        # A second, optional layer written by the settings UI.
        #
        # Deliberately a separate file rather than a rewrite of config.yaml: that
        # file's 140 lines of comments *are* the documentation for these settings,
        # and round-tripping it through a YAML library would delete every one of
        # them. An override file also makes "put it back" a one-file delete, and
        # lets the UI show which values the user changed rather than which ones the
        # project shipped.
        overrides_path = base_dir / OVERRIDES_NAME
        if overrides_path.exists():
            with overrides_path.open("r", encoding="utf-8") as fh:
                layer = yaml.safe_load(fh)
            if layer is not None and not isinstance(layer, dict):
                raise ValueError(f"{overrides_path}: expected a mapping at the top level")
            if layer:
                merged = _deep_merge(merged, layer)

        config = cls(
            base_dir=base_dir,
            capture=merged["capture"],
            ocr=merged["ocr"],
            translation=merged["translation"],
            corpus=merged["corpus"],
            rules=merged["rules"],
            plugins=merged["plugins"],
            overlay=merged["overlay"],
            logging=merged["logging"],
            presentation=merged.get("presentation", "bar"),
            profile=merged.get("profile"),
            nmt_model_configured=merged["translation"].get("nmt_model"),
        )

        region_spec = merged["capture"].get("region")
        if region_spec:
            config.region = Region.parse(str(region_spec))
        return config

    @staticmethod
    def overrides_path(base_dir: Path) -> Path:
        return Path(base_dir) / OVERRIDES_NAME

    def read_overrides(self) -> dict[str, Any]:
        """The raw override layer, as nested dicts. Empty when there is none."""
        path = self.overrides_path(self.base_dir)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            layer = yaml.safe_load(fh)
        return layer if isinstance(layer, dict) else {}

    def save_overrides(self, changes: dict[str, Any]) -> list[str]:
        """Write ``{dotted.key: value}`` into the override file. Returns the keys written.

        Merging into whatever is already there is the point: the UI sends one field
        at a time, and each write must not discard the others. A value equal to the
        shipped default is *removed* from the layer rather than stored, so the file
        stays a record of deliberate changes and "reset to default" actually resets.
        """
        from . import settings_schema

        layer = self.read_overrides()

        for key, value in changes.items():
            field = settings_schema.find(key)
            if field is None:
                raise ValueError(f"{key!r} is not a known setting")
            value = settings_schema.coerce(field, value)
            section, name = settings_schema.split(key)
            shipped = (
                DEFAULTS.get(section, {}).get(name)
                if section
                else DEFAULTS.get(name)
            )
            if value == shipped:
                if section and section in layer:
                    layer[section].pop(name, None)
                    if not layer[section]:
                        del layer[section]
                elif not section:
                    layer.pop(name, None)
                continue
            if section:
                layer.setdefault(section, {})[name] = value
            else:
                layer[name] = value

        path = self.overrides_path(self.base_dir)
        if layer:
            path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                "# Written by the settings panel. Merged over config.yaml.\n"
                "# Delete this file to return every setting to its shipped value.\n"
                "# Only values that differ from the defaults are stored here.\n"
            )
            body = yaml.safe_dump(layer, allow_unicode=True, sort_keys=True, default_flow_style=False)
            path.write_text(header + body, encoding="utf-8")
        elif path.exists():
            # nothing left to override: remove the file so it cannot drift
            path.unlink()

        return sorted(changes)

    def describe(self) -> Sequence[str]:
        lines = [
            f"config dir      : {self.base_dir}",
            f"monitor         : {self.monitor}",
            f"region          : {self.region if self.region else 'auto (bottom strip)'}",
            f"fps target      : {self.fps}",
            f"diff threshold  : {self.capture.get('diff_threshold')}",
            f"OCR max width   : {self.capture.get('max_width')}",
            f"OCR threads     : intra {self.ocr.get('intra_op_threads')} / "
            f"inter {self.ocr.get('inter_op_threads')}, "
            f"mem arena {self.ocr.get('use_mem_arena')}, "
            f"det limit_type {self.ocr.get('det_limit_type')}",
            f"language        : {self.source_lang} -> {self.target_lang}",
            f"presentation    : {self.presentation!r}",
            f"profile         : {self.profile or '(none)'}",
            f"overlay mode    : {self.overlay.get('mode')} ({self.overlay.get('subtitle_style')})",
        ]
        for layer in ("user", "domain", "general"):
            dirs = self.corpus_dirs(layer)
            lines.append(f"corpus {layer:8s}: {', '.join(str(d) for d in dirs) or '(none)'}")
        lines.append(f"rule files      : {', '.join(str(f) for f in self.rule_files()) or '(none)'}")
        model = self.translation.get("nmt_model")
        if model:
            path = self._resolve(str(model))
            state = "present" if path.exists() else "MISSING (run fetch_model.py)"
            lines.append(
                f"local model     : {path} [{state}, "
                f"{self.translation.get('nmt_compute_type')}, "
                f"beam {self.translation.get('nmt_beam_size')}, "
                f"threads {self.translation.get('nmt_intra_threads')}]"
            )
            lines.append(
                f"term protection : {self.translation.get('protect_terms')} "
                f"(min confidence {self.translation.get('protect_min_confidence')})"
            )
        else:
            lines.append("local model     : disabled (corpus + rules only)")
        return lines
