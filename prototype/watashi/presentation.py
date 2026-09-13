"""The declarative presentation spec -- how customization is expressed.

One description, honoured by every surface. The tkinter overlay renders it, the
web panel renders it, and a future client renders it the same way, so a custom
look is defined once instead of reimplemented per UI.

Why declarative rather than a code renderer interface: a code renderer means
loading user code into the engine process, and Python cannot be sandboxed, which
conflicts with the project's requirement that a bad plugin must not take down the
host and that plugins get least privilege. Declarative specs give most of the
customization without that problem. Code renderers remain possible as a
trust-based extension point (see docs/presentation-spec.md).

The spec is plain JSON-serialisable data so it can travel over the same event
stream as everything else: ``set_presentation`` applies it, and the active spec
is broadcast back so all surfaces stay in sync.

Nothing here imports tkinter or a web framework, so both can consume it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

SPEC_VERSION = 1

#: Where a block sits on screen.
ANCHORS = (
    "top-left",
    "top-center",
    "top-right",
    "middle-left",
    "middle-center",
    "middle-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)

#: Layout modes. ``inplace`` is the one that needs per-line geometry.
MODES = ("bar", "lines", "inplace", "panel", "hidden")

#: Element roles, i.e. which text to draw.
#:
#: `previous` is the line before this one, for a scrolling line that keeps the thread
#: of a conversation when the subtitle replaces itself. It is validated here like any
#: other role: leaving it out of this tuple meant a spec carrying a `previous` element
#: silently lost it on the way back through `from_dict`, which the round-trip
#: assertion in selfcheck_presentation caught.
ROLES = ("source", "target", "trace", "previous")

BACKGROUND_KINDS = ("none", "plate")
ALIGNMENTS = ("left", "center", "right")

_DEFAULT_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Arial",
)


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_pair(value: Any, fallback: tuple[int, int]) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (_as_int(value[0], fallback[0]), _as_int(value[1], fallback[1]))
    return fallback


def _as_string(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# parts
# --------------------------------------------------------------------------- #


@dataclass
class FontSpec:
    family: str | None = None
    size: int = 20
    bold: bool = False
    italic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "size": self.size,
            "bold": self.bold,
            "italic": self.italic,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FontSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            family=data.get("family") or None,
            size=max(4, _as_int(data.get("size"), 20)),
            bold=bool(data.get("bold", False)),
            italic=bool(data.get("italic", False)),
        )


@dataclass
class OutlineSpec:
    color: str = "#000000"
    width: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {"color": self.color, "width": self.width}

    @classmethod
    def from_dict(cls, data: Any) -> "OutlineSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            color=_as_string(data.get("color"), "#000000"),
            width=max(0, _as_int(data.get("width"), 2)),
        )


@dataclass
class ElementSpec:
    """One piece of text inside a block."""

    role: str = "target"
    order: int = 0
    font: FontSpec = field(default_factory=FontSpec)
    color: str = "#ffffff"
    opacity: float = 1.0
    outline: OutlineSpec | None = None
    #: 0 means no limit
    max_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "order": self.order,
            "font": self.font.to_dict(),
            "color": self.color,
            "opacity": round(self.opacity, 3),
            "outline": self.outline.to_dict() if self.outline else None,
            "max_lines": self.max_lines,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ElementSpec":
        if not isinstance(data, dict):
            return cls()
        role = _as_string(data.get("role"), "target")
        if role not in ROLES:
            role = "target"
        outline = data.get("outline")
        return cls(
            role=role,
            order=_as_int(data.get("order"), 0),
            font=FontSpec.from_dict(data.get("font")),
            color=_as_string(data.get("color"), "#ffffff"),
            opacity=_clamp(_as_float(data.get("opacity"), 1.0), 0.0, 1.0),
            outline=OutlineSpec.from_dict(outline) if isinstance(outline, dict) else None,
            max_lines=max(0, _as_int(data.get("max_lines"), 0)),
        )


@dataclass
class BackgroundSpec:
    kind: str = "plate"
    color: str = "#000000"
    opacity: float = 0.72
    radius: int = 8
    padding: tuple[int, int] = (12, 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "color": self.color,
            "opacity": round(self.opacity, 3),
            "radius": self.radius,
            "padding": list(self.padding),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "BackgroundSpec":
        if not isinstance(data, dict):
            return cls()
        kind = _as_string(data.get("kind"), "plate")
        if kind not in BACKGROUND_KINDS:
            kind = "plate"
        return cls(
            kind=kind,
            color=_as_string(data.get("color"), "#000000"),
            opacity=_clamp(_as_float(data.get("opacity"), 0.72), 0.0, 1.0),
            radius=max(0, _as_int(data.get("radius"), 8)),
            padding=_as_pair(data.get("padding"), (12, 8)),
        )


@dataclass
class LayoutSpec:
    mode: str = "bar"
    anchor: str = "bottom-center"
    #: pixels, applied after anchoring; negative y moves up from the bottom
    offset: tuple[int, int] = (0, -90)
    max_width_ratio: float = 0.9
    line_gap: int = 6
    align: str = "left"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "anchor": self.anchor,
            "offset": list(self.offset),
            "max_width_ratio": round(self.max_width_ratio, 3),
            "line_gap": self.line_gap,
            "align": self.align,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LayoutSpec":
        if not isinstance(data, dict):
            return cls()
        mode = _as_string(data.get("mode"), "bar")
        if mode not in MODES:
            mode = "bar"
        anchor = _as_string(data.get("anchor"), "bottom-center")
        if anchor not in ANCHORS:
            anchor = "bottom-center"
        align = _as_string(data.get("align"), "left")
        if align not in ALIGNMENTS:
            align = "left"
        return cls(
            mode=mode,
            anchor=anchor,
            offset=_as_pair(data.get("offset"), (0, -90)),
            max_width_ratio=_clamp(_as_float(data.get("max_width_ratio"), 0.9), 0.05, 1.0),
            line_gap=max(0, _as_int(data.get("line_gap"), 6)),
            align=align,
        )


@dataclass
class ConfidenceSpec:
    """Dim output the pipeline is unsure about, so a reader can see it."""

    dim_below: float = 0.5
    dim_opacity: float = 0.6

    def to_dict(self) -> dict[str, Any]:
        return {"dim_below": round(self.dim_below, 3), "dim_opacity": round(self.dim_opacity, 3)}

    @classmethod
    def from_dict(cls, data: Any) -> "ConfidenceSpec":
        if not isinstance(data, dict):
            return cls()
        return cls(
            dim_below=_clamp(_as_float(data.get("dim_below"), 0.5), 0.0, 1.0),
            dim_opacity=_clamp(_as_float(data.get("dim_opacity"), 0.6), 0.0, 1.0),
        )


# --------------------------------------------------------------------------- #
# the spec
# --------------------------------------------------------------------------- #


@dataclass
class PresentationSpec:
    version: int = SPEC_VERSION
    name: str = "bar"
    layout: LayoutSpec = field(default_factory=LayoutSpec)
    elements: list[ElementSpec] = field(default_factory=list)
    background: BackgroundSpec = field(default_factory=BackgroundSpec)
    confidence: ConfidenceSpec = field(default_factory=ConfidenceSpec)
    #: draw each recognised line as its own block rather than one block per frame
    per_line: bool = False

    # -- serialisation ---------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "layout": self.layout.to_dict(),
            "elements": [e.to_dict() for e in self.ordered_elements()],
            "background": self.background.to_dict(),
            "confidence": self.confidence.to_dict(),
            "per_line": self.per_line,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PresentationSpec":
        if isinstance(data, str):
            return cls.preset(data)
        if not isinstance(data, dict):
            return cls.preset("bar")
        raw_elements = data.get("elements")
        elements = (
            [ElementSpec.from_dict(e) for e in raw_elements]
            if isinstance(raw_elements, list) and raw_elements
            else cls.preset("bar").elements
        )
        return cls(
            version=_as_int(data.get("version"), SPEC_VERSION),
            name=_as_string(data.get("name"), "custom"),
            layout=LayoutSpec.from_dict(data.get("layout")),
            elements=elements,
            background=BackgroundSpec.from_dict(data.get("background")),
            confidence=ConfidenceSpec.from_dict(data.get("confidence")),
            per_line=bool(data.get("per_line", False)),
        )

    # -- helpers ---------------------------------------------------------- #

    def ordered_elements(self) -> list[ElementSpec]:
        return sorted(self.elements, key=lambda e: e.order)

    def element(self, role: str) -> ElementSpec | None:
        for element in self.elements:
            if element.role == role:
                return element
        return None

    @property
    def needs_geometry(self) -> bool:
        """True when the layout cannot work without per-line boxes."""
        return self.layout.mode == "inplace"

    def resolve_font_family(self, available: Sequence[str] | None = None) -> str:
        """Pick a font family: the spec's first choice, else a CJK capable one.

        A spec that names a font the machine does not have should degrade to
        something readable rather than draw blank boxes, so this is resolved
        against the platform's font list.
        """
        for element in self.ordered_elements():
            if element.font.family:
                return element.font.family
        if available:
            lowered = {name.lower() for name in available}
            for candidate in _DEFAULT_FONT_CANDIDATES:
                if candidate.lower() in lowered:
                    return candidate
        return _DEFAULT_FONT_CANDIDATES[-1]

    def merged(self, overrides: dict[str, Any]) -> "PresentationSpec":
        """Apply a partial change, e.g. ``{"layout": {"mode": "inplace"}}``.

        Deep merging matters for live editing: a UI that only wants to change the
        background should not have to resend the whole spec.
        """
        base = self.to_dict()
        _deep_merge(base, overrides or {})
        return PresentationSpec.from_dict(base)

    # -- presets ---------------------------------------------------------- #

    @classmethod
    def preset(cls, name: str) -> "PresentationSpec":
        factory = PRESETS.get(name)
        if factory is None:
            raise ValueError(
                f"unknown presentation preset {name!r}; known: {', '.join(sorted(PRESETS))}"
            )
        return factory()

    @classmethod
    def preset_names(cls) -> list[str]:
        return sorted(PRESETS)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #


def _preset_bar() -> PresentationSpec:
    """The default: previous line above, then source, then the translation large.

    The previous line is dim and small: it is there to keep the thread of a
    conversation when the subtitle replaces itself, not to compete with the line being
    read now. It is a scrolling line rather than a list -- `lines` already stacks
    several recognised lines and `panel` is a full history.
    """
    return PresentationSpec(
        name="bar",
        layout=LayoutSpec(mode="bar", anchor="bottom-center", offset=(0, -90), align="center"),
        elements=[
            ElementSpec(
                role="previous",
                order=0,
                font=FontSpec(size=13),
                color="#7d909f",
                opacity=0.75,
            ),
            ElementSpec(
                role="source",
                order=1,
                font=FontSpec(size=16),
                color="#c9d6e0",
                opacity=0.9,
            ),
            ElementSpec(
                role="target",
                order=2,
                font=FontSpec(size=24, bold=True),
                color="#ffffff",
            ),
        ],
        background=BackgroundSpec(kind="plate", color="#000000", opacity=0.72),
    )


def _preset_bare() -> PresentationSpec:
    """Like ``bar`` but transparent, with outlined text: real subtitle look."""
    spec = _preset_bar()
    spec.name = "bare"
    spec.background = BackgroundSpec(kind="none", opacity=0.0)
    spec.elements = [
        replace(element, outline=OutlineSpec(color="#000000", width=2))
        for element in spec.elements
    ]
    return spec


def _preset_minimal() -> PresentationSpec:
    """Translation only. Sometimes the original is just noise."""
    spec = _preset_bar()
    spec.name = "minimal"
    spec.elements = [element for element in spec.elements if element.role == "target"]
    return spec


def _preset_lines() -> PresentationSpec:
    """One block per recognised line, stacked at the anchor."""
    spec = _preset_bar()
    spec.name = "lines"
    spec.per_line = True
    spec.layout = LayoutSpec(
        mode="lines", anchor="bottom-center", offset=(0, -90), align="left", line_gap=8
    )
    spec.elements = [
        ElementSpec(role="source", order=0, font=FontSpec(size=14), color="#9fb0bd"),
        ElementSpec(
            role="target", order=1, font=FontSpec(size=20, bold=True), color="#ffffff"
        ),
    ]
    return spec


def _preset_inplace() -> PresentationSpec:
    """Draw the translation where the original text was.

    This is the layout that needs per-line geometry. It also has to paint a
    background, because the original glyphs are still on screen underneath --
    ``kind: plate`` with a high opacity is what makes the result readable rather
    than doubled up.
    """
    spec = _preset_bar()
    spec.name = "inplace"
    spec.per_line = True
    spec.layout = LayoutSpec(
        mode="inplace", anchor="top-left", offset=(0, 0), align="left", max_width_ratio=1.0
    )
    spec.background = BackgroundSpec(
        kind="plate", color="#000000", opacity=0.92, radius=4, padding=(6, 3)
    )
    spec.elements = [
        ElementSpec(
            role="target", order=0, font=FontSpec(size=18, bold=True), color="#ffffff"
        ),
    ]
    return spec


def _preset_panel() -> PresentationSpec:
    """Bilingual history with metadata, for review and post-editing."""
    spec = _preset_bar()
    spec.name = "panel"
    spec.per_line = True
    spec.layout = LayoutSpec(
        mode="panel", anchor="top-right", offset=(-24, 24), align="left", max_width_ratio=0.4
    )
    spec.background = BackgroundSpec(kind="plate", color="#101418", opacity=0.93, padding=(10, 6))
    spec.elements = [
        ElementSpec(role="source", order=0, font=FontSpec(size=13), color="#7d909f"),
        ElementSpec(role="target", order=1, font=FontSpec(size=15, bold=True), color="#e6eef5"),
    ]
    return spec


def _preset_hidden() -> PresentationSpec:
    """Recognise and translate but draw nothing."""
    spec = _preset_bar()
    spec.name = "hidden"
    spec.layout = LayoutSpec(mode="hidden")
    return spec


def resolve_presentation(config: Any) -> PresentationSpec:
    """Read the configured presentation and let ``overlay.*`` drive it.

    The spec stays authoritative -- it is what every surface renders -- but three
    config keys now feed into it instead of being read by nobody:
    ``subtitle_size``, ``bar_alpha`` and ``bar_bottom_margin`` existed in the config,
    in the settings page and as command line flags, and only the flags did anything,
    because they rewrite the spec. The same change made from a config file silently
    did nothing, which is the worst of both worlds.

    Their defaults match the presets exactly (24 / 0.72 / 90), so applying them
    unconditionally changes nothing for an untouched config.
    """
    return _apply_overlay_settings(_resolve_base(config), config)


def _apply_overlay_settings(spec: PresentationSpec, config: Any) -> PresentationSpec:
    overlay = getattr(config, "overlay", {}) or {}

    size = overlay.get("subtitle_size")
    target = spec.element("target")
    if size and target is not None:
        base = target.font.size or 24
        try:
            ratio = float(size) / float(base)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 1.0
        if ratio != 1.0 and ratio > 0:
            # Every element scales together, so the source line keeps its proportion
            # to the translation instead of the two drifting apart.
            for element in spec.elements:
                element.font.size = max(6, int(round(element.font.size * ratio)))

    alpha = overlay.get("bar_alpha")
    if alpha is not None and spec.background is not None:
        spec.background.opacity = _clamp(float(alpha), 0.0, 1.0)

    margin = overlay.get("bar_bottom_margin")
    if margin is not None:
        spec.layout.offset = (spec.layout.offset[0], -int(margin))

    return spec


def _resolve_base(config: Any) -> PresentationSpec:
    """The configured presentation: a preset name or a spec object.

    Kept here rather than in ``session`` so that a config carrying a spec dict is
    validated the same way whether it came from a file, a command or a profile.
    """
    raw = getattr(config, "presentation", None)
    if raw is None:
        raw = (getattr(config, "overlay", {}) or {}).get("presentation")
    if isinstance(raw, str) and raw:
        try:
            return PresentationSpec.preset(raw)
        except ValueError:
            return PresentationSpec.preset("bar")
    if isinstance(raw, dict) and raw:
        return PresentationSpec.from_dict(raw)
    # no presentation configured: derive a sensible one from the legacy
    # overlay.subtitle_style setting so existing config files keep working
    style = str((getattr(config, "overlay", {}) or {}).get("subtitle_style", "plate"))
    return PresentationSpec.preset("bare" if style == "bare" else "bar")


PRESETS: dict[str, Callable[[], PresentationSpec]] = {
    "bar": _preset_bar,
    "bare": _preset_bare,
    "hidden": _preset_hidden,
    "inplace": _preset_inplace,
    "lines": _preset_lines,
    "minimal": _preset_minimal,
    "panel": _preset_panel,
}


# --------------------------------------------------------------------------- #
# layout: a pure function, deliberately free of any UI toolkit
# --------------------------------------------------------------------------- #

#: ``measure(text, size, bold) -> (width, height)`` in pixels for a **single
#: line**. Multi-line text is split here rather than delegated, so every surface
#: agrees on how embedded newlines are laid out and a stub measurer is trivial
#: to write for tests.
Measure = Callable[[str, int, bool], tuple[int, int]]


def measure_block_text(text: str, size: int, bold: bool, measure: Measure) -> tuple[int, int]:
    """Measure possibly multi-line text: widest line, and summed line heights."""
    parts = text.split("\n") or [""]
    width = 0
    height = 0
    for part in parts:
        part_w, part_h = measure(part, size, bold)
        width = max(width, part_w)
        height += part_h
    return width, height


@dataclass
class DrawText:
    """One run of text to paint, already positioned."""

    text: str
    x: int
    y: int
    size: int
    bold: bool
    color: str
    opacity: float = 1.0
    outline_color: str | None = None
    outline_width: int = 0
    role: str = "target"


@dataclass
class DrawBlock:
    """A background rectangle plus the text drawn on top of it."""

    x: int
    y: int
    width: int
    height: int
    texts: list[DrawText] = field(default_factory=list)
    background: BackgroundSpec | None = None

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def _anchor_position(
    anchor: str, screen: tuple[int, int], block: tuple[int, int], offset: tuple[int, int]
) -> tuple[int, int]:
    """Top-left corner for a block of the given size at the given anchor.

    Note the order: anchors are written ``<vertical>-<horizontal>``
    ("bottom-center" means bottom edge, horizontally centred). Getting this
    backwards still produces plausible looking output -- everything lands
    centred -- which is exactly the kind of bug that survives eyeballing, so
    ``selfcheck_presentation.py`` asserts concrete coordinates.
    """
    screen_w, screen_h = screen
    block_w, block_h = block
    vertical, _, horizontal = anchor.partition("-")

    if horizontal == "left":
        x = 0
    elif horizontal == "right":
        x = screen_w - block_w
    else:
        x = (screen_w - block_w) // 2

    if vertical == "top":
        y = 0
    elif vertical == "bottom":
        y = screen_h - block_h
    else:
        y = (screen_h - block_h) // 2

    return x + offset[0], y + offset[1]


def _element_text(role: str, source: str, target: str, trace: str,
                  previous: str = "") -> str:
    """The text an element shows.

    ``previous`` is the line before this one. Reading along with a subtitle that
    replaces itself every few seconds loses the thread of the conversation -- a
    scrolling line of what was just said is what keeps it. ``inplace`` does not need
    it (the old text is still on screen under the plate) and ``panel`` already is a
    history, which is why this is a role that presets opt into rather than something
    every preset gets.
    """
    if role == "source":
        return source
    if role == "target":
        return target
    if role == "trace":
        return trace
    if role == "previous":
        return previous
    return target


def compute_blocks(
    spec: PresentationSpec,
    *,
    source_text: str,
    target_text: str,
    lines: Sequence[Any] = (),
    screen: tuple[int, int] = (1920, 1080),
    screen_origin: tuple[int, int] = (0, 0),
    region_origin: tuple[int, int] = (0, 0),
    measure: Measure,
    trace: str = "",
    coverage: float = 1.0,
    dim: bool = False,
    previous: str = "",
) -> list[DrawBlock]:
    """Turn a spec plus a translation into positioned drawing primitives.

    This is a **pure function with no UI toolkit involvement** (text measurement
    is injected), which buys two things:

    * the layout maths is unit testable, unlike the overlay window it feeds;
    * the web panel can compute the identical geometry instead of reimplementing
      anchoring, so the two surfaces genuinely agree.

    ``lines`` supplies per-line geometry. Without boxes, ``inplace`` cannot be
    honoured and degrades to an anchored stack rather than drawing nothing --
    silently showing nothing would look like a bug to the user.

    ``screen`` is the drawing surface size and ``screen_origin`` its top-left in
    the same coordinate space as ``region_origin`` (usually the virtual desktop,
    which can start at a negative x on a multi-monitor setup).
    """
    if spec.layout.mode == "hidden":
        return []

    left, top = screen_origin
    right, bottom = left + screen[0], top + screen[1]

    padding_x, padding_y = spec.background.padding if spec.background else (0, 0)
    ordered = spec.ordered_elements()
    if not ordered:
        return []

    # ---- work out the units: one block per frame, or one per line ---------- #
    units: list[tuple[str, str, tuple[int, int, int, int] | None]] = []
    per_line = spec.per_line and bool(lines)
    if per_line:
        for line in lines:
            units.append((line.source, line.target, line.box))
    else:
        units.append((source_text, target_text, None))

    blocks: list[DrawBlock] = []

    if spec.layout.mode == "inplace":
        # one block per line, pinned to where that line was on screen
        last_bottom = top
        for source, target, box in units:
            texts = _layout_texts(
                ordered, source, target, trace, 0, 0, measure, dim, coverage, spec
            )
            if not texts:
                continue
            text_width = max(
                measure_block_text(t.text, t.size, t.bold, measure)[0] for t in texts
            )
            last = texts[-1]
            text_height = (last.y - texts[0].y) + measure_block_text(
                last.text, last.size, last.bold, measure
            )[1]
            # The plate has to cover the original glyphs, otherwise the source
            # text shows through beside the translation. So the block is at
            # least as wide as the line's own box.
            width = max(text_width, box[2] if box else 0) + padding_x * 2
            height = text_height + padding_y * 2
            if box is not None:
                x = region_origin[0] + box[0] - padding_x
                y = region_origin[1] + box[1] - padding_y
            else:
                # no geometry available: fall back to an anchored position so the
                # text is still shown rather than silently dropped
                x, y = _anchor_position(
                    spec.layout.anchor, screen, (width, height), spec.layout.offset
                )
            x = max(left, min(x, max(left, right - width)))
            y = max(top, min(y, max(top, bottom - height)))
            if y < last_bottom:
                # Clamping can pull several lines onto the same row, which would
                # stack them on top of each other and lose reading order. Nudge
                # the block down instead so the order survives.
                y = min(last_bottom, max(top, bottom - height))
            last_bottom = y + height + spec.layout.line_gap
            _offset_texts(texts, x + padding_x, y + padding_y)
            blocks.append(
                DrawBlock(
                    x=x, y=y, width=width, height=height, texts=texts,
                    background=spec.background,
                )
            )
        return blocks

    # ---- anchored modes: bar / lines / panel ------------------------------- #
    # Build every unit first so the group can be anchored as a whole.
    staged: list[tuple[list[DrawText], int, int]] = []
    for source, target, _box in units:
        texts = _layout_texts(
            ordered, source, target, trace, 0, 0, measure, dim, coverage, spec, previous
        )
        if not texts:
            continue
        width = (
            max(measure_block_text(t.text, t.size, t.bold, measure)[0] for t in texts)
            + padding_x * 2
        )
        last = texts[-1]
        height = (last.y - texts[0].y) + measure_block_text(
            last.text, last.size, last.bold, measure
        )[1] + padding_y * 2
        staged.append((texts, width, height))

    if not staged:
        return []

    max_width = int(screen[0] * spec.layout.max_width_ratio)
    widths = [min(width, max_width) for _texts, width, _h in staged]
    total_height = sum(height for _t, _w, height in staged) + spec.layout.line_gap * (
        len(staged) - 1
    )
    group_width = max(widths)
    group_x, group_y = _anchor_position(
        spec.layout.anchor, screen, (group_width, total_height), spec.layout.offset
    )
    # clamp the group as a whole so the stack never partially leaves the surface
    group_x = max(left, min(group_x, max(left, right - group_width)))
    group_y = max(top, min(group_y, max(top, bottom - total_height)))

    cursor_y = group_y
    for index, (texts, width, height) in enumerate(staged):
        block_width = widths[index]
        if spec.layout.align == "center":
            block_x = group_x + (group_width - block_width) // 2
        elif spec.layout.align == "right":
            block_x = group_x + (group_width - block_width)
        else:
            block_x = group_x
        _offset_texts(texts, block_x + padding_x, cursor_y + padding_y)
        blocks.append(
            DrawBlock(
                x=block_x,
                y=cursor_y,
                width=block_width,
                height=height,
                texts=texts,
                background=spec.background,
            )
        )
        cursor_y += height + spec.layout.line_gap

    return blocks


def _layout_texts(
    elements: Sequence[ElementSpec],
    source: str,
    target: str,
    trace: str,
    x: int,
    y: int,
    measure: Measure,
    dim: bool,
    coverage: float,
    spec: PresentationSpec,
    previous: str = "",
) -> list[DrawText]:
    """Stack the configured elements vertically inside a block."""
    texts: list[DrawText] = []
    cursor = y
    for element in elements:
        value = _element_text(element.role, source, target, trace, previous)
        if not value.strip():
            continue
        opacity = element.opacity
        if dim and element.role == "target":
            opacity = min(opacity, spec.confidence.dim_opacity)
        outline = element.outline
        texts.append(
            DrawText(
                text=value,
                x=x,
                y=cursor,
                size=element.font.size,
                bold=element.font.bold,
                color=element.color,
                opacity=opacity,
                outline_color=outline.color if outline and outline.width else None,
                outline_width=outline.width if outline else 0,
                role=element.role,
            )
        )
        cursor += measure_block_text(value, element.font.size, element.font.bold, measure)[1] + 2
    return texts


def _offset_texts(texts: Sequence[DrawText], dx: int, dy: int) -> None:
    for text in texts:
        text.x += dx
        text.y += dy
