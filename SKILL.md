---
name: codex-bridge
description: >
  Drive the Codex desktop app (Codex.app on macOS) from Claude Code via CDP +
  agent-browser. Lets Claude Code dispatch coding tasks to Codex while keeping
  the conversation visible in Codex.app's sidebar so the user can watch it.

  Use when the user says "派给 codex" / "让 codex 写" / "让 codex 改" / "/codex"
  / "drive codex" / "ask codex to implement" / "send to codex" / "have codex
  review", or when the workflow is: Claude plans → Codex codes → cross-review
  with isolated sub-agents on both sides → Codex executes fixes → re-review
  until both reviewers green. Also triggers when the user mentions running a
  feature dev loop with Codex as the executor.

  Architecture: Codex.app is Electron. We launch it with --remote-debugging-port=9222
  and drive its DOM via the agent-browser CLI (CDP client). All conversations
  appear in the user's Codex.app sidebar — they can watch every turn.

triggers:
  - "派给 codex"
  - "让 codex"
  - "/codex"
  - "drive codex"
  - "send to codex"
  - "have codex review"
  - "codex 来执行"
  - "codex 来改"
  - "codex 实现"
  - "cross review"
  - "交叉 review"
  - "派给 codex 改 + Claude review"
---

# codex-bridge

Wraps the Codex desktop app as a controllable subagent. The user keeps full
visibility — every Codex turn shows up in their sidebar.

## Prereqs (one-time)

- `agent-browser` installed (`npm i -g agent-browser`)
- Codex.app installed at `/Applications/Codex.app`
- `~/.codex/auth.json` populated (user already logged in)

## Lifecycle (always do this first)

```bash
python3 ~/.claude/skills/codex-bridge/bridge.py attach
```

If Codex.app isn't running with the debug port, this will quit-and-relaunch it
(the user must close any active conversations first). On success, prints
`{"attached": true, "title": "Codex", ...}`.

## Core commands

| Command | Purpose |
|---|---|
| `attach` | Ensure Codex.app is up + CDP connected |
| `list-workspaces` | Print sidebar workspace names as JSON array |
| `new-workspace --name "<label>"` | Create a NEW BLANK project labelled `<label>` |
| `new-workspace --folder /abs/path` | Create a project bound to an existing folder. Requires Accessibility permission for the calling terminal (System Settings → Privacy & Security → Accessibility) — the native NSOpenPanel is outside the WebView and is driven via osascript keystrokes |
| `new --workspace <ws> --prompt-file <f>` | Start fresh thread, send prompt from file |
| `send --prompt-file <f>` | Follow-up in the currently-open thread |
| `wait [--timeout 600] [--auto-approve]` | Block until turn settles. `--auto-approve` clicks "是" on Codex command-approval dialogs (e.g. `git add`). |
| `read [--since-user]` | Print latest message; or full turn since last user msg |
| `archive` | Archive (with confirm) the currently-open thread |
| `open-thread --title "<substring>"` | Click a sidebar thread by title substring |
| `review --workspace <ws>` | Start isolated review thread in `<ws>` |

All commands accept `--json` for structured output. Both `new` and `send` accept
`--prompt <text>` OR `--prompt-file <path>`. **Strongly prefer `--prompt-file`** —
see "Hard-won lessons" below.

## Cross-review workflow (the actual pattern that works)

The user's role: orchestrator-of-orchestrators. They plan, you (Claude) plan and
review, Codex executes. Reviews go through **two independent staff-engineer
sub-agents** in parallel — one Claude, one Codex — and converge to consensus
before any merge gate.

### The pattern

```
1. Claude plans the change (no Codex call)
2. Codex turn 1 — IMPLEMENT (only)
   - Codex writes code, runs tests, reports done
   - Turn ends. Claude gets notification.
3. Codex turn 2  ──┐  (in parallel)  ┌──  Claude sub-agent
   spawn isolated  │                 │    spawn isolated
   staff-engineer  │                 │    staff-engineer
   sub-agent for   │                 │    review of the
   re-review       │                 │    same diff
                   ▼                 ▼
4. Both verdicts arrive. Claude cross-compares.
   - Both ALL_CLEAN_SHIP_IT → notify user, done.
   - Either BLOCKED → send disagreement back to Codex,
     converge to consensus must-fix list, → step 2 (next round).
```

### Hard rules for this pattern

1. **One Codex turn = one job.** Never put "fix + then spawn re-review sub-agent"
   in the same prompt. Reason: it serializes the re-review behind the fix
   inside one turn, blocking Claude from running its own review in parallel
   and wasting clock time. Always split: turn for fix, separate turn for
   re-review.

2. **Both sides spawn fresh isolated sub-agents.** Not just Codex. The whole
   point is two independent reads of the same code. Single-sided review will
   miss things the other side's blind spot would catch (we found this in
   practice — Claude missed an SQLite-CTE-DML dialect issue Codex caught).

3. **Specify the role explicitly in EVERY review/re-review prompt.** Default:
   `Staff Engineer`. The role sets the strictness filter. Don't rely on
   "context remembers" — fresh sub-agents don't have your conversation history.

4. **Tell sub-agents NOT to read prior review documents** (e.g. any
   `code-review/` notes directory in your repo). They bias the result toward
   existing findings. Independent reads are the whole point.

5. **Fix prompts and review prompts are different templates.** See
   `references/workflow.md` for both.

6. **`--prompt-file` for anything containing `"`, `$`, backticks, or longer
   than ~500 chars.** bash quote handling will mangle the prompt otherwise.

7. **Long, important prompts go in files** (e.g. `/tmp/codex-fix-r2.md`).
   Easier to review before sending; can be re-sent if Codex's first run
   gets disrupted.

## When things go wrong

- `attach` fails with "CDP port not open": Codex.app is running without the
  flag. Ask user to quit it, re-run `attach`.
- `new` errors "workspace button not found": run `list-workspaces`, check
  spelling. Workspace must exist in the sidebar (skill won't create it).
- `wait` returns "done" but message is suspiciously short: this usually means
  Codex is between phases (model→tool→model). Use `--quiet-for 8` or higher,
  OR re-check via `read --since-user` and `agent-browser eval` for stop
  button visibility.
- `wait` returns "done" but Codex actually paused on a command-approval
  dialog (e.g. for `git add` / `git commit`): the dialog hides the stop
  button so the busy signal goes false. As of the latest bridge.py, `wait`
  already detects approval dialogs and treats them as busy. If you WANT
  it to auto-advance, pass `--auto-approve` — clicks the "是" (yes-once)
  button. Otherwise the user clicks manually and the next `wait` poll
  continues.
- `send` mangled the prompt (truncated, fragmented messages, "message got
  clipped" reply): bash quote escape issue. Use `--prompt-file` instead.
- `bridge.py send/new` exits with `unrecognized arguments`: same shell
  quoting issue. Use `--prompt-file`.
- Multiple threads created from one `new` call: agent-browser was using
  `keyboard type` (per-char keys) which submitted on each `\n`. Should not
  happen with current bridge.py — uses `keyboard inserttext` (single beforeinput
  event). If you see it, check `_insert_prompt`.

## Read on demand

- `references/workflow.md` — full cross-review recipe, prompt templates,
  common pitfalls and how today's bridge.py was hardened against them.
- `references/selectors.md` — DOM selectors and ARIA conventions Codex.app
  exposes. Read when adding a new primitive or a click breaks.
