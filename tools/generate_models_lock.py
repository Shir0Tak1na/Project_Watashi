#!/usr/bin/env python3
"""Build models.lock.json from the weights actually installed on this machine.

    .venv/Scripts/python.exe tools/generate_models_lock.py

Each file records path, size, SHA-256, the model's source, its licence and the
date it was acquired. The lock is checked by ``fetch_model --check``; it is not a
download manifest, so a missing or altered byte is reported rather than fetched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "models.lock.json"

MODELS = (
    {
        "id": "nllb-200-distilled-600M-ct2-int8",
        "kind": "translation",
        "path": "models/llm/nllb-200-distilled-600M-ct2-int8",
        "source": "https://huggingface.co/JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "license": "CC-BY-NC-4.0",
        "acquired": "2026-09-13",
        "files": (
            "config.json",
            "model.bin",
            "sentencepiece.bpe.model",
            "shared_vocabulary.txt",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
    },
    {
        "id": "whisper-small-ct2-int8",
        "kind": "speech",
        "path": "models/whisper/small",
        "source": "https://huggingface.co/Systran/faster-whisper-small",
        "license": "MIT",
        "acquired": "2026-10-28",
        "files": (
            "config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.txt",
        ),
    },
    {
        "id": "pp-ocrv4-onnx",
        "kind": "ocr",
        "package": "rapidocr_onnxruntime",
        "version": "1.4.4",
        "source": "PyPI: rapidocr-onnxruntime==1.4.4",
        "license": "Apache-2.0",
        "acquired": "2026-09-12",
        "files": (
            "models/ch_PP-OCRv4_det_infer.onnx",
            "models/ch_PP-OCRv4_rec_infer.onnx",
            "models/ch_ppocr_mobile_v2.0_cls_infer.onnx",
        ),
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_root(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"package {name!r} is not installed")
    return Path(next(iter(spec.submodule_search_locations)))


def main() -> int:
    models = []
    for spec in MODELS:
        base = (
            package_root(spec["package"])
            if spec.get("package")
            else ROOT / spec["path"]
        )
        files = []
        for relative in spec["files"]:
            path = base / relative
            if not path.exists():
                raise SystemExit(f"missing model file: {path}")
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
        entry = {
            key: value for key, value in spec.items() if key != "files"
        }
        entry["files"] = files
        models.append(entry)

    lock = {
        "lock_version": 1,
        "models": models,
        "total_bytes": sum(
            item["size"] for model in models for item in model["files"]
        ),
    }
    LOCK.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {LOCK.relative_to(ROOT)}")
    for model in models:
        total = sum(item["size"] for item in model["files"])
        print(f"  {model['id']:38s} {len(model['files'])} file(s)  {total} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
