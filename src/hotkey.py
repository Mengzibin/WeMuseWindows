"""Windows 全局热键：使用 keyboard 库实现，对应 macOS 版的 hotkey.py。

热键映射（macOS → Windows）：
  ⌘⇧R  →  Ctrl+Shift+R  显示/隐藏窗口
  ⌘⇧G  →  Ctrl+Shift+G  截图 OCR
  ⌘⇧A  →  Ctrl+Shift+A  Accessibility 读取 + 生成

keyboard 库在 Windows 上无需管理员权限即可注册全局热键。
"""
from __future__ import annotations

from typing import Callable

try:
    import keyboard as _kb
    _AVAIL = True
    _ERR = ""
except Exception as e:  # noqa: BLE001
    _AVAIL = False
    _ERR = str(e)
    _kb = None  # type: ignore[assignment]

# 已注册的热键列表，保存下来以便 unregister_all 清理
_registered: list[str] = []


def available() -> bool:
    return _AVAIL


def import_error() -> str:
    return _ERR


def register(
    key_char: str,
    callback: Callable[[], None],
    ctrl: bool = True,
    shift: bool = True,
    alt: bool = False,
    win: bool = False,
) -> str | None:
    """注册一个全局热键。

    key_char: 单个字母，如 'r' / 'g' / 'a'
    ctrl/shift/alt/win: 修饰键开关

    返回热键字符串（如 'ctrl+shift+r'），可用于后续取消注册。
    不可用时返回 None。
    """
    if not _AVAIL:
        return None

    parts: list[str] = []
    if ctrl:
        parts.append("ctrl")
    if shift:
        parts.append("shift")
    if alt:
        parts.append("alt")
    if win:
        parts.append("windows")
    parts.append(key_char.lower())
    combo = "+".join(parts)

    try:
        _kb.add_hotkey(combo, callback, suppress=False)
        _registered.append(combo)
        return combo
    except Exception as e:  # noqa: BLE001
        print(f"[hotkey] 注册 {combo!r} 失败：{e}", flush=True)
        return None


def unregister_all() -> None:
    """取消注册所有已注册的热键（程序退出前调用）。"""
    if not _AVAIL:
        return
    for combo in _registered:
        try:
            _kb.remove_hotkey(combo)
        except Exception:  # noqa: BLE001
            pass
    _registered.clear()
