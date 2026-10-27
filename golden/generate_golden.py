#!/usr/bin/env python3
"""Generate the W0.4 golden corpus from the *current* Python implementation.

Every expected value is produced by running the engine at the commit recorded in
the output. Nothing is predicted from the C++ plan or written by hand.

    .venv/Scripts/python.exe golden/generate_golden.py
    .venv/Scripts/python.exe golden/generate_golden.py --commit <sha>

Outputs:
    golden/cases.jsonl   one case per line, sorted keys, no timestamps
    golden/meta.json     counts, commit, corpus digest, model path

The file is byte-identical when regenerated at the same commit: no clock, no
latency and no unordered container is serialised. Model output is greedy and the
engine is deterministic; this was verified by running the generator twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from watashi.config import AppConfig  # noqa: E402
from watashi.local_nmt import HybridTranslator, load_hybrid_translator  # noqa: E402
from watashi.pipeline import Pipeline, PipelineConfig  # noqa: E402
from watashi.translate import (  # noqa: E402
    LAYER_GENERAL,
    LAYER_USER,
    CorpusStore,
    Context,
    LAYER_DOMAIN,
)

GOLDEN = ROOT / "golden"
FIXTURE = GOLDEN / "fixtures" / "corpus.json"
CASES = GOLDEN / "cases.jsonl"
META = GOLDEN / "meta.json"


def git_commit(explicit: str | None = None) -> str:
    """Read the commit without starting a process.

    ``subprocess`` fails in the restricted sandbox (CreatePipe -> WinError 5),
    which is the same class of failure as selfcheck_web. ``.git/HEAD`` is a flat
    file, so reading it is enough and works in CI. A detached HEAD stores the sha
    directly; a symbolic ref is resolved from ``.git/<ref>`` or ``packed-refs``.
    ``--commit`` overrides both, for environments that hand in the sha.
    """
    if explicit:
        return explicit.strip()
    git_dir = ROOT / ".git"
    if git_dir.is_file():
        # Worktree/submodule: .git is a file containing "gitdir: <path>".
        text = git_dir.read_text("utf-8").strip()
        if text.startswith("gitdir:"):
            git_dir = (ROOT / text.split(":", 1)[1].strip()).resolve()
    head = (git_dir / "HEAD").read_text("utf-8").strip()
    if not head.startswith("ref:"):
        return head
    ref = head.split(":", 1)[1].strip()
    ref_file = git_dir / ref
    if ref_file.exists():
        return ref_file.read_text("utf-8").strip()
    packed = git_dir / "packed-refs"
    if packed.exists():
        for line in packed.read_text("utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip()
    raise SystemExit(f"cannot resolve {ref} from {git_dir}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_corpus(config: AppConfig) -> CorpusStore:
    """The shipped corpus plus the scene/condition fixture in the user layer."""
    layers = {
        LAYER_USER: [FIXTURE.parent, *config.corpus_dirs("user")],
        LAYER_DOMAIN: config.corpus_dirs("domain"),
        LAYER_GENERAL: config.corpus_dirs("general"),
    }
    return CorpusStore(
        layers=layers,
        rule_files=config.rule_files(),
        auto_reload=False,
    )


def corpus_digest(config: AppConfig) -> str:
    paths: set[Path] = set()
    for layer in ("user", "domain", "general"):
        for directory in config.corpus_dirs(layer):
            if directory.is_dir():
                paths.update(directory.glob("*.json"))
    paths.add(FIXTURE)
    paths.update(config.rule_files())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_summary(
    *,
    target: str,
    model_path: str | None,
    scene: str = "",
    window: str = "",
    digest: str,
) -> dict:
    return {
        "target": target,
        "source": "auto",
        "scene": scene,
        "window": window,
        "protect_terms": True,
        "protect_min_confidence": 0.4,
        "final_translation": True,
        "join_fragmented_lines": True,
        "join_max_lines": 3,
        "join_max_chars": 240,
        "nmt_model": model_path,
        "corpus_digest": digest,
    }


def make_case(
    *,
    case_id: str,
    category: str,
    text: str,
    expected: dict,
    config: dict,
    commit: str,
    mode: str = "screen",
) -> dict:
    return {
        "id": case_id,
        "category": category,
        "git_commit": commit,
        "config_digest": hashlib.sha256(canonical(config).encode("utf-8")).hexdigest(),
        "config": config,
        "input": {
            "text": text,
            "mode": mode,
            "target": config["target"],
            "source": config["source"],
            "scene": config["scene"],
            "window": config["window"],
        },
        "expected": expected,
    }


def expected_of(refinement) -> dict:
    return {
        "target": refinement.target_text,
        "backend": refinement.backend,
        "protected_terms": int(refinement.protected_terms),
        "fallback_reason": refinement.fallback_reason,
        "used_model": bool(refinement.used_nmt),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        default=None,
        help="commit sha to record; default reads .git/HEAD without a subprocess",
    )
    args = parser.parse_args(argv)

    config = AppConfig.load()
    commit = git_commit(args.commit)
    corpus = build_corpus(config)
    digest = corpus_digest(config)

    model_path = config.translation.get("nmt_model")
    resolved_model = None
    if model_path:
        candidate = Path(str(model_path))
        resolved_model = (
            candidate if candidate.is_absolute() else (config.base_dir / candidate)
        )
        resolved_model = str(resolved_model.resolve())
    display_model = (
        str(Path(resolved_model).relative_to(ROOT)).replace("\\", "/")
        if resolved_model
        else None
    )

    full = load_hybrid_translator(
        corpus,
        resolved_model,
        protect_terms=True,
        source_lang=config.source_lang,
        protect_min_confidence=0.4,
    )
    corpus_only = load_hybrid_translator(
        corpus,
        None,
        protect_terms=True,
        source_lang=config.source_lang,
        protect_min_confidence=0.4,
    )
    if full.model is None or not full.model.available:
        raise SystemExit("the NLLB model is not available; golden output needs it")

    cases: list[dict] = []

    def add(
        category: str,
        text: str,
        translator: HybridTranslator,
        *,
        target: str = "zh-CN",
        scene: str = "",
        window: str = "",
    ) -> None:
        context = Context(target_lang=target, scene=scene, window=window)
        refinement = translator.translate_readable_batch(
            [text], target, context
        )[0]
        model_for_config = display_model if translator.model is not None else None
        summary = config_summary(
            target=target,
            model_path=model_for_config,
            scene=scene,
            window=window,
            digest=digest,
        )
        cases.append(
            make_case(
                case_id=f"{category}-{len(cases) + 1:03d}",
                category=category,
                text=text,
                expected=expected_of(refinement),
                config=summary,
                commit=commit,
            )
        )

    # 1. corpus exact hits: term-only (corpus short-circuit) and in a sentence.
    demo = json.loads((ROOT / "prototype" / "corpus" / "demo_terms.json").read_text("utf-8"))
    terms = sorted(demo["entries"].keys())
    for index, term in enumerate(terms[:35]):
        add("corpus_exact", term, full)
    for term in terms[35:55]:
        add("corpus_exact", f"the {term} appeared again", full)

    # 2. rule-engine words that are not in the shipped corpus.
    rule_words = [
        "antidragon", "superspirit", "voidsword", "warpdrive", "terraforming",
        "hyperdrive", "darkmatter", "starforge", "soulblade", "runeblade",
        "voidwarden", "dragonlord", "spiritfox", "skybreaker", "moonwell",
        "bloodrune", "shadowgate", "ironveil", "stormcaller", "emberwing",
        "frostfang", "gravewarden", "sunfire", "voidstep", "astralgate",
    ]
    for word in rule_words:
        add("rules", f"the {word} was seen at the gate", full)

    # 3. scenes: the same term answered differently by the selected scene.
    for scene, line in [
        ("finance", "the bank approved the loan"),
        ("finance", "the bank raised the interest rate"),
        ("finance", "the bank closed the account"),
        ("finance", "the bank reported record profit"),
        ("finance", "the bank issued a new card"),
        ("nature", "the bank was muddy after the rain"),
        ("nature", "the bank overflowed in the storm"),
        ("nature", "the bank was covered in moss"),
        ("nature", "the bank collapsed into the water"),
        ("nature", "the bank was full of reeds"),
        ("legal", "the seal was stamped on the contract"),
        ("legal", "the seal proved the document was valid"),
        ("legal", "the seal was stored in the drawer"),
        ("legal", "the seal carried the company name"),
        ("legal", "the seal was broken by the clerk"),
        ("shipping", "the seal was broken before delivery"),
        ("shipping", "the seal kept the crate closed"),
        ("shipping", "the seal was checked at the dock"),
        ("shipping", "the seal had a tracking number"),
        ("shipping", "the seal was replaced after inspection"),
    ]:
        add("scene", line, full, scene=scene)

    # 4. when_line / when_near / when_window conditions.
    for line in [
        "the account crane was audited",
        "the account crane failed the review",
        "the account crane was closed",
        "the account crane needs a receipt",
        "the account crane was overdue",
    ]:
        add("conditions", line, full, scene="nature")
    for line in [
        "the river crane took flight",
        "the river crane stood in the shallows",
        "the river crane hunted for fish",
        "the river crane was painted on the wall",
        "the river crane returned at dusk",
    ]:
        add("conditions", line, full, scene="nature")
    for line in [
        "the port accepted the ship",
        "the port unloaded the barge",
        "the port refused the ship",
        "the port was closed to the barge",
        "the port guided the ship in",
    ]:
        add("conditions", line, full)
    for line in [
        "the port for the api call",
        "the port exposed by the code",
        "the port in the api response",
        "the port used by the code",
        "the port changed in the api",
    ]:
        add("conditions", line, full)
    for window in ("Novel", "Novel", "Novel", "Report", "Report"):
        add(
            "conditions",
            "the quayside was empty before dawn",
            full,
            scene="harbour",
            window=window,
        )

    # 5. term protection: several authoritative terms in one sentence.
    for line in [
        "the sword intent of this sect is a myth",
        "he broke through to the void realm",
        "she poured all her qi into the formation",
        "the antidragon superspirit guard blocked the gate",
        "a voidblade cut through the ward",
        "the elder refined a pill in the dantian",
        "the sect elder guarded the spiritual root",
        "the immortal opened the grimoire",
        "a wyrm guarded the elixir",
        "the lich cast a rune on the sigil",
        "the nascent soul entered the meridian",
        "the heavenly dao rejected the tribulation",
        "the dragon lord crossed the star chart",
        "the fleet entered hyperspace",
        "the hive mind controlled the colony",
        "the cyborg repaired the neural link",
        "the android studied the arcane",
        "the sorcerer opened a portal to the void",
        "the sword saint shattered the ward",
        "the demon lord consumed the elixir",
        "the cultivator refined the spiritual root",
        "the immortal ascended through the tribulation",
        "the chimera guarded the singularity",
        "the terraforming crew reached the exoplanet",
        "the warp drive opened a path through the void",
    ]:
        add("term_protection", line, full)

    # 6. fallback: a target with no NLLB code. The corpus result is all the
    #    engine can offer; this is recorded rather than hidden.
    for index in range(15):
        add(
            "fallback",
            f"the void realm opened on attempt {index + 1}",
            full,
            target="xx",
        )

    # 7. live mode: the same final-translation path fed through
    #    Pipeline.translate_texts, which is what the audio pipeline calls.
    live_texts = [
        "the sword intent of this sect is a myth",
        "he broke through to the void realm",
        "the streamer said gg wp noob",
        "that nerf was brutal but the movement is fine",
        "the elder refined a pill for the sect",
        "she poured all her qi into the formation",
        "the antidragon guard blocked the gate",
        "the wyrm slept under the mountain",
        "the immortal opened the grimoire",
        "the fleet jumped into hyperspace",
        "the colony lost contact with the hive mind",
        "the cyborg repaired the neural link",
        "the lich cast a rune on the sigil",
        "the cultivator reached the nascent soul",
        "the heavenly dao sent a tribulation",
        "the dragon lord crossed the star chart",
        "the terraforming crew reached the exoplanet",
        "the warp drive opened a path",
        "the sword saint shattered the ward",
        "the demon lord consumed the elixir",
        "the chimera guarded the singularity",
        "the android studied the arcane",
        "the spiritual root awakened in the dantian",
        "the voidblade cut through the ward",
        "the immortal ascended through the tribulation",
    ]
    live_config = PipelineConfig(
        target_lang="zh-CN",
        source_lang="auto",
        final_translation=True,
        join_fragmented_lines=True,
    )
    live_pipeline = Pipeline(None, None, full, live_config)  # type: ignore[arg-type]
    live_context = Context(target_lang="zh-CN")
    for text in live_texts:
        update = live_pipeline.translate_texts([text])
        if update is None or not update.lines:
            raise SystemExit(f"live pipeline produced no line for {text!r}")
        refinement = full.translate_readable_batch([text], "zh-CN", live_context)[0]
        if update.lines[0].target != refinement.target_text:
            raise SystemExit(f"live and batch paths disagree for {text!r}")
        summary = config_summary(
            target="zh-CN",
            model_path=display_model,
            digest=digest,
        )
        cases.append(
            make_case(
                case_id=f"live-{len(cases) + 1:03d}",
                category="live",
                text=text,
                expected=expected_of(refinement),
                config=summary,
                commit=commit,
                mode="live",
            )
        )

    # 8. model missing: the same corpus with no NMT configured.
    for text in [
        "the sword intent of this sect is a myth",
        "he broke through to the void realm",
        "the elder refined a pill for the sect",
        "she poured all her qi into the formation",
        "the antidragon guard blocked the gate",
        "the wyrm slept under the mountain",
        "the immortal opened the grimoire",
        "the fleet jumped into hyperspace",
        "the colony lost contact with the hive mind",
        "the cyborg repaired the neural link",
    ]:
        add("model_missing", text, corpus_only)

    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    if len(cases) != 200:
        raise SystemExit(f"expected 200 cases, produced {len(cases)}")
    if sum(counts.values()) != 200:
        raise SystemExit("category counts do not sum to 200")

    GOLDEN.mkdir(parents=True, exist_ok=True)
    with CASES.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(canonical(case) + "\n")

    meta = {
        "case_count": len(cases),
        "categories": counts,
        "git_commit": commit,
        "corpus_digest": digest,
        "model": display_model,
        "generator": "golden/generate_golden.py",
    }
    META.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {CASES.relative_to(ROOT)} ({len(cases)} cases)")
    print(f"wrote {META.relative_to(ROOT)}")
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
