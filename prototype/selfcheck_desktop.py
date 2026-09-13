#!/usr/bin/env python3
"""Desktop UI verification. Needs a display.

The desktop window is the second surface that cannot be asserted headlessly, so
this drives it the way the overlay self check drives the overlay: build the real
window on a real Tk root, push the engine's events through the same queue the
engine would use, and assert what the widgets end up showing.

Three things here exist because they were bugs, not because they were easy to
write:

**The refinement must replace its own line, not append.** The engine publishes a
fast corpus result and then a better model result for the same sentence. Appending
both makes every subtitle appear twice, which a user reads as a duplicate-line
bug rather than as a two-tier display.

**The region selector must not block.** A ``wait_window``-style blocking selector
would keep this window's ``after`` pump from running, so the panel freezes for as
long as the selector is open. The check asserts the selector is built and returns
immediately, with the result delivered through a callback.

**Shared root.** The overlay and the main window must live on one Tk root, because
two ``Tk()`` instances have separate interpreters. Closing the main window must
take the overlay down with it without the overlay trying to destroy a root it does
not own.

    run.cmd selfcheck_desktop
"""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.desktop import DesktopApp  # noqa: E402
from watashi.events import (  # noqa: E402
    CMD_SET_REGION,
    CMD_TOGGLE_PAUSE,
    CMD_USE_WINDOW,
    EVENT_PRESENTATION,
    EVENT_READY,
    EVENT_REFINEMENT,
    EVENT_STATS,
    EVENT_SUBTITLE,
    OverlayStats,
    OverlayUpdate,
    TranslatedLine,
    encode_event,
    encode_refinement,
    encode_stats,
    encode_update,
)
from watashi.presentation import PresentationSpec  # noqa: E402

INFO = {
    "profile": "balanced",
    "region": "600,1200,1280,180",
    "region_box": [600, 1200, 1280, 180],
    "capture_mode": "region",
    "target_lang": "zh-CN",
    "source_lang": "en",
    "fps_target": 10.0,
    "diff_threshold": 2.0,
    "corpus_entries": 42,
    "rules": 3,
    "backend": "corpus+rules+nmt",
    "profiles": ["lean", "balanced", "full"],
    "presentation": PresentationSpec.preset("bar").to_dict(),
    "plugins": {
        "api_version": 1,
        "loaded": 2,
        "failed": 0,
        "connected_points": ["export", "postprocess"],
        "export_formats": ["srt", "glossary"],
        "plugins": [
            {
                "name": "postprocess_tidy", "loaded": True, "error": None,
                "provided_points": ["postprocess"], "description": "rejoin split CJK",
            },
            {
                "name": "export_srt", "loaded": True, "error": None,
                "provided_points": ["export"], "description": "SRT and glossary",
            },
            {
                "name": "broken_thing", "loaded": False, "error": "boom",
                "provided_points": [], "description": "",
            },
        ],
        "failures": {"postprocess_tidy.clean: TypeError": 2},
    },
}

UPDATE = OverlayUpdate(
    source_text="gg wp noob",
    target_text="打得好，打得漂亮 新手",
    coverage=0.9,
    confidence=0.85,
    latency_ms=88.0,
    backend="corpus+rules",
    lines=[
        TranslatedLine(source="gg wp noob", target="打得好，打得漂亮 新手",
                       box=(20, 30, 700, 46), confidence=0.9),
    ],
)

STATS = OverlayStats(
    fps=9.4, ocr_ms=95.0, translate_ms=4.0, total_ms=101.0,
    frames=57, skipped=12, corpus_entries=42, rules=3, cache_hit_rate=0.62,
    backend="corpus+rules+nmt", paused=False, refinements=8,
    refinements_pending=0, refinements_dropped=1, memory_mib=893.0,
)


class FakeSession:
    """The subset of ``Session`` the desktop UI is allowed to touch."""

    def __init__(self) -> None:
        self.channel: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.fail_names: set[str] = set()
        self.presentation = PresentationSpec.preset("bar")
        self.presentation_sinks: list[Any] = []

    def subscribe(self, maxsize: int = 512) -> "queue.Queue[dict[str, Any]]":
        return self.channel

    def unsubscribe(self, channel: Any) -> None:
        pass

    def publish(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        envelope = encode_event(kind, data or {})
        self.channel.put_nowait(envelope)
        return envelope

    def command(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        self.commands.append((name, payload))
        if name in self.fail_names:
            return {"cmd": name, "ok": False, "detail": "refused by the fake session"}
        return {"cmd": name, "ok": True, "detail": "ok"}

    def info(self) -> dict[str, Any]:
        return dict(INFO)

    def on_presentation_change(self, callback: Any) -> None:
        self.presentation_sinks.append(callback)


class FakeOverlay:
    """Stands in for the overlay so the shared-root path can be checked."""

    def __init__(self) -> None:
        self.reselects = 0
        self.closes: list[bool | None] = []

    def request_reselect(self) -> None:
        self.reselects += 1

    def close(self, destroy_root: bool | None = None) -> None:
        self.closes.append(destroy_root)


def drain(app: DesktopApp, rounds: int = 3) -> None:
    """Run the pump and let Tk process the resulting widget work."""
    for _ in range(rounds):
        app._pump()  # noqa: SLF001 - the pump is the unit under test
        app.root.update()


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Desktop UI self check (requires a display)")
    print("=" * 78)

    session = FakeSession()
    overlay = FakeOverlay()
    app = DesktopApp(session, overlay=overlay, title="watashi self check")
    app.attach()
    try:
        # ---------------------------------------------------------------- #
        check.section("the window builds with every tab")
        tabs = [
            app.notebook.tab(i, "text") for i in range(app.notebook.index("end"))
        ]
        check.check(
            "six tabs exist",
            tabs == ["字幕", "采集", "翻译", "呈现", "插件", "诊断"],
            f"got {tabs}",
        )
        check.check("the status strip starts empty-ish", app.status_var.get() != "")
        check.check(
            "the history starts empty",
            app.history == [],
            f"got {app.history!r}",
        )

        # ---------------------------------------------------------------- #
        check.section("a ready event populates the settings views")
        session.publish(EVENT_READY, session.info())
        drain(app)
        check.check(
            "profile shown",
            app.profile_var.get() == "配置档：balanced",
            app.profile_var.get(),
        )
        check.check(
            "region shown",
            app.region_var.get() == "600,1200,1280,180",
            app.region_var.get(),
        )
        check.check(
            "explanation line carries the counts",
            "42 条词条" in app.corpus_var.get() and "3 条规则" in app.corpus_var.get(),
            app.corpus_var.get(),
        )
        profile_buttons = [
            w.cget("text") for w in app.profiles_frame.winfo_children()
        ]
        check.check(
            "one button per profile",
            profile_buttons == ["lean", "balanced", "full"],
            f"got {profile_buttons}",
        )
        preset_buttons = [
            w.cget("text") for w in app.presets_frame.winfo_children()
        ]
        check.check(
            "one button per presentation preset",
            preset_buttons == PresentationSpec.preset_names(),
            f"got {preset_buttons}",
        )
        check.check(
            "presentation summary names the current preset",
            "当前：bar" in app.spec_var.get(),
            app.spec_var.get(),
        )
        export_values = list(app.export_combo.cget("values"))
        check.check(
            "export formats come from the plugin registry",
            export_values == ["srt", "glossary"],
            f"got {export_values}",
        )
        check.check(
            "first export format is preselected",
            app.export_format_var.get() == "srt",
            app.export_format_var.get(),
        )

        plugin_text = app.plugins_box.get("1.0", "end")
        check.check(
            "the plugin tab names each plugin",
            all(name in plugin_text for name in
                ("postprocess_tidy", "export_srt", "broken_thing")),
            plugin_text.splitlines()[:3],
        )
        check.check(
            "a failed plugin is reported, not hidden",
            "boom" in plugin_text,
        )
        check.check(
            "runtime failures are surfaced with a count",
            "2x postprocess_tidy.clean" in plugin_text,
        )

        # ---------------------------------------------------------------- #
        check.section("subtitles, stats and refinement arrive through the queue")
        session.publish(EVENT_SUBTITLE, encode_update(UPDATE))
        session.publish(EVENT_STATS, encode_stats(STATS))
        drain(app)
        check.check(
            "the translation reaches the big label",
            app.current_var.get() == UPDATE.target_text,
            app.current_var.get(),
        )
        check.check(
            "the source line is kept for contrast",
            app.source_var.get() == UPDATE.source_text,
            app.source_var.get(),
        )
        check.check(
            "the history has one entry",
            len(app.history) == 1,
            f"got {len(app.history)}",
        )
        entry = app.history[0]
        check.check(
            "the entry records provenance and timing",
            "语料库" in entry[3] and "88 ms" in entry[3],
            entry[3],
        )
        check.check(
            "the status strip reflects the stats event",
            "9.4 FPS" in app.status_var.get() and "893 MiB" in app.status_var.get(),
            app.status_var.get(),
        )
        stats_text = app.stats_box.get("1.0", "end")
        check.check(
            "the diagnostics tab shows raw counters",
            '"refinements_dropped": 1' in stats_text and '"frames": 57' in stats_text,
            stats_text.splitlines()[:2],
        )
        check.check(
            "the pause button flips with the paused state",
            app.pause_button.cget("text") == "暂停识别",
            app.pause_button.cget("text"),
        )

        # ---------------------------------------------------------------- #
        check.section("a refinement replaces its own line instead of duplicating it")
        session.publish(EVENT_REFINEMENT, encode_refinement(_Refinement(
            UPDATE.source_text, "打得好，打得漂亮。新手。"
        )))
        drain(app)
        check.check(
            "still one entry, not two",
            len(app.history) == 1,
            f"got {len(app.history)}: {app.history}",
        )
        check.check(
            "the model text replaced the corpus text",
            app.history[0][1] == "打得好，打得漂亮。新手。",
            app.history[0][1],
        )
        check.check(
            "the headline label follows the refinement",
            app.current_var.get() == "打得好，打得漂亮。新手。",
            app.current_var.get(),
        )
        session.publish(EVENT_SUBTITLE, encode_update(OverlayUpdate(
            source_text="a second line entirely", target_text="完全另一句",
        )))
        session.publish(EVENT_REFINEMENT, encode_refinement(_Refinement(
            "a second line entirely", "完全另一句（精修）"
        )))
        drain(app)
        check.check(
            "a different sentence does add an entry",
            len(app.history) == 2,
            f"got {len(app.history)}",
        )
        check.check(
            "and it is the refined one that is stored",
            app.history[1][1] == "完全另一句（精修）",
            app.history[1][1],
        )

        # ---------------------------------------------------------------- #
        check.section("controls issue engine commands, not local state")
        session.commands.clear()
        app.toggle_pause()
        check.check(
            "pause routes through the session",
            session.commands == [(CMD_TOGGLE_PAUSE, {})],
            f"got {session.commands}",
        )
        app.target_var.set("ja")
        app.apply_target()
        check.check(
            "the target language is applied as a command",
            session.commands[-1] == ("set_target_lang", {"target_lang": "ja"}),
            f"got {session.commands[-1]}",
        )
        app.fps_var.set("15")
        app.apply_value("set_fps", "15", float)
        check.check(
            "numeric settings are cast before being sent",
            session.commands[-1] == ("set_fps", {"value": 15.0}),
            f"got {session.commands[-1]}",
        )
        count_before = len(session.commands)
        app.apply_value("set_fps", "not-a-number", float)
        check.check(
            "a bad number is rejected without reaching the engine",
            len(session.commands) == count_before,
            f"got {session.commands[count_before:]}",
        )
        check.check(
            "and it is reported in the history rather than swallowed",
            any("无效的数值" in e[1] for e in app.history),
            [e[1] for e in app.history[-2:]],
        )
        app.apply_presentation("inplace")
        check.check(
            "presentation presets are applied by name",
            session.commands[-1] == ("set_presentation", {"preset": "inplace"}),
            f"got {session.commands[-1]}",
        )
        app.region_var.set("0,0,640,360")
        app.apply_value("set_diff_threshold", "1.5", float)
        check.check(
            "the diff threshold is applied",
            session.commands[-1] == ("set_diff_threshold", {"value": 1.5}),
            f"got {session.commands[-1]}",
        )

        # a refused command must be visible, not silently ignored
        session.fail_names.add("reload_corpus")
        session.commands.clear()
        app.reload_corpus()
        check.check(
            "a refused command is surfaced in the history",
            any("refused by the fake session" in e[1] for e in app.history),
            [e[1] for e in app.history[-3:]],
        )
        session.fail_names.discard("reload_corpus")

        # ---------------------------------------------------------------- #
        check.section("reselecting a region does not block the UI")
        session.commands.clear()
        app.select_region()
        check.check(
            "the overlay's own selector is reused when there is one",
            overlay.reselects == 1,
            f"got {overlay.reselects}",
        )
        check.check(
            "and nothing is sent until the user finishes dragging",
            session.commands == [],
            f"got {session.commands}",
        )

        app2 = DesktopApp(FakeSession(), overlay=None, title="selector path")
        app2.attach()
        try:
            start = time.perf_counter()
            app2.select_region()
            elapsed = time.perf_counter() - start
            app2.root.update()
            check.check(
                "with no overlay a selector window is created",
                app2._selector is not None and app2._selector._window is not None,
                f"selector={app2._selector!r}",
            )
            check.check(
                "and it returns immediately instead of blocking",
                elapsed < 1.0,
                f"{elapsed * 1000:.1f} ms",
            )
            selector = app2._selector
            selector.on_done = None
            selector.on_cancel()
            app2.root.update()
            check.check(
                "cancelling closes it",
                selector._window is None,
                f"window={selector._window!r}",
            )
        finally:
            app2.close()

        # ---------------------------------------------------------------- #
        check.section("the window picker drives real window capture")
        from watashi import winutil

        windows = winutil.list_windows(exclude_pid=0)
        check.check(
            "windows are enumerable for the picker",
            len(windows) > 0,
            f"got {len(windows)}",
        )
        if windows:
            order = [w.rect[2] * w.rect[3] for w in windows]
            check.check(
                "the list is largest-first, as the hint claims",
                order == sorted(order, reverse=True),
                f"first three: {order[:3]}",
            )
            from watashi.desktop import _WindowPicker

            dialog = _WindowPicker(app.root, windows[:5])
            check.check(
                "one row per window, plus its size",
                dialog.listbox.size() == min(5, len(windows)),
                f"got {dialog.listbox.size()}",
            )
            check.check(
                "a row names the process, which is what disambiguates twins",
                any("[" in dialog.listbox.get(i) for i in range(dialog.listbox.size())),
                dialog.listbox.get(0),
            )
            check.check(
                "the first row is preselected and double-click accepts",
                dialog.listbox.curselection() == (0,),
                f"got {dialog.listbox.curselection()}",
            )
            dialog._cancel()
            app.root.update()
            check.check(
                "cancelling the picker yields nothing",
                dialog.chosen is None,
                f"got {dialog.chosen!r}",
            )

        # ---------------------------------------------------------------- #
        check.section("use_window really swaps the capture source (engine level)")
        from watashi.capture import RegionCapturer, WindowCapturer
        from watashi.config import AppConfig
        from watashi.session import Session
        from watashi.synth import SyntheticCapturer

        engine = Session(
            AppConfig.load(),
            capturer=SyntheticCapturer(hold_seconds=99, width=640),
            translator=_StubTranslator(),
        )
        engine.pipeline  # build it; no model is loaded until start()
        check.check(
            "use_window is a registered command",
            CMD_USE_WINDOW in Session._handlers(engine),  # noqa: SLF001
        )
        check.check(
            "the session starts on a fixed region",
            engine.info()["capture_mode"] == "region",
            engine.info()["capture_mode"],
        )

        missing = engine.command(CMD_USE_WINDOW, {"spec": "zzz-no-such-window-zzz"})
        check.check(
            "an unknown window is refused rather than silently ignored",
            not missing.get("ok"),
            str(missing.get("detail")),
        )
        check.check(
            "a refused switch leaves the old capturer in place",
            engine.info()["capture_mode"] == "region",
            engine.info()["capture_mode"],
        )
        empty = engine.command(CMD_USE_WINDOW, {})
        check.check(
            "an empty spec is refused with a reason",
            not empty.get("ok") and "spec" in str(empty.get("detail")),
            str(empty.get("detail")),
        )

        if windows:
            target = windows[0]
            switched = engine.command(
                CMD_USE_WINDOW, {"spec": target.title, "hwnd": target.hwnd}
            )
            check.check(
                "switching to a real window succeeds",
                switched.get("ok"),
                str(switched.get("detail")),
            )
            check.check(
                "the pipeline now captures from that window",
                isinstance(engine.pipeline.capturer, WindowCapturer),
                type(engine.pipeline.capturer).__name__,
            )
            check.check(
                "and the capturer is the session's, not a copy",
                engine.pipeline.capturer is engine._capturer,  # noqa: SLF001
            )
            info = engine.info()
            check.check(
                "the settings view reports window capture",
                info["capture_mode"] == "window",
                info["capture_mode"],
            )
            check.check(
                "the region string names the window, so it is not mistaken for a box",
                str(info["region"]).startswith("窗口："),
                str(info["region"]),
            )
            check.check(
                "the reported box is the live client area",
                info["region_box"] == list(target.rect),
                f"{info['region_box']} vs {list(target.rect)}",
            )

            # This is the bug the check exists for: dragging a region after
            # choosing a window must actually take effect. Unfixed, the window
            # kept being followed while the new rectangle was ignored.
            engine.command(CMD_SET_REGION, {"region": "10,20,320,180"})
            check.check(
                "dragging a region after a window switch takes over",
                isinstance(engine.pipeline.capturer, RegionCapturer),
                type(engine.pipeline.capturer).__name__,
            )
            check.check(
                "and the reported box is the dragged rectangle again",
                engine.info()["region_box"] == [10, 20, 320, 180],
                f"{engine.info()['region_box']}",
            )

        try:
            engine.pipeline.capturer.close()
        except Exception:
            pass

        # ---------------------------------------------------------------- #
        check.section("closing the window tears down what it owns")
        check.check(
            "the overlay was not closed early",
            overlay.closes == [],
            f"got {overlay.closes}",
        )
        app.close()
        check.check(
            "closing the main window closed the overlay first",
            overlay.closes == [False],
            f"got {overlay.closes}",
        )
        check.check(
            "and the overlay was told not to destroy a root it does not own",
            overlay.closes and overlay.closes[0] is False,
            f"got {overlay.closes}",
        )
        check.check("the app knows it is closed", app.closed is True)
        try:
            app.root.winfo_exists()
            destroyed = False
        except Exception:
            destroyed = True
        check.check("the Tk root is gone, so no interpreter is leaked", destroyed)
        # Closing twice must not raise: the window manager can deliver the close
        # request after a hotkey already stopped the session.
        try:
            app.close()
            double_close_ok = True
        except Exception as exc:
            double_close_ok = False
            print(f"      second close raised: {type(exc).__name__}: {exc}")
        check.check("closing twice is harmless", double_close_ok)

    finally:
        if not app.closed:
            app.close()

    return check.report()


class _Refinement:
    """The fields ``encode_refinement`` reads, without loading the model."""

    def __init__(self, source: str, target: str) -> None:
        self.source_text = source
        self.target_text = target
        self.backend = "nmt"
        self.protected_terms = 1
        self.lost_placeholders = 0
        self.elapsed_ms = 240.0
        self.used_nmt = True
        self.fallback_reason = None


class _StubTranslator:
    """Enough of a translator for ``Session.build`` and ``info``.

    The real one loads a 715 MiB model on construction, which this check has no
    reason to do: it verifies capture-source switching, not translation. Loading it
    cost 1.3 s and several hundred MiB of RSS per run of this file.
    """

    def stats(self) -> dict[str, Any]:
        return {
            "backend": "stub",
            "corpus_entries": 0,
            "rules": 0,
            "rule_ids": [],
            "nmt_available": False,
            "nmt_error": None,
            "nmt_load_ms": 0.0,
        }

    def defer_while(self, _predicate: Any) -> None:  # pragma: no cover - unused
        return None


if __name__ == "__main__":
    raise SystemExit(main())
