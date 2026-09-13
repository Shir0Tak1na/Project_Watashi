#!/usr/bin/env python3
"""Headless verification of the presentation spec and its layout maths.

Layout bugs are the quiet kind: an inverted anchor still produces output that
looks plausible, so eyeballing a screenshot proves very little. This asserts
concrete coordinates instead, with an injected text measurer so no font toolkit
and no window are involved. It loads no models and touches no screen, so it runs
in well under a second.

    run.cmd selfcheck_presentation
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.events import TranslatedLine
from watashi.presentation import (
    BackgroundSpec,
    ElementSpec,
    FontSpec,
    LayoutSpec,
    PresentationSpec,
    compute_blocks,
    measure_block_text,
)




def fixed_measure(char_w: int = 10, line_h: int = 20):
    """Deterministic measurer: every character is the same width."""

    def measure(text: str, size: int, bold: bool) -> tuple[int, int]:
        del size, bold
        return len(text) * char_w, line_h

    return measure


SCREEN = (2560, 1600)
REGION = (400, 1150)
LINES = [
    TranslatedLine(source="AAAA", target="甲甲", box=(24, 88, 640, 40)),
    TranslatedLine(source="BB", target="乙乙乙", box=(24, 140, 260, 40)),
]


def blocks_for(spec: PresentationSpec, **overrides):
    lines = overrides.pop("lines", LINES)
    kwargs = dict(
        source_text="\n".join(l.source for l in lines),
        target_text="\n".join(l.target for l in lines),
        lines=lines,
        screen=SCREEN,
        region_origin=REGION,
        measure=fixed_measure(),
    )
    kwargs.update(overrides)
    return compute_blocks(spec, **kwargs)


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Presentation spec self check (no screen, no fonts, no models)")
    print("=" * 78)

    print("")
    print("-- spec serialisation --")
    for name in PresentationSpec.preset_names():
        spec = PresentationSpec.preset(name)
        data = spec.to_dict()
        check.check(
            f"preset {name!r} round-trips exactly",
            PresentationSpec.from_dict(data).to_dict() == data,
        )
    check.check(
        "every preset declares a known mode",
        all(
            PresentationSpec.preset(n).layout.mode
            in ("bar", "lines", "inplace", "panel", "hidden")
            for n in PresentationSpec.preset_names()
        ),
    )
    check.check("unknown preset is rejected with a helpful message", _raises(lambda: PresentationSpec.preset("nope")))
    check.check(
        "garbage input degrades to a valid spec",
        PresentationSpec.from_dict({"layout": {"mode": "bogus", "anchor": "nowhere"}}).layout.mode
        == "bar",
    )
    check.check(
        "out of range values are clamped",
        PresentationSpec.from_dict({"layout": {"max_width_ratio": 99}}).layout.max_width_ratio == 1.0,
    )

    print("")
    print("-- deep merge (what live editing sends) --")
    base = PresentationSpec.preset("bar")
    merged = base.merged({"layout": {"mode": "inplace"}, "background": {"opacity": 0.5}})
    check.check("a partial change applies", merged.layout.mode == "inplace")
    check.check("untouched fields survive the merge", merged.layout.anchor == base.layout.anchor,
                f"anchor={merged.layout.anchor}")
    check.check("nested fields merge rather than replace", merged.background.opacity == 0.5
                and merged.background.kind == base.background.kind)
    check.check("elements are preserved", len(merged.elements) == len(base.elements))

    print("")
    print("-- anchoring (asserted coordinates, not eyeballed) --")
    spec = PresentationSpec.preset("minimal")  # one element, deterministic size
    spec.elements = [ElementSpec(role="target", font=FontSpec(size=10), color="#fff")]
    spec.background = BackgroundSpec(kind="plate", padding=(0, 0))
    spec.layout = LayoutSpec(mode="bar", anchor="bottom-center", offset=(0, 0))
    blocks = blocks_for(spec, lines=[TranslatedLine(source="", target="AAAA", box=None)])
    check.check("bottom-center produces exactly one block", len(blocks) == 1)
    if blocks:
        block = blocks[0]
        expected_x = (SCREEN[0] - block.width) // 2
        expected_y = SCREEN[1] - block.height
        check.check(
            "bottom-center x is horizontally centred",
            block.x == expected_x,
            f"x={block.x} expected={expected_x}",
        )
        check.check(
            "bottom-center y sits on the bottom edge",
            block.y == expected_y,
            f"y={block.y} expected={expected_y} (a swapped anchor would centre it at "
            f"{(SCREEN[1] - block.height) // 2})",
        )

    spec.layout = LayoutSpec(mode="bar", anchor="top-left", offset=(0, 0))
    blocks = blocks_for(spec, lines=[TranslatedLine(source="", target="AAAA", box=None)])
    check.check("top-left pins to the origin", blocks and blocks[0].x == 0 and blocks[0].y == 0,
                f"({blocks[0].x},{blocks[0].y})" if blocks else "no blocks")

    spec.layout = LayoutSpec(mode="bar", anchor="top-right", offset=(-24, 24))
    blocks = blocks_for(spec, lines=[TranslatedLine(source="", target="AAAA", box=None)])
    if blocks:
        block = blocks[0]
        check.check(
            "top-right uses the right edge and applies the offset",
            block.x == SCREEN[0] - block.width - 24 and block.y == 24,
            f"({block.x},{block.y})",
        )
    check.check(
        "every anchor produces an in-bounds block",
        all(
            0 <= b.x and b.right <= SCREEN[0] and 0 <= b.y and b.bottom <= SCREEN[1]
            for anchor in (
                "top-left", "top-center", "top-right",
                "middle-left", "middle-center", "middle-right",
                "bottom-left", "bottom-center", "bottom-right",
            )
            for b in blocks_for(
                PresentationSpec.from_dict({**spec.to_dict(), "layout": {"anchor": anchor, "offset": [0, 0]}}),
                lines=[TranslatedLine(source="", target="AAAA", box=None)],
            )
        ),
    )

    print("")
    print("-- in-place layout needs geometry and uses it --")
    spec = PresentationSpec.preset("inplace")
    blocks = blocks_for(spec)
    check.check("inplace declares that it needs geometry", spec.needs_geometry)
    check.check("one block per line", len(blocks) == 2, f"{len(blocks)} block(s)")
    if len(blocks) == 2:
        pad_x, pad_y = spec.background.padding
        expected = [
            (REGION[0] + LINES[0].box[0] - pad_x, REGION[1] + LINES[0].box[1] - pad_y),
            (REGION[0] + LINES[1].box[0] - pad_x, REGION[1] + LINES[1].box[1] - pad_y),
        ]
        actual = [(b.x, b.y) for b in blocks]
        check.check("blocks land on region_origin + box", actual == expected,
                    f"actual={actual} expected={expected}")
        check.check(
            "the plate is at least as wide as the original line's box, so the "
            "source glyphs are covered",
            all(b.width >= line.box[2] for b, line in zip(blocks, LINES)),
            f"widths={[b.width for b in blocks]} boxes={[l.box[2] for l in LINES]}",
        )
        check.check("lines are ordered top to bottom", blocks[0].y < blocks[1].y)

    no_geometry = [TranslatedLine(source="AA", target="BB", box=None)]
    fallback = blocks_for(spec, lines=no_geometry)
    check.check(
        "inplace without geometry still draws (falls back to an anchor, never silent)",
        len(fallback) == 1,
        "silently drawing nothing would look like a bug",
    )

    print("")
    print("-- lines layout stacks one block per recognised line --")
    spec = PresentationSpec.preset("lines")
    blocks = blocks_for(spec)
    check.check("one block per line", len(blocks) == 2, f"{len(blocks)} block(s)")
    if len(blocks) == 2:
        gap = spec.layout.line_gap
        check.check("blocks are stacked with the configured gap",
                    blocks[1].y == blocks[0].bottom + gap,
                    f"{blocks[0].bottom} + {gap} == {blocks[1].y}")

    print("")
    print("-- element selection --")
    full = PresentationSpec.preset("bar")
    minimal = PresentationSpec.preset("minimal")
    multi = blocks_for(full, lines=[TranslatedLine(source="AA", target="BB", box=None)])
    single = blocks_for(minimal, lines=[TranslatedLine(source="AA", target="BB", box=None)])
    check.check("bar draws both source and target",
                len(multi[0].texts) == 2, f"{[t.role for t in multi[0].texts]}")
    check.check("minimal draws only the target",
                len(single[0].texts) == 1 and single[0].texts[0].role == "target",
                f"{[t.role for t in single[0].texts]}")

    custom = PresentationSpec.from_dict({
        "elements": [
            {"role": "trace", "order": 0, "font": {"size": 9}, "color": "#888"},
            {"role": "target", "order": 1, "font": {"size": 30, "bold": True}},
        ]
    })
    trace_blocks = blocks_for(custom, lines=[TranslatedLine(source="AA", target="BB", box=None)],
                              trace="'AA'->'BB'[corpus 0.95]")
    check.check("a custom element set is honoured in order",
                [t.role for t in trace_blocks[0].texts] == ["trace", "target"],
                f"{[t.role for t in trace_blocks[0].texts]}")

    print("")
    print("-- opacities and outlines --")
    bare = PresentationSpec.preset("bare")
    bare_blocks = blocks_for(bare, lines=[TranslatedLine(source="AA", target="BB", box=None)])
    check.check("bare uses no background plate", bare.background.kind == "none")
    check.check("bare outlines its text",
                all(t.outline_width > 0 and t.outline_color for t in bare_blocks[0].texts),
                f"widths={[t.outline_width for t in bare_blocks[0].texts]}")
    dimmed = blocks_for(bare, lines=[TranslatedLine(source="AA", target="BB", box=None)], dim=True)
    target_text = [t for t in dimmed[0].texts if t.role == "target"][0]
    check.check("low confidence dims the target", target_text.opacity < 1.0,
                f"opacity={target_text.opacity}")

    print("")
    print("-- multi-line text measurement --")
    measure = fixed_measure(char_w=10, line_h=20)
    check.check("single line measured as one line",
                measure_block_text("AAAA", 10, False, measure) == (40, 20))
    check.check("newlines widen to the longest line and sum the heights",
                measure_block_text("AA\nAAAA\nA", 10, False, measure) == (40, 60))
    wrapped = blocks_for(
        PresentationSpec.preset("bar"),
        lines=[TranslatedLine(source="AAAA\nAAAA", target="B", box=None)],
    )
    check.check("a multi-line block is tall enough for every line",
                wrapped[0].height >= 60, f"height={wrapped[0].height}")

    print("")
    print("-- hidden mode --")
    check.check("hidden draws nothing", blocks_for(PresentationSpec.preset("hidden")) == [])

    return check.report()


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
