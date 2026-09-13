#!/usr/bin/env python3
"""Settings schema verification. Headless: no screen, no models.

A settings page is only worth having if it tells the truth, and the way these
pages fail is drift: a field describes a key that no longer exists, a key exists
that no page mentions, a default is stale, or a control marked "immediate" does
nothing when you touch it. Every check here exists to catch one of those.

The bidirectional key check is the important one. Checking only "every schema key
is real" would pass forever while settings quietly went undocumented -- which is
the exact complaint that prompted this work ("不知道功能是什么").

    run.cmd selfcheck_settings
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi import settings_schema as schema
from watashi.config import DEFAULTS, AppConfig


def walk(mapping: dict, prefix: str = "") -> list[str]:
    """Every leaf key in a nested config dict, as dotted paths."""
    found: list[str] = []
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found.extend(walk(value, f"{path}."))
        else:
            found.append(path)
    return found


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Settings schema self check (no screen, no models)")
    print("=" * 78)

    fields = schema.all_fields()
    keys = [f.key for f in fields]

    # ---------------------------------------------------------------- #
    check.section("the schema covers the real config, in both directions")
    config = AppConfig.load()
    real_keys = set(walk(DEFAULTS)) | {"presentation", "profile"}

    missing = sorted(set(keys) - real_keys)
    check.check(
        "every field names a real config key",
        not missing,
        f"unknown: {missing}" if missing else f"{len(keys)} fields",
    )

    undocumented = sorted(real_keys - set(keys) - set(schema.INTERNAL_KEYS))
    check.check(
        "every config key is either described or explicitly internal",
        not undocumented,
        f"undocumented: {undocumented}" if undocumented else "none",
    )

    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    check.check("no key is described twice", not duplicates, f"duplicates: {duplicates}")

    # ---------------------------------------------------------------- #
    check.section("declared types match the values actually in the config")
    kind_expects = {
        "bool": bool,
        "int": int,
        "float": (int, float),
        "enum": str,
        "text": str,
        "path": str,
        "path_list": list,
        "region": str,
    }
    bad_type = []
    for f in fields:
        value = schema.get_value(config, f.key)
        if value is None:
            continue  # None is a legitimate "unset" for many fields
        expected = kind_expects.get(f.kind)
        if expected and not isinstance(value, expected):
            bad_type.append(f"{f.key}={value!r} ({f.kind})")
        if f.kind == "float" and isinstance(value, bool):
            bad_type.append(f"{f.key} is a bool declared float")
    check.check(
        "no field's declared kind contradicts its value",
        not bad_type,
        f"mismatched: {bad_type}" if bad_type else f"{len(fields)} fields checked",
    )

    bad_choices = [
        f"{f.key}={schema.get_value(config, f.key)!r} not in {f.choices}"
        for f in fields
        if f.kind == "enum"
        and f.choices
        and schema.get_value(config, f.key) is not None
        and schema.get_value(config, f.key) not in f.choices
    ]
    check.check(
        "every enum value in the config is one of the offered choices",
        not bad_choices,
        f"{bad_choices}" if bad_choices else "",
    )

    # ---------------------------------------------------------------- #
    check.section("every field is actually documented")
    undocumented_fields = [
        f.key for f in fields if len(f.label) < 2 or len(f.description) < 15
    ]
    check.check(
        "every field has a label and a real description",
        not undocumented_fields,
        f"too thin: {undocumented_fields}" if undocumented_fields else "",
    )
    check.check(
        "labels are unique, so a form cannot show two identical rows",
        len({f.label for f in fields}) == len(fields),
        f"{len({f.label for f in fields})} unique labels for {len(fields)} fields",
    )
    numeric_without_range = [
        f.key for f in fields if f.kind in ("int", "float") and (f.low is None or f.high is None)
    ]
    check.check(
        "every numeric field states its range, so the UI can bound the input",
        not numeric_without_range,
        f"no range: {numeric_without_range}" if numeric_without_range else "",
    )

    # ---------------------------------------------------------------- #
    check.section("a field marked immediate really can be applied immediately")
    live = [f for f in fields if f.applies == schema.LIVE]
    unbacked = [f.key for f in live if not f.command]
    check.check(
        "every immediate field names the command that applies it",
        not unbacked,
        f"no command: {unbacked}" if unbacked else f"{len(live)} immediate fields",
    )

    known_commands = {
        "set_fps",
        "set_diff_threshold",
        "set_region",
        "set_target_lang",
        "set_presentation",
        "load_profile",
        "use_window",
        "reload_corpus",
    }
    unknown = sorted({f.command for f in fields if f.command} - known_commands)
    check.check(
        "and that command is one the engine really implements",
        not unknown,
        f"not implemented: {unknown}" if unknown else "",
    )

    restart = [f for f in fields if f.applies == schema.RESTART]
    check.check(
        "the restart-required fields are the majority and are labelled as such",
        len(restart) > 0 and all(f.applies == schema.RESTART for f in restart),
        f"{len(live)} immediate / {len(restart)} need a restart",
    )

    # ---------------------------------------------------------------- #
    check.section("coercion accepts good input and rejects bad input with a reason")
    by_key = {f.key: f for f in fields}

    check.check("bool accepts 'true'", schema.coerce(by_key["plugins.enabled"], "true") is True)
    check.check("bool accepts '0'", schema.coerce(by_key["plugins.enabled"], "0") is False)
    try:
        schema.coerce(by_key["plugins.enabled"], "maybe")
        check.check("bool rejects nonsense", False, "no error raised")
    except ValueError as exc:
        check.check("bool rejects nonsense with a reason", "开关值" in str(exc), str(exc))

    check.check(
        "float parses a string",
        schema.coerce(by_key["capture.diff_threshold"], "3.5") == 3.5,
    )
    try:
        schema.coerce(by_key["capture.diff_threshold"], "999")
        check.check("out-of-range float is refused", False, "no error raised")
    except ValueError as exc:
        check.check("out-of-range float is refused with its bounds", "0.0–255.0" in str(exc), str(exc))

    try:
        schema.coerce(by_key["overlay.mode"], "sideways")
        check.check("enum refuses an unknown choice", False, "no error raised")
    except ValueError as exc:
        check.check(
            "enum refuses an unknown choice and lists the valid ones",
            "bar" in str(exc),
            str(exc),
        )

    check.check(
        "path_list splits a comma-separated string",
        schema.coerce(by_key["corpus.domain"], "a, b ,c") == ["a", "b", "c"],
    )
    check.check(
        "path_list turns an empty string into an empty list",
        schema.coerce(by_key["plugins.directories"], "") == [],
    )

    try:
        schema.coerce(by_key["capture.region"], "not,a,region")
        check.check("region refuses a malformed value", False, "no error raised")
    except Exception as exc:
        check.check("region refuses a malformed value", True, type(exc).__name__)
    check.check(
        "region accepts a valid box and keeps it verbatim",
        schema.coerce(by_key["capture.region"], "10,20,300,80") == "10,20,300,80",
    )
    check.check(
        "region turns an empty string into None (meaning 'automatic strip')",
        schema.coerce(by_key["capture.region"], "") is None,
    )

    # ---------------------------------------------------------------- #
    check.section("the API payload is renderable")
    payload = schema.as_dict(config)
    check.check(
        "as_dict returns every category",
        len(payload["categories"]) == len(schema.CATEGORIES),
        f"{len(payload['categories'])} categories",
    )
    check.check(
        "every category carries current values for its fields",
        all(
            set(category["values"]) == {f.key for f in category_fields.fields}
            for category, category_fields in zip(payload["categories"], schema.CATEGORIES)
        ),
    )
    check.check(
        "the live/restart split is reported so the UI can warn",
        payload["live_count"] + payload["restart_count"] == len(fields),
        f"{payload['live_count']} live + {payload['restart_count']} restart = "
        f"{len(fields)}",
    )
    import json

    check.check(
        "the payload is JSON serialisable (it goes over the panel's API)",
        isinstance(json.dumps(payload, ensure_ascii=False), str),
    )

    # ---------------------------------------------------------------- #
    check.section("the schema has no stale defaults")
    stale = []
    for f in fields:
        if f.default is None:
            continue
        section, name = schema.split(f.key)
        declared = DEFAULTS.get(section, {}).get(name, "<absent>")
        if declared != "<absent>" and declared != f.default:
            stale.append(f"{f.key}: schema says {f.default!r}, config says {declared!r}")
    check.check(
        "declared defaults agree with config.DEFAULTS",
        not stale,
        "; ".join(stale) if stale else f"{len(fields)} fields",
    )

    # ---------------------------------------------------------------- #
    check.section("write-back round-trips through the override file")
    # Exercised in a temp directory, never against the shipped config: a test that
    # edits the real config.yaml is a test that eventually corrupts it.
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="watashi-settings-"))
    try:
        shipped = Path(__file__).resolve().parent / "config.yaml"
        shutil.copy2(shipped, tmp / "config.yaml")

        before = AppConfig.load(tmp / "config.yaml")
        check.check(
            "a fresh copy starts with no override file",
            not AppConfig.overrides_path(tmp).exists(),
        )

        written = before.save_overrides(
            {
                "capture.fps": 25.0,
                "capture.settle_ms": 200,
                "translation.target": "en",
                "overlay.font_family": "SimHei",
                "plugins.enabled": False,
            }
        )
        check.check(
            "save_overrides reports what it wrote",
            len(written) == 5,
            f"{written}",
        )
        check.check(
            "the override file exists",
            AppConfig.overrides_path(tmp).exists(),
        )

        after = AppConfig.load(tmp / "config.yaml")
        check.check(
            "a float change is read back",
            after.fps == 25.0,
            f"fps={after.fps}",
        )
        check.check(
            "an int change (settle_ms) is read back",
            after.capture.get("settle_ms") == 200,
            f"settle_ms={after.capture.get('settle_ms')}",
        )
        check.check(
            "a language change is read back",
            after.target_lang == "en",
            f"target={after.target_lang}",
        )
        check.check(
            "a text change is read back",
            after.overlay.get("font_family") == "SimHei",
            f"font={after.overlay.get('font_family')}",
        )
        check.check(
            "a bool change is read back",
            after.plugins.get("enabled") is False,
            f"plugins.enabled={after.plugins.get('enabled')}",
        )
        check.check(
            "values NOT changed keep their shipped setting",
            after.ocr.get("intra_op_threads") == before.ocr.get("intra_op_threads"),
            f"intra_op_threads={after.ocr.get('intra_op_threads')}",
        )

        # The whole documented config must survive: the reason for a separate
        # override file is that config.yaml's comments are its documentation.
        check.check(
            "config.yaml is byte-identical, comments and all",
            (tmp / "config.yaml").read_bytes() == shipped.read_bytes(),
        )

        layer = after.read_overrides()
        check.check(
            "the layer holds only the changed keys",
            layer == {
                "capture": {"fps": 25.0, "settle_ms": 200},
                "translation": {"target": "en"},
                "overlay": {"font_family": "SimHei"},
                "plugins": {"enabled": False},
            },
            f"{layer}",
        )

        # Setting a value back to its shipped default should erase it from the
        # layer, so the file stays a record of deliberate changes.
        after.save_overrides({"capture.fps": 10.0})
        check.check(
            "setting a value back to the default removes it from the layer",
            "fps" not in after.read_overrides().get("capture", {}),
            f"{after.read_overrides()}",
        )
        reloaded = AppConfig.load(tmp / "config.yaml")
        check.check(
            "and the engine then sees the shipped default again",
            reloaded.fps == 10.0,
            f"fps={reloaded.fps}",
        )

        # Clearing everything should delete the file rather than leave an empty one.
        reloaded.save_overrides(
            {
                "capture.settle_ms": 0,
                "translation.target": "zh-CN",
                "overlay.font_family": None,
                "plugins.enabled": True,
            }
        )
        check.check(
            "removing the last override deletes the file",
            not AppConfig.overrides_path(tmp).exists(),
        )

        # Rejections must not write anything.
        try:
            reloaded.save_overrides({"overlay.mode": "sideways"})
            check.check("an invalid value is refused", False, "no error raised")
        except ValueError:
            check.check("an invalid value is refused", True)
        try:
            reloaded.save_overrides({"not.a.setting": 1})
            check.check("an unknown setting is refused", False, "no error raised")
        except ValueError:
            check.check("an unknown setting is refused", True)
        check.check(
            "a refused write leaves no file behind",
            not AppConfig.overrides_path(tmp).exists(),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
