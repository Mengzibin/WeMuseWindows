"""Windows UI Automation 读取微信聊天内容（PC 版微信 3.x/4.x）。

核心思路（与 macOS 版对应）：
  1. 用 uiautomation 定位微信主窗口（ClassName='WeChatMainWndForPC'）
  2. 按几何位置筛选聊天区（去除左侧侧边栏、顶部工具栏、底部输入框）
  3. 遍历 UIA 控件树，提取所有文本节点
  4. 优先按 "XXX说:" 前缀解析发言人；兜底用 x 坐标
  5. read_wechat_multi_pass：向上翻页触发懒加载，合并去重
"""
from __future__ import annotations

import re
import time
from collections import Counter

try:
    import uiautomation as uia
    _AVAILABLE = True
    _IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001
    _AVAILABLE = False
    _IMPORT_ERR = str(e)
    uia = None  # type: ignore[assignment]

try:
    import win32gui
    import win32con
    import win32process
    _WIN32 = True
except Exception:  # noqa: BLE001
    _WIN32 = False

try:
    import mss
    _MSS = True
except Exception:  # noqa: BLE001
    _MSS = False

WECHAT_CLASS = "WeChatMainWndForPC"
WECHAT_NAMES = {"微信", "WeChat"}

# 几何比例（Windows 微信典型布局：左侧功能栏+联系人列表约占 30%）
CONTACT_FRAC = 0.28   # 左侧 28%（功能图标栏 + 联系人列表）
TOOLBAR_FRAC = 0.09   # 顶部 9%（标题栏 + 聊天对象名称）
INPUT_FRAC   = 0.18   # 底部 18%（输入框 + 工具条，含表情/发送文件/截图按钮行）

_UI_NOISE: set[str] = {
    "搜索", "Search",
    "通讯录", "发现", "设置",
    "收藏", "聊天", "朋友圈", "扫一扫",
    "发送", "Send", "发送(S)",
    "群助手", "新的朋友", "公众号",
    # 顶栏按钮
    "更多", "最小化", "最大化", "还原", "关闭",
    # 输入区按钮
    "表情", "聊天记录", "语音聊天", "视频聊天",
}

_STICKER_RE = re.compile(
    r"发送了一(?:个表情|张图片|张动画表情|段视频|段语音|条语音)|撤回了一条消息"
)
# UI 按钮/快捷键标记（"表情(Alt+E)"、"截图(Alt + A)"、"发送(S)"等）
_UI_BTN_RE = re.compile(r"\(Alt\s*\+\s*[A-Z]\)|\([A-Z]\)$")
# 微信系统分隔符
_SYS_MARK_RE = re.compile(r"^以下(?:为|是)新消息$|^\s*—+\s*$")
# 时间戳：HH:MM 或 HH:MM:SS；支持中文冒号；允许前后有空白
_TIME_RE = re.compile(r"^\s*\d{1,2}[:：]\d{2}(?:[:：]\d{2})?\s*$")
# 日期+时间组合：e.g. "昨天 15:30"、"上午 10:05"、"2024/3/5 20:00"、"3月5日 15:30"
_DATE_TIME_RE = re.compile(
    r"^\s*(?:"
    r"\d{4}[年/\-]\d{1,2}[月/\-]\d{1,2}日?"                    # 2024年3月5日 / 2024/3/5
    r"|\d{1,2}[月/\-]\d{1,2}日?"                               # 3月5日 / 3/5
    r"|昨天|今天|前天|星期[一二三四五六日天]|周[一二三四五六日天]"
    r"|上午|下午|凌晨|早上|中午|晚上|傍晚"
    r")"
    r"(?:\s*\d{1,2}[:：]\d{2}(?:[:：]\d{2})?)?\s*$"
)
_DATE_WORDS = ("昨天", "今天", "前天", "星期", "周一", "周二", "周三",
               "周四", "周五", "周六", "周日", "上午", "下午",
               "凌晨", "早上", "中午", "晚上", "傍晚")

# 明确跳过的控件类型（按钮、工具栏等非消息控件）
_SKIP_CTRL_TYPES: set[str] = {
    "ButtonControl", "ToolBarControl", "MenuBarControl", "MenuItemControl",
    "TitleBarControl", "HeaderControl", "ScrollBarControl", "SliderControl",
    "SplitButtonControl", "TabItemControl", "TreeItemControl",
}

# 严格的发言人前缀：必须含"说"，或冒号前是人名样的短串（禁止纯数字/URL）
# 例： "张三说：" / "张三:" / "我说:"  —— 不匹配 "15:30" / "http://..." / "8:00"
_SPEAKER_SAY_RE = re.compile(r"^(.{1,15}?)说\s*[:：]\s*")
_SPEAKER_COLON_RE = re.compile(r"^([^\d\s:：/\\.()（）<>《》\"'`]{1,15})\s*[:：]\s*")


def available() -> bool:
    return _AVAILABLE


def import_error() -> str:
    return _IMPORT_ERR


# ---------- 定位微信窗口 ----------

def _find_wechat_hwnd() -> int | None:
    """通过 win32gui 枚举窗口找到微信主窗口句柄。

    只认 ClassName=WeChatMainWndForPC 且尺寸够大的窗口——标题为"微信"的
    桌面弹窗通知会干扰，必须排除。
    """
    if not _WIN32:
        return None
    candidates: list[tuple[int, int]] = []  # (hwnd, area)

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return
            cls = win32gui.GetClassName(hwnd)
            if cls != WECHAT_CLASS:
                return
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            # 主窗口至少 600x400；通知弹窗只有 ~220x100
            if w >= 600 and h >= 400:
                candidates.append((hwnd, w * h))
        except Exception:  # noqa: BLE001
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[1])  # 面积最大的优先
    return candidates[0][0]


def _get_wechat_control():
    """获取微信 UIA 根控件。优先按 hwnd 精确获取，否则按 ClassName 搜索。"""
    hwnd = _find_wechat_hwnd()
    if hwnd and _WIN32:
        try:
            ctrl = uia.ControlFromHandle(hwnd)
            if ctrl and ctrl.Exists(0):
                return ctrl
        except Exception:  # noqa: BLE001
            pass
    # 回退：全局搜索（深度 2 以提速）
    try:
        ctrl = uia.WindowControl(ClassName=WECHAT_CLASS, searchDepth=2)
        if ctrl.Exists(1):
            return ctrl
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------- 控件树遍历 ----------

def _walk_controls(
    ctrl,
    out: list[dict],
    bounds_filter: tuple[float, float, float, float] | None = None,
    depth: int = 0,
    max_depth: int = 18,
    cap: int = 800,
) -> None:
    """递归遍历 UIA 控件树，收集文本节点。

    bounds_filter: (x, y, w, h)——只收录中心点在此矩形内的控件。
    """
    if len(out) >= cap or depth > max_depth:
        return

    try:
        name: str = (ctrl.Name or "").strip()
        role: str = ctrl.ControlTypeName or ""
        rect = ctrl.BoundingRectangle
        x, y = rect.left, rect.top
        w, h = rect.right - x, rect.bottom - y

        # 综合过滤
        keep = (
            name
            and name not in _UI_NOISE
            and role not in _SKIP_CTRL_TYPES
            and not _STICKER_RE.search(name)
            and not _UI_BTN_RE.search(name)
            and not _SYS_MARK_RE.match(name)
        )
        if keep:
            in_area = True
            if bounds_filter is not None:
                bx, by, bw, bh = bounds_filter
                cx = x + w / 2
                # 水平：用中心点（避免把侧栏联系人拉进来）
                # 垂直：用 TOP——只要控件顶边在聊天区内就收录，允许底边
                # 探进输入区。否则最新消息（靠近输入框）会因中心点越界被漏。
                in_area = (bx <= cx <= bx + bw) and (by <= y <= by + bh)
            if in_area:
                out.append({
                    "text": name, "role": role,
                    "x": x, "y": y, "w": w, "h": h,
                })
    except Exception:  # noqa: BLE001
        pass

    # 遍历子控件
    try:
        child = ctrl.GetFirstChildControl()
        while child:
            _walk_controls(child, out, bounds_filter, depth + 1, max_depth, cap)
            try:
                child = child.GetNextSiblingControl()
            except Exception:  # noqa: BLE001
                break
    except Exception:  # noqa: BLE001
        pass


# ---------- 过滤 ----------

def _is_timestamp(text: str) -> bool:
    t = text.strip()
    if len(t) > 25:
        return False
    if _TIME_RE.match(t):
        return True
    if _DATE_TIME_RE.match(t):
        return True
    # 兜底：短文本且含日期词（如 "星期一"、"昨天"）
    if len(t) <= 12 and any(w in t for w in _DATE_WORDS):
        return True
    return False


def _parse_speaker(text: str) -> tuple[str | None, str]:
    # 优先匹配带"说"的格式（最可靠）
    m = _SPEAKER_SAY_RE.match(text)
    if m:
        name = m.group(1).strip()
        if name:
            return name, text[m.end():].strip()
    # 其次匹配冒号前是干净短串（无数字/斜杠/点号，排除 "15:30"、"http://..."）
    m = _SPEAKER_COLON_RE.match(text)
    if m:
        name = m.group(1).strip()
        # 排除明显不是人名的前缀
        if name and name not in ("http", "https", "ftp", "www"):
            return name, text[m.end():].strip()
    return None, text


# ---------- 气泡颜色判定 ----------

def _grab_wechat_pixels(rect: tuple[int, int, int, int]):
    """截取微信窗口像素数据。返回 (pixels, origin_x, origin_y, width)——
    pixels 是扁平 BGRA bytes，索引 (y*W + x)*4 拿到 B,G,R,A。
    """
    if not _MSS:
        return None
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    try:
        with mss.mss() as sct:
            raw = sct.grab({"left": left, "top": top, "width": w, "height": h})
            return (bytes(raw.bgra), left, top, w, h)
    except Exception as e:  # noqa: BLE001
        print(f"[UIA] 截屏失败: {e}", flush=True)
        return None


def _vote_bubble_speaker(pixels_info, item: dict) -> tuple[str | None, int, int, int]:
    """在文字 bbox 的**外侧**采样气泡背景色，返回 ('我'/'对方'/None, greens, whites, skipped)。

    为什么采样点要落在 bbox 外侧：UIA 的 TextControl 返回的 BoundingRectangle 是
    紧贴文字的排版框，"四角内移 3 像素"这种采法对中文短字会直接打在笔画上
    （r+g+b 很小），无论绿白气泡都会被当成非绿，导致所有"我"都被判成"对方"。
    正确做法是在文字 bbox 的左右两侧紧邻 4/8/12 像素处采样——微信气泡对文字
    有 8~12 像素的内边距，这些采样点几乎必然在气泡背景色上。
    """
    if pixels_info is None:
        return None, 0, 0, 0
    pixels, ox, oy, pw, ph = pixels_info
    x, y, w, h = item["x"], item["y"], item["w"], item["h"]
    lx, ly = x - ox, y - oy

    y_mid = ly + h // 2
    y_top = ly + max(2, h // 4)
    y_bot = ly + h - max(2, h // 4)

    pts: list[tuple[int, int]] = []
    for dx in (4, 8, 12):
        pts.append((lx - dx, y_mid))
        pts.append((lx - dx, y_top))
        pts.append((lx - dx, y_bot))
        pts.append((lx + w + dx, y_mid))
        pts.append((lx + w + dx, y_top))
        pts.append((lx + w + dx, y_bot))

    greens = whites = skipped = 0
    for sx, sy in pts:
        if not (0 <= sx < pw and 0 <= sy < ph):
            skipped += 1
            continue
        idx = (sy * pw + sx) * 4
        if idx + 3 > len(pixels):
            skipped += 1
            continue
        b, g, r = pixels[idx], pixels[idx + 1], pixels[idx + 2]
        if r + g + b < 200:  # 文字笔画 / 头像深色区
            skipped += 1
            continue
        # 绿气泡 #95EC69 ≈ (149,236,105)
        if g > 170 and g > r + 15 and g > b + 40:
            greens += 1
        # 白气泡 / 浅灰
        elif r > 220 and g > 220 and b > 220:
            whites += 1
        else:
            skipped += 1

    if greens > whites and greens >= 2:
        return "我", greens, whites, skipped
    if whites > greens and whites >= 2:
        return "对方", greens, whites, skipped
    return None, greens, whites, skipped


# ---------- 公开接口 ----------

def read_wechat_as_text(debug: bool = True) -> tuple[str, int] | None:
    """读取当前微信聊天窗口，返回 (带我/对方标注的文本, 消息条数)。失败返回 None。"""
    if not _AVAILABLE:
        print(f"[UIA] uiautomation 不可用: {_IMPORT_ERR}", flush=True)
        return None

    print("[UIA] 开始读取微信…", flush=True)

    win_ctrl = _get_wechat_control()
    if win_ctrl is None:
        print("[UIA] 未找到微信窗口（未启动 / 未登录 / 已最小化？）", flush=True)
        return None

    try:
        rect = win_ctrl.BoundingRectangle
        wx, wy = rect.left, rect.top
        ww, wh = rect.right - wx, rect.bottom - wy
    except Exception as e:  # noqa: BLE001
        print(f"[UIA] 获取窗口几何失败: {e}", flush=True)
        return None

    # 聊天区范围
    chat_x = wx + ww * CONTACT_FRAC
    chat_y = wy + wh * TOOLBAR_FRAC
    chat_w = ww * (1.0 - CONTACT_FRAC)
    chat_h = wh * (1.0 - TOOLBAR_FRAC - INPUT_FRAC)

    raw_items: list[dict] = []
    _walk_controls(win_ctrl, raw_items, bounds_filter=(chat_x, chat_y, chat_w, chat_h))

    if debug:
        print(
            f"[UIA] 窗口=({wx},{wy},{ww},{wh})  "
            f"聊天区=({chat_x:.0f},{chat_y:.0f},{chat_w:.0f},{chat_h:.0f})",
            flush=True,
        )
        role_cnt = Counter(it["role"] for it in raw_items)
        print(f"[UIA] 角色分布: {dict(role_cnt.most_common(6))}  总条目={len(raw_items)}", flush=True)
        for it in raw_items[:15]:
            print(
                f"  raw role={it['role']:<20} x={it['x']} y={it['y']}"
                f" text={it['text'][:50]!r}",
                flush=True,
            )

    if not raw_items:
        print(
            "[UIA] 0 条通过过滤。可能原因：\n"
            "  • 微信不在聊天界面（在通讯录/发现）\n"
            "  • 窗口被遮挡或最小化\n"
            "  • 几何参数需要调整（调整 CONTACT_FRAC / TOOLBAR_FRAC / INPUT_FRAC）",
            flush=True,
        )
        return None

    # 去重：同一条消息会被 UIA 同时报为 ListItemControl（整行容器，宽度≈聊天区宽）
    # 和 TextControl（紧贴文字的气泡框），两者 text 相同但 y 可能差 10~15 像素。
    # 老的 "y//20 桶" 会在桶边界两侧翻车（如 y=372→18 而 y=385→19），导致同一条
    # 被保留两份——一份被判成"对方"（宽容器的采样点全落在聊天区白底上），一份
    # 被判成"我"（紧贴文字的采样点落在绿气泡上）。
    # 改法：按 text 分组，组内按 y 排序，相邻两条 y 差 ≤ 30 像素视为同一条气泡
    # 的不同控件，保留**最窄**的那个（真正的文本气泡，不是容器）。
    raw_items.sort(key=lambda it: (it["text"], it["y"]))
    items: list[dict] = []
    for it in raw_items:
        if (
            items
            and items[-1]["text"] == it["text"]
            and abs(items[-1]["y"] - it["y"]) <= 30
        ):
            if it["w"] < items[-1]["w"]:
                items[-1] = it
        else:
            items.append(it)
    items.sort(key=lambda it: (it["y"], it["x"]))

    # 先对整个微信窗口截一次屏，供气泡颜色采样（最可靠的发言人判定依据）
    pixels_info = _grab_wechat_pixels((wx, wy, wx + ww, wy + wh))

    chat_right = chat_x + chat_w
    lines: list[str] = []
    prev = None
    for it in items:
        text = it["text"]
        if _is_timestamp(text):
            ln = f"  ──【{text.strip()}】──"
        else:
            name, body = _parse_speaker(text)
            if name == "我":
                ln = f"我：{body}"
            elif name is not None:
                ln = f"对方：{body}"
            else:
                # 发言人判定（对齐 macOS accessibility.py 的兜底）：
                #   1) 优先气泡颜色投票（文字 bbox 外侧采样，绿=我，白=对方）
                #   2) 颜色不分胜负时：用气泡中心点相对聊天区中线判定
                speaker, g_votes, w_votes, skip = _vote_bubble_speaker(pixels_info, it)
                src = "color"
                if speaker is None:
                    split = chat_x + chat_w / 2
                    x_center = it["x"] + it["w"] / 2
                    speaker = "我" if x_center >= split else "对方"
                    src = "position"
                if debug:
                    preview = text[:16] + ("…" if len(text) > 16 else "")
                    print(
                        f"[UIA] speaker={speaker} via={src} "
                        f"green={g_votes} white={w_votes} skip={skip} "
                        f"x={it['x']} w={it['w']} text={preview!r}",
                        flush=True,
                    )
                ln = f"{speaker}：{text}"
        if ln != prev:  # 相邻去重（防后续重复排版）
            lines.append(ln)
            prev = ln

    total = len(lines)
    print(f"[UIA] 读取完成，{total} 条消息", flush=True)
    return "\n".join(lines), total


# ---------- 翻页滚动（macOS 风格：精准 ScrollPattern 控制） ----------
#
# 参考 accessibility.py（macOS）的做法：
#   1. 定位聊天区的滚动容器（macOS 是 AXScrollArea，Windows 是支持 ScrollPattern 的控件）
#   2. 结束后恢复原始滚动位置，对用户视角零打扰
#   3. 合并用最简单的 set 去重（按完整行文本），跨 pass 的重复自然被吃掉
#
# 关键差异 —— 为什么 Windows 要用 SmallDecrement 而不是 SetScrollPercent：
#   • macOS AXScrollArea 的 AXChildren 会暴露**已加载的全部行**（含屏外），_walk 一次
#     就能抓满，就算 scroll 百分比跳跃式变化也不会漏中间消息。
#   • Windows UIA 对 ListControl 做了 virtualization，UIA 树里**只有 viewport 可见的
#     ListItemControl**。屏外的消息在 UIA 里不存在，必须滚到可见区域才能抓到。
#   • 所以 Windows 必须保证相邻 pass 的 viewport **有足够重叠**，否则中间消息会
#     被跳过。SetScrollPercent(step=25%) 在长历史聊天里一次跳 20+ 行，viewport
#     只有 ~15 行 → 必漏。改用 Scroll(NoAmount, SmallDecrement) 每次滚一行，
#     一 pass 滚固定行数（< 半个 viewport），重叠有保证。

def _find_chat_scroll_container(win_ctrl) -> dict | None:
    """在微信窗口里搜索支持 ScrollPattern 且位于聊天区的最大控件。

    微信的聊天列表通常是 List/Pane，对应的 UIA 控件会暴露 ScrollPattern。
    判断标准：
      • 支持 GetScrollPattern() 且 VerticalScrollPercent != -1（真的可滚）
      • 中心点在窗口右半边（排除左侧联系人列表的滚动条）
      • 尺寸够大（> 200x200，排除小部件）
      • 同时满足多个时取面积最大的
    """
    try:
        rect = win_ctrl.BoundingRectangle
        wx, wy = rect.left, rect.top
        ww, wh = rect.right - wx, rect.bottom - wy
    except Exception:  # noqa: BLE001
        return None

    chat_left = wx + ww * CONTACT_FRAC
    best: dict | None = None
    stack: list[tuple[object, int]] = [(win_ctrl, 0)]

    while stack:
        ctrl, depth = stack.pop()
        if depth > 18:
            continue
        try:
            pat = ctrl.GetScrollPattern()
        except Exception:  # noqa: BLE001
            pat = None
        if pat is not None:
            try:
                v = pat.VerticalScrollPercent
            except Exception:  # noqa: BLE001
                v = -1
            if v is not None and v >= 0:
                try:
                    r = ctrl.BoundingRectangle
                    cx = (r.left + r.right) / 2
                    w = r.right - r.left
                    h = r.bottom - r.top
                    if cx > chat_left and w > 200 and h > 200:
                        area = w * h
                        if best is None or area > best["area"]:
                            best = {
                                "ctrl": ctrl, "pattern": pat,
                                "percent": float(v), "area": area,
                            }
                except Exception:  # noqa: BLE001
                    pass
        try:
            child = ctrl.GetFirstChildControl()
            while child:
                stack.append((child, depth + 1))
                try:
                    child = child.GetNextSiblingControl()
                except Exception:  # noqa: BLE001
                    break
        except Exception:  # noqa: BLE001
            pass
    return best


def _send_mousewheel_to_hwnd(hwnd: int, screen_x: int, screen_y: int,
                              clicks: int = 1, direction: int = 1) -> bool:
    """直接向 HWND 投递 WM_MOUSEWHEEL，不移动用户光标，不依赖 UIA。

    这是最接近"用户真的滚了一下鼠标滚轮"的方式。DirectUI 自绘控件（微信
    聊天区）对 UIA ScrollPattern 静默失败，但对 WM_MOUSEWHEEL 必然响应，
    因为微信自己就是按这个消息来处理滚动的。

    direction=+1 向上（向老消息），-1 向下（向新消息）。
    返回 True 表示调用 SendMessage 成功。
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        WM_MOUSEWHEEL = 0x020A
        WHEEL_DELTA = 120
        # wParam: HIWORD = wheel delta (signed 16-bit)
        delta = WHEEL_DELTA * (1 if direction > 0 else -1)
        wparam = (delta & 0xFFFF) << 16
        # lParam: 低 16 位 x，高 16 位 y（屏幕坐标）
        lparam = ((screen_y & 0xFFFF) << 16) | (screen_x & 0xFFFF)
        for _ in range(max(1, clicks)):
            user32.SendMessageW(hwnd, WM_MOUSEWHEEL, wparam, lparam)
            time.sleep(0.05)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[UIA] SendMessage WM_MOUSEWHEEL 失败: {e}", flush=True)
        return False


def _scroll_wheel_real(cx: int, cy: int, clicks: int = 1, direction: int = 1) -> None:
    """真实 mouse_event 滚轮（会临时移动光标）。SendMessage 打不动时的终极兜底。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        MOUSEEVENTF_WHEEL = 0x0800
        WHEEL_DELTA = 120
        delta = WHEEL_DELTA * (1 if direction > 0 else -1)
        # 保存当前光标位置
        from ctypes import wintypes
        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
        saved = POINT()
        user32.GetCursorPos(ctypes.byref(saved))
        user32.SetCursorPos(int(cx), int(cy))
        time.sleep(0.03)
        for _ in range(max(1, clicks)):
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            time.sleep(0.06)
        # 恢复光标
        user32.SetCursorPos(saved.x, saved.y)
    except Exception as e:  # noqa: BLE001
        print(f"[UIA] mouse_event 滚轮失败: {e}", flush=True)


def _topmost_chat_text(win_ctrl, bounds: tuple[float, float, float, float]) -> str | None:
    """取当前 viewport 最顶 ListItemControl 的文本，用作滚动是否生效的锚点。"""
    items: list[dict] = []
    _walk_controls(win_ctrl, items, bounds_filter=bounds, max_depth=15, cap=300)
    list_items = [it for it in items if it["role"] == "ListItemControl"]
    if not list_items:
        return None
    top = min(list_items, key=lambda it: it["y"])
    return top["text"]


# UIA ScrollAmount 常量（uiautomation 包不总是暴露 enum，直接用数值）
_SCROLL_LARGE_DECREMENT = 0
_SCROLL_SMALL_DECREMENT = 1
_SCROLL_NO_AMOUNT = 2
_SCROLL_LARGE_INCREMENT = 3
_SCROLL_SMALL_INCREMENT = 4


def _scroll_up_small(pattern, steps: int) -> bool:
    """用 ScrollPattern.Scroll(SmallDecrement) 向上滚 steps 行。

    每次 SmallDecrement ≈ 滚一行消息。调用 N 次 = 向上 N 行。
    配合 viewport ~15 行，每 pass 滚 8 行 ⇒ 相邻 pass 的 viewport 有 ~7 行重叠
    ⇒ 绝不跳过中间消息。成功返回 True，不支持返回 False 让上层走 SetScrollPercent。
    """
    try:
        for _ in range(max(1, steps)):
            pattern.Scroll(_SCROLL_NO_AMOUNT, _SCROLL_SMALL_DECREMENT)
            time.sleep(0.02)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[UIA] SmallDecrement 不支持 ({e})", flush=True)
        return False


def read_wechat_multi_pass(passes: int = 8, clicks_per_pass: int = 1) -> tuple[str, int] | None:
    """读取微信并自动向上滚动 N 次收集更多历史。

    **关键：不依赖 UIA ScrollPattern** —— 上一版日志显示那个容器在微信 DirectUI
    上定位错误（原位置 1.0% 却在聊天底部；一次 SmallDecrement 就跳到 0%），所以
    百分比语义完全不可靠。改为直接给微信主 HWND 发 WM_MOUSEWHEEL，等效于用户
    手动滚一下鼠标滚轮，微信必然按消息条数滚动，中间消息一条都不漏。

    策略：
      1. 读首屏；定位微信主 HWND 和聊天区中心屏幕坐标；
      2. 循环 passes 次：SendMessage(WM_MOUSEWHEEL, +DELTA) × clicks_per_pass,
         每 click ≈ 3 行消息，等 0.6s 让懒加载渲染完成，再读一次；
      3. **锚点验证** —— 记每 pass 开始时最顶 ListItem 的文本，滚完对比：
         • 文本变了 → 滚动成功；
         • 文本没变 → SendMessage 被吃了，改 mouse_event 真实滚轮（临时动光标再恢复）；
         • 还不动 = 判定触顶，break。
      4. 结束后反向滚回去，恢复用户视角；
      5. 合并：reversed + set 去重（macOS 同款）。
    """
    first = read_wechat_as_text()
    if first is None or passes <= 0:
        return first

    win_ctrl = _get_wechat_control()
    hwnd = _find_wechat_hwnd()
    if win_ctrl is None or not hwnd:
        print("[UIA] 无法定位微信 HWND，返回首屏", flush=True)
        return first

    try:
        r = win_ctrl.BoundingRectangle
        wx, wy = r.left, r.top
        ww, wh = r.right - wx, r.bottom - wy
    except Exception:  # noqa: BLE001
        return first

    chat_bounds = (
        wx + ww * CONTACT_FRAC,
        wy + wh * TOOLBAR_FRAC,
        ww * (1.0 - CONTACT_FRAC),
        wh * (1.0 - TOOLBAR_FRAC - INPUT_FRAC),
    )
    chat_cx = int(chat_bounds[0] + chat_bounds[2] / 2)
    chat_cy = int(chat_bounds[1] + chat_bounds[3] / 2)

    outputs: list[list[str]] = [first[0].splitlines()]
    print(f"[UIA] 多轮读取：每 pass 滚 {clicks_per_pass} 刻度，"
          f"中心=({chat_cx},{chat_cy}) HWND={hwnd}", flush=True)

    stuck_count = 0
    total_clicks_up = 0
    prev_anchor = _topmost_chat_text(win_ctrl, chat_bounds)

    try:
        for i in range(1, passes + 1):
            # 第 1 层：WM_MOUSEWHEEL via SendMessage（不动用户光标）
            _send_mousewheel_to_hwnd(hwnd, chat_cx, chat_cy,
                                     clicks=clicks_per_pass, direction=1)
            total_clicks_up += clicks_per_pass
            time.sleep(0.6)

            new_anchor = _topmost_chat_text(win_ctrl, chat_bounds)

            if new_anchor is not None and new_anchor == prev_anchor:
                # 第 2 层：mouse_event 真实滚轮
                print(f"[UIA] pass {i}: SendMessage 未生效，改用 mouse_event",
                      flush=True)
                _scroll_wheel_real(chat_cx, chat_cy,
                                   clicks=max(2, clicks_per_pass), direction=1)
                total_clicks_up += max(2, clicks_per_pass)
                time.sleep(0.6)
                new_anchor = _topmost_chat_text(win_ctrl, chat_bounds)
                if new_anchor == prev_anchor:
                    stuck_count += 1
                    if stuck_count >= 2:
                        print("[UIA] 连续 2 pass 滚不动，判定已触顶", flush=True)
                        break
                    continue

            stuck_count = 0
            prev_anchor = new_anchor

            r = read_wechat_as_text(debug=False)
            if r is None:
                print(f"[UIA] pass {i}: 读取失败，跳过", flush=True)
                continue
            lines = r[0].splitlines()
            anchor_preview = (new_anchor[:16] if new_anchor else "?")
            print(f"[UIA] pass {i}: 读到 {len(lines)} 行 (顶部={anchor_preview!r})",
                  flush=True)
            outputs.append(lines)
    finally:
        if total_clicks_up > 0:
            print(f"[UIA] 恢复视角：向下滚 {total_clicks_up + 1} 刻度", flush=True)
            _send_mousewheel_to_hwnd(hwnd, chat_cx, chat_cy,
                                     clicks=total_clicks_up + 1, direction=-1)

    # 合并：参考 macOS 版本，reversed 顺序 + set 去重
    seen: set[str] = set()
    merged: list[str] = []
    for pass_lines in reversed(outputs):
        for line in pass_lines:
            if line and line not in seen:
                seen.add(line)
                merged.append(line)

    total = len(merged)
    total_raw = sum(len(p) for p in outputs)
    print(f"[UIA] 多轮合并后共 {total} 条（去重前 {total_raw}）", flush=True)
    return "\n".join(merged), total
