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
SKIP = "SKIP"


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
        self.skips = 0
        self.skipped: list[str] = []
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

    def skip(self, label: str, detail: str = "") -> None:
        """Record something that could not be checked, without calling it a pass.

        Not a failure: a check whose precondition is missing (the screen is covered,
        the model is not downloaded) is neither evidence for nor against the code. Not
        silence either: a check that quietly passes when it verified nothing is how a
        green run starts meaning less than it appears to, which is the whole reason
        these files are counted rather than trusted.
        """
        self.skips += 1
        self.skipped.append(label)
        if self.verbose:
            suffix = f"   {detail}" if detail else ""
            print(f"  [{SKIP}] {label}{suffix}")

    def section(self, title: str) -> None:
        if self.verbose:
            print("")
            print(f"-- {title} --")

    def report(self) -> int:
        """Print the summary and return a process exit code."""
        print("")
        print("=" * 78)
        passed = self.checks - len(self.failures)
        suffix = f" ({self.skips} skipped)" if self.skips else ""
        print(f"  {passed}/{self.checks} checks passed{suffix}")
        if self.failures:
            print("  failures:")
            for name in self.failures:
                print(f"    - {name}")
        if self.skipped:
            # Printed even in --summary: a skip is the one outcome that must never be
            # mistaken for a pass, and CI logs are where that mistake would happen.
            print("  skipped:")
            for name in self.skipped:
                print(f"    - {name}")
        print("=" * 78)
        return 1 if self.failures else 0
