"""Desktop UI: a main window with controls, settings and live output.

Another adapter over the same ``Session`` event stream as the CLI, the overlay
and the web panel -- not a second application. It shares the Tk root with the
overlay, because two ``Tk()`` instances in one process have separate
interpreters and widgets cannot cross between them, so anything that wants a main
window *and* an overlay must share one root.

Toolkit is tkinter, per the decision recorded earlier: it is in the standard
library and costs ~6 MiB, which matters because the model already costs 715 MiB.
The project documents Qt for the C++ port; building a Qt UI now would add
100-200 MiB of dependencies and be rewritten at port time.

Threading: the UI runs on the Tk thread and drains the session's queue from an
``after`` tick. Worker threads never touch a widget -- the same rule the overlay
follows, and the reason both go through a queue.
"""

from __future__ import annotations

import json
import os
import queue
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .capture import Region
from .correct import SCOPE_LINE, SCOPE_TERM
from .events import (
    CMD_CORRECT,
    CMD_EXPORT,
    CMD_SET_DIFF_THRESHOLD,
    CMD_SET_FPS,
    CMD_SET_REGION,
    CMD_SET_SCENE,
    CMD_SET_TARGET_LANG,
    CMD_TOGGLE_PAUSE,
    CMD_USE_WINDOW,
    EVENT_CORRECTION,
    EVENT_ERROR,
    EVENT_LIBRARY,
    EVENT_READY,
    EVENT_REFINEMENT,
    EVENT_SETTINGS,
    EVENT_STATS,
    EVENT_STATUS,
    EVENT_STOPPED,
    EVENT_SUBTITLE,
    decode_stats,
    decode_update,
)

#: Refreshed from a stats event. Kept short because a desktop window that lags is
#: worse than one that updates plainly.
STATS_INTERVAL_MS = 500
HISTORY_LIMIT = 200

#: The two scopes in the user's words, because "line" and "term" do not say which
#: one to pick: for a name that appears on every screen the term is the robust
#: choice, and for one bad sentence the line is the exact one.
SCOPE_LABELS = ("整句（只改这一句）", "词语（该词在任何句子中都改）")
SCOPE_VALUES = {SCOPE_LABELS[0]: SCOPE_LINE, SCOPE_LABELS[1]: SCOPE_TERM}


class DesktopApp:
    """The main window. Owns the Tk root and drives the session from it."""

    def __init__(
        self,
        session: Any,
        overlay: Any | None = None,
        title: str = "Project Watashi",
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.session = session
        self.overlay = overlay
        self.on_quit = on_quit
        self.history: list[tuple[str, str, bool]] = []
        self.stats: Any = None
        #: one line about the loaded vocabulary, for the status strip
        self.corpus_summary = ""
        #: the web panel, started on demand by 「打开设置面板」
        self._panel: Any = None
        self._channel: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=512)
        self._after_id: str | None = None
        self._closed = False
        self._last_status = ""
        #: (source, target) of a correction this window made, so the repainted frame
        #: that follows it updates the row it belongs to instead of being appended as
        #: a second subtitle for the same sentence
        self._expect_repaint: tuple[str, str] | None = None
        #: kept so the selector window is not garbage collected mid-drag
        self._selector: Any = None

        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("980x640")
        self.root.minsize(760, 520)
        self._build()
        self._exclude_from_capture()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _exclude_from_capture(self) -> None:
        """Ask Windows to keep this window out of screen capture.

        The engine photographs a rectangle of the screen; if this window is inside it, the
        engine reads its own counters and status line, and change detection fires on every
        repaint -- the stutter at startup. The overlay has always done this for itself
        (it must, or it would read its own subtitles); the control window is the other
        window we own that can end up in the region.

        The cost is real and worth stating: ``WDA_EXCLUDEFROMCAPTURE`` hides the window
        from *every* capture, including a screenshot the user takes on purpose. Hence the
        setting, and hence the other half of the problem being handled by detection rather
        than exclusion -- a browser showing the web panel is not our window, so there is
        nothing to ask Windows to hide.
        """
        if not bool(self.session.config.capture.get("exclude_self", True)):
            return
        from .overlay import set_capture_exclusion

        applied = set_capture_exclusion(self.root, True)
        self.capture_affinity = applied
        if applied == 0:
            # Not fatal, and worth saying: the detection still catches this window.
            self._append_history((
                "",
                "提示：无法把设置窗口排除在截屏之外，"
                "如果采集区域覆盖它，引擎会先暂停并告诉你。",
                True,
            ))

    @property
    def excluded_from_capture(self) -> bool:
        """Whether Windows is keeping this window out of screen capture."""
        from .overlay import WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE

        return getattr(self, "capture_affinity", 0) in (WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        # ---- status strip ------------------------------------------------ #
        strip = ttk.Frame(self.root, padding=(10, 6))
        strip.pack(fill="x")
        self.status_var = tk.StringVar(value="starting…")
        ttk.Label(strip, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )
        self.profile_var = tk.StringVar(value="")
        ttk.Label(strip, textvariable=self.profile_var).pack(side="right")

        # ---- controls ---------------------------------------------------- #
        #
        # What is here, and what is deliberately not. The window is a desktop window, so
        # it keeps the things only a native window can do -- a global pause, dragging a
        # region on the real screen, picking a window, and the target language, which is
        # the one setting a user changes while watching. Everything else that used to be
        # a tab here (the full settings schema, the presentation spec editor, the raw
        # counters, the memory tiers) duplicated the web panel and is gone: two surfaces
        # editing the same state is how they start disagreeing, and a form of fifty
        # described fields is something a browser does better than tkinter.
        controls = ttk.LabelFrame(self.root, text="控制", padding=(10, 6))
        controls.pack(fill="x", padx=10)
        self.pause_button = ttk.Button(controls, text="暂停识别", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="框选区域…", command=self.select_region).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(controls, text="选择窗口…", command=self.pick_window).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(controls, text="导出…", command=self.export).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="重载语料库", command=self.reload_corpus).pack(
            side="left", padx=(0, 12)
        )
        # Next to the button it belongs to, because "is it recognising?" is the one thing
        # a user asking about pause wants answered, and the long status line on the row
        # above is where that answer got lost.
        self.pause_hint = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.pause_hint, foreground="#7fc4ff").pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(controls, text="目标语言").pack(side="left")
        self.target_var = tk.StringVar(value="zh-CN")
        ttk.Entry(controls, textvariable=self.target_var, width=10).pack(
            side="left", padx=(4, 4)
        )
        ttk.Button(controls, text="应用", command=self.apply_target).pack(side="left")
        # The scene is here for the same reason the target language is: it is a setting a
        # user changes *while watching*, and the panel is a browser they would have to
        # alt-tab to. It is a scene name, matched against each entry's domain; empty means
        # no preference, which is the default and the old behaviour.
        ttk.Label(controls, text="场景").pack(side="left", padx=(12, 0))
        self.scene_var = tk.StringVar(value="")
        ttk.Entry(controls, textvariable=self.scene_var, width=12).pack(
            side="left", padx=(4, 4)
        )
        ttk.Button(controls, text="应用", command=self.apply_scene).pack(side="left")
        self.panel_button = ttk.Button(
            controls, text="打开设置面板", command=self.open_panel
        )
        self.panel_button.pack(side="right")

        # ---- tabs -------------------------------------------------------- #
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.notebook = notebook
        self._build_subtitles(notebook)
        self._build_capture(notebook)
        self._build_plugins(notebook)

    def _tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        frame = ttk.Frame(notebook, padding=10)
        notebook.add(frame, text=title)
        return frame

    def _build_subtitles(self, notebook: ttk.Notebook) -> None:
        frame = self._tab(notebook, "字幕")
        self.current_var = tk.StringVar(value="（等待识别）")
        current = tk.Label(
            frame, textvariable=self.current_var, wraplength=880, justify="left",
            anchor="w", font=("Microsoft YaHei UI", 16), bg="#111820", fg="#e6eef5",
            padx=14, pady=14,
        )
        current.pack(fill="x")
        self.source_var = tk.StringVar(value="")
        tk.Label(
            frame, textvariable=self.source_var, wraplength=880, justify="left",
            anchor="w", bg="#0b0f13", fg="#7d909f", padx=14, pady=6,
        ).pack(fill="x")
        ttk.Label(frame, text="对照历史").pack(anchor="w", pady=(10, 4))
        self.history_box = tk.Text(
            frame, height=12, wrap="word", bg="#0b0f13", fg="#c9d6e0",
            insertwidth=0, relief="flat", font=("Microsoft YaHei UI", 11),
        )
        self.history_box.pack(fill="both", expand=True)
        self.history_box.tag_configure("source", foreground="#7d909f")
        self.history_box.tag_configure("target", foreground="#e6eef5")
        self.history_box.tag_configure("meta", foreground="#5f7386")
        self.history_box.tag_configure("corrected", foreground="#8fd6a0")
        self.history_box.configure(state="disabled")
        #: display line (1-based) -> index into self.history, so a click lands on the
        #: entry it looks like it lands on instead of on a text match that may repeat
        self._display_lines: list[int] = []
        self.history_box.bind("<ButtonRelease-1>", self._pick_history_line)

        self._build_correction(frame)

    def _build_correction(self, parent: ttk.Frame) -> None:
        """The correction editor: read a bad translation, type the right one, save.

        In this tab rather than in a settings page, because this is where the user is
        when they notice the mistake -- the wrong line is on screen above it. The
        source box is filled from the history by clicking a line, because a
        whole-sentence correction only matches the frame it was typed from if the text
        is byte-identical to what OCR produced, and retyping it by hand is both tedious
        and a way to introduce the very mismatch that stops it working.
        """
        box = ttk.LabelFrame(parent, text="实时纠正 · 改错的那一行", padding=(10, 6))
        box.pack(fill="x", pady=(10, 0))
        ttk.Label(
            box,
            text="点上面的历史行会自动填入原文，改好译文后保存：会写进用户语料库最高优先级的一层，"
                 "当前这一帧立刻改过来，以后同样的句子也用它。",
            foreground="#5f7386", justify="left", wraplength=900,
        ).pack(anchor="w")

        self.correct_source_var = tk.StringVar(value="")
        self.correct_target_var = tk.StringVar(value="")
        self.correct_scope_var = tk.StringVar(value=SCOPE_LABELS[0])

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 2))
        ttk.Label(row, text="原文", width=6).pack(side="left")
        ttk.Entry(row, textvariable=self.correct_source_var).pack(
            side="left", fill="x", expand=True
        )
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="译文", width=6).pack(side="left")
        ttk.Entry(row2, textvariable=self.correct_target_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Combobox(
            row2, textvariable=self.correct_scope_var, values=SCOPE_LABELS,
            state="readonly", width=22,
        ).pack(side="left", padx=(6, 6))
        ttk.Button(row2, text="保存纠正", command=self.save_correction).pack(side="left")

        self.correct_status_var = tk.StringVar(value="")
        ttk.Label(
            box, textvariable=self.correct_status_var, foreground="#5f7386",
            justify="left", wraplength=900,
        ).pack(anchor="w", pady=(4, 0))
        #: Separate from the message above, because a running count that overwrites the
        #: result of the correction just made reads as the save having failed.
        self.correct_count_var = tk.StringVar(value="")
        ttk.Label(
            box, textvariable=self.correct_count_var, foreground="#55697a",
        ).pack(anchor="w")
        self._refresh_correction_count()

    def _refresh_correction_count(self) -> None:
        try:
            listing = self.session.correction_listing()
        except Exception:
            return
        if not listing:
            self.correct_count_var.set("还没有纠正记录。")
            return
        self.correct_count_var.set(
            f"已记录 {len(listing)} 条纠正；最近一条："
            f"{listing[-1]['source']} → {listing[-1]['target']}"
        )

    def _pick_history_line(self, event: Any) -> None:
        try:
            index = self.history_box.index(f"@{event.x},{event.y}")
            line = int(str(index).split(".")[0])
        except (tk.TclError, ValueError):
            return
        if not (1 <= line <= len(self._display_lines)):
            return
        entry = self.history[ self._display_lines[line - 1] ]
        if entry[2] or not entry[0]:
            return  # an error row or a status message is not a translation
        self.correct_source_var.set(entry[0])
        self.correct_target_var.set(entry[1])
        self.correct_status_var.set("已填入原文与译文，改完点「保存纠正」。")

    def save_correction(self) -> None:
        source = self.correct_source_var.get().strip()
        target = self.correct_target_var.get().strip()
        label = self.correct_scope_var.get()
        result = self._command(
            CMD_CORRECT,
            {"source": source, "target": target, "scope": SCOPE_VALUES.get(label, "line")},
        )
        if result.get("ok"):
            self.correct_status_var.set(f"已保存：{result.get('detail')}")
        else:
            self.correct_status_var.set(f"未保存：{result.get('detail')}")

    def _build_capture(self, notebook: ttk.Notebook) -> None:
        frame = self._tab(notebook, "采集")
        self.region_var = tk.StringVar(value="(auto)")
        self._row(frame, "当前区域", ttk.Label(frame, textvariable=self.region_var))
        self.fps_var = tk.StringVar(value="10")
        self._entry_row(frame, "采集帧率", self.fps_var, CMD_SET_FPS, float)
        self.diff_var = tk.StringVar(value="2.0")
        self._entry_row(frame, "变化阈值", self.diff_var, CMD_SET_DIFF_THRESHOLD, float)
        ttk.Label(
            frame,
            text="变化阈值越低越容易触发 OCR，越高越省 CPU，但可能漏掉短暂字幕。\n"
                 "窗口捕获请优先用「窗口的一部分」，密文本整窗识别每帧可到 1–4 秒。",
            justify="left", foreground="#5f7386",
        ).pack(anchor="w", pady=(10, 0))

    def _build_plugins(self, notebook: ttk.Notebook) -> None:
        frame = self._tab(notebook, "插件")
        self.plugins_box = tk.Text(
            frame, height=16, wrap="none", bg="#0b0f13", fg="#c9d6e0",
            insertwidth=0, relief="flat", font=("Consolas", 10),
        )
        self.plugins_box.pack(fill="both", expand=True)
        self.plugins_box.configure(state="disabled")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        self.export_format_var = tk.StringVar(value="")
        ttk.Label(row, text="导出格式").pack(side="left")
        self.export_combo = ttk.Combobox(
            row, textvariable=self.export_format_var, width=14, state="readonly"
        )
        self.export_combo.pack(side="left", padx=6)
        ttk.Button(row, text="导出…", command=self.export).pack(side="left")
        ttk.Label(
            frame,
            text="插件在本进程内运行，Python 无法沙箱化：它们是给你自己机器用的，\n"
                 "不适合分发给他人。失败会被报告并跳过，不会影响主程序。",
            justify="left", foreground="#5f7386",
        ).pack(anchor="w", pady=(8, 0))

    # -- small builders ---------------------------------------------------- #

    def _row(self, parent: tk.Misc, label: str, widget: tk.Misc) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=12).pack(side="left")
        widget.pack(side="left")

    def _entry_row(
        self, parent: tk.Misc, label: str, variable: tk.StringVar,
        command: str, caster: Callable[[str], Any],
    ) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=12).pack(side="left")
        ttk.Entry(row, textvariable=variable, width=10).pack(side="left")
        ttk.Button(
            row, text="应用",
            command=lambda: self.apply_value(command, variable.get(), caster),
        ).pack(side="left", padx=6)

    # ------------------------------------------------------------------ #
    # session plumbing
    # ------------------------------------------------------------------ #

    def attach(self) -> None:
        """Subscribe to the engine's event stream."""
        self._channel = self.session.subscribe()

    def run(self) -> None:
        """Enter the Tk main loop (blocks until the window closes)."""
        self._pump()
        self.root.mainloop()

    def _pump(self) -> None:
        if self._closed:
            return
        # Cancel any tick that is still queued before scheduling the next one, so the
        # contract is "at most one pending" no matter how this is driven. Tk only ever
        # calls it from `after`, but the self checks call it directly to drive the widget
        # without a main loop, and each of those calls used to queue another tick -- 13
        # leaked callbacks at teardown, each printing "invalid command name ..._pump" to
        # stderr, which reads like a crash in the middle of a passing test run.
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        pending: list[tuple[str, Any]] = []
        while True:
            try:
                event = self._channel.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            data = event.get("data") or {}
            if kind == EVENT_READY:
                self._on_ready(data)
            elif kind == EVENT_SUBTITLE:
                # Deferred, not applied inline: a refinement that arrives in the
                # same batch must be applied *after* the subtitle it refines, or
                # it compares against the previous line, fails to match, and the
                # model result is silently lost.
                pending.append(("subtitle", decode_update(data)))
            elif kind == EVENT_REFINEMENT:
                pending.append(("refinement", data))
            elif kind == EVENT_STATS:
                self.stats = decode_stats(data)
            elif kind == EVENT_SETTINGS:
                # Some surface changed a setting. Reported here rather than re-rendered:
                # the settings form lives in the web panel now, and this window's job is
                # to say that something changed, not to keep a second copy of the form.
                self._append_history(
                    ("", "设置已由其他界面更新：" + "、".join(data.get("changed") or []), True)
                )
            elif kind == EVENT_LIBRARY:
                # Same reasoning for the corpus: the table is in the panel, the notice is
                # here, so a user watching the subtitles knows their edit landed.
                self._append_history(
                    ("", "语料库已更新：" + str(data.get("detail") or ""), True)
                )
            elif kind == EVENT_CORRECTION:
                self._on_correction(data)
            elif kind == EVENT_STATUS:
                self._last_status = str(data.get("message", ""))
            elif kind == EVENT_ERROR:
                self._append_history(("", f"错误：{data.get('message', '')}", True))
            elif kind == EVENT_STOPPED:
                self.status_var.set("已停止")
        # Only the last subtitle is drawn, but every refinement is replayed, so a
        # burst of frames does not cost one repaint each.
        last_subtitle = max(
            (i for i, (kind, _) in enumerate(pending) if kind == "subtitle"),
            default=None,
        )
        for index, (kind, payload) in enumerate(pending):
            if kind == "subtitle":
                if index == last_subtitle:
                    self._on_subtitle(payload)
            else:
                self._on_refinement(payload)
        self._refresh_stats()
        try:
            self._after_id = self.root.after(STATS_INTERVAL_MS // 8, self._pump)
        except tk.TclError:
            pass

    # -- event handlers ---------------------------------------------------- #

    def _on_ready(self, info: dict[str, Any]) -> None:
        self.profile_var.set(f"配置档：{info.get('profile') or '(none)'}")
        self.region_var.set(str(info.get("region")))
        self.target_var.set(str(info.get("target_lang", "")))
        # Prefilled from what is actually in effect, so the box is never lying about the
        # current scene after a restart or a panel change.
        self.scene_var.set(str(info.get("scene", "")))
        self.fps_var.set(str(info.get("fps_target", "")))
        self.diff_var.set(str(info.get("diff_threshold", "")))
        # Kept for the status line rather than a tab of its own: how much vocabulary is
        # loaded is something to see at a glance while using the window, not a page.
        self.corpus_summary = (
            f"{info.get('corpus_entries')} 词条({info.get('corpus_languages') or '未标注'})"
            f" · {info.get('rules')} 规则"
        )

        plugins = info.get("plugins") or {}
        formats = plugins.get("export_formats") or []
        self.export_combo.configure(values=formats)
        if formats and not self.export_format_var.get():
            self.export_format_var.set(formats[0])
        self._render_plugin_status(plugins)

    def _on_subtitle(self, update: Any) -> None:
        # A frame republished by a correction, not a new one. It is the same sentence
        # with a different translation, so the row is updated in place: appending would
        # show the user their bad translation and their good one as two subtitle lines.
        pending = self._expect_repaint
        if pending is not None and update.source_text == pending[0]:
            self._expect_repaint = None
            expected, new_target = pending
            for index in range(len(self.history) - 1, -1, -1):
                if self.history[index][0] == expected:
                    self.history[index] = (
                        expected,
                        update.target_text or new_target,
                        False,
                        "已纠正 · " + self.history[index][3],
                    )
                    break
            self.current_var.set(update.target_text or new_target)
            self.source_var.set(update.source_text or "")
            self._rerender_history()
            return

        self.current_var.set(update.target_text or "（空）")
        self.source_var.set(update.source_text or "")
        origin = "模型" if update.refined else "语料库"
        self._append_history((
            update.source_text,
            update.target_text,
            False,
            f"{origin} · {update.latency_ms:.0f} ms · 覆盖率 {update.coverage * 100:.0f}%",
        ))

    def _on_correction(self, data: dict[str, Any]) -> None:
        """Some surface recorded a correction: show it here too, and do not duplicate it.

        The event arrives from the session, so a correction made in another surface
        updates this window's history without either surface knowing the other exists.
        """
        source = str(data.get("source") or "")
        target = str(data.get("target") or "")
        if source and target:
            self._expect_repaint = (source, target)
            self.correct_status_var.set(
                f"已记录纠正（{data.get('scope') or 'line'}）：{source} → {target}\n"
                f"写入 {data.get('path')}，共 {data.get('total')} 条"
            )
        elif data.get("removed"):
            self.correct_status_var.set(f"已删除纠正：{data['removed']}")
        self._refresh_correction_count()

    def _on_refinement(self, data: dict[str, Any]) -> None:
        target = str(data.get("target") or "")
        if not target:
            return
        source = str(data.get("source") or "")
        # Replace the last entry rather than appending: the refinement is the same
        # sentence as the instant corpus result, only better. Appending would show
        # every subtitle twice, which reads as a duplicate-subtitle bug.
        if self.history and self.history[-1][0] == source:
            self.history[-1] = (source, target, False, self.history[-1][3])
            self._rerender_history()
        self.current_var.set(target)

    def _append_history(self, entry: tuple) -> None:
        """Append one entry, normalised to ``(source, target, is_error, meta)``."""
        source = entry[0] if len(entry) > 0 else ""
        target = entry[1] if len(entry) > 1 else ""
        is_error = bool(entry[2]) if len(entry) > 2 else False
        meta = entry[3] if len(entry) > 3 else ""
        self.history.append((source, target, is_error, meta))
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]
        self._rerender_history()

    def _rerender_history(self) -> None:
        box = self.history_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        self._display_lines = []
        for index in range(len(self.history) - 1, -1, -1):
            source, target, is_error = self.history[index][0], self.history[index][1], self.history[index][2]
            meta = self.history[index][3] if len(self.history[index]) > 3 else ""
            if source:
                box.insert("end", source + "\n", ("source",))
                self._display_lines.append(index)
            box.insert("end", target + "\n", ("target", "corrected") if meta.startswith("已纠正") else ("target",))
            self._display_lines.append(index)
            if meta:
                box.insert("end", meta + "\n", ("meta",))
                self._display_lines.append(index)
            box.insert("end", "\n")
            self._display_lines.append(index)
        box.configure(state="disabled")

    def _refresh_stats(self) -> None:
        s = self.stats
        if s is None:
            return
        paused = "⏸ 已暂停" if s.paused else "识别中"
        # Everything the old 诊断 tab showed that is worth glancing at, on one line: the
        # counters a user actually acts on. The full JSON dump moved to the web panel's
        # 诊断 tab, which is where you go when you want all of it.
        self.status_var.set(
            f"{paused} · {s.backend} · {s.fps:.1f} FPS · OCR {s.ocr_ms:.0f} ms · "
            f"总 {s.total_ms:.0f} ms · 内存 {s.memory_mib:.0f} MiB · "
            f"识别 {s.frames} · 跳过 {s.skipped} · 精修 {s.refinements}"
            + (f" · {self.corpus_summary}" if self.corpus_summary else "")
        )
        self._apply_paused(bool(s.paused))

    def open_panel(self) -> None:
        """Start the web panel in-process and open it in the default browser.

        The desktop window is no longer where settings and the corpus are edited, so it
        has to be able to hand the user to the surface that is -- with one click, rather
        than a sentence telling them to run a command. The panel shares this process's
        engine, so the subtitles it shows are the ones being recognised right now.

        Uses ``WebPanel``'s public surface only: ``running``, ``start``,
        ``wait_until_ready``, ``url`` and ``stop``. An earlier version read
        ``panel.running`` (which did not exist, so every click raised AttributeError) and
        then called ``panel.url()`` (a property, so it would have raised TypeError next).
        Both mistakes survived review because the check used a hand-written stand-in that
        had exactly the attributes this function guessed at instead of the real ones.
        """
        from .web import WebPanel

        panel = getattr(self, "_panel", None)
        if panel is None:
            panel = WebPanel(self.session)
            self._panel = panel
        # A second click has to reuse the live server, so the question is whether the
        # panel is up -- not what ``start()`` returned. ``start()`` returns False for a
        # panel that is already running just as it does for one that cannot bind.
        if not panel.running:
            if not panel.start():
                self._append_history(
                    ("", "设置面板启动失败：请确认已安装 uvicorn，且端口 8765 未被占用", True)
                )
                return
            # Blocks the click for at most this long; uvicorn normally binds in well
            # under a tenth of a second, and opening the browser before the socket
            # accepts would show the user a connection error instead of the panel.
            if not panel.wait_until_ready(timeout=3.0):
                self._append_history(
                    ("", f"设置面板启动超时：端口 8765 可能已被占用，请稍后再试（{panel.url}）", True)
                )
                return
        url = panel.url
        self._append_history(("", f"设置面板已启动：{url}", True))
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._append_history(("", f"无法打开浏览器（{exc}）：请手动访问 {url}", True))

    def _render_plugin_status(self, status: dict[str, Any]) -> None:
        box = self.plugins_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", f"插件 API v{status.get('api_version')}\n")
        box.insert(
            "end",
            f"已加载 {status.get('loaded', 0)} · 失败 {status.get('failed', 0)}\n",
        )
        box.insert("end", f"导出格式：{', '.join(status.get('export_formats') or []) or '(无)'}\n")
        box.insert("end", f"已接通的扩展点：{', '.join(status.get('connected_points') or [])}\n\n")
        for plugin in status.get("plugins") or []:
            mark = "ok " if plugin.get("loaded") and not plugin.get("error") else "!! "
            points = ", ".join(plugin.get("provided_points") or []) or "(无)"
            box.insert("end", f"{mark}{plugin.get('name')}  -> {points}\n")
            if plugin.get("description"):
                box.insert("end", f"     {plugin['description']}\n")
            if plugin.get("error"):
                box.insert("end", f"     {plugin['error']}\n")
        failures = status.get("failures") or {}
        if failures:
            box.insert("end", "\n运行期失败：\n")
            for key, count in failures.items():
                box.insert("end", f"  {count}x {key}\n")
        box.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #

    def _command(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.session.command(name, payload or {})
        if not result.get("ok"):
            self._append_history(("", f"{name} 失败：{result.get('detail')}", True))
        return result

    def toggle_pause(self) -> None:
        """Pause or resume, and say so immediately.

        The button is set from the command's own result rather than waiting for the next
        stats event. Stats now carry the paused state too, but a control that only moves
        when the engine gets round to reporting is a control the user presses twice --
        and before that, a paused pipeline emitted no stats at all, so the button never
        moved and the strip went on claiming it was recognising.
        """
        result = self._command(CMD_TOGGLE_PAUSE)
        if result.get("ok") and "paused" in result:
            self._apply_paused(bool(result["paused"]))

    def _apply_paused(self, paused: bool) -> None:
        self.pause_button.configure(text="继续识别" if paused else "暂停识别")
        self.pause_hint.set("已暂停：不再识别屏幕" if paused else "正在识别屏幕")

    def reload_corpus(self) -> None:
        result = self._command("reload_corpus")
        if result.get("ok"):
            self._append_history(("", f"语料库已重载：{result.get('detail')}", True))

    def apply_target(self) -> None:
        self._command(CMD_SET_TARGET_LANG, {"target_lang": self.target_var.get().strip()})

    def apply_scene(self) -> None:
        """Select which scene's term entries win, or clear it with an empty box."""
        self._command(CMD_SET_SCENE, {"scene": self.scene_var.get().strip()})

    def apply_value(self, command: str, raw: str, caster: Callable[[str], Any]) -> None:
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            self._append_history(("", f"无效的数值：{raw!r}", True))
            return
        self._command(command, {"value": value})

    def select_region(self) -> None:
        """Drag a new region.

        Non-blocking on purpose: a nested ``wait_window`` loop would keep this
        window's own ``after`` pump from running, so the panel would freeze for as
        long as the selector is up. The selector reports through a callback
        instead, and the overlay's existing request path is reused when there is
        an overlay, so both surfaces share one implementation.
        """
        if self.overlay is not None and hasattr(self.overlay, "request_reselect"):
            self.overlay.request_reselect()
            return

        from .selector import RegionSelector

        def done(region: Region | None) -> None:
            if region is not None:
                self.region_var.set(str(region))
                self._command(CMD_SET_REGION, {"region": str(region)})

        self._selector = RegionSelector(root=self.root, on_done=done)
        self._selector.build()

    def pick_window(self) -> None:
        """Choose a capture window from a list, then apply it."""
        from . import winutil

        windows = winutil.list_windows(exclude_pid=os.getpid())
        if not windows:
            messagebox.showinfo("选择窗口", "没有找到可捕获的窗口。")
            return
        chosen = _WindowPicker(self.root, windows).show()
        if chosen is None:
            return
        result = self._command(CMD_USE_WINDOW, {"spec": str(chosen.title), "hwnd": chosen.hwnd})
        if result.get("ok"):
            self.region_var.set(f"窗口：{chosen.title}")
            self._append_history(
                ("", f"已切换为窗口捕获：{chosen.title}  客户端 {chosen.size_label}", True)
            )

    def export(self) -> None:
        fmt = self.export_format_var.get().strip()
        if not fmt:
            messagebox.showinfo("导出", "没有可用的导出格式（需要提供 export 扩展点的插件）。")
            return
        path = filedialog.asksaveasfilename(
            title=f"导出为 {fmt}", defaultextension=f".{fmt}",
            filetypes=[(fmt.upper(), f"*.{fmt}"), ("所有文件", "*.*")],
        )
        if not path:
            return
        result = self._command(CMD_EXPORT, {"format": fmt, "path": path})
        if result.get("ok"):
            self._append_history(("", f"已导出：{path}", True))

    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        if self.overlay is not None:
            try:
                self.overlay.close(destroy_root=False)
            except Exception:
                pass
        if self.on_quit is not None:
            try:
                self.on_quit()
            except Exception:
                pass
        # The panel runs uvicorn on a daemon thread and keeps port 8765 bound. Left
        # alone it would outlive this window inside a still-running process: the port
        # stays taken and the next launch reports "port in use" for a server nobody can
        # see. Stop it explicitly, and tolerate a panel that never started.
        panel = getattr(self, "_panel", None)
        if panel is not None:
            try:
                stopped = panel.stop()
            except Exception:
                stopped = False
            # Only forget the panel if it really went down. A panel whose thread refused
            # to stop still owns the port and still serves *this* session, so keeping the
            # handle lets a later click reopen the server that is actually alive instead
            # of building a second one that cannot bind.
            if stopped:
                self._panel = None
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


class _WindowPicker:
    """A small modal list of capturable windows."""

    def __init__(self, parent: tk.Misc, windows: list[Any]) -> None:
        self.windows = windows
        self.chosen = None
        self.top = tk.Toplevel(parent)
        self.top.title("选择要捕获的窗口")
        self.top.geometry("620x400")
        self.top.transient(parent)
        self.top.grab_set()

        ttk.Label(
            self.top, text="按面积从大到小排列；输入法宿主与显卡叠加层也会出现在这里，请按标题选择。",
            wraplength=580, justify="left", foreground="#5f7386",
        ).pack(fill="x", padx=10, pady=(10, 4))

        self.listbox = tk.Listbox(self.top, activestyle="dotbox")
        self.listbox.pack(fill="both", expand=True, padx=10)
        for index, window in enumerate(windows):
            marker = "  [已最小化]" if window.minimized else ""
            self.listbox.insert(
                "end", f"[{index:2d}] {window.label()}{marker}"
            )
        if windows:
            self.listbox.selection_set(0)
        self.listbox.bind("<Double-Button-1>", lambda _e: self._accept())

        row = ttk.Frame(self.top, padding=10)
        row.pack(fill="x")
        ttk.Button(row, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(row, text="选择", command=self._accept).pack(side="right", padx=6)

    def _accept(self) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.chosen = self.windows[selection[0]]
        self._close()

    def _cancel(self) -> None:
        self.chosen = None
        self._close()

    def _close(self) -> None:
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass

    def show(self) -> Any:
        self.top.wait_window()
        return self.chosen
