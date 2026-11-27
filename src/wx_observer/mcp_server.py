"""Official MCP server (stdio & 127.0.0.1 Streamable HTTP) for wx-observer."""

from __future__ import annotations

import hmac
import json
import logging
import signal
import sys
from typing import Any, Callable, Literal

import anyio
import uvicorn
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


def create_mcp_server(
    service: ObserverService,
    *,
    name: str = "wx-observer",
) -> MCPServer:
    """Create a read-only MCPServer exposing account discovery, history extraction, and live watching."""
    server = MCPServer(
        name=name,
        version="0.1.0",
        instructions=(
            "Read-only Windows WeChat 4.x local observer. "
            "Use list_accounts and select_account to choose a running account, "
            "list_chats/get_messages/search_messages to inspect history, and "
            "configure_watch + poll_new_messages or wait_for_messages for real-time monitoring."
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
            "Supports pagination (before_seq/after_seq), ISO/epoch time range (since/until), type, sender, and keyword filters."
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
            account=account,
        )

    @server.tool(
        name="search_messages",
        description="Search historical messages by keyword across a specific chat or across private/group chats.",
        annotations=_READONLY_TOOL,
    )
    async def search_messages(
        keyword: str,
        chat: str | None = None,
        chat_type: Literal["all", "private", "group"] = "all",
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
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
            "Non-blocking poll for newly arrived messages since the last persisted watermark for cursor_id. "
            "First call establishes baseline from the latest message and returns empty."
        ),
        annotations=_POLL_TOOL,
    )
    async def poll_new_messages(
        cursor_id: str = "default",
        limit: int = 100,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.poll_new_messages,
            cursor_id=cursor_id,
            limit=limit,
            account=account,
        )

    @server.tool(
        name="wait_for_messages",
        description=(
            "Long-poll for newly arrived messages for cursor_id; returns immediately when new messages arrive "
            "or returns an empty list after timeout_seconds."
        ),
        annotations=_POLL_TOOL,
    )
    async def wait_for_messages(
        cursor_id: str = "default",
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        limit: int = 100,
        account: str | None = None,
    ) -> dict[str, Any]:
        return await _exec_tool(
            service.wait_for_messages,
            cursor_id=cursor_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
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
    """Build a Starlette ASGI app serving MCP Streamable HTTP at /mcp."""
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
