# codex-bridge

> 让 Claude Code 把 **Codex.app** 当作可控的子代理来用：Claude 负责规划与审阅，Codex 负责执行——所有对话都实时显示在 Codex.app 的左侧栏，你能看见每一步在做什么。

这是嗨妮好（Hinihao）做的一个 Claude Code Skill 彩蛋，开源送给愿意一起折腾 AI 协作工作流的粉丝朋友 :)

- 小红书：<https://xhslink.com/m/Ancw5wgPEjJ>
- 抖音：<https://v.douyin.com/S1l59Ohobnw/>

---

## 它解决什么问题

如果你已经在用 Claude Code，但想让 Codex（OpenAI Codex 桌面端）也加入工作流——比如 **Claude 当总规划师 + Codex 当执行写代码 + 双方互相 review**，目前没有现成的桥。

这个 Skill 就是那座桥：

- Claude Code 通过 Chrome DevTools Protocol（CDP）+ [agent-browser](https://www.npmjs.com/package/agent-browser) 远程控制 Codex.app 的 DOM；
- Codex.app 还是 Codex.app——所有对话在它原生的 UI 里展开，你能正常看到、读到、点击、随时接手；
- 但 Claude Code 可以**主动**：开新对话、贴长 prompt、等回合结束、读结果、归档线程，全自动；
- 配合内置的 **cross-review 工作流**（一份代码两边独立审 → 共识才放行），让 AI 写的代码更靠谱一点点。

---

## 工作流示意

```
        ┌─────────────┐                         ┌─────────────┐
        │ Claude Code │  ────  CDP / agent  ──> │  Codex.app  │
        │ (规划/审阅) │  <───  最新对话内容 ──── │  (执行/写码) │
        └─────────────┘                         └─────────────┘
              │
              │  spawn 一个独立 sub-agent
              ▼
       ┌────────────────┐         ┌────────────────┐
       │ Claude 侧 review│  对比 ◀│ Codex 侧 review │
       └────────────────┘   结果  └────────────────┘
              │
              ▼
         共识 → 上线 / 分歧 → 派回 Codex 修
```

---

## 安装

> 前置：macOS、装好 Codex.app（`/Applications/Codex.app`）、Codex 账号已登录、Python 3.8+、Node.js。

### 1. 装 agent-browser（驱动 CDP 的 npm CLI）

```bash
npm install -g agent-browser
```

### 2. 把这个 Skill 放到 Claude Code 的 skills 目录

```bash
# 把整个仓库 clone 到 Claude Code 的 skills 目录
git clone https://github.com/DuoDuo25/codex-bridge.git \
  ~/.claude/skills/codex-bridge

# 验证文件落到了正确位置
ls ~/.claude/skills/codex-bridge/
# 应该能看到 SKILL.md、bridge.py、references/
```

### 3. 验证

在 Claude Code 里跟 Claude 说一句：

```
派给 Codex 写一个 hello world Python 脚本
```

如果一切正常，Claude 会：
1. 自动启动 / attach 到 Codex.app（带 `--remote-debugging-port=9222`）
2. 在你选的 workspace 里开新对话
3. 把 prompt 贴进去，按发送
4. 等 Codex 执行完，读结果回报给你

---

## 命令速查

所有命令都是 `python3 ~/.claude/skills/codex-bridge/bridge.py <command>`。

| 命令 | 作用 |
|---|---|
| `attach` | 确保 Codex.app 起来 + CDP 已连接 |
| `list-workspaces` | 列出左侧栏所有 workspace 名（JSON） |
| `new-workspace --name "<label>"` | **新增**：创建一个空白项目 |
| `new-workspace --folder /abs/path` | **新增**：把项目绑定到一个已有文件夹（需要辅助功能权限，见下方） |
| `new --workspace <ws> --prompt-file <f>` | 开新对话并发送 prompt |
| `send --prompt-file <f>` | 在当前对话里发追加消息 |
| `wait [--timeout 600] [--auto-approve]` | 阻塞等当前 turn 结束 |
| `read [--since-user]` | 读最新一条 / 自上一条用户消息以来的全部回复 |
| `archive` | 归档当前活动对话（带确认） |
| `open-thread --title "<substring>"` | 按标题子串切到历史对话 |
| `review --workspace <ws>` | 在指定 workspace 开一条独立的 review 线程 |

> **`new-workspace --folder` 需要辅助功能权限**：因为 Codex.app 选文件夹的对话框是 macOS 原生 NSOpenPanel，CDP 看不到，得用 `osascript` 模拟键盘（Cmd+Shift+G 贴路径 + Enter）。第一次用之前去：**系统设置 → 隐私与安全性 → 辅助功能**，把你跑命令的终端 app（Terminal / iTerm / 等）勾上。如果忘了，会看到一行 `osascript is not allowed assistive access` 报错。

任何命令都可加 `--json` 拿结构化输出。`new` 和 `send` 强烈建议用 `--prompt-file`——shell 转义会毁掉带引号 / `$` / 反引号的长 prompt。

---

## Cross-review 工作流

这是这个 Skill 最有意思的部分：**两份独立 staff-engineer review，达成共识才放行**。

```
1. Claude 规划改动（暂不调 Codex）
2. Codex turn 1 —— 只实现，不 review
3. 并行：
   ├─ Codex turn 2: spawn 一个 isolated sub-agent 重审刚才的 diff
   └─ Claude 侧:    Agent 工具 spawn 一个 isolated sub-agent 重审同一份 diff
4. 两份判决到位 → Claude 比对
   - 都 ALL_CLEAN_SHIP_IT → 通知你完成
   - 有任一 BLOCKED       → 整理 must-fix，回到第 2 步
```

完整模板和踩坑记录在 [`references/workflow.md`](references/workflow.md)。

---

## 已知限制

- **macOS only**：靠 `pgrep` + `open -a` + Codex.app 的本地路径。Linux/Windows 适配需要改 `bridge.py` 的进程检测和启动逻辑。
- **Codex.app 必须先登录**：Skill 借用你已有的 ChatGPT/Codex 账号，token 用量计入你的账号。
- **依赖 Codex.app 的 DOM 结构**：Codex 大版本更新可能让选择器失效。如果断了，先去翻 [`references/selectors.md`](references/selectors.md) 比对当前页面 DOM。
- **中文 ARIA label**：当前 selector 写死了中文（如 `归档对话`、`是`、`保存`、`项目名称`、`使用现有文件夹`），系统语言切英文时需要相应改 `bridge.py`。欢迎 PR 适配多语言。
- **archive 偶尔被沙盒限制**：如果你的 Claude Code 配置里限制了某些点击，archive 可能要手动做。

---

## 贡献

- 提 Issue 描述场景 + 重现步骤。
- PR 欢迎，特别是：
  - Linux / Windows 启动逻辑适配
  - Codex.app 英文 locale 下的选择器
  - 新增 cross-review 模板
  - 文档/示例的中英 / 中日翻译

---

## 致谢

这个 Skill 是在做 Claude × Codex 协作工作流时一点一点踩坑磨出来的。每一个看起来奇怪的实现细节（为什么不用 `keyboard type`、为什么 `wait` 要看 stop 按钮和审批弹窗两个信号、为什么要 `--prompt-file`）背后都有一次掉坑经历——详见 [`references/workflow.md`](references/workflow.md) 末尾的 "Hard-won lessons" 章节。

---

## License

MIT —— 详见 [LICENSE](LICENSE)。你可以自由使用、修改、再分发，包括商用。

---

**作者**：嗨妮好（Hinihao）
**关注我**：[小红书](https://xhslink.com/m/Ancw5wgPEjJ) · [抖音](https://v.douyin.com/S1l59Ohobnw/)
