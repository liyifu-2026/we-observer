"""Unified ObserverService facade shared by CLI and MCP server."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from wx_observer.attachments import collect_attachments
from wx_observer.digest import build_digest
from wx_observer.discovery import discover_environment
from wx_observer.exceptions import AccountSelectionError, WxObserverError
from wx_observer.models import AccountInfo, WatchFilter, WatchMode
from wx_observer.monitor import RealtimeMonitor
from wx_observer.repository import AccountRepository
from wx_observer.shaping import estimate_tokens, shape_messages_batch
from wx_observer.state import CursorStateStore

_REL_TIME_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
_REL_UNIT_SECS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_timestamp(value: int | float | str | None) -> int | None:
    """Parse Unix epoch seconds, relative intervals (e.g. '24h', '7d'), or ISO-8601 into UTC epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    rel = _REL_TIME_RE.match(text)
    if rel:
        qty = int(rel.group(1))
        unit = rel.group(2).lower()
        return max(0, int(time.time()) - qty * _REL_UNIT_SECS[unit])
    if text.lstrip("-").isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WxObserverError(
            "invalid_timestamp",
            f"Invalid timestamp '{value}'. Use epoch seconds, relative window (e.g. 24h, 7d), or ISO-8601.",
            {"value": value},
        ) from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp())


class ObserverService:
    """Thread-safe service coordinating discovery, stateless account lookup, and cursor watching."""

    def __init__(
        self,
        *,
        db_dir: str | None = None,
        keys_file: str | None = None,
        state_dir: str | Path | None = None,
        temp_root: str | Path | None = None,
    ) -> None:
        self.db_dir = db_dir
        self.keys_file = keys_file
        self.state_dir = Path(state_dir) if state_dir else None
        self.temp_root = Path(temp_root) if temp_root else None

        self._registry_lock = threading.RLock()
        self._state_store = CursorStateStore(state_dir=self.state_dir)
        self._repos: dict[tuple[str, int | None], AccountRepository] = {}
        self._monitors: dict[tuple[str, int | None], RealtimeMonitor] = {}
        self._selected_key: tuple[str, int | None] | None = None

    def close(self) -> None:
        with self._registry_lock:
            for mon in self._monitors.values():
                mon.stop()
            for repo in self._repos.values():
                repo.close()
            self._monitors.clear()
            self._repos.clear()
            self._selected_key = None

    def list_accounts(self, *, db_dir: str | None = None) -> dict[str, Any]:
        with self._registry_lock:
            selected_acct = self._selected_key[0] if self._selected_key else None
            repos_snapshot = list(self._repos.items())
        env = discover_environment(
            db_dir=db_dir or self.db_dir,
            selected_account=selected_acct,
        )
        accounts_out: list[dict[str, Any]] = []
        for acct in env["accounts"]:
            row = acct.to_dict()
            for (rid, _rpid), repo in repos_snapshot:
                if rid == acct.account_id:
                    row["display_name"] = repo.get_own_display_name()
                    break
            accounts_out.append(row)
        return {
            "ok": True,
            "db_dir": env["db_dir"],
            "selected_account": selected_acct,
            "accounts": accounts_out,
            "processes": [p.to_dict() for p in env["processes"]],
            "access_denied": env["access_denied"],
            "legacy_only": env["legacy_only"],
            "reason": env["reason"],
        }

    @staticmethod
    def _match_account(
        accounts: list[AccountInfo],
        *,
        account: str | None,
        pid: int | None,
        keys_file: str | None,
        reason: str | None,
    ) -> tuple[AccountInfo, list[int]]:
        if pid is not None:
            pid_matches = [a for a in accounts if pid in a.pids]
            if not pid_matches:
                raise AccountSelectionError(
                    "pid_account_not_found",
                    f"No online WeChat account was found for PID {pid}.",
                    {"pid": pid, "available_accounts": [a.to_dict() for a in accounts]},
                )
            chosen = pid_matches[0]
            if account and account not in (chosen.account_id, chosen.wxid):
                raise AccountSelectionError(
                    "pid_account_mismatch",
                    f"PID {pid} owns account '{chosen.account_id}', not '{account}'.",
                    {"pid": pid, "requested_account": account, "actual_account": chosen.account_id},
                )
            return chosen, [pid]

        if account:
            ref = account.strip()
            exact = [a for a in accounts if ref in (a.account_id, a.wxid)]
            if len(exact) == 1:
                return exact[0], list(exact[0].pids)
            if len(exact) > 1:
                raise AccountSelectionError(
                    "ambiguous_accounts",
                    f"Multiple accounts matched '{account}'. Specify exact account_id or pid.",
                    {"candidates": [a.to_dict() for a in exact]},
                )
            ci = ref.casefold()
            fuzzy = [
                a
                for a in accounts
                if ci in a.account_id.casefold() or ci in a.wxid.casefold()
            ]
            if len(fuzzy) == 1:
                return fuzzy[0], list(fuzzy[0].pids)
            if len(fuzzy) > 1:
                raise AccountSelectionError(
                    "ambiguous_accounts",
                    f"Multiple accounts matched '{account}'. Specify exact account_id or pid.",
                    {"candidates": [a.to_dict() for a in fuzzy]},
                )
            raise AccountSelectionError(
                "account_not_found",
                f"Account '{account}' was not found in discovered WeChat directories.",
                {"account": account, "available_accounts": [a.to_dict() for a in accounts]},
            )

        online = [a for a in accounts if a.online]
        if len(online) == 1:
            return online[0], list(online[0].pids)
        if len(online) > 1:
            raise AccountSelectionError(
                "ambiguous_accounts",
                f"Found {len(online)} online WeChat accounts; call select_account with account or pid.",
                {"accounts": [a.to_dict() for a in online]},
            )
        if keys_file and len(accounts) == 1:
            return accounts[0], []
        raise AccountSelectionError(
            "no_online_account",
            reason or "No online WeChat 4.x account is available. Specify account with cached/external keys or log into WeChat.",
            {"accounts": [a.to_dict() for a in accounts]},
        )

    def _open_or_reuse(
        self,
        *,
        account: str | None = None,
        pid: int | None = None,
        db_dir: str | None = None,
        keys_file: str | None = None,
        make_default: bool = False,
    ) -> tuple[AccountRepository, RealtimeMonitor, AccountInfo]:
        with self._registry_lock:
            if not make_default and account is None and pid is None and self._selected_key is not None:
                key = self._selected_key
                repo = self._repos[key]
                mon = self._monitors[key]
                return repo, mon, repo.account

            eff_db_dir = db_dir or self.db_dir
            eff_keys = keys_file or self.keys_file
            env = discover_environment(db_dir=eff_db_dir)
            chosen, target_pids = self._match_account(
                env["accounts"],
                account=account,
                pid=pid,
                keys_file=eff_keys,
                reason=env["reason"],
            )
            bound_pid = target_pids[0] if len(target_pids) == 1 else (pid if pid is not None else None)
            cache_key = (chosen.account_id, bound_pid)

            if cache_key not in self._repos:
                repo = AccountRepository(
                    chosen,
                    state_dir=self.state_dir,
                    keys_file=eff_keys,
                    temp_root=self.temp_root,
                    target_pids=target_pids,
                )
                mon = RealtimeMonitor(repo, self._state_store)
                self._repos[cache_key] = repo
                self._monitors[cache_key] = mon
            else:
                repo = self._repos[cache_key]
                mon = self._monitors[cache_key]

            if make_default or self._selected_key is None:
                self._selected_key = cache_key
            return repo, mon, chosen

    def select_account(
        self,
        *,
        account: str | None = None,
        pid: int | None = None,
        db_dir: str | None = None,
        keys_file: str | None = None,
    ) -> dict[str, Any]:
        repo, _mon, chosen = self._open_or_reuse(
            account=account,
            pid=pid,
            db_dir=db_dir,
            keys_file=keys_file,
            make_default=True,
        )
        chosen.selected = True
        chosen.display_name = repo.get_own_display_name()
        return {
            "ok": True,
            "account": chosen.to_dict(),
            "total_dbs": repo.total_db_count,
            "keyed_dbs": repo.keyed_db_count,
            "unkeyed_dbs": repo.unkeyed_dbs,
        }

    def list_chats(
        self,
        *,
        chat_type: str = "all",
        query: str | None = None,
        limit: int = 50,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        chats = repo.list_chats(chat_type=chat_type, query=query, limit=limit)
        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "count": len(chats),
            "chats": [c.to_dict() for c in chats],
        }
        payload["est_tokens"] = estimate_tokens(payload)
        return payload

    def get_messages(
        self,
        chat: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
        since: int | float | str | None = None,
        until: int | float | str | None = None,
        msg_types: list[str] | None = None,
        sender: str | None = None,
        keyword: str | None = None,
        include_self: bool = True,
        detail: str = "full",
        max_text_chars: int | None = None,
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        chat_info = repo.resolve_chat(chat)
        since_ts = parse_timestamp(since)
        until_ts = parse_timestamp(until)
        result = repo.get_messages(
            chat_info.chat_id,
            chat_name=chat_info.display_name,
            limit=limit,
            before_seq=before_seq,
            after_seq=after_seq,
            since_ts=since_ts,
            until_ts=until_ts,
            msg_types=msg_types,
            sender=sender,
            keyword=keyword,
            include_self=include_self,
        )
        shaped, skipped_noise = shape_messages_batch(
            result["messages"],
            detail=detail,
            max_text_chars=max_text_chars,
            skip_noise=skip_noise,
            include_chat=False,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "detail": detail,
            "chat": chat_info.to_dict(),
            "has_more": result["has_more"],
            "next_before_seq": result["next_before_seq"],
            "latest_sort_seq": result["latest_sort_seq"],
            "count": len(shaped),
            "skipped_noise": skipped_noise,
            "messages": shaped,
        }
        payload["est_tokens"] = estimate_tokens(payload)
        return payload

    def search_messages(
        self,
        keyword: str,
        *,
        chat: str | None = None,
        chat_type: str = "all",
        since: int | float | str | None = None,
        until: int | float | str | None = None,
        limit: int = 50,
        detail: str = "full",
        max_text_chars: int | None = None,
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        since_ts = parse_timestamp(since)
        until_ts = parse_timestamp(until)
        events = repo.search_messages(
            keyword,
            chat_id=chat,
            chat_type=chat_type,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=limit,
        )
        shaped, skipped_noise = shape_messages_batch(
            events,
            detail=detail,
            max_text_chars=max_text_chars,
            skip_noise=skip_noise,
            include_chat=True,
            snippet_keyword=keyword,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "detail": detail,
            "keyword": keyword,
            "count": len(shaped),
            "skipped_noise": skipped_noise,
            "messages": shaped,
        }
        payload["est_tokens"] = estimate_tokens(payload)
        return payload

    def configure_watch(
        self,
        *,
        cursor_id: str = "default",
        mode: WatchMode = "all",
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
        include_self: bool = False,
        msg_types: list[str] | None = None,
        reset_cursor: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        if mode not in ("all", "private", "group", "allowlist"):
            raise WxObserverError(
                "invalid_watch_mode",
                f"Unsupported watch mode '{mode}'. Expected all, private, group, or allowlist.",
            )
        repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        resolved_chats = [repo.resolve_chat(ref).chat_id for ref in (chats or [])]
        resolved_excludes = [repo.resolve_chat(ref).chat_id for ref in (exclude_chats or [])]
        if mode == "allowlist" and not resolved_chats:
            raise WxObserverError(
                "invalid_watch_config",
                "allowlist mode requires at least one conversation in chats.",
            )
        wf = WatchFilter(
            mode=mode,
            chats=list(dict.fromkeys(resolved_chats)),
            exclude_chats=list(dict.fromkeys(resolved_excludes)),
            include_self=bool(include_self),
            msg_types=[t.strip().lower() for t in (msg_types or []) if t.strip()],
        )
        status = mon.configure_watch(
            cursor_id=cursor_id,
            watch_filter=wf,
            reset_cursor=reset_cursor,
        )
        return {"ok": True, "status": status.to_dict()}

    def poll_new_messages(
        self,
        *,
        cursor_id: str = "default",
        limit: int = 100,
        peek: bool = False,
        detail: str = "full",
        max_text_chars: int | None = None,
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        batch = mon.poll_batch(
            cursor_id=cursor_id,
            limit=limit,
            peek=peek,
            skip_noise=skip_noise,
        )
        status = mon.get_status(cursor_id=cursor_id)
        shaped, _ = shape_messages_batch(
            batch.events,
            detail=detail,
            max_text_chars=max_text_chars,
            skip_noise=False,
            include_chat=True,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "cursor_id": cursor_id,
            "initialized": status.initialized,
            "peek": peek,
            "ack_token": batch.ack_token,
            "detail": detail,
            "count": len(shaped),
            "skipped_noise": batch.skipped_noise,
            "messages": shaped,
        }
        payload["est_tokens"] = estimate_tokens(payload)
        return payload

    def ack_messages(
        self,
        *,
        cursor_id: str = "default",
        ack_token: str | None = None,
        upto: dict[str, int] | None = None,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        committed = mon.ack(cursor_id=cursor_id, ack_token=ack_token, upto=upto)
        return {
            "ok": True,
            "account_id": repo.account.account_id,
            "cursor_id": cursor_id,
            "committed_chats": len(committed),
            "committed": committed,
        }

    def wait_for_messages(
        self,
        *,
        cursor_id: str = "default",
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        limit: int = 100,
        peek: bool = False,
        detail: str = "full",
        max_text_chars: int | None = None,
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        batch = mon.wait_for_batch(
            cursor_id=cursor_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            limit=limit,
            peek=peek,
            skip_noise=skip_noise,
        )
        shaped, _ = shape_messages_batch(
            batch.events,
            detail=detail,
            max_text_chars=max_text_chars,
            skip_noise=False,
            include_chat=True,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "cursor_id": cursor_id,
            "timed_out": len(shaped) == 0,
            "peek": peek,
            "ack_token": batch.ack_token,
            "detail": detail,
            "count": len(shaped),
            "skipped_noise": batch.skipped_noise,
            "messages": shaped,
        }
        payload["est_tokens"] = estimate_tokens(payload)
        return payload

    def digest(
        self,
        *,
        scope: str = "all",
        chats: list[str] | None = None,
        since: int | float | str | None = "24h",
        until: int | float | str | None = None,
        buckets: str = "hour",
        include: list[str] | None = None,
        max_highlights_per_chat: int = 8,
        max_text_chars: int = 160,
        max_chats: int = 20,
        token_budget: int = 4000,
        include_self: bool = True,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        if scope not in ("all", "private", "group"):
            raise WxObserverError("invalid_scope", f"Unsupported scope '{scope}'.")
        if buckets not in ("hour", "day", "none"):
            raise WxObserverError("invalid_buckets", f"Unsupported buckets '{buckets}'.")
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        since_ts = parse_timestamp(since)
        until_ts = parse_timestamp(until)
        return build_digest(
            repo,
            scope=scope,
            chats=chats,
            since_ts=since_ts,
            until_ts=until_ts,
            buckets=buckets,
            include=include,
            max_highlights_per_chat=max(1, min(int(max_highlights_per_chat), 50)),
            max_text_chars=max(20, min(int(max_text_chars), 2000)),
            max_chats=max(1, min(int(max_chats), 200)),
            token_budget=max(0, int(token_budget)),
            include_self=include_self,
        )

    def list_attachments(
        self,
        *,
        scope: str = "all",
        chats: list[str] | None = None,
        since: int | float | str | None = "7d",
        until: int | float | str | None = None,
        extensions: list[str] | None = None,
        only_available: bool = False,
        limit: int = 50,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        if scope not in ("all", "private", "group"):
            raise WxObserverError("invalid_scope", f"Unsupported scope '{scope}'.")
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        since_ts = parse_timestamp(since)
        until_ts = parse_timestamp(until)
        return collect_attachments(
            repo,
            scope=scope,
            chats=chats,
            since_ts=since_ts,
            until_ts=until_ts,
            extensions=extensions,
            only_available=only_available,
            limit=limit,
        )

    def watch_stream(
        self,
        *,
        cursor_id: str = "default",
        poll_interval: float = 1.0,
        max_events: int | None = None,
        duration_seconds: float | None = None,
        account: str | None = None,
        pid: int | None = None,
    ):
        _repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        return mon.watch_stream(
            cursor_id=cursor_id,
            poll_interval=poll_interval,
            max_events=max_events,
            duration_seconds=duration_seconds,
        )

    def get_status(
        self,
        *,
        cursor_id: str = "default",
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        with self._registry_lock:
            selected_key = self._selected_key
        if account is None and pid is None and selected_key is None:
            env = discover_environment(db_dir=self.db_dir)
            return {
                "ok": True,
                "selected": False,
                "db_dir": env["db_dir"],
                "accounts_count": len(env["accounts"]),
                "online_accounts_count": sum(1 for a in env["accounts"] if a.online),
                "reason": env["reason"],
            }
        _repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        status = mon.get_status(cursor_id=cursor_id)
        return {
            "ok": True,
            "selected": True,
            "status": status.to_dict(),
        }
