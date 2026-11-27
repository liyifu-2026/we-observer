"""Official MCP server (stdio & 127.0.0.1 Streamable HTTP) for wx-observer."""

from __future__ import annotations

import hmac
import json
import logging
import signal
import sys
from typing import Any, Callable, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from wx_observer.exceptions import WxObserverError
from wx_observer.models import WatchMode
from wx_observer.service import ObserverService

_READONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_POLL_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


async def _exec_tool(fn: Callable[..., dict[str, Any]], /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except WxObserverError as exc:
        return exc.to_error_dict()
    except Exception as exc:
        return {
            "ok": False,
            "error": "internal_error",
            "message": f"{type(exc).__name__}: {exc}",
        }


def _resolve_max_chars(detail: str, max_text_chars: int | None) -> int | None:
    if max_text_chars is None:
        return 300 if detail == "compact" else None
    if max_text_chars <= 0:
        return None
    return int(max_text_chars)


def create_mcp_server(
    service: ObserverService,
    *,
    name: str = "wx-observer",
) -> MCPServer:
    """Create a read-only MCPServer exposing account discovery, history, digests, attachments, and live watching."""
    server = MCPServer(
        name=name,
        version="0.3.0",
        instructions=(
            "Read-only Windows WeChat 4.x local observer. "
            "Use list_accounts and select_account (or pass explicit account='...' per call without mutating global state). "
            "Use digest for quick cross-chat summaries, list_attachments to resolve downloaded local file paths, "
            "and list_chats/get_messages/search_messages to inspect history (default detail='compact' with 300-char limit, skip_noise=True, and est_tokens; pass detail='full' for raw fields). "
            "Use configure_watch + poll_new_messages/wait_for_messages (default peek=True returning ack_token) followed by ack_messages to advance cursors reliably."
        ),
        log_level="WARNING",
    )

    @server.tool(
        name="list_accounts",
        description="Discover running Weixin.exe processes and local WeChat 4.x account directories with online/offline status.",
        annotations=_READONLY_TOOL,
    )
    async def list_accounts(db_dir: str | None = None) -> dict[str, Any]:
        return await _exec_tool(service.list_accounts, db_dir=db_dir)

    @server.tool(
        name="select_account",
        description="Bind wx-observer to a specific WeChat account by account_id, wxid, or running PID.",
        annotations=_POLL_TOOL,
    )
    async def select_account(
        account: str | None = None,
        pid: int | None = None,
        db_dir: str | None = None,
        keys_file: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.select_account,
            account=account,
            pid=pid,
            db_dir=db_dir,
            keys_file=keys_file,
        )

    @server.tool(
        name="list_chats",
        description="List private and group chats for the bound WeChat account, optionally filtering by chat_type or search query.",
        annotations=_READONLY_TOOL,
    )
    async def list_chats(
        chat_type: Literal["all", "private", "group"] = "all",
        query: str | None = None,
        limit: int = 50,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.list_chats,
            chat_type=chat_type,
            query=query,
            limit=limit,
            account=account,
        )

    @server.tool(
        name="get_messages",
        description=(
            "Extract historical messages from a private or group chat (by chat_id, remark, nickname, or group name). "
            "Supports pagination (before_seq/after_seq), relative/ISO/epoch time range (since/until e.g. '24h', '7d'), type, sender, and keyword filters. "
            "Defaults to detail='compact' (300-char cap unless max_text_chars is set; 0 disables truncation), skip_noise=True, and est_tokens."
        ),
        annotations=_READONLY_TOOL,
    )
    async def get_messages(
        chat: str,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
        since: str | None = None,
        until: str | None = None,
        msg_types: list[str] | None = None,
        sender: str | None = None,
        keyword: str | None = None,
        include_self: bool = True,
        detail: Literal["compact", "full"] = "compact",
        max_text_chars: int | None = None,
        skip_noise: bool = True,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.get_messages,
            chat,
            limit=limit,
            before_seq=before_seq,
            after_seq=after_seq,
            since=since,
            until=until,
            msg_types=msg_types,
            sender=sender,
            keyword=keyword,
            include_self=include_self,
            detail=detail,
            max_text_chars=_resolve_max_chars(detail, max_text_chars),
            skip_noise=skip_noise,
            account=account,
        )

    @server.tool(
        name="search_messages",
        description=(
            "Search historical messages by keyword across a specific chat or across private/group chats. "
            "In compact mode (default), returns keyword-centered snippets, match_offset, and est_tokens."
        ),
        annotations=_READONLY_TOOL,
    )
    async def search_messages(
        keyword: str,
        chat: str | None = None,
        chat_type: Literal["all", "private", "group"] = "all",
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        detail: Literal["compact", "full"] = "compact",
        max_text_chars: int | None = None,
        skip_noise: bool = True,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.search_messages,
            keyword,
            chat=chat,
            chat_type=chat_type,
            since=since,
            until=until,
            limit=limit,
            detail=detail,
            max_text_chars=_resolve_max_chars(detail, max_text_chars),
            skip_noise=skip_noise,
            account=account,
        )

    @server.tool(
        name="configure_watch",
        description=(
            "Configure real-time message monitoring rules for an isolated cursor_id. "
            "Supports mode='all'|'private'|'group'|'allowlist', chat allow/exclude lists, include_self, and cursor reset."
        ),
        annotations=_POLL_TOOL,
    )
    async def configure_watch(
        cursor_id: str = "default",
        mode: WatchMode = "all",
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
        include_self: bool = False,
        msg_types: list[str] | None = None,
        reset_cursor: bool = False,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.configure_watch,
            cursor_id=cursor_id,
            mode=mode,
            chats=chats,
            exclude_chats=exclude_chats,
            include_self=include_self,
            msg_types=msg_types,
            reset_cursor=reset_cursor,
            account=account,
        )

    @server.tool(
        name="poll_new_messages",
        description=(
            "Non-blocking poll for newly arrived messages since the persisted watermark for cursor_id. "
            "Defaults to peek=True (returns ack_token without advancing watermark until ack_messages is called) and detail='compact'."
        ),
        annotations=_POLL_TOOL,
    )
    async def poll_new_messages(
        cursor_id: str = "default",
        limit: int = 100,
        peek: bool = True,
        detail: Literal["compact", "full"] = "compact",
        max_text_chars: int | None = None,
        skip_noise: bool = True,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.poll_new_messages,
            cursor_id=cursor_id,
            limit=limit,
            peek=peek,
            detail=detail,
            max_text_chars=_resolve_max_chars(detail, max_text_chars),
            skip_noise=skip_noise,
            account=account,
        )

    @server.tool(
        name="ack_messages",
        description=(
            "Commit persisted read watermarks returned by poll_new_messages or wait_for_messages when peek=True. "
            "Pass ack_token from the poll response, and optionally upto={chat_id: max_sort_seq} for partial acknowledgment."
        ),
        annotations=_POLL_TOOL,
    )
    async def ack_messages(
        cursor_id: str = "default",
        ack_token: str | None = None,
        upto: dict[str, int] | None = None,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.ack_messages,
            cursor_id=cursor_id,
            ack_token=ack_token,
            upto=upto,
            account=account,
        )

    @server.tool(
        name="wait_for_messages",
        description=(
            "Long-poll for newly arrived messages for cursor_id; returns immediately when new messages arrive "
            "or returns empty after timeout_seconds. Defaults to peek=True (issues ack_token) and detail='compact'."
        ),
        annotations=_POLL_TOOL,
    )
    async def wait_for_messages(
        cursor_id: str = "default",
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        limit: int = 100,
        peek: bool = True,
        detail: Literal["compact", "full"] = "compact",
        max_text_chars: int | None = None,
        skip_noise: bool = True,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.wait_for_messages,
            cursor_id=cursor_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            limit=limit,
            peek=peek,
            detail=detail,
            max_text_chars=_resolve_max_chars(detail, max_text_chars),
            skip_noise=skip_noise,
            account=account,
        )

    @server.tool(
        name="digest",
        description=(
            "Build a compact server-side cross-chat digest over a time window (e.g. since='24h', '7d', '30m'). "
            "Reduces multiple chats into per-chat counts, hourly/daily buckets, top_senders, links, attachments, @mentions, and highlights, "
            "enforcing max_chats (default 20) and token_budget (default 4000; 0 disables) while keeping exact global counts."
        ),
        annotations=_READONLY_TOOL,
    )
    async def digest(
        scope: Literal["all", "private", "group"] = "all",
        chats: list[str] | None = None,
        since: str = "24h",
        until: str | None = None,
        buckets: Literal["hour", "day", "none"] = "hour",
        include: list[Literal["counts", "top_senders", "links", "attachments", "mentions", "highlights"]] | None = None,
        max_highlights_per_chat: int = 8,
        max_text_chars: int = 160,
        max_chats: int = 20,
        token_budget: int = 4000,
        include_self: bool = True,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.digest,
            scope=scope,
            chats=chats,
            since=since,
            until=until,
            buckets=buckets,
            include=include,
            max_highlights_per_chat=max_highlights_per_chat,
            max_text_chars=max_text_chars,
            max_chats=max_chats,
            token_budget=token_budget,
            include_self=include_self,
            account=account,
        )

    @server.tool(
        name="list_attachments",
        description=(
            "List file attachments shared across chats (including forwarded chat-history embedded files) "
            "and resolve local WeChat plaintext file paths with size/MD5 confidence metadata."
        ),
        annotations=_READONLY_TOOL,
    )
    async def list_attachments(
        scope: Literal["all", "private", "group"] = "all",
        chats: list[str] | None = None,
        since: str = "7d",
        until: str | None = None,
        extensions: list[str] | None = None,
        only_available: bool = False,
        limit: int = 50,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.list_attachments,
            scope=scope,
            chats=chats,
            since=since,
            until=until,
            extensions=extensions,
            only_available=only_available,
            limit=limit,
            account=account,
        )

    @server.tool(
        name="get_status",
        description="Inspect currently bound WeChat account, watch filter, cursor watermark progress, and database health.",
        annotations=_READONLY_TOOL,
    )
    async def get_status(
        cursor_id: str = "default",
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.get_status,
            cursor_id=cursor_id,
            account=account,
        )

    return server


class _BearerAuthASGI:
    """Lightweight ASGI middleware enforcing Authorization: Bearer <token> on HTTP scopes."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            auth_val = b""
            for k, v in scope.get("headers") or ():
                if k.lower() == b"authorization":
                    auth_val = v
                    break
            if not hmac.compare_digest(auth_val, self.expected):
                body = json.dumps(
                    {"ok": False, "error": "unauthorized", "message": "Missing or invalid Bearer token."}
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_streamable_http_app(
    service: ObserverService,
    *,
    host: str = "127.0.0.1",
    token: str | None = None,
    json_response: bool = True,
) -> Any:
    """Build an ASGI app serving MCP Streamable HTTP at /mcp."""
    server = create_mcp_server(service)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=json_response,
        host=host,
    )
    if token:
        return _BearerAuthASGI(app, token)
    return app


def run_mcp_server(
    service: ObserverService,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    account: str | None = None,
    pid: int | None = None,
) -> None:
    """Run the MCP server in stdio or localhost Streamable HTTP mode."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    try:
        if account is not None or pid is not None:
            service.select_account(account=account, pid=pid)

        if transport == "stdio":
            server = create_mcp_server(service)
            server.run(transport="stdio")
        elif transport == "http":
            import uvicorn

            app = build_streamable_http_app(service, host=host, token=token)
            config = uvicorn.Config(
                app,
                host=host,
                port=int(port),
                log_level="warning",
                access_log=False,
            )
            uv_server = uvicorn.Server(config)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(
                    signal.SIGBREAK,
                    lambda *_args: setattr(uv_server, "should_exit", True),
                )
            uv_server.run()
        else:
            raise ValueError(f"Unsupported MCP transport: {transport}")
    finally:
        service.close()
