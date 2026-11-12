"""Modern, dependency-free installer UI (Tkinter) for Open QQ Dot.

The installer has to run before the app's Python environment exists, so this
module deliberately uses only the standard library.  The visual language
matches the app: deep navy background, one blue accent, rounded cards, a step
timeline and a live log.  ``--preview`` opens the window without installing.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

BG = "#EFF6FF"
CARD = "#FFFFFF"
CARD_2 = "#DBEAFE"
INK = "#0F172A"
MUTED = "#64748B"
ACCENT = "#2563EB"
ACCENT_2 = "#1D4ED8"
GREEN = "#059669"
RED = "#DC2626"
LINE = "#BFDBFE"
STEPS = ("检查环境", "安装程序文件", "配置 Python 环境", "完成")


def _font(size=11, weight="normal"):
    return ("Microsoft YaHei UI", size, weight)


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, *, primary=True, width=200, height=42):
        super().__init__(parent, width=width, height=height, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0)
        self.text, self.command = text, command
        self.primary = primary
        self.width, self.height = width, height
        self.enabled = True
        self._hover = False
        self.bind("<Enter>", lambda _: self._set_hover(True))
        self.bind("<Leave>", lambda _: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self.paint()

    def _round(self, color):
        self.delete("all")
        r = 12
        x0, y0, x1, y1 = 1, 1, self.width - 1, self.height - 1
        points = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
                  x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        self.create_polygon(points, smooth=True, fill=color, outline="")
        text_color = "#FFFFFF" if self.primary else INK
        if not self.enabled:
            text_color = MUTED
        self.create_text(self.width // 2, self.height // 2, text=self.text,
                         fill=text_color, font=_font(10, "bold"))

    def paint(self):
        if not self.enabled:
            color = "#E2E8F0"
        elif self.primary:
            color = ACCENT_2 if self._hover else ACCENT
        else:
            color = "#BFDBFE" if self._hover else "#DBEAFE"
        self._round(color)

    def _set_hover(self, value):
        self._hover = value
        self.paint()

    def _click(self, _event):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, value):
        self.enabled = bool(value)
        self.paint()

    def set_text(self, text):
        self.text = text
        self.paint()


class StepTimeline(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=CARD)
        self.rows = []
        for index, label in enumerate(STEPS):
            row = tk.Frame(self, bg=CARD)
            row.pack(fill="x", pady=4)
            dot = tk.Canvas(row, width=22, height=22, bg=CARD, highlightthickness=0)
            dot.pack(side="left")
            text = tk.Label(row, text=label, bg=CARD, fg=MUTED, font=_font(10), anchor="w")
            text.pack(side="left", padx=8)
            self.rows.append((dot, text))

    def set(self, active: int, done: bool = False):
        for index, (dot, text) in enumerate(self.rows):
            dot.delete("all")
            completed = index < active or (done and index <= active)
            current = index == active and not done
            color = GREEN if completed else (ACCENT if current else "#31415E")
            dot.create_oval(3, 3, 19, 19, outline=color, width=2, fill=color if completed else CARD)
            if completed:
                dot.create_line(7, 11, 10, 14, 16, 8, fill="#0B1220", width=2)
            text.config(fg=INK if completed or current else MUTED,
                        font=_font(10, "bold" if current else "normal"))


class InstallerApp:
    def __init__(self, resources: Path, *, preview: bool = False, destination: Path | None = None):
        self.resources = Path(resources)
        self.preview = preview
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.done = False
        self.cancel_event = threading.Event()
        self.root = tk.Tk()
        self.log_visible = tk.BooleanVar(value=False)
        self.root.title("Open QQ Dot 安装器")
        self.root.geometry("920x680")
        self.root.minsize(860, 640)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(default=str(self.resources / "assets" / "app-icon.ico"))
        except Exception:
            pass
        self.destination = tk.StringVar(value=str(destination or self.default_destination()))
        self.shortcut = tk.BooleanVar(value=True)
        self.autostart = tk.BooleanVar(value=False)
        self.launch = tk.BooleanVar(value=True)
        self.associate = tk.BooleanVar(value=True)
        self.runtimes = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="准备就绪。安装会保留已有好友、聊天记录与登录状态。")
        self.percent = tk.DoubleVar(value=0.0)
        self._build()
        self._center()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(90, self._poll)

    # ---------- layout ----------
    def default_destination(self) -> Path:
        from windows_installer import default_destination
        return default_destination()

    def _center(self, height=680):
        self.root.update_idletasks()
        width = min(920, self.root.winfo_screenwidth() - 24)
        height = min(height, self.root.winfo_screenheight() - 80)
        self.root.minsize(min(860, width), min(640, height))
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    def _card(self, parent, padding=18):
        frame = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        inner = tk.Frame(frame, bg=CARD)
        inner.pack(fill="both", expand=True, padx=padding, pady=padding)
        return frame, inner

    def _build(self):
        header = tk.Canvas(self.root, height=150, bg=BG, highlightthickness=0)
        header.pack(fill="x")
        header.bind("<Configure>", self._paint_header)
        self.header = header

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=(4, 0))
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)

        left, left_inner = self._card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        tk.Label(left_inner, text="安装位置", bg=CARD, fg=INK, font=_font(11, "bold"), anchor="w").pack(fill="x")
        path_row = tk.Frame(left_inner, bg=CARD)
        path_row.pack(fill="x", pady=(8, 14))
        entry = tk.Entry(path_row, textvariable=self.destination, bg="#F8FAFC", fg=INK,
                         insertbackground=INK, relief="flat", font=_font(10))
        entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        RoundButton(path_row, "选择目录", self._browse, primary=False, width=110, height=38).pack(side="right")

        tk.Label(left_inner, text="安装选项", bg=CARD, fg=INK, font=_font(11, "bold"), anchor="w").pack(fill="x")
        for text, variable in (("创建桌面快捷方式", self.shortcut), ("开机自动启动（可随时关闭）", self.autostart),
                               ("关联 .oqtc / .oqdbackup 文件（双击打开）", self.associate),
                               ("安装完成后打开应用", self.launch),
                               ("同时安装本地向量运行环境（PyTorch CPU，约 260 MB）", self.runtimes)):
            check = tk.Checkbutton(left_inner, text=text, variable=variable, bg=CARD, fg=INK,
                                   activebackground=CARD, activeforeground=INK, selectcolor="#DBEAFE",
                                   font=_font(10), anchor="w", highlightthickness=0, bd=0)
            check.pack(fill="x", pady=2)

        tk.Label(left_inner, text="说明", bg=CARD, fg=INK, font=_font(11, "bold"), anchor="w").pack(fill="x", pady=(14, 4))
        tk.Label(left_inner, text="一键完成：校验安装包 → 复制程序 → 准备独立 Python 环境 → 安装依赖 → 创建快捷方式 → 启动。\n"
                                  "QQ 直连运行时（NapCat）不在安装时下载；首次点连接会先征得确认，再从官方下载并附完整许可证。\n"
                                  "更新时不会覆盖 friends/、data/ 等私人数据；需要更多空间约 2GB。",
                 bg=CARD, fg=MUTED, font=_font(9), justify="left", anchor="w", wraplength=470).pack(fill="x")

        right, right_inner = self._card(body)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right_inner, text="部署进度", bg=CARD, fg=INK, font=_font(11, "bold"), anchor="w").pack(fill="x")
        self.timeline = StepTimeline(right_inner)
        self.timeline.pack(fill="x", pady=(10, 12))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Install.Horizontal.TProgressbar", troughcolor="#DBEAFE", background=ACCENT,
                        bordercolor="#BFDBFE", lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        ttk.Progressbar(right_inner, variable=self.percent, maximum=100,
                        style="Install.Horizontal.TProgressbar").pack(fill="x")
        self.percent_label = tk.Label(right_inner, text="0%", bg=CARD, fg=MUTED, font=_font(9), anchor="e")
        self.percent_label.pack(fill="x", pady=(4, 10))
        self.status_label = tk.Label(right_inner, textvariable=self.status, bg=CARD, fg=INK,
                                     font=_font(9), justify="left", anchor="w", wraplength=280)
        self.status_label.pack(fill="x")
        self.action = RoundButton(right_inner, "一键安装 / 更新", self._start, width=280, height=46)
        self.action.pack(pady=(18, 6))
        self.cancel_button = RoundButton(right_inner, "取消安装", self._cancel, primary=False, width=280, height=32)
        self.cancel_button.pack(pady=(0, 6))
        self.cancel_button.set_enabled(False)

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="both", expand=False, padx=28, pady=(12, 16))
        self.log_toggle = tk.Checkbutton(footer, text="显示安装日志", variable=self.log_visible,
                                         command=self._toggle_log, bg=BG, fg=MUTED, activebackground=BG,
                                         activeforeground=INK, selectcolor=BG, font=_font(9),
                                         highlightthickness=0, bd=0)
        self.log_toggle.pack(side="left")
        self.log_button = RoundButton(footer, "打开日志", self._open_log, primary=False, width=110, height=34)
        self.log_button.pack(side="right", padx=(0, 8))
        self.open_button = RoundButton(footer, "打开安装目录", self._open_folder, primary=False, width=150, height=34)
        self.open_button.pack(side="right")
        self.open_button.set_enabled(False)
        self.uninstall_button = RoundButton(footer, "卸载程序", self._uninstall, primary=False, width=130, height=34)
        self.uninstall_button.pack(side="right", padx=(0, 8))
        self.log_frame = tk.Frame(self.root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        self.log = tk.Text(self.log_frame, height=7, bg="#F8FAFC", fg="#334155", insertbackground=INK,
                           relief="flat", font=("Consolas", 9), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=8)

    def _paint_header(self, event):
        canvas = self.header
        canvas.delete("all")
        width, height = max(1, event.width), 150
        for y in range(height):
            ratio = y / height
            r = int(219 + (239 - 219) * ratio)
            g = int(234 + (246 - 234) * ratio)
            b = int(254 + (255 - 254) * ratio)
            canvas.create_line(0, y, width, y, fill=f"#{r:02x}{g:02x}{b:02x}")
        canvas.create_oval(width - 280, -120, width + 80, 200, outline="", fill="#BFDBFE")
        canvas.create_text(38, 46, text="Open QQ Dot", fill=INK, font=("Microsoft YaHei UI", 24, "bold"), anchor="w")
        canvas.create_text(40, 84, text="桌面 AI 伙伴 · 一键部署", fill="#1D4ED8",
                           font=("Microsoft YaHei UI", 11), anchor="w")
        canvas.create_text(40, 112, text="本地运行 · 数据留在自己电脑 · 保留已有好友与聊天记录",
                           fill=MUTED, font=("Microsoft YaHei UI", 9), anchor="w")
        try:
            image = tk.PhotoImage(file=str(self.resources / "assets" / "app-icon.png"))
            factor = max(1, image.width() // 96)
            self._icon = image.subsample(factor, factor)
            canvas.create_image(width - 90, 72, image=self._icon)
        except Exception:
            canvas.create_oval(width - 126, 34, width - 54, 106, fill=ACCENT, outline="")

    def _toggle_log(self):
        if self.log_visible.get():
            self.log_frame.pack(fill="both", expand=True, padx=28, pady=(0, 16))
            self._center(820)
        else:
            self.log_frame.pack_forget()
            self._center()

    def _browse(self):
        selected = filedialog.askdirectory(title="选择安装目录", initialdir=str(Path(self.destination.get()).parent))
        if selected:
            self.destination.set(selected)

    def _uninstall(self):
        from tkinter import messagebox
        from windows_installer import uninstall
        destination = Path(self.destination.get())
        if not messagebox.askyesno("卸载 Open QQ Dot",
                                   "移除程序文件，但保留 friends/、data/、workspaces/ 等私人数据？\n\n"
                                   "程序文件会被删除，私人数据不会自动删除。"):
            return
        try:
            result = uninstall(destination, self._append, keep_data=True)
        except Exception as exc:
            messagebox.showerror("卸载失败", str(exc))
            return
        self.status.set("已卸载程序文件；保留：" + ("、".join(result["kept"]) or "无"))
        self.action.set_enabled(False)

    def _open_folder(self):
        target = Path(self.destination.get())
        if target.is_dir():
            os.startfile(target)  # noqa: S606 - local folder chosen by the user

    def _open_log(self):
        log = Path(self.destination.get()) / "installation.log"
        if not log.is_file():
            return messagebox.showinfo("还没有日志", "安装尚未开始，暂无日志。")
        os.startfile(log)  # noqa: S606 - fixed path inside the chosen install directory

    def _append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _start(self):
        if self.busy:
            return
        if self.done:
            self._launch_existing()
            return
        destination = self.destination.get().strip()
        if not destination:
            return messagebox.showerror("需要安装目录", "请选择一个安装目录。")
        if not self.preview:
            from windows_installer import preflight
            check = preflight(Path(destination), self.resources)
            if not check["ok"]:
                return messagebox.showerror("无法开始安装", "\n".join(check["errors"]))
            for warning in check["warnings"]:
                self._append("提示：" + warning)
            if check["update"]:
                self.status.set("检测到已有安装：将保留好友与聊天记录并更新。")
        self.busy = True
        self.cancel_event.clear()
        self.cancel_button.set_enabled(True)
        self.action.set_enabled(False)
        self.action.set_text("正在安装…")
        self.percent.set(0)
        self.percent_label.config(text="0%")
        self.timeline.set(0)
        self.status.set("正在检查安装环境…")
        options = {"shortcut": self.shortcut.get(), "autostart": self.autostart.get(),
                   "associate": self.associate.get(), "runtimes": self.runtimes.get(),
                   "launch": self.launch.get() and not self.preview}
        if self.preview:
            self.root.after(700, lambda: self._finish({"ok": True, "preview": True}))
            return

        def work():
            try:
                from windows_installer import InstallCancelled, run_install
                run_install(self.resources, Path(destination), self._emit, launch=options["launch"],
                            options=options, on_step=self._step, cancel_event=self.cancel_event,
                            on_progress=lambda value: self.events.put(("progress", value)))
                self.events.put(("finish", {"ok": True}))
            except InstallCancelled:
                self.events.put(("cancelled", "安装已取消，已复制的文件保留。"))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        if not self.busy:
            return
        self.cancel_event.set()
        self.cancel_button.set_enabled(False)
        self.status.set("正在取消安装，请稍候…")
        self._append("正在取消安装……")

    def _step(self, index, label=""):
        self.events.put(("step", (index, label, {0: 6, 1: 26, 2: 58, 3: 96}.get(index, 0))))

    def _emit(self, text):
        self.events.put(("log", text))

    def _poll(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "log":
                self._append(value)
                lowered = value.lower()
                if "校验" in value:
                    self._advance(0, 6)
                elif "复制" in value or "安装程序文件" in value:
                    self._advance(1, 26)
                elif "python" in lowered or "依赖" in value:
                    self._advance(2, 58)
                elif "完成" in value:
                    self._advance(3, 96)
            elif kind == "step":
                index, label, percent = value
                self.timeline.set(index)
                self._set_percent(percent)
                if label:
                    self.status.set(label)
            elif kind == "progress":
                if value.get("percent") is not None:
                    self._set_percent(value["percent"])
                if value.get("text"):
                    self.status.set(str(value["text"])[:120])
            elif kind == "finish":
                self._finish(value)
            elif kind == "error":
                self._fail(value)
            elif kind == "cancelled":
                self._cancelled(value)
        self.root.after(90, self._poll)

    def _advance(self, index, percent):
        self.timeline.set(index)
        self._set_percent(percent)

    def _set_percent(self, value):
        self.percent.set(max(self.percent.get(), value))
        self.percent_label.config(text=f"{int(self.percent.get())}%")

    def _finish(self, value):
        self.busy = False
        self.done = True
        self.cancel_button.set_enabled(False)
        self.timeline.set(3, done=True)
        self._set_percent(100)
        if value.get("preview"):
            self.status.set("预览模式：界面正常，未写入任何文件。")
            self.action.set_text("预览完成")
            self.action.set_enabled(True)
            return
        self.status.set("安装完成。好友数据、聊天记录与登录状态均已保留。")
        self.action.set_text("启动 Open QQ Dot")
        self.action.set_enabled(True)
        self.action.command = self._launch_existing
        self.open_button.set_enabled(True)
        self._append("安装完成。")
    def _fail(self, text):
        self.busy = False
        self.cancel_button.set_enabled(False)
        self.action.set_enabled(True)
        self.action.set_text("重试安装")
        self.status.set("安装未完成，可以修正后重试；已复制的文件会保留在 installation-backup。")
        self._append("错误：" + text)
        messagebox.showerror("安装未完成", text[:1500])

    def _cancelled(self, text):
        self.busy = False
        self.cancel_button.set_enabled(False)
        self.action.set_enabled(True)
        self.action.set_text("重新安装")
        self.status.set(text)
        self._append(text)

    def _launch_existing(self):
        try:
            from windows_installer import launch_app
            launch_app(Path(self.destination.get()))
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))

    def _close(self):
        if self.busy:
            if not messagebox.askyesno("安装正在进行", "现在关闭会中断安装，确定关闭吗？"):
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run(resources: Path, preview: bool = False, destination: Path | None = None) -> int:
    InstallerApp(resources, preview=preview, destination=destination).run()
    return 0


if __name__ == "__main__":
    import sys
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    raise SystemExit(run(root, "--preview" in sys.argv[1:]))
