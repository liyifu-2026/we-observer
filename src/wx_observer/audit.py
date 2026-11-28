"""Privacy-safe append-only JSONL audit logger for wx-observer tool accesses."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from wx_observer.key_store import default_state_dir

DEFAULT_AUDIT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_AUDIT_BACKUPS = 3

_SAFE_SCALAR_KEYS = (
    "detail",
    "limit",
    "cursor_id",
    "peek",
    "only_available",
    "include_embedded",
    "buckets",
    "truncate",
    "chat_type",
    "scope",
    "since",
    "until",
    "min_activity",
    "include_self",
    "skip_noise",
    "max_chats",
    "token_budget",
    "reset_cursor",
    "mode",
    "before_seq",
    "after_seq",
    "seq",
)


def sha256_prefix(value: str, length: int = 12) -> str:
    """Return an irreversible SHA-256 hex prefix for privacy-preserving audit entries."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def _count_rows(result: dict[str, Any]) -> tuple[int, str]:
    if isinstance(result.get("per_chat"), list):
        summary = result.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("total_messages"), int):
            return int(summary["total_messages"]), "matched_messages"
        return len(result["per_chat"]), "per_chat"
    for key in ("messages", "chats", "attachments", "items", "accounts"):
        val = result.get(key)
        if isinstance(val, list):
            return len(val), key
    return 0, "none"


class AuditLogger:
    """Thread-safe rotating JSONL audit logger storing only whitelisted fields and SHA-256 digests."""

    def __init__(
        self,
        mode_or_path: str | Path | None = None,
        *,
        state_dir: str | Path | None = None,
        max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
        backup_count: int = DEFAULT_AUDIT_BACKUPS,
    ) -> None:
        raw = os.environ.get("WX_OBSERVER_AUDIT") if mode_or_path is None else str(mode_or_path)
        norm = (raw or "").strip()
        low = norm.lower()

        if low in ("off", "false", "0", "none"):
            self.enabled = False
            self.path: Path | None = None
        elif low in ("", "on", "true", "1"):
            self.enabled = True
            root = Path(state_dir) if state_dir else default_state_dir()
            self.path = root / "audit.jsonl"
        else:
            self.enabled = True
            self.path = Path(norm)

        self.max_bytes = max(1024, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._lock = threading.Lock()
        self._today_date: str | None = None
        self._today_count: int = 0

    def _rotate_if_needed(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return

        if self.backup_count <= 0:
            try:
                self.path.unlink()
            except OSError:
                pass
            return

        oldest = Path(f"{self.path}.{self.backup_count}")
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass

        for idx in range(self.backup_count - 1, 0, -1):
            src = Path(f"{self.path}.{idx}")
            dst = Path(f"{self.path}.{idx + 1}")
            if src.exists():
                try:
                    os.replace(src, dst)
                except OSError:
                    pass

        try:
            os.replace(self.path, Path(f"{self.path}.1"))
        except OSError:
            pass

    def _count_file_today(self, today_prefix: str) -> int:
        if self.path is None or not self.path.is_file():
            return 0
        count = 0
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = str(obj.get("ts") or "")
                    if ts.startswith(today_prefix):
                        count += 1
        except OSError:
            return count
        return count

    def _sync_today_locked(self, today_prefix: str) -> None:
        if self._today_date != today_prefix:
            self._today_date = today_prefix
            self._today_count = self._count_file_today(today_prefix)

    def calls_today(self) -> int:
        """Return the number of audited tool calls recorded on the current local calendar day."""
        if not self.enabled or self.path is None:
            return 0
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            self._sync_today_locked(today_prefix)
            return self._today_count

    def record(
        self,
        tool: str,
        params: dict[str, Any] | None,
        result: dict[str, Any] | None,
    ) -> None:
        """Append one sanitized audit record without ever storing plaintext chat names or keywords."""
        if not self.enabled or self.path is None:
            return

        now_dt = datetime.now().astimezone()
        today_prefix = now_dt.strftime("%Y-%m-%d")
        p = params or {}
        r = result or {}

        entry: dict[str, Any] = {
            "ts": now_dt.isoformat(timespec="seconds"),
            "tool": tool,
        }

        if p.get("account"):
            entry["account"] = sha256_prefix(str(p["account"]))
        elif r.get("account_id"):
            entry["account"] = sha256_prefix(str(r["account_id"]))

        if p.get("chat"):
            entry["chat"] = sha256_prefix(str(p["chat"]))
        elif isinstance(r.get("chat"), dict) and r["chat"].get("chat_id"):
            entry["chat"] = sha256_prefix(str(r["chat"]["chat_id"]))

        if isinstance(p.get("chats"), list):
            entry["chats_count"] = len(p["chats"])
        if isinstance(p.get("exclude_chats"), list):
            entry["exclude_chats_count"] = len(p["exclude_chats"])
        if p.get("keyword"):
            entry["keyword_sha256"] = sha256_prefix(str(p["keyword"]))
        query_val = p.get("name_query") if p.get("name_query") is not None else p.get("query")
        if query_val:
            entry["name_query_sha256"] = sha256_prefix(str(query_val))
        if p.get("message_id"):
            entry["message_id_sha256"] = sha256_prefix(str(p["message_id"]))

        for key in _SAFE_SCALAR_KEYS:
            val = p.get(key)
            if val is not None:
                entry[key] = val

        ok = bool(r.get("ok", True))
        rows_count, rows_kind = _count_rows(r)
        entry["rows"] = rows_count
        entry["rows_kind"] = rows_kind
        if isinstance(r.get("per_chat"), list):
            entry["returned_chats"] = len(r["per_chat"])
            summary = r.get("summary")
            if isinstance(summary, dict) and isinstance(summary.get("total_messages"), int):
                entry["matched_messages"] = int(summary["total_messages"])
        if isinstance(r.get("est_tokens"), int):
            entry["est_tokens"] = int(r["est_tokens"])
        entry["ok"] = ok
        if not ok and r.get("error"):
            entry["error"] = str(r["error"])

        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self._sync_today_locked(today_prefix)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
                self._today_count += 1
            except OSError:
                pass
