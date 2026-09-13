#!/usr/bin/env python3
"""Local translation model benchmark -- the numbers behind the M3 design.

Measures what actually matters for a real time subtitle pipeline:

* model load cost, and per sentence latency at several thread counts
* single vs batched latency (batching is the single biggest win)
* **term protection on vs off**, which is the whole reason this project exists:
  a general NMT model renders invented terms differently every time, so the
  corpus has to stay authoritative
* how often a corpus term survives the model as a placeholder, and why the
  remaining cases fall back

    run.cmd bench_nmt
    run.cmd bench_nmt --threads 2,4,8 --beams 1,4
    run.cmd bench_nmt --contention      # also measure the effect on OCR
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.local_nmt import NmtModel, refine_with_nmt
from watashi.translate import LAYER_DOMAIN, LAYER_GENERAL, LAYER_USER, CorpusStore

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

#: Sentences designed to show the difference: the invented terms are exactly the
#: ones a general model gets wrong.
SENTENCES = (
    "he broke through to the void realm",
    "the sword intent of this sect is a myth",
    "she poured all her qi into the formation",
    "the antidragon superspirit guard blocked the gate",
    "gg wp noob",
    "antidragon superspirit voidsword",
)


def build_corpus() -> CorpusStore:
    return CorpusStore(
        layers={
            LAYER_USER: [ROOT / "plugins" / "user" / "custom_rules"],
            LAYER_DOMAIN: [HERE / "corpus", ROOT / "rules" / "slang", ROOT / "rules" / "fiction"],
            LAYER_GENERAL: [ROOT / "rules" / "dictionaries"],
        },
        rule_files=[HERE / "rules" / "engine_rules.json"],
        auto_reload=False,
    )


def build_model(model_dir: Path, compute_type: str, beam: int, threads: int) -> NmtModel:
    return NmtModel(
        model_dir,
        compute_type=compute_type,
        beam_size=beam,
        intra_threads=threads,
        inter_threads=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench_nmt",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "models" / "llm" / "nllb-200-distilled-600M-ct2-int8",
    )
    parser.add_argument("--threads", type=str, default="4")
    parser.add_argument("--beams", type=str, default="1")
    parser.add_argument("--target", type=str, default="zh-CN")
    parser.add_argument("--contention", action="store_true",
                        help="also measure OCR latency while the model translates")
    args = parser.parse_args(argv)

    model_dir = args.model.resolve()
    print("=" * 78)
    print("Project Watashi - local translation model benchmark")
    print("=" * 78)
    print(f"  model      : {model_dir}")
    print(f"  present    : {model_dir.exists()}")
    if not model_dir.exists():
        print("  run: prototype\run.cmd fetch_model")
        return 1

    corpus = build_corpus()
    print(f"  corpus     : {corpus.size} entries, {corpus.rule_count} rules")
    print("")

    thread_list = [int(t) for t in args.threads.split(",") if t.strip()]
    beam_list = [int(b) for b in args.beams.split(",") if b.strip()]

    # ------------------------------------------------------------------ #
    # 1. load cost and single-sentence latency by thread count
    # ------------------------------------------------------------------ #
    print("-- load cost and per sentence latency --")
    print(f"  {'threads':>8} {'beam':>5} {'load ms':>9} {'mean ms':>9} {'median':>8} {'sent/s':>8}")
    print("  " + "-" * 52)

    best = None
    benchmark_model: NmtModel | None = None
    for threads in thread_list:
        for beam in beam_list:
            model = build_model(model_dir, "int8", beam, threads)
            if not model.load():
                print(f"  load failed: {model.load_error}")
                return 1
            # warm
            model.translate_one(SENTENCES[0], "zho_Hans")
            samples = []
            for sentence in SENTENCES:
                started = time.perf_counter()
                model.translate_one(sentence, "zho_Hans")
                samples.append((time.perf_counter() - started) * 1000.0)
            mean = statistics.mean(samples)
            median = statistics.median(samples)
            print(
                f"  {threads:>8} {beam:>5} {model.load_ms:>9.0f} {mean:>9.0f} "
                f"{median:>8.0f} {1000.0 / mean:>8.2f}"
            )
            if best is None or mean < best[0]:
                best = (mean, threads, beam, model)
            else:
                del model

    assert best is not None
    _, best_threads, best_beam, benchmark_model = best
    print(f"  best: threads={best_threads} beam={best_beam} ({best[0]:.0f} ms/sentence)")

    # ------------------------------------------------------------------ #
    # 2. batching
    # ------------------------------------------------------------------ #
    print("")
    print("-- batching: the biggest single win --")
    batch = list(SENTENCES)
    started = time.perf_counter()
    benchmark_model.translate_batch(batch, "zho_Hans")
    batched_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    for sentence in batch:
        benchmark_model.translate_one(sentence, "zho_Hans")
    single_ms = (time.perf_counter() - started) * 1000.0
    print(f"  one at a time : {single_ms:7.0f} ms total, {single_ms / len(batch):6.0f} ms/sentence")
    print(f"  batched ({len(batch):2d})   : {batched_ms:7.0f} ms total, {batched_ms / len(batch):6.0f} ms/sentence")
    print(f"  speedup       : {single_ms / max(1.0, batched_ms):.2f}x")

    # ------------------------------------------------------------------ #
    # 3. term protection: the reason the corpus stays authoritative
    # ------------------------------------------------------------------ #
    print("")
    print("-- term protection: corpus terms kept, with vs without --")
    print(f"  {'sentence':42s} {'raw model (no protection)':34s} {'protected'}")
    print("  " + "-" * 108)

    protected_count = 0
    lost_count = 0
    fallback_reasons: dict[str, int] = {}
    for sentence in SENTENCES:
        off = refine_with_nmt(
            benchmark_model, corpus, sentence, args.target,
            protect=False, min_confidence=0.4,
        )
        on = refine_with_nmt(
            benchmark_model, corpus, sentence, args.target,
            protect=True, min_confidence=0.4,
        )
        protected_count += on.protected_terms
        lost_count += on.lost_placeholders
        if on.fallback_reason:
            fallback_reasons[on.fallback_reason] = (
                fallback_reasons.get(on.fallback_reason, 0) + 1
            )
            label = "corpus (model rejected)"
        else:
            label = f"{on.protected_terms} term(s) kept"
        print(f"  {sentence[:42]:42s} {off.target_text[:34]:34s} {on.target_text}")
        print(f"  {'':42s} {'':34s}   ^ {label}")

    print("")
    print(f"  corpus terms carried through  : {protected_count}")
    print(f"  placeholders lost             : {lost_count}")
    print(f"  lines served by corpus instead: {sum(fallback_reasons.values())}/{len(SENTENCES)}")
    for reason, count in sorted(fallback_reasons.items(), key=lambda kv: -kv[1]):
        print(f"      {count}x  {reason}")

    print("")
    print("-- why term protection matters --")
    print("  Without it the model invents a different rendering for each invented")
    print("  term, so a work's terminology drifts between lines. Placeholders keep")
    print("  the corpus authoritative while the model supplies the grammar.")

    # ------------------------------------------------------------------ #
    # 4. optional: contention with OCR
    # ------------------------------------------------------------------ #
    if args.contention:
        print("")
        print("-- contention with OCR (both saturate memory bandwidth) --")
        import threading

        from watashi.ocr import RapidOcrEngine
        from watashi.selftest import render_text_image

        frame = render_text_image(
            ("he broke through to the void realm", "gg wp noob that nerf was brutal"),
            width=1280,
        )
        ocr = RapidOcrEngine(max_width=0, use_cls=False, intra_op_threads=4)
        ocr.recognize(frame)

        def median_ocr(n: int = 5) -> float:
            samples = []
            for _ in range(n):
                started = time.perf_counter()
                ocr.recognize(frame)
                samples.append((time.perf_counter() - started) * 1000.0)
            return statistics.median(samples)

        idle = median_ocr()
        stop = threading.Event()

        def refine_loop() -> None:
            while not stop.is_set():
                benchmark_model.translate_one(SENTENCES[0], "zho_Hans")

        thread = threading.Thread(target=refine_loop, daemon=True)
        thread.start()
        time.sleep(0.3)
        busy = median_ocr()
        stop.set()
        thread.join(timeout=30)

        print(f"  OCR with the model idle        : {idle:7.1f} ms")
        print(f"  OCR while the model translates : {busy:7.1f} ms  ({busy / idle:.2f}x)")
        print("")
        print("  This is why the pipeline defers refinement while OCR is busy:")
        print("  see HybridTranslator._wait_for_quiet_period.")

    print("")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
