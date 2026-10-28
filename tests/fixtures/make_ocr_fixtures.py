#!/usr/bin/env python3
"""Build the OCR fixture set: 50 rendered frames, a manifest, and the Python
reference for the same pixels.

    .venv/Scripts/python.exe tests/fixtures/make_ocr_fixtures.py

Outputs (all under build/, none of it in git):

    build/w11_fixtures/frame_000.bgr ... frame_049.bgr   1200x200x3 raw BGR
    build/w11_fixtures/manifest.json                     frame list + source text
    build/w11_fixtures/reference.jsonl                   RapidOcrEngine lines

The text comes from the project's own vocabulary (``prototype/corpus`` and
``rules/``), not from a hardcoded list, so the fixtures exercise the words the
engine is actually expected to meet. Cases deliberately cover one and two
lines, digits, punctuation, mixed case and several screen positions.

The reference is produced by the *same* frame the C++ side reads: comparing two
engines on one image is the question W1.1 asks, and generating text from the
source strings instead would measure the generator, not OCR.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "prototype"
OUT = ROOT / "build" / "w11_fixtures"

WIDTH = 1200
HEIGHT = 200
BACKGROUND = (24, 24, 24)
INK = (238, 238, 238)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def vocabulary() -> list[str]:
    """English terms the project already ships: corpus entries and rule keys."""
    terms: set[str] = set()
    for path in [
        PROTOTYPE / "corpus" / "demo_terms.json",
        ROOT / "rules" / "fiction" / "fantasy_terms.json",
        ROOT / "rules" / "slang" / "internet_slang.json",
    ]:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        terms.update(str(key).lower() for key in (data.get("entries") or {}))
    # A few ordinary words make the sentences read like subtitles rather than a
    # glossary dump; they are all ASCII because the renderer is a bitmap font.
    terms.update(
        {
            "sect",
            "myth",
            "legend",
            "gate",
            "realm",
            "sword",
            "signal",
            "storm",
            "engine",
            "shadow",
            "chapter",
            "theory",
            "quantum",
            "breakthrough",
        }
    )
    return sorted(term for term in terms if term.isascii() and term.isalpha())


TEMPLATES = (
    "the {a} of this {b} is a myth",
    "gg wp {a}",
    "breaking news: {a} found at {n}",
    "{a}, {b} and {c} (v{n}.{m})",
    "level {n} {a} unlocked!",
    "why is the {a} so {b}?",
    "{a} update {n}% complete...",
    "chapter {n}: the {a} and the {b}",
)


def sentence(rng: random.Random, terms: list[str], template: str) -> str:
    return template.format(
        a=rng.choice(terms),
        b=rng.choice(terms),
        c=rng.choice(terms),
        n=rng.randint(2, 999),
        m=rng.randint(1, 99),
    )


def layout(rng: random.Random, terms: list[str]) -> tuple[list[str], int, int, float]:
    """Returns (lines, x, y, scale). Two-line cases are the wrapped-subtitle case."""
    scale = rng.choice((1.0, 1.1, 1.25, 1.4))
    x = rng.choice((20, 60, 120, 200, 300))
    template = rng.choice(TEMPLATES)
    text = sentence(rng, terms, template)
    if rng.random() < 0.4:
        words = text.split(" ")
        cut = max(1, len(words) // 2)
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
        y = rng.randint(45, 70)
        return lines, x, y, scale
    y = rng.randint(70, 110)
    return [text], x, y, scale


def render(lines: list[str], x: int, y: int, scale: float) -> np.ndarray:
    frame = np.full((HEIGHT, WIDTH, 3), BACKGROUND, dtype=np.uint8)
    size, _ = cv2.getTextSize(lines[0], FONT, scale, 2)
    x = min(x, max(0, WIDTH - size[0] - 10))
    step = int(round(48 * scale))
    for index, text in enumerate(lines):
        # Clamp so a long two-line case cannot run off the right edge.
        text_size, _ = cv2.getTextSize(text, FONT, scale, 2)
        line_x = min(x, max(0, WIDTH - text_size[0] - 10))
        cv2.putText(frame, text, (line_x, y + index * step), FONT, scale, INK, 2, cv2.LINE_AA)
    return frame


def load_ocr():
    sys.path.insert(0, str(PROTOTYPE))
    from watashi.config import AppConfig
    from watashi.session import build_ocr

    return build_ocr(AppConfig.load())


def reference_for(engine, frame: np.ndarray) -> list[dict]:
    result = engine.recognize(frame)
    return [
        {
            "text": line.text,
            "box": [int(value) for value in line.box],
            "score": round(float(line.confidence), 6),
        }
        for line in result.lines
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    terms = vocabulary()
    if len(terms) < 8:
        raise SystemExit("vocabulary is too small to compose fixtures")

    OUT.mkdir(parents=True, exist_ok=True)
    engine = load_ocr()

    manifest = {"width": WIDTH, "height": HEIGHT, "count": args.count, "frames": []}
    references = []
    for index in range(args.count):
        lines, x, y, scale = layout(rng, terms)
        frame = render(lines, x, y, scale)
        name = f"frame_{index:03d}.bgr"
        np.ascontiguousarray(frame).tofile(OUT / name)
        manifest["frames"].append({"file": name, "source": lines})
        references.append({"frame": name, "lines": reference_for(engine, frame)})
        if (index + 1) % 10 == 0:
            print(f"  {index + 1}/{args.count} frames")

    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (OUT / "reference.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in references:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lines_total = sum(len(item["lines"]) for item in references)
    print(f"wrote {args.count} frames, {lines_total} reference line(s) to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
