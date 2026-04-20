"""把回复发到微信（Windows 版）。

两档通道，优先级从高到低：
  1. WeChatFerry（DLL 注入）→ 调 spy.dll 的 send_text，原生发送，不抢焦点
  2. win32clipboard + keybd_event → 激活微信窗口 → Ctrl+V → 可选回车（兜底）

当调用方提供 contact_name 且 WCF 运行中时走 1；否则走 2。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import pyperclip

from . import wcferry_client as wcf

try:
    import win32gui
    import win32con
    _WIN32 = True
except Exception:  # noqa: BLE001
    _WIN32 = False

try:
    import win32clipboard  # type: ignore
    _WIN32CLIP = True
except Exception:  # noqa: BLE001
    _WIN32CLIP = False

WECHAT_CLASS = "WeChatMainWndForPC"
WECHAT_NAMES = {"微信", "WeChat"}


# ---------- 剪贴板 ----------


def _win32_copy(text: str) -> bool:
    """用 win32clipboard 直接写剪贴板。

    为什么不走 pyperclip：pyperclip 在 win32clipboard 缺失时会悄悄退化到
    Tk 的 clipboard_append，Tk 剪贴板是"进程私有"的——外部 Ctrl+V
    拿到的是 Tk 最后一次写入的值，连发 N 条变成全贴最后一条。
    """
    if not _WIN32CLIP:
        return False
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[sender] win32 剪贴板写入失败: {e}", flush=True)
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass
        return False


def _win32_paste() -> str | None:
    if not _WIN32CLIP:
        return None
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return data if isinstance(data, str) else None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:  # noqa: BLE001
        try:
            win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass
    return None


def _robust_copy(text: str, timeout: float = 1.2) -> bool:
    """写剪贴板并读回验证，最多等 timeout 秒。"""
    deadline = time.time() + timeout
    last_read = None
    used_api = "win32" if _WIN32CLIP else "pyperclip"
    while time.time() < deadline:
        ok = False
        if _WIN32CLIP:
            ok = _win32_copy(text)
        if not ok:
            try:
                pyperclip.copy(text)
                ok = True
                used_api = "pyperclip"
            except Exception as e:  # noqa: BLE001
                print(f"[sender] 剪贴板写入异常: {e}", flush=True)
                time.sleep(0.08)
                continue
        time.sleep(0.08)  # 等剪贴板传播
        last_read = _win32_paste() if _WIN32CLIP else None
        if last_read is None:
            try:
                last_read = pyperclip.paste()
            except Exception:  # noqa: BLE001
                last_read = None
        if last_read == text:
            print(f"[sender] copy ok via {used_api} (len={len(text)})", flush=True)
            return True
        time.sleep(0.05)
    print(f"[sender] 剪贴板验证失败 via {used_api}：期望 {len(text)} 字，"
          f"读回 {len(last_read or '')} 字", flush=True)
    return False


# ---------- 窗口 / 焦点 ----------


def _find_wechat_hwnd() -> int | None:
    if not _WIN32:
        return None
    found: list[int] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd)
            name = win32gui.GetWindowText(hwnd)
            if cls == WECHAT_CLASS or name in WECHAT_NAMES:
                found.append(hwnd)
        except Exception:  # noqa: BLE001
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return found[0] if found else None


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _focus_info() -> tuple[int, int]:
    """返回 (hwndFocus, hwndActive) — 当前前台线程的焦点窗口与活动窗口。"""
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    try:
        if ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            return int(info.hwndFocus or 0), int(info.hwndActive or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def _force_foreground(hwnd: int) -> bool:
    """用 AttachThreadInput 绕过 Windows 的焦点抢夺保护，强制把 hwnd 搞到前台。

    常规 SetForegroundWindow 在"非用户触发"的后台线程里调会被 Windows
    拒绝，只是闪一下任务栏。连发第二条开始就是这种情况——第一次还带着点
    点击余温，第二次就凉了。把当前线程 attach 到当前前台窗口的线程就能
    共享"输入队列"，绕过这条拦截。
    """
    if not _WIN32 or hwnd == 0:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:  # noqa: BLE001
        pass
    cur_fg = user32.GetForegroundWindow()
    if cur_fg == hwnd:
        return True
    this_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(cur_fg, None) if cur_fg else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached_fg = False
    attached_tg = False
    try:
        if fg_tid and fg_tid != this_tid:
            attached_fg = bool(user32.AttachThreadInput(this_tid, fg_tid, True))
        if target_tid and target_tid != this_tid:
            attached_tg = bool(user32.AttachThreadInput(this_tid, target_tid, True))
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached_fg:
            user32.AttachThreadInput(this_tid, fg_tid, False)
        if attached_tg:
            user32.AttachThreadInput(this_tid, target_tid, False)
    return user32.GetForegroundWindow() == hwnd


def activate_wechat() -> bool:
    hwnd = _find_wechat_hwnd()
    if hwnd is None:
        return False
    if _force_foreground(hwnd):
        return True
    # 兜底：点一下窗口中心
    try:
        rect = win32gui.GetWindowRect(hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        from pynput.mouse import Controller as MouseCtrl, Button
        mouse = MouseCtrl()
        mouse.position = (cx, cy)
        mouse.click(Button.left)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[sender] 鼠标点击兜底失败: {e}", flush=True)
        return False


# ---------- 键盘（keybd_event + SendInput Unicode） ----------

_VK_CONTROL = 0x11
_VK_V = 0x56
_VK_RETURN = 0x0D
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1


def _kbd_down(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)


def _kbd_up(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def _send_ctrl_v() -> None:
    _kbd_down(_VK_CONTROL)
    time.sleep(0.03)
    _kbd_down(_VK_V)
    time.sleep(0.03)
    _kbd_up(_VK_V)
    time.sleep(0.03)
    _kbd_up(_VK_CONTROL)


def _send_enter() -> None:
    _kbd_down(_VK_RETURN)
    time.sleep(0.03)
    _kbd_up(_VK_RETURN)


# --- SendInput Unicode 直打（不走剪贴板） ---
# 为什么：连发 Ctrl+V 会全贴成最后一条。日志里能看到 3 轮
# active=focus=target 都锁定在微信 top-level，但 top-level 本身不是输入
# 控件——微信的输入框是 top-level 下的子控件。我们 SetForegroundWindow
# 之后发 Ctrl+V 给 top-level，实际是靠全局加速键/WeChat 的窗口过程把
# paste 转派到输入框；这条转派路径会读"当前剪贴板"而不是排队保留每轮
# 的值，快速 3 连发时 WeChat 只读到最后一个值，于是 3 条消息全是最后
# 一条。（WeChat 3.x 和 4.x 在这点行为一致。）
# 直打 KEYEVENTF_UNICODE 把每个字符作为独立键盘事件送进去，不经剪贴板、
# 不走 paste 加速键，字符按顺序一个个进输入框，天然不会合并。

_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUT_UNION),
    ]


def _send_unicode_unit(code: int, key_up: bool = False) -> bool:
    extra = ctypes.c_ulong(0)
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if key_up else 0)
    ki = _KEYBDINPUT(0, code, flags, 0, ctypes.pointer(extra))
    u = _INPUT_UNION()
    u.ki = ki
    inp = _INPUT(type=_INPUT_KEYBOARD, u=u)
    n = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    return n == 1


def _type_unicode(text: str, per_char_delay: float = 0.004) -> bool:
    """用 SendInput KEYEVENTF_UNICODE 直接把 text 打进当前焦点控件。"""
    sent = 0
    for c in text:
        code = ord(c)
        if code > 0xFFFF:
            # BMP 以外的字符要拆成 UTF-16 代理对
            code -= 0x10000
            hi = 0xD800 + (code >> 10)
            lo = 0xDC00 + (code & 0x3FF)
            if not (_send_unicode_unit(hi) and _send_unicode_unit(hi, key_up=True)):
                return False
            if not (_send_unicode_unit(lo) and _send_unicode_unit(lo, key_up=True)):
                return False
        else:
            if not _send_unicode_unit(code):
                return False
            if not _send_unicode_unit(code, key_up=True):
                return False
        sent += 1
        if per_char_delay:
            time.sleep(per_char_delay)
    return sent == len(text)


# ---------- 发送 ----------


import os

# 环境变量开关：=1 回退到旧的 Ctrl+V 路径（调试用）
_USE_PASTE = os.environ.get("WEMUSE_SEND_PASTE", "0") == "1"


def _send_one(text: str, press_enter: bool, wechat_hwnd: int | None) -> tuple[bool, str]:
    """发一条：激活 → 直打 Unicode → （可选）Enter。

    默认走 SendInput KEYEVENTF_UNICODE 直打，不碰剪贴板——WeChat 4.x 的
    Ctrl+V 是异步 paste 命令，连发会只留下最后一个值。字符直打每个字符是
    独立键盘事件，顺序可靠。
    设 WEMUSE_SEND_PASTE=1 走回 Ctrl+V 路径，仅供对比调试。
    """
    preview = text if len(text) <= 20 else text[:20] + "…"
    print(f"[sender] 发送开始: '{preview}' ({len(text)} 字, enter={press_enter}, "
          f"mode={'paste' if _USE_PASTE else 'type'})", flush=True)

    hwnd = wechat_hwnd or _find_wechat_hwnd()
    if hwnd is None:
        return False, "未找到微信窗口（未启动或未登录？）"
    if not _force_foreground(hwnd):
        print("[sender] 警告：force_foreground 未成功，尝试继续", flush=True)
    time.sleep(0.10)  # 让 Windows 把焦点切换跑完

    focus_hwnd, active_hwnd = _focus_info()
    print(f"[sender] 焦点状态: active=0x{active_hwnd:x} focus=0x{focus_hwnd:x} "
          f"target=0x{hwnd:x}", flush=True)
    if active_hwnd and active_hwnd != hwnd:
        print(f"[sender] 警告：active 窗口不是微信 (class="
              f"{win32gui.GetClassName(active_hwnd) if active_hwnd else '?'})",
              flush=True)

    if _USE_PASTE:
        if not _robust_copy(text):
            return False, "剪贴板写入/验证失败（被剪贴板管理器占用？）"
        try:
            _send_ctrl_v()
        except Exception as e:  # noqa: BLE001
            return False, f"粘贴失败：{e}"
        print("[sender] Ctrl+V 已发出", flush=True)
        time.sleep(0.30)
    else:
        try:
            ok = _type_unicode(text)
        except Exception as e:  # noqa: BLE001
            return False, f"直打失败：{e}"
        if not ok:
            return False, "SendInput 直打失败（部分字符未送出）"
        print(f"[sender] Unicode 直打完成 ({len(text)} 字)", flush=True)
        time.sleep(0.12)  # 给 WeChat 把最后一个字符吃进去的时间

    if press_enter:
        try:
            _send_enter()
        except Exception as e:  # noqa: BLE001
            return False, f"回车失败：{e}"
        print("[sender] Enter 已发出", flush=True)
        time.sleep(0.25)  # 给 WeChat 发出去的时间
        return True, "已发送"
    return True, "已输入到聊天框"


def send_to_wechat(
    text: str,
    press_enter: bool = True,
    contact_name: str | None = None,
) -> tuple[bool, str]:
    """把 text 发到微信。WCF 优先，失败回退剪贴板+键盘。"""
    if not text.strip():
        return False, "回复内容为空"

    if contact_name:
        client = wcf.WCFClient.instance()
        if client.is_running():
            ok, msg = client.send_text_by_name(contact_name, text)
            if ok:
                return True, msg
            print(f"[sender] WCF 发送失败，回退剪贴板：{msg}", flush=True)

    return _send_one(text, press_enter, wechat_hwnd=None)


def send_messages_to_wechat(
    messages: list[str],
    press_enter: bool = True,
    contact_name: str | None = None,
    inter_delay: float = 0.9,
) -> tuple[bool, str]:
    """把多条 text 顺序发到微信。

    WCF 走 per-message 调 WCF API；剪贴板路径在循环前拿一次 WeChat hwnd，
    每条都 force_foreground 重新抢焦点（不然第二条开始会被面板偷走）。
    """
    msgs = [m.strip() for m in messages if m and m.strip()]
    if not msgs:
        return False, "回复内容为空"

    if len(msgs) == 1:
        return send_to_wechat(msgs[0], press_enter=press_enter, contact_name=contact_name)

    # WCF 路径：一条一条调 API，不碰剪贴板/键盘
    if contact_name:
        client = wcf.WCFClient.instance()
        if client.is_running():
            sent = 0
            for i, m in enumerate(msgs):
                ok, msg = client.send_text_by_name(contact_name, m)
                if not ok:
                    print(f"[sender] WCF 第 {i+1} 条失败，回退剪贴板：{msg}", flush=True)
                    break
                sent += 1
                if i != len(msgs) - 1:
                    time.sleep(0.3)
            if sent == len(msgs):
                return True, f"已发送 {sent} 条（WCF）"
            # WCF 没发完 → 剩下的走剪贴板
            msgs = msgs[sent:]

    hwnd = _find_wechat_hwnd()
    print(f"[sender] 多条发送开始: {len(msgs)} 条, inter_delay={inter_delay}s, "
          f"clip_api={'win32' if _WIN32CLIP else 'pyperclip'}, "
          f"wechat_hwnd={'0x%x' % hwnd if hwnd else 'None'}",
          flush=True)

    sent = 0
    for i, m in enumerate(msgs):
        is_last = (i == len(msgs) - 1)
        enter = True if not is_last else press_enter
        print(f"[sender] === 第 {i + 1}/{len(msgs)} 条 ===", flush=True)
        ok, msg = _send_one(m, press_enter=enter, wechat_hwnd=hwnd)
        if not ok:
            return False, f"第 {i + 1}/{len(msgs)} 条失败：{msg}（已发 {sent} 条）"
        sent += 1
        if not is_last:
            time.sleep(inter_delay)
    return True, f"已发送 {sent} 条"
