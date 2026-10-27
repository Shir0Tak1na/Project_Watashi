#!/usr/bin/env python3
"""Record a repeatable performance baseline for the Python reference engine.

    python tools/bench_baseline.py --runs 3

Measures, on the fixed 1200x200 synthetic region:

* OCR P50/P95
* corpus+rules translation P50/P95
* NMT single-sentence P50/P95 and model load time

The harness reuses ``SyntheticCapturer``, ``RapidOcrEngine`` (via
``build_ocr``), ``CorpusStore`` and ``NmtModel``; it does not reimplement any
engine logic. There is no pass/fail threshold: machines vary. The second run
prints the delta against ``bench/baseline.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from watashi.config import AppConfig  # noqa: E402
from watashi.lang import to_nllb_code  # noqa: E402
from watashi.local_nmt import NmtModel  # noqa: E402
from watashi.session import build_corpus, build_ocr  # noqa: E402
from watashi.synth import DEMO_LINES, SyntheticCapturer  # noqa: E402

BASELINE = ROOT / "bench" / "baseline.json"
LOCK = ROOT / "models.lock.json"
REGION = (1200, 200)
SENTENCE = DEMO_LINES[0]


def git_commit() -> str:
    """Read .git/HEAD without a subprocess (sandboxes reject CreatePipe)."""
    git_dir = ROOT / ".git"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head
    ref = head.split(":", 1)[1].strip()
    ref_file = git_dir / ref
    if ref_file.exists():
        return ref_file.read_text(encoding="utf-8").strip()
    for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha.strip()
    raise SystemExit(f"cannot resolve {ref}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(np.ceil(percent / 100.0 * len(ordered))) - 1))
    return ordered[index]


def summary(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
    }


def machine() -> dict[str, Any]:
    import ctranslate2
    import onnxruntime

    cpu = (
        platform.processor()
        or os.environ.get("PROCESSOR_IDENTIFIER", "")
        or platform.machine()
    )
    windows = sys.getwindowsversion() if os.name == "nt" else None
    info = {
        "platform": platform.platform(),
        "windows_build": windows.build if windows else None,
        "windows_version": windows.major if windows else None,
        "hostname": socket.gethostname(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "onnxruntime": onnxruntime.__version__,
        "ctranslate2": ctranslate2.__version__,
    }
    # platform.platform() is not the fingerprint: on Windows it can report the
    # compatibility-layer build (26100) while the real build is 26300. The
    # fingerprint is built from the values that actually change a measurement.
    identity = {
        "windows_build": info["windows_build"],
        "hostname": info["hostname"],
        "cpu": info["cpu"],
        "onnxruntime": info["onnxruntime"],
        "ctranslate2": info["ctranslate2"],
    }
    info["fingerprint"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return info


def frame_1200x200() -> np.ndarray:
    """A real synthetic frame from the engine, padded to the fixed region height."""
    capturer = SyntheticCapturer(frames=[tuple(DEMO_LINES[:2])], width=REGION[0])
    frame = capturer.grab()
    capturer.close()
    height, width = frame.shape[:2]
    if width != REGION[0]:
        raise SystemExit(f"synthetic frame is {width}px wide, expected {REGION[0]}")
    if height < REGION[1]:
        pad = np.zeros((REGION[1] - height, width, 3), dtype=frame.dtype)
        frame = np.vstack([frame, pad])
    return np.ascontiguousarray(frame[: REGION[1], : REGION[0]])


def print_delta(previous: dict[str, Any], current: dict[str, Any]) -> None:
    old_machine = previous.get("machine") or {}
    new_machine = current.get("machine") or {}
    old_fp = old_machine.get("fingerprint")
    new_fp = new_machine.get("fingerprint")
    if not old_fp or old_fp != new_fp:
        print(
            "not comparable: machine fingerprint "
            f"{str(old_fp)[:12] or '<missing>'} != {str(new_fp)[:12]}"
        )
        return
    print("delta vs previous baseline (same machine fingerprint):")
    old = previous.get("metrics") or {}
    new = current.get("metrics") or {}
    for name in (
        "ocr_ms",
        "corpus_translate_ms",
        "corpus_reload_scan_ms",
        "model_ms",
    ):
        for key in ("p50", "p95"):
            before = (old.get(name) or {}).get(key)
            after = (new.get(name) or {}).get(key)
            if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before:
                change = (after - before) / before * 100.0
                print(f"  {name}.{key:3s} {before:8.2f} -> {after:8.2f} ms  {change:+6.1f}%")
    before = old.get("model_load_ms")
    after = new.get("model_load_ms")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        print(f"  model_load_ms {before:8.2f} -> {after:8.2f} ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        default=20,
        help="samples per metric (default 20; use at least 20 for a baseline)",
    )
    args = parser.parse_args(argv)
    runs = max(1, int(args.runs))

    config = AppConfig.load()
    frame = frame_1200x200()

    ocr = build_ocr(config)
    ocr.ensure_loaded()
    ocr.recognize(frame)  # warm-up: shape/kernel init is not steady state

    corpus = build_corpus(config)
    # The benchmark must measure lookup, not the periodic mtime scan. Leaving hot
    # reload on makes the same commit swing by 10-50x depending on whether a call
    # happens to cross reload_interval_s (0.5 s). The scan is measured separately.
    corpus.auto_reload = False
    corpus.translate(SENTENCE, "zh-CN")  # warm-up: first load
    corpus.reload_if_changed(force=True)  # warm-up: stat scan

    model_path = config.translation.get("nmt_model")
    if not model_path:
        raise SystemExit("translation.nmt_model is not configured")
    model_dir = Path(str(model_path))
    if not model_dir.is_absolute():
        model_dir = config.base_dir / model_dir
    model = NmtModel(
        model_dir,
        compute_type=str(config.translation.get("nmt_compute_type", "int8")),
        beam_size=int(config.translation.get("nmt_beam_size", 1)),
        intra_threads=int(config.translation.get("nmt_intra_threads", 4)),
    )
    if not model.load():
        raise SystemExit(f"model failed to load: {model.load_error}")
    target = to_nllb_code(config.target_lang)
    model.translate_one(SENTENCE, target)  # warm-up

    ocr_ms: list[float] = []
    corpus_ms: list[float] = []
    reload_ms: list[float] = []
    model_ms: list[float] = []

    for _ in range(runs):
        started = time.perf_counter()
        ocr.recognize(frame)
        ocr_ms.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        corpus.translate(SENTENCE, "zh-CN")
        corpus_ms.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        corpus.reload_if_changed(force=True)
        reload_ms.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        model.translate_one(SENTENCE, target)
        model_ms.append((time.perf_counter() - started) * 1000.0)

    baseline = {
        "schema_version": 1,
        "recorded": date.today().isoformat(),
        "git_commit": git_commit(),
        "models_lock_sha256": sha256_file(LOCK),
        "config_digest": hashlib.sha256(
            (ROOT / "prototype" / "config.yaml").read_bytes()
        ).hexdigest(),
        "machine": machine(),
        "region": {"width": REGION[0], "height": REGION[1]},
        "runs": runs,
        "metrics": {
            "ocr_ms": {**summary(ocr_ms), "samples": [round(v, 2) for v in ocr_ms]},
            "corpus_translate_ms": {
                **summary(corpus_ms),
                "samples": [round(v, 2) for v in corpus_ms],
            },
            "corpus_reload_scan_ms": {
                **summary(reload_ms),
                "samples": [round(v, 2) for v in reload_ms],
            },
            "model_ms": {**summary(model_ms), "samples": [round(v, 2) for v in model_ms]},
            "model_load_ms": round(model.load_ms, 2),
        },
    }

    previous = None
    if BASELINE.exists():
        try:
            previous = json.loads(BASELINE.read_text(encoding="utf-8"))
        except Exception:
            previous = None

    print("performance baseline")
    print(f"  region    : {REGION[0]}x{REGION[1]}")
    print(f"  runs      : {runs}")
    print(f"  machine   : {baseline['machine']['platform']} "
          f"(build {baseline['machine']['windows_build']})")
    print(f"  host      : {baseline['machine']['hostname']}")
    print(f"  CPU       : {baseline['machine']['cpu']}")
    print(f"  fingerprint: {baseline['machine']['fingerprint'][:16]}...")
    print(f"  OCR       : p50 {baseline['metrics']['ocr_ms']['p50']:.2f} ms, "
          f"p95 {baseline['metrics']['ocr_ms']['p95']:.2f} ms")
    print(f"  corpus    : p50 {baseline['metrics']['corpus_translate_ms']['p50']:.2f} ms, "
          f"p95 {baseline['metrics']['corpus_translate_ms']['p95']:.2f} ms")
    print(f"  reload scan: p50 {baseline['metrics']['corpus_reload_scan_ms']['p50']:.2f} ms, "
          f"p95 {baseline['metrics']['corpus_reload_scan_ms']['p95']:.2f} ms")
    print(f"  model     : p50 {baseline['metrics']['model_ms']['p50']:.2f} ms, "
          f"p95 {baseline['metrics']['model_ms']['p95']:.2f} ms")
    print(f"  load      : {baseline['metrics']['model_load_ms']:.2f} ms")
    ocr_p50 = baseline["metrics"]["ocr_ms"]["p50"]
    if not 60.0 <= ocr_p50 <= 250.0:
        print(f"  WARNING   : OCR p50 {ocr_p50:.2f} ms is outside 60-250 ms; "
              "suspect machine load, do not tune the code")
    if previous is not None:
        print_delta(previous, baseline)

    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {BASELINE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
