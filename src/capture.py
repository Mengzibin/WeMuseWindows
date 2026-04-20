"""Windows 截图：全屏截图 + Tkinter 半透明覆盖层供用户框选区域。

对应 macOS 版的 capture.py（macOS 用 screencapture -i）。
Windows 没有内置的交互式截图命令，这里用 mss + tkinter 自己实现。

使用流程：
  1. 先用 mss 截取全屏
  2. 弹出全屏 Tkinter 窗口展示截图（半透明遮罩）
  3. 用户拖拽框选区域
  4. 返回临时 PNG 路径，供 OCR 模块处理
"""
from __future__ import annotations

import os
import tempfile
import tkinter as tk
from typing import Optional

try:
    import mss
    import mss.tools
    _MSS = True
except Exception:  # noqa: BLE001
    _MSS = False

try:
    from PIL import Image, ImageTk, ImageEnhance
    _PIL = True
except Exception:  # noqa: BLE001
    _PIL = False


class _RegionSelector:
    """全屏半透明覆盖层，让用户拖拽框选截图区域。

    处理 DPI 缩放：
      - screenshot 是物理像素（mss）
      - Tkinter 在 DPI 不感知进程里用逻辑像素
      - scale = 物理/逻辑；显示时把图缩到逻辑大小，选完再把坐标乘回去
    """

    def __init__(
        self,
        screenshot: "Image.Image",
        parent: tk.Misc | None = None,
        screen_left: int = 0,
        screen_top: int = 0,
        scale: float = 1.0,
    ) -> None:
        self._screenshot = screenshot
        self._scale = scale
        self._region: tuple[int, int, int, int] | None = None
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None

        pw, ph = screenshot.size
        # 逻辑尺寸 = 物理尺寸 / scale，用于 Tkinter 窗口与画布
        lw = max(1, int(round(pw / scale)))
        lh = max(1, int(round(ph / scale)))
        lx = int(round(screen_left / scale))
        ly = int(round(screen_top / scale))

        self.root = tk.Toplevel(parent)
        self.root.overrideredirect(True)           # 无边框
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{lw}x{lh}+{lx}+{ly}")
        self.root.configure(cursor="crosshair")

        # 背景：截图先缩到逻辑尺寸再变暗，避免超出 Tk 可见区
        if (lw, lh) != (pw, ph):
            display_img = screenshot.resize((lw, lh), Image.Resampling.LANCZOS)
        else:
            display_img = screenshot
        darkened = ImageEnhance.Brightness(display_img).enhance(0.55)
        self._bg_photo = ImageTk.PhotoImage(darkened)

        self.canvas = tk.Canvas(
            self.root, width=lw, height=lh,
            highlightthickness=0, cursor="crosshair",
        )
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self._bg_photo, anchor="nw")

        # 提示文字
        self.canvas.create_text(
            lw // 2, 40,
            text="拖拽框选截图区域  ·  ESC 取消",
            fill="white", font=("Microsoft YaHei", 16),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda _: self._cancel())

    def _on_press(self, event: tk.Event) -> None:
        self._start = (event.x, event.y)
        if self._rect_id:
            self.canvas.delete(self._rect_id)
            self._rect_id = None

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start:
            return
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        x0, y0 = self._start
        self._rect_id = self.canvas.create_rectangle(
            x0, y0, event.x, event.y,
            outline="#00ff88", width=2, dash=(4, 2),
        )

    def _on_release(self, event: tk.Event) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = event.x, event.y
        # 归一化（用户可能从右下往左上拖）
        left   = min(x0, x1)
        top    = min(y0, y1)
        right  = max(x0, x1)
        bottom = max(y0, y1)
        if right - left > 5 and bottom - top > 5:
            s = self._scale
            # 逻辑 → 物理，再 clamp 到 PIL 图像范围
            pw, ph = self._screenshot.size
            px0 = max(0, min(pw, int(round(left * s))))
            py0 = max(0, min(ph, int(round(top * s))))
            px1 = max(0, min(pw, int(round(right * s))))
            py1 = max(0, min(ph, int(round(bottom * s))))
            if px1 > px0 and py1 > py0:
                self._region = (px0, py0, px1, py1)
        self.root.destroy()

    def _cancel(self) -> None:
        self._region = None
        self.root.destroy()

    def select(self) -> tuple[int, int, int, int] | None:
        """阻塞直到用户完成框选或取消，返回 (left, top, right, bottom) 或 None。"""
        self.root.wait_window()
        return self._region


def capture_region(parent: tk.Misc | None = None) -> tuple[str | None, str]:
    """交互式截图，让用户框选区域，返回 (临时文件路径, 原因)。

    **必须从已有 Tk 主循环调用**（传入 parent=panel.root），不要再建新的 tk.Tk()——
    Tkinter 不允许同时存在两个 root，会死锁。
    失败或取消时 path=None，原因说明取消/失败原因。

    捕获范围：整个虚拟桌面（多屏支持）。
    自动处理 DPI 缩放：mss 给物理像素，Tk 用逻辑像素，二者比值即 scale。
    """
    if not _MSS:
        return None, "mss 未安装（pip install mss）"
    if not _PIL:
        return None, "Pillow 未安装（pip install Pillow）"

    # 1. 截全虚拟桌面（monitors[0] = 包含所有显示器的虚拟矩形）
    try:
        with mss.mss() as sct:
            vdesk = sct.monitors[0]
            primary = sct.monitors[1] if len(sct.monitors) > 1 else vdesk
            screen_left = int(vdesk["left"])
            screen_top = int(vdesk["top"])
            raw = sct.grab(vdesk)
            screenshot = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            primary_phys_w = int(primary["width"])
    except Exception as e:  # noqa: BLE001
        return None, f"截屏失败：{e}"

    pw, ph = screenshot.size
    print(f"[capture] mss 虚拟桌面: {pw}x{ph} 起点=({screen_left}, {screen_top})",
          flush=True)

    # 2. 计算 DPI scale：主屏物理宽 / Tk 报告的主屏逻辑宽
    scale = 1.0
    try:
        if parent is not None:
            log_w = int(parent.winfo_screenwidth())
            if log_w > 0 and primary_phys_w > 0:
                scale = primary_phys_w / log_w
    except Exception:  # noqa: BLE001
        pass
    if scale <= 0 or not (0.5 < scale < 4.0):
        scale = 1.0
    print(f"[capture] DPI scale = {scale:.3f} (物理主屏 {primary_phys_w} / 逻辑主屏)",
          flush=True)

    # 3. 弹覆盖层让用户框选（Toplevel 挂在主 root 上）
    try:
        selector = _RegionSelector(
            screenshot, parent=parent,
            screen_left=screen_left, screen_top=screen_top, scale=scale,
        )
        region = selector.select()
    except Exception as e:  # noqa: BLE001
        return None, f"选区窗口异常：{e}"

    if region is None:
        return None, "用户取消截图"

    left, top, right, bottom = region
    cw, ch = right - left, bottom - top
    print(f"[capture] 物理裁剪区域 L/T/R/B=({left},{top},{right},{bottom}) "
          f"尺寸={cw}x{ch}", flush=True)

    if cw < 5 or ch < 5:
        return None, f"选区过小（{cw}x{ch}），请重新拖选"

    # 4. 裁剪并保存到临时文件
    cropped = screenshot.crop((left, top, right, bottom))
    fd, path = tempfile.mkstemp(suffix=".png", prefix="wemuse_cap_")
    os.close(fd)
    try:
        cropped.save(path)
    except Exception as e:  # noqa: BLE001
        return None, f"保存截图失败：{e}"

    print(f"[capture] 已保存: {path} ({cw}x{ch})", flush=True)
    return path, "截图成功"
