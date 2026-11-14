#!/usr/bin/env python3
"""Check that every release artefact agrees with version.txt.

version.txt is the single source of truth. CMake reads it before project(), so
the project version, the CPack archive name and the compiled --version stamp all
come from that file. The manifest, the licence manifest, the committed SBOM and
the docs are written by other tools, so this test is what keeps them from
drifting apart.

With --build it also runs the three binaries' --version and compares the printed
number, which is the only check that sees the compiled value rather than a file
next to it.

Exit codes: 0 all agree, 1 a mismatch, 77 the binaries were not built (ctest
shows that as skipped, never as a pass).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(message: str) -> None:
    print(f"version: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_version() -> str:
    text = (ROOT / "version.txt").read_text(encoding="utf-8").splitlines()
    if not text:
        fail("version.txt is empty")
    version = text[0].strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail(f"version.txt is not MAJOR.MINOR.PATCH: {version!r}")
    return version


def expect(condition: bool, what: str, value: object) -> None:
    if not condition:
        fail(f"{what}: {value!r}")


def check_source(version: str) -> None:
    """Every file that repeats the number must repeat this one."""
    tag = f"v{version}"
    archive = f"ProjectWatashi-{version}-win64.zip"

    cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    expect("include(cmake/Version.cmake)" in cmake,
           "CMakeLists.txt does not include cmake/Version.cmake", "missing")
    expect("project(ProjectWatashi VERSION ${WATASHI_VERSION}" in cmake,
           "CMakeLists.txt hard-codes the project version", "hard-coded")
    expect(not re.search(r"^set\(WATASHI_VERSION ", cmake, re.MULTILINE),
           "CMakeLists.txt still sets WATASHI_VERSION itself", "duplicate source")

    manifest = json.loads((ROOT / "config" / "install_manifest.json").read_text(encoding="utf-8"))
    expect(manifest.get("version") == version, "install_manifest version", manifest.get("version"))

    licence_manifest = json.loads(
        (ROOT / "tools" / "license_manifest.json").read_text(encoding="utf-8"))
    sbom = licence_manifest.get("sbom", {})
    expect(sbom.get("name") == f"ProjectWatashi-{version}", "licence manifest SBOM name",
           sbom.get("name"))
    expect((sbom.get("namespace") or "").endswith(f"/{version}"),
           "licence manifest SBOM namespace", sbom.get("namespace"))
    application = [c for c in licence_manifest.get("components", [])
                   if c.get("name") == "Project Watashi"]
    expect(len(application) == 1, "licence manifest application component count", len(application))
    expect(application[0].get("version") == version,
           "licence manifest application version", application[0].get("version"))

    spdx = json.loads((ROOT / "licenses" / "sbom.spdx.json").read_text(encoding="utf-8"))
    expect(spdx.get("name") == f"ProjectWatashi-{version}", "committed SBOM name", spdx.get("name"))
    expect((spdx.get("documentNamespace") or "").endswith(f"/{version}"),
           "committed SBOM namespace", spdx.get("documentNamespace"))
    packages = [p for p in spdx.get("packages", []) if p.get("name") == "Project Watashi"]
    expect(len(packages) == 1, "committed SBOM application package count", len(packages))
    expect(packages[0].get("versionInfo") == version,
           "committed SBOM application version", packages[0].get("versionInfo"))

    # Documentation tells a reader which archive and which tag to use, so the
    # two markers have to move with the release.
    for name in ("README.md", "README_zh.md", "docs/RELEASE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        expect(archive in text, f"{name} does not name {archive}", "missing")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    expect(heading is not None and heading.group(1) == version,
           "CHANGELOG.md first release heading", heading.group(1) if heading else "missing")
    for name in ("HANDOVER.md", "docs/ACCEPTANCE.md", "docs/POST-V1.0.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        expect(tag in text, f"{name} does not mention {tag}", "missing")

    # One packaging path: CPack, into the build tree. dist/ holds the published
    # archive for the released tag and only that version.
    expect(not (ROOT / "tools" / "package_rc.ps1").exists(),
           "tools/package_rc.ps1 still exists (second packaging path)", "present")
    packaging = (ROOT / "cmake" / "Packaging.cmake").read_text(encoding="utf-8")
    expect('CPACK_OUTPUT_FILE_PREFIX "${CMAKE_BINARY_DIR}/dist"' in packaging,
           "cmake/Packaging.cmake does not package into the build tree", "missing")
    published = sorted(p.name for p in (ROOT / "dist").glob("ProjectWatashi-*-win64.zip"))
    expect(published == [archive], f"dist/ holds {published}, expected only {archive}",
           published)

    print(f"version: sources agree on {version}")


def find_binary(build: Path, config: str, name: str) -> Path | None:
    for candidate in (build / config / name, build / name):
        if candidate.is_file():
            return candidate
    return None


def check_binaries(build: Path, config: str, version: str) -> None:
    missing = []
    for name in ("watashi_ui.exe", "watashi_cli.exe", "watashi_fetch.exe"):
        binary = find_binary(build, config, name)
        if binary is None:
            missing.append(name)
            continue
        output = subprocess.run([str(binary), "--version"], capture_output=True,
                                text=True, timeout=60).stdout
        match = re.search(r"Project Watashi (\d+\.\d+\.\d+)", output)
        expect(match is not None, f"{name} --version output", output.strip())
        expect(match.group(1) == version, f"{name} --version", match.group(1))
    if missing:
        print(f"version: SKIP binaries not built: {', '.join(missing)}; "
              "source artefacts still agreed", file=sys.stderr)
        raise SystemExit(77)
    print(f"version: ui/cli/fetch report {version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", type=Path, help="build directory with the binaries")
    parser.add_argument("--config", default="Release")
    args = parser.parse_args()

    version = read_version()
    check_source(version)
    if args.build is not None:
        check_binaries(args.build, args.config, version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
