#!/usr/bin/env python3
"""Decisive experiment: pure detection cost, and what capping boxes could save.

How the flags work, established by reading ocr.py rather than guessing:
`_invoke` calls `engine(image, **self._kwargs)`, and `_kwargs` is a snapshot taken in
__init__. So the flags are call-time kwargs, and the way to change them is to change
`_kwargs` -- mutating `use_rec` on the object does nothing, which is why three earlier
probes compared identical configurations and reported nonsense.

Timing is INTERLEAVED (A,B,A,B,...) with both median and spread reported. The previous
attempts compared conditions in sequence and were swamped: the same 8-line frame
measured 588 ms in one run and 204 ms in another, a 3x drift larger than the effect
being measured.

Temporary diagnostic.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from watashi.config import AppConfig
from watashi.session import build_ocr

SENTENCES = [
    "he broke through to the void realm",
    "the sword intent of this sect is a myth",
    "she walked into the empty hall alone",
    "they spoke of the ancient path",
    "the old master said nothing",
    "rain fell on the mountain",
    "his blade never knew defeat",
    "the gates opened at dawn",
]


def make_frame(count: int, width: int = 1280, height: int = 240) -> np.ndarray:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    step = height // max(1, count + 1)
    for index in range(count):
        draw.text((20, 12 + index * step), SENTENCES[index % len(SENTENCES)],
                  fill="black", font=font)
    return np.asarray(image, dtype=np.uint8)[:, :, ::-1].copy()


def timed(engine, frame, rounds: int) -> list[float]:
    out = []
    for _ in range(rounds):
        started = time.perf_counter()
        engine.recognize(frame)
        out.append((time.perf_counter() - started) * 1000.0)
    return out


def main() -> int:
    engine = build_ocr(AppConfig.load())
    engine.ensure_loaded()
    frame = make_frame(8)
    base_kwargs = dict(getattr(engine, "_kwargs", {}) or {})

    print("=" * 84)
    print("阶段开关（通过 _kwargs，即调用时真正传进去的东西）")
    print("=" * 84)
    print(f"\n  基线 _kwargs = {base_kwargs}\n")

    def probe(label: str, **flags) -> tuple[int, int]:
        engine._kwargs = {**base_kwargs, **flags}  # noqa: SLF001 - the call-time kwargs
        try:
            result = engine.recognize(frame)
        except Exception as exc:
            # use_rec=False makes RapidOCR return a shape our _unpack does not expect,
            # so a detection-only call cannot go through recognize(). Reported rather
            # than swallowed: the flag works, the wrapper just cannot handle the result.
            print(f"  {label:22s} -> recognize() raised {type(exc).__name__}: {exc}")
            print(f"  {'':22s}    (detection-only must bypass recognize and call _invoke)")
            raw = engine._invoke(frame)  # noqa: SLF001
            boxes_only = len(raw[0]) if isinstance(raw, (list, tuple)) and raw and raw[0] else 0
            print(f"  {'':22s}    _invoke() returned {boxes_only} box(es)")
            return boxes_only, 0
        with_text = sum(1 for line in result.lines if line.text.strip())
        print(f"  {label:22s} -> {len(result.lines)} 框, {with_text} 个有文本")
        return len(result.lines), with_text

    full_boxes, full_text = probe("全部开启")
    rec_boxes, rec_text = probe("use_rec=False", use_rec=False)
    det_boxes, det_text = probe("use_det=False", use_det=False)
    engine._kwargs = base_kwargs

    print("\n-- 判读 --")
    if rec_text == 0 and rec_boxes > 0:
        print("  use_rec=True->False: 框还在、文本消失 = 标志【有效】，可用来量纯检测")
    elif rec_text == full_text:
        print("  use_rec: 与基线相同 = 标志【无效】，纯检测耗时无法这样量")
    if det_boxes == 0 and full_boxes > 0:
        print("  use_det=False: 框消失 = 标志【有效】")
    elif det_boxes == full_boxes:
        print("  use_det: 与基线相同 = 标志【无效】")

    # ---- interleaved timing --------------------------------------------- #
    print("\n" + "=" * 84)
    print("交错采样：完整 vs 仅检测（同一次进程内交替，抵消漂移）")
    print("=" * 84)
    rounds = 7
    full_ms: list[float] = []
    det_ms: list[float] = []

    def time_invoke() -> float:
        started = time.perf_counter()
        engine._invoke(frame)  # noqa: SLF001 - bypasses _unpack, which cannot take det-only
        return (time.perf_counter() - started) * 1000.0

    for _ in range(rounds):
        engine._kwargs = base_kwargs
        started = time.perf_counter()
        engine.recognize(frame)          # full: det + rec + grouping
        full_ms.append((time.perf_counter() - started) * 1000.0)
        engine._kwargs = {**base_kwargs, "use_rec": False}
        det_ms.append(time_invoke())     # detection only
    engine._kwargs = base_kwargs

    fm, dm = statistics.median(full_ms), statistics.median(det_ms)
    print(f"\n  完整 (n={len(full_ms)}): 中位 {fm:.1f} ms  范围 "
          f"{min(full_ms):.1f}–{max(full_ms):.1f}")
    print(f"  仅检测(n={len(det_ms)}): 中位 {dm:.1f} ms  范围 "
          f"{min(det_ms):.1f}–{max(det_ms):.1f}")
    print(f"\n  识别部分 ≈ {fm - dm:.1f} ms  （占 {100 * (fm - dm) / fm:.0f}%）")
    print(f"  检测部分 ≈ {dm:.1f} ms    （占 {100 * dm / fm:.0f}%）")
    print("")
    if fm - dm > dm:
        print("  识别占大头 => 限制识别框数基本按比例省时，值得做。")
    else:
        print("  检测占大头 => 限框只能省识别那部分，设置应如实说明，")
        print("                不要宣传成「解决卡顿」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
