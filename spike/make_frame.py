#!/usr/bin/env python3
"""Export the exact 1200x200 benchmark frame plus the Python OCR reference.

    .venv/Scripts/python.exe spike/make_frame.py

Writes under spike/:
    frame.bgr          raw BGR bytes, 1200*200*3
    chars.txt          rec model character list, one UTF-8 char per line
    python_ocr.jsonl   RapidOcrEngine lines: {text, box, score}
    frame.meta.json    size, sha256, git commit, line count
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spike"


def load_bench():
    path = ROOT / "tools" / "bench_baseline.py"
    spec = importlib.util.spec_from_file_location("bench_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def git_commit() -> str:
    git_dir = ROOT / ".git"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        return (git_dir / ref).read_text(encoding="utf-8").strip()
    return head


def main() -> int:
    sys.path.insert(0, str(ROOT / "prototype"))
    from watashi.config import AppConfig
    from watashi.session import build_ocr

    SPIKE.mkdir(parents=True, exist_ok=True)
    bench = load_bench()
    frame = bench.frame_1200x200()
    height, width, channels = frame.shape
    if (width, height, channels) != (1200, 200, 3):
        raise SystemExit(f"unexpected frame shape {frame.shape}")

    (SPIKE / "frame.bgr").write_bytes(np.ascontiguousarray(frame).tobytes())

    import onnxruntime as ort

    rec_path = (
        ROOT
        / ".venv"
        / "Lib"
        / "site-packages"
        / "rapidocr_onnxruntime"
        / "models"
        / "ch_PP-OCRv4_rec_infer.onnx"
    )
    session = ort.InferenceSession(str(rec_path), providers=["CPUExecutionProvider"])
    characters = session.get_modelmeta().custom_metadata_map["character"].splitlines()
    (SPIKE / "chars.txt").write_text("\n".join(characters) + "\n", encoding="utf-8")

    ocr = build_ocr(AppConfig.load())
    result = ocr.recognize(frame)
    lines = [
        {"text": line.text, "box": list(line.box), "score": round(line.confidence, 6)}
        for line in result.lines
    ]
    with (SPIKE / "python_ocr.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")

    meta = {
        "width": width,
        "height": height,
        "channels": channels,
        "frame_sha256": hashlib.sha256((SPIKE / "frame.bgr").read_bytes()).hexdigest(),
        "git_commit": git_commit(),
        "python_lines": len(lines),
        "ocr_ms": round(result.elapsed_ms, 2),
        "characters": len(characters),
    }
    (SPIKE / "frame.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote frame.bgr {width}x{height}, {len(lines)} Python line(s)")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
