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
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .capture import Region
from .correct import SCOPE_LINE, SCOPE_TERM
from .events import (
    CMD_CORRECT,
    CMD_EXPORT,
    CMD_LOAD_PROFILE,
    CMD_SET_DIFF_THRESHOLD,
    CMD_SET_FPS,
    CMD_SET_PRESENTATION,
    CMD_SET_REGION,
    CMD_SET_TARGET_LANG,
    CMD_TOGGLE_PAUSE,
    CMD_USE_WINDOW,
    EVENT_CORRECTION,
    EVENT_ERROR,
    EVENT_PRESENTATION,
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
from .presentation import PresentationSpec

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
        self.spec: PresentationSpec | None = None
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
        self.root.protocol("WM_DELETE_WINDOW", self.close)

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
        ttk.Button(controls, text="重载语料库", command=self.reload_corpus).pack(side="left")

        # ---- tabs -------------------------------------------------------- #
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.notebook = notebook
        self._build_subtitles(notebook)
        self._build_capture(notebook)
        self._build_translation(notebook)
        self._build_presentation(notebook)
        self._build_plugins(notebook)
        self._build_settings(notebook)
        self._build_diagnostics(notebook)

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

    def _build_translation(self, notebook: ttk.Notebook) -> None:
        frame = self._tab(notebook, "翻译")
        self.target_var = tk.StringVar(value="zh-CN")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="目标语言", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.target_var, width=14).pack(side="left")
        ttk.Button(row, text="应用", command=self.apply_target).pack(side="left", padx=6)

        ttk.Label(frame, text="内存档位 / 配置档").pack(anchor="w", pady=(12, 4))
        self.profiles_frame = ttk.Frame(frame)
        self.profiles_frame.pack(fill="x")
        ttk.Label(
            frame,
            text="lean 不加载模型（约 175 MiB）；balanced 常规桌面用法（约 890 MiB）。",
            foreground="#5f7386",
        ).pack(anchor="w", pady=(6, 0))

        ttk.Label(frame, text="语料库").pack(anchor="w", pady=(14, 4))
        self.corpus_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.corpus_var, foreground="#5f7386").pack(anchor="w")
        ttk.Label(
            frame,
            text="词条是磁盘上的文件；直接改文件即可，引擎按修改时间自动热加载。",
            foreground="#5f7386",
        ).pack(anchor="w", pady=(4, 0))

    def _build_presentation(self, notebook: ttk.Notebook) -> None:
        frame = self._tab(notebook, "呈现")
        ttk.Label(frame, text="预设").pack(anchor="w")
        self.presets_frame = ttk.Frame(frame)
        self.presets_frame.pack(fill="x", pady=(4, 10))
        self.spec_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.spec_var, foreground="#5f7386",
                  justify="left").pack(anchor="w")
        ttk.Label(
            frame,
            text="改动会立即广播给悬浮窗——两者共用同一份声明式呈现规格。",
            foreground="#5f7386",
        ).pack(anchor="w", pady=(8, 0))

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

    def _build_settings(self, notebook: ttk.Notebook) -> None:
        """Every setting, with the same explanation the web panel shows.

        Read-only on purpose. The division of labour settled earlier is that the web
        panel adjusts settings and this window controls and displays; a second editor
        here would be a second place for the two to disagree. What this tab is for is
        answering "what is this program doing and why", which is the complaint that
        started this work -- so it shows all 49 settings with their descriptions,
        their current values, and whether changing one needs a restart.

        It refreshes whenever any surface changes a setting, so the reference cannot
        go stale while the window is open.
        """
        frame = self._tab(notebook, "设置")
        ttk.Label(
            frame,
            text="全部设置及其说明。调整请用 Web 面板（run.cmd --serve）；"
                 "这里随任何界面的改动自动刷新。",
            foreground="#5f7386", justify="left", wraplength=880,
        ).pack(anchor="w", pady=(0, 6))
        self.settings_header = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.settings_header, foreground="#5f7386",
                  justify="left", wraplength=880).pack(anchor="w", pady=(0, 6))
        self.settings_box = tk.Text(
            frame, wrap="word", bg="#0b0f13", fg="#c9d6e0", relief="flat",
            insertwidth=0, font=("Microsoft YaHei UI", 10), padx=10, pady=8,
        )
        self.settings_box.pack(fill="both", expand=True)
        self.settings_box.tag_configure("cat", foreground="#7fc4ff",
                                        font=("Microsoft YaHei UI", 11, "bold"),
                                        spacing1=10, spacing3=4)
        self.settings_box.tag_configure("label", foreground="#e6eef5",
                                        font=("Microsoft YaHei UI", 10, "bold"))
        self.settings_box.tag_configure("desc", foreground="#93a6b5")
        self.settings_box.tag_configure("meta", foreground="#5f7386")
        self.settings_box.tag_configure("warn", foreground="#e0b070")
        self.settings_box.configure(state="disabled")

    def _refresh_settings(self) -> None:
        if self._closed or self.session is None:
            return
        try:
            payload = self.session.settings_payload()
        except Exception as exc:  # a broken payload must not take the window down
            self.settings_header.set(f"读取设置失败：{exc}")
            return
        overridden = set(payload.get("overridden") or [])
        self.settings_header.set(
            f"{payload.get('live_count', 0)} 项立即生效 · "
            f"{payload.get('restart_count', 0)} 项需重启 · "
            f"已修改 {len(overridden)} 项 · 配置文件 {payload.get('overrides_file')}"
        )
        box = self.settings_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        for category in payload.get("categories", []):
            box.insert("end", f"{category['title']}\n", ("cat",))
            if category.get("summary"):
                box.insert("end", f"  {category['summary']}\n", ("meta",))
            for field in category.get("fields", []):
                value = (category.get("values") or {}).get(field["key"])
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value) or "(空)"
                elif value is None:
                    value = "(空)"
                marker = "  [已修改]" if field["key"] in overridden else ""
                needs = "需重启" if field.get("applies") == "restart" else "立即生效"
                box.insert(
                    "end",
                    f"  {field['label']}  =  {value}{marker}   ({needs})\n",
                    ("label",),
                )
                box.insert("end", f"      {field['description']}\n", ("desc",))
                bits = [field["key"]]
                if field.get("low") is not None and field.get("high") is not None:
                    bits.append(
                        f"范围 {field['low']}–{field['high']}{field.get('unit', '')}"
                    )
                bits.append(f"默认 {field.get('default')!r}")
                if field.get("note"):
                    bits.append(f"提示 {field['note']}")
                box.insert("end", "      " + " · ".join(bits) + "\n", ("meta",))
                if field.get("danger"):
                    box.insert("end", f"      ⚠ {field['danger']}\n", ("warn",))
            box.insert("end", "\n")
        box.configure(state="disabled")

    def _build_diagnostics(self, notebook: ttk.Notebook) -> None:
        frame = self._tab(notebook, "诊断")
        self.stats_box = tk.Text(
            frame, height=18, wrap="none", bg="#0b0f13", fg="#c9d6e0",
            insertwidth=0, relief="flat", font=("Consolas", 10),
        )
        self.stats_box.pack(fill="both", expand=True)
        self.stats_box.configure(state="disabled")

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
                # Some surface changed a setting. Re-read rather than trust the
                # event's payload: the point of the linkage is that both surfaces
                # show the same state, and the config is that state.
                self._refresh_settings()
                self._append_history(
                    ("", "设置已由其他界面更新：" + "、".join(data.get("changed") or []), True)
                )
            elif kind == EVENT_PRESENTATION:
                self._refresh_presentation(PresentationSpec.from_dict(data))
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
        self.fps_var.set(str(info.get("fps_target", "")))
        self.diff_var.set(str(info.get("diff_threshold", "")))
        self.corpus_var.set(
            f"{info.get('corpus_entries')} 条词条 · {info.get('rules')} 条规则 · "
            f"{info.get('backend')} · 语料库语言：{info.get('corpus_languages') or '未标注'}"
        )
        if isinstance(info.get("presentation"), dict):
            self._refresh_presentation(PresentationSpec.from_dict(info["presentation"]))

        for child in list(self.profiles_frame.winfo_children()):
            child.destroy()
        for name in info.get("profiles") or []:
            ttk.Button(
                self.profiles_frame, text=name,
                command=lambda n=name: self.apply_profile(n),
            ).pack(side="left", padx=(0, 6))

        plugins = info.get("plugins") or {}
        formats = plugins.get("export_formats") or []
        self.export_combo.configure(values=formats)
        if formats and not self.export_format_var.get():
            self.export_format_var.set(formats[0])
        self._render_plugin_status(plugins)
        self._refresh_settings()

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
        self.status_var.set(
            f"{paused} · {s.backend} · {s.fps:.1f} FPS · OCR {s.ocr_ms:.0f} ms · "
            f"总 {s.total_ms:.0f} ms · 内存 {s.memory_mib:.0f} MiB · "
            f"识别 {s.frames} · 跳过 {s.skipped} · 精修 {s.refinements}"
        )
        self.pause_button.configure(text="继续识别" if s.paused else "暂停识别")
        box = self.stats_box
        if box is not None:
            payload = {
                "fps": round(s.fps, 2),
                "ocr_ms": round(s.ocr_ms, 2),
                "translate_ms": round(s.translate_ms, 2),
                "total_ms": round(s.total_ms, 2),
                "frames": s.frames,
                "skipped_unchanged": s.skipped,
                "corpus_entries": s.corpus_entries,
                "rules": s.rules,
                "cache_hit_rate": round(s.cache_hit_rate, 3),
                "refinements": s.refinements,
                "refinements_pending": s.refinements_pending,
                "refinements_dropped": s.refinements_dropped,
                "memory_mib": round(s.memory_mib, 1),
                "paused": s.paused,
                "backend": s.backend,
                # the last engine status message, which is where a refused or
                # unusual transition explains itself
                "status": self._last_status,
            }
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("end", json.dumps(payload, ensure_ascii=False, indent=2))
            box.configure(state="disabled")

    def _refresh_presentation(self, spec: PresentationSpec) -> None:
        self.spec = spec
        for child in list(self.presets_frame.winfo_children()):
            child.destroy()
        for name in PresentationSpec.preset_names():
            ttk.Button(
                self.presets_frame, text=name,
                command=lambda n=name: self.apply_presentation(n),
            ).pack(side="left", padx=(0, 6))
        self.spec_var.set(
            f"当前：{spec.name} · 布局 {spec.layout.mode} · "
            f"锚点 {spec.layout.anchor} · 元素 "
            f"{', '.join(e.role for e in spec.ordered_elements())}"
        )

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
        self._command(CMD_TOGGLE_PAUSE)

    def reload_corpus(self) -> None:
        result = self._command("reload_corpus")
        if result.get("ok"):
            self._append_history(("", f"语料库已重载：{result.get('detail')}", True))

    def apply_target(self) -> None:
        self._command(CMD_SET_TARGET_LANG, {"target_lang": self.target_var.get().strip()})

    def apply_value(self, command: str, raw: str, caster: Callable[[str], Any]) -> None:
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            self._append_history(("", f"无效的数值：{raw!r}", True))
            return
        self._command(command, {"value": value})

    def apply_profile(self, name: str) -> None:
        result = self._command(CMD_LOAD_PROFILE, {"name": name})
        if result.get("ok"):
            self.profile_var.set(f"配置档：{name}")

    def apply_presentation(self, name: str) -> None:
        self._command(CMD_SET_PRESENTATION, {"preset": name})

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
