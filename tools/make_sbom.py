#!/usr/bin/env python3
"""Generate the project SBOM from tools/license_manifest.json.

    python tools/make_sbom.py            # rewrite licenses/sbom.spdx.json
    python tools/make_sbom.py --check    # fail when the committed SBOM is stale

The SBOM is SPDX 2.3 JSON. It lists every build, runtime, test and model
dependency with name/version/licence/source/type. Model weights are NOT payload:
each model package says ``payload=no`` and points at its models.lock.json entry
instead of claiming a bundled file. Output is deterministic (no build timestamp),
so ``--check`` is a byte-for-byte drift check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools" / "license_manifest.json"
SBOM = ROOT / "licenses" / "sbom.spdx.json"

REQUIRED_FIELDS = ("name", "version", "license", "source", "type", "payload")
PURPOSE = {
    "application": "APPLICATION",
    "runtime": "LIBRARY",
    "build": "LIBRARY",
    "test": "OTHER",
    "model": "OTHER",
}


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing licence manifest: {path}")


def validate(document: dict) -> list[str]:
    errors: list[str] = []
    if document.get("manifest_version") != 1:
        errors.append("manifest_version must be 1")
    sbom = document.get("sbom")
    if not isinstance(sbom, dict):
        errors.append("sbom block is missing")
        sbom = {}
    for key in ("name", "namespace", "created"):
        if not str(sbom.get(key, "")).strip():
            errors.append(f"sbom.{key} is empty")
    if not str(sbom.get("namespace", "")).startswith("http"):
        errors.append("sbom.namespace must be an absolute URL")

    components = document.get("components")
    if not isinstance(components, list) or not components:
        errors.append("components must be a non-empty list")
        return errors

    seen: set[str] = set()
    for index, component in enumerate(components):
        where = f"components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{where} must be an object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in component]
        for field in missing:
            errors.append(f"{where} is missing {field}")
        name = str(component.get("name", ""))
        if not name.strip():
            errors.append(f"{where}.name is empty")
        elif name in seen:
            errors.append(f"{where}.name is duplicated: {name}")
        seen.add(name)
        if not isinstance(component.get("payload"), bool):
            errors.append(f"{where}.payload must be true or false")
        if component.get("type") not in PURPOSE:
            errors.append(f"{where}.type is not one of {', '.join(PURPOSE)}")
        if component.get("type") == "model":
            if component.get("payload"):
                errors.append(f"{where}: a model must never be payload")
            if not str(component.get("weights", "")).strip():
                errors.append(f"{where}: a model needs a models.lock.json reference")
    return errors


def spdx_id(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-")
    return f"SPDXRef-{slug or 'Package'}"


def build_sbom(document: dict) -> dict:
    sbom = document["sbom"]
    packages = []
    for component in document["components"]:
        details = [
            f"type={component['type']}",
            f"payload={'yes' if component['payload'] else 'no'}",
        ]
        if component.get("location"):
            details.append(f"location={component['location']}")
        if component.get("weights"):
            details.append(f"weights={component['weights']}")
        if component.get("license_note"):
            details.append(f"note={component['license_note']}")
        packages.append(
            {
                "SPDXID": spdx_id(component["name"]),
                "name": component["name"],
                "versionInfo": str(component["version"]),
                "supplier": "NOASSERTION",
                "downloadLocation": component["source"],
                "filesAnalyzed": False,
                "primaryPackagePurpose": PURPOSE[component["type"]],
                "licenseConcluded": component["license"],
                "licenseDeclared": component["license"],
                "copyrightText": "NOASSERTION",
                "comment": "; ".join(details),
            }
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": sbom["name"],
        "documentNamespace": sbom["namespace"],
        "creationInfo": {
            "created": sbom["created"],
            "creators": ["Tool: tools/make_sbom.py"],
            "licenseListVersion": "3.24",
        },
        "packages": packages,
    }


def serialize(document: dict) -> str:
    document = build_sbom(document)
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def first_difference(expected: str, actual: str) -> str:
    for number, (want, got) in enumerate(
        zip(expected.splitlines(), actual.splitlines()), start=1
    ):
        if want != got:
            return f"line {number}:\n  expected: {want}\n  actual:   {got}"
    return "the files differ in length"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="fail when the committed SBOM is stale")
    parser.add_argument("--output", type=Path, default=SBOM)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    arguments = parser.parse_args()

    document = load_manifest(arguments.manifest)
    errors = validate(document)
    if errors:
        for error in errors:
            print(f"licence manifest: {error}", file=sys.stderr)
        return 1

    text = serialize(document)
    components = len(document["components"])
    if arguments.check:
        try:
            committed = arguments.output.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"SBOM is missing: {arguments.output}", file=sys.stderr)
            return 1
        if committed != text:
            print("SBOM is stale; run python tools/make_sbom.py", file=sys.stderr)
            print(first_difference(text, committed), file=sys.stderr)
            return 1
        print(f"SBOM is up to date ({components} packages, {arguments.output.name})")
        return 0

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(text, encoding="utf-8")
    print(f"wrote {arguments.output} ({components} packages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
