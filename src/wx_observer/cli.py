"""Command-line interface for wx-observer."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Sequence

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
        print(f"- {acct['account_id']} ({acct['wxid']}) [{status}]{sel}")
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


def _print_event_line(ev: dict) -> None:
    ts = _format_ts(int(ev.get("timestamp") or 0))
    chat_label = ev.get("chat_name") or ev.get("chat_id")
    sender = ev.get("sender_name") or ev.get("sender_id") or ("Me" if ev.get("is_self") else "-")
    tag = "GROUP" if ev.get("chat_type") == "group" else "DM"
    quote = ev.get("quote")
    q_part = ""
    if isinstance(quote, dict) and quote.get("excerpt"):
        q_part = f" [replying to: {quote['excerpt']}]"
    print(f"[{ts}] [{tag} {chat_label}] {sender} ({ev.get('msg_type')}): {ev.get('text')}{q_part}")


def _add_common_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    kwargs = {"default": argparse.SUPPRESS} if suppress_defaults else {"default": None}
    json_kwargs = {"default": argparse.SUPPRESS} if suppress_defaults else {"default": False}
    parser.add_argument("--db-dir", help="Explicit WeChat xwechat_files root directory", **kwargs)
    parser.add_argument("--keys-file", help="Optional JSON file containing SQLCipher keys or master_key", **kwargs)
    parser.add_argument("--state-dir", help="Override persistent state/key-cache directory", **kwargs)
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output", **json_kwargs)


def _build_parser() -> argparse.ArgumentParser:
    common_sub = argparse.ArgumentParser(add_help=False)
    _add_common_flags(common_sub, suppress_defaults=True)

    parser = argparse.ArgumentParser(
        prog="wx-observer",
        description="Read-only Windows WeChat 4.x local message history extractor and real-time observer.",
    )
    _add_common_flags(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts", parents=[common_sub], help="List running Weixin.exe processes and local accounts")

    p_chats = sub.add_parser("chats", parents=[common_sub], help="List private and group chats")
    p_chats.add_argument("--account", default=None, help="Target account_id or wxid")
    p_chats.add_argument("--pid", type=int, default=None, help="Target Weixin.exe PID")
    p_chats.add_argument("--type", dest="chat_type", choices=["all", "private", "group"], default="all")
    p_chats.add_argument("--query", default=None, help="Fuzzy match on chat_id, nickname, remark, or group name")
    p_chats.add_argument("--limit", type=int, default=50)

    p_hist = sub.add_parser("history", parents=[common_sub], help="Extract message history for a chat")
    p_hist.add_argument("chat", help="Conversation wxid, @chatroom id, remark, nickname, or group name")
    p_hist.add_argument("--account", default=None)
    p_hist.add_argument("--pid", type=int, default=None)
    p_hist.add_argument("--limit", type=int, default=50)
    p_hist.add_argument("--before-seq", type=int, default=None)
    p_hist.add_argument("--after-seq", type=int, default=None)
    p_hist.add_argument("--since", default=None, help="Start time (epoch seconds or ISO-8601)")
    p_hist.add_argument("--until", default=None, help="End time (epoch seconds or ISO-8601)")
    p_hist.add_argument("--type", action="append", dest="msg_types", help="Filter message types (comma-separated)")
    p_hist.add_argument("--sender", default=None, help="Filter by sender wxid or display name")
    p_hist.add_argument("--keyword", default=None, help="Filter messages containing keyword")
    p_hist.add_argument("--exclude-self", action="store_true", help="Exclude messages sent by this account")
    p_hist.add_argument("--export", dest="export_fmt", choices=["json", "jsonl", "csv", "md"], default=None)
    p_hist.add_argument("--output", default=None, help="Write exported messages to file path")

    p_search = sub.add_parser("search", parents=[common_sub], help="Search messages across chats")
    p_search.add_argument("keyword", help="Search keyword")
    p_search.add_argument("--chat", default=None, help="Restrict search to one conversation")
    p_search.add_argument("--account", default=None)
    p_search.add_argument("--pid", type=int, default=None)
    p_search.add_argument("--type", dest="chat_type", choices=["all", "private", "group"], default="all")
    p_search.add_argument("--since", default=None)
    p_search.add_argument("--until", default=None)
    p_search.add_argument("--limit", type=int, default=50)

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
    p_mcp.add_argument("--token", default=None, help="Optional Bearer token for HTTP transport")
    p_mcp.add_argument("--account", default=None)
    p_mcp.add_argument("--pid", type=int, default=None)

    p_ui = sub.add_parser("ui", parents=[common_sub], help="Launch the minimal local read-only Web UI")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=8766)
    p_ui.add_argument("--token", default=None, help="Optional access token for Web UI APIs")
    p_ui.add_argument("--account", default=None)
    p_ui.add_argument("--pid", type=int, default=None)
    p_ui.add_argument("--no-open", action="store_true", help="Do not open browser automatically")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)

    service = ObserverService(
        db_dir=args.db_dir,
        keys_file=args.keys_file,
        state_dir=args.state_dir,
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
                    _print_event_line(ev)
            return 0

        if cmd == "search":
            res = service.search_messages(
                args.keyword,
                chat=args.chat,
                chat_type=args.chat_type,
                since=args.since,
                until=args.until,
                limit=args.limit,
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
            from wx_observer.mcp_server import run_mcp_server

            run_mcp_server(
                service,
                transport=args.transport,
                host=args.host,
                port=args.port,
                token=args.token,
                account=args.account,
                pid=args.pid,
            )
            return 0

        if cmd == "ui":
            from wx_observer.web_ui import run_web_ui

            run_web_ui(
                service,
                host=args.host,
                port=args.port,
                token=args.token,
                account=args.account,
                pid=args.pid,
                open_browser=not args.no_open,
            )
            return 0

        parser.print_help()
        return 1
    except WxObserverError as exc:
        if getattr(args, "as_json", False):
            sys.stderr.write(format_json(exc.to_error_dict()) + "\n")
        else:
            sys.stderr.write(f"Error [{exc.code}]: {exc.message}\n")
        return 2
    finally:
        service.close()
