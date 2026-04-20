# 微信聊天助手 · WeMuse

> 中文名：**微信聊天助手** ｜ 英文名：**WeMuse**（WeChat + Muse）

Windows 下的 AI 微信回复助手：**侧栏面板自动贴在微信主窗口右侧** → 读取当前聊天对话 → 按选定风格生成回复 → 一键发送。**不注入微信、不 hook、不会被封号**（可选的 WeChatFerry 通道默认关闭，不主动启用）。

---

## 关于名字

项目中文名是**微信聊天助手**，对外称 **WeMuse** —— **We**Chat + **Muse**（缪斯，古希腊神话里的灵感之神）。

缪斯专司给诗人、乐师、工匠投递灵感——这个应用做的事正是同一件：你在微信里一时没合适措辞时，WeMuse 用你当下的对话上下文 + 你的说话风格写出一段贴合的回复给你挑。既是"替你写"，也是"点你一下"。

## 目录

- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [运行要求](#运行要求)
- [快速上手](#快速上手)
- [使用方式](#使用方式)
- [项目结构](#项目结构)
- [架构选型与技术要点](#架构选型与技术要点)
- [已知限制](#已知限制)
- [排错](#排错)
- [Roadmap](#roadmap)
- [许可协议](#许可协议--license)

---

## 核心特性

| 能力 | 说明 |
|------|------|
| **自动贴靠微信右侧** | 面板通过 Win32 Owner 关系（`GWLP_HWNDPARENT`）跟随微信主窗口移动、最小化、还原，不覆盖聊天区 |
| **UIA 直读聊天** | 通过 Windows UI Automation 读取 `WeChatMainWndForPC` 主窗口的消息树，不依赖 OCR |
| **按气泡颜色判定发言人** | 对窗口截屏，采样每条消息 bbox 四角像素：绿色气泡（#95EC69）→ 我；白色 → 对方；比 x 坐标更可靠 |
| **多轮翻页扩上下文** | 直接向微信主 HWND 投递 `WM_MOUSEWHEEL` 触发懒加载，逐刻度滚动、读取顶部锚点后再决定下一步，合并多轮结果去重，一次典型能读到 30-60 条历史 |
| **Windows 内置 OCR 兜底** | 截图 → `Windows.Media.Ocr`（WinRT），无需额外下载模型，支持中英文 |
| **10 种风格预设** | 幽默 / 严肃 / 认真 / 正式 / 嬉皮笑脸 / 温柔 / 专业 / 高冷 / 暧昧 / 怼回去 |
| **多风格并行生成** | 3 种风格并发出，5-6 秒全部就绪 |
| **模仿用户说话风格** | 把历史"我：…"作为 few-shot，生成贴近你本人的语气 / 用词 / 长度 |
| **流式输出** | Claude 边想边显示 |
| **WCF 可选直发通道** | 默认关闭；用户手动点"启用 WCF"后注入 spy.dll，走 WeChatFerry 发送、读联系人列表（有闪退风险，需匹配微信 3.19.12.51 原生 3.x 版本） |
| **剪贴板 + 键盘模拟兜底** | 未启用 WCF 时走传统通道：`pyperclip` 拷贝 + `pynput` `Ctrl+V` + 回车 |
| **全局热键** | `Ctrl+Shift+W` 显示/隐藏 · `Ctrl+Shift+G` 立即生成 |
| **系统托盘常驻** | 关窗不退出 |
| **复用 Claude Code 账号** | 自动探测 VSCode 扩展内置的 `claude.exe`，**零 API Key** |

## 工作原理

```
┌────────────────────┐     UIA 读        ┌─────────────────────┐
│  微信 Windows 版    │ ───────────────▶ │  uiautomation       │
│ (3.19.12.51, x64)  │                  │  (IAccessible2)     │
└─────────┬──────────┘                  └──────────┬──────────┘
          │                                        │  消息 + bbox
          │                                        ▼
          │   Win32 Owner 关系                ┌──────────────┐
          │   (GWLP_HWNDPARENT)        ┌────▶│ 按气泡颜色判  │
          │   面板贴右侧, 跟随位置       │     │ 我 / 对方     │
          ├──────────────────────────┐  │     └──────┬───────┘
          │                          │  │            │
          │                          ▼  │            ▼
          │                   ┌──────────┴──────┐   ┌──────────────┐
          │                   │ Tkinter Panel   │──▶│ build_prompt │
          │                   │ (右侧 380px 条)  │   └──────┬───────┘
          │                   └──────┬──────────┘          │
          │   WCF DLL 注入（可选）      │                   ▼
          │   / pyperclip + pynput    │           ┌──────────────┐
          └──────◀─────────回复────────┘           │ claude.exe   │
                                                  │ (stream-json) │
                                                  └──────────────┘
```

**整体是一个 Windows 本地 Python 应用**（Tkinter + pywin32 + uiautomation）。默认通道完全不触碰微信进程本身：读消息走 UIA 公开 API、写消息走剪贴板+键盘模拟。只有当用户手动点"启用 WCF"时，才会把 `spy.dll` 注入微信进程走更高效的消息通道。

## 运行要求

- **Windows 10 / 11**（x64）
- **Python 3.9+**（项目开发用 conda env `cpu` + Python 3.9）
- **VSCode + Claude Code 扩展**已安装并登录 Anthropic 账号
  - 自动探测 `~\.vscode\extensions\anthropic.claude-code-*-win32-x64\resources\native-binary\claude.exe`
- **海外网络**（项目默认走 `127.0.0.1:7890`，可通过 `CLAUDE_PROXY` 环境变量改）
- **微信 Windows 版已登录**（推荐 **3.19.12.51 原生 3.x 版本**，非 CEF / 非 Qt，其他版本 WCF 不兼容）

## 快速上手

### 1. 装依赖

```cmd
conda activate cpu
pip install -r requirements.txt
```

或者直接：

```cmd
setup.bat
```

核心依赖：
- `uiautomation` — UIA 读微信
- `pywin32` — Owner 关系 / SetWindowPos / 窗口枚举
- `mss` + `Pillow` — 截屏 + 气泡取色
- `winrt-runtime` + 几个 winrt 子模块 — 内置 OCR
- `pyperclip` + `pynput` — 兜底发送
- `wcferry`（可选）— 直发通道，默认不启用

### 2. 启动

```cmd
run_panel.bat
```

脚本会找到 conda `cpu` 环境，起 `python -m src.main_panel`。日志落在 `%TEMP%\wemuse_panel.log`。

### 3. 使用

1. 打开微信，登录进入任意聊天界面
2. 面板会**自动贴在微信右侧外部**（如果屏幕放不下会叠在微信右边缘内部）
3. 点 **📥 读取微信** → 当前对话按"我 / 对方"格式填进对话框
4. 选风格、可选填额外要求
5. 点 **✨ 生成** 或 **🎲 3 风格对比**
6. 点 **📤 发送** 或 **📝 只粘贴** 把回复推到微信输入框

## 使用方式

### 面板位置逻辑

启动后面板自动定位：

- **优先**：紧贴微信窗口**外部右边缘**，高度与微信等高
- **屏幕右边放不下**：叠在微信**内部右边缘**（覆盖聊天区最右 380 像素）
- **永远在右侧**，不会跑到左边

面板支持**拖动**——拖动后自动停止跟随（你自己摆到哪就定在哪）。

### 全局热键

| 热键 | 功能 |
|------|------|
| `Ctrl + Shift + W` | 显示 / 隐藏面板 |
| `Ctrl + Shift + G` | 立即生成回复（用当前对话框内容）|

### 可选选项

- **换风格自动重生成**（默认开）
- **自动发送**（默认关；勾上后生成完直接发回微信）
- **模仿我**（默认开；用历史"我：…"做 few-shot）

### WeChatFerry（可选，有风险）

面板顶部有 **WCF 启用按钮**，默认未启用。启用流程：

1. 点"启用"按钮
2. 弹窗确认（有闪退风险提示）
3. 同意后注入 `spy.dll` 到微信进程

启用后：
- 联系人下拉框会被填充
- 发送走 WCF 原生通道（不抢焦点、快）
- 失败时自动回退到剪贴板+键盘模拟

**闪退常见原因**：
- 微信版本不是 3.19.12.51 精确版本（原生 3.x，不是 Qt/CEF 壳）
- Windows Defender / 其它 AV 拦截了 DLL 注入
- 微信某次自动更新了 exe（要锁住版本）

**建议**：日常使用无需启用 WCF，剪贴板+键盘通道足够可靠。

## 项目结构

```
WeMuse/
├── README.md                   ← 你正在看
├── LICENSE
├── requirements.txt            ← pip 依赖
├── run_panel.bat               ← 启动（找 conda cpu + 运行主入口）
├── setup.bat                   ← 一键装依赖
└── src/
    ├── __init__.py
    ├── main_panel.py           ← 入口：起 UI、注册热键、托盘
    ├── panel_ui.py             ← Tkinter 380px 侧栏；所有按钮/输入/状态
    ├── window_tracker.py       ← 后台轮询微信主窗口 rect 变化；算出面板贴右侧的 geometry
    ├── wechat_embed.py         ← Win32 Owner 关系（GWLP_HWNDPARENT）+ SetWindowPos 移动窗口
    ├── accessibility.py        ← UIA 读微信 + 气泡颜色判定发言人 + 多轮翻页
    ├── capture.py              ← mss 全屏截图 + Tk Toplevel 框选
    ├── ocr.py                  ← Windows.Media.Ocr（WinRT），兜底 pytesseract
    ├── sender.py               ← WCF 直发 → 剪贴板+键盘兜底
    ├── wcferry_client.py       ← WeChatFerry 封装（单例，手动启用）
    ├── styles.py               ← 10 种风格预设 + prompt 组装
    ├── llm.py                  ← 自动探测 claude.exe，提供阻塞+流式调用
    ├── hotkey.py               ← keyboard 库全局热键
    └── tray.py                 ← pystray 系统托盘
```

## 架构选型与技术要点

### 1. 为什么选 Owner 关系而不是 SetParent？

早期实现用 `SetParent` 把面板塞进微信的 child window 列表——**直接导致微信闪退**。

原因：微信自己内部的布局/重绘/消息路由代码在遍历子窗口时遇到外部控件（跨进程、非预期类名）会走到错误分支。

现在用 `SetWindowLongPtrW(GWLP_HWNDPARENT, owner)`：
- 面板仍是顶层窗口（`WS_POPUP`），**不是**微信的 child
- 但会**跟随**微信最小化/还原
- Z-order 永远在微信之上（不会被微信盖住）
- 微信的 `EnumChildWindows` **看不到**我们——规避闪退

### 2. 为什么拿顶层 HWND 要用 `GetAncestor(GA_ROOT)`？

Tkinter 的 `root.winfo_id()` 在 Windows 上返回的是**内部子 HWND**（Tk 包装器），不是真正的顶层窗口。`set_owner` 和 `SetWindowPos` 作用在这个子 HWND 上只会影响内部控件，不影响面板整体。

修复：`GetAncestor(winfo_id(), GA_ROOT=2)` 向上找到真正的顶层窗口 HWND。

### 3. 为什么面板位置要用 SetWindowPos？

Tk 的 `root.geometry("380x761+1711+351")` 在 `overrideredirect(True)` + 有 Owner 的窗口上，**坐标部分会被忽略**（只吃尺寸）——一个 Tkinter 在 Windows 上的已知坑。

解决：先 `root.geometry()` 设尺寸，再用 Win32 `SetWindowPos` 直接改坐标。

### 4. UIA 读取的三层过滤

1. **窗口定位**：严格要求 `ClassName == "WeChatMainWndForPC"` 且尺寸 ≥ 600×400。这一步排除微信的**桌面通知弹窗**（也叫"微信"，但只有 222×114，之前会误匹配）
2. **控件过滤**：
   - 按控件类型排除 `ButtonControl` / `ToolBarControl` / `TitleBarControl` 等（剔除"表情(Alt+E)" "发送文件" 等按钮）
   - 正则剔除"(Alt+X)"尾缀、"以下为新消息"分隔符
   - 贴纸、时间戳、UI 噪声词黑名单
3. **几何过滤**：
   - 水平：控件中心 x 必须落在聊天区（排除左侧联系人栏）
   - 垂直：控件**顶边** y 在聊天区（允许底边探进输入区，避免最新消息被裁）

### 5. 按气泡颜色判定发言人

之前用 x 坐标判（中心点 vs 中线），长消息跨线就误判。

现在：读取开始时用 `mss` 对微信窗口**整体截一次屏**，对每条消息的 bbox 四角内移 3px 采样 → 4 点平均 → 看是接近 `#95EC69`（绿 = 我）还是 `#FFFFFF`（白 = 对方）。色样失败才回退到左右边距比较。

### 6. 多轮翻页触发懒加载

```
pass 0: 读当前视图                                      → 最新 M10~M15
pass 1: SendMessage(HWND, WM_MOUSEWHEEL, +120) × 1      → 校验顶部锚点
        锚点未变则回退 user32.mouse_event 物理滚轮      → 读 → M5~M12
pass 2: 重复                                             → M1~M8
读完后再 SendMessage 负向滚动 (总 clicks+1) 次回到原位
合并去重（reversed，oldest first）
```

**为什么不用 UIA 的 `ScrollPattern.SetScrollPercent`？** 微信聊天区是 DirectUI 自绘控件，`ScrollPattern` 要么找不到、要么匹配到外层 Pane（percent 从 1.0% 直接跳到 0.0%，中间 20+ 条消息永远不会进视口）。直接给主窗口 HWND 投 `SendMessageW(hwnd, WM_MOUSEWHEEL, ...)` 等效于真实鼠标滚轮，必然命中正确的内部子控件。

**为什么逐刻度 (clicks_per_pass=1) 滚？** 每 pass 只滚一格、读取顶部 `ListItemControl` 作为锚点；锚点没变说明 `SendMessage` 被静默吞掉，立即回退到 `user32.mouse_event(MOUSEEVENTF_WHEEL, ...)` 的物理滚轮（会临时移动光标再还原）。这样能保证每次都前进一刻度，不会跳过中间消息。

### 7. Claude Code 账号复用

`src/llm.py` 自动探测：

```
~/.vscode/extensions/anthropic.claude-code-*-win32-x64/resources/native-binary/claude.exe
```

调 `claude -p --output-format stream-json --verbose --include-partial-messages <prompt>`，解析 JSON 流取文本增量。完全复用你 VSCode 里登录的 Claude Code 会话态，**零 API Key**。

`subprocess` 强制用 `encoding="utf-8"` + `errors="replace"`——Windows 默认 GBK 解不了 Claude 输出的 UTF-8 汉字/emoji。

### 8. WinRT OCR 的 apartment 初始化

`winrt-runtime` 3.x 的异步操作要求进程处于 MTA/STA。首次调用 `ocr_image` 时触发 `winrt.runtime.init_apartment(MTA)`，且必须 `import winrt.windows.foundation`（否则 `_IAsyncOperation` 绑定不到）。

## 已知限制

- **只支持 Windows 版微信**。macOS 版见项目早期 macOS 分支
- **WeChat 4.x 不支持**（使用 Qt 类 `Qt51514QWindowIcon`，不是 `WeChatMainWndForPC`）。请用 **3.19.12.x 原生版本**，推荐 **3.19.12.51**
- **WCF 直发通道有闪退风险**（版本不匹配 / AV 拦截）。默认关闭
- **多显示器**：面板位置使用虚拟屏幕宽度（`SM_CXVIRTUALSCREEN`），但副屏右边沿是否"放得下面板"的判定对非矩形排列显示器可能不准
- **引用消息格式**：微信 UIA 把引用消息和正文拼在一起读出，尚未专门解析
- **流式输出**：只有单风格生成走流式，"3 风格对比"走阻塞调用
- **Cloudflare 拉黑**：VPN 节点被 Anthropic 拉黑时 `claude -p` 返回 403，换节点

## 排错

日志：`%TEMP%\wemuse_panel.log`

### 面板不显示 / 位置不对

查日志里的：

```
[panel] self HWND: raw=919878 top=526638     ← top 应该 != raw
[panel] Owner 已设为微信 HWND=...
[panel] wechat_rect=(..) -> x=.. y=.. 380x761
[panel] move_window ok=True                 ← False 说明 SetWindowPos 失败
```

- `raw == top` → `GetAncestor` 找顶层失败，面板操作作用到错的 HWND
- `ok=False` → 检查 HWND 是否有效
- 位置不对但 ok=True → 可能是 overrideredirect 坑，联系我

### 读取微信读到弹窗通知（222×114 的小矩形）

旧 bug，已修：`_find_wechat_hwnd` 严格按 `ClassName + 面积` 筛选，只取最大的 WeChatMainWndForPC。

### 读取微信漏最新 1-2 条消息

检查 log 里 `[UIA] 聊天区=(...)` 的高度。如果你的输入框高度和默认 `INPUT_FRAC=0.18` 不匹配（比如你的微信缩放比例不一样），可以调 `src/accessibility.py` 顶部的 `INPUT_FRAC`。

### 读取微信发言人判反

检查日志里每条是否走了颜色采样。如果 `[mss]` 截屏失败，会回退到边距比较（不那么准）。确认微信窗口没被其它窗口遮挡，`mss` 能截到像素。

### OCR 返回 "`_IAsyncOperation` 缺失"

缺 `winrt-Windows.Foundation` 包。重跑 `pip install -r requirements.txt`。

### 生成时 "'gbk' codec can't decode ..."

旧 bug 已修：`subprocess` 已强制 UTF-8 + replace。

### 生成时 "claude CLI 返回 403"

VPN 节点被 Anthropic 拉黑，换节点。

### WCF 启用后微信闪退

1. 确认微信是 **3.19.12.51 精确版本**（原生 3.x，不是 Qt/CEF 壳；也不是 3.19.12.52 / 57 等）
2. 把 spy.dll 加进 Windows Defender 白名单
3. 关掉别的 AV 再试
4. 不行就别用 WCF，走默认的剪贴板+键盘通道

## Roadmap

- [ ] 发送前"即将发给 XXX"确认浮窗
- [ ] 解析微信"引用"消息格式
- [ ] 面板尺寸/位置持久化
- [ ] 生成过程可中断
- [ ] WeChat 4.x Qt 版的 UIA 兼容层
- [ ] 托盘图标右键菜单补全

---

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

**Disclaimer**: 本项目默认通道（UIA 读 + 剪贴板写）使用 Windows 公开 API 与微信客户端交互，**不修改微信二进制、不注入代码、不逆向协议**；可选的 WeChatFerry 通道（默认关闭）会注入 `spy.dll`，由用户自行选择是否启用。使用本工具是否符合腾讯《微信软件许可及服务协议》由用户自行判断与承担责任。作者对因使用本工具导致的任何账号问题、数据丢失或其他后果不承担任何责任。
