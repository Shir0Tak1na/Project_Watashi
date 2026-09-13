#!/usr/bin/env python3
"""Verify that the CI configuration actually covers every self check.

The failure this guards against is silent and common: someone adds a self check, it
passes locally, and it never runs in CI because nobody added it to the workflow. The
badge stays green and means less than it appears to.

So the CI list lives in a file (``checks.txt``) rather than being written into the
workflow a second time, and this asserts the two lists between them account for every
``selfcheck_*.py`` on disk -- no more, no less. It also checks that the workflow reads
that file, because a workflow with its own hardcoded list would drift the same way.

    run.cmd selfcheck_ci --summary
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
HEADLESS_LIST = HERE / "checks.txt"
DISPLAY_LIST = HERE / "checks_display.txt"
WORKFLOW = REPO / ".github" / "workflows" / "checks.yml"

#: Modules that pull in a GUI toolkit, directly or through the package.
GUI_MODULES = {"tkinter", "watashi.overlay", "watashi.desktop", "watashi.selector"}
#: The library that actually grabs the screen. Importing it means a session is needed.
#:
#: Note what is *not* here: ``watashi.capture``. That module holds both
#: ``RegionCapturer``, which needs a screen, and ``ChangeDetector``, which is pure
#: numpy frame differencing and needs nothing of the sort. Treating the whole module
#: as a screen dependency made ``selfcheck_settle`` look like it needed a display --
#: and excluding a genuinely headless check from CI loses coverage in exactly the
#: direction this check exists to protect.
SCREEN_APIS = {"mss"}


def imported_modules(path: Path) -> set[str]:
    """Every module imported by a file, read from the AST.

    Deliberately not a substring search on the source. The first version of this
    check looked for the text "tkinter" and "Overlay(" and promptly failed on
    *itself*, because those are the very strings it searches for. Names that appear
    in a string literal are not imports, and only the AST can tell the difference.
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
            if node.module:
                found.add(node.module)
    return found


def needed_session(path: Path) -> tuple[bool, str]:
    """Whether a script needs a desktop session, and the evidence for it."""
    modules = imported_modules(path)
    gui = sorted(m for m in modules if m.split(".")[0] == "tkinter" or m in GUI_MODULES)
    if gui:
        return True, "imports " + ", ".join(gui)
    screen = sorted(m for m in modules if m in SCREEN_APIS)
    if screen:
        return True, "captures the screen via " + ", ".join(screen)
    return False, "no GUI or screen-capture import"


def read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        names.append(entry)
    return names


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("CI coverage self check (no screen, no models, no network)")
    print("=" * 78)

    headless = read_list(HEADLESS_LIST)
    display = read_list(DISPLAY_LIST)
    on_disk = sorted(p.stem for p in HERE.glob("selfcheck_*.py"))

    check.section("the two lists partition the self checks on disk")
    check.check("checks.txt lists something", bool(headless), f"{len(headless)} entries")
    check.check(
        "checks_display.txt lists something",
        bool(display),
        f"{len(display)} entries",
    )

    both = sorted(set(headless) & set(display))
    check.check(
        "no check is in both lists",
        not both,
        f"in both: {both}" if both else "",
    )

    covered = set(headless) | set(display)
    missing = sorted(set(on_disk) - covered)
    check.check(
        "every selfcheck_*.py is in one of the lists, so none is silently skipped",
        not missing,
        f"missing from both lists: {missing}" if missing else f"{len(on_disk)} scripts",
    )

    ghost = sorted(covered - set(on_disk))
    check.check(
        "no list names a script that does not exist",
        not ghost,
        f"named but absent: {ghost}" if ghost else "",
    )

    check.section("the workflow reads the list instead of repeating it")
    if not WORKFLOW.exists():
        check.check("the workflow exists", False, str(WORKFLOW))
    else:
        text = WORKFLOW.read_text(encoding="utf-8")
        check.check("the workflow exists", True, str(WORKFLOW.relative_to(REPO)))
        check.check(
            "it consumes checks.txt rather than a second hardcoded list",
            "checks.txt" in text,
            "otherwise the workflow's list and this one drift apart",
        )
        check.check(
            "it runs on push and pull_request",
            "push" in text and "pull_request" in text,
        )
        check.check(
            "it installs the requirements before running anything",
            "requirements.txt" in text,
        )
        # Every headless check must be reachable. They are run from the list, so this
        # asserts the mechanism rather than enumerating names again.
        hardcoded = [
            name for name in on_disk if re.search(rf"selfcheck_{name}\.py", text)
        ]
        check.check(
            "and it does not hardcode individual check names",
            not hardcoded,
            f"hardcoded: {hardcoded}" if hardcoded else "runs them from the list",
        )
        check.check(
            "it does not try to run the display-only checks",
            not any(re.search(rf"selfcheck_{name}\.py", text) for name in display),
            "a hosted runner has no graphical session",
        )

    check.section("the lists match what the scripts actually need")
    reasons = {}
    for name in display:
        source = HERE / f"{name}.py"
        if source.exists():
            needs, why = needed_session(source)
            reasons[name] = (needs, why)
    check.check(
        "every display-only check really does need a desktop session",
        reasons and all(needs for needs, _ in reasons.values()),
        "; ".join(f"{n}: {w}" for n, (_, w) in reasons.items()),
    )

    check.section("no headless check needs a display")
    # This is the direction that actually breaks CI: a GUI or screen-capture import in
    # a script listed as headless makes the whole job fail on a runner with no screen.
    offenders = []
    evidence = []
    for name in headless:
        source = HERE / f"{name}.py"
        if not source.exists():
            continue
        needs, why = needed_session(source)
        evidence.append(f"{name}: {why}")
        if needs:
            offenders.append(f"{name} ({why})")
    check.check(
        "no headless check imports a toolkit or captures the screen",
        not offenders,
        "; ".join(offenders) if offenders else f"{len(headless)} checks inspected",
    )
    if not offenders:
        print("      " + "\n      ".join(evidence[:4]) + " ...")

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
