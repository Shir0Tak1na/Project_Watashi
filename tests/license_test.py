"""Compliance checks for the licence manifest, the SBOM and the packaged licences.

No network, no model weights and no build products are required: the checks read
tools/license_manifest.json, licenses/sbom.spdx.json, licenses/* and
config/install_manifest.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools" / "license_manifest.json"
SBOM = ROOT / "licenses" / "sbom.spdx.json"
LOCK = ROOT / "models.lock.json"
INSTALL_MANIFEST = ROOT / "config" / "install_manifest.json"
LICENSES = ROOT / "licenses"

WEIGHT_SUFFIXES = (".bin", ".onnx", ".pt", ".pth", ".safetensors")

EXPECTED = {
    "ONNX Runtime": ("1.30.0", "MIT"),
    "CTranslate2": ("4.8.4", "MIT"),
    "oneDNN": ("2023.2.0.49513", "Apache-2.0"),
    "SentencePiece": ("0.2.1", "Apache-2.0"),
    "nlohmann-json": ("3.11.3", "MIT"),
    "Catch2": ("3.5.2", "BSL-1.0"),
    "MSVC/UCRT": ("14.44.35207", "LicenseRef-Microsoft-Visual-Cpp-Redistributable"),
    "PP-OCRv4": (None, "Apache-2.0"),
    "NLLB-200-distilled-600M (CTranslate2 int8)": (None, "CC-BY-NC-4.0"),
    "Whisper small (CTranslate2 int8)": (None, "MIT"),
}
MODEL_NAMES = {
    "PP-OCRv4",
    "NLLB-200-distilled-600M (CTranslate2 int8)",
    "Whisper small (CTranslate2 int8)",
}


def log(message: str) -> None:
    print(message, flush=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def case_manifest() -> None:
    document = load_json(MANIFEST)
    components = document["components"]
    by_name = {component["name"]: component for component in components}
    for name, (version, license_name) in EXPECTED.items():
        if name not in by_name:
            raise SystemExit(f"licence manifest is missing {name}")
        component = by_name[name]
        if version is not None and component["version"] != version:
            raise SystemExit(f"{name}: expected version {version}, got {component['version']}")
        if component["license"] != license_name:
            raise SystemExit(f"{name}: expected licence {license_name}, got {component['license']}")
    for component in components:
        for field in ("name", "version", "license", "source", "type", "payload"):
            if field not in component:
                raise SystemExit(f"{component.get('name')}: missing {field}")
        if component["type"] == "model":
            if component["payload"]:
                raise SystemExit(f"{component['name']}: a model must not be payload")
            if not component.get("weights"):
                raise SystemExit(f"{component['name']}: missing models.lock.json reference")
    if not any(component["type"] == "test" for component in components):
        raise SystemExit("Catch2/test component is missing")
    log(f"licence manifest: {len(components)} components, versions and licences match")


def case_sbom() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_sbom.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise SystemExit("make_sbom.py --check failed:\n" + (result.stdout + result.stderr))
    document = load_json(SBOM)
    if document.get("spdxVersion") != "SPDX-2.3":
        raise SystemExit("SBOM is not SPDX-2.3")
    packages = document["packages"]
    manifest = load_json(MANIFEST)["components"]
    if len(packages) != len(manifest):
        raise SystemExit(f"SBOM has {len(packages)} packages, manifest has {len(manifest)}")
    for package in packages:
        for field in ("name", "versionInfo", "licenseDeclared", "downloadLocation"):
            if not package.get(field):
                raise SystemExit(f"SBOM package missing {field}: {package.get('name')}")
    log(f"SBOM matches the manifest ({len(packages)} packages, SPDX-2.3)")


def case_sbom_models() -> None:
    packages = {package["name"]: package for package in load_json(SBOM)["packages"]}
    for name in MODEL_NAMES:
        package = packages.get(name)
        if package is None:
            raise SystemExit(f"SBOM is missing model package {name}")
        comment = package.get("comment", "")
        if "payload=no" not in comment:
            raise SystemExit(f"{name}: SBOM does not say payload=no")
        if "weights=models.lock.json#" not in comment:
            raise SystemExit(f"{name}: SBOM lacks a models.lock.json reference")
        if "payload=yes" in comment:
            raise SystemExit(f"{name}: SBOM claims model weights are payload")
    log("SBOM: model packages are licence-only, payload=no, with models.lock references")


def case_copies() -> None:
    pairs = [
        (LICENSES / "LICENSE", ROOT / "LICENSE"),
        (LICENSES / "THIRD_PARTY_LICENSES.md", ROOT / "THIRD_PARTY_LICENSES.md"),
    ]
    for copy, original in pairs:
        if not copy.exists():
            raise SystemExit(f"missing packaged licence: {copy}")
        if copy.read_bytes() != original.read_bytes():
            raise SystemExit(f"{copy.name} drifted from {original.name}")
    models = (LICENSES / "MODEL_LICENSES.md").read_text(encoding="utf-8")
    for token in ("CC-BY-NC-4.0", "Whisper", "PP-OCRv4", "Apache-2.0", "MIT", "非商业"):
        if token not in models:
            raise SystemExit(f"MODEL_LICENSES.md does not mention {token}")
    log("licenses/ copies match the root licences; model licence notes cover all three")


def case_no_weights() -> None:
    if not LICENSES.is_dir():
        raise SystemExit("licenses/ directory is missing")
    for path in LICENSES.rglob("*"):
        if path.is_file() and path.suffix.lower() in WEIGHT_SUFFIXES:
            raise SystemExit(f"model weight in licenses/: {path}")
    manifest = load_json(INSTALL_MANIFEST)
    payload = manifest["payload"]
    required = [
        "licenses/LICENSE", "licenses/THIRD_PARTY_LICENSES.md",
        "licenses/MODEL_LICENSES.md", "licenses/sbom.spdx.json",
    ]
    for name in required:
        if name not in payload:
            raise SystemExit(f"install manifest is missing {name}")
    for name in payload:
        if Path(name).suffix.lower() in WEIGHT_SUFFIXES:
            raise SystemExit(f"install manifest lists a weight: {name}")
    if "models" in payload:
        raise SystemExit("install manifest lists a models directory")
    log("install payload includes licenses/ and the SBOM, with no weight files")


def case_model_refs() -> None:
    manifest = load_json(MANIFEST)
    lock = load_json(LOCK)
    ids = {model["id"] for model in lock["models"]}
    for component in manifest["components"]:
        reference = component.get("weights")
        if not reference:
            continue
        anchor = reference.split("#", 1)[-1]
        if anchor not in ids:
            raise SystemExit(f"{component['name']}: {anchor} is not in models.lock.json")
    if lock.get("total_bytes", 0) <= 0:
        raise SystemExit("models.lock.json has no total_bytes")
    for model in lock["models"]:
        for item in model["files"]:
            if not item.get("sha256"):
                raise SystemExit(f"{model['id']}: {item['path']} has no sha256")
    log("every model reference resolves to a hashed models.lock.json entry")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True,
                        choices=["manifest", "sbom", "sbom_models", "copies",
                                 "no_weights", "model_refs"])
    arguments = parser.parse_args()
    handlers = {
        "manifest": case_manifest,
        "sbom": case_sbom,
        "sbom_models": case_sbom_models,
        "copies": case_copies,
        "no_weights": case_no_weights,
        "model_refs": case_model_refs,
    }
    handlers[arguments.case]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
