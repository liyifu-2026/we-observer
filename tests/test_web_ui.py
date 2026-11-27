"""Tests for the minimal read-only local Web UI (static assets, APIs, auth, and SSE)."""

from __future__ import annotations

import json
import socket
import threading
import time

import anyio
import httpx2
import uvicorn
from starlette.testclient import TestClient

from tests.fixtures import SyntheticAccountEnv
from wx_observer.service import ObserverService
from wx_observer.web_ui import create_web_ui_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_web_ui_static_and_watch_config_apis(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    temp_root = tmp_path / "pool_tmp"
    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=temp_root,
    )
    app = create_web_ui_app(service)

    try:
        with TestClient(app) as client:
            r_idx = client.get("/")
            assert r_idx.status_code == 200
            assert "text/html" in r_idx.headers["content-type"]
            assert "wx-observer" in r_idx.text

            r_css = client.get("/assets/styles.css")
            assert r_css.status_code == 200 and "text/css" in r_css.headers["content-type"]

            r_js = client.get("/assets/app.js")
            assert r_js.status_code == 200 and "javascript" in r_js.headers["content-type"]

            r_acct = client.get("/api/accounts")
            assert r_acct.status_code == 200
            assert r_acct.json()["accounts"][0]["account_id"] == env.account_id

            r_sel = client.post("/api/accounts/select", json={"account": env.account_id})
            assert r_sel.status_code == 200
            assert r_sel.json()["keyed_dbs"] == 4

            r_chats = client.get("/api/chats?type=all")
            assert r_chats.status_code == 200
            assert r_chats.json()["count"] == 2

            r_cfg = client.post(
                "/api/watch/config",
                json={"cursor_id": "ui", "mode": "allowlist", "chats": ["AliceRemark"], "include_self": False},
            )
            assert r_cfg.status_code == 200
            assert r_cfg.json()["status"]["watch_filter"]["mode"] == "allowlist"
            assert r_cfg.json()["status"]["watch_filter"]["chats"] == [env.alice_id]

            r_status = client.get("/api/status", params={"cursor_id": "ui"})
            assert r_status.status_code == 200
            assert r_status.json()["status"]["watch_filter"]["mode"] == "allowlist"

            # Negative validation: empty allowlist fails closed
            r_bad = client.post(
                "/api/watch/config",
                json={"cursor_id": "ui", "mode": "allowlist", "chats": []},
            )
            assert r_bad.status_code == 400
            assert r_bad.json()["ok"] is False
            assert r_bad.json()["error"] == "invalid_watch_config"
    finally:
        service.close()
    assert not list(temp_root.glob("sess-*"))


def test_web_ui_sse_realtime_and_token_auth(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    temp_root = tmp_path / "pool_tmp"
    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=temp_root,
    )
    service.select_account(account=env.account_id)

    shutdown_event = threading.Event()
    port = _free_port()
    token = "ui-secret-token"
    app = create_web_ui_app(service, token=token, shutdown_event=shutdown_event)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        deadline = time.monotonic() + 10.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        base_url = f"http://127.0.0.1:{port}"

        async def verify_sse() -> None:
            async with httpx2.AsyncClient(timeout=10.0) as client:
                r_unauth = await client.get(f"{base_url}/api/accounts")
                assert r_unauth.status_code == 401

                r_hdr = await client.get(
                    f"{base_url}/api/accounts",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert r_hdr.status_code == 200

                r_qs = await client.get(f"{base_url}/api/accounts?token={token}")
                assert r_qs.status_code == 200

                stream_url = f"{base_url}/api/watch/stream?cursor_id=sse-test&interval=0.2&token={token}"
                async with client.stream("GET", stream_url) as resp:
                    assert resp.status_code == 200
                    current_event = None
                    saw_ready = False
                    saw_message = False
                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            current_event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            payload = json.loads(line.split(":", 1)[1].strip())
                            if current_event == "ready":
                                saw_ready = True
                                assert payload["ok"] is True
                                env.append_wal_messages(
                                    env.alice_id,
                                    [(50, 1, 999, 3, int(time.time()), "SSE live push")],
                                )
                            elif current_event == "message":
                                assert payload["text"] == "SSE live push"
                                saw_message = True
                                break
                    assert saw_ready and saw_message

        anyio.run(verify_sse)
    finally:
        shutdown_event.set()
        server.should_exit = True
        thread.join(timeout=5.0)
        service.close()

    assert not list(temp_root.glob("sess-*"))
