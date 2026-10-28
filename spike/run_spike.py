#!/usr/bin/env python3
"""Run the C++ OCR probe against the Python reference and record the result.

    .venv/Scripts/python.exe spike/run_spike.py --runs 20

Build first (once):

    cmake -S spike -B spike/build -G "Visual Studio 17 2022" -A x64 ^
          -DORT_ROOT=C:/Users/<you>/AppData/Local/Temp/ort-1.30.0/pkg
    cmake --build spike/build --config Release

This script does not reimplement OCR. It shells out to ``ocr_probe.exe``,
parses its stdout lines and stderr timings, and compares them with the
``RapidOcrEngine`` output that ``spike/make_frame.py`` wrote next to the frame.
The comparison gates are the W0.6a acceptance criteria:

* normalised text character agreement >= 0.95 (spaces and punctuation removed)
* same number of boxes, every coordinate within 3 px
* 20-run P50 inside +-20% of the 71-76 ms target band (56.8-91.2 ms)

The JSON it writes to ``bench/spike-ocr.json`` carries the same machine
fingerprint and ``models.lock`` digest as ``bench/baseline.json`` so the delta
against the Python baseline is only computed when both ran on one machine.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spike"
BENCH = ROOT / "bench" / "spike-ocr.json"
BASELINE = ROOT / "bench" / "baseline.json"
LOCK = ROOT / "models.lock.json"
EXE = SPIKE / "build" / "Release" / "ocr_probe.exe"
DET = (
    ROOT
    / ".venv"
    / "Lib"
    / "site-packages"
    / "rapidocr_onnxruntime"
    / "models"
    / "ch_PP-OCRv4_det_infer.onnx"
)
REC = (
    ROOT
    / ".venv"
    / "Lib"
    / "site-packages"
    / "rapidocr_onnxruntime"
    / "models"
    / "ch_PP-OCRv4_rec_infer.onnx"
)
FRAME = SPIKE / "frame.bgr"
CHARS = SPIKE / "chars.txt"
PYTHON_REF = SPIKE / "python_ocr.jsonl"

#: The 71-76 ms band W0.6a measured for this region. The probe P50 must fall
#: inside +-20% of it, i.e. 56.8-91.2 ms.
TARGET_BAND_MS = (71.0, 76.0)
TARGET_TOLERANCE = 0.20
MAX_BOX_ERROR_PX = 3


def load_bench():
    path = ROOT / "tools" / "bench_baseline.py"
    spec = importlib.util.spec_from_file_location("bench_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(math.ceil(percent / 100.0 * len(ordered))) - 1))
    return ordered[index]


def normalise(text: str) -> str:
    """Drop whitespace and punctuation, the W0.6a comparison basis."""
    kept = [
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    ]
    return "".join(kept)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    lines = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    return lines


def parse_probe(stdout: str, stderr: str) -> tuple[list[dict[str, Any]], list[float]]:
    lines = [json.loads(raw) for raw in stdout.splitlines() if raw.strip()]
    timings = [
        float(raw.split("=", 1)[1])
        for raw in stderr.splitlines()
        if raw.startswith("run_ms=")
    ]
    return lines, timings


def compare(cpp: list[dict[str, Any]], reference: list[dict[str, Any]]) -> dict[str, Any]:
    cpp = sorted(cpp, key=lambda line: (line["box"][1], line["box"][0]))
    reference = sorted(reference, key=lambda line: (line["box"][1], line["box"][0]))

    per_line: list[float] = []
    for left, right in zip(cpp, reference):
        a = normalise(left["text"])
        b = normalise(right["text"])
        ratio = 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()
        per_line.append(rounded(ratio, 4))

    joined_cpp = "\n".join(normalise(line["text"]) for line in cpp)
    joined_ref = "\n".join(normalise(line["text"]) for line in reference)
    overall = (
        1.0
        if joined_cpp == joined_ref
        else difflib.SequenceMatcher(None, joined_cpp, joined_ref).ratio()
    )

    box_errors: list[int] = []
    pairs = []
    for left, right in zip(cpp, reference):
        errors = [abs(int(round(a)) - int(b)) for a, b in zip(left["box"], right["box"])]
        box_errors.extend(errors)
        pairs.append(
            {
                "cpp": {"text": left["text"], "box": left["box"], "score": left["score"]},
                "python": {
                    "text": right["text"],
                    "box": right["box"],
                    "score": right["score"],
                },
                "normalised_agreement": rounded(
                    difflib.SequenceMatcher(
                        None, normalise(left["text"]), normalise(right["text"])
                    ).ratio(),
                    4,
                ),
                "box_error_px": errors,
            }
        )

    return {
        "cpp_lines": len(cpp),
        "python_lines": len(reference),
        "box_count_ok": len(cpp) == len(reference),
        "character_agreement": rounded(overall, 4),
        "per_line_agreement": per_line,
        "min_per_line_agreement": rounded(min(per_line) if per_line else 0.0, 4),
        "max_box_error_px": max(box_errors) if box_errors else 0,
        "pairs": pairs,
    }


def rounded(value: float, digits: int) -> float:
    return float(f"{value:.{digits}f}")


def run_probe(runs: int) -> tuple[list[dict[str, Any]], list[float], str]:
    if not EXE.exists():
        raise SystemExit(f"missing {EXE}; build spike first (see module docstring)")
    completed = subprocess.run(
        [
            str(EXE),
            str(FRAME),
            str(DET),
            str(REC),
            str(CHARS),
            "--runs",
            str(runs),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"ocr_probe exited {completed.returncode}:\n{completed.stderr}")
    lines, timings = parse_probe(completed.stdout, completed.stderr)
    if len(timings) != runs:
        raise SystemExit(f"expected {runs} timings, got {len(timings)}")
    return lines, timings, completed.stdout


def baseline_delta(machine: dict[str, Any], p50: float) -> dict[str, Any]:
    if not BASELINE.exists():
        return {"available": False, "reason": "bench/baseline.json missing"}
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    old = (baseline.get("machine") or {}).get("fingerprint")
    if not old or old != machine.get("fingerprint"):
        return {
            "available": False,
            "reason": (
                "not comparable: machine fingerprint "
                f"{str(old)[:12] or '<missing>'} != {str(machine.get('fingerprint'))[:12]}"
            ),
        }
    old_p50 = float((baseline.get("metrics") or {}).get("ocr_ms", {}).get("p50", 0.0))
    if old_p50 <= 0:
        return {"available": False, "reason": "baseline has no ocr_ms p50"}
    return {
        "available": True,
        "python_ocr_p50_ms": old_p50,
        "delta_ms": round(p50 - old_p50, 2),
        "delta_pct": round((p50 - old_p50) / old_p50 * 100.0, 2),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args(argv)
    runs = max(1, int(args.runs))

    bench = load_bench()
    machine = bench.machine()

    lines, timings, stdout = run_probe(runs)
    reference = read_jsonl(PYTHON_REF)
    comparison = compare(lines, reference)

    p50 = percentile(timings, 50)
    p95 = percentile(timings, 95)
    low = TARGET_BAND_MS[0] * (1.0 - TARGET_TOLERANCE)
    high = TARGET_BAND_MS[1] * (1.0 + TARGET_TOLERANCE)

    checks = {
        "text_agreement": comparison["character_agreement"] >= 0.95
        and comparison["min_per_line_agreement"] >= 0.95,
        "box_count": comparison["box_count_ok"],
        "box_error": comparison["max_box_error_px"] <= MAX_BOX_ERROR_PX,
        "p50_band": low <= p50 <= high,
    }

    record = {
        "schema_version": 1,
        "git_commit": git_commit(),
        "region": {"width": 1200, "height": 200},
        "runs": runs,
        "machine": machine,
        "models_lock_sha256": sha256_file(LOCK),
        "model_files": {
            "det": {"name": DET.name, "sha256": sha256_file(DET)},
            "rec": {"name": REC.name, "sha256": sha256_file(REC)},
        },
        "cpp_probe": {
            "exe": str(EXE.relative_to(ROOT)),
            "stdout": [json.loads(raw) for raw in stdout.splitlines() if raw.strip()],
            "samples_ms": [round(value, 2) for value in timings],
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
        },
        "python_reference": {
            "source": str(PYTHON_REF.relative_to(ROOT)),
            "lines": reference,
        },
        "comparison": comparison,
        "target": {
            "band_ms": [round(low, 2), round(high, 2)],
            "tolerance": TARGET_TOLERANCE,
            "max_box_error_px": MAX_BOX_ERROR_PX,
            "min_text_agreement": 0.95,
        },
        "baseline_delta": baseline_delta(machine, p50),
        "checks": checks,
        "ok": all(checks.values()),
        "recorded": bench_date(),
    }

    BENCH.parent.mkdir(parents=True, exist_ok=True)
    BENCH.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(record["comparison"]["pairs"], ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "band_ms": record["target"]["band_ms"],
                "checks": checks,
                "baseline_delta": record["baseline_delta"],
                "ok": record["ok"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if record["ok"] else 1


def bench_date() -> str:
    import datetime

    return datetime.date.today().isoformat()


def git_commit() -> str:
    git_dir = ROOT / ".git"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        return (git_dir / ref).read_text(encoding="utf-8").strip()
    return head


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
