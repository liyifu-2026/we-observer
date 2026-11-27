"""End-to-end MCP protocol tests (in-memory MCP client + 127.0.0.1 Streamable HTTP)."""

from __future__ import annotations

import json
import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.fixtures import SyntheticAccountEnv
from wx_observer.mcp_server import build_streamable_http_app, create_mcp_server
from wx_observer.service import ObserverService


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _extract_json_result(call_result) -> dict:
    if getattr(call_result, "structuredContent", None):
        return dict(call_result.structuredContent)
    for block in call_result.content:
        txt = getattr(block, "text", None)
        if txt:
            return json.loads(txt)
    raise AssertionError(f"Empty tool result: {call_result}")


def test_mcp_all_12_tools_over_memory_transport(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    file_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    file_dir.mkdir(parents=True, exist_ok=True)
    (file_dir / "spec.pdf").write_bytes(b"P" * 12345)

    temp_root = tmp_path / "pool_tmp"
    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=temp_root,
    )
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            tools_res = await client.list_tools()
            names = {t.name for t in tools_res.tools}
            assert names == {
                "list_accounts",
                "select_account",
                "list_chats",
                "get_messages",
                "search_messages",
                "digest",
                "list_attachments",
                "configure_watch",
                "poll_new_messages",
                "ack_messages",
                "wait_for_messages",
                "get_status",
            }

            acct_res = _extract_json_result(await client.call_tool("list_accounts", {}))
            assert acct_res["ok"] is True
            assert acct_res["accounts"][0]["account_id"] == env.account_id

            # Offline account fails closed without explicit account, succeeds when named
            sel_res = _extract_json_result(
                await client.call_tool("select_account", {"account": env.account_id})
            )
            assert sel_res["ok"] is True
            assert sel_res["keyed_dbs"] == 4

            chats_res = _extract_json_result(await client.call_tool("list_chats", {}))
            assert chats_res["count"] == 2
            assert chats_res["est_tokens"] > 0

            # Default MCP output is compact
            msgs_res = _extract_json_result(
                await client.call_tool("get_messages", {"chat": "AliceRemark", "limit": 10})
            )
            assert msgs_res["detail"] == "compact"
            assert msgs_res["count"] == 5
            assert msgs_res["est_tokens"] > 0
            assert "t" in msgs_res["messages"][0]
            assert "seq" in msgs_res["messages"][0]

            full_res = _extract_json_result(
                await client.call_tool("get_messages", {"chat": "AliceRemark", "limit": 10, "detail": "full"})
            )
            assert full_res["detail"] == "full"
            assert "sort_seq" in full_res["messages"][0]

            search_res = _extract_json_result(
                await client.call_tool("search_messages", {"keyword": "中文测试"})
            )
            assert search_res["count"] == 1
            assert search_res["detail"] == "compact"
            assert search_res["est_tokens"] > 0

            digest_res = _extract_json_result(
                await client.call_tool("digest", {"scope": "all", "since": "24h"})
            )
            assert digest_res["ok"] is True
            assert digest_res["summary"]["total_messages"] >= 9
            assert any(att["filename"] == "spec.pdf" and att["available"] for att in digest_res["attachments"])

            attach_res = _extract_json_result(
                await client.call_tool("list_attachments", {"scope": "all", "since": "7d"})
            )
            assert attach_res["ok"] is True
            assert attach_res["available_count"] >= 1
            assert attach_res["attachments"][0]["filename"] == "spec.pdf"

            cfg_res = _extract_json_result(
                await client.call_tool(
                    "configure_watch",
                    {"cursor_id": "mcp-test", "mode": "private"},
                )
            )
            assert cfg_res["ok"] is True

            # Baseline poll -> empty
            poll1 = _extract_json_result(
                await client.call_tool("poll_new_messages", {"cursor_id": "mcp-test"})
            )
            assert poll1["initialized"] is True and poll1["count"] == 0

            # Append WAL row and wake wait_for_messages (defaults to peek=True)
            env.append_wal_messages(
                env.alice_id,
                [(40, 1, 999, 3, int(time.time()), "MCP live message")],
            )
            waited = _extract_json_result(
                await client.call_tool(
                    "wait_for_messages",
                    {"cursor_id": "mcp-test", "timeout_seconds": 2.0, "poll_interval": 0.2},
                )
            )
            assert waited["timed_out"] is False
            assert waited["peek"] is True
            assert waited["ack_token"]
            assert waited["messages"][0]["text"] == "MCP live message"

            # Un-acked batch is still re-delivered on subsequent peek
            redeliver = _extract_json_result(
                await client.call_tool("poll_new_messages", {"cursor_id": "mcp-test"})
            )
            assert redeliver["count"] == 1

            # Explicit ack commits watermark and supports idempotent retry
            ack_res = _extract_json_result(
                await client.call_tool(
                    "ack_messages",
                    {"cursor_id": "mcp-test", "ack_token": waited["ack_token"]},
                )
            )
            assert ack_res["ok"] is True
            assert ack_res["already_committed"] is False
            assert ack_res["committed"] == {env.alice_id: 999}

            ack_retry = _extract_json_result(
                await client.call_tool(
                    "ack_messages",
                    {"cursor_id": "mcp-test", "ack_token": waited["ack_token"]},
                )
            )
            assert ack_retry["ok"] is True
            assert ack_retry["already_committed"] is True
            assert ack_retry["committed"] == {env.alice_id: 999}

            after_ack = _extract_json_result(
                await client.call_tool("poll_new_messages", {"cursor_id": "mcp-test"})
            )
            assert after_ack["count"] == 0

            status = _extract_json_result(
                await client.call_tool("get_status", {"cursor_id": "mcp-test"})
            )
            assert status["status"]["tracked_chats"] >= 2

            # Structured domain error (no stacktrace leak, exposes retryable + next_action)
            missing = _extract_json_result(
                await client.call_tool("get_messages", {"chat": "does_not_exist"})
            )
            assert missing["ok"] is False and missing["error"] == "chat_not_found"
            assert missing["retryable"] is False
            assert isinstance(missing["next_action"], str) and missing["next_action"]

    try:
        anyio.run(scenario)
    finally:
        service.close()
    assert not list(temp_root.glob("sess-*"))


def test_mcp_streamable_http_bearer_auth_and_tool_call(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    temp_root = tmp_path / "pool_tmp"
    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=temp_root,
    )
    port = _free_port()
    token = "test-bearer-secret"
    app = build_streamable_http_app(service, host="127.0.0.1", token=token)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        url = f"http://127.0.0.1:{port}/mcp"

        async def check_http() -> None:
            async with httpx2.AsyncClient() as unauth:
                resp = await unauth.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                assert resp.status_code == 401

            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as authed:
                async with streamable_http_client(url, http_client=authed) as (read_s, write_s):
                    async with ClientSession(read_s, write_s) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 12
                        sel = _extract_json_result(
                            await session.call_tool("select_account", {"account": env.account_id})
                        )
                        assert sel["ok"] is True

        anyio.run(check_http)
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        service.close()
    assert not list(temp_root.glob("sess-*"))
