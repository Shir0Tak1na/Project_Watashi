"""Per-user install/uninstall checks for the PowerShell installer.

Run by CTest, one --case per test. It stages the CMake install tree, then runs
the real install.ps1/uninstall.ps1 against a temporary prefix, a test-only
uninstall key and a temporary Start-menu directory. No administrator rights,
no real uninstall entry and no model download are involved.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import winreg
from pathlib import Path

POWERSHELL = "powershell.exe"


def log(message: str) -> None:
    print(message, flush=True)


def run(command: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    log("+ " + " ".join(str(part) for part in command))
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    return result


def stage(build: Path, config: str, root: Path) -> Path:
    destination = root / "stage"
    command = ["cmake", "--install", str(build)]
    if config:
        command += ["--config", config]
    # Only the runtime component: third-party FetchContent install rules
    # (sentencepiece headers) must not enter the package.
    command += ["--component", "watashi", "--prefix", str(destination)]
    result = run(command)
    if result.returncode != 0:
        raise SystemExit("cmake --install failed")
    return destination


def manifest_payload(stage_dir: Path) -> list[str]:
    document = json.loads((stage_dir / "install_manifest.json").read_text(encoding="utf-8"))
    return list(document["payload"])


def registry_path(uninstall_key: str) -> str:
    prefix = "HKCU:\\"
    if not uninstall_key.startswith(prefix):
        raise SystemExit("test key must be under HKCU")
    return uninstall_key[len(prefix):]


def key_exists(path: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
            return True
    except OSError:
        return False


def remove_key(path: str) -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def identity_and_groups() -> str:
    """whoami plus whoami /groups, so a refusal names the token it hit."""
    text = []
    for command in (["whoami"], ["whoami", "/groups"]):
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        text.append(result.stdout or "")
    return "\n".join(text)


def integrity_level(groups_text: str) -> str:
    """Classify the token's mandatory label from `whoami /groups`.

    Low IL (S-1-16-4096) is a test-host property: such a token cannot create
    keys under its own HKCU, so the installer is not at fault. This is a pure
    function so --self-test can cover it without touching a registry.
    """
    text = groups_text.lower()
    if "s-1-16-4096" in text or "low mandatory level" in text:
        return "low"
    if "s-1-16-8192" in text or "medium mandatory level" in text:
        return "medium"
    if "s-1-16-12288" in text or "high mandatory level" in text:
        return "high"
    return "unknown"


def is_low_integrity(groups_text: str) -> bool:
    return integrity_level(groups_text) == "low"


def require_testable_integrity() -> str:
    """Refuse to blame the installer on a host where HKCU is unwritable.

    Runs before staging or install.ps1. A Low IL token (S-1-16-4096) is a
    host/session property, so the case is skipped with 77 and never reported
    as a pass.
    """
    identity = identity_and_groups()
    level = integrity_level(identity)
    if level == "low":
        log(identity)
        print("low-integrity host: HKCU is not writable for this test, "
              "skipping (77)", flush=True)
        raise SystemExit(77)
    return level


def require_hkcu_write() -> None:
    """Probe HKCU before the installer runs.

    The probe is kept even after the integrity check: it catches a real
    installer regression on Medium/High. A Low IL failure is a skip, a
    Medium/High failure is an installer bug, and an unclassifiable token is
    skipped rather than accused.
    """
    path = f"Software\\ProjectWatashi\\Probe_{uuid.uuid4().hex}"
    identity = identity_and_groups()
    level = integrity_level(identity)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_ALL_ACCESS) as handle:
            winreg.SetValueEx(handle, "probe", 0, winreg.REG_SZ, "1")
    except OSError as error:
        log(identity)
        print(f"HKCU probe failed: {error}", flush=True)
        if level == "low" or "S-1-5-12" in identity or "NT AUTHORITY\\RESTRICTED" in identity:
            print("low-integrity or restricted host: HKCU is not writable for "
                  "this test, skipping (77)", flush=True)
            raise SystemExit(77)
        if level == "unknown":
            print("integrity level unknown; not blaming the installer, "
                  "skipping (77)", flush=True)
            raise SystemExit(77)
        print(f"the token is {level} integrity, so this is an installer bug; "
              "install.ps1 must fix its HKCU path", flush=True)
        raise SystemExit(1)
    finally:
        remove_key(path)
        remove_key("Software\\ProjectWatashi")


def self_test() -> int:
    """Pure checks for Low/Medium/Unknown, so the skip logic is itself tested."""
    low = ("Mandatory Label\\Low Mandatory Level  Label  S-1-16-4096")
    medium = ("Mandatory Label\\Medium Mandatory Level  Label  S-1-16-8192")
    unknown = "User\\ryutaki\nBUILTIN\\Users"
    failures = []
    if not is_low_integrity(low):
        failures.append("Low Mandatory Level was not recognised as low")
    if is_low_integrity(medium):
        failures.append("Medium Mandatory Level was misread as low")
    if is_low_integrity(unknown):
        failures.append("an unlabelled token was misread as low")
    if integrity_level(medium) != "medium":
        failures.append("Medium Mandatory Level was not classified as medium")
    if integrity_level(unknown) != "unknown":
        failures.append("an unlabelled token was not classified as unknown")
    if failures:
        for failure in failures:
            print(f"SELFTEST FAIL: {failure}", flush=True)
        return 1
    print("SELFTEST OK: low, medium and unknown integrity classifications", flush=True)
    return 0


def install(stage_dir: Path, prefix: Path, key: str, shortcut: Path, extra: list[str] | None = None) -> None:
    command = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(stage_dir / "install.ps1"),
        "-Prefix", str(prefix),
        "-SourceDir", str(stage_dir),
        "-UninstallKey", key,
        "-ShortcutDir", str(shortcut),
    ]
    if extra:
        command += extra
    result = run(command)
    if result.returncode != 0:
        raise SystemExit("install.ps1 failed")


def uninstall(prefix: Path, key: str, shortcut: Path, extra: list[str] | None = None) -> None:
    command = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(prefix / "uninstall.ps1"),
        "-Prefix", str(prefix),
        "-UninstallKey", key,
        "-ShortcutDir", str(shortcut),
    ]
    if extra:
        command += extra
    result = run(command)
    if result.returncode != 0:
        raise SystemExit("uninstall.ps1 failed")


def case_manifest(stage_dir: Path) -> None:
    payload = manifest_payload(stage_dir)
    required = [
        "watashi_ui.exe", "watashi_cli.exe", "onnxruntime.dll",
        "ctranslate2.dll", "dnnl.dll", "LICENSE",
        "THIRD_PARTY_LICENSES.md", "config/presentation.json",
        "config/settings_ui.json",
    ]
    for name in required:
        if name not in payload:
            raise SystemExit(f"manifest is missing {name}")
    for relative in payload:
        if not (stage_dir / relative).exists():
            raise SystemExit(f"payload file is not staged: {relative}")
    if not (stage_dir / "install_manifest.json").exists():
        raise SystemExit("install_manifest.json is not staged")
    if not (stage_dir / "install.ps1").exists() or not (stage_dir / "uninstall.ps1").exists():
        raise SystemExit("installer scripts are not staged")
    # Weights must never ship. models.lock.json is a manifest, not a model.
    for path in stage_dir.rglob("*.bin"):
        raise SystemExit(f"model weight in the package: {path}")
    models = stage_dir / "models"
    if models.exists():
        raise SystemExit("a models directory was staged")
    if not (stage_dir / "watashi_fetch.exe").exists():
        raise SystemExit("the model fetcher is not staged")
    if not (stage_dir / "models.lock.json").exists():
        raise SystemExit("models.lock.json is not staged")
    installer = (stage_dir / "install.ps1").read_text(encoding="utf-8")
    if "LOCALAPPDATA" not in installer or "ProjectWatashi" not in installer:
        raise SystemExit("the default prefix is not the per-user LOCALAPPDATA path")
    if "RunAs" in installer or "Start-Process" in installer:
        raise SystemExit("the installer attempts elevation")
    log("manifest and payload are complete; no weights are staged; install is per-user")


def case_install(root: Path, stage_dir: Path) -> None:
    require_hkcu_write()
    prefix = root / "prefix"
    shortcut = root / "StartMenu"
    key = f"HKCU:\\Software\\ProjectWatashi\\TestUninstall_{uuid.uuid4().hex}"
    registry = registry_path(key)
    try:
        install(stage_dir, prefix, key, shortcut)
        for relative in manifest_payload(stage_dir):
            if not (prefix / relative).exists():
                raise SystemExit(f"installed file is missing: {relative}")
        if not (prefix / "user").is_dir():
            raise SystemExit("the user directory was not created")
        if not (prefix / "uninstall.ps1").exists():
            raise SystemExit("the uninstaller was not copied")
        if not (prefix / "installed_files.json").exists():
            raise SystemExit("the installed file list was not written")
        if not (shortcut / "Project Watashi.lnk").exists():
            raise SystemExit("the Start-menu shortcut was not created")

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry) as handle:
            display, _ = winreg.QueryValueEx(handle, "DisplayName")
            location, _ = winreg.QueryValueEx(handle, "InstallLocation")
            uninstall_string, _ = winreg.QueryValueEx(handle, "UninstallString")
        if display != "Project Watashi":
            raise SystemExit(f"unexpected DisplayName: {display}")
        if Path(location) != prefix.resolve():
            raise SystemExit(f"unexpected InstallLocation: {location}")
        if str(prefix) not in uninstall_string:
            raise SystemExit("UninstallString does not point at the prefix")

        user_file = prefix / "user" / "user.json"
        user_file.write_text('{"translation.target": "en"}', encoding="utf-8")

        uninstall(prefix, key, shortcut)
        for relative in manifest_payload(stage_dir):
            if (prefix / relative).exists():
                raise SystemExit(f"uninstall left a payload file: {relative}")
        if key_exists(registry):
            raise SystemExit("uninstall left the registry entry")
        if (shortcut / "Project Watashi.lnk").exists():
            raise SystemExit("uninstall left the shortcut")
        if not user_file.exists():
            raise SystemExit("uninstall removed user data by default")
        if not prefix.exists():
            raise SystemExit("the prefix holding user data was removed")
        log("install and uninstall are complete; user data is preserved")
    finally:
        remove_key(registry)
        parent = "Software\\ProjectWatashi"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent):
                pass
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)
        except OSError:
            pass


def case_remove_user_data(root: Path, stage_dir: Path) -> None:
    require_hkcu_write()
    prefix = root / "prefix"
    shortcut = root / "StartMenu"
    key = f"HKCU:\\Software\\ProjectWatashi\\TestUninstall_{uuid.uuid4().hex}"
    registry = registry_path(key)
    try:
        install(stage_dir, prefix, key, shortcut)
        (prefix / "user" / "corpora").mkdir(parents=True)
        (prefix / "user" / "corpora" / "terms.json").write_text("{}", encoding="utf-8")
        uninstall(prefix, key, shortcut, ["-RemoveUserData"])
        if (prefix / "user").exists():
            raise SystemExit("user data survived -RemoveUserData")
        if prefix.exists():
            raise SystemExit("the prefix survived -RemoveUserData")
        if key_exists(registry):
            raise SystemExit("uninstall left the registry entry")
        log("-RemoveUserData removes the prefix and its user data")
    finally:
        remove_key(registry)


def case_unsigned(root: Path) -> None:
    dummy = root / "watashi_ui.exe"
    dummy.write_bytes(b"not a real executable")
    environment = dict(os.environ)
    environment.pop("WATASHI_SIGN_CERT", None)
    environment.pop("WATASHI_SIGN_PASSWORD", None)
    script = Path(__file__).resolve().parent.parent / "installer" / "sign.ps1"
    result = run([
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(script), "-Path", str(dummy),
    ], env=environment)
    if result.returncode != 0:
        raise SystemExit("sign.ps1 without a certificate must skip, not fail")
    if "UNSIGNED" not in result.stdout:
        raise SystemExit("sign.ps1 did not state that the file is unsigned")
    result = run([
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(script), "-Path", str(dummy), "-RequireSignature",
    ], env=environment)
    if result.returncode != 2:
        raise SystemExit("-RequireSignature must fail when there is no certificate")
    log("unsigned mode is explicit and cannot pretend to be signed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case",
                        choices=["manifest", "install", "remove_user_data", "unsigned"])
    parser.add_argument("--build", type=Path, default=None)
    parser.add_argument("--config", default="")
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true",
                        help="pure integrity-classification checks; needs no build")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()
    if not arguments.case:
        parser.error("--case is required unless --self-test is given")
    if arguments.build is None:
        parser.error("--build is required unless --self-test is given")

    root = Path(tempfile.mkdtemp(prefix="watashi-installer-test-"))
    try:
        if arguments.case == "unsigned":
            case_unsigned(root)
        else:
            # Low IL hosts get 77 before staging: install.ps1 must not see a
            # token whose HKCU is unwritable.
            if arguments.case in ("install", "remove_user_data"):
                require_testable_integrity()
            stage_dir = stage(arguments.build, arguments.config, root)
            if arguments.case == "manifest":
                case_manifest(stage_dir)
            elif arguments.case == "install":
                case_install(root, stage_dir)
            elif arguments.case == "remove_user_data":
                case_remove_user_data(root, stage_dir)
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
