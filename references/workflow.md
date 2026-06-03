# Cross-review workflow with codex-bridge

The user's intended division of labor:

- **Claude Code (orchestrator)**: plans, dispatches, reviews, decides PASS/FAIL
- **Codex (executor)**: implements, runs tests, reports done. Also spawns its
  own isolated sub-agent for self-review when asked, on a separate turn.
- **Cross-review**: when stakes are high (production launch, accounting, state
  machines), TWO independent staff-engineer sub-agents (one Claude-side, one
  Codex-side) review the same diff in fresh isolated contexts and converge.

Codex.app maintains a single feature thread for one feature's lifecycle (so
Codex keeps full context across many turns). Reviews can be:
- **In-thread sub-agent re-review** (preferred for production gate): Codex
  spawns an isolated sub-agent in the same thread, fresh context, no fork of
  the conversation history. Result lands in the same timeline so future
  fix-execution turns see the review verdict.
- **Fresh-thread review** (lighter, when continuity isn't needed): use
  `bridge.py review --workspace <ws>` to start a fully separate thread.

Default to in-thread sub-agent for cross-review. Use fresh-thread only for
ad-hoc one-off audits.

## The strict pattern (production-gate cross-review)

```
1. Claude plans the change (no Codex call yet)

2. Codex turn 1 — IMPLEMENT ONLY
   prompt: "fix items 1..N + run tests. When done, send a brief summary
            message and STOP. Do NOT spawn any review sub-agent yet."
   ▼
   Codex writes code, runs tests, reports done. Turn ends.
   Claude receives wait completion notification.

3. ── parallel ──
   a) Codex turn 2:                b) Claude sub-agent (Agent tool):
      prompt: "Now spawn an           prompt: "Staff Engineer re-review
      isolated Staff Engineer         of the diff. Fresh context. No
      sub-agent to re-review the      reading prior reviews. file:line
      diff. Same scope as turn 1.     refs throughout. Verdict:
      Verdict: ALL_CLEAN_SHIP_IT      ALL_CLEAN_SHIP_IT or BLOCKED."
      or BLOCKED."

4. Both verdicts arrive. Claude cross-compares.
   - Both ALL_CLEAN_SHIP_IT → notify user, archive review threads if any.
   - Either BLOCKED → send the disagreement to the *other* side, ask for
     confirm/refute on each unique finding, converge to consensus must-fix
     list, then go to step 2 (next round).
```

## Hard rules for the pattern

### 1. One Codex turn = one job. NEVER bundle "fix + spawn re-review" in one prompt.

**Wrong:**
```
fix items 1..N + after fixing, spawn isolated Staff Engineer sub-agent
to re-review and report verdict.
```

This serializes Codex's fix → sub-agent inside ONE turn. Claude can only
detect "Codex done" after both finish. The Claude-side sub-agent re-review
that should run in parallel is now blocked behind Codex's full sequence.
Wall-clock cost: easily 2x.

**Right:** see step 2 above. Fix and re-review go in separate turns.

### 2. Both sides must spawn fresh isolated sub-agents

A single side's review has predictable blind spots. Real example from this
project: Claude's sub-agent passed all 7 fixes as ALL_CLEAN_SHIP_IT. Codex's
sub-agent caught a critical issue Claude missed — SQLite/D1 syntax error in
the new CTE-DML accounting statements. Cross-validation against actual SQLite
3.51.0 confirmed Codex was correct. Without two-sided review the bug would
have shipped.

### 3. Always specify the engineering role explicitly

Default: `Staff Engineer`. Sets the strictness filter ("what would actually
break in production?"). Don't rely on context — sub-agents are fresh.

Other valid framings if the situation calls for it:
- Principal Engineer — cross-system architecture / long-term evolution
- SRE Lead — observability / oncall load
- Security Engineer — attack surface / credential / data leakage
- Performance Engineer — latency / throughput / resource use

### 4. Tell sub-agents NOT to read prior reviews

Lines like "do not read any prior `code-review/` notes directory" are required. Prior review
files bias the sub-agent toward their findings (and away from new ones).
The whole point of a fresh sub-agent is independent perspective.

### 5. Re-review prompts must repeat all the rigor requirements

Don't write "re-review the same way you reviewed before." Sub-agents are
fresh and don't share the previous instructions. Spell out: scope, role,
deliverable format, severity grading, "do not soften", file:line refs,
verdict format. Every time.

### 6. Verify SQL dialect and runtime behavior, not just logic

When the change touches DB queries (CTEs, joins, RETURNING, transactions),
the review must verify dialect compatibility. Mocked unit tests
(`db.run` mocks that string-match SQL) prove nothing about whether the
SQL parses on real D1/SQLite. Use real SQLite (`sqlite3` CLI on macOS,
or `better-sqlite3` in Node) to test the actual statement.

## Prompt templates

### Initial dispatch (start feature thread)

Save to a file (e.g. `/tmp/codex-task.md`) and pass via `--prompt-file`:

```
工作目录: <repo path on disk>

任务: <one-paragraph plan from Claude>

第一步要做的事 (具体到文件/函数):
- <step 1>
- <step 2>

注意:
- 改完每一步用 git diff 自查一遍再继续
- 遇到不确定的设计决策不要拍脑袋,先打住等我确认
- 只动这次任务相关的文件,不要顺手做别的清理
- 跑完 npm test + npm run typecheck 全绿才算这一步完成
```

### Fix-only turn (production-gate pattern)

```
这一轮请只做修复,不要在这同一个 turn 里 spawn re-review sub-agent。

修复清单:
1. <file:line> — <一句话描述> — <具体修法>
2. ...

测试要求:
- npm test 全绿
- npm run typecheck 全绿
- 必要时新增 <X> 集成测试

修完先停下来发一条简短总结,标志这一 turn 结束。
不要自己开 sub-agent,等我下一条 message 再 spawn re-review sub-agent。
```

### Re-review turn (sent to Codex on a separate turn)

```
现在 spawn 一个 isolated Staff Engineer sub-agent 对刚才你修的代码做 re-review。

硬性要求:
- Fresh isolated context,不 fork 当前对话历史
- 不读任何先前 review 文档(会引入 bias)
- 直接读 git diff,不靠 helper 名字推测
- Do NOT soften findings
- 每条 finding 必须 file:line + reproducer + 具体修法

逐条验证以下修复(对每条:trace 真实代码路径,判断是否真关掉了原始 bug,
以及修复本身是否引入新 bug):
1. <fix item 1>
   位置:<file:line>
   验证:<具体验证目标>
2. ...

新增/修改的测试也要逐条验证:
- <test file:line> — <claim 覆盖的 finding>
- 对每条:测试是否真覆盖目标?如果 mock 了 SQL,mock 行为是否忠实反映生产?

每条 deliverable 格式:
- Status: FIXED / PARTIAL / NOT_FIXED / NEW_BUG_INTRODUCED
- Trace: 走代码路径 (file:line) 说清楚为什么 fix 真的关掉原始 bug
- New concerns: 是否有 fix 自身的 bug 或暴露的旧问题

最终 verdict:
- ALL_CLEAN_SHIP_IT 如果所有条都干净
- BLOCKED + 剩余 must-fix 列表 (file:line + 具体修法)

不要软化结论。这是 production launch 前最后一道关。

(并行的 Claude staff engineer sub-agent 也在做同样的 re-review。
我会在拿到你的独立结果之前不告知 Claude 那份,避免锚定。)
```

### Cross-check turn (when reviews disagree)

```
两份 staff engineer re-review 出现分歧。下面是对方独立发现、你之前没列出的
N 条。请逐条 confirm 或 refute,理由带上具体代码 trace,不要软化。然后产出
两份合并后的最终 must-fix 清单(只列 launch 前必修的)。

A) [对方判 H/M/L] <issue 简述>
- <file:line>
- 场景:<reproducer>
- 修法建议:<concrete fix>
- 你确认这是真问题吗?

B) ...

最后,合并 must-fix 清单格式要求:
- 只列 launch 前必修的 (High + 影响业务的 Medium)
- 每条:位置、一句话描述、具体修法
- 排序按修复优先级
```

## Full bash loop (production-gate pattern)

```bash
BRIDGE=~/.claude/skills/codex-bridge/bridge.py
WS=your-workspace-name           # the workspace label as shown in Codex.app's sidebar
REPO=/absolute/path/to/your/repo

python3 $BRIDGE attach
python3 $BRIDGE list-workspaces

# Round 1, turn 1: implement
python3 $BRIDGE new --workspace "$WS" --prompt-file /tmp/codex-task-r1.md
python3 $BRIDGE wait
python3 $BRIDGE read --since-user --json > /tmp/codex-r1-out.json

# Round 1, in parallel:
#   (a) ask Codex turn 2 to spawn its review sub-agent
#   (b) launch Claude's own review sub-agent (Agent tool, run_in_background:true)
python3 $BRIDGE send --prompt-file /tmp/codex-rereview-r1.md
# (Claude background sub-agent launched separately via Agent tool)
python3 $BRIDGE wait                                  # Codex sub-agent done

# Cross-compare
python3 $BRIDGE read --since-user --json > /tmp/codex-rereview-r1.json
# (Claude background sub-agent notification arrives via task system)

# If both ALL_CLEAN_SHIP_IT: done
# If BLOCKED on either side: write next-round fix prompt → next turn
```

## Decision rubric for Claude (the orchestrator)

After each round, before deciding PASS/FAIL, Claude must check:

1. **Diff sanity**: `git diff` shows ONLY files relevant to the task. If
   Codex touched unrelated files, send a "revert these unrelated changes"
   prompt.
2. **Test alignment**: did the change break something that already worked?
   Run the test suite; verify pass count.
3. **SQL dialect** (if applicable): if changes touch DB queries, run the
   actual statements through `sqlite3` (or whatever runtime is target)
   to confirm syntactic validity. Don't trust mock-based tests.
4. **Both reviewers' verdicts**: at least one disagreement = converge first
   via cross-check turn before any further fix dispatch.
5. **Edge cases**: at least one edge case neither reviewer mentioned but
   the diff might miss. Force articulation before allowing PASS.

PASS only if all five green. Otherwise feed specifics back into feature thread.

## When to interrupt and ask the user

- Codex hits a design decision the spec didn't cover (e.g. error-handling
  convention, naming, public API shape)
- The diff size grows beyond the task scope and Codex appears to be
  "tidying up" — confirm with user before more rounds
- Tests start failing in a way that suggests a deeper issue (incorrect
  assumption in the plan)
- Two reviewers disagree on something that requires runtime verification
  (SQL dialect, race condition timing) — ask whether to verify locally
  or trust one side

## Hard-won lessons (today's debugging session, distilled)

These are why bridge.py looks the way it does. Don't undo them.

### Prompt fragmentation: NEVER use `keyboard type` for multi-line prompts

Symptom: a single multi-line prompt creates multiple Codex threads, each
with truncated content; first Codex reply says "your message got clipped."

Cause: `agent-browser keyboard type <text>` types each character including
`\n` as an Enter key event. Codex.app's ProseMirror binds Enter to "submit
message" in the empty/new-thread state, so each `\n` submits the buffer as
a new turn / sometimes a new thread.

Fix in `bridge.py`: use `agent-browser keyboard inserttext` (single
beforeinput event, ProseMirror inserts the whole block as paragraphs without
firing Enter).

### `execCommand('insertText')` truncates around 3KB

Symptom: long prompt only inserts the first ~3000 chars into the editor.

Cause: WebView's `document.execCommand('insertText', ...)` has an undocumented
size cap on Codex.app's build. Streaming via CDP `Input.insertText` doesn't.

Fix in `bridge.py`: `_insert_prompt` uses agent-browser `keyboard inserttext`,
not `execCommand`.

### Plain Enter doesn't submit when the draft has multiple paragraphs

Symptom: prompt fully inserted but Enter key just adds a paragraph break;
nothing submits.

Cause: ProseMirror binds Enter to "insert paragraph" when the doc has
multiple blocks; submit is gated on a different shortcut.

Fix in `bridge.py`: `_click_send_button` finds the rounded-full circular
send button at the bottom-right of the composer (`size-token-button-composer
rounded-full` classes) and clicks it directly. Falls back to `Meta+Enter`
(macOS Cmd+Enter) if the button isn't located.

### Command-approval dialogs trick `wait` into thinking Codex is idle

Symptom: `wait` returns "done" but git log shows no new commits, and `read`
returns Codex's mid-turn narration ("我现在先提交...") rather than a real
completion summary.

Cause: when Codex.app needs to run a sandboxed command (e.g. `git add`,
`git commit`, `rm`), it pauses and shows a numbered dialog asking the user
to approve. While the dialog is up, the stop button DISAPPEARS — Codex
isn't running anything. So `wait`'s stop-button signal goes false and
declares the turn done. But Codex is actually frozen waiting for a click.

Fix in `bridge.py`: `wait` now detects the presence of a button with
`aria-label="是"` (the "yes-once" approve option) and treats it as a busy
signal. The dialog blocks `wait` until the user clicks it OR `wait`
auto-clicks it via `--auto-approve`.

Pattern when running long automated workflows:
- Use `--auto-approve` when you trust the dispatched prompt's command set
  (e.g. you instructed Codex to commit on a feature branch with no `rm`).
- Skip `--auto-approve` when manual oversight is desired — the dialog
  appears in Codex.app's UI and the user clicks themselves.

### `wait` exiting too early on "正在思考" text

Symptom: `wait` returns "done" but `read` returns Codex's preamble narration
(short), not the substantive final answer.

Cause: "正在思考" / "Thinking…" text only shows during model-token-generation
phases. During tool calls (file reads, command runs) the text disappears. A
2-second quiet window across multiple model→tool→model transitions in one
turn fooled the wait into declaring done while Codex was still working.

Fix in `bridge.py`: `cmd_wait` uses **stop button visible** as the canonical
busy signal. The stop button stays up for the WHOLE turn including tool
calls. Default `--quiet-for=5.0` (was 2.0).

### `read` "truncating" was actually wait-too-eager picking the wrong block

Symptom: `read` returns ~3000 chars, missing the final 5000-char answer.

Cause: not a transport-layer truncation. agent-browser via CDP can return
5KB+ strings cleanly. The "truncation" was wait declaring done early
(see above), so when `read` ran, only the early Codex narration block was
visible — the substantive answer hadn't been emitted yet.

Fix: fix `wait`. Don't add clipboard hacks to `read`. Verified by direct
agent-browser eval that 5313-char messages round-trip without loss.

### `read` only returns the last block, missing turn-wide context

Symptom: even after wait works correctly, `read` returns a short close-out
line, not the substantive output. Codex emits multiple markdown blocks per
turn (preamble narration + status updates + final answer); `last block` may
be a 200-char closing remark, not the 5000-char review.

Fix in `bridge.py`: added `read --since-user` mode. Returns ALL assistant
markdown blocks since the last user message, joined with `\n\n---\n\n`.
Use this for review/cross-check turns. Default `read` (last block only)
remains for short Q&A use.

### bash quote-handling mangles `--prompt` with `"` / `$` / backticks

Symptom: `bridge.py send --prompt "...含 \"...\" 的内容..."` exits with
`unrecognized arguments` or sends a truncated/garbled prompt.

Cause: bash interprets the embedded quotes as terminating the outer string.

Fix in `bridge.py`: added `--prompt-file <path>` to both `new` and `send`.
Use this for any non-trivial prompt; reserve `--prompt` for short literals.

### Stopping a Codex thread mid-turn doesn't always work

Symptom: clicking the stop button stops the current generation, but Codex
auto-restarts because there were queued user messages (from a prior
fragmented prompt) waiting in the thread.

Workaround: archive the thread (sandbox may block this for the agent — ask
the user to do it manually). Or accept the abandoned thread and start a
fresh one.

Prevention: don't send fragmented prompts in the first place — use
`--prompt-file` and the fixed `_insert_prompt`.

### Sub-agent role inconsistency between rounds

Symptom: round-1 review found H1 (budget double-count), round-2 review
done by Codex's sub-agent didn't catch a similar-severity issue, because
the round-2 prompt forgot to re-state "Staff Engineer" role and rigor
requirements.

Fix: ALWAYS spell out the role + rigor reqs in EVERY review prompt.
Sub-agents don't share context across runs.

## Limits / known issues

- **Workspace must exist in the sidebar already.** This skill doesn't create
  project folders. User has to add the repo as a Codex 项目 once.
- **Single Codex.app window.** Multiple threads in the same workspace are
  fine — switch with `open-thread` — but multiple windows aren't supported.
- **Auth piggybacks on the user's ChatGPT login.** Token consumption hits
  their account; rate-limited == all bets off until reset.
- **Archive is sandboxed.** The agent's bridge can click stop but
  `bridge.py archive` may be blocked from external write rules. User
  archives manually for now.
