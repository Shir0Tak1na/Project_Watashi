#!/usr/bin/env python3
"""Translation accuracy benchmark. No screen, no network.

Why this exists: the rest of the suite proves translation *runs*. Nothing proved it
was any *good*, which makes "improve accuracy" unfalsifiable -- every change looks
like progress and nobody can show it. This scores output against a reference set so
each candidate change has to justify itself with a number.

Two metrics, because the project's goal is two things at once and one number would
hide the other:

* **term accuracy** -- did the terms that must appear actually appear. This is the
  project's reason to exist: a generic model renders an invented word inconsistently
  (`voidsword` -> "无效的字符"), and the corpus is what fixes that.
* **chrF** -- character n-gram F-score against a reference, which measures fluency.
  Character level on purpose: it is far more stable than BLEU at sentence length and
  needs no dependency.

Measure one configuration at a time with ``--backend``, so a change can be attributed:

    run.cmd bench_translate --backend corpus     # corpus + rules only
    run.cmd bench_translate --backend model      # the local NMT model alone
    run.cmd bench_translate --set my_lines.jsonl

Add real lines from a real game. A set written by the person who wrote the code
measures that person's imagination, not the thing being shipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.config import AppConfig
from watashi.lang import to_nllb_code

#: Seed set. Each entry: source, language, reference translation, terms that MUST
#: appear. Extend it -- and prefer real lines from a real visual novel.
SEED: list[dict] = [
    {"src": "彼は虚空の境地に達した", "lang": "ja", "tgt": "zh-CN",
     "ref": "他达到了虚空的境界",
     # 境界 must be *produced*: the source says 境地, not 境界.
     "terms": ["境界"]},
    {"src": "その剣意は伝説に過ぎない", "lang": "ja", "tgt": "zh-CN",
     "ref": "那剑意不过是传说",
     # 剑意 and 传说: the source writes 剣意 and 伝説, so neither is satisfied
     # by echoing. Terms that appear verbatim in the source would be.
     "terms": ["剑意", "传说"]},
    {"src": "この宗门の功法を学びたい", "lang": "ja", "tgt": "zh-CN",
     "ref": "我想学这个宗门的功法",
     # No term requirement: 宗门 and 功法 are written identically in both languages,
     # so they cannot discriminate. This line tests fluency only; chrF covers it.
     "terms": []},
    {"src": "反龍超霊力虚空剣", "lang": "ja", "tgt": "zh-CN",
     "ref": "反龙超灵力虚空剑",
     "terms": ["反龙", "超灵力", "虚空剑"]},
    {"src": "he broke through to the void realm", "lang": "en", "tgt": "zh-CN",
     "ref": "他突破到了虚空境界", "terms": ["虚空", "境界"]},
    {"src": "the sword intent of this sect is a myth", "lang": "en", "tgt": "zh-CN",
     "ref": "这个宗门的剑意是个神话", "terms": ["剑意", "宗门"]},
    {"src": "antidragon superspirit voidsword", "lang": "en", "tgt": "zh-CN",
     "ref": "反龙超灵力虚空剑", "terms": ["反龙", "超灵力", "虚空剑"]},
    {"src": "他突破到了虚空境界", "lang": "zh", "tgt": "en",
     "ref": "he broke through to the void realm", "terms": []},
    {"src": "その剣意は伝説に過ぎない", "lang": "ja", "tgt": "en",
     "ref": "that sword intent is nothing but a legend", "terms": []},
    {"src": "この功法は強力だ", "lang": "ja", "tgt": "en",
     "ref": "this technique is powerful", "terms": []},
]


def chrf(hypothesis: str, reference: str, max_n: int = 4, beta: float = 2.0) -> float:
    """Character n-gram F-score. Dependency free, and stable at sentence length."""
    if not hypothesis or not reference:
        return 0.0
    scores = []
    for n in range(1, max_n + 1):
        hyp = [hypothesis[i:i + n] for i in range(len(hypothesis) - n + 1)]
        ref = [reference[i:i + n] for i in range(len(reference) - n + 1)]
        if not hyp or not ref:
            continue
        overlap = 0
        remaining = list(ref)
        for gram in hyp:
            if gram in remaining:
                remaining.remove(gram)
                overlap += 1
        precision = overlap / len(hyp)
        recall = overlap / len(ref)
        if precision + recall == 0:
            scores.append(0.0)
        else:
            beta2 = beta * beta
            scores.append(
                (1 + beta2) * precision * recall / (beta2 * precision + recall)
            )
    return sum(scores) / len(scores) if scores else 0.0


def term_hits(output: str, terms: list[str]) -> tuple[int, list[str]]:
    missing = [t for t in terms if t not in output]
    return len(terms) - len(missing), missing


def load_set(path: Path | None) -> list[dict]:
    if path is None:
        return SEED
    entries = []
    # utf-8-sig, not utf-8: PowerShell's `Set-Content -Encoding UTF8` and many Windows
    # editors prefix a byte order mark, which makes the first line fail to parse as
    # JSON. The error it produced was an exit with no output at all, which is a
    # miserable thing to debug from a benchmark tool.
    for number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{number}: not valid JSON: {exc}") from exc
    return entries


def outcome_text(outcome: object) -> str:
    """The translated string out of a corpus-path ``Outcome``.

    The field is ``target_text``. An earlier version asked for ``target``, which does
    not exist, so it silently fell back to ``str(outcome)`` -- the whole dataclass
    repr. Every term then "matched" because the repr contains the *source* text and
    the span list as well as the translation, and the reported term accuracy was
    meaningless. A fallback that hides a missing attribute turns a measurement bug
    into a confident wrong number, so nothing here falls back to the repr.
    """
    for name in ("target_text", "target", "text"):
        value = getattr(outcome, name, None)
        if isinstance(value, str):
            return value
    raise TypeError(f"cannot read a translation out of {type(outcome).__name__}")


def run_corpus(entries: list[dict]) -> list[str]:
    from watashi.session import build_translator

    config = AppConfig.load()
    # No model: this is the corpus + rules path alone, which is instant and whose
    # accuracy is entirely a matter of corpus coverage.
    config.translation["nmt_model"] = None
    translator = build_translator(config)
    outputs = []
    for entry in entries:
        try:
            outputs.append(outcome_text(translator.translate(entry["src"], entry["tgt"])))
        except Exception as exc:
            outputs.append(f"<error: {exc}>")
    return outputs


def run_model(entries: list[dict], model_dir: Path) -> list[str]:
    from watashi.local_nmt import NmtModel

    model = NmtModel(model_dir)
    if not model.load():
        raise SystemExit(f"model did not load: {model.load_error}")
    outputs = []
    for entry in entries:
        try:
            outputs.append(
                model.translate_one(
                    entry["src"], to_nllb_code(entry["tgt"]), to_nllb_code(entry["lang"])
                )
            )
        except Exception as exc:
            outputs.append(f"<error: {exc}>")
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["corpus", "model"], default="corpus")
    parser.add_argument("--set", type=Path, default=None, help="JSONL evaluation set")
    parser.add_argument("--model", type=Path,
                        default=Path(__file__).resolve().parent.parent / "models" / "llm" / "nllb-200-distilled-600M-ct2-int8")
    parser.add_argument("--quiet", action="store_true", help="totals only")
    args = parser.parse_args()

    entries = load_set(args.set)
    print("=" * 96)
    print(f"Translation benchmark  backend={args.backend}  lines={len(entries)}")
    print("=" * 96)

    outputs = run_corpus(entries) if args.backend == "corpus" else run_model(entries, args.model)

    total_terms = hit_terms = 0
    chrf_scores = []
    rows = []
    non_discriminating: list[str] = []
    for entry in entries:
        source = entry["src"]
        for term in entry.get("terms") or []:
            # A term that already appears verbatim in the source proves nothing: an
            # untranslated echo satisfies it. Japanese-to-Chinese made this blatant,
            # because a kanji term like 宗门 is written identically in both languages,
            # so この宗门の功法を学びたい scored a "hit" on a line that was echoed
            # unchanged. Surfaced rather than silently tolerated, because a test set
            # that can be satisfied by doing nothing measures nothing.
            if term in source:
                non_discriminating.append(f"{term!r} 出现在原文 {source!r} 中")

    for entry, output in zip(entries, outputs):
        echoed = output.strip() == entry["src"].strip()
        terms = entry.get("terms") or []
        if echoed:
            # Nothing was translated, so no term can count, however it looks.
            hits, missing = 0, list(terms)
        else:
            hits, missing = term_hits(output, terms)
        total_terms += len(terms)
        hit_terms += hits
        score = chrf(output, entry.get("ref", ""))
        chrf_scores.append(score)
        # A line with a required term counts as correct only if every term landed;
        # partial credit would let a change that breaks one term look like progress.
        ok = (not missing) and not echoed
        rows.append((entry, output, score, missing, ok))

    for entry, output, score, missing, ok in rows:
        if args.quiet:
            continue
        # Output identical to the input means nothing was actually translated: the
        # transliterate fallback echoed it. That is the common outcome when a
        # language pair has no corpus coverage at all, and it is invisible in the
        # totals, so it is called out per line.
        echoed = output.strip() == entry["src"].strip()
        flag = "ECHO" if echoed else ("ok  " if ok else "MISS")
        print(f"\n[{flag}] {entry['lang']}->{entry['tgt']}  chrF={score:.3f}")
        print(f"   原文   : {entry['src']}")
        print(f"   输出   : {output}")
        print(f"   参考   : {entry.get('ref', '')}")
        if missing:
            print(f"   缺失术语: {'、'.join(missing)}")

    lines_ok = sum(1 for *_, ok in rows if ok)
    echoed = sum(1 for e, o, *_ in rows if o.strip() == e["src"].strip())
    print("\n" + "=" * 96)
    print(f"  行数            {len(rows)}")
    if total_terms:
        print(f"  术语准确率      {hit_terms}/{total_terms} "
              f"({100.0 * hit_terms / total_terms:.1f}%)")
    else:
        print("  术语准确率      (无要求术语)")
    print(f"  完全通过的行    {lines_ok}/{len(rows)} ({100.0 * lines_ok / len(rows):.1f}%)")
    print(f"  原样回显的行    {echoed}/{len(rows)}"
          f"  <- 完全没翻译，兜底规则把原文还了回来")
    check_corpus_terms(non_discriminating, len(rows))
    print(f"  平均 chrF       {sum(chrf_scores) / len(chrf_scores):.3f}")
    print("=" * 96)
    return 0


def check_corpus_terms(non_discriminating: list[str], lines: int) -> None:
    """Report required terms that the source already satisfies.

    Printed as a defect in the *test set*, not in the translation. The failure is
    quiet otherwise: the score looks better than the tool deserves, and the person
    reading the number has no way to know.
    """
    if not non_discriminating:
        print(f"  测试集检查      {lines} 行的术语都要求翻译产出（无假要求）")
        return
    print(f"  测试集缺陷      {len(non_discriminating)} 个术语在原文中已存在，"
          f"可被原文回显满足：")
    for item in non_discriminating:
        print(f"                    - {item}")


if __name__ == "__main__":
    raise SystemExit(main())
