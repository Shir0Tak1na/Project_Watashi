#!/usr/bin/env python3
"""Verify the frozen 200-case golden corpus using only the stdlib.

    python golden/verify_cases.py
    python golden/verify_cases.py --selftest

Read-only: this never rewrites ``golden/cases.jsonl``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "golden" / "cases.jsonl"
META = ROOT / "golden" / "meta.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_CASE = ("id", "category", "git_commit", "config", "config_digest", "input", "expected")


# --------------------------------------------------------------------------- #
# selftest: four in-memory mutations, all of which must be rejected
# --------------------------------------------------------------------------- #


def _load(cases_path: Path = CASES, meta_path: Path = META) -> tuple[list[dict], dict]:
    cases = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return cases, meta


def _selftest() -> int:
    cases, meta = _load()
    mutations = []

    duplicate = copy.deepcopy(cases)
    duplicate[1]["id"] = duplicate[0]["id"]
    mutations.append(("duplicate id", duplicate, copy.deepcopy(meta)))

    wrong_count = copy.deepcopy(cases)
    wrong_meta = copy.deepcopy(meta)
    first = next(iter(wrong_meta["categories"]))
    wrong_meta["categories"][first] += 1
    mutations.append(("wrong category count", wrong_count, wrong_meta))

    wrong_digest = copy.deepcopy(cases)
    wrong_digest[0]["config_digest"] = "0" * 64
    mutations.append(("wrong config_digest", wrong_digest, copy.deepcopy(meta)))

    wrong_type = copy.deepcopy(cases)
    wrong_type[0]["expected"]["protected_terms"] = "zero"
    mutations.append(("wrong expected type", wrong_type, copy.deepcopy(meta)))

    failed = 0
    for name, mutated_cases, mutated_meta in mutations:
        errors = validate_cases(mutated_cases, mutated_meta)
        if errors:
            print(f"PASS rejected: {name} -> {errors[0]}")
        else:
            print(f"FAIL accepted: {name}")
            failed += 1
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- #
# implementation
# --------------------------------------------------------------------------- #


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_cases(cases: list[Any], meta: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if len(cases) != 200:
        errors.append(f"case count is {len(cases)}, expected 200")

    counts: dict[str, int] = {}
    seen_ids: set[str] = set()
    for index, case in enumerate(cases, 1):
        where = f"case {index}"
        if not isinstance(case, dict):
            errors.append(f"{where}: not an object")
            continue
        for key in REQUIRED_CASE:
            if key not in case:
                errors.append(f"{where}: missing {key}")
        if errors and any(error.startswith(f"{where}: missing") for error in errors):
            continue

        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{where}: id must be a non-empty string")
        else:
            where = f"{case_id}"
            if case_id in seen_ids:
                errors.append(f"{where}: duplicate id")
            seen_ids.add(case_id)

        category = case.get("category")
        if not isinstance(category, str) or not category:
            errors.append(f"{where}: category must be a non-empty string")
        else:
            counts[category] = counts.get(category, 0) + 1

        commit = case.get("git_commit")
        if not isinstance(commit, str) or not COMMIT_RE.match(commit):
            errors.append(f"{where}: git_commit must be a 40-char lowercase hex sha")

        config = case.get("config")
        digest = case.get("config_digest")
        if not isinstance(config, dict):
            errors.append(f"{where}: config must be an object")
        elif not isinstance(digest, str) or not DIGEST_RE.match(digest):
            errors.append(f"{where}: config_digest must be a 64-char lowercase hex sha")
        else:
            actual = hashlib.sha256(canonical(config).encode("utf-8")).hexdigest()
            if actual != digest:
                errors.append(f"{where}: config_digest does not match config")

        if not isinstance(case.get("input"), dict):
            errors.append(f"{where}: input must be an object")

        expected = case.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{where}: expected must be an object")
            continue
        target = expected.get("target")
        if not isinstance(target, str):
            errors.append(f"{where}: expected.target must be a string")
        backend = expected.get("backend")
        if not isinstance(backend, str) or not backend:
            errors.append(f"{where}: expected.backend must be a non-empty string")
        terms = expected.get("protected_terms")
        if not isinstance(terms, int) or isinstance(terms, bool) or terms < 0:
            errors.append(f"{where}: expected.protected_terms must be an int >= 0")
        reason = expected.get("fallback_reason")
        if reason is not None and not isinstance(reason, str):
            errors.append(f"{where}: expected.fallback_reason must be a string or null")
        used = expected.get("used_model")
        if not isinstance(used, bool):
            errors.append(f"{where}: expected.used_model must be a boolean")

    meta_count = meta.get("case_count")
    if meta_count != len(cases):
        errors.append(f"meta.case_count is {meta_count}, actual is {len(cases)}")
    meta_categories = meta.get("categories")
    if not isinstance(meta_categories, dict):
        errors.append("meta.categories must be an object")
    else:
        if meta_categories != dict(sorted(counts.items())):
            errors.append(
                f"category counts differ: meta={meta_categories} actual={counts}"
            )
        if sum(meta_categories.values()) != len(cases):
            errors.append("meta.categories do not sum to the case count")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="run the four rejection cases")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    cases, meta = _load()
    errors = validate_cases(cases, meta)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    print(f"OK {len(cases)} case(s)")
    for category in sorted(counts):
        print(f"  {category:20s} {counts[category]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
