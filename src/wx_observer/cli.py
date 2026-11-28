"""Command-line interface for wx-observer."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from wx_observer import __version__
from wx_observer.exceptions import WxObserverError
from wx_observer.exporter import export_messages, format_json, format_jsonl
from wx_observer.models import WatchMode
from wx_observer.service import ObserverService


def _configure_utf8_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _split_csv_arg(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for item in raw.split(","):
            stripped = item.strip()
            if stripped:
                out.append(stripped)
    return out


def _format_ts(ts: int) -> str:
    if ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _print_accounts_table(payload: dict) -> None:
    accounts = payload.get("accounts") or []
    if not accounts:
        print(payload.get("reason") or "No WeChat 4.x accounts detected.")
        return
    print(f"Data dir: {payload.get('db_dir') or '-'}")
    for acct in accounts:
        status = f"ONLINE pids={acct['pids']}" if acct.get("online") else "OFFLINE"
        sel = " *" if acct.get("selected") else ""
        name = acct.get("display_name") or ""
        name_part = f" [{name}]" if name else ""
        print(f"- {acct['account_id']} ({acct['wxid']}){name_part} [{status}]{sel}")
    if payload.get("reason"):
        print(f"Note: {payload['reason']}")


def _print_chats_table(payload: dict) -> None:
    chats = payload.get("chats") or []
    if not chats:
        print("No matching chats found.")
        return
    for c in chats:
        ts = _format_ts(int(c.get("last_timestamp") or 0))
        tag = "GROUP" if c.get("chat_type") == "group" else "DM"
        print(f"[{tag}] {c['display_name']} ({c['chat_id']}) | last={ts} | unread={c.get('unread_count', 0)}")


def _print_event_line(ev: dict, *, fallback_chat: str = "") -> None:
    if "t" in ev and "timestamp" not in ev:
        ts = ev.get("t") or "-"
        chat_label = ev.get("chat") or ev.get("chat_id") or fallback_chat or "-"
        sender = ev.get("sender") or ("Me" if ev.get("self") else "-")
        msg_type = ev.get("type") or "-"
        quote = ev.get("quote")
        q_part = f" [replying to: {quote}]" if isinstance(quote, str) and quote else ""
        print(f"[{ts}] [{chat_label}] {sender} ({msg_type}): {ev.get('text')}{q_part}")
        return
    ts = _format_ts(int(ev.get("timestamp") or 0))
    chat_label = ev.get("chat_name") or ev.get("chat_id") or fallback_chat
    sender = ev.get("sender_name") or ev.get("sender_id") or ("Me" if ev.get("is_self") else "-")
    tag = "GROUP" if ev.get("chat_type") == "group" else "DM"
    quote = ev.get("quote")
    q_part = ""
    if isinstance(quote, dict) and quote.get("excerpt"):
        q_part = f" [replying to: {quote['excerpt']}]"
    print(f"[{ts}] [{tag} {chat_label}] {sender} ({ev.get('msg_type')}): {ev.get('text')}{q_part}")


def _print_digest_summary(payload: dict) -> None:
    summary = payload.get("summary") or {}
    win = payload.get("window") or {}
    chat_scope = win.get("chat_type") or win.get("scope")
    print(
        f"Digest account={payload.get('account_id')} chat_type={chat_scope} "
        f"active_chats={summary.get('active_chats', 0)} total={summary.get('total_messages', 0)} "
        f"signal={summary.get('signal_messages', 0)} noise={summary.get('noise_messages', 0)} "
        f"omitted_chats={summary.get('omitted_chats', 0)} trimmed={summary.get('budget_trimmed', False)} "
        f"est_tokens={payload.get('est_tokens', 0)}"
    )
    for chat in payload.get("per_chat") or []:
        print(
            f"- [{chat.get('chat_type')}] {chat.get('chat')} ({chat.get('chat_id')}): "
            f"total={chat.get('total', 0)} signal={chat.get('signal_count', 0)}"
        )
        for hi in chat.get("highlights") or []:
            print(f"    [{hi.get('t')}] {hi.get('sender')} ({hi.get('type')}): {hi.get('text')}")
    attachments = payload.get("attachments") or []
    if attachments:
        print(f"Attachments ({len(attachments)}):")
        for att in attachments[:10]:
            flag = "OK" if att.get("available") else "MISSING"
            path_part = f" -> {att['local_path']}" if att.get("local_path") else ""
            print(f"  - [{flag}] {att.get('filename')} ({att.get('chat')} @ {att.get('t')}){path_part}")
    links = payload.get("links") or []
    if links:
        print(f"Links ({len(links)}):")
        for link in links[:10]:
            label = f"{link['title']} - " if link.get("title") else ""
            print(f"  - {label}{link.get('url')} ({link.get('chat')})")


def _print_attachments_table(payload: dict) -> None:
    items = payload.get("attachments") or []
    print(
        f"Attachments account={payload.get('account_id')} count={payload.get('count', 0)} "
        f"available={payload.get('available_count', 0)} est_tokens={payload.get('est_tokens', 0)}"
    )
    if not items:
        print("No matching attachments.")
        return
    for item in items:
        flag = "AVAILABLE" if item.get("available") else "MISSING"
        conf = f" [{item['match_confidence']}]" if item.get("match_confidence") else ""
        size = item.get("size_bytes")
        size_part = f" {size}B" if size is not None else ""
        path_part = f"\n    path: {item['local_path']}" if item.get("local_path") else ""
        print(
            f"- [{flag}{conf}] {item.get('filename')}{size_part} | {item.get('t')} | "
            f"{item.get('chat')} | {item.get('sender')}{path_part}"
        )


def _warn_broad_token_file_permissions(path: Path) -> None:
    if os.name == "nt":
        try:
            import win32security  # type: ignore

            sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
            dacl = sd.GetSecurityDescriptorDacl()
            if dacl is None:
                return
            broad_sids = {
                win32security.ConvertSidToStringSid(win32security.CreateWellKnownSid(sid_type))
                for sid_type in (
                    win32security.WinWorldSid,
                    win32security.WinBuiltinUsersSid,
                    win32security.WinAuthenticatedUserSid,
                )
            }
            for idx in range(dacl.GetAceCount()):
                ace = dacl.GetAce(idx)
                ace_type = ace[0][0]
                if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
                    continue
                sid_str = win32security.ConvertSidToStringSid(ace[2])
                if sid_str in broad_sids:
                    sys.stderr.write(
                        f"Warning: token file '{path}' grants access to broad Windows group ({sid_str}); "
                        "consider restricting ACLs with icacls.\n"
                    )
                    return
        except Exception:
            return
    else:
        try:
            mode = path.stat().st_mode
            if mode & 0o077:
                sys.stderr.write(
                    f"Warning: token file '{path}' is accessible by group/other users; run chmod 600.\n"
                )
        except OSError:
            return


def _resolve_mcp_token(args: argparse.Namespace) -> str | None:
    if args.token and args.token_file:
        raise WxObserverError(
            "invalid_token_config",
            "Specify either --token-file or --token, not both.",
        )
    if args.token_file:
        tp = Path(args.token_file)
        if not tp.is_file():
            raise WxObserverError(
                "token_file_not_found",
                f"Bearer token file does not exist: {tp}",
                {"token_file": str(tp)},
            )
        _warn_broad_token_file_permissions(tp)
        try:
            raw = tp.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise WxObserverError(
                "invalid_token_file",
                f"Failed to read token file {tp}: {exc}",
            ) from exc
        if not raw:
            raise WxObserverError(
                "invalid_token_file",
                f"Token file is empty: {tp}",
            )
        return raw.splitlines()[0].strip()
    if args.token:
        sys.stderr.write(
            "Warning: passing --token on the command line exposes the Bearer secret in process listings; "
            "prefer WX_OBSERVER_MCP_TOKEN or --token-file.\n"
        )
        return str(args.token)
    env_tok = os.environ.get("WX_OBSERVER_MCP_TOKEN", "").strip()
    return env_tok or None


def _resolve_mcp_command(args: argparse.Namespace) -> tuple[str, list[str]]:
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [exe_dir / "wx-observer.exe", exe_dir / "wx-observer"]
    command = ""
    cmd_args: list[str] = []
    for cand in candidates:
        if cand.is_file():
            command = str(cand)
            cmd_args = ["mcp", "--transport", "stdio"]
            break
    if not command:
        command = str(Path(sys.executable).resolve())
        cmd_args = ["-m", "wx_observer", "mcp", "--transport", "stdio"]

    if getattr(args, "db_dir", None):
        cmd_args.extend(["--db-dir", str(args.db_dir)])
    if getattr(args, "keys_file", None):
        cmd_args.extend(["--keys-file", str(args.keys_file)])
    if getattr(args, "state_dir", None):
        cmd_args.extend(["--state-dir", str(args.state_dir)])
    if getattr(args, "no_key_cache", False):
        cmd_args.append("--no-key-cache")
    if getattr(args, "audit", None) is not None:
        cmd_args.extend(["--audit", str(args.audit)])
    if getattr(args, "account", None):
        cmd_args.extend(["--account", str(args.account)])
    return command, cmd_args


def _render_print_config(client: str, args: argparse.Namespace) -> tuple[str, str | None]:
    command, cmd_args = _resolve_mcp_command(args)
    entry_mcp_servers = {"mcpServers": {"wx-observer": {"command": command, "args": cmd_args}}}
    entry_vscode = {"servers": {"wx-observer": {"type": "stdio", "command": command, "args": cmd_args}}}

    if client == "cursor":
        return format_json(entry_mcp_servers), "Target file: ~/.cursor/mcp.json (or .cursor/mcp.json)"
    if client == "claude":
        return format_json(entry_mcp_servers), "Target file: %APPDATA%\\Claude\\claude_desktop_config.json (or ~/.claude.json)"
    if client == "vscode":
        return format_json(entry_vscode), "Target file: .vscode/mcp.json"
    if client == "generic":
        return format_json(entry_mcp_servers), None
    if client == "codex":
        import json as _json

        toml = (
            "[mcp_servers.wx-observer]\n"
            f"command = {_json.dumps(command)}\n"
            f"args = {_json.dumps(cmd_args)}\n"
        )
        return toml.rstrip(), "Target file: ~/.codex/config.toml"

    # cordis
    notes = (
        "# Cordis plugin-style MCP client patch instructions:\n"
        "# Cordis patch files require an `insert:` wrapper when adding a new MCP instance;\n"
        "# writing a bare id without `insert:` triggers `patch: entry \"wx-observer\" not found` and skips silently.\n"
        "# Equivalent generic MCP JSON entry:\n"
        + format_json(entry_mcp_servers)
    )
    return notes, None


def _print_verify_summary(res: dict[str, Any]) -> None:
    print(
        f"Accounts: {res['accounts_total']} (online {res['accounts_online']} / offline {res['accounts_offline']}) "
        f"| active={res['account_id']} | key_cache={res['key_cache_mode']}"
    )
    qc = "passed" if res.get("quick_check_passed") else "FAILED"
    req_ok = res.get("verified_required_dbs", res.get("verified_dbs", 0))
    req_tot = res.get("required_dbs", res.get("total_dbs", 0))
    print(
        f"Decrypt: {req_ok}/{req_tot} required DBs ok "
        f"({res['verified_dbs']}/{res['total_dbs']} total, WAL merged {res['wal_frames_merged']} frames, quick_check {qc})"
    )
    print(f"Latest message: {res.get('latest_iso') or '-'} (chats={res.get('chats_count', 0)})")
    failed_opt = res.get("failed_optional") or []
    if failed_opt:
        print(f"Optional skipped/failed ({len(failed_opt)}): {', '.join(failed_opt[:5])}")
    if not res.get("ok") and res.get("next_action"):
        print(f"next_action: {res['next_action']}")


def _print_doctor_summary(res: dict[str, Any]) -> None:
    env = res.get("environment") or {}
    disc = res.get("discovery") or {}
    print(
        f"wx-observer {res.get('wx_observer_version')} | Python {env.get('python_version')} ({env.get('python_bits')}-bit) "
        f"| WeChat={env.get('wechat_version') or 'unknown'} | OS={env.get('os')}"
    )
    print(
        f"Discovery: db_dir={disc.get('db_dir_detected')} accounts={disc.get('accounts_count', 0)} "
        f"(online={disc.get('online_accounts_count', 0)}) | "
        f"DBs={res.get('keyed_dbs', 0)}/{res.get('total_dbs', 0)} keyed, {res.get('verified_dbs', 0)} verified "
        f"| key_cache={res.get('key_cache_mode')} | residual_tmp={res.get('temp_dir', {}).get('residual_session_dirs', 0)}"
    )
    if res.get("bundle_path"):
        print(f"Bundle written: {res['bundle_path']}")
    if res.get("repo_error"):
        err = res["repo_error"]
        print(f"Error [{err.get('error')}]: {err.get('message')}")
        if err.get("next_action"):
            print(f"next_action: {err['next_action']}")


def _add_common_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    kwargs = {"default": argparse.SUPPRESS} if suppress_defaults else {"default": None}
    bool_kwargs = {"default": argparse.SUPPRESS} if suppress_defaults else {"default": False}
    parser.add_argument("--db-dir", help="Explicit WeChat xwechat_files root directory", **kwargs)
    parser.add_argument("--keys-file", help="Optional JSON file containing SQLCipher keys or master_key", **kwargs)
    parser.add_argument("--state-dir", help="Override persistent state/key-cache directory", **kwargs)
    parser.add_argument(
        "--no-key-cache",
        action="store_true",
        dest="no_key_cache",
        help="Disable DPAPI key cache read/write (scan process memory or use --keys-file only)",
        **bool_kwargs,
    )
    parser.add_argument(
        "--audit",
        metavar="off|on|PATH",
        help="Configure audit.jsonl logging ('on', 'off', or custom JSONL path; default on)",
        **kwargs,
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output", **bool_kwargs)


def _build_parser() -> argparse.ArgumentParser:
    common_sub = argparse.ArgumentParser(add_help=False)
    _add_common_flags(common_sub, suppress_defaults=True)

    parser = argparse.ArgumentParser(
        prog="wx-observer",
        description="Read-only Windows WeChat 4.x local message history extractor and real-time observer.",
    )
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    _add_common_flags(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts", parents=[common_sub], help="List running Weixin.exe processes and local accounts")

    p_chats = sub.add_parser("chats", parents=[common_sub], help="List private and group chats")
    p_chats.add_argument("--account", default=None, help="Target account_id or wxid")
    p_chats.add_argument("--pid", type=int, default=None, help="Target Weixin.exe PID")
    p_chats.add_argument("--type", dest="chat_type", choices=["all", "private", "group"], default="all")
    p_chats.add_argument("--name-query", dest="name_query", default=None, help="Fuzzy match on chat_id, nickname, remark, or group name")
    p_chats.add_argument("--query", default=None, help="Deprecated alias for --name-query")
    p_chats.add_argument("--limit", type=int, default=50)

    p_hist = sub.add_parser("history", parents=[common_sub], help="Extract message history for a chat")
    p_hist.add_argument("chat", help="Conversation wxid, @chatroom id, remark, nickname, or group name")
    p_hist.add_argument("--account", default=None)
    p_hist.add_argument("--pid", type=int, default=None)
    p_hist.add_argument("--limit", type=int, default=50)
    p_hist.add_argument("--before-seq", type=int, default=None)
    p_hist.add_argument("--after-seq", type=int, default=None)
    p_hist.add_argument("--since", default=None, help="Start time (relative e.g. '24h'/'7d', epoch seconds, or ISO-8601)")
    p_hist.add_argument("--until", default=None, help="End time (relative, epoch seconds, or ISO-8601)")
    p_hist.add_argument("--type", action="append", dest="msg_types", help="Filter message types (comma-separated)")
    p_hist.add_argument("--sender", default=None, help="Filter by sender wxid or display name")
    p_hist.add_argument("--keyword", default=None, help="Filter messages containing keyword")
    p_hist.add_argument("--exclude-self", action="store_true", help="Exclude messages sent by this account")
    p_hist.add_argument("--compact", action="store_true", help="Emit compact token-efficient message schema")
    p_hist.add_argument("--max-chars", type=int, default=None, dest="max_chars", help="Truncate message text to N chars")
    p_hist.add_argument("--truncate", choices=["head", "tail", "head_tail", "none"], default="head_tail", help="Truncation strategy")
    p_hist.add_argument("--skip-noise", action="store_true", help="Skip emoji/sticker and system notice messages")
    p_hist.add_argument("--export", dest="export_fmt", choices=["json", "jsonl", "csv", "md"], default=None)
    p_hist.add_argument("--output", default=None, help="Write exported messages to file path")

    p_search = sub.add_parser("search", parents=[common_sub], help="Search messages across chats")
    p_search.add_argument("keyword", help="Search keyword")
    p_search.add_argument("--chat", default=None, help="Restrict search to one conversation")
    p_search.add_argument("--chats", action="append", help="Restrict search to specific chats (comma-separated or repeated)")
    p_search.add_argument("--exclude", action="append", help="Exclude specific chats (comma-separated or repeated)")
    p_search.add_argument("--account", default=None)
    p_search.add_argument("--pid", type=int, default=None)
    p_search.add_argument("--type", dest="chat_type", choices=["all", "private", "group"], default="all")
    p_search.add_argument("--since", default=None)
    p_search.add_argument("--until", default=None)
    p_search.add_argument("--min-activity", default=None, dest="min_activity", help="Only scan chats active within N days or window")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--compact", action="store_true", help="Emit compact keyword snippets")
    p_search.add_argument("--max-chars", type=int, default=None, dest="max_chars", help="Truncate/snippet window width")
    p_search.add_argument("--skip-noise", action="store_true", help="Skip emoji/sticker and system notice messages")

    p_watch = sub.add_parser("watch", parents=[common_sub], help="Stream newly arriving messages in real time")
    p_watch.add_argument("--account", default=None)
    p_watch.add_argument("--pid", type=int, default=None)
    p_watch.add_argument("--cursor-id", default="default", help="Independent consumer cursor identifier")
    p_watch.add_argument("--mode", choices=["all", "private", "group", "allowlist"], default="all")
    p_watch.add_argument("--chats", action="append", help="Allowed chats (comma-separated or repeated)")
    p_watch.add_argument("--exclude", action="append", help="Excluded chats (comma-separated or repeated)")
    p_watch.add_argument("--include-self", action="store_true", help="Include messages sent by self")
    p_watch.add_argument("--types", action="append", dest="msg_types", help="Filter watched message types")
    p_watch.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds")
    p_watch.add_argument("--reset-cursor", action="store_true", help="Reset cursor baseline to current latest")
    p_watch.add_argument("--jsonl", action="store_true", help="Stream one JSON object per line")
    p_watch.add_argument("--max-events", type=int, default=None, help="Stop after emitting N events")
    p_watch.add_argument("--duration-seconds", type=float, default=None, help="Stop after N seconds")

    p_mcp = sub.add_parser("mcp", parents=[common_sub], help="Start the wx-observer MCP server")
    p_mcp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p_mcp.add_argument("--host", default="127.0.0.1")
    p_mcp.add_argument("--port", type=int, default=8765)
    p_mcp.add_argument("--token", default=None, help="Deprecated: Bearer token via CLI (prefer WX_OBSERVER_MCP_TOKEN or --token-file)")
    p_mcp.add_argument("--token-file", dest="token_file", default=None, help="Read HTTP Bearer token from file")
    p_mcp.add_argument(
        "--print-config",
        dest="print_config",
        choices=["cursor", "claude", "vscode", "codex", "cordis", "generic"],
        default=None,
        help="Print ready-to-paste MCP client configuration and exit",
    )
    p_mcp.add_argument("--account", default=None)
    p_mcp.add_argument("--pid", type=int, default=None)

    p_digest = sub.add_parser("digest", parents=[common_sub], help="Build a compact cross-chat digest")
    p_digest.add_argument("--type", "--chat-type", dest="chat_type", choices=["all", "private", "group"], default=None)
    p_digest.add_argument("--scope", choices=["all", "private", "group"], default=None, help="Deprecated alias for --type")
    p_digest.add_argument("--chats", action="append", help="Restrict digest to specific chats (comma-separated or repeated)")
    p_digest.add_argument("--exclude", action="append", help="Exclude specific chats (comma-separated or repeated)")
    p_digest.add_argument("--since", default="24h", help="Window start (e.g. '24h', '7d', ISO-8601, or epoch)")
    p_digest.add_argument("--until", default=None, help="Window end")
    p_digest.add_argument("--buckets", choices=["hour", "day", "none"], default="hour")
    p_digest.add_argument("--include", action="append", help="Sections (counts,top_senders,links,attachments,mentions,highlights)")
    p_digest.add_argument("--highlights", type=int, default=8, dest="highlights", help="Max highlight items per chat")
    p_digest.add_argument("--max-chars", type=int, default=160, dest="max_chars", help="Max highlight text length")
    p_digest.add_argument("--max-chats", type=int, default=20, dest="max_chats", help="Max per-chat sections to include (1-200)")
    p_digest.add_argument("--token-budget", type=int, default=4000, dest="token_budget", help="Target output est_tokens cap (0 disables)")
    p_digest.add_argument("--exclude-self", action="store_true", help="Exclude messages sent by this account")
    p_digest.add_argument("--account", default=None)
    p_digest.add_argument("--pid", type=int, default=None)

    p_attach = sub.add_parser("attachments", parents=[common_sub], help="List file attachments and local plaintext paths")
    p_attach.add_argument("--type", "--chat-type", dest="chat_type", choices=["all", "private", "group"], default=None)
    p_attach.add_argument("--scope", choices=["all", "private", "group"], default=None, help="Deprecated alias for --type")
    p_attach.add_argument("--chats", action="append", help="Restrict search to specific chats (comma-separated or repeated)")
    p_attach.add_argument("--exclude", action="append", help="Exclude specific chats (comma-separated or repeated)")
    p_attach.add_argument("--since", default="7d", help="Window start (e.g. '7d', '24h', ISO-8601, or epoch)")
    p_attach.add_argument("--until", default=None)
    p_attach.add_argument("--ext", action="append", dest="ext", help="Filter file extensions (e.g. pdf,docx)")
    p_attach.add_argument("--only-available", action="store_true", help="Only list attachments found on local disk")
    p_attach.add_argument("--no-embedded", action="store_true", help="Exclude files inside forwarded chat_history records")
    p_attach.add_argument("--limit", type=int, default=50)
    p_attach.add_argument("--account", default=None)
    p_attach.add_argument("--pid", type=int, default=None)

    p_verify = sub.add_parser("verify", parents=[common_sub], help="Verify account discovery, decryption, and DB health")
    p_verify.add_argument("--account", default=None)
    p_verify.add_argument("--pid", type=int, default=None)
    p_verify.add_argument(
        "--strict",
        action="store_true",
        help="Fail with exit code 2 if any optional/FTS database also fails quick_check",
    )

    p_doctor = sub.add_parser("doctor", parents=[common_sub], help="Generate redacted diagnostic info or zip bundle")
    p_doctor.add_argument("--account", default=None)
    p_doctor.add_argument("--pid", type=int, default=None)
    p_doctor.add_argument("--bundle", default=None, help="Write redacted diagnostic zip bundle to path")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "mcp" and args.print_config:
        rendered, hint = _render_print_config(args.print_config, args)
        if args.as_json:
            command, cmd_args = _resolve_mcp_command(args)
            print(
                format_json(
                    {
                        "ok": True,
                        "client": args.print_config,
                        "command": command,
                        "args": cmd_args,
                        "hint": hint,
                        "rendered": rendered,
                    }
                )
            )
        else:
            print(rendered)
            if hint:
                sys.stderr.write(hint + "\n")
        return 0

    service = ObserverService(
        db_dir=args.db_dir,
        keys_file=args.keys_file,
        state_dir=args.state_dir,
        use_key_cache=(False if args.no_key_cache else None),
        audit=args.audit,
    )
    try:
        cmd = args.command
        if cmd == "accounts":
            res = service.list_accounts()
            if args.as_json:
                print(format_json(res))
            else:
                _print_accounts_table(res)
            return 0

        if cmd == "chats":
            res = service.list_chats(
                chat_type=args.chat_type,
                name_query=args.name_query,
                query=args.query,
                limit=args.limit,
                account=args.account,
                pid=args.pid,
            )
            if args.as_json:
                print(format_json(res))
            else:
                _print_chats_table(res)
            return 0

        if cmd == "history":
            res = service.get_messages(
                args.chat,
                limit=args.limit,
                before_seq=args.before_seq,
                after_seq=args.after_seq,
                since=args.since,
                until=args.until,
                msg_types=_split_csv_arg(args.msg_types) or None,
                sender=args.sender,
                keyword=args.keyword,
                include_self=not args.exclude_self,
                detail="compact" if args.compact else "full",
                max_text_chars=args.max_chars,
                truncate=args.truncate,
                skip_noise=bool(args.skip_noise),
                account=args.account,
                pid=args.pid,
            )
            if args.export_fmt or args.output:
                fmt = args.export_fmt or "json"
                rendered = export_messages(
                    res["messages"],
                    fmt,
                    output_path=args.output,
                    title=res["chat"]["display_name"],
                )
                if args.output:
                    if args.as_json:
                        print(
                            format_json(
                                {
                                    "ok": True,
                                    "exported": len(res["messages"]),
                                    "format": fmt,
                                    "output": args.output,
                                }
                            )
                        )
                    else:
                        print(f"Exported {len(res['messages'])} messages ({fmt}) to {args.output}")
                else:
                    sys.stdout.write(rendered)
            elif args.as_json:
                print(format_json(res))
            else:
                if not res["messages"]:
                    print("No matching messages.")
                for ev in res["messages"]:
                    _print_event_line(ev, fallback_chat=res["chat"]["display_name"])
            return 0

        if cmd == "search":
            res = service.search_messages(
                args.keyword,
                chat=args.chat,
                chats=_split_csv_arg(args.chats) or None,
                exclude_chats=_split_csv_arg(args.exclude) or None,
                chat_type=args.chat_type,
                since=args.since,
                until=args.until,
                min_activity=args.min_activity,
                limit=args.limit,
                detail="compact" if args.compact else "full",
                max_text_chars=args.max_chars,
                skip_noise=bool(args.skip_noise),
                account=args.account,
                pid=args.pid,
            )
            if args.as_json:
                print(format_json(res))
            else:
                if not res["messages"]:
                    print("No matching messages.")
                for ev in res["messages"]:
                    _print_event_line(ev)
            return 0

        if cmd == "digest":
            res = service.digest(
                chat_type=args.chat_type,
                scope=args.scope,
                chats=_split_csv_arg(args.chats) or None,
                exclude_chats=_split_csv_arg(args.exclude) or None,
                since=args.since,
                until=args.until,
                buckets=args.buckets,
                include=_split_csv_arg(args.include) or None,
                max_highlights_per_chat=args.highlights,
                max_text_chars=args.max_chars,
                max_chats=args.max_chats,
                token_budget=args.token_budget,
                include_self=not args.exclude_self,
                account=args.account,
                pid=args.pid,
            )
            if args.as_json:
                print(format_json(res))
            else:
                _print_digest_summary(res)
            return 0

        if cmd == "attachments":
            res = service.list_attachments(
                chat_type=args.chat_type,
                scope=args.scope,
                chats=_split_csv_arg(args.chats) or None,
                exclude_chats=_split_csv_arg(args.exclude) or None,
                since=args.since,
                until=args.until,
                extensions=_split_csv_arg(args.ext) or None,
                only_available=bool(args.only_available),
                include_embedded=not args.no_embedded,
                limit=args.limit,
                account=args.account,
                pid=args.pid,
            )
            if args.as_json:
                print(format_json(res))
            else:
                _print_attachments_table(res)
            return 0

        if cmd == "verify":
            res = service.verify(account=args.account, pid=args.pid, strict=bool(args.strict))
            if args.as_json:
                print(format_json(res))
            else:
                _print_verify_summary(res)
            return int(res.get("exit_code", 0 if res.get("ok") else 2))

        if cmd == "doctor":
            res = service.doctor(account=args.account, pid=args.pid, bundle_path=args.bundle)
            if args.as_json:
                print(format_json(res))
            else:
                _print_doctor_summary(res)
            return 0 if res.get("ok") else 2

        if cmd == "watch":
            mode: WatchMode = args.mode
            service.configure_watch(
                cursor_id=args.cursor_id,
                mode=mode,
                chats=_split_csv_arg(args.chats) or None,
                exclude_chats=_split_csv_arg(args.exclude) or None,
                include_self=bool(args.include_self),
                msg_types=_split_csv_arg(args.msg_types) or None,
                reset_cursor=bool(args.reset_cursor),
                account=args.account,
                pid=args.pid,
            )
            use_jsonl = bool(args.jsonl or args.as_json)
            try:
                for ev in service.watch_stream(
                    cursor_id=args.cursor_id,
                    poll_interval=args.interval,
                    max_events=args.max_events,
                    duration_seconds=args.duration_seconds,
                    account=args.account,
                    pid=args.pid,
                ):
                    payload = ev.to_dict()
                    if use_jsonl:
                        sys.stdout.write(format_jsonl([payload]))
                        sys.stdout.flush()
                    else:
                        _print_event_line(payload)
                        sys.stdout.flush()
            except KeyboardInterrupt:
                return 0
            return 0

        if cmd == "mcp":
            eff_token = _resolve_mcp_token(args)
            try:
                from wx_observer.mcp_server import run_mcp_server
            except ModuleNotFoundError as exc:
                raise WxObserverError(
                    "mcp_not_installed",
                    'MCP dependencies are not installed. Install with: pip install -e ".[mcp]"',
                ) from exc

            run_mcp_server(
                service,
                transport=args.transport,
                host=args.host,
                port=args.port,
                token=eff_token,
                account=args.account,
                pid=args.pid,
            )
            return 0

        parser.print_help()
        return 1
    except WxObserverError as exc:
        if getattr(args, "as_json", False):
            sys.stderr.write(format_json(exc.to_error_dict()) + "\n")
        else:
            sys.stderr.write(f"Error [{exc.code}]: {exc.message}\n")
            if exc.next_action:
                sys.stderr.write(f"next_action: {exc.next_action}\n")
        return 2
    finally:
        service.close()
