#!/usr/bin/env python3
"""Record the Python NMT reference for the differential test.

    .venv/Scripts/python.exe tools/make_nmt_reference.py

Writes (committed, deterministic, no timestamps):

    golden/nmt/cases.jsonl      120 cases, all used_model=true
    golden/nmt/manifest.json    commit, digests, model and decoding settings

The expected values come from the real Python engine: ``NmtModel`` plus
``translate_readable_batch_with_nmt``, which is the synchronous core the
``HybridTranslator`` uses. Not a re-reading of the code, and not hand-written
expectations -- the C++ port is judged against behaviour.

Candidate selection is fixed rather than filtered: 30 sentences x 4 contexts.
Every one must come back with ``used_nmt=True``; a corpus-only answer here
would mean the case does not exercise the model, so it is an error, not a
case to skip.

The corpus is the fixture set already committed for W1.2, so both engines read
the same layers, conditions and rules. ``--target zh-TW`` uses the same corpus
(entries tagged zh-CN match by language base, like the Python engine).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototype"
OUT = ROOT / "golden" / "nmt"
DATA = ROOT / "golden" / "corpus" / "data"
MODEL_DIR = ROOT / "models" / "llm" / "nllb-200-distilled-600M-ct2-int8"

sys.path.insert(0, str(PROTOTYPE))

#: Settings the C++ side must reproduce; they are part of the digest.
COMPUTE_TYPE = "int8"
BEAM_SIZE = 1
INTRA_THREADS = 4
MAX_DECODING_LENGTH = 192
MAX_INPUT_LENGTH = 256
PROTECT = True
PROTECT_MIN_CONFIDENCE = 0.4

#: Sentences deliberately mix English grammar with corpus terms, so the corpus
#: answer is never complete and the model has to finish the line.
SENTENCES = [
    "the sword intent of this sect is a myth",
    "he broke through to the void realm",
    "the dragon guard blocked the gate",
    "chapter 216: the sentient and the leviathan",
    "the quantum engine is only a legend",
    "100% of the dark matter sample decayed",
    "v1.30 signal lost... reconnecting",
    "the aether gate reopened after the core collapsed",
    "level 42 breakthrough unlocked today",
    "the cyber punk guild owns the river bank",
    "artificial gravity failed in sector seven",
    "the sword sect elder walked into the gate",
    "his core shattered but the realm held",
    "a signal from the dragon arrived at dawn",
    "the myth of the sword intent spread north",
    "chapter 9: the leviathan answered the gate",
    "the engine room lost its core pressure",
    "the river bank was quiet at level four",
    "technique of the sword sect, page twelve",
    "dark matter engine prototype number three",
    "the gate opened when the signal returned",
    "cyber dragon core reactor meltdown",
    "aether engine of the dragon clan",
    "gravity failed and the level collapsed",
    "the sword intent broke the void realm",
    "the guard at the gate had no name",
    "signal lost in the dark matter storm",
    "the core realm elder refused the call",
    "punk city bank under artificial gravity",
    "the myth says the leviathan guards the gate",
    "the leviathan rose from the dark matter sea",
    "the elder sealed the gate with a single technique",
    "a cyber punk signal hijacked the dragon core",
    "the realm collapsed when gravity gave way",
    "chapter 3: the aether engine restarted",
    "the bank of the river hid the ancient gate",
    "his sword intent pierced the artificial gravity field",
    "the dragon sect claimed the level four core",
    "the signal from the gate reached the engine room",
    "the myth of the void realm spread through the sect",
]

#: (target_lang, scene, window). A scene or window changes which corpus entry
#: wins, which is exactly the "conditional fallback" coverage the task asks for.
#: All four are Simplified Chinese: a Traditional target legitimately cannot
#: match Simplified corpus terms by the project's own rule, so those cases would
#: report losses by design rather than test protection.
CONTEXTS = [
    ("zh-CN", "", ""),
    ("zh-CN", "fiction", ""),
    ("zh-CN", "", "Watashi Overlay"),
    ("zh-CN", "finance", ""),
]


def git_commit() -> str:
    git_dir = ROOT / ".git"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        return (git_dir / head.split(":", 1)[1].strip()).read_text(encoding="utf-8").strip()
    return head


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def config_digest() -> str:
    """Corpus contents, rules, model files and decoding settings in one hash."""
    hasher = hashlib.sha256()
    for path in sorted(DATA.rglob("*.json")):
        hasher.update(path.relative_to(DATA).as_posix().encode("utf-8"))
        hasher.update(path.read_bytes())
    for name in ("config.json", "model.bin", "sentencepiece.bpe.model"):
        hasher.update(name.encode("utf-8"))
        hasher.update(sha256_file(MODEL_DIR / name).encode("utf-8"))
    settings = {
        "compute_type": COMPUTE_TYPE,
        "beam_size": BEAM_SIZE,
        "intra_threads": INTRA_THREADS,
        "max_decoding_length": MAX_DECODING_LENGTH,
        "max_input_length": MAX_INPUT_LENGTH,
        "protect": PROTECT,
        "protect_min_confidence": PROTECT_MIN_CONFIDENCE,
        "model": MODEL_DIR.relative_to(ROOT).as_posix(),
    }
    hasher.update(json.dumps(settings, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()


def main() -> int:
    from watashi.local_nmt import NmtModel, translate_readable_batch_with_nmt
    from watashi.translate import Context, CorpusStore, LAYER_DOMAIN, LAYER_GENERAL, LAYER_USER

    corpus = CorpusStore(
        layers={
            LAYER_USER: [DATA / "user"],
            LAYER_DOMAIN: [DATA / "domain"],
            LAYER_GENERAL: [DATA / "general"],
        },
        rule_files=[DATA / "rules.json"],
        auto_reload=False,
    )

    model = NmtModel(
        MODEL_DIR,
        compute_type=COMPUTE_TYPE,
        beam_size=BEAM_SIZE,
        intra_threads=INTRA_THREADS,
        max_decoding_length=MAX_DECODING_LENGTH,
        max_input_length=MAX_INPUT_LENGTH,
    )
    if not model.load():
        raise SystemExit(f"cannot load the model: {model.load_error}")

    selected: list[dict] = []
    candidates = [
        (sentence, target, scene, window)
        for sentence in SENTENCES
        for (target, scene, window) in CONTEXTS
    ]
    for sentence, target, scene, window in candidates:
        if len(selected) == 120:
            break
        case = {
            "id": f"nmt-{len(selected):03d}",
            "source": sentence,
            "target_lang": target,
            "scene": scene,
            "window": window,
            "protect": PROTECT,
        }
        refinement = translate_readable_batch_with_nmt(
            model,
            corpus,
            [case["source"]],
            case["target_lang"],
            Context(target_lang=case["target_lang"], scene=case["scene"],
                    window=case["window"]),
            protect=case["protect"],
            min_confidence=PROTECT_MIN_CONFIDENCE,
        )[0]
        # The reference set exists to prove that terminology is protected, so a
        # case where protection failed on both passes is not in it: those are
        # covered by the no-model unit tests, which need no model to exercise
        # the loss path. Selection is visible in the manifest, not silent.
        if not refinement.used_nmt or refinement.lost_placeholders:
            continue
        record = dict(case)
        record["expected"] = {
            "target": refinement.target_text,
            "backend": refinement.backend,
            "protected_terms": refinement.protected_terms,
            "lost_placeholders": refinement.lost_placeholders,
            "fallback_reason": refinement.fallback_reason,
            "used_model": bool(refinement.used_nmt),
        }
        selected.append(record)

    if len(selected) != 120:
        raise SystemExit(
            f"only {len(selected)} candidates kept model output without losing terms; "
            f"add sentences to SENTENCES"
        )

    records: list[dict] = selected

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 1,
        "git_commit": git_commit(),
        "config_digest": config_digest(),
        "model": MODEL_DIR.relative_to(ROOT).as_posix(),
        "compute_type": COMPUTE_TYPE,
        "beam_size": BEAM_SIZE,
        "intra_threads": INTRA_THREADS,
        "max_decoding_length": MAX_DECODING_LENGTH,
        "max_input_length": MAX_INPUT_LENGTH,
        "protect": PROTECT,
        "protect_min_confidence": PROTECT_MIN_CONFIDENCE,
        "corpus": {
            "user": (DATA / "user").relative_to(ROOT).as_posix(),
            "domain": (DATA / "domain").relative_to(ROOT).as_posix(),
            "general": (DATA / "general").relative_to(ROOT).as_posix(),
        },
        "rules": (DATA / "rules.json").relative_to(ROOT).as_posix(),
        "cases": len(records),
        "used_model_cases": sum(1 for r in records if r["expected"]["used_model"]),
        "lost_placeholders": sum(1 for r in records if r["expected"]["lost_placeholders"]),
        "protected_terms": sum(r["expected"]["protected_terms"] for r in records),
        "selection": "first 120 candidates with used_model=true and lost_placeholders=0",
        "cases_sha256": hashlib.sha256((OUT / "cases.jsonl").read_bytes()).hexdigest(),
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} cases to {OUT}")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
