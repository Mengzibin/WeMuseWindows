"""Windows 系统托盘图标：用 pystray 实现，对应 macOS 版的 menubar.py。

托盘图标使用 Pillow 绘制一个简单的「💬」图案，
点击右键弹出菜单（与 macOS 菜单栏菜单等价）。

pystray 在后台线程运行其事件循环，主线程不阻塞。
"""
from __future__ import annotations

from typing import Callable

try:
    import pystray
    from pystray import MenuItem as TrayItem, Menu as TrayMenu
    _AVAIL = True
    _ERR = ""
except Exception as e:  # noqa: BLE001
    _AVAIL = False
    _ERR = str(e)
    pystray = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL = True
except Exception:  # noqa: BLE001
    _PIL = False


def available() -> bool:
    return _AVAIL and _PIL


def import_error() -> str:
    if not _AVAIL:
        return _ERR
    if not _PIL:
        return "Pillow 不可用"
    return ""


def _make_icon(size: int = 64) -> "Image.Image":
    """绘制一个简单的托盘图标（深绿色圆形 + 白色气泡图案）。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 深绿色圆形背景
    draw.ellipse([2, 2, size - 2, size - 2], fill=(7, 193, 96, 255))
    # 两个小白色圆（模拟气泡省略号）
    r = size // 10
    cy = size // 2
    for cx in [size // 3, size // 2, size * 2 // 3]:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    return img


def setup(
    title: str,
    menu_items: list[tuple[str | None, Callable | None]],
) -> "pystray.Icon | None":
    """创建并启动系统托盘图标（在后台线程运行）。

    menu_items: [(标签, 回调), ...]，标签为 None 时插入分隔线。
    返回 pystray.Icon 对象；不可用时返回 None。

    注意：调用方必须保存返回值，否则图标会被 GC 清除。
    """
    if not available():
        return None

    items: list = []
    for label, cb in menu_items:
        if label is None:
            items.append(pystray.Menu.SEPARATOR)
        else:
            items.append(TrayItem(label, cb))

    icon = pystray.Icon(
        name="WeMuse",
        icon=_make_icon(),
        title=title,
        menu=TrayMenu(*items),
    )

    # 非阻塞：在守护线程运行 pystray 事件循环
    import threading
    t = threading.Thread(target=icon.run, daemon=True)
    t.start()
    return icon


def stop(icon: "pystray.Icon | None") -> None:
    """停止并移除托盘图标。"""
    if icon is not None:
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
