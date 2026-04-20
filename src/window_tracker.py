"""追踪微信主窗口：位置、大小、客户区尺寸。

支持两种模式：
  • 嵌入模式（SetParent）：面板作为微信子窗口，随父窗口自动移动；追踪器只
    负责感知「微信出现/消失」和「客户区尺寸变化」来重新定位面板
  • 叠加模式（默认退化）：面板是独立顶层窗口，追踪器每 0.4s 轮询微信 rect，
    把面板 geometry 同步到「吸附在微信右侧」
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

try:
    import win32gui
    _WIN32 = True
except Exception:  # noqa: BLE001
    _WIN32 = False

WECHAT_CLASS = "WeChatMainWndForPC"
WECHAT_NAMES = {"微信", "WeChat"}

# 嵌入模式下面板宽度（微信客户区右侧占用多少像素）
PANEL_WIDTH = 380


@dataclass
class WeChatState:
    hwnd: int
    rect: tuple[int, int, int, int]    # 屏幕坐标 (left, top, right, bottom)
    client_w: int                      # 客户区宽（不含边框）
    client_h: int                      # 客户区高


def get_wechat_hwnd() -> int | None:
    """定位微信主窗口 HWND（可见且未最小化）。"""
    if not _WIN32:
        return None
    try:
        hwnd = win32gui.FindWindow(WECHAT_CLASS, None)
        if hwnd and win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            return hwnd
    except Exception:  # noqa: BLE001
        pass
    # 兜底：枚举找类名或标题匹配
    found: list[int] = []

    def _cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                return
            cls = win32gui.GetClassName(h)
            name = win32gui.GetWindowText(h)
            if cls == WECHAT_CLASS or (cls.startswith("Qt") and name in WECHAT_NAMES):
                found.append(h)
        except Exception:  # noqa: BLE001
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return found[0] if found else None


def get_wechat_state() -> WeChatState | None:
    """返回微信当前窗口状态；未找到/最小化时返回 None。"""
    hwnd = get_wechat_hwnd()
    if hwnd is None:
        return None
    try:
        rect = win32gui.GetWindowRect(hwnd)
        client = win32gui.GetClientRect(hwnd)  # (0, 0, w, h)
        return WeChatState(hwnd=hwnd, rect=rect, client_w=client[2], client_h=client[3])
    except Exception:  # noqa: BLE001
        return None


def get_wechat_rect() -> tuple[int, int, int, int] | None:
    """兼容旧调用：只返回 rect。"""
    s = get_wechat_state()
    return s.rect if s else None


def panel_geometry(wechat_rect: tuple[int, int, int, int]) -> str:
    """计算面板 geometry：**永远在微信窗口右侧**（屏幕坐标）。

    优先紧贴微信右边缘外部；屏幕右边放不下就叠在微信内部右边缘（覆盖聊天区
    右侧一条）。**绝不放到左边** —— 这是产品设计的硬性要求。
    """
    left, top, right, bottom = wechat_rect
    h = max(bottom - top, 400)

    # 默认：微信外部右侧
    panel_x = right

    if _WIN32:
        try:
            import ctypes
            # 多屏：用虚拟屏幕右边界，而不是主屏宽度
            # SM_XVIRTUALSCREEN=76, SM_CXVIRTUALSCREEN=78
            gm = ctypes.windll.user32.GetSystemMetrics
            virt_x = gm(76)
            virt_w = gm(78)
            virt_right = virt_x + virt_w if virt_w > 0 else gm(0)
            # 外部放不下 → 叠在微信内部右边缘（仍然在「右侧」）
            if panel_x + PANEL_WIDTH > virt_right:
                panel_x = right - PANEL_WIDTH
        except Exception:  # noqa: BLE001
            pass

    return f"{PANEL_WIDTH}x{h}+{panel_x}+{top}"


class WeChatTracker:
    """后台线程：感知微信「出现/消失/客户区尺寸变化」，触发回调。

    on_state_changed(state | None):
      • state != None — 微信在。嵌入模式下应 embed+position，叠加模式下应 reposition
      • state == None — 微信没了。应 detach+hide
    """

    def __init__(
        self,
        on_state_changed: Callable[[WeChatState | None], None],
        interval: float = 0.4,
    ) -> None:
        self._cb = on_state_changed
        self._interval = interval
        self._last: tuple | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            state = get_wechat_state()
            # 仅在「存在与否」或「客户区尺寸 / hwnd / rect」变化时触发回调
            key = (
                None if state is None
                else (state.hwnd, state.client_w, state.client_h, state.rect)
            )
            if key != self._last:
                self._last = key
                try:
                    self._cb(state)
                except Exception as e:  # noqa: BLE001
                    print(f"[tracker] callback 异常: {e}", flush=True)
            time.sleep(self._interval)
