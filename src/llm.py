"""调用本机 Claude Code CLI（复用其登录态）生成回复。"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from typing import Callable


def _find_claude_cli() -> str:
    """优先 PATH 中的 claude；否则按平台回落到 VSCode 扩展内置二进制。"""
    import sys

    # 1. PATH 中直接有 claude / claude.exe
    path_claude = shutil.which("claude") or shutil.which("claude.exe")
    if path_claude:
        return path_claude

    # 2. 平台特定的 VSCode 扩展路径（版本号通配，取最新）
    if sys.platform == "win32":
        patterns = [
            os.path.expanduser(
                r"~\.vscode\extensions\anthropic.claude-code-*-win32-x64\resources\native-binary\claude.exe"
            ),
            os.path.expanduser(
                r"~\.vscode\extensions\anthropic.claude-code-*-win32-arm64\resources\native-binary\claude.exe"
            ),
            # Claude 桌面应用（Windows 安装程序）
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Claude\claude.exe"),
            os.path.expandvars(r"%APPDATA%\Claude\claude.exe"),
        ]
    else:
        # macOS（arm64 / x64）
        patterns = [
            os.path.expanduser(
                "~/.vscode/extensions/anthropic.claude-code-*-darwin-arm64/resources/native-binary/claude"
            ),
            os.path.expanduser(
                "~/.vscode/extensions/anthropic.claude-code-*-darwin-x64/resources/native-binary/claude"
            ),
        ]

    for pattern in patterns:
        candidates = sorted(glob.glob(pattern))
        if candidates:
            return candidates[-1]

    raise RuntimeError(
        "未找到 claude CLI。\n"
        "请确认 Claude Code 已安装：\n"
        "  • VSCode 扩展（推荐）：在 VSCode 扩展市场搜索 'Claude Code'\n"
        "  • 或 npm 全局安装：npm install -g @anthropic-ai/claude-code"
    )


CLAUDE_BIN = _find_claude_cli()

# Anthropic API 在国内无法直连。优先沿用父进程 HTTPS_PROXY；否则兜底 Clash/Surge 常用端口。
# 想改代理端口，启动前 `export CLAUDE_PROXY=http://127.0.0.1:7897`，或编辑这里。
DEFAULT_PROXY = os.environ.get("CLAUDE_PROXY", "http://127.0.0.1:7890")


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    if not (env.get("HTTPS_PROXY") or env.get("https_proxy")):
        env["HTTPS_PROXY"] = DEFAULT_PROXY
        env["HTTP_PROXY"] = DEFAULT_PROXY
    return env


def generate_reply(prompt: str, timeout: int = 60) -> str:
    """阻塞调用 claude -p，返回纯文本回复。"""
    result = subprocess.run(
        [CLAUDE_BIN, "-p", "--output-format", "text", prompt],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_build_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI 返回非零退出码 {result.returncode}：{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def generate_reply_stream(
    prompt: str,
    on_chunk: Callable[[str], None],
    timeout: int = 90,
) -> str:
    """流式调用：每拿到一个文本 delta 就回调 on_chunk(delta)。返回最终完整文本。

    用 --output-format stream-json + --include-partial-messages，从 stream_event
    里的 content_block_delta 抽 text 字段。失败抛 RuntimeError。
    """
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        prompt,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # 行缓冲
        env=_build_env(),
    )
    chunks: list[str] = []
    try:
        for line in iter(proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "stream_event":
                ev = obj.get("event") or {}
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    text = delta.get("text", "")
                    if text:
                        chunks.append(text)
                        try:
                            on_chunk(text)
                        except Exception:  # noqa: BLE001
                            pass
            elif obj.get("type") == "result":
                break
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("claude 调用超时") from None
    except Exception:
        proc.kill()
        raise

    if proc.returncode not in (None, 0):
        err = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(
            f"claude CLI 返回 {proc.returncode}：{err.strip() or '（无 stderr）'}"
        )

    return "".join(chunks).strip()
