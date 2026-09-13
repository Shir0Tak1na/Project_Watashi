#!/usr/bin/env python3
"""Project Watashi -- real time screen translation prototype (CLI).

Examples
--------
Self test, no screen needed, proves OCR + corpus + rules work::

    run.cmd --selftest

List monitors, then run with the default bottom strip region::

    run.cmd --list-monitors
    run.cmd --mode both

Pick a region by dragging on screen, then run::

    run.cmd --select

Headless: print recognised lines and translations, no overlay at all::

    run.cmd --mode none --duration 20 --print

Measure the latency budget without drawing anything::

    run.cmd --mode none --duration 30 --stats-json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

# allow running as a plain script from the prototype directory:
#   run.cmd --selftest        (see prototype/run.cmd)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.adapters import attach_console, attach_overlay  # noqa: E402
from watashi.capture import Region, RegionCapturer, list_monitors  # noqa: E402
from watashi.config import AppConfig  # noqa: E402
from watashi.overlay import Overlay  # noqa: E402
from watashi.presentation import PresentationSpec, resolve_presentation  # noqa: E402
from watashi.profiles import apply_profile, describe_profiles, find_profile  # noqa: E402
from watashi.selftest import run_selftest  # noqa: E402
from watashi import RELEASE_STAGE, __version__  # noqa: E402
from watashi.session import Session  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watashi_proto",
        description="Project Watashi -- local real time screen translation prototype",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument("--selftest", action="store_true", help="run the headless self test and exit")
    parser.add_argument("--list-monitors", action="store_true", help="list monitors and exit")
    parser.add_argument("--select", action="store_true", help="drag to choose a region, then run")

    capture = parser.add_argument_group("capture")
    capture.add_argument("--region", type=str, default=None, help="region as 'x,y,w,h'")
    capture.add_argument("--window", type=str, default=None,
                         help="capture a window instead of a region: its list index or a title substring")
    capture.add_argument("--window-subregion", type=str, default=None,
                         help="capture only part of the window, as fractions "
                              "'left,top,right,bottom' (e.g. 0,0.6,1,1 for the bottom 40%%)")
    capture.add_argument("--list-windows", action="store_true", default=None,
                         help="list capturable windows and exit")
    capture.add_argument("--monitor", type=int, default=None, help="monitor index (0 = all, 1 = primary)")
    capture.add_argument("--fps", type=float, default=None, help="target capture rate")
    capture.add_argument("--diff-threshold", type=float, default=None, help="skip OCR below this frame difference")
    capture.add_argument("--max-width", type=int, default=None, help="downscale frames wider than this before OCR")
    capture.add_argument("--min-ocr-interval", type=float, default=None, help="minimum seconds between OCR runs")
    capture.add_argument("--settle-ms", type=float, default=None,
                         help="hold OCR back until the frame has been still this many "
                              "milliseconds. 0 (the default) recognises the changing "
                              "frame itself, which captures moving glyphs; a non-zero "
                              "value is what makes scrolling text and danmaku legible")

    ocr_group = parser.add_argument_group("ocr")
    ocr_group.add_argument("--ocr-threads", type=int, default=None, help="ONNX intra-op threads (default 4; more is slower)")
    ocr_group.add_argument("--det-limit-type", choices=["min", "max"], default=None, help="detector resize limit; 'max' avoids upscaling strips")
    ocr_group.add_argument("--no-mem-arena", action="store_true", default=None, help="disable the ONNX CPU memory arena (slower)")

    translation = parser.add_argument_group("translation")
    translation.add_argument("--target", type=str, default=None, help="target language, e.g. zh-CN or en")
    translation.add_argument("--source", type=str, default=None, help="source language or 'auto'")
    translation.add_argument("--llm-model", type=str, default=None, help="path to a local GGUF model (optional)")
    translation.add_argument("--nmt-model", type=str, default=None, help="path to a local CTranslate2 model directory")
    translation.add_argument("--no-nmt", action="store_true", default=None, help="disable the local model, corpus + rules only")

    overlay = parser.add_argument_group("overlay")
    overlay.add_argument("--mode", choices=["bar", "panel", "both", "none"], default=None, help="what to display")
    overlay.add_argument("--presentation", type=str, default=None,
                         help="presentation preset: " + ", ".join(PresentationSpec.preset_names()))
    overlay.add_argument("--subtitle-style", choices=["plate", "bare"], default=None,
                         help="legacy shorthand for --presentation")
    overlay.add_argument("--subtitle-size", type=int, default=None, help="scale every element's font size")
    overlay.add_argument("--bar-alpha", type=float, default=None, help="background plate opacity")
    overlay.add_argument("--list-presentations", action="store_true", default=None,
                         help="list presentation presets and exit")

    profile_group = parser.add_argument_group("profiles")
    profile_group.add_argument("--profile", type=str, default=None,
                               help="apply a profile: lean, balanced, full, or a file in profiles/")
    profile_group.add_argument("--list-profiles", action="store_true", default=None,
                               help="list profiles and exit")

    plugin_group = parser.add_argument_group("plugins")
    plugin_group.add_argument("--list-plugins", action="store_true", default=None,
                              help="list discovered plugins, extension points and failures")
    plugin_group.add_argument("--no-plugins", action="store_true", default=None,
                              help="skip plugin discovery entirely")
    plugin_group.add_argument("--export", type=str, default=None,
                              help="on exit, export the session history in this format "
                                   "(see --list-plugins for the available formats)")
    plugin_group.add_argument("--export-out", type=Path, default=None,
                              help="where --export writes; omit to print to stdout")
    plugin_group.add_argument("--export-options", type=str, default=None,
                              help="JSON object of options passed to the export plugin")

    desktop = parser.add_argument_group("desktop UI")
    desktop.add_argument("--desktop", action="store_true", default=None,
                         help="open the desktop control window (and the overlay, if "
                              "--mode is not 'none', sharing one Tk root)")
    desktop.add_argument("--no-overlay-with-desktop", action="store_true", default=None,
                         help="with --desktop, run the control window without a floating overlay")

    serve = parser.add_argument_group("web panel")
    serve.add_argument("--serve", action="store_true", default=None,
                       help="start the local web panel in-process")
    serve.add_argument("--port", type=int, default=None, help="web panel port (default 8765)")
    serve.add_argument("--host", type=str, default=None,
                       help="web panel bind address; defaults to 127.0.0.1. "
                            "Anything else exposes the panel on the network")

    parser.add_argument(
        "--version",
        action="version",
        version=f"Project Watashi {__version__} -- {RELEASE_STAGE}",
        help="print the version and exit",
    )
    run = parser.add_argument_group("run")
    run.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    run.add_argument("--print", dest="print_lines", action="store_true", default=None, help="echo lines to the console")
    run.add_argument("--print-trace", action="store_true", default=None, help="echo per span provenance")
    run.add_argument("--print-stats", action="store_true", default=None, help="echo stats events")
    run.add_argument("--json-lines", action="store_true", default=None,
                     help="emit engine events as JSON lines on stdout (machine readable)")
    run.add_argument("--synthetic", action="store_true", default=None,
                     help="use synthetic frames instead of the screen (no capture needed)")
    run.add_argument("--stats-json", type=Path, default=None, help="write final latency stats as JSON")
    run.add_argument("--quiet", action="store_true", help="suppress the startup banner")

    controls = parser.add_argument_group("controls")
    controls.add_argument("--hotkeys", action="store_true", default=None,
                          help="enable global hotkeys (default on when an overlay is shown)")
    controls.add_argument("--no-hotkeys", action="store_true", default=None,
                          help="disable global hotkeys")
    controls.add_argument("--hotkey-pause", type=str, default=None,
                          help="pause/resume recognition (default ctrl+alt+p)")
    controls.add_argument("--hotkey-hide", type=str, default=None,
                          help="hide/show the overlay (default ctrl+alt+h)")
    controls.add_argument("--hotkey-quit", type=str, default=None,
                          help="quit (default ctrl+alt+q)")
    controls.add_argument("--hotkey-reselect", type=str, default=None,
                          help="drag a new capture region while running (default ctrl+alt+r)")
    controls.add_argument("--stdin-controls", action="store_true", default=None,
                          help="read p/r/q commands from stdin while running")
    return parser


def _scale_presentation_fonts(config: AppConfig, size: int) -> None:
    """Apply ``--subtitle-size`` to the target element of the presentation."""
    from watashi.presentation import PresentationSpec

    spec = PresentationSpec.from_dict(config.presentation) if not isinstance(
        config.presentation, str
    ) else PresentationSpec.preset(config.presentation)
    for element in spec.elements:
        if element.role == "target":
            element.font.size = size
    config.presentation = spec.to_dict()


def _set_background_opacity(config: AppConfig, opacity: float) -> None:
    from watashi.presentation import PresentationSpec

    spec = PresentationSpec.from_dict(config.presentation) if not isinstance(
        config.presentation, str
    ) else PresentationSpec.preset(config.presentation)
    spec.background.opacity = opacity
    config.presentation = spec.to_dict()


def apply_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    if args.region:
        config.region = Region.parse(args.region)
    if args.monitor is not None:
        config.capture["monitor"] = args.monitor
    if args.fps is not None:
        config.capture["fps"] = args.fps
    if args.diff_threshold is not None:
        config.capture["diff_threshold"] = args.diff_threshold
    if args.max_width is not None:
        config.capture["max_width"] = args.max_width
    if args.min_ocr_interval is not None:
        config.capture["min_ocr_interval"] = args.min_ocr_interval
    if args.settle_ms is not None:
        config.capture["settle_ms"] = args.settle_ms

    if args.ocr_threads is not None:
        config.ocr["intra_op_threads"] = args.ocr_threads
    if args.det_limit_type is not None:
        config.ocr["det_limit_type"] = args.det_limit_type
    if args.no_mem_arena:
        config.ocr["use_mem_arena"] = False

    if args.target is not None:
        config.translation["target"] = args.target
    if args.source is not None:
        config.translation["source"] = args.source
    if args.llm_model is not None:
        config.translation["llm_model"] = args.llm_model
    if args.nmt_model is not None:
        config.translation["nmt_model"] = args.nmt_model
    if args.no_nmt:
        config.translation["nmt_model"] = None

    if args.mode is not None:
        config.overlay["mode"] = args.mode
    if args.presentation is not None:
        config.presentation = args.presentation
    if args.subtitle_style is not None:
        # legacy shorthand: keep existing config files meaningful
        config.presentation = args.subtitle_style
    if args.subtitle_size is not None:
        _scale_presentation_fonts(config, args.subtitle_size)
    if args.bar_alpha is not None:
        _set_background_opacity(config, args.bar_alpha)

    if args.profile is not None:
        config.profile = args.profile

    if args.print_lines is not None:
        config.logging["print_lines"] = args.print_lines
    if args.print_trace is not None:
        config.logging["print_trace"] = args.print_trace
    if args.print_stats is not None:
        config.logging["print_stats"] = args.print_stats
    if args.no_plugins:
        config.plugins["enabled"] = False
    return config


def _physical_screen_size(config: AppConfig) -> tuple[int, int] | None:
    """The monitor's real pixel size, so the overlay can convert to Tk's units.

    Tk reports a DPI-scaled logical size while capture and OCR use physical
    pixels; without this the overlay cannot tell that the two disagree.
    """
    try:
        monitors = list_monitors()
        index = config.monitor if 0 <= config.monitor < len(monitors) else 1
        monitor = monitors[index] if index < len(monitors) else monitors[-1]
        return (int(monitor["width"]), int(monitor["height"]))
    except Exception:
        return None


def _install_hotkeys(args: Any, session: Session, overlay: Any, request_stop: Any) -> Any:
    """Register the global hotkeys, unless they are switched off.

    Defaults on whenever an overlay is shown, because that is the only
    configuration where the user has no other way to reach the engine: the
    overlay passes mouse input straight through, so there is no button to click
    and no window to focus.
    """
    enabled = bool(args.hotkeys) or (overlay is not None and not args.no_hotkeys)
    if not enabled:
        return None

    from watashi.hotkeys import HotkeyManager

    manager = HotkeyManager()

    def toggle_pause() -> None:
        result = session.command("toggle_pause")
        state = "已暂停" if result.get("paused") else "识别中"
        session.set_status(state)
        print(f"  [hotkey] {state}")

    def toggle_visibility() -> None:
        if overlay is None:
            return
        overlay.toggle_visibility()
        print("  [hotkey] overlay visibility toggled")

    def quit_now() -> None:
        print("  [hotkey] quit")
        request_stop()

    manager.add(args.hotkey_pause or "ctrl+alt+p", toggle_pause)
    if overlay is not None:
        manager.add(args.hotkey_hide or "ctrl+alt+h", toggle_visibility)

        def reselect() -> None:
            # drawn on the Tk thread; this callback is the hotkey thread, so it
            # only asks, it does not touch Tk itself
            print("  [hotkey] drag a new capture region (Esc cancels)")
            overlay.request_reselect()

        manager.add(args.hotkey_reselect or "ctrl+alt+r", reselect)
    manager.add(args.hotkey_quit or "ctrl+alt+q", quit_now)

    if manager.start():
        print("  global hotkeys:")
        for line in manager.describe():
            print(f"    {line}")
        for failure in manager.failures:
            print(f"    warning: {failure}")
    else:
        print("  global hotkeys unavailable:")
        for failure in manager.failures:
            print(f"    {failure}")
        print("    use --stdin-controls, or the pause control on the panel")
        return None
    return manager


def _start_stdin_controls(session: Session, request_stop: Any, overlay: Any) -> None:
    """Read simple commands from stdin on a background thread.

    A fallback for environments where global hotkeys cannot be registered, and
    the natural control when the CLI is already sitting in a terminal.
    """
    import threading

    def loop() -> None:
        print("  stdin controls: p = pause/resume, h = hide/show, s = status, q = quit")
        try:
            for line in sys.stdin:
                word = line.strip().lower()
                if word in ("p", "pause", "toggle"):
                    result = session.command("toggle_pause")
                    print(f"    {'paused' if result.get('paused') else 'running'}")
                elif word in ("h", "hide") and overlay is not None:
                    overlay.toggle_visibility()
                    print("    overlay toggled")
                elif word in ("s", "status"):
                    print("    " + session.stats_dict().__str__()[:200])
                elif word in ("q", "quit", "exit"):
                    request_stop()
                    return
                elif word:
                    print(f"    unknown command {word!r}")
        except Exception:
            return

    threading.Thread(target=loop, name="watashi-stdin", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_monitors:
        for index, monitor in enumerate(list_monitors()):
            label = "all combined" if index == 0 else f"monitor {index}"
            print(
                f"[{index}] {label:14s} {monitor['width']}x{monitor['height']} "
                f"at ({monitor['left']},{monitor['top']})"
            )
        return 0

    if args.list_windows:
        from watashi import winutil

        windows = winutil.list_windows(exclude_pid=os.getpid())
        if not windows:
            print("no capturable windows found")
            return 1
        print("capturable windows (largest first):")
        for line in winutil.describe_windows(windows):
            print(f"  {line}")
        print("")
        print("  pick one with:  prototype\\run.cmd --window <index>")
        print("                  prototype\\run.cmd --window \"part of the title\"")
        print("")
        print("  note: input-method hosts and GPU overlay windows appear here too;")
        print("        pick by title. Our own windows are excluded on purpose.")
        return 0

    if args.list_presentations:
        print("presentation presets:")
        for name in PresentationSpec.preset_names():
            spec = PresentationSpec.preset(name)
            note = " (needs text geometry)" if spec.needs_geometry else ""
            print(f"  {name:9s} mode={spec.layout.mode:8s} per_line={spec.per_line!s:5s}{note}")
        return 0

    config = apply_overrides(AppConfig.load(args.config), args)

    if args.list_profiles:
        print("profiles:")
        for line in describe_profiles(config):
            print(f"  {line}")
        return 0

    if args.list_plugins:
        from watashi.plugins import EXTENSION_POINTS, PluginRegistry, plugin_directories

        registry = PluginRegistry()
        directories = plugin_directories(config)
        print("plugin directories:")
        for directory in directories:
            state = "present" if directory.is_dir() else "missing"
            print(f"  [{state}] {directory}")
        print("")
        if not config.plugins.get("enabled", True):
            print("plugins are disabled in config")
            return 0
        registry.load_all(directories)
        print(f"loaded {sum(1 for p in registry.plugins if p.ok)} plugin(s):")
        for line in registry.describe():
            print(f"  {line}")
        print("")
        print("extension points:")
        for name, (signature, wired) in sorted(EXTENSION_POINTS.items()):
            state = "connected" if wired else "RESERVED (not called yet)"
            count = len(registry.implementations(name))
            print(f"  {name:14s} {signature:34s} [{state}] {count} implementation(s)")
        print("")
        formats = registry.export_formats()
        print(f"export formats: {', '.join(formats) if formats else '(none)'}")
        print("")
        print("  note: plugins run in this process. Python cannot sandbox them, so")
        print("        they are for your own machine, not for distribution.")
        return 0

    if args.profile:
        profile = find_profile(config, args.profile)
        if profile is None:
            print(f"no profile named {args.profile!r}; see --list-profiles")
            return 1
        for change in apply_profile(config, profile):
            print(f"  profile {profile.name}: {change}")

    if args.selftest:
        from watashi.session import build_ocr, build_translator

        report = run_selftest(
            build_translator(config), build_ocr(config), config.target_lang
        )
        print(report.render())
        # A broken model path used to exit 0 because the per-line exception was
        # caught and merely printed, so the self test reported a pass while the
        # model path was dead. Fail loudly instead.
        if report.model_errors:
            print(f"  FAILED: {report.model_errors} error(s) on the model path")
            return 1
        return 0

    if args.select:
        from watashi.selector import select_region

        selected = select_region(physical_screen=_physical_screen_size(config))
        if selected is None:
            print("selection cancelled")
            return 1
        config.region = selected
        print(f"selected region: {selected}  (physical pixels)")

    # ---- build the engine ------------------------------------------------- #
    # Every surface below is an adapter over the Session event stream: the CLI,
    # the overlay and (later) the web panel all consume identical events, so
    # there is exactly one engine code path.
    capturer = None
    if args.synthetic:
        from watashi.synth import SyntheticCapturer

        capturer = SyntheticCapturer(hold_seconds=1.5)
        print("  using synthetic frames (no screen capture)")
    elif args.window:
        from watashi import winutil
        from watashi.capture import WindowCapturer, WindowUnavailable

        try:
            sub_region = (
                WindowCapturer.parse_sub_region(args.window_subregion)
                if args.window_subregion
                else None
            )
            # our own windows are never valid targets: capturing the overlay would
            # rebuild the feedback loop the capture exclusion exists to prevent
            capturer = WindowCapturer(
                spec=args.window, exclude_pid=os.getpid(), sub_region=sub_region
            )
        except (WindowUnavailable, ValueError) as exc:
            print(f"  cannot capture {args.window!r}: {exc}")
            print("  run --list-windows to see what is available")
            return 1
        print(f"  capturing window: {capturer.describe()}")
        if capturer.region is not None:
            config.region = capturer.region

    session = Session(config, capturer=capturer)

    if not args.quiet:
        print("=" * 72)
        print("Project Watashi -- local real time screen translation prototype")
        print("=" * 72)
        for line in config.describe():
            print(f"  {line}")
        print("=" * 72)

    session.build()
    print(f"  OCR engine ready in {session.ocr.load_ms:.0f} ms")
    if getattr(session.translator, "model_available", False):
        print("  local model loaded; refinements run in the background")

    # ---- attach surfaces -------------------------------------------------- #
    # Defined before the surfaces because the desktop window's close handler needs
    # to signal shutdown, and it is built while the surfaces are being wired.
    stopping = {"flag": False}

    def request_stop(*_args: object) -> None:
        stopping["flag"] = True

    # One queue PER surface.
    #
    # This used to be a single `session.subscribe()` shared by the console and the
    # overlay, which is a race: a queue delivers each item to exactly one consumer,
    # and both adapters poll it from their own thread. So every event went to
    # whichever thread got there first.
    #
    # The visible damage was not subtle. `ready` is published once, at session
    # start, and it is what tells the overlay where the capture region is; if the
    # console won that race the overlay's region_box stayed None, in-place layout
    # fell back to the screen origin, and the translation blocks were drawn in the
    # wrong place. Subtitle events were split between the two consumers in the same
    # way, so each surface saw roughly half of them.
    adapters: list[Any] = []

    console = attach_console(
        session.subscribe(),
        json_lines=bool(args.json_lines),
        print_lines=bool(config.logging.get("print_lines")),
        print_refined=bool(config.logging.get("print_refined", True)),
        print_trace=bool(config.logging.get("print_trace")),
        print_stats=bool(config.logging.get("print_stats")),
    )
    adapters.append(console)

    overlay: Overlay | None = None
    mode = str(config.overlay.get("mode", "both"))
    desktop_ui = bool(args.desktop)
    if desktop_ui and args.no_overlay_with_desktop:
        mode = "none"
    if mode not in ("none", "off"):
        spec = resolve_presentation(config)
        # "panel" as an overlay mode means the history panel; the presentation
        # spec decides how the subtitle itself is drawn
        spec_for_overlay = (
            PresentationSpec.preset("panel") if mode == "panel" else spec
        )
        overlay = Overlay(
            presentation=spec_for_overlay,
            font_family=config.overlay.get("font_family"),
            physical_screen=_physical_screen_size(config),
            panel_width=int(config.overlay.get("panel_width", 540)),
            panel_height=int(config.overlay.get("panel_height", 320)),
            panel_font_size=int(config.overlay.get("panel_size", 13)),
            panel_history=int(config.overlay.get("panel_history", 12)),
            region_box=(
                (config.region.x, config.region.y, config.region.width, config.region.height)
                if config.region
                else None
            ),
        )
        # the panel's pause button issues a command, exactly like any other UI
        def _toggle_pause() -> bool:
            return bool(session.command("toggle_pause").get("paused", False))

        overlay.on_toggle_pause = _toggle_pause

        def _apply_reselection(region: Any) -> None:
            """Push a newly dragged region into the running engine."""
            result = session.command("set_region", {"region": str(region)})
            if result.get("ok"):
                print(f"  region -> {region}  (physical pixels)")
            else:
                print(f"  could not apply the region: {result.get('detail')}")

        overlay.on_reselect = _apply_reselection

        if desktop_ui:
            # The desktop window creates the Tk root; the overlay is started
            # afterwards on the same root. Two Tk() instances would have separate
            # interpreters and the overlay's widgets could not be reached from the
            # main window, so the order matters and is not interchangeable.
            pass
        else:
            overlay.start()
        adapters.append(attach_overlay(overlay, session.subscribe()))
        # a presentation change from any surface repaints this one too
        if mode != "panel":
            session.on_presentation_change(overlay.apply_presentation)

    desktop: Any = None
    if desktop_ui:
        from watashi.desktop import DesktopApp

        desktop = DesktopApp(
            session,
            overlay=overlay,
            title=f"Project Watashi — {config.target_lang}",
            on_quit=request_stop,
        )
        if overlay is not None:
            overlay.start(root=desktop.root)
        desktop.attach()

    for adapter in adapters:
        adapter.start()

    session.start()

    # ---- run -------------------------------------------------------------- #

    try:
        signal.signal(signal.SIGINT, request_stop)
    except (ValueError, OSError):
        pass

    # ---- global hotkeys --------------------------------------------------- #
    # The overlay is click-through, so it can never receive a click of its own.
    # Without a global hotkey there is literally no way to pause it while another
    # application has focus -- which is exactly when it is being used.
    hotkeys = _install_hotkeys(args, session, overlay, request_stop)

    # ---- optional stdin controls ------------------------------------------ #
    if args.stdin_controls:
        _start_stdin_controls(session, request_stop, overlay)

    # ---- web panel -------------------------------------------------------- #
    panel: Any = None
    if args.serve:
        from watashi.web import WebPanel

        host = args.host or "127.0.0.1"
        port = args.port or 8765
        panel = WebPanel(session, host=host, port=port)
        if panel.start() and panel.wait_until_ready():
            print("")
            print(f"  web panel: {panel.url}")
            if panel.exposed:
                print(f"  WARNING: bound to {host}, so the panel is reachable from the")
                print("           network. It has no authentication: only do this on a")
                print("           network you trust, and see docs/presentation-spec.md.")
            else:
                print("  (loopback only; pass --host to expose it on the network)")
        else:
            print("  web panel failed to start; continuing without it")
            panel = None

    deadline = time.perf_counter() + args.duration if args.duration else None
    try:
        if desktop is not None:
            if deadline:
                desktop.root.after(max(1, int(args.duration * 1000)), desktop.close)
            # With a shared root the desktop window owns the mainloop; the overlay
            # installed its own `after` pump on the same root and keeps painting.
            desktop.run()
        elif overlay is not None:
            if deadline:
                overlay._root.after(  # noqa: SLF001 - prototype convenience
                    max(1, int(args.duration * 1000)), overlay.close
                )
            overlay.run()
        else:
            while not stopping["flag"]:
                if deadline and time.perf_counter() >= deadline:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        if desktop is not None and not desktop.closed:
            desktop.close()
        for adapter in adapters:
            adapter.stop()
        if panel is not None:
            panel.stop()
        if hotkeys is not None:
            hotkeys.stop()

    # ---- report ----------------------------------------------------------- #
    stats = session.stats_dict()
    if not args.json_lines:
        print("")
        print("-- session statistics --")
        for key, value in stats.items():
            if key in ("last_lines", "nmt"):
                continue
            print(f"  {key:26s} {value}")
        nmt = stats.get("nmt") or {}
        if nmt:
            print(f"  {'model':26s} {nmt}")
        if stats.get("last_lines"):
            print("  final frame lines:")
            for line in stats["last_lines"]:
                print(f"      | {line}")

    if args.stats_json:
        args.stats_json.parent.mkdir(parents=True, exist_ok=True)
        with args.stats_json.open("w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
        if not args.json_lines:
            print(f"  stats written to {args.stats_json}")

    # ---- export on exit ---------------------------------------------------- #
    if args.export:
        options: dict[str, Any] = {}
        if args.export_options:
            try:
                loaded = json.loads(args.export_options)
                if isinstance(loaded, dict):
                    options = loaded
                else:
                    print(f"  --export-options must be a JSON object, got {type(loaded).__name__}")
            except json.JSONDecodeError as exc:
                print(f"  --export-options is not valid JSON: {exc}")
                return 1
        try:
            # Resolved against the working directory, like --stats-json. The
            # session would otherwise resolve it against the config directory,
            # so the same string meant two different places on the command line.
            export_target = args.export_out.resolve() if args.export_out else None
            text = session.export(args.export, path=export_target, options=options)
        except ValueError as exc:
            print(f"  export failed: {exc}")
            return 1
        if export_target:
            print(f"  exported {len(session.history)} frame(s) to {export_target}")
        elif text:
            # stdout, so the output can be piped
            print("")
            print(text, end="" if text.endswith("\n") else "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
