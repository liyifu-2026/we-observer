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
python -m pip --python .\.venv\Scripts\python.exe install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\wx-observer.exe --help
```

> If `python -m venv .venv` fails with an `ensurepip` error on a stripped Python build, create the venv with `python -m venv --without-pip .venv` and run the second line (`python -m pip --python ...`) directly to bootstrap pip.

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

Add `--help` after any subcommand for full flags (e.g. `wx-observer digest --help`). Global flags (`--version`/`-V`, `--db-dir`, `--keys-file`, `--state-dir`, `--no-key-cache`, `--audit`, `--json`) can be passed before or after the subcommand.

| Command | Purpose | Example |
| --- | --- | --- |
| `accounts` | List discovered WeChat directories and live `Weixin.exe` PIDs | `wx-observer accounts --json` |
| `verify` | Run 3-line health check (exit `0` ok, `1` optional/FTS degraded, `2` required or `--strict` failed) | `wx-observer verify` |
| `doctor` | Collect redacted OS/Python/WeChat/shard diagnostics (or zip bundle) | `wx-observer doctor --bundle doctor.zip` |
| `chats` | Filter private/group chats by name, remark, or `chat_id` | `wx-observer chats --type group --name-query "读书"` |
| `history` | Read chat history and export to `md` / `csv` / `json` / `jsonl` | `wx-observer history "<chat>" --since 7d --export md --output out.md` |
| `search` | Keyword search across chats (`--chats`, `--exclude`, `--min-activity`) | `wx-observer search "第三章" --since 30d --compact --skip-noise` |
| `digest` | Single-pass cross-chat summary with counts, links, attachments, and highlights | `wx-observer digest --since 24h --max-chats 20 --token-budget 4000` |
| `attachments` | List shared files (including forwarded records) and verify local paths | `wx-observer attachments --since 7d --ext pdf,docx --only-available` |
| `watch` | Stream newly arriving messages continuously for an isolated `--cursor-id` | `wx-observer watch --cursor-id dev --mode group --jsonl` |
| `mcp` | Run the MCP server (`stdio`/`http`) or generate client configs (`--print-config`) | `wx-observer mcp --print-config cursor` |

CLI outputs default to full raw message fields (`detail="full"`). Pass `--compact` to shorten long text (450-char `head_tail` default via `--truncate`) and include `est_tokens`, and `--skip-noise` to drop animated emoji placeholders while retaining system notices (marked `noise: true`).

## 3. MCP Server Configuration

### Print Client-Ready Snippets (`--print-config`)

Generate copy-pasteable configs with your active `.venv` executable path filled in automatically:

```powershell
.\.venv\Scripts\wx-observer.exe mcp --print-config cursor
.\.venv\Scripts\wx-observer.exe mcp --print-config claude
.\.venv\Scripts\wx-observer.exe mcp --print-config vscode
.\.venv\Scripts\wx-observer.exe mcp --print-config codex
.\.venv\Scripts\wx-observer.exe mcp --print-config cordis
.\.venv\Scripts\wx-observer.exe mcp --print-config generic
```

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

Recommended when multiple clients or polls hit the same account so snapshots and key caches stay warm in memory. Pass the Bearer token via `WX_OBSERVER_MCP_TOKEN` or `--token-file` instead of command-line `--token` (which exposes secrets in process listings):

```powershell
$env:WX_OBSERVER_MCP_TOKEN = "replace-with-your-secret"
.\.venv\Scripts\wx-observer.exe mcp --transport http --host 127.0.0.1 --port 8765
# Or read from a restricted file:
.\.venv\Scripts\wx-observer.exe mcp --transport http --host 127.0.0.1 --port 8765 --token-file .\mcp-token.txt
```

Connect to `http://127.0.0.1:8765/mcp` with `Authorization: Bearer <token>`.

### MCP Read-Only Tools (13 Total)

| Category | Tools | Default Agent Ergonomics (`v0.4.0`) |
| --- | --- | --- |
| Account & Status | `list_accounts`, `select_account`, `get_status` | Populates `display_name`/`alias` and top-level `self`; `get_status` exposes `ready`, `watch_initialized`, `key_cache_mode`, and `audit_path`/`audit_calls_today`. |
| History & Search | `list_chats`, `get_messages`, `expand_chat_history`, `search_messages` | Unified `chat_type`, `name_query`, `chats`, `exclude_chats`; compact defaults to 450-char `head_tail` truncation, `ts_iso`, `untrusted: true`, and defused links. |
| Digest & Files | `digest`, `list_attachments` | `digest` adds `self`, `is_self` in `top_senders`, ISO buckets, and `since_iso`/`until_iso`; `list_attachments` resolves embedded forwarded files and sets `next_action` when missing locally. |
| Cursor Watch | `configure_watch`, `poll_new_messages`, `wait_for_messages`, `ack_messages` | Isolated per `cursor_id`; polling defaults to `peek=True` with `ack_token`; `ack_messages` replays are idempotent (`already_committed: true`). |

Notes on fields & compatibility:
- `ts_iso` provides timezone-aware ISO-8601 timestamps with year alongside short `t`; `seq` is the merged cross-shard millisecond-ordered sort key.
- Deprecated aliases (`query` on `list_chats`, `scope` on `digest`/`list_attachments`, and `initialized` on `get_status`) remain supported for backward compatibility.
- All error responses return machine-readable `retryable` (`bool`) and `next_action` recovery guidance. Multi-consumer setups should assign a dedicated `cursor_id` instead of sharing `"default"`.

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
