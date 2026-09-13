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

from watashi.desktop import SCOPE_LABELS, DesktopApp  # noqa: E402
from watashi.config import AppConfig  # noqa: E402
from watashi.overlay import WDA_EXCLUDEFROMCAPTURE, _IS_WINDOWS  # noqa: E402
from watashi.events import (  # noqa: E402
    CMD_CORRECT,
    CMD_SET_REGION,
    CMD_TOGGLE_PAUSE,
    CMD_USE_WINDOW,
    EVENT_CORRECTION,
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
        self.corrections: list[dict[str, Any]] = []
        self.paused = False
        self.presentation = PresentationSpec.preset("bar")
        self.presentation_sinks: list[Any] = []
        #: the desktop window reads capture.exclude_self from it, and a fake that lacks it
        #: would hide that code path from this check rather than exercise it
        self.config = AppConfig.load()

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
        result: dict[str, Any] = {"cmd": name, "ok": True, "detail": "ok"}
        # The real session reports the paused state on every command result, and the
        # window reads it to flip its button without waiting for an event. A fake that
        # omitted it would hide exactly that code path.
        if name in ("pause", "resume", "toggle_pause"):
            if name == "toggle_pause":
                self.paused = not self.paused
            else:
                self.paused = name == "pause"
            result["paused"] = self.paused
        return result

    def correction_listing(self) -> list[dict[str, Any]]:
        return list(self.corrections)

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


def _scene_from_ready(app: DesktopApp) -> str:
    """What the scene box shows after a ready event that names a scene.

    The real ``INFO`` fixture with only ``scene`` added, and the boxes are put back
    afterwards: ``_on_ready`` also fills the plugin tab and the export format list, so a
    partial payload here would fail later assertions for a reason that belongs to this one.
    """
    before_target = app.target_var.get()
    before_scene = app.scene_var.get()
    app._on_ready({**INFO, "scene": "wildlife"})
    shown = app.scene_var.get()
    app._on_ready({**INFO, "scene": before_scene})
    app.target_var.set(before_target)
    return shown


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
        check.section("the window builds with the tabs a desktop window is for")
        tabs = [
            app.notebook.tab(i, "text") for i in range(app.notebook.index("end"))
        ]
        check.check(
            "three tabs exist: the live view, capture, and plugins",
            tabs == ["字幕", "采集", "插件"],
            f"got {tabs}",
        )
        check.check("the status strip starts empty-ish", app.status_var.get() != "")
        check.check(
            "the history starts empty",
            app.history == [],
            f"got {app.history!r}",
        )

        # The tabs that used to be here -- 设置, 呈现, 诊断, 翻译 -- duplicated the web
        # panel, and this is the assertion that they stay gone rather than growing back
        # one at a time. A second editor of the same state is how two surfaces start
        # disagreeing, which is the reason the panel became the editing surface.
        gone = [
            name for name in ("设置", "呈现", "诊断", "翻译") if name in tabs
        ]
        check.check(
            "the duplicated settings/presentation/diagnostics/translation tabs are gone",
            not gone,
            f"still present: {gone}",
        )
        for attribute in ("settings_box", "presets_frame", "stats_box", "profiles_frame"):
            check.check(
                f"and their widgets went with them ({attribute})",
                not hasattr(app, attribute),
                "a half-removed tab leaves code that still tries to update it",
            )

        # "Screen recognition must exclude itself": the engine photographs a rectangle,
        # and this window is one of the two windows we own that can be inside it.
        check.check(
            "the window asks Windows to keep it out of screen capture",
            app.excluded_from_capture or not _IS_WINDOWS,
            f"capture_affinity={hex(getattr(app, 'capture_affinity', 0))}",
        )
        check.check(
            "using the variant that hides it from capture rather than rendering it black",
            getattr(app, "capture_affinity", 0) == WDA_EXCLUDEFROMCAPTURE or not _IS_WINDOWS,
            "WDA_MONITOR would also stop the loop but draws the window black",
        )

        off_session = FakeSession()
        off_session.config.capture["exclude_self"] = False
        app_off = DesktopApp(off_session, overlay=None, title="no exclusion")
        try:
            check.check(
                "with the setting off it is not excluded, so the assertion above is not vacuous",
                not app_off.excluded_from_capture,
                f"capture_affinity={hex(getattr(app_off, 'capture_affinity', 0))}",
            )
        finally:
            app_off.close()

        # ---------------------------------------------------------------- #
        check.section("a ready event fills the strip, not four tabs")
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
            "the vocabulary line carries the counts and its languages",
            "42 词条" in app.corpus_summary and "3 规则" in app.corpus_summary,
            app.corpus_summary,
        )
        check.check(
            "the target language control is in the top bar, where it is used",
            app.target_var.get() == "zh-CN",
            app.target_var.get(),
        )
        # The scene is here for the same reason the target language is: it is a setting a
        # user changes while watching, and the panel is a browser they would have to
        # alt-tab to. The window has to be able to set it, and to clear it.
        check.check(
            "the scene control is in the top bar too, and starts empty",
            app.scene_var.get() == "",
            app.scene_var.get(),
        )
        session.commands.clear()
        app.scene_var.set("finance")
        app.apply_scene()
        check.check(
            "applying a scene sends the command with what the box says",
            session.commands[-1] == ("set_scene", {"scene": "finance"}),
            f"got {session.commands[-1]}",
        )
        session.commands.clear()
        app.scene_var.set("  ")
        app.apply_scene()
        check.check(
            "and an emptied box clears the selection rather than sending a blank scene",
            session.commands[-1] == ("set_scene", {"scene": ""}),
            f"got {session.commands[-1]}",
        )
        check.check(
            "the scene a restart actually has is shown, not remembered from the box",
            _scene_from_ready(app) == "wildlife",
            "info() carries it, so the box cannot lie about the current scene",
        )
        check.check(
            "the panel button is present, so the surface that edits is one click away",
            "面板" in app.panel_button.cget("text"),
            app.panel_button.cget("text"),
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
        check.check(
            "the counters a user acts on are on that one line, where the diagnostics tab used to be",
            "识别 57" in app.status_var.get() and "精修 8" in app.status_var.get(),
            app.status_var.get(),
        )
        check.check(
            "and so is the size of the loaded vocabulary",
            "42 词条" in app.status_var.get(),
            app.status_var.get(),
        )
        check.check(
            "a presentation event from another surface does not break this window",
            (session.publish(EVENT_PRESENTATION, PresentationSpec.preset("minimal").to_dict()),
             drain(app),
             app.current_var.get() == UPDATE.target_text)[-1],
            "the presentation spec is edited in the panel now; this window must ignore it",
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
        check.section("实时纠正：选中屏幕上的那一行，改掉它")
        session.publish(EVENT_SUBTITLE, encode_update(OverlayUpdate(
            source_text="他突破到了虚空境界",
            target_text="He broke through to the void realm",
        )))
        drain(app)
        app.correct_source_var.set("")
        app.correct_target_var.set("")
        app.root.update()

        check.check(
            "the scope defaults to the whole sentence",
            app.correct_scope_var.get() == SCOPE_LABELS[0],
            app.correct_scope_var.get(),
        )
        check.check(
            "the scope offers both a line and a term",
            len(SCOPE_LABELS) == 2 and "整句" in SCOPE_LABELS[0] and "词语" in SCOPE_LABELS[1],
            str(SCOPE_LABELS),
        )

        bbox = app.history_box.bbox("1.0")
        check.check(
            "the history is laid out far enough to click a line",
            bbox is not None,
            f"bbox={bbox}",
        )
        if bbox:
            app._pick_history_line(type("E", (), {"x": bbox[0] + 3, "y": bbox[1] + 3})())
        check.check(
            "clicking a history line fills in its source, so the user need not retype it",
            app.correct_source_var.get() == "他突破到了虚空境界",
            app.correct_source_var.get(),
        )
        check.check(
            "and its current translation, to edit",
            app.correct_target_var.get() == "He broke through to the void realm",
            app.correct_target_var.get(),
        )

        session.commands.clear()
        app.correct_target_var.set("He has broken through into the Void Realm")
        app.save_correction()
        check.check(
            "saving issues one correct command with source, target and scope",
            session.commands
            == [(
                CMD_CORRECT,
                {
                    "source": "他突破到了虚空境界",
                    "target": "He has broken through into the Void Realm",
                    "scope": "line",
                },
            )],
            f"got {session.commands}",
        )
        check.check(
            "and reports where it was written",
            "已保存" in app.correct_status_var.get(),
            app.correct_status_var.get(),
        )

        app.correct_scope_var.set(SCOPE_LABELS[1])
        app.save_correction()
        check.check(
            "choosing 词语 sends term scope, which is the one that survives OCR drift",
            session.commands[-1][1]["scope"] == "term",
            str(session.commands[-1]),
        )

        # A correction made in another surface arrives as an event, and the frame the
        # session republishes behind it must update that row rather than add a second
        # subtitle for the same sentence.
        rows = len(app.history)
        session.corrections.append({"source": "他突破到了虚空境界", "target": "X", "scope": "line"})
        session.publish(EVENT_CORRECTION, {
            "source": "他突破到了虚空境界",
            "target": "He has broken through into the Void Realm",
            "scope": "line",
            "path": "corrections.json",
            "total": 1,
        })
        drain(app)
        check.check(
            "a correction from elsewhere adds no history row of its own",
            len(app.history) == rows,
            f"{rows} -> {len(app.history)}",
        )
        check.check(
            "the editor shows what was recorded and where",
            "已记录纠正" in app.correct_status_var.get()
            and "corrections.json" in app.correct_status_var.get(),
            app.correct_status_var.get(),
        )
        check.check(
            "the running count has its own line, so it does not overwrite that message",
            "已记录 1 条纠正" in app.correct_count_var.get()
            and app.correct_status_var.get().startswith("已记录纠正"),
            f"count={app.correct_count_var.get()!r} message={app.correct_status_var.get()!r}",
        )

        session.publish(EVENT_SUBTITLE, encode_update(OverlayUpdate(
            source_text="他突破到了虚空境界",
            target_text="He has broken through into the Void Realm",
        )))
        drain(app)
        check.check(
            "the repainted frame updates its own row instead of duplicating the subtitle",
            len(app.history) == rows,
            f"{rows} -> {len(app.history)}: {[e[1] for e in app.history[-2:]]}",
        )
        corrected_row = next(
            (e for e in app.history if e[0] == "他突破到了虚空境界"), None
        )
        check.check(
            "and that row now carries the corrected text",
            corrected_row is not None
            and corrected_row[1] == "He has broken through into the Void Realm",
            str(corrected_row),
        )
        check.check(
            "marked as corrected, so the user can see which lines they changed",
            corrected_row is not None and corrected_row[3].startswith("已纠正"),
            str(corrected_row[3] if corrected_row else None),
        )
        check.check(
            "a genuinely new sentence still appends as before",
            (session.publish(EVENT_SUBTITLE, encode_update(OverlayUpdate(
                source_text="完全是另一句话", target_text="an entirely different line",
            ))), drain(app), len(app.history) == rows + 1)[-1],
            f"{rows} -> {len(app.history)}",
        )

        # Last, because a refused command writes an error row into the history and that
        # would be the row the assertions above were reading.
        session.fail_names.add(CMD_CORRECT)
        app.save_correction()
        check.check(
            "a refused correction is reported in the editor rather than raising",
            "未保存" in app.correct_status_var.get(),
            app.correct_status_var.get(),
        )
        session.fail_names.discard(CMD_CORRECT)

        # ---------------------------------------------------------------- #
        check.section("controls issue engine commands, not local state")
        session.commands.clear()
        app.toggle_pause()
        check.check(
            "pause routes through the session",
            session.commands == [(CMD_TOGGLE_PAUSE, {})],
            f"got {session.commands}",
        )
        # The user's report: pressing the stop button changed nothing they could see. The
        # button used to be set only from the next stats event, and a paused pipeline
        # emitted none -- so it never moved.
        check.check(
            "and the button changes at once, without waiting for a stats event",
            app.pause_button.cget("text") == "继续识别",
            app.pause_button.cget("text"),
        )
        check.check(
            "with the state said next to it, where a user asking 'is it running?' looks",
            "已暂停" in app.pause_hint.get(),
            app.pause_hint.get(),
        )
        app.toggle_pause()
        check.check(
            "clicking again flips both back",
            app.pause_button.cget("text") == "暂停识别" and "正在识别" in app.pause_hint.get(),
            f"{app.pause_button.cget('text')!r} / {app.pause_hint.get()!r}",
        )
        session.fail_names.add(CMD_TOGGLE_PAUSE)
        app.toggle_pause()
        check.check(
            "a refused pause does not pretend to have happened",
            app.pause_button.cget("text") == "暂停识别",
            "the update is guarded by the command result, not by the click",
        )
        session.fail_names.discard(CMD_TOGGLE_PAUSE)
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
        app.target_var.set("ja")
        app.apply_target()
        check.check(
            "the target language in the top bar is applied as a command",
            session.commands[-1] == ("set_target_lang", {"target_lang": "ja"}),
            f"got {session.commands[-1]}",
        )
        app.region_var.set("0,0,640,360")
        app.apply_value("set_diff_threshold", "1.5", float)
        check.check(
            "the diff threshold is applied",
            session.commands[-1] == ("set_diff_threshold", {"value": 1.5}),
            f"got {session.commands[-1]}",
        )

        # The one control that hands the user to the surface that edits: it has to
        # actually start the panel, and it must not open a browser window in a test run.
        #
        # This runs against the *real* WebPanel, on a real port, and then fetches the
        # page over HTTP. The previous version of this section swapped in a hand-written
        # stand-in, and that stand-in is exactly why a broken button shipped: it defined
        # `running` (which WebPanel does not have, so every click raised AttributeError)
        # and made `url` a method (it is a property, so the next line raised TypeError).
        # The check agreed with the code because both were written from the same wrong
        # idea. A contract this function invents is not a contract; the class is.
        import watashi.desktop as desktop_module
        import watashi.web as web_module

        opened: list[str] = []
        original_open = desktop_module.webbrowser.open
        desktop_module.webbrowser.open = (  # type: ignore[assignment]
            lambda url: opened.append(url) or True
        )

        real_panel = None
        panel_error: str | None = None
        real_cls = web_module.WebPanel
        try:
            # A free port, so a stray uvicorn from another session cannot make this fail
            # for a reason that has nothing to do with the wiring. Only the port is
            # overridden -- this is still the real class, constructed by open_panel
            # itself, so what comes back is what the button would really build.
            import functools
            import socket as _socket

            with _socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                free_port = int(probe.getsockname()[1])
            web_module.WebPanel = functools.partial(real_cls, port=free_port)  # type: ignore[assignment]
            app._panel = None
            # Timed, not asserted: open_panel blocks the click while the socket comes up,
            # so the number behind the 3 s timeout should be a measurement rather than a
            # guess. A hard bound here would flake the way the latency bench does when
            # the machine is busy.
            started_monotonic = time.perf_counter()
            app.open_panel()
            panel_up_ms = (time.perf_counter() - started_monotonic) * 1000.0
            print(f"      panel up in {panel_up_ms:.0f} ms (open_panel blocks the click)")
            real_panel = getattr(app, "_panel", None)
        except Exception as exc:
            panel_error = f"{type(exc).__name__}: {exc}"
        finally:
            web_module.WebPanel = real_cls  # type: ignore[assignment]
            desktop_module.webbrowser.open = original_open  # type: ignore[assignment]

        check.check(
            "clicking 打开设置面板 does not raise, running against the real WebPanel",
            panel_error is None,
            panel_error or "no exception",
        )
        check.check(
            "the panel the button builds is a real one, on the port it was given",
            isinstance(real_panel, real_cls),
            f"got {type(real_panel).__name__ if real_panel is not None else None}",
        )
        # "Linked up" is the whole point of the button: the panel has to read and write the
        # session this window is displaying. A panel built around a copy of the session
        # would serve a page that looks right and shows nothing happening.
        check.check(
            "and it is wired to this window's own session, not a copy",
            real_panel is not None and real_panel.session is app.session,
            f"panel.session is app.session: "
            f"{real_panel is not None and real_panel.session is app.session}",
        )
        check.check(
            "the button starts a server that is actually accepting connections",
            real_panel is not None and real_panel.running,
            f"running={getattr(real_panel, 'running', None)}",
        )
        check.check(
            "and hands the browser that server's URL",
            real_panel is not None and opened == [real_panel.url],
            f"opened={opened} url={getattr(real_panel, 'url', None)}",
        )

        # Serving the page is the point of the button; a URL that 404s is not success.
        page_status: Any = None
        page_title = ""
        if real_panel is not None:
            import urllib.error
            import urllib.request

            try:
                with urllib.request.urlopen(real_panel.url, timeout=5.0) as response:
                    page_status = response.status
                    body = response.read().decode("utf-8", "replace")
                page_title = body.split("<title>", 1)[1].split("</title>", 1)[0] if "<title>" in body else ""
            except urllib.error.URLError as exc:
                page_status = f"URLError: {exc}"
        check.check(
            "the URL the button opens really serves the settings page",
            page_status == 200 and "Project Watashi" in page_title,
            f"status={page_status} title={page_title!r}",
        )

        # A second click must reuse that server: start() returns False for an already
        # running panel too, so a check on the return value would report a bogus
        # "port in use" to a user who just clicked the button twice.
        opened.clear()
        second_error: str | None = None
        desktop_module.webbrowser.open = (  # type: ignore[assignment]
            lambda url: opened.append(url) or True
        )
        try:
            app.open_panel()
        except Exception as exc:
            second_error = f"{type(exc).__name__}: {exc}"
        finally:
            desktop_module.webbrowser.open = original_open  # type: ignore[assignment]

        check.check(
            "clicking a second time reopens the same live server",
            second_error is None and real_panel is not None and opened == [real_panel.url],
            f"error={second_error} opened={opened}",
        )
        check.check(
            "and does not report a port problem for a panel it is already serving",
            not any("启动失败" in e[1] or "超时" in e[1] for e in app.history),
            [e[1] for e in app.history[-3:]],
        )
        check.check(
            "the URL is also written into the history, for when the browser does not open",
            real_panel is not None
            and any(real_panel.url in entry[1] for entry in app.history),
            [entry[1] for entry in app.history[-3:]],
        )

        # Closing the window releases the port; that is asserted at the end, where this
        # app is closed anyway -- calling close() here would destroy the Tk root and take
        # the rest of this check with it.

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

        # The panel started earlier in this check must have gone down with the window.
        # A panel left running keeps port 8765 bound inside a process that no longer
        # shows anything, and the next launch reports "port in use" for a server the
        # user cannot see or stop.
        check.check(
            "closing the window stopped the web panel it started",
            real_panel is not None and not real_panel.running,
            f"panel={real_panel!r} running={getattr(real_panel, 'running', None)}",
        )
        if real_panel is not None:
            import socket as _rebind_socket

            try:
                with _rebind_socket.socket() as rebind:
                    rebind.setsockopt(_rebind_socket.SOL_SOCKET, _rebind_socket.SO_REUSEADDR, 1)
                    rebind.bind(("127.0.0.1", real_panel.port))
                port_free: Any = True
            except OSError as exc:
                port_free = f"OSError: {exc}"
            check.check(
                "and the port is genuinely free again, not merely reported free",
                port_free is True,
                f"port {real_panel.port}: {port_free}",
            )

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
