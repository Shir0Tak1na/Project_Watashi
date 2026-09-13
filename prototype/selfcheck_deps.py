#!/usr/bin/env python3
"""Verify that CI can actually install what the checks need.

The failure this guards against is the one every "it works on my machine" story is
made of, and it was live in this repository: ``watashi/web.py`` imports ``fastapi``
when the module is loaded, ``selfcheck_web`` imports that module, ``selfcheck_web`` is
in ``checks.txt`` and therefore runs in CI -- and ``fastapi`` was **not** in
``requirements.txt``. The local virtual environment had it because someone installed it
by hand. Every check passed locally, and the first CI run would have failed on both
Linux and Windows at the same line.

So: for every check listed in ``checks.txt``, walk the real import closure, collect the
top-level third-party module names, and require each one to be either declared in
``requirements.txt`` or listed in ``OPTIONAL`` below with a reason. An undeclared
package is a check that cannot start in a clean environment.

Two controls keep this from passing for the wrong reason:

* the **vacuity guard** asserts that known dependencies really are discovered. An
  import walker that silently stopped traversing, or a module map that matched nothing,
  would report "all clear" forever.
* the **negative control** asserts that a deliberately optional import
  (``llama_cpp``) is found *and* exempted, so the exemption path is exercised rather
  than accidentally dead.

    run.cmd selfcheck_deps --summary
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REQUIREMENTS = HERE / "requirements.txt"
HEADLESS_LIST = HERE / "checks.txt"

#: import name -> the distribution that provides it. Anything not here is assumed to
#: be its own distribution name, which is true for most modern packages.
MODULE_TO_DIST = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "numpy": "numpy",
    "mss": "mss",
    "onnxruntime": "onnxruntime",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "ctranslate2": "ctranslate2",
    "sentencepiece": "sentencepiece",
    "tokenizers": "tokenizers",
    "requests": "requests",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "starlette": "starlette",
    "pydantic": "pydantic",
    "anyio": "anyio",
    "httpx": "httpx",
    "Xlib": "python-xlib",
    "llama_cpp": "llama-cpp-python",
    "shapely": "shapely",
    "pyclipper": "pyclipper",
    "six": "six",
}

#: Packages a check touches but that a clean install deliberately does not provide.
#: Each needs a reason, because "it works without it" is exactly the claim that turned
#: out to be false for fastapi. Presence here is a statement about *this* set of
#: checks, not about the application: the LLM backend is a real feature, it is simply
#: not exercised by anything in checks.txt, and requiring a multi-gigabyte GGUF file to
#: be installed to run a text-matching assertion would be its own kind of wrong.
OPTIONAL: dict[str, str] = {
    "llama_cpp": (
        "the optional local LLM backend (translate.LLMTranslator). No check in "
        "checks.txt constructs it; its import is guarded and reports the missing "
        "backend rather than raising. Install llama-cpp-python to use it."
    ),
}

#: A check that is in checks.txt but needs a session is a configuration error
#: elsewhere; this check only cares about packages, so display checks are not walked.
WINDOWS_ONLY_MODULES = {"winreg", "msvcrt", "_winapi"}


def imports_unguarded_or_not(path: Path) -> set[str]:
    """Every third-party module name a file imports, at any nesting level.

    Guarding is recorded but not used to exclude: a `try: import uvicorn` still means
    the code needs uvicorn to do its job, it just fails politely without it. Excluding
    guarded imports is how `uvicorn` was missed -- the module imported fine, so the
    audit was happy, and `selfcheck_web` failed when it tried to serve.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module == "watashi":
                for alias in node.names:
                    found.add(f"watashi.{alias.name}")
            elif node.module:
                found.add(node.module)
    return found


def declared_distributions() -> dict[str, str]:
    """What requirements.txt asks for, as {normalised name: the line}."""
    out: dict[str, str] = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        name = line.split(";")[0]
        for sep in (">=", "==", "<=", "~=", ">", "<", "!="):
            name = name.split(sep)[0]
        out[name.strip().lower().replace("_", "-")] = raw.strip()
    return out


def closure(script: Path) -> set[str]:
    """The third-party modules reachable from a check, following watashi.* modules."""
    todo = imports_unguarded_or_not(script)
    walked: set[str] = set()
    third: set[str] = set()
    while todo:
        module = todo.pop()
        if module in walked:
            continue
        walked.add(module)
        top = module.split(".")[0]
        if top == "watashi":
            if module == "watashi":
                # `from watashi import x` is already expanded by the caller
                todo |= {
                    f"watashi.{p.stem}"
                    for p in (HERE / "watashi").glob("*.py")
                    if p.stem != "__init__"
                }
                continue
            target = HERE / "watashi" / f"{module.split('.')[1]}.py"
            if target.exists():
                todo |= imports_unguarded_or_not(target)
            continue
        if top in sys.stdlib_module_names or top in WINDOWS_ONLY_MODULES:
            continue
        third.add(top)
    return third


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Dependency self check: can a clean environment run the checks?")
    print("=" * 78)

    declared = declared_distributions()
    checks = [
        line.split("#")[0].strip()
        for line in HEADLESS_LIST.read_text(encoding="utf-8").splitlines()
        if line.split("#")[0].strip()
    ]

    check.section("requirements.txt is readable and declares the core stack")
    check.check(
        "requirements.txt exists and declares packages",
        len(declared) >= 10,
        f"{len(declared)}: {', '.join(sorted(declared))}",
    )
    for expected in ("numpy", "opencv-python", "pyyaml", "rapidocr-onnxruntime"):
        check.check(
            f"the core stack declares {expected}",
            expected in declared,
            declared.get(expected, "MISSING"),
        )

    # ---------------------------------------------------------------- #
    check.section("every check's imports are installable")

    needs: dict[str, set[str]] = {}
    for name in checks:
        script = HERE / f"{name}.py"
        if not script.exists():
            needs[name] = set()
            continue
        needs[name] = closure(script)

    missing: dict[str, set[str]] = {}
    for name, modules in needs.items():
        gaps = {
            module
            for module in modules
            if MODULE_TO_DIST.get(module, module).lower() not in declared
            and module not in OPTIONAL
        }
        if gaps:
            missing[name] = gaps
        print(
            f"       {name:24s} {', '.join(sorted(modules)) or '(no third-party imports)'}"
        )

    check.check(
        "no check needs a package that requirements.txt does not install",
        not missing,
        "; ".join(
            f"{name} needs {', '.join(sorted(MODULE_TO_DIST.get(m, m) for m in gaps))}"
            for name, gaps in sorted(missing.items())
        )
        or f"{len(checks)} checks, all satisfied",
    )

    # ---------------------------------------------------------------- #
    check.section("the walker really walks (or every line above is meaningless)")

    check.check(
        "the web check is found to need fastapi",
        "fastapi" in needs.get("selfcheck_web", set()),
        f"got {sorted(needs.get('selfcheck_web', set()))}",
    )
    check.check(
        "and uvicorn, which it needs at run time rather than at import time",
        "uvicorn" in needs.get("selfcheck_web", set()),
        "the guarded import still has to be installed for the server to start",
    )
    check.check(
        "the OCR check is found to need the OCR stack",
        {"rapidocr_onnxruntime", "onnxruntime", "cv2", "PIL"} <= needs.get("selfcheck_ocr", set()),
        f"got {sorted(needs.get('selfcheck_ocr', set()))}",
    )
    check.check(
        "the corpus check reaches the engine's YAML config reader",
        "yaml" in needs.get("selfcheck_corpus", set()),
        f"got {sorted(needs.get('selfcheck_corpus', set()))}",
    )
    check.check(
        "a check that needs nothing third-party still reports nothing",
        needs.get("selfcheck_ci") == set(),
        f"got {sorted(needs.get('selfcheck_ci', set()))}",
    )

    # ---------------------------------------------------------------- #
    check.section("the exemption path is exercised, not dead")

    found_optional = {
        module for modules in needs.values() for module in modules if module in OPTIONAL
    }
    check.check(
        "an intentionally optional dependency is still discovered",
        "llama_cpp" in found_optional,
        f"found {sorted(found_optional)}",
    )
    check.check(
        "and it is exempt because it is listed, with a reason",
        all(OPTIONAL[module].strip() for module in OPTIONAL),
        "; ".join(f"{m}: {r[:40]}..." for m, r in OPTIONAL.items()),
    )
    check.check(
        "the exemption is not hiding something the checks actually run",
        not (found_optional - set(OPTIONAL)) and "fastapi" not in OPTIONAL,
        "a package a check exercises must be installed, not exempted",
    )

    # ---------------------------------------------------------------- #
    check.section("the CI workflow installs this file, and reads the check list")

    workflow = (REPO / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
    check.check(
        "the workflow installs requirements.txt",
        "pip install -r prototype/requirements.txt" in workflow,
        "otherwise the checks run against whatever the runner happens to have",
    )
    check.check(
        "and runs the checks listed in checks.txt rather than a list of its own",
        "prototype/checks.txt" in workflow and "while IFS= read -r line" in workflow,
        "a second hardcoded list would drift from the first",
    )
    check.check(
        "Linux installs the system libraries that cv2 needs",
        "libgl1" in workflow,
        "opencv-python imports libGL; a headless runner does not have it by default",
    )

    return check.report()


if __name__ == "__main__":
    sys.exit(main())
