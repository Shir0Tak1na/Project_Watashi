"""Shared check reporting for the self-check tools.

Previously each self check carried its own copy of this class. One shared
version also brings ``--summary``: CI and scripted verification want the totals
and the failures, not sixty lines of PASS.
"""

from __future__ import annotations

import sys
from typing import Any

PASS = "PASS"
FAIL = "FAIL"


def summary_mode() -> bool:
    """True when ``--summary`` appears anywhere on the command line."""
    return "--summary" in sys.argv


class Checker:
    """Counts checks, prints results, and collects failures.

    ``check`` returns the condition so callers can branch on it, which several
    of the self checks rely on.
    """

    def __init__(self, verbose: bool | None = None) -> None:
        self.failures: list[str] = []
        self.checks = 0
        self.verbose = (not summary_mode()) if verbose is None else verbose

    def check(self, label: str, condition: Any, detail: str = "") -> bool:
        passed = bool(condition)
        self.checks += 1
        if not passed:
            self.failures.append(label)
        if self.verbose or not passed:
            suffix = f"   {detail}" if detail else ""
            print(f"  [{PASS if passed else FAIL}] {label}{suffix}")
        return passed

    def section(self, title: str) -> None:
        if self.verbose:
            print("")
            print(f"-- {title} --")

    def report(self) -> int:
        """Print the summary and return a process exit code."""
        print("")
        print("=" * 78)
        passed = self.checks - len(self.failures)
        print(f"  {passed}/{self.checks} checks passed")
        if self.failures:
            print("  failures:")
            for name in self.failures:
                print(f"    - {name}")
        print("=" * 78)
        return 1 if self.failures else 0
