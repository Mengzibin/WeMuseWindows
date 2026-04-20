"""微信「附属窗口」：用 Win32 Owner 关系（GWLP_HWNDPARENT），不是 SetParent。

**为什么不用 SetParent**：
  SetParent 会把我们插进微信的子窗口列表。微信自己的代码遇到外部子窗口
  （跨进程、非预期控件类）时，内部的布局/重绘/消息路由会出问题，最终闪退。

**Owner 关系做了什么**：
  • 我们的窗口**仍是顶层窗口**（WS_POPUP），不是微信的 child
  • 但会「跟随」微信：微信最小化 → 我们隐藏；微信还原 → 我们跟着出来
  • Z-order 上永远在微信之上（不会被微信盖住）
  • 微信关闭 → 我们不会被一起销毁（需要自己 detach）
  • 微信的 `EnumChildWindows` 遍历**看不到我们**——彻底避免崩溃路径

Win32 API：
  GWLP_HWNDPARENT (-8)：对 top-level 窗口来说，设置 owner
  `SetWindowLongPtrW(child, GWLP_HWNDPARENT, owner_hwnd)` 设置 owner
  `SetWindowLongPtrW(child, GWLP_HWNDPARENT, 0)` 清除 owner
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

try:
    import win32gui
    _WIN32 = True
except Exception:  # noqa: BLE001
    _WIN32 = False

GWLP_HWNDPARENT = -8

user32 = ctypes.windll.user32 if _WIN32 else None


def available() -> bool:
    return _WIN32


def _set_window_long_ptr(hwnd: int, index: int, value: int) -> int:
    """跨 32/64 位兼容的 SetWindowLongPtr。"""
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        fn = user32.SetWindowLongPtrW
        fn.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        fn.restype = ctypes.c_longlong
    else:
        fn = user32.SetWindowLongW
        fn.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        fn.restype = ctypes.c_long
    return fn(hwnd, index, value)


def set_owner(child_hwnd: int, owner_hwnd: int) -> bool:
    """把 child 的 owner 设为 owner_hwnd。child 仍是 top-level。"""
    if not _WIN32 or not child_hwnd or not owner_hwnd:
        return False
    try:
        _set_window_long_ptr(child_hwnd, GWLP_HWNDPARENT, owner_hwnd)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[embed] set_owner 失败: {e}", flush=True)
        return False


def clear_owner(child_hwnd: int) -> bool:
    """清除 owner 关系（微信关闭前必须调用，否则我们可能被一起销毁）。"""
    if not _WIN32 or not child_hwnd:
        return False
    try:
        _set_window_long_ptr(child_hwnd, GWLP_HWNDPARENT, 0)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[embed] clear_owner 失败: {e}", flush=True)
        return False


def get_toplevel_hwnd(hwnd: int) -> int:
    """Tk 的 winfo_id() 在 Windows 上返回内部子 HWND，这里向上找到真正顶层窗口。"""
    if not _WIN32 or not hwnd:
        return hwnd
    try:
        fn = user32.GetAncestor
        fn.argtypes = [wintypes.HWND, ctypes.c_uint]
        fn.restype = wintypes.HWND
        GA_ROOT = 2
        top = fn(hwnd, GA_ROOT)
        return int(top) if top else hwnd
    except Exception:  # noqa: BLE001
        return hwnd


def is_window_alive(hwnd: int) -> bool:
    if not _WIN32 or not hwnd:
        return False
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


# SetWindowPos flags
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def move_window(hwnd: int, x: int, y: int, w: int, h: int) -> bool:
    """用 Win32 SetWindowPos 直接定位；绕过 Tk geometry 在 overrideredirect 下的坑。"""
    if not _WIN32 or not hwnd:
        return False
    try:
        fn = user32.SetWindowPos
        fn.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        fn.restype = wintypes.BOOL
        return bool(fn(hwnd, 0, x, y, w, h,
                       SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW))
    except Exception as e:  # noqa: BLE001
        print(f"[embed] move_window 失败: {e}", flush=True)
        return False
