"""微信聊天助手 · 侧面板。

**Owner 附属窗口模式**：
  • 面板是独立顶层窗口（WS_POPUP），不是微信的子窗口
  • 通过 Win32 `GWLP_HWNDPARENT` 把微信设为我们的 **Owner**
  • 效果：跟随微信最小化/还原、永远在微信之上、但**不进入微信的子窗口列表**
    → 规避了 SetParent 跨进程嵌入导致微信闪退的已知问题
  • 位置仍用追踪器轮询微信 rect 来更新

**WeChatFerry 改为手动启用**（之前自动注入可能是闪退元凶）：
  • 启动时不注入，面板顶部有「启用 WCF」按钮，用户主动点才注入
  • 不启用时走传统剪贴板 + Ctrl+V 发送
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import pyperclip
from PIL import Image, ImageTk

from . import accessibility as ax
from . import wcferry_client as wcf
from . import wechat_embed as embed
from .capture import capture_region
from .llm import generate_reply, generate_reply_stream
from .ocr import ocr_image, backend_info as _ocr_backend
from .sender import send_messages_to_wechat, send_to_wechat
from .styles import (
    DEFAULT_STYLE,
    STYLES,
    build_prompt,
    opponent_length_since_my_last,
    split_replies,
)
from .window_tracker import (
    PANEL_WIDTH,
    WeChatState,
    WeChatTracker,
    get_wechat_state,
    panel_geometry,
)

# ── 视觉常量 ────────────────────────────────────────────────
BG         = "#f5f5f5"
HEADER_BG  = "#07c160"
CARD_BG    = "#ffffff"
TEXT_FG    = "#1a1a1a"
MUTED_FG   = "#767676"
OK_FG      = "#107c10"
ERR_FG     = "#c50f1f"
BORDER     = "#d0d0d0"

FONT_BASE = ("Microsoft YaHei UI", 9)
FONT_BODY = ("Microsoft YaHei UI", 10)
FONT_H    = ("Microsoft YaHei UI", 10, "bold")
FONT_SM   = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")

THUMB_MAX_W = 160
THUMB_MAX_H = 60
MULTI_STYLES: tuple[str, str, str] = ("幽默", "认真", "温柔")

DEFAULT_H = 760
TITLE_BAR_H = 36


class PanelUI:
    """竖版侧面板：优先嵌入微信，失败则退化为叠加跟随。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("微信聊天助手")
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.geometry(f"{PANEL_WIDTH}x{DEFAULT_H}+100+80")

        # 运行时状态
        self._drag_x = 0
        self._drag_y = 0
        self._tracking = True              # 是否跟随微信自动定位
        self._self_hwnd: int | None = None
        self._owner_hwnd: int | None = None  # 当前 Owner（微信 HWND）

        # UI 状态
        self.auto_regen       = tk.BooleanVar(value=True)
        self.auto_send_var    = tk.BooleanVar(value=False)
        self.mimic_var        = tk.BooleanVar(value=True)
        self.align_length_var = tk.BooleanVar(value=True)   # 和对方累计字数对齐
        self.reply_count_var  = tk.IntVar(value=1)          # 发送几句
        self.style_var        = tk.StringVar(value=DEFAULT_STYLE)
        self.extra_var        = tk.StringVar()
        self.contact_var      = tk.StringVar(value="")

        self._last_capture_path: str | None = None
        self._thumb_image: ImageTk.PhotoImage | None = None
        self._last_prompt: str | None = None
        self._multi_remaining = 0
        self._wcf_status = "未启用"

        self._apply_ttk_style()
        self._build_widgets()
        self.root.update_idletasks()

        # 拿到自己的顶层 HWND（Tk 的 winfo_id() 返回内部子 HWND，要向上找根）
        try:
            raw = self.root.winfo_id()
            self._self_hwnd = embed.get_toplevel_hwnd(raw)
            print(f"[panel] self HWND: raw={raw} top={self._self_hwnd}", flush=True)
        except Exception:  # noqa: BLE001
            self._self_hwnd = None

        # 启动微信窗口追踪
        self._tracker = WeChatTracker(self._on_wechat_state_changed)
        self._tracker.start()

        # 初始态：若微信已在，立即贴过去
        initial = get_wechat_state()
        if initial:
            self.root.after(80, lambda: self._on_wechat_state_changed(initial))

    # ── ttk 主题 ────────────────────────────────────────────
    def _apply_ttk_style(self) -> None:
        s = ttk.Style()
        for theme in ("vista", "winnative", "xpnative", "clam"):
            try:
                s.theme_use(theme)
                break
            except tk.TclError:
                continue
        s.configure("TFrame",        background=BG)
        s.configure("TLabel",        background=BG, font=FONT_BASE, foreground=TEXT_FG)
        s.configure("Heading.TLabel", background=BG, font=FONT_H,    foreground=TEXT_FG)
        s.configure("Muted.TLabel",  background=BG, font=FONT_SM,    foreground=MUTED_FG)
        s.configure("Status.TLabel", background=BG, font=FONT_SM,    foreground=MUTED_FG)
        s.configure("TCheckbutton",  background=BG, font=FONT_BASE)
        s.configure("TRadiobutton",  background=BG, font=FONT_BASE)
        s.configure("TLabelframe",   background=BG, font=FONT_H,     foreground=TEXT_FG)
        s.configure("TLabelframe.Label", background=BG, font=FONT_H, foreground=TEXT_FG)
        s.configure("TButton",       font=FONT_BASE)
        s.configure("TNotebook",     background=BG)
        s.configure("TNotebook.Tab", font=FONT_BASE, padding=(10, 4))
        s.configure("TEntry",        font=FONT_BODY)
        s.configure("TCombobox",     font=FONT_BASE)

    # ── UI 构建 ─────────────────────────────────────────────
    def _build_widgets(self) -> None:
        self._build_titlebar()
        self._build_wcf_row()
        self._build_body()

    def _build_titlebar(self) -> None:
        bar = tk.Frame(self.root, bg=HEADER_BG, height=TITLE_BAR_H)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>",     self._drag_move)

        title = tk.Label(
            bar, text="💬 微信聊天助手",
            bg=HEADER_BG, fg="white", font=FONT_TITLE,
        )
        title.pack(side=tk.LEFT, padx=12)
        title.bind("<ButtonPress-1>", self._drag_start)
        title.bind("<B1-Motion>",     self._drag_move)

        self.status = tk.Label(
            bar, text="就绪", bg=HEADER_BG, fg="#e8ffe8", font=FONT_SM,
        )
        self.status.pack(side=tk.LEFT, padx=6)

        btn_close = tk.Label(
            bar, text="✕", bg=HEADER_BG, fg="white",
            font=FONT_H, cursor="hand2", padx=10,
        )
        btn_close.pack(side=tk.RIGHT)
        btn_close.bind("<Button-1>", lambda _: self.hide())
        btn_close.bind("<Enter>", lambda _: btn_close.config(fg="#ffcccc"))
        btn_close.bind("<Leave>", lambda _: btn_close.config(fg="white"))

    def _build_wcf_row(self) -> None:
        """WCF 状态 + 手动启用按钮 + 联系人选择。"""
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill=tk.X, padx=8, pady=(6, 2))

        self._wcf_label = tk.Label(
            row, text="🔌 WCF：未启用", bg=BG, fg=MUTED_FG, font=FONT_SM,
        )
        self._wcf_label.pack(side=tk.LEFT)

        self._wcf_btn = ttk.Button(
            row, text="启用", width=5, command=self._enable_wcf,
        )
        self._wcf_btn.pack(side=tk.LEFT, padx=(4, 8))

        tk.Label(row, text="联系人：", bg=BG, fg=TEXT_FG, font=FONT_SM).pack(
            side=tk.LEFT)
        self.contact_combo = ttk.Combobox(
            row, textvariable=self.contact_var, values=[],
            state="readonly", font=FONT_SM, width=12,
        )
        self.contact_combo.pack(side=tk.LEFT, padx=2)

        ttk.Button(row, text="↻", width=2, command=self._refresh_contacts).pack(
            side=tk.LEFT, padx=2)

    def _build_body(self) -> None:
        PX = 8

        # Row 1: 数据源
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=PX, pady=(6, 4))
        ttk.Button(top, text="📥 读取微信", command=self.on_read_ax, width=12).pack(side=tk.LEFT)
        ttk.Button(top, text="📸 截图 OCR", command=self.on_capture, width=12).pack(
            side=tk.LEFT, padx=4)

        # Row 2: 截图预览
        preview_frame = ttk.LabelFrame(self.root, text=" 截图预览 ")
        preview_frame.pack(fill=tk.X, padx=PX, pady=4)
        inner = ttk.Frame(preview_frame)
        inner.pack(fill=tk.X, padx=4, pady=4)
        self.thumb_label = tk.Label(
            inner, text="未截图",
            fg=MUTED_FG, bg=CARD_BG, width=22, height=3,
            relief="flat", bd=1,
            highlightthickness=1, highlightbackground=BORDER, font=FONT_SM,
        )
        self.thumb_label.pack(side=tk.LEFT)
        pb = ttk.Frame(inner)
        pb.pack(side=tk.LEFT, padx=6)
        self.preview_open_btn = ttk.Button(
            pb, text="🔍 看大图", width=9,
            command=self.on_open_preview, state=tk.DISABLED,
        )
        self.preview_open_btn.pack(anchor="w", pady=1)
        self.recapture_btn = ttk.Button(
            pb, text="🔁 重截", width=9,
            command=self.on_capture, state=tk.DISABLED,
        )
        self.recapture_btn.pack(anchor="w", pady=1)

        # Row 3: 对话内容
        ttk.Label(self.root, text="对话内容（可编辑）",
                  style="Heading.TLabel").pack(anchor="w", padx=PX, pady=(6, 2))
        chat_frame = ttk.Frame(self.root)
        chat_frame.pack(fill=tk.X, padx=PX, pady=(0, 4))
        chat_scroll = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL)
        chat_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text = tk.Text(
            chat_frame, height=6, wrap=tk.WORD, font=FONT_BODY,
            bg=CARD_BG, fg=TEXT_FG, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            padx=6, pady=4, yscrollcommand=chat_scroll.set,
        )
        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chat_scroll.config(command=self.chat_text.yview)

        # Row 4: 风格
        style_frame = ttk.LabelFrame(self.root, text=" 风格 ")
        style_frame.pack(fill=tk.X, padx=PX, pady=4)
        names = list(STYLES.keys())
        cols = 3
        for i, name in enumerate(names):
            rb = ttk.Radiobutton(
                style_frame, text=name, value=name,
                variable=self.style_var, command=self._on_style_change,
            )
            rb.grid(row=i // cols, column=i % cols, sticky="w", padx=8, pady=2)
        for c in range(cols):
            style_frame.columnconfigure(c, weight=1)

        # Row 5: 额外要求
        extra_row = ttk.Frame(self.root)
        extra_row.pack(fill=tk.X, padx=PX, pady=4)
        ttk.Label(extra_row, text="额外要求").pack(side=tk.LEFT)
        entry = tk.Entry(
            extra_row, textvariable=self.extra_var, font=FONT_BODY,
            bg=CARD_BG, fg=TEXT_FG, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            insertbackground=TEXT_FG,
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, ipady=3)

        # Row 6: 生成按钮
        action = ttk.Frame(self.root)
        action.pack(fill=tk.X, padx=PX, pady=(4, 2))
        self.gen_btn = ttk.Button(action, text="✨ 生成", command=self.on_generate, width=8)
        self.gen_btn.pack(side=tk.LEFT)
        ttk.Button(action, text="🔄 换一条", command=self.on_generate, width=9).pack(
            side=tk.LEFT, padx=3)
        self.multi_btn = ttk.Button(
            action, text="🎲 3 风格对比", command=self.on_generate_multi, width=12)
        self.multi_btn.pack(side=tk.LEFT, padx=3)
        ttk.Button(action, text="📜", width=3,
                   command=self.on_view_prompt).pack(side=tk.LEFT, padx=2)

        # Row 7: 选项
        opts = ttk.Frame(self.root)
        opts.pack(fill=tk.X, padx=PX, pady=(0, 4))
        ttk.Checkbutton(
            opts, text="换风格自动重生成", variable=self.auto_regen
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            opts, text="自动发送", variable=self.auto_send_var
        ).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(
            opts, text="模仿我", variable=self.mimic_var
        ).pack(side=tk.LEFT, padx=4)

        # Row 7b: 长度对齐 + 发送几句
        opts2 = ttk.Frame(self.root)
        opts2.pack(fill=tk.X, padx=PX, pady=(0, 4))
        ttk.Checkbutton(
            opts2, text="长度对齐对方", variable=self.align_length_var,
        ).pack(side=tk.LEFT)
        ttk.Label(opts2, text="发送").pack(side=tk.LEFT, padx=(12, 2))
        ttk.Spinbox(
            opts2, from_=1, to=10, width=3, textvariable=self.reply_count_var,
            font=FONT_BASE,
        ).pack(side=tk.LEFT)
        ttk.Label(opts2, text="句").pack(side=tk.LEFT, padx=(2, 0))

        # Row 8: Notebook
        self.result_nb = ttk.Notebook(self.root)
        self.result_nb.pack(fill=tk.BOTH, expand=True, padx=PX, pady=(4, PX))

        # Tab 1
        tab_single = ttk.Frame(self.result_nb)
        self.result_nb.add(tab_single, text=" 建议回复 ")
        self.reply_text = tk.Text(
            tab_single, height=5, wrap=tk.WORD, font=("Microsoft YaHei UI", 11),
            bg=CARD_BG, fg=TEXT_FG, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER, padx=6, pady=4,
        )
        self.reply_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 3))
        sr = ttk.Frame(tab_single)
        sr.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Button(sr, text="📤 发送", width=8,
                   command=lambda: self.on_send(True)).pack(side=tk.LEFT)
        ttk.Button(sr, text="📝 只粘贴", width=9,
                   command=lambda: self.on_send(False)).pack(side=tk.LEFT, padx=3)
        ttk.Button(sr, text="📋 复制", width=8,
                   command=self.on_copy).pack(side=tk.LEFT, padx=3)

        # Tab 2
        tab_multi = ttk.Frame(self.result_nb)
        self.result_nb.add(tab_multi, text=" 🎲 3 候选 ")
        ttk.Label(
            tab_multi, text="点「选中」→ 回填并复制",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=6, pady=(4, 2))
        self.candidate_widgets: list[dict] = []
        for i in range(3):
            row = ttk.Frame(tab_multi)
            row.pack(fill=tk.BOTH, expand=True, pady=2, padx=6)
            label = ttk.Label(row, text="", width=6, style="Muted.TLabel")
            label.pack(side=tk.LEFT, anchor="n", pady=2)
            ctext = tk.Text(
                row, height=3, wrap=tk.WORD, font=FONT_BASE,
                bg=CARD_BG, fg=TEXT_FG, relief="flat", bd=0,
                highlightthickness=1, highlightbackground=BORDER, padx=4, pady=3,
            )
            ctext.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
            pick_btn = ttk.Button(row, text="选中", state=tk.DISABLED, width=5)
            pick_btn.pack(side=tk.LEFT, anchor="n", pady=2)
            self.candidate_widgets.append({"label": label, "text": ctext, "btn": pick_btn})

    # ── WCF 手动启用（有闪退风险，用户确认后才注入）────────
    def _enable_wcf(self) -> None:
        if not wcf.available():
            messagebox.showerror(
                "WCF 未安装",
                f"wcferry 未导入：{wcf.import_error()}\n\n"
                "请在 conda cpu 环境运行：pip install wcferry",
            )
            return
        confirm = messagebox.askyesno(
            "启用 WeChatFerry（DLL 注入）",
            "启用后会把 spy.dll 注入到微信进程。\n\n"
            "⚠ 风险：\n"
            "  • 注入不兼容会导致微信闪退（尤其是反病毒误拦时）\n"
            "  • 请确认当前微信版本为 3.9.12.51\n\n"
            "确定要启用？",
        )
        if not confirm:
            return
        self._wcf_label.config(text="🔌 WCF：注入中…", fg=MUTED_FG)
        self._wcf_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._wcf_worker, daemon=True).start()

    def _wcf_worker(self) -> None:
        client = wcf.WCFClient.instance()
        ok = client.start(debug=False)
        if ok:
            self._wcf_status = "已注入"
            self.root.after(0, lambda: self._wcf_label.config(
                text="🔌 WCF：已注入 ✓", fg=OK_FG))
            self.root.after(0, self._refresh_contacts)
        else:
            self._wcf_status = f"失败：{client.last_error()[:40]}"
            self.root.after(0, lambda: self._wcf_label.config(
                text="🔌 WCF：失败（保留剪贴板）", fg=ERR_FG))
            self.root.after(0, lambda: self._wcf_btn.config(state=tk.NORMAL))

    def _refresh_contacts(self) -> None:
        def _load() -> None:
            client = wcf.WCFClient.instance()
            if not client.is_running():
                return
            contacts = client.get_contacts(refresh=True)
            # 只留个人联系人（过滤掉群 / 公众号 / 系统号）
            names: list[str] = []
            for c in contacts:
                wxid = c["wxid"] or ""
                if wxid.startswith("wxid_") or (wxid and not wxid.startswith("gh_") and "@" not in wxid):
                    name = c["remark"] or c["name"]
                    if name:
                        names.append(name)
            names = sorted(set(names))[:300]
            self.root.after(0, lambda: self.contact_combo.config(values=names))

        threading.Thread(target=_load, daemon=True).start()

    # ── 微信状态变化（Owner + 定位更新）────────────────────
    def _on_wechat_state_changed(self, state: WeChatState | None) -> None:
        if state is None:
            # 微信最小化/关闭 → 清 owner 后隐藏（避免被一起销毁）
            if self._owner_hwnd and self._self_hwnd:
                embed.clear_owner(self._self_hwnd)
                self._owner_hwnd = None
            try:
                self.root.withdraw()
            except Exception:  # noqa: BLE001
                pass
            return

        # 首次看到微信 / 微信 hwnd 变了 → 设置/重设 Owner
        if self._self_hwnd and state.hwnd != self._owner_hwnd:
            if embed.set_owner(self._self_hwnd, state.hwnd):
                self._owner_hwnd = state.hwnd
                print(f"[panel] Owner 已设为微信 HWND={state.hwnd}", flush=True)

        if not self._tracking:
            return
        # 计算目标位置：紧贴微信右侧外部（放不下就叠在内部右边缘）
        left, top, right, bottom = state.rect
        h = max(bottom - top, 400)
        panel_x = right
        try:
            import ctypes
            gm = ctypes.windll.user32.GetSystemMetrics
            virt_right = gm(76) + gm(78)
            if panel_x + PANEL_WIDTH > virt_right:
                panel_x = right - PANEL_WIDTH
        except Exception:  # noqa: BLE001
            pass
        print(f"[panel] wechat_rect={state.rect} -> x={panel_x} y={top} {PANEL_WIDTH}x{h}",
              flush=True)
        try:
            self.root.deiconify()
            # 双重保险：Tk geometry 先设尺寸+位置，再用 Win32 SetWindowPos 强制坐标
            self.root.geometry(f"{PANEL_WIDTH}x{h}+{panel_x}+{top}")
            self.root.update_idletasks()
            if self._self_hwnd:
                ok = embed.move_window(self._self_hwnd, panel_x, top, PANEL_WIDTH, h)
                print(f"[panel] move_window ok={ok}", flush=True)
            self.root.lift()
        except Exception as e:  # noqa: BLE001
            print(f"[panel] move 异常: {e}", flush=True)

    # ── 拖动（拖动后停止自动吸附）──────────────────────────
    def _drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event: tk.Event) -> None:
        self._tracking = False
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        try:
            self.root.geometry(f"+{x}+{y}")
        except Exception:  # noqa: BLE001
            pass

    # ── 状态栏 ──────────────────────────────────────────────
    def _set_status(self, text: str, color: str = "#e8ffe8") -> None:
        self.status.config(text=text, fg=color)
        try:
            self.root.update_idletasks()
        except Exception:  # noqa: BLE001
            pass

    # ── 风格 ────────────────────────────────────────────────
    def _on_style_change(self) -> None:
        self._set_status(f"已选风格：{self.style_var.get()}")
        if (
            self.auto_regen.get()
            and self.reply_text.get("1.0", "end").strip()
            and self.chat_text.get("1.0", "end").strip()
        ):
            self.on_generate()

    # ── 截图 OCR ────────────────────────────────────────────
    def on_capture(self) -> None:
        self.root.withdraw()
        self.root.after(200, self._do_capture)

    def _do_capture(self) -> None:
        try:
            path, reason = capture_region(parent=self.root)
            if path is None:
                self.thumb_label.config(
                    image="", text=f"⚠ 截图未成功\n{reason}",
                    fg=ERR_FG, width=24, height=4,
                )
                self._thumb_image = None
                self._set_status(reason, "#ffcccc")
                return
            old = self._last_capture_path
            self._last_capture_path = path
            if old and os.path.exists(old):
                try:
                    os.unlink(old)
                except OSError:
                    pass

            self._show_thumbnail(path)
            self.preview_open_btn.config(state=tk.NORMAL)
            self.recapture_btn.config(state=tk.NORMAL)

            backend = _ocr_backend()
            self._set_status(f"识别中（{backend}）…")
            text = ocr_image(path)
            self.chat_text.delete("1.0", tk.END)
            self.chat_text.insert("1.0", text)
            self._set_status(f"识别完成（{len(text)} 字）")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("OCR 失败", str(e))
            self._set_status("OCR 失败", "#ffcccc")
        finally:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            state = get_wechat_state()
            if state and self._tracking:
                self._on_wechat_state_changed(state)

    def _show_thumbnail(self, path: str) -> None:
        img = Image.open(path)
        img.thumbnail((THUMB_MAX_W, THUMB_MAX_H), Image.Resampling.LANCZOS)
        self._thumb_image = ImageTk.PhotoImage(img)
        self.thumb_label.config(image=self._thumb_image, text="", width=0, height=0)

    def on_open_preview(self) -> None:
        if self._last_capture_path and os.path.exists(self._last_capture_path):
            os.startfile(self._last_capture_path)
        else:
            self._set_status("还没有截图可查看", "#ffcccc")

    # ── 读取微信（UIA）─────────────────────────────────────
    def on_read_ax(self) -> None:
        if not ax.available():
            messagebox.showerror(
                "UIA 不可用",
                f"未能导入 uiautomation。\n详细错误：{ax.import_error()}",
            )
            return
        self._set_status("正在从微信读取…")
        threading.Thread(target=self._read_ax_worker, daemon=True).start()

    def _read_ax_worker(self) -> None:
        try:
            result = ax.read_wechat_multi_pass(passes=2)
        except Exception as e:  # noqa: BLE001
            err = str(e)
            self.root.after(0, lambda: self._show_error(f"UIA 读取异常：{err}"))
            return
        self.root.after(0, lambda: self._on_ax_done(result))

    def _on_ax_done(self, result: tuple[str, int] | None) -> None:
        if result is None:
            messagebox.showwarning(
                "读取失败",
                "未读到聊天内容。常见原因：\n"
                "• 微信未启动 / 未登录\n"
                "• 不在聊天界面\n"
                "• 微信窗口被最小化",
            )
            self._set_status("读取失败", "#ffcccc")
            return
        text, n = result
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.insert("1.0", text)
        self.chat_text.see(tk.END)
        self._set_status(f"读取完成（{n} 条）")

    # ── 生成 ────────────────────────────────────────────────
    def _resolve_length_and_count(self, chat: str) -> tuple[int, int]:
        """从 UI 设置里算出本次生成的 (target_length, reply_count)。

        target_length=0 表示不强制长度；>0 表示让 LLM 尽量贴这个字数。
        reply_count 至少为 1。
        """
        target = 0
        if self.align_length_var.get():
            target = opponent_length_since_my_last(chat)
        try:
            count = int(self.reply_count_var.get() or 1)
        except (tk.TclError, TypeError, ValueError):
            count = 1
        count = max(1, min(10, count))
        return target, count

    def on_generate(self) -> None:
        chat = self.chat_text.get("1.0", tk.END).strip()
        if not chat:
            messagebox.showwarning("提示", "聊天内容为空，先读取微信或截图 OCR。")
            return
        self.result_nb.select(0)
        self.gen_btn.config(state=tk.DISABLED)
        target_len, count = self._resolve_length_and_count(chat)
        hint = f"{count} 句" + (f" · 约{target_len}字" if target_len else "")
        self._set_status(f"生成中【{self.style_var.get()}】· {hint}…")
        self.reply_text.delete("1.0", tk.END)
        threading.Thread(
            target=self._generate_worker,
            args=(chat, self.style_var.get(), self.extra_var.get(), target_len, count),
            daemon=True,
        ).start()

    def _generate_worker(
        self, chat: str, style: str, extra: str,
        target_length: int, reply_count: int,
    ) -> None:
        try:
            prompt = build_prompt(
                chat, style, extra,
                mimic_user=self.mimic_var.get(),
                target_length=target_length,
                reply_count=reply_count,
            )
            self._last_prompt = prompt
            self.root.after(0, lambda: self.reply_text.delete("1.0", tk.END))

            def on_chunk(delta: str) -> None:
                self.root.after(0, self._append_reply_chunk, delta)

            full = generate_reply_stream(prompt, on_chunk)
            self.root.after(0, lambda: self._finalize_reply(full, style))
        except Exception as e:  # noqa: BLE001
            err = str(e)
            self.root.after(0, lambda: self._show_error(err))

    def _append_reply_chunk(self, delta: str) -> None:
        self.reply_text.insert(tk.END, delta)
        self.reply_text.see(tk.END)

    def _finalize_reply(self, reply: str, style: str) -> None:
        self.gen_btn.config(state=tk.NORMAL)
        msgs = split_replies(reply)
        pyperclip.copy("\n".join(msgs))
        suffix = f" · {len(msgs)} 条已复制" if len(msgs) > 1 else " · 已复制"
        self._set_status(f"已生成【{style}】{suffix}")
        if self.auto_send_var.get():
            self.root.after(250, lambda: self.on_send(True))

    def on_generate_multi(self) -> None:
        chat = self.chat_text.get("1.0", tk.END).strip()
        if not chat:
            messagebox.showwarning("提示", "聊天内容为空。")
            return
        self.result_nb.select(1)
        extra = self.extra_var.get()
        self.multi_btn.config(state=tk.DISABLED)
        self.gen_btn.config(state=tk.DISABLED)
        for w in self.candidate_widgets:
            w["label"].config(text="… 生成中")
            w["text"].delete("1.0", tk.END)
            w["btn"].config(state=tk.DISABLED, command=lambda: None)
        self._set_status(f"并发生成：{' / '.join(MULTI_STYLES)}…")
        self._multi_remaining = len(MULTI_STYLES)
        target_len, count = self._resolve_length_and_count(chat)
        for i, style in enumerate(MULTI_STYLES):
            threading.Thread(
                target=self._multi_worker,
                args=(i, style, chat, extra, target_len, count),
                daemon=True,
            ).start()

    def _multi_worker(
        self, i: int, style: str, chat: str, extra: str,
        target_length: int, reply_count: int,
    ) -> None:
        try:
            prompt = build_prompt(
                chat, style, extra,
                mimic_user=self.mimic_var.get(),
                target_length=target_length,
                reply_count=reply_count,
            )
            if i == 0:
                self._last_prompt = prompt
            reply = generate_reply(prompt)
            self.root.after(0, lambda: self._show_candidate(i, style, reply))
        except Exception as e:  # noqa: BLE001
            err = str(e)
            self.root.after(0, lambda: self._show_candidate(i, style, f"[失败] {err}"))

    def _show_candidate(self, i: int, style: str, reply: str) -> None:
        w = self.candidate_widgets[i]
        w["label"].config(text=f"【{style}】")
        w["text"].delete("1.0", tk.END)
        w["text"].insert("1.0", reply)
        if not reply.startswith("[失败]"):
            w["btn"].config(
                state=tk.NORMAL,
                command=lambda r=reply, s=style: self._pick_candidate(r, s),
            )
        else:
            w["btn"].config(state=tk.DISABLED)
        self._multi_remaining -= 1
        if self._multi_remaining <= 0:
            self.multi_btn.config(state=tk.NORMAL)
            self.gen_btn.config(state=tk.NORMAL)
            self._set_status("3 种风格已生成")

    def _pick_candidate(self, reply: str, style: str) -> None:
        self.reply_text.delete("1.0", tk.END)
        self.reply_text.insert("1.0", reply)
        msgs = split_replies(reply)
        pyperclip.copy("\n".join(msgs))
        self.result_nb.select(0)
        suffix = f" · {len(msgs)} 条已复制" if len(msgs) > 1 else " · 已复制"
        self._set_status(f"已选【{style}】{suffix}")
        if self.auto_send_var.get():
            self.root.after(150, lambda: self.on_send(True))

    # ── 错误 / 复制 / 发送 ──────────────────────────────────
    def _show_error(self, msg: str) -> None:
        messagebox.showerror("生成失败", msg)
        self._set_status("生成失败", "#ffcccc")
        self.gen_btn.config(state=tk.NORMAL)

    def on_copy(self) -> None:
        text = self.reply_text.get("1.0", tk.END).strip()
        if not text:
            return
        # 复制到剪贴板时去掉 <MSG> 分隔符，改成换行
        msgs = split_replies(text)
        pyperclip.copy("\n".join(msgs))
        self._set_status(f"已复制 {len(msgs)} 条" if len(msgs) > 1 else "已复制")

    def on_send(self, press_enter: bool) -> None:
        reply = self.reply_text.get("1.0", tk.END).strip()
        if not reply:
            messagebox.showwarning("提示", "当前没有待发送的回复。")
            return
        contact = self.contact_var.get().strip() or None
        msgs = split_replies(reply)
        # 只粘贴多条没意义（无法分条粘到同一输入框）→ 合并为单块粘进去
        if len(msgs) > 1 and press_enter:
            ok, msg = send_messages_to_wechat(
                msgs, press_enter=True, contact_name=contact,
            )
        else:
            ok, msg = send_to_wechat(
                "\n".join(msgs), press_enter=press_enter, contact_name=contact,
            )
        self._set_status(msg, "#e8ffe8" if ok else "#ffcccc")
        if not ok:
            messagebox.showerror("发送失败", msg)

    # ── 查看 Prompt ─────────────────────────────────────────
    def on_view_prompt(self) -> None:
        from .llm import CLAUDE_BIN

        win = tk.Toplevel(self.root)
        win.title("最近一次 Prompt · Claude Code CLI")
        win.geometry("640x600")
        win.configure(bg=BG)
        info = ttk.Label(
            win,
            text=(
                f"CLI: {CLAUDE_BIN}\n"
                "调用方式: claude -p --output-format stream-json\n"
                f"WCF 状态: {self._wcf_status}"
            ),
            style="Muted.TLabel",
            justify=tk.LEFT,
        )
        info.pack(fill=tk.X, padx=12, pady=(10, 6))
        ttk.Label(win, text="Prompt 正文：", style="Heading.TLabel").pack(
            anchor="w", padx=12, pady=(2, 4))
        scr = ttk.Scrollbar(win, orient=tk.VERTICAL)
        txt = tk.Text(
            win, wrap=tk.WORD, font=FONT_SM,
            bg=CARD_BG, fg=TEXT_FG, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            padx=8, pady=6, yscrollcommand=scr.set,
        )
        scr.config(command=txt.yview)
        scr.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 12), pady=(0, 12))
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        content = self._last_prompt or "（还没调用过生成）"
        txt.insert("1.0", content)
        txt.config(state=tk.DISABLED)

    # ── 生命周期 ────────────────────────────────────────────
    def show(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:  # noqa: BLE001
            pass

    def hide(self) -> None:
        try:
            self.root.withdraw()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        self.root.mainloop()

    def destroy(self) -> None:
        try:
            self._tracker.stop()
        except Exception:  # noqa: BLE001
            pass
        # 清 Owner（必须！否则微信关闭时我们可能被一起销毁）
        if self._owner_hwnd and self._self_hwnd:
            try:
                embed.clear_owner(self._self_hwnd)
            except Exception:  # noqa: BLE001
                pass
            self._owner_hwnd = None
        # 停 WCF
        try:
            wcf.WCFClient.instance().stop()
        except Exception:  # noqa: BLE001
            pass
        if self._last_capture_path and os.path.exists(self._last_capture_path):
            try:
                os.unlink(self._last_capture_path)
            except OSError:
                pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
