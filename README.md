# 微信聊天助手 · WeMuse

> 中文名：**微信聊天助手** ｜ 英文名：**WeMuse**（WeChat + Muse）

macOS 下的 AI 微信回复助手，自动读取当前聊天对话 → 按选定风格生成回复 → 一键发送。**不注入微信、不 hook、不会被封号**。

---

## 关于名字

项目中文名是**微信聊天助手**，对外（英文语境、代码仓库、GitHub 标题等场合）称作 **WeMuse** —— **We**Chat + **Muse**（缪斯，古希腊神话中的灵感之神）。

缪斯在传说里专司给诗人、乐师、工匠投递灵感——这个应用做的事情正是同一件：你在微信里有话要说但一时没有合适的措辞时，WeMuse 用你当下语境 + 你的说话风格写出一段贴合的回复给你挑。既是"替你写"，也是"点你一下"。

> 日常界面、菜单栏、macOS Spotlight 都用中文「微信聊天助手」—— WeMuse 只是这个项目的英文代号，不影响使用。

## 目录

- [关于名字](#关于名字)
- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [运行要求](#运行要求)
- [快速上手](#快速上手)
- [使用方式](#使用方式)
- [必需权限](#必需权限)
- [项目结构](#项目结构)
- [架构选型与技术要点](#架构选型与技术要点)
- [已知限制](#已知限制)
- [排错](#排错)
- [Roadmap](#roadmap)
- [致谢 · Built With](#致谢--built-with)
- [许可协议 · License](#许可协议--license)

---

## 核心特性

| 能力 | 说明 |
|------|------|
| **Accessibility 直读聊天** | 用 macOS 官方辅助功能 API 直接读取当前微信窗口的消息内容，精度 100%，不依赖 OCR |
| **多轮翻页自动扩展上下文** | 按下 PageUp 触发微信懒加载旧消息，合并 2-3 轮结果后去重，典型一次读到 30-60 条历史 |
| **说话人 + 时间戳感知** | 输出 `我：…` / `对方：…` / `──【14:55】──` 的清洁格式，Claude 能判断回复对象和消息时间跨度 |
| **OCR 兜底** | 万一 Accessibility 在特殊界面读不到，可以手动框选截图走 Apple Vision OCR |
| **10 种风格预设** | 幽默 / 严肃 / 认真 / 正式 / 嬉皮笑脸 / 温柔 / 专业 / 高冷 / 暧昧 / 怼回去，点击切换 |
| **多风格并行生成** | 一次并发 3 种风格让你挑（通常 5-6 秒全部出来，比串行快 ~3 倍） |
| **模仿用户说话风格** | 把历史里「我：…」作为 few-shot，生成贴近你本人的语气 / 用词 / 长度 |
| **流式输出** | Claude 边想边显示，长回复不再傻等 |
| **自动发送** | 生成完自动切到微信、粘贴、回车——按一次热键消息就飞出去 |
| **全局热键** | `⌘⇧R` / `⌘⇧G` / `⌘⇧A` 在任何 app 里都可用 |
| **菜单栏常驻** | 顶部 💬 图标，关窗不退出；像原生插件一样轻 |
| **复用 Claude Code 账号** | 自动探测 VSCode 扩展内置的 `claude` CLI，**零 API Key、零配置**，走你订阅的默认模型 |

## 工作原理

```
┌──────────────┐     读取      ┌───────────────────────┐
│   微信 Mac 版 │ ────────────▶ │  macOS Accessibility  │
└──────┬───────┘                │      API (pyobjc)     │
       │                        └──────────┬────────────┘
       │                                   │  消息列表 (带说话人 + 时间)
       │ (粘贴 + 回车)                      ▼
       │                        ┌──────────────────────┐
       │                        │  build_prompt(风格)   │
       │                        └──────────┬───────────┘
       │                                   │
       │                                   ▼
       │                        ┌──────────────────────┐
       │                        │  claude -p (流式)    │ ← 复用 Claude Code CLI 登录态
       │                        └──────────┬───────────┘
       │                                   │
       │           CGEvent + 剪贴板        ▼
       └─────◀────────────────── 生成的回复
```

整体是一个 **macOS 本地 Python 应用**（Tkinter + pyobjc），**完全不触碰微信进程本身**：

- 读消息：通过 macOS Accessibility 查询 WeChat.app 暴露的 UI 元素树
- 写消息：通过系统级 CGEvent 发送 `⌘V` + 回车
- 生成：通过 `subprocess` 调本机的 `claude` CLI（VSCode 扩展内置二进制）

所以你的微信客户端是**原版未修改**的，账号和 Tencent 安全策略都认这是"用户自己在用键盘"。

## 运行要求

- **macOS 11+**（开发和测试在 macOS 26 Tahoe 上进行）
- **Python 3.11+**（系统自带的 `python3` 即可）
- **VSCode + Claude Code 扩展**已安装并登录 Anthropic 账号
  - 本项目会自动去 `~/.vscode/extensions/anthropic.claude-code-*` 找内置的 CLI，无需单独装 `claude`
- **海外网络**（`api.anthropic.com` 在大陆需要代理；项目默认走 `127.0.0.1:7890`）
- **微信 Mac 版**已登录（会话列表可见 + 在某个聊天界面）

## 快速上手

### 1. 克隆或下载本仓库

```bash
cd ~/Documents/hkust/program/wechat  # 你当前的位置
```

### 2. 首次启动（从终端）

```bash
./run.sh
```

首次运行会自动：
1. 创建 `.venv/` 虚拟环境
2. `pip install -r requirements.txt`（装 `ocrmac / pyperclip / pynput / Pillow / pyobjc-*`）
3. 启动主程序

之后 macOS 会依次弹出 3 个权限请求——**全部点"允许"**：

| 权限 | 用途 |
|------|------|
| 辅助功能 | 读取 WeChat 的 UI 树 + 监听全局热键 |
| 屏幕录制 | OCR 截图（只有用 `📸 截图 OCR` 功能时才需要） |
| 自动化（控制系统事件） | 激活自己 / 激活微信 |

### 3. 打包成独立 .app（可选，推荐）

```bash
./build_app.sh
mv build/微信聊天助手.app ~/Applications/
```

完成后：

- ⌘ + 空格 搜「微信聊天助手」一键启动
- 可设置开机自启（`系统设置 → 通用 → 登录项 → 添加微信聊天助手.app`）
- 窗口右上角红叉 = 隐藏到菜单栏；彻底退出走菜单栏 💬 → 退出

注意：**辅助功能权限是绑定二进制的**。从终端 `./run.sh` 授权的对象是 Terminal；从 `.app` 启动需要**重新**给 `微信聊天助手.app` 授权一次。

## 使用方式

### 全局热键（任何 app 内都生效）

| 热键 | 功能 |
|------|------|
| `⌘ ⇧ R` | 显示 / 隐藏助手面板 |
| `⌘ ⇧ G` | 截图 OCR → 生成回复（兜底用） |
| `⌘ ⇧ A` | Accessibility 读取微信 → 生成回复（主流程） |

### 标准工作流（推荐）

1. 在微信里打开某个联系人的聊天界面
2. 按 `⌘ ⇧ A`（或点面板里的「📥 读取微信」）
   - 微信会轻微地往上滚动 2 次（触发懒加载）
   - 对话内容填入「对话内容」文本框
3. 选一个风格（幽默 / 温柔 / 认真 / …），可选填「额外要求」
4. 点「✨ 生成回复」或「🎲 对比 3 种风格」
5. 结果显示在下方，**自动复制到剪贴板**
6. 要发就按「📤 发送 (回车)」，或自己 ⌘V 粘贴到微信

### 高级选项

- **换风格自动重生成**（默认开）：选中回复后再点其它风格，立刻用新风格重跑
- **自动发送**（默认关）：勾上后生成完直接粘贴 + 回车到微信，按一次热键消息就飞出去
- **模仿我的说话风格**（默认开）：用历史「我：…」作为 few-shot，让生成贴近你本人
- **🔍 查看 Prompt**：弹窗显示上次发给 Claude 的完整 prompt 和模型信息

### 菜单栏图标 💬

快捷入口：

- 📥 显示面板
- ✨ Accessibility 读 + 生成（等同 `⌘⇧A`）
- 📸 截图 OCR 生成（等同 `⌘⇧G`）
- 👋 退出（真正终止进程；红叉关窗只是隐藏）

## 必需权限

首次用每个功能时 macOS 会弹窗询问，也可以手动去 `系统设置 → 隐私与安全性`：

### 辅助功能（Accessibility）

授权对象：
- 从 Terminal 跑 → `Terminal.app`
- 从 `微信聊天助手.app` 跑 → `微信聊天助手.app`
- 从 VSCode 跑 → `Code.app`

用途：
- 读取微信消息的 UI 树
- 接收全局热键
- 激活自己的窗口到前台

### 屏幕录制（Screen Recording）

仅当你使用「📸 截图 OCR」功能时需要。Accessibility 路径不需要。

### 自动化（Automation）

首次调用 `osascript` 时弹窗："Python 想要控制 系统事件"，允许即可。

## 项目结构

```
wechat/
├── README.md                    ← 你正在看
├── requirements.txt             ← pip 依赖
├── run.sh                       ← 开发模式启动（venv + python -m src.main）
├── build_app.sh                 ← 打包 .app 壳脚本
├── build/                       ← 打包产物（不入版本控制）
│   └── 微信聊天助手.app/
└── src/
    ├── __init__.py
    ├── main.py                  ← 入口；注册热键、菜单栏、启动 UI
    ├── ui.py                    ← Tkinter 主窗口（680×820 固定尺寸）
    ├── styles.py                ← 10 种风格预设 + prompt 组装 + few-shot 提取
    ├── llm.py                   ← 自动探测 claude CLI，提供阻塞和流式两种调用
    ├── accessibility.py         ← 核心：通过 macOS AX API 读微信消息
    ├── ocr.py                   ← Apple Vision OCR（兜底路径）
    ├── capture.py               ← 框选截图（screencapture -i）
    ├── sender.py                ← 自动粘贴 + 回车到微信输入框
    ├── hotkey.py                ← Cocoa NSEvent 全局热键（避开 pynput 线程陷阱）
    └── menubar.py               ← NSStatusItem 菜单栏图标
```

## 架构选型与技术要点

### 1. 为什么不做真正的 WeChat 插件？

- macOS 历史上有 `WeChatPlugin-MacOS`（TKkk-iOSer），**2019 年被腾讯法务下架**
- 现代 macOS 的 SIP / Hardened Runtime / Library Validation 拦截未签名 dylib 注入
- 每次微信小更新都会打破 hook，维护成本高
- WeChat 登录时校验客户端完整性，注入有**封号风险**

本项目用 Accessibility 官方 API，**零封号风险 + 不怕微信更新**。

### 2. Claude Code 账号复用

`src/llm.py` 自动探测：

```python
~/.vscode/extensions/anthropic.claude-code-*-darwin-arm64/resources/native-binary/claude
```

子进程调 `claude -p --output-format stream-json --verbose --include-partial-messages <prompt>`，解析 JSON 流获取文本增量。完全复用你 VSCode 里登录的 Claude Code 会话态，**零 API Key**。模型由你的 Anthropic 订阅决定（Pro/Max 默认 Sonnet/Opus）。

### 3. macOS 网络代理坑

GUI 启动的进程不继承 shell 的 `HTTPS_PROXY` 环境变量。`src/llm.py` 里做了兜底：默认自动加 `HTTPS_PROXY=http://127.0.0.1:7890`（Clash/Surge 常用端口）。想改端口：

```bash
export CLAUDE_PROXY=http://127.0.0.1:7897
```

或直接编辑 `src/llm.py` 开头的 `DEFAULT_PROXY` 常量。

### 4. Accessibility 读取的三层过滤

1. **定位聊天区子树**：遍历窗口的所有 `AXScrollArea`，挑中心点在右 60% 且面积最大的那个
2. **内容过滤**：噪音词集合（"折叠置顶聊天"、"搜索"、…）+ 时间戳正则 + 贴纸正则 + 最小长度 2
3. **发言人解析**：微信 AX 把消息写成 `"我说:xxx"` / `"葛诗霖说:xxx"`，正则抽前缀 → `我` / `对方`，比 x 坐标 100% 准

### 5. 多轮翻页触发懒加载

```
pass 0: 读当前视图                            → M10~M15
pass 1: 发 PageUp → 等 0.7s → 读            → M5~M12（有重叠）
pass 2: 发 PageUp → 等 0.7s → 读            → M1~M8（有重叠）
合并去重 + 按首次出现顺序                    → M1 … M15
最后发 End 键滚回底部
```

### 6. macOS 26 (Tahoe) 的 TSM 线程陷阱 —— 本项目踩过两次

**问题**：macOS 新版本的 Text Services Manager API 加了主线程断言，**任何从非主线程调 TSM 都会 SIGTRAP 杀进程**（无法用 `try/except` 拦截）。

**触发源 1**：`pynput.GlobalHotKeys` 监听器在子线程查键盘布局 → 按热键就崩
- **解决**：改用 `NSEvent.addGlobalMonitorForEventsMatchingMask_handler_`（`src/hotkey.py`），主线程监听

**触发源 2**：`pynput.Controller.press()` 在 `read_wechat_multi_pass` 的子线程里查 TSM → 点「读取微信」就崩
- **解决**：改用 Quartz `CGEventCreateKeyboardEvent + CGEventPost`（`src/accessibility.py` 的 `_post_key_tap`），底层 API 不碰 TSM

### 7. "固定窗口尺寸 + 所有控件放得下" 的布局

窗口 **720×820 不可调**（`root.resizable(False, False)`），垂直分 8 行：

1. 顶部按钮 + 置顶 + 状态
2. 截图预览卡片
3. 对话内容（带滚动条）
4. 风格 5×2 网格
5. 额外要求输入
6. 生成按钮
7. 选项 checkbox
8. 结果 Notebook（"建议回复" / "🎲 3 候选对比" 两 tab 互斥显示）

### 8. macOS 首次点击不响应的修复

- `_activate_app` 用 **pyobjc 同步调用** `NSApplication.sharedApplication().activateIgnoringOtherApps_(True)`（不是 osascript 异步）
- 绑定 `<FocusIn>` 只在 `event.widget is self.root` 时激活，**不抢子控件的键盘焦点**（否则 Entry 输入不了字）

## 已知限制

- **只支持 macOS 版微信**。Windows 版需要走 WeChatFerry（本项目未集成）
- **不支持多设备同步**。助手跑在哪台 Mac，就只能读那台 Mac 上微信客户端里的消息
- **iPhone 上无法使用**。Apple 沙箱限制第三方 app 读取微信内容
- **微信"引用"消息的格式尚未专门解析**，会作为普通文本直通。遇到请贴日志样本，我再加规则
- **流式输出只对单风格生成生效**。多风格并行走的是阻塞调用
- **Cloudflare 拉黑**：如果你的 VPN 节点被 Anthropic 的 Cloudflare 拉黑，`claude -p` 会返回 403。换节点即可
- **消息格式依赖 WeChat Mac 客户端的 AX 暴露**。如果腾讯某次更新改变了暴露结构，需要重新调 `accessibility.py` 的过滤规则

## 排错

所有诊断都写到 `~/Library/Logs/wechat-assistant.log`（从 `.app` 启动时）或终端（从 `./run.sh` 启动时）。

### 启动时闪退 / "意外退出"

99% 是 TSM 线程陷阱。看 `~/Library/Logs/DiagnosticReports/python3.11-*.ips`，看崩溃调用栈里有没有 `TSM` / `_dispatch_assert_queue_fail`。已知的两处都已修复；如果遇到新的，把 `.ips` 贴出来即可定位。

### 点「📥 读取微信」读不到内容

查日志里的 `[AX]` 开头行：

```
[AX] trusted=True       ← False 说明辅助功能没授权给当前启动者
[AX] wechat_pid=68068   ← None 说明没检测到微信进程
[AX] roles in subtree: {'AXUnknown': 33, 'AXTable': 1}
[AX] raw items = 34, filtered = 31
```

- `trusted=False` → 去系统设置重新授权
- `wechat_pid=None` → 微信没开 / 没登录
- `raw=N, filtered=0` → 过滤规则太严，贴 `raw role=... text=...` 出来让我调

### 生成时 "claude CLI 返回 403"

出口 IP 被 Anthropic Cloudflare 拉黑。换 VPN 节点（优先美西美中，避开日本 / 香港共享节点）。

### 生成时 "Not logged in"

从 `.app` 启动时代理环境变量没继承导致；`src/llm.py` 已有兜底常量 `DEFAULT_PROXY = "http://127.0.0.1:7890"`，如果你的代理端口不是 7890，编辑这一行。

### "自动发送" 发到了错误窗口

`send_to_wechat` 会先激活 WeChat 再粘贴。如果激活失败或微信焦点在错误的聊天里，消息会发到错地方。**首次使用建议勾「📝 只粘贴不回车」**，确认内容+目标对话都对，再改回「自动发送」。

### 菜单栏 💬 图标没出现

看日志里是否有 `⚠ 菜单栏不可用：...`。多半是 pyobjc-framework-Cocoa 没装上 —— 重跑 `./run.sh` 会自动补装。

## Roadmap

按优先级：

- [ ] 生成过程中可中断（kill 当前流式 Popen）
- [ ] 配置持久化（记住上次选的风格 / 额外要求 / 勾选状态）
- [ ] 发送前的 "即将发给 XXX，3 秒取消" 小浮窗，防误发
- [ ] 解析微信"引用"消息的格式
- [ ] 多显示器坐标系的更稳健处理
- [ ] Windows 版支持（迁移到 WeChatFerry 的技术路径，架构需要较大重构）

---

## 致谢 · Built With

本项目所有代码在 **[Claude Code](https://claude.com/claude-code)** 结对辅助下完成——一个非常好用的 AI 编程伙伴（Anthropic 出品）。它帮助完成了从架构讨论、踩坑定位（macOS TSM 线程陷阱、Accessibility 子树过滤、流式输出解析等）到实际代码落地的大部分工作，特此声明。

运行期间，应用调用你本机已登录的 **Claude Code CLI**（VSCode 扩展内置二进制）来生成微信回复——**模型复用你自己的 Anthropic 账号订阅**（通常是 Claude Sonnet 或 Opus）。应用本身**不包含任何 API key**，也不会收集你的对话数据。

## 许可协议 · License

本项目采用**非商业使用许可证**，完整条款见 [LICENSE](LICENSE)。

**简要版**：

✅ **可以**：
- 个人使用
- 学习 / 科研 / 教学
- 修改 / 再分发（保留本许可声明）
- 在社交平台分享使用体验

❌ **不可以**：
- 商业销售或转售
- 用作付费服务的一部分（SaaS、咨询等）
- 集成到商业产品中
- 批量骚扰、自动营销、身份冒充
- 任何违反微信服务条款或当地法律的行为

如需商业授权请联系版权持有者。

---

**Disclaimer**: 本项目通过 Apple 公开的 Accessibility API 与 macOS 版微信客户端交互，**不修改微信二进制、不注入代码、不逆向协议**。但使用本工具是否符合腾讯《微信软件许可及服务协议》由用户自行判断与承担责任。作者对因使用本工具导致的任何账号问题、数据丢失或其他后果不承担任何责任。
