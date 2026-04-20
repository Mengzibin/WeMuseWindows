"""聊天风格预设。每个风格对应一段 system-prompt 片段。"""

STYLES: dict[str, str] = {
    "幽默": "风趣俏皮，带一点机灵小玩笑，但不要油腻，不要强行谐音梗。",
    "严肃": "语气正经、克制、不带情绪词，句子简短，直接回应要点。",
    "认真": "诚恳、就事论事，把关键信息讲清楚，必要时分点陈述。",
    "正式": "使用书面语，礼貌得体，适合工作或长辈场景，避免口语词和表情。",
    "嬉皮笑脸": "轻松调侃、有网感、可适度使用「哈哈」「嘿嘿」「(doge)」一类气口，但不过火。",
    "温柔": "关怀体贴、语气柔和，多用「呀」「呢」「好的哦」等软化词，让对方感觉被在意。",
    "专业": "工作语境下的同事口吻，逻辑清晰、简练，不寒暄废话，必要时给出下一步。",
    "高冷": "简短、留白、不解释过多，一两句带过，保持距离感但不失礼。",
    "暧昧": "含蓄带点小心思，语气轻，有进有退，不直白但留余地。",
    "怼回去": "不客气地反驳或吐槽，占住理但不骂人，句子利落有力。",
}

DEFAULT_STYLE = "认真"


def extract_my_examples(chat_text: str, limit: int = 10) -> list[str]:
    """从标注过发言人的聊天文本里抽取"我说过的话"，做风格模仿的 few-shot 样本。

    只要标记为「我：」的行；过滤长度 < 3 的（大多是 "嗯" "啊" 这种虚词，不带风格信息）。
    """
    out: list[str] = []
    for line in chat_text.splitlines():
        line = line.strip()
        if not (line.startswith("我：") or line.startswith("我:")):
            continue
        msg = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        if len(msg) >= 3:
            out.append(msg)
    # 只要最近的 N 条
    return out[-limit:]


def opponent_length_since_my_last(chat_text: str) -> int:
    """统计"上一轮我发言之后，对方一共发了多少字"。

    用于让助手生成的回复长度跟对方的发言总量对齐——比如对方连发 3 条 40 字的
    消息，对方总长 120 字，就让我的回复也在这个量级。

    规则：
    - 从聊天尾部倒着扫，碰到「我：」就停
    - 中间所有「对方：」行的正文（去掉前缀、去掉时间戳行）累加字数
    - 全都是"对方：..."（没见过「我：」）也照样全累加
    - 没有任何「对方：」返回 0
    """
    lines = chat_text.splitlines()
    total = 0
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("我：") or s.startswith("我:"):
            break
        if s.startswith("──【") or s.startswith("──["):
            continue  # 时间分隔条，跳过
        if s.startswith("对方：") or s.startswith("对方:"):
            msg = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            total += len(msg)
    return total


# 多条回复用这个分隔符串起来输出；选它是因为聊天里几乎不可能出现裸的 <MSG>
MSG_DELIMITER = "<MSG>"


def split_replies(reply: str) -> list[str]:
    """把 LLM 输出按 <MSG> 拆成多条消息，过滤空串。

    如果模型没按规则吐分隔符（只给一整段），就按段落（空行）做次选切分；
    还是拆不出就整段作为单条返回。
    """
    reply = (reply or "").strip()
    if not reply:
        return []
    if MSG_DELIMITER in reply:
        parts = [p.strip() for p in reply.split(MSG_DELIMITER)]
    else:
        # 次选：按空行切（段落分隔）
        parts = [p.strip() for p in reply.split("\n\n")]
    parts = [p for p in parts if p]
    return parts or [reply]


def build_prompt(
    chat_text: str,
    style: str,
    extra_instruction: str = "",
    mimic_user: bool = False,
    target_length: int = 0,
    reply_count: int = 1,
) -> str:
    """拼接最终送给 Claude 的 prompt。

    mimic_user=True 时，会把聊天里「我：…」的历史作为 few-shot 塞进去，让生成尽量贴近用户本人的说话习惯。
    target_length>0 时，覆盖默认的"匹配最后一条"规则，直接要求总长接近该值。
    reply_count>1 时，要求产出 N 条独立消息，用 <MSG> 分隔。
    """
    style_desc = STYLES.get(style, style)
    extra = f"\n额外要求：{extra_instruction}" if extra_instruction.strip() else ""

    mimic_block = ""
    if mimic_user:
        examples = extract_my_examples(chat_text)
        if examples:
            bullets = "\n".join(f"  - {e}" for e in examples)
            mimic_block = (
                "\n\n以下是「我」在这段对话里已经发过的消息。请仔细观察用词、长度、语气、习惯表达，"
                "让生成的回复在满足风格要求的前提下，尽量贴近【我】本人的说话方式：\n"
                f"{bullets}\n"
            )

    # 规则 2：长度——目标长度优先，否则走默认的"对齐最后一条"逻辑
    reply_count = max(1, int(reply_count or 1))
    if target_length and target_length > 0:
        if reply_count > 1:
            per = max(3, target_length // reply_count)
            length_rule = (
                f"2. 总回复长度约 {target_length} 字（允许 ±25%），分成 {reply_count} 条消息，"
                f"**每条约 {per} 字**。别把一条长话硬切成几段，每条要能独立成立。"
            )
        else:
            length_rule = (
                f"2. 回复长度控制在 {target_length} 字左右（允许 ±25%）。"
                f"这是根据上轮我发言后对方累计字数算出来的，用来和对方"
                f"的表达量对齐——别显得敷衍，也别冗长。"
            )
    else:
        if reply_count > 1:
            length_rule = (
                f"2. 生成 {reply_count} 条独立消息，每条长度与对话里最后一条大致匹配，"
                f"**不要**把一条长消息切成几段。"
            )
        else:
            length_rule = (
                "2. 长度要和\"对话里最后一条消息\"的长度**大致匹配**——对方 5 字别回 50 字，"
                "我刚发了一长段别只续两个字。允许 ±30% 浮动。"
            )

    # 多条消息的输出格式规则
    multi_rule = ""
    if reply_count > 1:
        multi_rule = (
            f"\n6. 输出 {reply_count} 条消息时，**每两条之间用一个 `{MSG_DELIMITER}` 分隔**，"
            f"前后不加空行也不加别的标点。示例（3 条）：\n"
            f"   第一句{MSG_DELIMITER}第二句{MSG_DELIMITER}第三句\n"
            f"   每条消息都要自成一句话，不要彼此重复或简单续写。"
        )

    return f"""你是用户的微信聊天助手。下面是用户当前聊天窗口里识别到的对话内容，按时间从上到下排列。
每行格式约定：
- 以「我：」开头 → 用户本人发出的消息（绿色气泡）
- 以「对方：」开头 → 聊天对方发来的消息（白/灰气泡）
- 以「──【HH:MM】──」或「──【昨天 12:30】──」这样的格式 → 时间分隔条，用于帮助你判断消息的时间跨度（对方隔了多久才回、是不是隔夜话题）；**不要在回复里提这些标记**

<chat>
{chat_text.strip()}
</chat>{mimic_block}

请站在「我」的角度，生成下一条要发出的消息。**根据对话里最后一行的发言人判断任务类型**：

- 如果最后一行是「对方：」→ 这是一次**回复**，直接针对那句「对方：」的内容作答。
- 如果最后一行是「我：」→ 这是一次**续接**，沿着我刚刚发出的那句话再自然地补一句（补理由 / 抛新问题 / 缓和语气 / 推进话题），**绝对不要跳过我的话去回答对方之前的消息**，也不要和我刚才那句语义重复。

风格要求：【{style}】——{style_desc}{extra}

硬性规则：
1. 只输出消息正文本身，不要加「回复：」「我：」之类的前缀，不要加引号，不要解释。
{length_rule}
3. 符合中文微信聊天习惯：偏口语，除非必要不分段、不列要点。
4. 续接时，新消息要承接上一句的语气和话题，有新增信息或推进，不要单纯重复。
5. 不要编造用户没说过的事实（时间、地点、承诺、人名等）。{multi_rule}
"""
