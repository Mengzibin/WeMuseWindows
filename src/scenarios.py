"""意图场景定义 + 一次生成3条候选的 prompt 构建器。

与 styles.py 的区别：
  styles.py  → 语气风格（幽默/严肃/温柔…）
  scenarios.py → 意图场景（委婉拒绝/邀请吃饭/表白…）
面板 UI 使用本模块，更贴近实际聊天意图。
"""
from __future__ import annotations

SCENARIOS: dict[str, str] = {
    "智能建议":    "",   # 让 LLM 自行判断，不加限定
    "开启新话题":  "找一个自然切入点引出新话题，与上文相关但开辟新方向，语气轻松",
    "委婉拒绝":    "温和地拒绝对方的请求，给出简短合理的理由，不显生硬",
    "邀请对方吃饭":"用轻松自然的方式邀请对方一起吃饭，时间/地点可留白",
    "关心对方状况":"询问并表达对对方近况的关心，语气温暖自然",
    "道歉":        "诚恳道歉，态度真诚但不过分卑微，简短有力",
    "委婉表白":    "含蓄地表达喜意，话里有话，给对方留余地",
    "阴阳怪气":    "用反讽手法回复，话里有话，幽默不失礼",
    "生气了":      "表达不满情绪，有情绪感但不失控、不骂人",
    "装忙":        "礼貌表示自己很忙暂时无法详细回复，语气不冷漠",
    "卖个关子":    "故意不说完，勾起对方好奇心，让对方主动追问",
    "撒个娇":      "用俏皮可爱的语气回复，带点小撒娇，不油腻",
}

DEFAULT_SCENARIO = "智能建议"


def build_multi_prompt(
    chat_text: str,
    scenario: str = DEFAULT_SCENARIO,
    extra: str = "",
) -> str:
    """构造让 Claude 一次输出 3 条候选回复的 prompt。

    输出约定：3 行，每行是一条回复正文，没有序号和说明。
    """
    scenario_desc = SCENARIOS.get(scenario, "")
    intent_block = (
        f"\n意图/场景：【{scenario}】——{scenario_desc}"
        if scenario_desc else ""
    )
    extra_block = f"\n额外要求：{extra.strip()}" if extra.strip() else ""

    return f"""你是用户的微信聊天助手。下面是当前聊天记录（按时间从上到下）：

<chat>
{chat_text.strip()}
</chat>

请站在「我」的角度，针对对话中**最后一条消息**，一次性生成 **3 条** 风格略有差异的回复候选。

输出格式（严格遵守）：
- 共 3 行，每行是一条完整回复
- 直接输出消息正文，**不加序号、编号、前缀、解释或分隔线**
- 3 条在语气/长度/角度上有所区分（例如：轻松随意 / 简短直接 / 稍微热情）
- 符合中文微信聊天的口语习惯{intent_block}{extra_block}

不要编造用户没说过的事实。不要输出任何多余内容。"""


def parse_replies(raw: str) -> list[str]:
    """把 Claude 返回的多行文本解析成最多 3 条回复列表。"""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # 过滤掉像 "1." "①" 之类的序号行（以防 LLM 没遵守格式）
    import re
    cleaned = [re.sub(r"^[\d①②③一二三]+[.、．\s]+", "", ln) for ln in lines]
    cleaned = [ln for ln in cleaned if len(ln) >= 2]
    return cleaned[:3]
