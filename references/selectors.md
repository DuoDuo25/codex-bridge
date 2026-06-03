# Codex.app DOM selector cheatsheet

Codex.app version observed: 26.429.61741 (Electron 41.2.0). Selectors verified
against this build; revisit if the UI is restyled.

## Sidebar / workspaces (项目)

- Workspace folder list lives in `aside / nav` of the left pane.
- "Start new conversation in workspace X" button:
  ```
  [aria-label="在 <workspace-name> 中开始新对话"]
  ```
  Click this to create a new thread in that workspace and immediately focus
  the input area.

- "Workspace actions" menu (rename / delete project):
  ```
  [aria-label="<workspace-name> 的项目操作"]
  ```

- "Archive conversation" button (one per conversation row):
  ```
  [aria-label="归档对话"]
  ```
  There are multiple of these on the page (one per visible conversation). To
  target the active conversation specifically, find the archive button whose
  parent chain contains the current top-bar title (see `bridge.py` cmd_archive).

- Archive confirmation:
  ```
  // After clicking 归档对话, look for a button whose innerText is exactly:
  '确认'
  ```
  This appears as an inline confirm right next to the conversation row.

## Input area

- ProseMirror rich-text editor:
  ```
  .ProseMirror
  ```
  Single instance per page. Click to focus, then `keyboard type "..."` to enter
  text. Press `Enter` to submit (no separate Send-button click needed).

- Send button (used by `wait` to detect "turn running"):
  ```
  button[aria-label*="发送" i], button[aria-label*="send" i]
  ```
  Disabled while a turn is in flight.

## Active turn detection

- "Thinking..." indicator appears as text inside the conversation pane:
  ```
  document.body.innerText matches /正在思考|Thinking…|Thinking\.\.\./
  ```
  Used by `bridge.py wait` to poll for completion.

## Message bubbles

- Assistant message content (markdown-rendered):
  ```
  [class*="markdown" i], [class*="Markdown" i]
  ```
  Codex uses CSS modules so class names are hashed (e.g. `_markdownContent_7mcvb_31`).
  Use case-insensitive substring match. The latest visible match is the most
  recent assistant reply.

- User-sent message (right-aligned bubble): no consistent class observed yet;
  if needed, walk DOM by structure rather than class.

## Top bar

- Conversation title:
  ```
  h1 / header > first text node
  ```
  Codex auto-titles threads after the first turn (e.g. our "Claude Code 通过
  agent-browser 控制你！请用 5 个字回复" got auto-renamed to "控制浏览器").

- Conversation overflow menu (`...`) for delete / rename / fork:
  ```
  [aria-label="对话操作"]
  ```

## Stable techniques

- Prefer `[aria-label="..."]` matches over class-based selectors. ARIA labels
  are user-facing strings and tend to be stable across UI redesigns.
- For "find in a row by adjacent text content," walk up `parentElement` chain
  up to ~5 levels and check `innerText.includes(...)`. See `cmd_archive` for the
  pattern.
- Class-name substring with `[class*="..." i]` survives CSS-module hashing.

## Things to NOT click without confirmation

- `[aria-label="<workspace> 的项目操作"]` opens a menu that includes "Delete
  project" — destructive and unconfirmed in some flows.
- Top-bar overflow `[aria-label="对话操作"]` exposes "Delete conversation"
  (different from 归档/archive). Permanent.

When in doubt, use `agent-browser screenshot` between steps and inspect.
