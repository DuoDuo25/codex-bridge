#!/usr/bin/env python3
"""codex-bridge: drive Codex.app from Claude Code via CDP + agent-browser.

Primitives (each maps to a CLI subcommand):
    attach              ensure Codex.app is running with --remote-debugging-port=9222,
                        connect agent-browser, return the page CDP URL
    list-workspaces     enumerate visible workspaces in sidebar
    new                 click "在 <ws> 中开始新对话", focus input, type prompt, send
    send                in the currently-focused thread, type prompt and send
    wait                poll until current turn finishes (agent-message bubble settles)
    read                print the latest assistant message text
    archive             archive (with confirm) the currently-open thread
    open-thread         click a sidebar conversation by its (substring of) title
    review              shortcut: in current workspace, start a fresh thread asking
                        Codex to review uncommitted changes (isolated review session)

The --json flag on `new` / `send` / `wait` / `read` returns structured JSON
on stdout for the caller to parse.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

CDP_PORT = int(os.environ.get("CODEX_CDP_PORT", "9222"))
CODEX_APP = os.environ.get("CODEX_APP_PATH", "/Applications/Codex.app")


def _run(cmd: list[str], check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def _ab(*args: str, check: bool = True, timeout: int = 30) -> str:
    """Call agent-browser; return stdout."""
    r = _run(["agent-browser", *args], check=check, timeout=timeout)
    return r.stdout.strip()


def _eval(js: str) -> str:
    """Run JS in the attached Codex page; return raw stdout from agent-browser eval."""
    return _ab("eval", js)


def _eval_json(js: str):
    """Run JS that returns JSON-serializable; parse and return it."""
    out = _eval(js)
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return out


def _insert_prompt(prompt: str) -> dict:
    """Insert a (possibly multi-line) prompt into Codex's ProseMirror input.

    Two pitfalls solved here:

    1. agent-browser's `keyboard type` types characters one at a time; each
       '\\n' becomes an Enter keydown. In Codex's ProseMirror binding, plain
       Enter inserts a paragraph break inside multi-paragraph drafts but ALSO
       acts as the submit shortcut for single-line state — so a multi-line
       prompt typed character-by-character gets split into many submissions
       and the conversation derails. Use `keyboard inserttext` (a single
       beforeinput event) instead — ProseMirror inserts the whole block.

    2. `document.execCommand('insertText', ...)` truncates around 3KB on
       Codex.app's WebView build (observed: 3004-char prompt landed only the
       first ~3040 chars of innerText). `keyboard inserttext` does not have
       this limit because it streams via CDP Input.insertText.

    Strategy: clear the editor via JS, then call agent-browser
    `keyboard inserttext` with the full text. Returns {ok, length, contentLen}.
    """
    clear_js = """(() => {
      const editor = document.querySelector('.ProseMirror');
      if (!editor) return {ok: false, err: 'ProseMirror not found'};
      editor.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(editor);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('delete');
      return {ok: true, cleared: editor.innerText.length};
    })()"""
    res = _eval_json(clear_js)
    if not res or not res.get("ok"):
        sys.exit(f"clear input failed: {res}")
    # `keyboard inserttext` streams via CDP and bypasses the 3KB execCommand limit.
    _ab("click", ".ProseMirror")
    _ab("keyboard", "inserttext", prompt)
    verify_js = """(() => {
      const editor = document.querySelector('.ProseMirror');
      return {contentLen: editor ? (editor.innerText || '').length : 0};
    })()"""
    after = _eval_json(verify_js) or {}
    return {"ok": True, "length": len(prompt), "contentLen": after.get("contentLen")}


def _click_send_button() -> dict:
    """Click Codex.app's circular send button at the bottom-right of the composer.

    Why this exists:
        Plain Enter does not submit when the draft has multiple paragraphs
        (Codex binds Enter to "insert paragraph" in that case). Cmd+Enter
        works on macOS but isn't portable across Codex.app builds. The
        composer always has a rounded-full circular send button — find it
        by structural class and click it directly.

    Selector strategy: walk up from the .ProseMirror to the nearest composer
    container (class includes 'bg-token-input-background'), then find the
    button with `rounded-full` AND `size-token-button-composer` (Codex's
    composer-button size class). Filter to enabled + visible.
    """
    js = """(() => {
      const editor = document.querySelector('.ProseMirror');
      if (!editor) return {ok: false, err: 'no editor'};
      const composer = editor.closest('div[class*="bg-token-input-background"]')
                    || editor.closest('form')
                    || editor.parentElement;
      if (!composer) return {ok: false, err: 'no composer'};
      const btns = [...composer.querySelectorAll('button')];
      const send = btns.find(b =>
        b.className.includes('rounded-full') &&
        b.className.includes('size-token-button-composer') &&
        !b.disabled &&
        b.offsetParent
      );
      if (!send) {
        return {ok: false, err: 'send button not found',
                candidates: btns.length, classes: btns.map(b => b.className.slice(0,80))};
      }
      send.click();
      return {ok: true};
    })()"""
    res = _eval_json(js)
    if not res or not res.get("ok"):
        # Fallback: Cmd+Enter on macOS submits even when the rounded-full
        # button can't be located (composer DOM may have changed in a build).
        editor_focus = """(() => { const e = document.querySelector('.ProseMirror'); if (!e) return false; e.focus(); return true; })()"""
        _eval(editor_focus)
        _ab("press", "Meta+Enter")
        return {"ok": True, "via": "Meta+Enter fallback", "originalErr": res}
    return res


def _cdp_targets():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _is_running() -> bool:
    r = subprocess.run(["pgrep", "-f", f"{CODEX_APP}/Contents/MacOS/Codex"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


# ---- primitives ----

def cmd_attach(args):
    targets = _cdp_targets()
    if not targets:
        if _is_running():
            sys.exit("Codex.app is running but CDP port not open. "
                     "Quit it (osascript -e 'quit app \"Codex\"') and let this command relaunch it.")
        subprocess.run(["open", "-a", CODEX_APP, "--args", f"--remote-debugging-port={CDP_PORT}"])
        for _ in range(20):
            time.sleep(0.5)
            targets = _cdp_targets()
            if targets:
                break
        if not targets:
            sys.exit("Codex.app failed to expose CDP within 10s")

    page = next((t for t in targets if t.get("type") == "page" and "index.html" in t.get("url", "")), None)
    if not page:
        sys.exit("no Codex page target found among CDP targets")
    _ab("connect", str(CDP_PORT))
    print(json.dumps({"attached": True, "title": page["title"], "url": page["url"],
                      "wsUrl": page["webSocketDebuggerUrl"]}, ensure_ascii=False))


def cmd_list_workspaces(args):
    js = """(() => {
      return [...document.querySelectorAll('[aria-label^="在 "][aria-label$=" 中开始新对话"]')]
        .map(b => b.getAttribute('aria-label').replace(/^在 /, '').replace(/ 中开始新对话$/, ''));
    })()"""
    print(json.dumps(_eval_json(js), ensure_ascii=False))


def _resolve_prompt(args):
    """Pick prompt body from --prompt or --prompt-file. Errors if neither.

    --prompt-file is the safer path for prompts that contain shell-meaningful
    characters (\", $, `, etc.) because the shell parses them on the way in
    and breaks the call. File-based passing is byte-clean.
    """
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, "r", encoding="utf-8") as fh:
            return fh.read()
    if getattr(args, "prompt", None) is not None:
        return args.prompt
    sys.exit("error: one of --prompt or --prompt-file is required")


def cmd_new(args):
    aria = f"在 {args.workspace} 中开始新对话"
    js = f"""(() => {{
      const btn = document.querySelector('[aria-label={json.dumps(aria)}]');
      if (!btn) return {{ok: false, err: 'workspace button not found'}};
      btn.click();
      return {{ok: true}};
    }})()"""
    res = _eval_json(js)
    if not res or not res.get("ok"):
        sys.exit(f"new: {res}")
    time.sleep(0.4)
    prompt = _resolve_prompt(args)
    info = _insert_prompt(prompt)
    submit = _click_send_button()
    if args.json:
        print(json.dumps({"sent": True, "workspace": args.workspace,
                          "promptLen": info.get("length"),
                          "contentLen": info.get("contentLen"),
                          "submit": submit},
                         ensure_ascii=False))


def cmd_send(args):
    prompt = _resolve_prompt(args)
    info = _insert_prompt(prompt)
    submit = _click_send_button()
    if args.json:
        print(json.dumps({"sent": True, "promptLen": info.get("length"),
                          "contentLen": info.get("contentLen"),
                          "submit": submit},
                         ensure_ascii=False))


def cmd_wait(args):
    """Wait for the current turn to finish.

    Busy signals (treat the turn as still running when ANY is true):
      1. Stop button visible — Codex is actively generating or running tools.
      2. Command-approval dialog visible — Codex paused waiting for the user
         to allow a sandboxed command (e.g. `git add`, `git commit`). The
         stop button DISAPPEARS while this dialog is up, so without this
         second signal `wait` falsely declares idle and `read` returns the
         mid-turn narration ("我现在先提交...") instead of the real summary.

    With `--auto-approve`, this command will click the "是" (yes-once) button
    on any pending approval dialog before declaring busy, so the workflow
    auto-advances without manual intervention. Without the flag, the dialog
    keeps `wait` blocked until the user clicks something themselves.

    Signal precedence: '正在思考' text alone is NOT used. It only shows
    during model-token generation and disappears during tool calls, which
    made an earlier version exit early in mid-turn quiet windows.
    """
    auto_approve = bool(getattr(args, "auto_approve", False))
    js = (
        """(() => {
      const stopBtn = document.querySelector('button[aria-label*="停止" i], button[aria-label*="stop" i]');
      const stopVisible = stopBtn ? !!stopBtn.offsetParent : false;
      // Codex.app command-approval dialog: a button labelled exactly "是" is
      // the approve-this-once option. Its presence means Codex paused for
      // user approval — still busy from the workflow's perspective.
      const yesBtns = [...document.querySelectorAll('button[aria-label="是"]')]
        .filter(b => b.offsetParent);
      let approved = 0;
      """
        + ("if (yesBtns.length > 0) { yesBtns[0].click(); approved = 1; }" if auto_approve else "")
        + """
      const approvalPending = yesBtns.length > 0 && approved === 0;
      return {busy: stopVisible || approvalPending, stopVisible, approvalPending, approved};
    })()"""
    )
    deadline = time.time() + args.timeout
    last_state = None
    quiet_since = None
    while time.time() < deadline:
        state = _eval_json(js)
        if state != last_state:
            last_state = state
        if isinstance(state, dict) and not state.get("busy"):
            if quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since >= args.quiet_for:
                if args.json:
                    print(json.dumps({"done": True, "elapsed": round(time.time()-deadline+args.timeout, 1)},
                                     ensure_ascii=False))
                return
        else:
            quiet_since = None
        time.sleep(args.poll)
    sys.exit(f"wait: timeout after {args.timeout}s, last state={last_state}")


def cmd_read(args):
    """Print Codex's latest output.

    Two modes:

      default        — last assistant markdown block. Fast, fine for short
                       Q&A and confirm/refute style answers where Codex
                       ends with one cohesive paragraph.

      --since-user   — concatenate ALL assistant markdown blocks since the
                       last user message. Use this whenever Codex did
                       narration + tool calls + final answer in one turn —
                       the "final answer" is often NOT the last block (a
                       short closing line might be), and intermediate
                       blocks contain the substance you asked for.

    Codex DOM model: each user submission becomes a userMessage bubble in
    the conversation. Each assistant block (narration, tool announce, code
    output, final answer) is a markdownContent div. To get "this turn's
    full output" we walk from the LAST userMessage forward and collect
    every markdownContent that appears after it in document order.
    """
    if args.since_user:
        js = """(() => {
          const root = document.querySelector('main, [role="main"]') || document.body;
          // userMessage selector — Codex marks user bubbles distinctly.
          // Try multiple selectors because class names vary across builds.
          const userBubbles = [...root.querySelectorAll(
            '[class*="userMessage" i], [data-message-author-role="user"], [class*="user-message"]'
          )].filter(el => el.offsetParent);
          const lastUser = userBubbles[userBubbles.length - 1] || null;
          const all = [...root.querySelectorAll('[class*="markdownContent" i], [class*="_markdownContent_" i]')];
          const set = new Set(all);
          const outer = all.filter(el => {
            let p = el.parentElement;
            while (p) { if (set.has(p)) return false; p = p.parentElement; }
            return true;
          });
          const visible = outer.filter(m => m.offsetParent && (m.innerText||'').trim());
          // Keep only blocks AFTER the last user bubble in document order.
          const after = lastUser
            ? visible.filter(m => (lastUser.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0)
            : visible;
          return {
            text: after.map(m => (m.innerText || '').trim()).filter(Boolean).join('\\n\\n---\\n\\n'),
            blockCount: after.length,
            foundUser: !!lastUser,
          };
        })()"""
        info = _eval_json(js) or {}
        text = info.get("text")
        if args.json:
            print(json.dumps({"text": text, "length": len(text) if text else 0,
                              "blockCount": info.get("blockCount"),
                              "foundUser": info.get("foundUser")}, ensure_ascii=False))
        elif text:
            print(text)
        return

    js = """(() => {
      const all = [...document.querySelectorAll('[class*="markdownContent" i], [class*="_markdownContent_" i]')];
      const set = new Set(all);
      const outer = all.filter(el => {
        let p = el.parentElement;
        while (p) { if (set.has(p)) return false; p = p.parentElement; }
        return true;
      });
      const visible = outer.filter(m => m.offsetParent && (m.innerText||'').trim());
      const last = visible[visible.length - 1];
      return last ? (last.innerText||'').trim() : null;
    })()"""
    text = _eval_json(js)
    if args.json:
        print(json.dumps({"text": text, "length": len(text) if text else 0}, ensure_ascii=False))
    elif text is not None:
        print(text)


def cmd_archive(args):
    """Archive the active thread (with confirm).

    Locates the row marked `aria-current="page"` in the sidebar, then clicks
    its 归档对话 button. Falls back to title-substring search if --title given.
    """
    target_title = getattr(args, "title", None)
    js_find = f"""(() => {{
      const wantTitle = {json.dumps(target_title)};
      let row = null;
      if (wantTitle) {{
        // explicit title: scan all rows for substring match
        const all = [...document.querySelectorAll('[aria-current], div, li')];
        row = all.find(el => (el.innerText || '').includes(wantTitle)
                          && el.querySelector?.('[aria-label="归档对话"]'));
      }} else {{
        // active row marker
        row = document.querySelector('[aria-current="page"]');
        // climb to nearest container that has an archive button
        while (row && !row.querySelector?.('[aria-label="归档对话"]')) {{
          row = row.parentElement;
        }}
      }}
      if (!row) return {{clicked: false, err: 'no row'}};
      const btn = row.querySelector('[aria-label="归档对话"]');
      if (!btn) return {{clicked: false, err: 'no archive btn in row'}};
      const title = (row.innerText || '').replace(/归档对话/g, '').trim().slice(0, 80);
      btn.click();
      return {{clicked: true, title}};
    }})()"""
    res = _eval_json(js_find)
    if not res or not res.get("clicked"):
        sys.exit(f"archive: {res}")
    time.sleep(0.4)
    js_confirm = """(() => {
      const btns = [...document.querySelectorAll('button, [role=button]')]
        .filter(b => (b.innerText||'').trim() === '确认' && b.offsetParent);
      btns[0]?.click();
      return {confirmed: btns.length > 0};
    })()"""
    res2 = _eval_json(js_confirm)
    if args.json:
        print(json.dumps({**res, **res2}, ensure_ascii=False))


def cmd_open_thread(args):
    """Click a sidebar conversation whose title contains the given substring."""
    needle = args.title
    js = f"""(() => {{
      const links = [...document.querySelectorAll('a, [role=link], [role=button], li, button')];
      const t = links.find(el => (el.innerText||'').includes({json.dumps(needle)}));
      if (!t) return {{ok: false}};
      t.click();
      return {{ok: true, text: (t.innerText||'').slice(0, 80)}};
    }})()"""
    res = _eval_json(js)
    if not res or not res.get("ok"):
        sys.exit(f"open-thread: {res}")
    if args.json:
        print(json.dumps(res, ensure_ascii=False))


def cmd_review(args):
    """Start an isolated review thread in `--workspace`. Asks Codex to review
    uncommitted changes (or a custom focus). Doesn't touch the feature thread."""
    prompt = args.prompt or (
        "请对当前工作区的未提交改动做 code review。"
        "要求：1) 列出每个文件变化的影响范围；2) 标出可能引入的回归或副作用；"
        "3) 检查是否有未处理的 edge case；4) 给出 PASS / 需修改 的明确结论。"
        "如发现问题，按【严重 / 重要 / 建议】分级输出。"
    )
    cmd_new(argparse.Namespace(workspace=args.workspace, prompt=prompt, json=args.json))


def main():
    p = argparse.ArgumentParser(description="Drive Codex.app from Claude Code")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("attach").set_defaults(func=cmd_attach)
    sub.add_parser("list-workspaces").set_defaults(func=cmd_list_workspaces)

    pn = sub.add_parser("new", help="start new thread in workspace + send prompt")
    pn.add_argument("--workspace", required=True)
    pn.add_argument("--prompt")
    pn.add_argument("--prompt-file", help="read prompt body from a file. Prefer this "
                    "when the prompt contains shell metacharacters (quotes, $, backticks).")
    pn.add_argument("--json", action="store_true")
    pn.set_defaults(func=cmd_new)

    ps = sub.add_parser("send", help="send a follow-up in the current thread")
    ps.add_argument("--prompt")
    ps.add_argument("--prompt-file", help="read prompt body from a file. Prefer this "
                    "when the prompt contains shell metacharacters (quotes, $, backticks).")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_send)

    pw = sub.add_parser("wait", help="block until current turn settles")
    pw.add_argument("--timeout", type=int, default=600)
    pw.add_argument("--poll", type=float, default=1.0)
    pw.add_argument("--quiet-for", type=float, default=5.0,
                    help="seconds of quiet (stop button gone, no approval "
                         "dialog) before declaring done. Default 5s because "
                         "Codex tool-call sequences have brief quiet windows "
                         "between phases that fooled shorter windows into "
                         "early-exiting.")
    pw.add_argument("--auto-approve", action="store_true",
                    help="auto-click '是' (yes-once) on any command-approval "
                         "dialog Codex.app shows during the turn. Without "
                         "this flag, the dialog keeps wait blocked until "
                         "the user clicks something. Use only when you "
                         "trust the dispatched prompt's command set.")
    pw.add_argument("--json", action="store_true")
    pw.set_defaults(func=cmd_wait)

    pr = sub.add_parser("read", help="print latest assistant message text")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--since-user", action="store_true",
                    help="read ALL assistant blocks since the last user message, "
                         "joined with separators. Use when Codex did "
                         "narration + tool calls + final answer in one turn.")
    pr.set_defaults(func=cmd_read)

    pa = sub.add_parser("archive", help="archive currently active thread")
    pa.add_argument("--title", default=None,
                    help="archive thread by title substring instead of active one")
    pa.add_argument("--json", action="store_true")
    pa.set_defaults(func=cmd_archive)

    po = sub.add_parser("open-thread", help="open a sidebar thread by title substring")
    po.add_argument("--title", required=True)
    po.add_argument("--json", action="store_true")
    po.set_defaults(func=cmd_open_thread)

    pv = sub.add_parser("review", help="open isolated review thread in workspace")
    pv.add_argument("--workspace", required=True)
    pv.add_argument("--prompt", default=None,
                    help="custom review prompt; defaults to standard uncommitted-diff review")
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(func=cmd_review)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
