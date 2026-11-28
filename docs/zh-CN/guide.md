<p align="right">
  <a href="../../README.zh-CN.md">首页</a> · <a href="../en/guide.md">English</a> · <strong>简体中文</strong> · <a href="./architecture.md">工作原理</a>
</p>

# 使用指南

`wx-observer` 的完整安装步骤、CLI 子命令说明、MCP 配置方式与内置 Agent Skills 参考。

## 1. 运行环境与安装

环境要求：**64 位 Windows 10 / 11**、**64 位 Python 3.11+**、桌面端**微信 4.x（`Weixin.exe`）**。读取本机已登录账号时，工具须与微信运行在同一 Windows 用户和权限级别下；仅查看帮助或运行离线导出示例无需启动微信。

### 安装核心 CLI（4 个基础依赖）

```powershell
python -m venv .venv
python -m pip --python .\.venv\Scripts\python.exe install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\wx-observer.exe --help
```

> 若 `python -m venv .venv` 在精简 Python 环境中报 `ensurepip` 错误，请先用 `python -m venv --without-pip .venv` 创建虚拟环境，再直接执行第二行（`python -m pip --python ...`）引导安装 pip。

### 按需安装 MCP Server 依赖（`[mcp]`）

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
```

### 安装开发与离线测试依赖（`[dev]`）

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -v tests
```

<details>
<summary>让 AI 编程助手代为安装的提示词模板</summary>

> 请在当前 `wx-observer` 仓库（或从 `https://github.com/liyifu-2026/we-observer` 克隆）确认 64 位 Windows 与 Python 3.11+ 环境，创建独立 `.venv` 并用其中的 Python 执行 `pip install -e ".[mcp]"`；运行 `wx-observer --help` 验证入口，为当前客户端配置 `stdio` MCP 并把 `skills/wx-*` 复制到客户端 Skills 目录，最后告诉我实际路径与配置位置。

</details>

### 离线试用 Markdown 导出（合成数据，无需登录微信）

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

## 2. CLI 子命令参考

任意子命令后加 `--help` 可查看全部筛选参数（如 `wx-observer digest --help`）。全局参数（`--db-dir`、`--keys-file`、`--state-dir`、`--no-key-cache`、`--audit`、`--json`）支持放在子命令前或后。

| 子命令 | 核心用途 | 常用示例 |
| --- | --- | --- |
| `accounts` | 列出发现的微信数据目录与在线 `Weixin.exe` PID | `wx-observer accounts --json` |
| `verify` | 3 行自检：账号发现、分片解密/quick_check 校验与最新消息时间 | `wx-observer verify` |
| `doctor` | 导出脱敏诊断报告或 zip 包（含版本、分片清单、密钥来源、WAL 状态） | `wx-observer doctor --bundle doctor.zip` |
| `chats` | 按会话类型、名称、备注或 `chat_id` 列出并筛选会话 | `wx-observer chats --type group --name-query "读书"` |
| `history` | 提取会话历史，或导出为 `md` / `csv` / `json` / `jsonl` | `wx-observer history "<群名>" --since 7d --export md --output out.md` |
| `search` | 跨分片或指定会话检索（支持 `--chats`、`--exclude`、`--min-activity`） | `wx-observer search "第三章" --since 30d --compact --skip-noise` |
| `digest` | 单遍跨会话聚合：计数、外链、附件、`@提及` 与高信号摘要 | `wx-observer digest --since 24h --max-chats 20 --token-budget 4000` |
| `attachments` | 汇总聊天及转发记录内嵌文件，并核验本机明文路径 | `wx-observer attachments --since 7d --ext pdf,docx --only-available` |
| `watch` | 为独立 `--cursor-id` 持续流式监听新消息 | `wx-observer watch --cursor-id dev --mode group --jsonl` |
| `mcp` | 启动只读 MCP Server 或生成各客户端配置片段（`--print-config`） | `wx-observer mcp --print-config cursor` |

CLI 默认保留完整消息字段（`detail="full"`）。传 `--compact` 可缩短长文本（默认 450 字符 `head_tail` 截断，可用 `--truncate` 调整）并输出 `est_tokens`；传 `--skip-noise` 会跳过动画表情占位符，同时保留系统消息并标上 `noise: true`。

## 3. MCP 服务配置

### 生成各客户端可直接粘贴的配置（`--print-config`）

自动填充当前 `.venv` 的可执行文件路径与启动参数：

```powershell
.\.venv\Scripts\wx-observer.exe mcp --print-config cursor
.\.venv\Scripts\wx-observer.exe mcp --print-config claude
.\.venv\Scripts\wx-observer.exe mcp --print-config vscode
.\.venv\Scripts\wx-observer.exe mcp --print-config codex
.\.venv\Scripts\wx-observer.exe mcp --print-config cordis
.\.venv\Scripts\wx-observer.exe mcp --print-config generic
```

### `stdio` 模式（由客户端按需拉起）

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

### `Streamable HTTP` 模式（本机常驻共享）

适合多个客户端并发或高频轮询同一账号。请使用 `WX_OBSERVER_MCP_TOKEN` 环境变量或 `--token-file` 传入 Bearer 密钥，避免直接把 `--token` 暴露在进程命令行中：

```powershell
$env:WX_OBSERVER_MCP_TOKEN = "replace-with-your-secret"
.\.venv\Scripts\wx-observer.exe mcp --transport http --host 127.0.0.1 --port 8765
# 或从权限受控的文件读取：
.\.venv\Scripts\wx-observer.exe mcp --transport http --host 127.0.0.1 --port 8765 --token-file .\mcp-token.txt
```

客户端连接 `http://127.0.0.1:8765/mcp`，请求头携带 `Authorization: Bearer <token>`。

### 13 个 MCP 只读工具

| 类别 | 工具名 | `v0.4.0` 默认行为与契约 |
| --- | --- | --- |
| 账号与状态 | `list_accounts`、`select_account`、`get_status` | 自动填充 `display_name`/`alias` 与顶层 `self`；`get_status` 提供 `ready`、`watch_initialized`、`key_cache_mode` 与 `audit_path`/`audit_calls_today`。 |
| 会话与历史 | `list_chats`、`get_messages`、`expand_chat_history`、`search_messages` | 统一 `chat_type`/`name_query`/`chats`/`exclude_chats`；compact 默认 450 字符 `head_tail` 截断、输出 `ts_iso`、`untrusted: true` 并拆断 Markdown 链接。 |
| 摘要与附件 | `digest`、`list_attachments` | `digest` 返回 `self`、带 `is_self` 的 `top_senders`、ISO 时间桶与 `since_iso`/`until_iso`；`list_attachments` 解析转发内嵌文件并附 `next_action`。 |
| 增量游标 | `configure_watch`、`poll_new_messages`、`wait_for_messages`、`ack_messages` | 按 `cursor_id` 隔离进度；默认 `peek=True` 签发 `ack_token`，同 token 重试 `ack_messages` 幂等返回 `already_committed: true`。 |

字段与兼容性说明：
- 每条消息提供带年份和时区的 `ts_iso`（短 `t` 保留）；`seq` 为跨分片合并后的毫秒时间戳顺序键。
- 旧参数/字段别名（`list_chats.query`、`digest.scope`、`list_attachments.scope`、`get_status.initialized`）保留向后兼容。
- 所有异常均统一返回结构化的 `retryable` 与 `next_action` 恢复指引；多消费者请各自传入独立 `cursor_id`，不要共用 `"default"`。

## 4. 内置场景化 Agent Skills

仓库 [`skills/`](../../skills/) 下提供 4 个轻量 `SKILL.md`，规范调用顺序、预算边界与 CLI 回退：

| Skill | 适用场景 | 推荐链路 |
| --- | --- | --- |
| [`wx-brief`](../../skills/wx-brief/SKILL.md) | 群日报/周报与漏看动态速览 | 4,000-token `digest` 聚合 → 定向 `get_messages` 补上下文 |
| [`wx-find`](../../skills/wx-find/SKILL.md) | 跨会话找原话、外链与本地附件 | `search_messages` + `list_attachments` → `before_seq` 溯源 + 直读 `local_path` |
| [`wx-todo`](../../skills/wx-todo/SKILL.md) | 提取 `@我` 请求、DDL 与未收尾待办 | 聚合提及/附件/重点 → 按对话上下文核验是否闭环 |
| [`wx-watch`](../../skills/wx-watch/SKILL.md) | 独立游标实时盯盘与分诊提醒 | 独立 `cursor_id` + `peek=True` → 处理完成再幂等 `ack_messages` |

按需将 `skills/wx-*` 复制到客户端 Skills 目录即可（例如 `~/.agents/skills/` 或 `~/.claude/skills/`）：

```powershell
Copy-Item -Recurse -Force .\skills\wx-* "$HOME\.agents\skills\"
```
