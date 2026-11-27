"""Minimal read-only local Web UI (Starlette + SSE + static assets) for wx-observer."""

from __future__ import annotations

import hmac
import json
import logging
import signal
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from wx_observer.exceptions import WxObserverError
from wx_observer.service import ObserverService

WEB_DIR = Path(__file__).resolve().parent / "web"

_ERROR_HTTP_STATUS = {
    "chat_not_found": 404,
    "account_not_found": 404,
    "pid_account_not_found": 404,
    "ambiguous_chat": 409,
    "ambiguous_accounts": 409,
    "pid_account_mismatch": 409,
    "access_denied": 403,
    "python_32bit_unsupported": 403,
    "invalid_watch_mode": 400,
    "invalid_watch_config": 400,
    "no_online_account": 400,
    "keys_unavailable": 400,
    "account_offline_no_keys": 400,
    "key_missing": 400,
}


def _parse_int(raw: str | None) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise WxObserverError("invalid_parameter", f"Expected integer, got '{raw}'.") from exc


def _error_response(exc: WxObserverError) -> JSONResponse:
    return JSONResponse(exc.to_error_dict(), status_code=_ERROR_HTTP_STATUS.get(exc.code, 400))


def _sse_frame(event: str, payload: Any) -> bytes:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


class _ApiTokenAuthASGI:
    """ASGI wrapper guarding /api/* endpoints via Bearer header or ?token= query param."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token
        self.expected_header = f"Bearer {token}".encode("utf-8")

    def _is_authorized(self, scope: dict[str, Any]) -> bool:
        auth_hdr = b""
        for k, v in scope.get("headers") or ():
            if k.lower() == b"authorization":
                auth_hdr = v
                break
        if auth_hdr and hmac.compare_digest(auth_hdr, self.expected_header):
            return True
        qs = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        if qs:
            params = urllib.parse.parse_qs(qs, keep_blank_values=False)
            vals = params.get("token")
            if vals and hmac.compare_digest(vals[0], self.token):
                return True
        return False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path") or "")
            if path.startswith("/api/") and not self._is_authorized(scope):
                body = json.dumps(
                    {
                        "ok": False,
                        "error": "unauthorized",
                        "message": "Missing or invalid access token.",
                    }
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


def create_web_ui_app(
    service: ObserverService,
    *,
    token: str | None = None,
    shutdown_event: threading.Event | None = None,
) -> Any:
    """Build the minimal local read-only Starlette live-monitor application."""
    stop_signal = shutdown_event or threading.Event()

    async def index(_request: Request) -> Response:
        return FileResponse(
            WEB_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def asset_css(_request: Request) -> Response:
        return FileResponse(
            WEB_DIR / "styles.css",
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def asset_js(_request: Request) -> Response:
        return FileResponse(
            WEB_DIR / "app.js",
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def api_accounts(_request: Request) -> JSONResponse:
        try:
            payload = await anyio.to_thread.run_sync(service.list_accounts)
            return JSONResponse(payload)
        except WxObserverError as exc:
            return _error_response(exc)

    async def api_select_account(request: Request) -> JSONResponse:
        try:
            raw = await request.body()
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise WxObserverError("invalid_body", "JSON object body required.")
            account = body.get("account")
            pid = int(body["pid"]) if body.get("pid") is not None else None
            payload = await anyio.to_thread.run_sync(
                lambda: service.select_account(account=account, pid=pid)
            )
            return JSONResponse(payload)
        except json.JSONDecodeError as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_json", "message": str(exc)},
                status_code=400,
            )
        except WxObserverError as exc:
            return _error_response(exc)

    async def api_chats(request: Request) -> JSONResponse:
        try:
            qp = request.query_params
            chat_type = qp.get("type") or "all"
            query = qp.get("query") or None
            limit = _parse_int(qp.get("limit")) or 200
            account = qp.get("account") or None
            payload = await anyio.to_thread.run_sync(
                lambda: service.list_chats(
                    chat_type=chat_type,
                    query=query,
                    limit=limit,
                    account=account,
                )
            )
            return JSONResponse(payload)
        except WxObserverError as exc:
            return _error_response(exc)

    async def api_status(request: Request) -> JSONResponse:
        try:
            qp = request.query_params
            cursor_id = qp.get("cursor_id") or "ui"
            account = qp.get("account") or None
            payload = await anyio.to_thread.run_sync(
                lambda: service.get_status(cursor_id=cursor_id, account=account)
            )
            return JSONResponse(payload)
        except WxObserverError as exc:
            return _error_response(exc)

    async def api_watch_config(request: Request) -> JSONResponse:
        try:
            raw = await request.body()
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise WxObserverError("invalid_body", "JSON object body required.")
            cursor_id = str(body.get("cursor_id") or "ui")
            mode = str(body.get("mode") or "all")
            chats = body.get("chats")
            exclude_chats = body.get("exclude_chats")
            include_self = bool(body.get("include_self", False))
            reset_cursor = bool(body.get("reset_cursor", False))
            account = body.get("account") or None
            payload = await anyio.to_thread.run_sync(
                lambda: service.configure_watch(
                    cursor_id=cursor_id,
                    mode=mode,  # type: ignore[arg-type]
                    chats=chats,
                    exclude_chats=exclude_chats,
                    include_self=include_self,
                    reset_cursor=reset_cursor,
                    account=account,
                )
            )
            return JSONResponse(payload)
        except json.JSONDecodeError as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_json", "message": str(exc)},
                status_code=400,
            )
        except WxObserverError as exc:
            return _error_response(exc)

    async def api_watch_stream(request: Request) -> StreamingResponse:
        qp = request.query_params
        cursor_id = qp.get("cursor_id") or "ui"
        account = qp.get("account") or None
        try:
            interval = float(qp.get("interval") or 1.0)
        except ValueError:
            interval = 1.0
        interval = max(0.2, min(interval, 5.0))

        async def event_gen() -> AsyncIterator[bytes]:
            try:
                initial = await anyio.to_thread.run_sync(
                    lambda: service.poll_new_messages(
                        cursor_id=cursor_id,
                        limit=100,
                        account=account,
                    )
                )
                status = await anyio.to_thread.run_sync(
                    lambda: service.get_status(cursor_id=cursor_id, account=account)
                )
                yield _sse_frame("ready", status)
                for msg in initial["messages"]:
                    yield _sse_frame("message", msg)

                last_emit = time.monotonic()
                while not stop_signal.is_set():
                    if await request.is_disconnected():
                        break
                    waited = 0.0
                    while waited < interval:
                        if stop_signal.is_set() or await request.is_disconnected():
                            return
                        step = min(0.2, interval - waited)
                        await anyio.sleep(step)
                        waited += step

                    if stop_signal.is_set() or await request.is_disconnected():
                        break

                    batch = await anyio.to_thread.run_sync(
                        lambda: service.poll_new_messages(
                            cursor_id=cursor_id,
                            limit=100,
                            account=account,
                        )
                    )
                    if batch["messages"]:
                        for msg in batch["messages"]:
                            yield _sse_frame("message", msg)
                        last_emit = time.monotonic()
                    elif time.monotonic() - last_emit >= 10.0:
                        yield b": heartbeat\n\n"
                        last_emit = time.monotonic()
            except WxObserverError as exc:
                yield _sse_frame("error", exc.to_error_dict())
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as exc:
                yield _sse_frame(
                    "error",
                    {"ok": False, "error": "stream_error", "message": str(exc)},
                )

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/assets/styles.css", asset_css, methods=["GET"]),
        Route("/assets/app.js", asset_js, methods=["GET"]),
        Route("/api/accounts", api_accounts, methods=["GET"]),
        Route("/api/accounts/select", api_select_account, methods=["POST"]),
        Route("/api/chats", api_chats, methods=["GET"]),
        Route("/api/status", api_status, methods=["GET"]),
        Route("/api/watch/config", api_watch_config, methods=["POST"]),
        Route("/api/watch/stream", api_watch_stream, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    if token:
        return _ApiTokenAuthASGI(app, token)
    return app


def run_web_ui(
    service: ObserverService,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    token: str | None = None,
    account: str | None = None,
    pid: int | None = None,
    open_browser: bool = True,
) -> None:
    """Start the local read-only Web UI HTTP server and clean up on exit."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    shutdown_event = threading.Event()
    try:
        if account is not None or pid is not None:
            service.select_account(account=account, pid=pid)

        app = create_web_ui_app(
            service,
            token=token,
            shutdown_event=shutdown_event,
        )
        config = uvicorn.Config(
            app,
            host=host,
            port=int(port),
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        uv_server = uvicorn.Server(config)

        def _trigger_shutdown(*_args: Any) -> None:
            shutdown_event.set()
            uv_server.should_exit = True

        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _trigger_shutdown)
        try:
            signal.signal(signal.SIGINT, _trigger_shutdown)
        except Exception:
            pass

        url = f"http://{host}:{port}/"
        if token:
            url += f"?token={urllib.parse.quote(token)}"
        sys.stderr.write(f"wx-observer Web UI listening at {url}\n")
        sys.stderr.flush()

        if open_browser:
            def _open_when_ready() -> None:
                deadline = time.monotonic() + 5.0
                while not uv_server.started and time.monotonic() < deadline and not shutdown_event.is_set():
                    time.sleep(0.05)
                if uv_server.started and not shutdown_event.is_set():
                    webbrowser.open(url)

            threading.Thread(target=_open_when_ready, daemon=True).start()

        uv_server.run()
    finally:
        shutdown_event.set()
        service.close()
