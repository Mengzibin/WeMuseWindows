"""Windows OCR：优先 PaddleOCR（ML 中文 OCR，带 bbox→气泡颜色识别发言人），
回退到 Windows 内置 OCR 或 pytesseract。

对应 macOS 版的 ocr.py（macOS 用 Apple Vision + 气泡颜色识别发言人）。

层级：
  1. PaddleOCR（ML，CPU 跑得动，中文准确率高，**返回 bbox**——与 macOS 对齐）
     → pip install paddleocr paddlepaddle   （paddlepaddle 无 -gpu 后缀即 CPU 版）
  2. Windows.Media.Ocr（系统内置，回退）
  3. pytesseract + Tesseract-OCR（需要外部安装 Tesseract）
  4. 都不可用时返回提示字符串

只有 PaddleOCR 路径能输出 "我：.../对方：..." 的发言人标注（依赖 bbox + 气泡颜色）；
其它两路只能给纯文本。
"""
from __future__ import annotations

import asyncio
import os

from PIL import Image

# ── 禁用 PaddlePaddle 的 OneDNN / PIR（必须在 import paddle* 之前设）──
# PaddlePaddle 3.x CPU 版在 Windows 上用 OneDNN+PIR 执行器会报
# "ConvertPirAttribute2RuntimeAttribute not support ..."（onednn_instruction.cc:118），
# 直接关掉这两个特性，走传统执行路径，稳定。
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_enable_pir_in_executor", "0")
# 启动时跳过 "Checking connectivity to the model hosters..." 的网络探测
# （每次启动浪费 5~10 秒，模型已缓存到本地的情况下完全没必要）
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

# ── 尝试 PaddleOCR（首选）──
_PADDLE_OK = False
_PADDLE_ERR = ""
_PADDLE_RUNTIME_ERR = ""      # 运行时首次失败的错误（init/inference），给 UI 显示用
_PADDLEOCR_VER = "?"
_PADDLE_VER = "?"
_PADDLE_MAJOR = 0             # paddlepaddle 主版本号，0 = 没装
_paddle_instance = None       # 延迟初始化：首次调用时才加载模型

try:
    import paddleocr as _paddleocr_mod  # noqa: F401
    _PADDLE_OK = True
    try:
        _PADDLEOCR_VER = getattr(_paddleocr_mod, "__version__", "?")
    except Exception:  # noqa: BLE001
        pass
except Exception as e:  # noqa: BLE001
    _PADDLE_ERR = str(e)

# 独立探测 paddlepaddle 版本，用于检测版本错位
try:
    import paddle as _paddle_mod  # type: ignore  # noqa: F401
    _PADDLE_VER = getattr(_paddle_mod, "__version__", "?")
    try:
        _PADDLE_MAJOR = int(str(_PADDLE_VER).split(".")[0])
    except Exception:  # noqa: BLE001
        _PADDLE_MAJOR = 0
except Exception:  # noqa: BLE001
    pass


def _paddleocr_major() -> int:
    """解析 paddleocr 主版本号（2 或 3）。解析不出时默认 3（按最新 API 试）。"""
    try:
        return int(str(_PADDLEOCR_VER).split(".")[0])
    except Exception:  # noqa: BLE001
        return 3


def _version_mismatch_hint() -> str:
    """如果 paddleocr 和 paddlepaddle 主版本号不一致，返回修复建议；否则空串。

    版本错位是最常见的坑——paddle 2.x + paddleocr 3.x 时，init 会抛异常，
    OCR 静默回退到 WinRT（带空格、不分发言人），用户看不出来为什么变差了。
    """
    if not _PADDLE_OK or _PADDLE_MAJOR == 0:
        return ""
    po_major = _paddleocr_major()
    if po_major == _PADDLE_MAJOR:
        return ""
    # 2.x paddle + 3.x paddleocr（最常见）
    if _PADDLE_MAJOR == 2 and po_major >= 3:
        return (
            f"版本错位：paddleocr={_PADDLEOCR_VER} (3.x) "
            f"但 paddlepaddle={_PADDLE_VER} (2.x)。\n"
            f"修复：pip install \"paddleocr<3\""
        )
    if _PADDLE_MAJOR >= 3 and po_major == 2:
        return (
            f"版本错位：paddleocr={_PADDLEOCR_VER} (2.x) "
            f"但 paddlepaddle={_PADDLE_VER} (3.x)。\n"
            f"修复：pip install -U paddleocr"
        )
    return ""


def _get_paddle():
    """首次调用时加载模型。**版本自适应**：

    paddleocr 2.x（配 paddlepaddle 2.x，典型版本 2.6.2 / 2.7.x）：
      • 签名：`PaddleOCR(use_angle_cls=True, lang='ch', use_gpu=?, show_log=False)`
      • **没有** `use_doc_orientation_classify` / `use_doc_unwarping` / `device` 这些 kwarg
      • GPU 开关叫 `use_gpu`（布尔），不是 `device`

    paddleocr 3.x（配 paddlepaddle 3.x）：
      • 签名换成 `use_textline_orientation` / `device`
      • 多出 `use_doc_orientation_classify` / `use_doc_unwarping`
      • `show_log` 被删掉了

    按 paddleocr 主版本号选合适的 kwarg 优先级，然后降级尝试，避免
    "全是 3.x kwarg 撞到 2.x PaddleOCR" 导致 init 整串失败。

    GPU：如果 paddle 编译带 CUDA 就默认开 GPU。可用 WEMUSE_PADDLE_DEVICE=gpu/cpu 强制覆盖。
    """
    global _paddle_instance, _PADDLE_RUNTIME_ERR
    if _paddle_instance is None:
        # 先检查版本错位——2.x paddle + 3.x paddleocr 在 init 时会抛异常，
        # 与其让用户看到莫名其妙的 TypeError，不如直接放弃并给出明确修复命令
        hint = _version_mismatch_hint()
        if hint:
            print(f"[ocr] ⚠ {hint}", flush=True)
            _PADDLE_RUNTIME_ERR = hint
            raise RuntimeError(hint)

        from paddleocr import PaddleOCR

        # 设备探测
        try:
            import paddle  # type: ignore
            cuda_compiled = bool(paddle.device.is_compiled_with_cuda())
        except Exception:  # noqa: BLE001
            cuda_compiled = False

        device_env = os.environ.get("WEMUSE_PADDLE_DEVICE", "").lower().strip()
        if device_env in ("gpu", "cpu"):
            want_gpu = (device_env == "gpu")
        else:
            want_gpu = cuda_compiled

        pver = _paddleocr_major()
        print(
            f"[ocr] paddleocr={_PADDLEOCR_VER}(major={pver}) paddle={_PADDLE_VER} "
            f"cuda_compiled={cuda_compiled} want_gpu={want_gpu}",
            flush=True,
        )

        # 3.x 候选 —— 关掉三个不必要预处理模型
        attempts_3x: tuple[dict, ...] = (
            {
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "lang": "ch",
                "device": "gpu" if want_gpu else "cpu",
                "enable_mkldnn": False,
            },
            {
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "lang": "ch",
                "enable_mkldnn": False,
            },
            {"use_textline_orientation": False, "lang": "ch", "enable_mkldnn": False},
            {"use_textline_orientation": True, "lang": "ch"},
        )

        # 2.x 候选 —— use_gpu + show_log=False
        attempts_2x: tuple[dict, ...] = (
            {
                "use_angle_cls": True,
                "lang": "ch",
                "use_gpu": want_gpu,
                "show_log": False,
                "enable_mkldnn": False,
            },
            {
                "use_angle_cls": True,
                "lang": "ch",
                "use_gpu": want_gpu,
                "show_log": False,
            },
            {"use_angle_cls": True, "lang": "ch", "use_gpu": want_gpu},
            {"use_angle_cls": True, "lang": "ch", "show_log": False},
            {"use_angle_cls": True, "lang": "ch"},
            {"lang": "ch"},
        )

        # 按检测到的版本号决定优先级
        attempts = attempts_3x + attempts_2x if pver >= 3 else attempts_2x + attempts_3x

        last_err: Exception | None = None
        for kwargs in attempts:
            try:
                _paddle_instance = PaddleOCR(**kwargs)
                print(f"[ocr] PaddleOCR 初始化成功: {kwargs}", flush=True)
                break
            except TypeError as e:
                # 不认识的 kwarg —— 正常降级，安静继续
                last_err = e
                continue
            except Exception as e:  # noqa: BLE001
                # 其他异常（模型下载失败、CUDA 初始化失败等）也继续降级
                print(f"[ocr] 试 {list(kwargs.keys())} 失败: {e}", flush=True)
                last_err = e
                continue
        if _paddle_instance is None:
            err_msg = str(last_err) if last_err else "PaddleOCR 初始化失败"
            _PADDLE_RUNTIME_ERR = err_msg
            raise last_err or RuntimeError(err_msg)
    return _paddle_instance

# ── 尝试 Windows 内置 OCR（winrt）──
# 必须 import winrt.windows.foundation 才能让异步操作绑定生效
# （winrt-runtime 3.x 的 _IAsyncOperation 挂在 Foundation 命名空间下）
_WINRT_OCR = False
_WINRT_ERR = ""
_WINRT_INIT_DONE = False
try:
    import winrt.runtime as _winrt_rt
    import winrt.windows.foundation  # noqa: F401  必需，否则异步操作报 _IAsyncOperation 缺失
    import winrt.windows.foundation.collections  # noqa: F401
    import winrt.windows.media.ocr as _winrt_ocr
    import winrt.windows.globalization as _winrt_glob
    import winrt.windows.graphics.imaging as _winrt_img
    import winrt.windows.storage.streams as _winrt_streams
    _WINRT_OCR = True
except Exception as e:  # noqa: BLE001
    _WINRT_ERR = str(e)


def _ensure_winrt_apartment() -> None:
    """WinRT 异步操作要求进程处于 MTA/STA 模式，只需初始化一次。"""
    global _WINRT_INIT_DONE
    if _WINRT_INIT_DONE or not _WINRT_OCR:
        return
    try:
        _winrt_rt.init_apartment(_winrt_rt.MTA)
    except Exception:  # noqa: BLE001
        # 已初始化过会抛异常，忽略即可
        pass
    _WINRT_INIT_DONE = True

# ── 尝试 pytesseract ──
_TESS = False
_TESS_ERR = ""
try:
    import pytesseract
    # 验证 tesseract 可执行文件存在
    pytesseract.get_tesseract_version()
    _TESS = True
except Exception as e:  # noqa: BLE001
    _TESS_ERR = str(e)


# ── 图像预处理：小图放大 + 对比度增强 ──

def _preprocess_for_ocr(img: "Image.Image") -> "Image.Image":
    """放大小图 + 轻度锐化——OCR 对小字识别率骤降，特别是 WeChat 聊天里的
    时间戳（字号 ~12px）和灰字。经验值：把高度放到 ≥1200px 后召回率显著提升。

    也可启用灰度+自动对比（WinRT 吃彩色图 OK，但灰度后对比更稳）。
    """
    from PIL import ImageFilter, ImageOps

    w, h = img.size
    # 放大：目标高度 1200（聊天截图通常在 300~800 高）
    target_h = 1200
    if h < target_h:
        scale = min(3.0, target_h / max(h, 1))
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        print(f"[ocr] 预处理：放大 {w}x{h} -> {new_w}x{new_h} (x{scale:.2f})",
              flush=True)
    # 轻度锐化，强化小字边缘
    try:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=80, threshold=3))
    except Exception:  # noqa: BLE001
        pass
    return img


# ── PaddleOCR 实现（带发言人识别，对齐 macOS ocr.py）──

def _classify_speaker(img: "Image.Image", px: int, py: int, pw: int, ph: int) -> str:
    """按 WeChat 气泡颜色判定发言人：绿色气泡 = 我，白色气泡 = 对方。

    以前用"位置"做主信号，但用户截图框可能包含了聊天区之外的边距（比如
    联系人列表或窗口右侧的 padding），导致所有气泡的 center_x / W 都偏左，
    把"我"的气泡也判成"对方"。改用颜色做主信号：WeChat 3.x 的气泡颜色是
    固定的 `#95EC69`（绿）vs 白，只要采样点落在气泡背景上，判断就稳。

    采样策略：文字左右侧、紧贴文字边（3~8 像素）的位置几乎必然在气泡内
    （气泡有 8~12 像素的内边距），避开了文字笔画和头像区。色彩投票。
    颜色不分胜负时再用位置兜底。
    """
    W, H_img = img.size

    # 文字左右两侧紧邻处 × 3 个垂直位置：9 个样本点都在气泡内边距上
    y_mid = py + ph // 2
    y_top = py + max(2, ph // 5)
    y_bot = py + ph - max(2, ph // 5)
    sample_positions = []
    for dx in (3, 6, 10):
        sample_positions.append((px - dx, y_mid))
        sample_positions.append((px - dx, y_top))
        sample_positions.append((px - dx, y_bot))
        sample_positions.append((px + pw + dx, y_mid))
        sample_positions.append((px + pw + dx, y_top))
        sample_positions.append((px + pw + dx, y_bot))

    greens = 0
    whites = 0
    skipped = 0
    for sx, sy in sample_positions:
        sx = max(0, min(W - 1, sx))
        sy = max(0, min(H_img - 1, sy))
        pix = img.getpixel((sx, sy))
        if isinstance(pix, tuple):
            r, g, b = pix[:3]
        else:
            r = g = b = int(pix)
        if r + g + b < 150:  # 太暗（文字/阴影）
            skipped += 1
            continue
        # 绿气泡：#95EC69 ≈ (149,236,105)，G 显著高于 R 和 B
        if g > 170 and g > r + 20 and g > b + 40:
            greens += 1
        # 白气泡：接近全白
        elif r > 220 and g > 220 and b > 220:
            whites += 1
        else:
            skipped += 1

    center_x = px + pw // 2
    if greens > whites and greens >= 2:
        print(f"[ocr] 气泡色: green={greens} white={whites} skip={skipped} "
              f"x={px}+{pw} → me", flush=True)
        return "me"
    if whites > greens and whites >= 2:
        print(f"[ocr] 气泡色: green={greens} white={whites} skip={skipped} "
              f"x={px}+{pw} → them", flush=True)
        return "them"

    # 颜色不分胜负（气泡被裁/采样全被文字占）→ 用位置兜底
    pos = "me" if center_x > W * 0.55 else ("them" if center_x < W * 0.45 else "them")
    print(f"[ocr] 气泡色不决: green={greens} white={whites} skip={skipped} "
          f"→ 位置兜底 cx={center_x}/{W} → {pos}", flush=True)
    return pos


def _field(obj, key, default=None):
    """兼容取字段：dict → .get(key)；object → getattr(obj, key)。"""
    try:
        if hasattr(obj, "__contains__") and key in obj:
            return obj[key] if default is None else obj.get(key, default)
    except Exception:  # noqa: BLE001
        pass
    if hasattr(obj, key):
        return getattr(obj, key, default)
    return default


def _extract_paddle_items(raw) -> list[tuple[list, str, float]]:
    """把 PaddleOCR 2.x / 3.x 的几种返回格式规整为 [(bbox_quad, text, conf), ...]。

    2.x：`[[[bbox, (text, conf)], ...]]`  （外层按图分页）
    3.x：`[OCRResult]`，OCRResult 里有 `rec_texts/rec_scores/rec_polys/rec_boxes`
    可能是 dict-like 也可能是 object-like；可能是 list 也可能是 generator。
    """
    out: list[tuple[list, str, float]] = []
    if raw is None:
        return out

    # 物化 generator
    if not hasattr(raw, "__getitem__") and not isinstance(raw, list):
        try:
            raw = list(raw)
        except Exception:  # noqa: BLE001
            return out
    if not raw:
        return out

    first = raw[0]

    # ── 分支 A：3.x OCRResult 对象/字典 ──
    texts = _field(first, "rec_texts")
    if texts is not None:
        scores = _field(first, "rec_scores") or []
        polys = _field(first, "rec_polys")
        if polys is None:
            polys = _field(first, "rec_boxes") or []
        try:
            texts_list = list(texts)
        except Exception:  # noqa: BLE001
            texts_list = []
        try:
            polys_list = list(polys)
        except Exception:  # noqa: BLE001
            polys_list = []
        n = min(len(texts_list), len(polys_list))
        for i in range(n):
            conf = float(scores[i]) if i < len(scores) else 1.0
            poly = polys_list[i]
            try:
                pts = [[float(p[0]), float(p[1])] for p in poly]
            except Exception:  # noqa: BLE001
                try:
                    flat = [float(v) for v in poly]
                except Exception:  # noqa: BLE001
                    continue
                if len(flat) == 4:
                    x1, y1, x2, y2 = flat
                    pts = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                else:
                    continue
            out.append((pts, str(texts_list[i]), conf))
        return out

    # ── 分支 B：2.x 嵌套列表 ──
    page = first if isinstance(first, list) else raw
    for line in page or []:
        try:
            bbox_quad, (text, conf) = line
            out.append((bbox_quad, str(text), float(conf)))
        except Exception:  # noqa: BLE001
            continue
    return out


def _run_paddle_ocr(image_path: str) -> str:
    """PaddleOCR 识别 + 按气泡颜色标注发言人，返回带 "我：/对方：" 前缀的文本。

    兼容 2.x 与 3.x 的 5 种调用签名，按稳定度从高到低依次尝试，每一步都打日志。
    """
    ocr = _get_paddle()
    attempts = [
        ("predict(path)",         lambda: ocr.predict(image_path) if hasattr(ocr, "predict") else None),
        ("predict(input=path)",   lambda: ocr.predict(input=image_path) if hasattr(ocr, "predict") else None),
        ("ocr(path)",             lambda: ocr.ocr(image_path)),
        ("ocr(input=path)",       lambda: ocr.ocr(input=image_path)),
        ("ocr(path, cls=True)",   lambda: ocr.ocr(image_path, cls=True)),
    ]
    raw = None
    for label, call in attempts:
        try:
            result = call()
            if result is None:
                print(f"[ocr] Paddle {label}: None", flush=True)
                continue
            # 物化 generator
            if not isinstance(result, (list, tuple)) and hasattr(result, "__iter__"):
                try:
                    result = list(result)
                except Exception:  # noqa: BLE001
                    pass
            raw = result
            try:
                n_hint = len(raw)
            except Exception:  # noqa: BLE001
                n_hint = "?"
            print(f"[ocr] Paddle {label}: 成功 (外层 {n_hint} 项)", flush=True)
            break
        except TypeError as e:
            print(f"[ocr] Paddle {label} 不支持: {e}", flush=True)
            continue
        except Exception as e:  # noqa: BLE001
            print(f"[ocr] Paddle {label} 运行报错: {e}", flush=True)
            continue
    if raw is None:
        raise RuntimeError("PaddleOCR 所有调用签名都不可用（见上方 [ocr] 日志）")

    lines = _extract_paddle_items(raw)
    if not lines:
        return ""

    img = Image.open(image_path).convert("RGB")

    # 时间戳正则：H:MM、HH:MM、HH:MM:SS（支持中文冒号），允许"上午/下午/昨天"等前缀
    import re as _re
    time_re = _re.compile(
        r"^\s*(?:(?:昨天|今天|前天|上午|下午|凌晨|早上|中午|晚上|傍晚|"
        r"星期[一二三四五六日天]|周[一二三四五六日天])\s*)?"
        r"\d{1,2}[:：]\d{2}(?:[:：]\d{2})?\s*$"
    )

    items: list[tuple[str, str, float, float, float]] = []  # (speaker, text, y, x, h)
    W_img = img.size[0]
    for bbox_quad, text, conf in lines:
        t = text.strip()
        if not t:
            continue
        xs = [p[0] for p in bbox_quad]
        ys = [p[1] for p in bbox_quad]
        x, y = int(min(xs)), int(min(ys))
        w = max(1, int(max(xs) - x))
        h = max(1, int(max(ys) - y))
        # 阈值压到 0.15——短中文如"嗐""晚安~"容易落在 0.15~0.25 区间，
        # 0.25 太严会漏；0.15 过滤的基本只是噪点
        if conf < 0.15:
            print(f"[ocr] 低置信度跳过: '{t}' conf={conf:.2f}", flush=True)
            continue
        # 时间戳：不做发言人判定，统一标为 "time"
        if time_re.match(t):
            items.append(("time", t, y, x, h))
            print(f"[ocr] det time: '{t}' conf={conf:.2f} x={x} y={y}", flush=True)
        else:
            speaker = _classify_speaker(img, x, y, w, h)
            items.append((speaker, t, y, x, h))
            cx = x + w // 2
            print(f"[ocr] det {speaker}: '{t}' conf={conf:.2f} "
                  f"cx={cx}/{W_img} ({100*cx/W_img:.0f}%) y={y}", flush=True)

    if not items:
        return ""

    # 从上到下（PIL 原点左上）
    items.sort(key=lambda it: (it[2], it[3]))
    print(f"[ocr] PaddleOCR 识别 {len(items)} 条", flush=True)

    # 输出策略：
    #   • 时间戳单独一行 "──【HH:MM】──"
    #   • 每个 OCR 检测框对应一条消息，独占一行（不合并同发言人相邻条目，
    #     保留原始消息边界；同一气泡跨多行才合并，用 y 间距判定）
    out: list[str] = []
    prev_speaker: str | None = None
    prev_bottom: float | None = None
    prev_h: float | None = None

    for speaker, text, y, x, h in items:
        if speaker == "time":
            out.append(f"  ──【{text}】──")
            prev_speaker = None
            prev_bottom = None
            prev_h = None
            continue

        label = "我" if speaker == "me" else "对方"

        # 合并条件：同一发言人 + 垂直间距 < 当前行高的 60%（同气泡换行）
        gap_is_small = (
            prev_speaker == speaker
            and prev_bottom is not None
            and prev_h is not None
            and y - prev_bottom < prev_h * 0.6
        )
        if gap_is_small and out:
            # 接到上一行同一气泡后面
            out[-1] = f"{out[-1]}{text}"
        else:
            out.append(f"{label}：{text}")

        prev_speaker = speaker
        prev_bottom = y + h
        prev_h = h

    return "\n".join(out)


# ── Windows 内置 OCR 实现 ──

async def _winrt_ocr_async(image_path: str) -> str:
    """用 Windows.Media.Ocr 识别图片，支持中英文混合。

    做法：把 PIL 图片存成临时 PNG 再读回字节流——绕开 WinRT 不同版本对
    RGBA→SoftwareBitmap 参数的签名差异。
    """
    import io
    img_pil = Image.open(image_path).convert("RGB")
    img_pil = _preprocess_for_ocr(img_pil)
    img_pil = img_pil.convert("RGBA")
    buf = io.BytesIO()
    img_pil.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    # 写到 InMemoryRandomAccessStream
    stream = _winrt_streams.InMemoryRandomAccessStream()
    writer = _winrt_streams.DataWriter(stream)
    writer.write_bytes(png_bytes)  # 新版 winrt 接受 bytes/bytearray
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)

    bitmap = await _winrt_img.BitmapDecoder.create_async(stream)
    soft_bmp = await bitmap.get_software_bitmap_async()

    # 优先简体中文，回退英文
    lang_zh = _winrt_glob.Language("zh-Hans-CN")
    if _winrt_ocr.OcrEngine.is_language_supported(lang_zh):
        engine = _winrt_ocr.OcrEngine.try_create_from_language(lang_zh)
    else:
        engine = _winrt_ocr.OcrEngine.try_create_from_user_profile_languages()

    if engine is None:
        return "[Windows OCR: 未找到支持的语言包，请在系统设置里安装中文语言包]"

    result = await engine.recognize_async(soft_bmp)
    lines = [line.text for line in result.lines]
    return "\n".join(lines)


def _run_winrt_ocr(image_path: str) -> str:
    _ensure_winrt_apartment()
    try:
        return asyncio.run(_winrt_ocr_async(image_path))
    except Exception as e:  # noqa: BLE001
        return f"[Windows OCR 失败: {e}]"


# ── pytesseract 实现 ──

def _run_tess_ocr(image_path: str) -> str:
    img = Image.open(image_path).convert("RGB")
    img = _preprocess_for_ocr(img)
    # PSM 6 = 把图当成一个统一的文本块（适合聊天截图）；中英文优先
    langs = "chi_sim+eng"
    config = "--psm 6"
    try:
        return pytesseract.image_to_string(img, lang=langs, config=config)
    except pytesseract.TesseractError:
        # 回退纯英文
        return pytesseract.image_to_string(img, config=config)


# ── 公开接口（与 macOS ocr.py 保持相同签名）──

def _looks_empty(text: str) -> bool:
    """WinRT 偶尔会返回空串或只含失败前缀——这时要回退到 tesseract。"""
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith("[Windows OCR 失败") or stripped.startswith("[Windows OCR:"):
        return True
    return False


def ocr_image(image_path: str) -> str:
    """对图片做 OCR，返回识别出的文字字符串。

    优先 PaddleOCR（带 "我：/对方：" 发言人标注），失败回退 WinRT，再回退 tesseract。
    """
    global _PADDLE_RUNTIME_ERR
    try:
        img_size = Image.open(image_path).size
    except Exception:  # noqa: BLE001
        img_size = None
    print(f"[ocr] 输入图片: {image_path} size={img_size} "
          f"paddle={_PADDLE_OK} winrt={_WINRT_OCR} tess={_TESS}", flush=True)

    # 1) PaddleOCR —— 有 bbox，能识别发言人，准确率最高
    paddle_failed = False
    if _PADDLE_OK:
        try:
            paddle_result = _run_paddle_ocr(image_path)
            print(f"[ocr] PaddleOCR 返回 {len(paddle_result)} 字符", flush=True)
            if not _looks_empty(paddle_result):
                return paddle_result
            paddle_failed = True
            _PADDLE_RUNTIME_ERR = _PADDLE_RUNTIME_ERR or "PaddleOCR 返回空结果"
        except Exception as e:  # noqa: BLE001
            paddle_failed = True
            _PADDLE_RUNTIME_ERR = str(e)
            print(f"[ocr] PaddleOCR 失败: {e}", flush=True)

    winrt_result = ""
    if _WINRT_OCR:
        if paddle_failed:
            print("[ocr] ⚠ PaddleOCR 不可用，回退到 Windows 内置 OCR（无发言人标注、字符间带空格）",
                  flush=True)
        winrt_result = _run_winrt_ocr(image_path)
        print(f"[ocr] WinRT 返回 {len(winrt_result)} 字符", flush=True)
        if not _looks_empty(winrt_result):
            # 回退时在第一行加一条警告，用户在聊天区能直接看到为什么质量下降
            if paddle_failed:
                mismatch = _version_mismatch_hint()
                head = f"⚠ PaddleOCR 未运行（{_PADDLE_RUNTIME_ERR[:80]}）"
                if mismatch:
                    head += f"\n⚠ {mismatch}"
                return f"{head}\n────\n{winrt_result}"
            return winrt_result

    if _TESS:
        try:
            tess_result = _run_tess_ocr(image_path)
            print(f"[ocr] Tesseract 返回 {len(tess_result)} 字符", flush=True)
            if not _looks_empty(tess_result):
                return tess_result
            if winrt_result:
                return winrt_result
            return "[OCR 未识别到文字，请尝试放大框选区域 / 提高对比度]"
        except Exception as e:  # noqa: BLE001
            if winrt_result:
                return winrt_result
            return f"[Tesseract 失败: {e}]"

    if winrt_result:
        # WinRT 虽然返回失败信息，但没有 tesseract 可回退
        return winrt_result

    # 三者均不可用
    tips = []
    if _PADDLE_ERR:
        tips.append(f"paddleocr: {_PADDLE_ERR}")
    if _WINRT_ERR:
        tips.append(f"winrt: {_WINRT_ERR}")
    if _TESS_ERR:
        tips.append(f"pytesseract: {_TESS_ERR}")
    return (
        "[OCR 不可用]\n"
        "方案 A（推荐）：pip install paddleocr paddlepaddle\n"
        "方案 B：pip install winrt-runtime \"winrt.windows.media.ocr\"\n"
        "方案 C：安装 Tesseract-OCR 后 pip install pytesseract\n"
        + "\n".join(tips)
    )


def backend_info() -> str:
    """返回当前使用的 OCR 后端描述，用于 UI 状态栏显示。

    会优先反映**实际运行状态**：即使 paddleocr 装了，只要首次初始化或推理失败过，
    就标成 "PaddleOCR 失败"，让用户知道当前在走降级路径。
    """
    # 版本错位或运行时失败——让用户立刻看到原因
    mismatch = _version_mismatch_hint()
    if mismatch:
        return f"PaddleOCR 版本错位（走 WinRT）· {mismatch.splitlines()[0]}"
    if _PADDLE_RUNTIME_ERR:
        return f"PaddleOCR 失败（走 WinRT）· {_PADDLE_RUNTIME_ERR[:60]}"
    if _PADDLE_OK:
        return "PaddleOCR（含发言人识别）"
    if _WINRT_OCR:
        return "Windows 内置 OCR"
    if _TESS:
        return "Tesseract OCR"
    return "OCR 不可用"
