"""微信聊天助手 WeMuse — 面板模式入口。

启动后面板自动吸附在微信窗口右侧，
随微信移动/改变大小同步更新位置。
"""
from __future__ import annotations

import sys

if sys.platform != "win32":
    raise RuntimeError("面板模式仅支持 Windows。")

# 中文 Windows 下 stdout 默认是 GBK，聊天里出现 💤 这类 BMP 外的字符时，
# print 会抛 UnicodeEncodeError。既然 print 只是日志，把编码强制改成 UTF-8
# 并把无法编码的字符替换掉，避免任何 print 调用把主流程带崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from . import hotkey
from . import tray
from .panel_ui import PanelUI


def main() -> None:
    panel = PanelUI()

    # 关窗口 = 隐藏到托盘
    panel.root.protocol("WM_DELETE_WINDOW", panel.hide)

    # 全局热键
    if hotkey.available():
        hotkey.register("w", lambda: panel.root.after(0, _toggle(panel)), shift=True, ctrl=True)
        hotkey.register("g", lambda: panel.root.after(0, panel.on_generate), shift=True, ctrl=True)
        print("热键：Ctrl+Shift+W 显示/隐藏面板  Ctrl+Shift+G 立即生成建议")
    else:
        print(f"⚠ 热键不可用：{hotkey.import_error()}")

    # 系统托盘
    tray_icon = None
    if tray.available():
        tray_icon = tray.setup(
            "微信聊天助手 WeMuse",
            [
                ("📋 显示面板 (Ctrl+Shift+W)", lambda: panel.root.after(0, panel.show)),
                ("✨ 立即生成建议 (Ctrl+Shift+G)", lambda: panel.root.after(0, panel.on_generate)),
                (None, None),
                ("👋 退出", lambda: _quit(panel, tray_icon)),
            ],
        )

    print("微信聊天助手 WeMuse 已启动 — 面板已吸附到微信右侧")

    try:
        panel.run()
    finally:
        hotkey.unregister_all()
        tray.stop(tray_icon)


def _toggle(panel: PanelUI) -> None:
    if panel.root.state() == "withdrawn":
        panel.show()
    else:
        panel.hide()


def _quit(panel: PanelUI, tray_icon) -> None:
    hotkey.unregister_all()
    tray.stop(tray_icon)
    panel.destroy()


if __name__ == "__main__":
    main()
