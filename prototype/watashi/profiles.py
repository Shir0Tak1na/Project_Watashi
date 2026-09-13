"""Profiles: named bundles of corpus, rules, presentation and settings.

Axis D of the customization request: "a named bundle per work or project, one
click to switch". A profile can add corpus directories, pick a presentation,
and override settings -- so a translator working on one novel can keep its
glossary, its preferred layout and its language pair together and switch the
whole bundle at once.

Two kinds ship built in: the **memory tiers**, which matter because the local
model is 715 MiB of resident memory and therefore the difference between running
on a small machine and not:

============  =========================================================
``lean``      corpus + rules only, no model. ~175 MiB. Works anywhere.
``balanced``  the model on, 4 threads. ~890 MiB. The normal desktop case.
``full``      the model on with more threads and a wider beam. Slower and
              heavier; useful when quality matters more than latency.
============  =========================================================

User profiles are YAML files, looked up in ``config/profiles/`` (repository
level) and ``<prototype>/profiles/``, so a profile can live with the project or
next to the working configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: Built-in memory tiers. ``None`` for nmt_model means "do not load the model".
MEMORY_TIERS: dict[str, dict[str, Any]] = {
    "lean": {
        "description": "corpus + rules only, no model (~175 MiB)",
        "translation": {"nmt_model": None},
        "capture": {"fps": 8.0},
    },
    "balanced": {
        "description": "local model on, 4 threads (~890 MiB)",
        "translation": {"nmt_model": "__configured__", "nmt_intra_threads": 4, "nmt_beam_size": 1},
        "capture": {"fps": 10.0},
    },
    "full": {
        "description": "local model on, more threads and a wider beam",
        "translation": {"nmt_model": "__configured__", "nmt_intra_threads": 8, "nmt_beam_size": 4},
        "capture": {"fps": 12.0},
    },
}


@dataclass
class Profile:
    name: str
    description: str = ""
    path: Path | None = None
    #: extra corpus directories per layer: {"user": [...], "domain": [...], ...}
    corpus: dict[str, list[str]] = field(default_factory=dict)
    presentation: Any = None
    translation: dict[str, Any] = field(default_factory=dict)
    capture: dict[str, Any] = field(default_factory=dict)
    ocr: dict[str, Any] = field(default_factory=dict)
    rules: list[str] = field(default_factory=list)
    memory: str | None = None

    @property
    def builtin(self) -> bool:
        return self.path is None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def profile_dirs(config: Any) -> list[Path]:
    """Where profiles are looked up, in priority order."""
    dirs: list[Path] = []
    base = Path(getattr(config, "base_dir", "."))
    for candidate in (base / "profiles", base.parent / "config" / "profiles"):
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _from_dict(name: str, data: dict[str, Any], path: Path | None = None) -> Profile:
    def as_dirs(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(v) for v in value]
        return []

    corpus_raw = data.get("corpus") or {}
    corpus = {
        layer: as_dirs(corpus_raw.get(layer))
        for layer in ("user", "domain", "general")
        if corpus_raw.get(layer)
    }
    return Profile(
        name=str(data.get("name") or name),
        description=str(data.get("description") or ""),
        path=path,
        corpus=corpus,
        presentation=data.get("presentation"),
        translation=dict(data.get("translation") or {}),
        capture=dict(data.get("capture") or {}),
        ocr=dict(data.get("ocr") or {}),
        rules=as_dirs((data.get("rules") or {}).get("files"))
        if isinstance(data.get("rules"), dict)
        else as_dirs(data.get("rules")),
        memory=data.get("memory"),
    )


def list_profiles(config: Any) -> list[Profile]:
    """Built-in memory tiers plus every profile file found on disk."""
    profiles: list[Profile] = []
    for tier, spec in MEMORY_TIERS.items():
        profiles.append(
            Profile(
                name=tier,
                description=str(spec.get("description", "")),
                translation=dict(spec.get("translation") or {}),
                capture=dict(spec.get("capture") or {}),
                memory=tier,
            )
        )

    seen = {p.name for p in profiles}
    for directory in profile_dirs(config):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.y*ml")):
            data = _load_yaml(path)
            if not data:
                continue
            profile = _from_dict(path.stem, data, path)
            if profile.name in seen:
                # a file overrides a built-in of the same name
                profiles = [p for p in profiles if p.name != profile.name]
            seen.add(profile.name)
            profiles.append(profile)
    return profiles


def find_profile(config: Any, name: str) -> Profile | None:
    wanted = name.strip().lower()
    for profile in list_profiles(config):
        if profile.name.lower() == wanted:
            return profile
    return None


def _resolve(value: str, config: Any) -> Any:
    """``__configured__`` restores the model path from the original config.

    It has to come from ``nmt_model_configured`` rather than the live
    ``translation.nmt_model``: the ``lean`` tier sets the live value to None, so
    reading it back would make "switch back to balanced" a silent no-op that
    leaves the user on the cheap path without saying so.
    """
    if value == "__configured__":
        remembered = getattr(config, "nmt_model_configured", None)
        if remembered is not None:
            return remembered
        return config.translation.get("nmt_model")
    return value


def apply_profile(config: Any, profile: Profile) -> list[str]:
    """Mutate the config in place. Returns human readable changes.

    Corpus directories are *added* to the layer lists rather than replacing them,
    so switching profiles is additive and a profile cannot accidentally hide the
    user's own vocabulary.
    """
    changes: list[str] = []

    if profile.translation:
        for key, value in profile.translation.items():
            value = _resolve(value, config) if isinstance(value, str) else value
            if config.translation.get(key) != value:
                config.translation[key] = value
                changes.append(f"translation.{key}={value}")
    if profile.capture:
        for key, value in profile.capture.items():
            if config.capture.get(key) != value:
                config.capture[key] = value
                changes.append(f"capture.{key}={value}")
    if profile.ocr:
        for key, value in profile.ocr.items():
            if config.ocr.get(key) != value:
                config.ocr[key] = value
                changes.append(f"ocr.{key}={value}")

    if profile.corpus:
        for layer, dirs in profile.corpus.items():
            current = config.corpus.setdefault(layer, [])
            if isinstance(current, str):
                current = [current]
                config.corpus[layer] = current
            for directory in dirs:
                if directory not in current:
                    current.append(directory)
                    changes.append(f"corpus.{layer} += {directory}")

    if profile.rules:
        current = config.rules.setdefault("files", [])
        if isinstance(current, str):
            current = [current]
            config.rules["files"] = current
        for rule_file in profile.rules:
            if rule_file not in current:
                current.append(rule_file)
                changes.append(f"rules.files += {rule_file}")

    if profile.presentation is not None:
        config.presentation = profile.presentation
        changes.append(f"presentation={profile.presentation}")

    if not changes:
        changes.append("no changes (already active)")
    return changes


def describe_profiles(config: Any) -> Iterable[str]:
    for profile in list_profiles(config):
        origin = "built-in" if profile.builtin else str(profile.path)
        yield f"{profile.name:12s} {profile.description or '-':48s} [{origin}]"
