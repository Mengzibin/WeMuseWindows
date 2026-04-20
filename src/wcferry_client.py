"""WeChatFerry 客户端封装：DLL 注入到微信进程，提供原生收发接口。

WCF 工作方式：
  pip install wcferry 自带 spy.dll + sdk.dll
  调用 Wcf() 时自动把 spy.dll 注入到 WeChat.exe
  之后所有收发走 RPC（nanomsg），不依赖剪贴板 + 键盘模拟

支持版本：WeChat 3.9.12.51 ↔ wcferry 39.5.x
"""
from __future__ import annotations

import threading
from typing import Any

try:
    from wcferry import Wcf  # type: ignore[import-not-found]
    _AVAIL = True
    _ERR = ""
except Exception as e:  # noqa: BLE001
    _AVAIL = False
    _ERR = str(e)
    Wcf = None  # type: ignore[assignment]


def available() -> bool:
    return _AVAIL


def import_error() -> str:
    return _ERR


class WCFClient:
    """WCF 单例封装。线程安全。"""

    _inst: "WCFClient | None" = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> "WCFClient":
        with cls._lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    def __init__(self) -> None:
        self._wcf: Any = None
        self._started = False
        self._start_err = ""
        self._contacts_cache: list[dict] | None = None
        self._self_wxid: str | None = None

    # ── 生命周期 ────────────────────────────────────────────

    def start(self, debug: bool = False) -> bool:
        """启动 WCF（会注入 DLL 到微信进程）。成功返回 True。"""
        if self._started:
            return True
        if not _AVAIL:
            self._start_err = f"wcferry 未安装: {_ERR}"
            return False
        try:
            self._wcf = Wcf(debug=debug)
            # 启动接收
            try:
                self._wcf.enable_receiving_msg()
            except Exception:  # noqa: BLE001
                pass
            # 拿自己的 wxid
            try:
                info = self._wcf.get_user_info()
                self._self_wxid = info.wxid if hasattr(info, "wxid") else info.get("wxid")
            except Exception:  # noqa: BLE001
                pass
            self._started = True
            print(f"[wcf] 已注入微信 · 自己 wxid = {self._self_wxid}", flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            self._start_err = str(e)
            print(f"[wcf] 启动失败: {e}", flush=True)
            return False

    def stop(self) -> None:
        if self._wcf is not None:
            try:
                self._wcf.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._wcf = None
        self._started = False

    def is_running(self) -> bool:
        if not self._started or self._wcf is None:
            return False
        try:
            return bool(self._wcf.is_login())
        except Exception:  # noqa: BLE001
            return False

    def last_error(self) -> str:
        return self._start_err

    # ── 联系人 ──────────────────────────────────────────────

    def get_contacts(self, refresh: bool = False) -> list[dict]:
        """返回联系人列表 [{wxid, name, remark, ...}]。"""
        if not self.is_running():
            return []
        if self._contacts_cache is not None and not refresh:
            return self._contacts_cache
        try:
            raw = self._wcf.get_contacts()
            out: list[dict] = []
            for c in raw:
                # WCF 返回的字段名在不同版本略有差异
                d = c if isinstance(c, dict) else dict(c.__dict__)
                out.append({
                    "wxid":   d.get("wxid") or d.get("UserName") or "",
                    "name":   d.get("name") or d.get("NickName") or "",
                    "remark": d.get("remark") or d.get("Remark") or "",
                    "type":   d.get("type") or 0,
                })
            self._contacts_cache = out
            return out
        except Exception as e:  # noqa: BLE001
            print(f"[wcf] get_contacts 失败: {e}", flush=True)
            return []

    def find_wxid_by_name(self, name: str) -> str | None:
        """按名字（昵称或备注）模糊匹配联系人 wxid。"""
        if not name:
            return None
        name = name.strip()
        for c in self.get_contacts():
            if c["remark"] == name or c["name"] == name:
                return c["wxid"]
        # 模糊：包含关系
        for c in self.get_contacts():
            if name in (c["remark"] or "") or name in (c["name"] or ""):
                return c["wxid"]
        return None

    # ── 发送 ────────────────────────────────────────────────

    def send_text(self, receiver: str, text: str) -> tuple[bool, str]:
        """直接给 wxid 发消息。"""
        if not self.is_running():
            return False, f"WCF 未运行：{self._start_err or '未启动'}"
        if not receiver:
            return False, "缺少接收者 wxid"
        if not text.strip():
            return False, "消息为空"
        try:
            ret = self._wcf.send_text(msg=text, receiver=receiver)
            if ret == 0:
                return True, "已通过 WCF 发送"
            return False, f"WCF 返回错误码 {ret}"
        except Exception as e:  # noqa: BLE001
            return False, f"WCF 发送异常：{e}"

    def send_text_by_name(self, name: str, text: str) -> tuple[bool, str]:
        """按联系人名字发消息（先查 wxid 再发）。"""
        wxid = self.find_wxid_by_name(name)
        if not wxid:
            return False, f"联系人「{name}」未找到"
        return self.send_text(wxid, text)

    # ── 接收 ────────────────────────────────────────────────

    def pop_new_message(self) -> dict | None:
        """非阻塞取一条新消息；没有则返回 None。"""
        if not self.is_running():
            return None
        try:
            q = getattr(self._wcf, "msgQ", None)
            if q is None or q.empty():
                return None
            msg = q.get_nowait()
            return {
                "id":      getattr(msg, "id", None),
                "type":    getattr(msg, "type", None),
                "sender":  getattr(msg, "sender", ""),
                "roomid":  getattr(msg, "roomid", ""),
                "content": getattr(msg, "content", ""),
                "is_self": getattr(msg, "from_self", False),
                "ts":      getattr(msg, "ts", 0),
            }
        except Exception:  # noqa: BLE001
            return None
