<p align="right">
  <a href="../../README.md">README</a> · <strong>English</strong> · <a href="../zh-CN/guide.md">简体中文</a> · <a href="./architecture.md">Architecture</a>
</p>

# Usage Guide

Full installation steps, CLI subcommands, MCP configuration, and built-in Agent Skills reference for `wx-observer`.

## 1. Environment & Installation

Requirements: **64-bit Windows 10 / 11**, **64-bit Python 3.11+**, and desktop **WeChat 4.x (`Weixin.exe`)**. When reading a local account, run `wx-observer` under the same Windows user account and privilege level as WeChat. Viewing help and running the offline export snippet require no running WeChat instance.

### Core CLI (4 dependencies)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\wx-observer.exe --help
```

### Add MCP Server Support (`[mcp]`)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

### Development & Offline Tests (`[dev]`)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -v tests
```

<details>
<summary>Copy-paste prompt to let an AI coding assistant set this up</summary>

> In the `wx-observer` repository root (or from `https://github.com/liyifu-2026/we-observer`), verify 64-bit Windows and Python 3.11+, create an isolated `.venv`, install `.[mcp]` with `.venv\Scripts\python.exe`, run `wx-observer --help`, configure `stdio` MCP in my client settings, copy `skills/wx-*` into my client skills directory, and report the exact paths installed.

</details>

### Try Offline Export (Synthetic Data, No WeChat Login Needed)

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
from wx_observer.exporter import format_markdown

message = {
    "chat_id": "demo",
    "chat_name": "读书小组",
    "iso_time": "2026-05-08 09:30",
    "sender_name": "林",
    "quote": {"sender_name": "青", "excerpt": "下次读哪一章？"},
    "text": "周五一起读第三章。",
}
print(format_markdown([message]))
'@ | .\.venv\Scripts\python.exe -
```

## 2. CLI Reference

Add `--help` after any subcommand for full flags (e.g. `wx-observer digest --help`). Global flags (`--db-dir`, `--keys-file`, `--state-dir`, `--json`) can be passed before or after the subcommand.

| Command | Purpose | Example |
| --- | --- | --- |
| `accounts` | List discovered WeChat directories and live `Weixin.exe` PIDs | `wx-observer accounts --json` |
| `chats` | Filter private/group chats by name, remark, or `chat_id` | `wx-observer chats --type group --query "读书"` |
| `history` | Read chat history and export to `md` / `csv` / `json` / `jsonl` | `wx-observer history "<chat>" --since 7d --export md --output out.md` |
| `search` | Keyword search across chats or a single chat (with `--compact` snippets) | `wx-observer search "第三章" --since 30d --compact --skip-noise` |
| `digest` | Single-pass cross-chat summary with counts, links, attachments, and highlights | `wx-observer digest --since 24h --max-chats 20 --token-budget 4000` |
| `attachments` | List shared files (including forwarded records) and verify local paths | `wx-observer attachments --since 7d --ext pdf,docx --only-available` |
| `watch` | Stream newly arriving messages continuously for an isolated `--cursor-id` | `wx-observer watch --cursor-id dev --mode group --jsonl` |
| `mcp` | Run the MCP server over `stdio` or `127.0.0.1` Streamable HTTP | `wx-observer mcp --transport stdio` |

CLI outputs default to full raw message fields (`detail="full"`). Pass `--compact` to shorten long text and include `est_tokens`, and `--skip-noise` to drop animated emoji placeholders while retaining system notices (marked `noise: true`).

## 3. MCP Server Configuration

### `stdio` Transport (Client-Managed)

```json
{
  "mcpServers": {
    "wx-observer": {
      "command": "C:\\path\\to\\wx-observer\\.venv\\Scripts\\wx-observer.exe",
      "args": ["mcp", "--transport", "stdio"]
    }
  }
}
```

### `Streamable HTTP` Transport (Persistent Localhost Daemon)

Recommended when multiple clients or polls hit the same account so snapshots and key caches stay warm in memory:

```powershell
.\.venv\Scripts\wx-observer.exe mcp --transport http --host 127.0.0.1 --port 8765 --token "replace-with-your-secret"
```

Connect to `http://127.0.0.1:8765/mcp` with `Authorization: Bearer <token>`.

### MCP Read-Only Tools (12 Total)

| Category | Tools | Default Agent Ergonomics (`v0.3.2`) |
| --- | --- | --- |
| Account & Status | `list_accounts`, `select_account`, `get_status` | Explicit `account` is stateless and uses a 30s alias cache (with PID check) to avoid repeated system scans. |
| History & Search | `list_chats`, `get_messages`, `search_messages` | Defaults to `detail="compact"` (300-char cap + `est_tokens`) and `skip_noise=True` (skips emojis, keeps `system` tagged `noise: true`). |
| Digest & Files | `digest`, `list_attachments` | `digest` enforces `max_chats=20` and `token_budget=4000` across a 5-stage reducer; `list_attachments` resolves local paths with size/MD5 checks. |
| Cursor Watch | `configure_watch`, `poll_new_messages`, `wait_for_messages`, `ack_messages` | Isolated per `cursor_id`; polling defaults to `peek=True` with `ack_token`; `ack_messages` replays are idempotent (`already_committed: true`). |

All error responses return machine-readable `retryable` (`bool`) and `next_action` recovery guidance. Multi-consumer setups should assign a dedicated `cursor_id` instead of sharing `"default"`.

## 4. Built-in Agent Skills

Four compact `SKILL.md` guides in [`skills/`](../../skills/) teach MCP/CLI agents optimal tool ordering, budget discipline, and read-only boundaries:

| Skill | Use Case | Workflow |
| --- | --- | --- |
| [`wx-brief`](../../skills/wx-brief/SKILL.md) | Daily/weekly briefings and catch-up | Bounded `digest` (4,000 tokens) → targeted `get_messages` context |
| [`wx-find`](../../skills/wx-find/SKILL.md) | Find messages, quotes, links, or local files | `search_messages` + `list_attachments` → `before_seq` window + read `local_path` |
| [`wx-todo`](../../skills/wx-todo/SKILL.md) | Extract `@mentions`, deadlines, and open items | `digest` mentions/attachments/highlights → verify closure in chat context |
| [`wx-watch`](../../skills/wx-watch/SKILL.md) | Real-time triage with at-least-once delivery | Dedicated `cursor_id` + `peek=True` → triage → idempotent `ack_messages` |

Copy any or all Skill folders into your client Skills directory (for example `~/.agents/skills/` or `~/.claude/skills/`):

```powershell
Copy-Item -Recurse -Force .\skills\wx-* "$HOME\.agents\skills\"
```
