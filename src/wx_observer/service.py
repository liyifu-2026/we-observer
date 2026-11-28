"""Unified ObserverService facade shared by CLI and MCP server."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from wx_observer.attachments import build_file_index, collect_attachments, enrich_embedded_files
from wx_observer.audit import AuditLogger
from wx_observer.diagnostics import build_doctor_report, write_doctor_bundle
from wx_observer.digest import build_digest
from wx_observer.discovery import discover_environment
from wx_observer.exceptions import AccountSelectionError, WxObserverError
from wx_observer.models import AccountInfo, WatchFilter, WatchMode
from wx_observer.monitor import RealtimeMonitor
from wx_observer.parser import parse_chat_history_xml
from wx_observer.repository import AccountRepository, probe_account_profile
from wx_observer.shaping import (
    TRUNCATE_STRATEGIES,
    _truncate_text,
    defuse_markdown_links,
    estimate_tokens,
    format_compact_time,
    format_local_iso,
    shape_messages_batch,
)
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


def parse_min_activity(value: int | float | str | None) -> int | None:
    """Parse min_activity as days ago (when <= 36500) or as a timestamp/relative window."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = int(value)
        if 0 < num <= 36500:
            return max(0, int(time.time()) - num * 86400)
        return num
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        num = int(text)
        if 0 < num <= 36500:
            return max(0, int(time.time()) - num * 86400)
        return num
    return parse_timestamp(text)


def _resolve_chat_type(chat_type: str | None, scope: str | None) -> str:
    if chat_type is not None and scope is not None and chat_type != scope:
        raise WxObserverError(
            "invalid_scope",
            f"Conflicting chat_type='{chat_type}' and deprecated scope='{scope}'.",
        )
    effective = chat_type if chat_type is not None else (scope if scope is not None else "all")
    if effective not in ("all", "private", "group"):
        raise WxObserverError("invalid_scope", f"Unsupported chat_type '{effective}'.")
    return effective


def _validate_truncate(truncate: str) -> str:
    if truncate not in TRUNCATE_STRATEGIES:
        raise WxObserverError(
            "invalid_truncate",
            f"Unsupported truncate strategy '{truncate}'.",
            {"truncate": truncate},
        )
    return truncate


def _norm_path_key(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.normcase(os.path.abspath(path))


class ObserverService:
    """Thread-safe service coordinating discovery, stateless account lookup, and cursor watching."""

    def __init__(
        self,
        *,
        db_dir: str | None = None,
        keys_file: str | None = None,
        state_dir: str | Path | None = None,
        temp_root: str | Path | None = None,
        account_cache_ttl: float = 30.0,
        use_key_cache: bool | None = None,
        audit: str | Path | None = None,
    ) -> None:
        self.db_dir = db_dir
        self.keys_file = keys_file
        self.state_dir = Path(state_dir) if state_dir else None
        self.temp_root = Path(temp_root) if temp_root else None
        env_no_cache = os.environ.get("WX_OBSERVER_NO_KEY_CACHE", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self.use_key_cache = (not env_no_cache) if use_key_cache is None else bool(use_key_cache)
        self.audit_logger = AuditLogger(audit, state_dir=self.state_dir)

        self._registry_lock = threading.RLock()
        self._state_store = CursorStateStore(state_dir=self.state_dir)
        self._repos: dict[tuple[str, int | None], AccountRepository] = {}
        self._monitors: dict[tuple[str, int | None], RealtimeMonitor] = {}
        self._selected_key: tuple[str, int | None] | None = None
        self._account_cache_ttl = max(0.0, float(account_cache_ttl))
        self._alias_cache: dict[
            tuple[str, int | None, str | None, str | None],
            tuple[tuple[str, int | None], float],
        ] = {}
        self._profile_cache: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}

    @property
    def key_cache_mode(self) -> str:
        has_ext = bool(self.keys_file or os.environ.get("WX_OBSERVER_KEYS_FILE"))
        if not self.use_key_cache:
            return "external_file" if has_ext else "disabled"
        return "dpapi"

    def close(self) -> None:
        with self._registry_lock:
            for mon in self._monitors.values():
                mon.stop()
            for repo in self._repos.values():
                repo.close()
            self._monitors.clear()
            self._repos.clear()
            self._alias_cache.clear()
            self._profile_cache.clear()
            self._selected_key = None

    def _resolve_account_profile(
        self,
        acct: AccountInfo,
        repos_snapshot: list[tuple[tuple[str, int | None], AccountRepository]],
    ) -> dict[str, str] | None:
        for (rid, _rpid), repo in repos_snapshot:
            if rid == acct.account_id:
                return repo.get_own_profile()

        contact_stamp: tuple[int, int] | None = None
        for cand in (
            Path(acct.db_storage_dir) / "contact" / "contact.db",
            Path(acct.db_storage_dir) / "contact.db",
        ):
            if cand.is_file():
                try:
                    st = cand.stat()
                    contact_stamp = (st.st_mtime_ns, st.st_size)
                except OSError:
                    contact_stamp = (-1, -1)
                break

        with self._registry_lock:
            cached = self._profile_cache.get(acct.account_id)
            if cached is not None and contact_stamp is not None and cached[0] == contact_stamp:
                return dict(cached[1])

        probed = probe_account_profile(
            acct,
            keys_file=self.keys_file,
            state_dir=self.state_dir,
            use_key_cache=self.use_key_cache,
            temp_root=self.temp_root,
        )
        if probed is not None:
            prof = {
                "wxid": probed["wxid"],
                "display_name": probed["display_name"],
                "alias": probed["alias"],
            }
            with self._registry_lock:
                self._profile_cache[acct.account_id] = (probed["stamp"], prof)
            return dict(prof)
        return None

    @staticmethod
    def _pick_self_profile(
        accounts_out: list[dict[str, Any]],
        selected_acct: str | None,
    ) -> dict[str, str] | None:
        chosen: dict[str, Any] | None = None
        if selected_acct:
            for row in accounts_out:
                if row.get("account_id") == selected_acct:
                    chosen = row
                    break
        if chosen is None:
            online_rows = [row for row in accounts_out if row.get("online")]
            if len(online_rows) == 1:
                chosen = online_rows[0]
            elif len(accounts_out) == 1:
                chosen = accounts_out[0]
        if chosen is None:
            return None
        return {
            "wxid": str(chosen.get("wxid") or ""),
            "display_name": str(chosen.get("display_name") or ""),
            "alias": str(chosen.get("alias") or ""),
        }

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
            prof = self._resolve_account_profile(acct, repos_snapshot)
            if prof:
                row["wxid"] = prof["wxid"] or row["wxid"]
                row["display_name"] = prof["display_name"]
                row["alias"] = prof["alias"]
            accounts_out.append(row)
        return {
            "ok": True,
            "db_dir": env["db_dir"],
            "selected_account": selected_acct,
            "self": self._pick_self_profile(accounts_out, selected_acct),
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

    @staticmethod
    def _is_cached_key_alive(cache_key: tuple[str, int | None]) -> bool:
        _acct_id, bound_pid = cache_key
        if bound_pid is None:
            return True
        return bool(psutil.pid_exists(bound_pid))

    def _open_or_reuse(
        self,
        *,
        account: str | None = None,
        pid: int | None = None,
        db_dir: str | None = None,
        keys_file: str | None = None,
        make_default: bool = False,
        refresh: bool = False,
    ) -> tuple[AccountRepository, RealtimeMonitor, AccountInfo]:
        eff_db_dir = db_dir or self.db_dir
        eff_keys = keys_file or self.keys_file
        db_key = _norm_path_key(eff_db_dir)
        keys_key = _norm_path_key(eff_keys)

        with self._registry_lock:
            if (
                not make_default
                and not refresh
                and account is None
                and pid is None
                and db_dir is None
                and keys_file is None
                and self._selected_key is not None
            ):
                key = self._selected_key
                repo = self._repos[key]
                mon = self._monitors[key]
                return repo, mon, repo.account

            if (
                not make_default
                and not refresh
                and (account is not None or pid is not None)
                and self._account_cache_ttl > 0
            ):
                ref_norm = account.strip().casefold() if account else ""
                alias_key = (ref_norm, pid, db_key, keys_key)
                cached = self._alias_cache.get(alias_key)
                if cached is not None:
                    cache_key, cached_at = cached
                    if (
                        (time.monotonic() - cached_at) <= self._account_cache_ttl
                        and cache_key in self._repos
                        and self._is_cached_key_alive(cache_key)
                    ):
                        repo = self._repos[cache_key]
                        mon = self._monitors[cache_key]
                        return repo, mon, repo.account
                    self._alias_cache.pop(alias_key, None)

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

        with self._registry_lock:
            if cache_key not in self._repos:
                repo = AccountRepository(
                    chosen,
                    state_dir=self.state_dir,
                    keys_file=eff_keys,
                    temp_root=self.temp_root,
                    target_pids=target_pids,
                    use_key_cache=self.use_key_cache,
                )
                mon = RealtimeMonitor(repo, self._state_store)
                self._repos[cache_key] = repo
                self._monitors[cache_key] = mon
            else:
                repo = self._repos[cache_key]
                mon = self._monitors[cache_key]

            now_mono = time.monotonic()
            self._alias_cache[(chosen.account_id.casefold(), pid, db_key, keys_key)] = (
                cache_key,
                now_mono,
            )
            self._alias_cache[(chosen.wxid.casefold(), pid, db_key, keys_key)] = (
                cache_key,
                now_mono,
            )
            if account:
                self._alias_cache[(account.strip().casefold(), pid, db_key, keys_key)] = (
                    cache_key,
                    now_mono,
                )
            if pid is not None and account is None:
                self._alias_cache[("", pid, db_key, keys_key)] = (cache_key, now_mono)

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
            refresh=True,
        )
        profile = repo.get_own_profile()
        chosen.selected = True
        chosen.display_name = profile["display_name"]
        chosen.alias = profile["alias"]
        return {
            "ok": True,
            "self": profile,
            "account": chosen.to_dict(),
            "total_dbs": repo.total_db_count,
            "keyed_dbs": repo.keyed_db_count,
            "unkeyed_dbs": repo.unkeyed_dbs,
        }

    def list_chats(
        self,
        *,
        chat_type: str = "all",
        name_query: str | None = None,
        query: str | None = None,
        limit: int = 50,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        eff_query = name_query if name_query is not None else query
        chats = repo.list_chats(chat_type=chat_type, query=eff_query, limit=limit)
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
        truncate: str = "head_tail",
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        _validate_truncate(truncate)
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
        ch_events = [
            ev
            for ev in result["messages"]
            if ev.msg_type == "chat_history" and ev.metadata.get("embedded_files")
        ]
        if ch_events:
            file_index = build_file_index(repo.account.data_dir)
            for ev in ch_events:
                ev.metadata["embedded_files"] = enrich_embedded_files(
                    repo.account.data_dir,
                    ev.metadata["embedded_files"],
                    ev.timestamp,
                    file_index=file_index,
                    verify_md5=True,
                )
        shaped, skipped_noise = shape_messages_batch(
            result["messages"],
            detail=detail,
            max_text_chars=max_text_chars,
            truncate=truncate,
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

    def expand_chat_history(
        self,
        chat: str,
        *,
        seq: int | None = None,
        message_id: str | int | None = None,
        max_text_chars: int | None = None,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        """Expand a forwarded chat_history message into items and resolve embedded files."""
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        chat_info = repo.resolve_chat(chat)
        raw_hit = repo.get_chat_history_raw(
            chat_info.chat_id,
            chat_name=chat_info.display_name,
            seq=seq,
            message_id=message_id,
        )
        ev = raw_hit["event"]
        parsed = parse_chat_history_xml(raw_hit["xml_body"], sender_id=ev.sender_id or None)
        file_index = build_file_index(repo.account.data_dir)

        def _enrich_items(
            items: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            out_items: list[dict[str, Any]] = []
            out_files: list[dict[str, Any]] = []
            for raw_item in items:
                item = dict(raw_item)
                item["untrusted"] = True
                if isinstance(item.get("text"), str):
                    txt = defuse_markdown_links(item["text"])
                    if max_text_chars is not None and max_text_chars > 0:
                        txt, was_trunc, orig_len = _truncate_text(
                            txt, max_text_chars, strategy="head_tail"
                        )
                        if was_trunc:
                            item["truncated"] = True
                            item["full_length"] = orig_len
                    item["text"] = txt
                if item.get("msg_type") == "file" and isinstance(item.get("meta"), dict):
                    enriched_list = enrich_embedded_files(
                        repo.account.data_dir,
                        [item["meta"]],
                        ev.timestamp,
                        file_index=file_index,
                        verify_md5=True,
                    )
                    if enriched_list:
                        f_meta = enriched_list[0]
                        item["meta"] = f_meta
                        item["available"] = f_meta["available"]
                        item["local_path"] = f_meta["local_path"]
                        if "next_action" in f_meta:
                            item["next_action"] = f_meta["next_action"]
                        out_files.append(dict(f_meta))
                if isinstance(item.get("items"), list):
                    sub_items, sub_files = _enrich_items(item["items"])
                    item["items"] = sub_items
                    out_files.extend(sub_files)
                out_items.append(item)
            return out_items, out_files

        items_out, embedded_files_out = _enrich_items(parsed["items"])
        if not embedded_files_out and parsed["embedded_files"]:
            embedded_files_out = enrich_embedded_files(
                repo.account.data_dir,
                parsed["embedded_files"],
                ev.timestamp,
                file_index=file_index,
                verify_md5=True,
            )

        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "chat": chat_info.to_dict(),
            "event_id": ev.event_id,
            "seq": ev.sort_seq,
            "timestamp": ev.timestamp,
            "ts_iso": format_local_iso(ev.timestamp, ev.iso_time),
            "sender": ev.sender_name or ev.sender_id or ("我" if ev.is_self else ""),
            "title": parsed["title"] or ev.metadata.get("title") or "",
            "item_count": len(items_out),
            "items": items_out,
            "embedded_files_count": len(embedded_files_out),
            "available_files_count": sum(1 for f in embedded_files_out if f.get("available")),
            "embedded_files": embedded_files_out,
        }
        payload["est_tokens"] = estimate_tokens(payload)
        return payload

    def search_messages(
        self,
        keyword: str,
        *,
        chat: str | None = None,
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
        chat_type: str = "all",
        since: int | float | str | None = None,
        until: int | float | str | None = None,
        min_activity: int | float | str | None = None,
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
        min_activity_ts = parse_min_activity(min_activity)
        search_res = repo.search_messages_detailed(
            keyword,
            chat_id=chat,
            chats=chats,
            exclude_chats=exclude_chats,
            chat_type=chat_type,
            since_ts=since_ts,
            until_ts=until_ts,
            min_activity_ts=min_activity_ts,
            limit=limit,
        )
        shaped, skipped_noise = shape_messages_batch(
            search_res["messages"],
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
            "total_matched": search_res["total_matched"],
            "scanned_chats": search_res["scanned_chats"],
            "truncated": search_res["truncated"],
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
        truncate: str = "head_tail",
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        _validate_truncate(truncate)
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
            truncate=truncate,
            skip_noise=False,
            include_chat=True,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "account_id": repo.account.account_id,
            "cursor_id": cursor_id,
            "initialized": status.initialized,
            "watch_initialized": status.watch_initialized,
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
        committed, already_committed = mon.ack_detailed(
            cursor_id=cursor_id, ack_token=ack_token, upto=upto
        )
        return {
            "ok": True,
            "account_id": repo.account.account_id,
            "cursor_id": cursor_id,
            "already_committed": already_committed,
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
        truncate: str = "head_tail",
        skip_noise: bool = False,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        _validate_truncate(truncate)
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
            truncate=truncate,
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
        chat_type: str | None = None,
        scope: str | None = None,
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
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
        effective_scope = _resolve_chat_type(chat_type, scope)
        if buckets not in ("hour", "day", "none"):
            raise WxObserverError("invalid_buckets", f"Unsupported buckets '{buckets}'.")
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        since_ts = parse_timestamp(since)
        until_ts = parse_timestamp(until)
        return build_digest(
            repo,
            scope=effective_scope,
            chats=chats,
            exclude_chats=exclude_chats,
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
        chat_type: str | None = None,
        scope: str | None = None,
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
        since: int | float | str | None = "7d",
        until: int | float | str | None = None,
        extensions: list[str] | None = None,
        only_available: bool = False,
        include_embedded: bool = True,
        limit: int = 50,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        effective_scope = _resolve_chat_type(chat_type, scope)
        repo, _mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        since_ts = parse_timestamp(since)
        until_ts = parse_timestamp(until)
        return collect_attachments(
            repo,
            scope=effective_scope,
            chats=chats,
            exclude_chats=exclude_chats,
            since_ts=since_ts,
            until_ts=until_ts,
            extensions=extensions,
            only_available=only_available,
            include_embedded=include_embedded,
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
        audit_info = {
            "audit_enabled": self.audit_logger.enabled,
            "audit_path": str(self.audit_logger.path) if self.audit_logger.path else None,
            "audit_calls_today": self.audit_logger.calls_today(),
            "key_cache_mode": self.key_cache_mode,
        }
        with self._registry_lock:
            selected_key = self._selected_key
            repos_snapshot = list(self._repos.items())
        if account is None and pid is None and selected_key is None:
            env = discover_environment(db_dir=self.db_dir)
            accounts_out: list[dict[str, Any]] = []
            for acct in env["accounts"]:
                row = acct.to_dict()
                prof = self._resolve_account_profile(acct, repos_snapshot)
                if prof:
                    row["wxid"] = prof["wxid"] or row["wxid"]
                    row["display_name"] = prof["display_name"]
                    row["alias"] = prof["alias"]
                accounts_out.append(row)
            online_count = sum(1 for a in env["accounts"] if a.online)
            ready = bool(online_count == 1 or (self.keys_file and len(env["accounts"]) == 1))
            return {
                "ok": True,
                "selected": False,
                "ready": ready,
                "watch_initialized": False,
                "self": self._pick_self_profile(accounts_out, None),
                "db_dir": env["db_dir"],
                "accounts_count": len(env["accounts"]),
                "online_accounts_count": online_count,
                "reason": env["reason"],
                **audit_info,
            }
        repo, mon, _chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        status = mon.get_status(cursor_id=cursor_id)
        return {
            "ok": True,
            "selected": True,
            "ready": status.ready,
            "watch_initialized": status.watch_initialized,
            "self": repo.get_own_profile(),
            **audit_info,
            "status": status.to_dict(),
        }

    def verify(
        self,
        *,
        account: str | None = None,
        pid: int | None = None,
    ) -> dict[str, Any]:
        """Run end-to-end discovery, decryption, quick_check, and latest-message verification."""
        env = discover_environment(db_dir=self.db_dir)
        accounts = env["accounts"]
        online_count = sum(1 for a in accounts if a.online)
        offline_count = len(accounts) - online_count
        repo, _mon, chosen = self._open_or_reuse(account=account, pid=pid, make_default=False)
        health = repo.verify_health()
        latest_ts = int(health["latest_timestamp"] or 0)
        ok = bool(health["quick_check_passed"])
        res: dict[str, Any] = {
            "ok": ok,
            "account_id": chosen.account_id,
            "self": repo.get_own_profile(),
            "accounts_total": len(accounts),
            "accounts_online": online_count,
            "accounts_offline": offline_count,
            "key_cache_mode": self.key_cache_mode,
            "total_dbs": health["total_dbs"],
            "keyed_dbs": health["keyed_dbs"],
            "verified_dbs": health["verified_dbs"],
            "failed_dbs": health["failed_dbs"],
            "quick_check_passed": health["quick_check_passed"],
            "wal_frames_merged": health["wal_frames_merged"],
            "chats_count": health["chats_count"],
            "latest_timestamp": latest_ts,
            "latest_t": format_compact_time(latest_ts) if latest_ts > 0 else "",
            "latest_iso": format_local_iso(latest_ts) if latest_ts > 0 else "",
            "warnings": health["warnings"],
        }
        if not ok:
            res["next_action"] = (
                "One or more databases failed key validation or quick_check; "
                "verify WeChat is logged in or run `wx-observer doctor --bundle doctor.zip`."
            )
        return res

    def doctor(
        self,
        *,
        account: str | None = None,
        pid: int | None = None,
        bundle_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Build a redacted diagnostic report and optionally package it into a zip bundle."""
        report = build_doctor_report(self, account=account, pid=pid)
        if bundle_path is not None:
            dest = write_doctor_bundle(report, bundle_path)
            report["bundle_path"] = str(dest)
        return report
