"""SQLite-backed persistent watch filter and multi-consumer cursor store."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from wx_observer.key_store import default_state_dir
from wx_observer.models import WatchFilter


class CursorStateStore:
    """Persist watch configurations and per-consumer chat watermarks in SQLite."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        root = Path(state_dir) if state_dir else default_state_dir()
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "state.db"
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS watch_configs (
                        account_id TEXT NOT NULL,
                        cursor_id TEXT NOT NULL,
                        mode TEXT NOT NULL DEFAULT 'all',
                        chats_json TEXT NOT NULL DEFAULT '[]',
                        exclude_json TEXT NOT NULL DEFAULT '[]',
                        include_self INTEGER NOT NULL DEFAULT 0,
                        msg_types_json TEXT NOT NULL DEFAULT '[]',
                        initialized_at INTEGER,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (account_id, cursor_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_watermarks (
                        account_id TEXT NOT NULL,
                        cursor_id TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        last_sort_seq INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (account_id, cursor_id, chat_id)
                    )
                    """
                )
        finally:
            conn.close()

    def get_watch_config(
        self,
        account_id: str,
        cursor_id: str = "default",
    ) -> tuple[WatchFilter, int | None]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT mode, chats_json, exclude_json, include_self, msg_types_json, initialized_at
                FROM watch_configs
                WHERE account_id = ? AND cursor_id = ?
                """,
                (account_id, cursor_id),
            ).fetchone()
            if row is None:
                return WatchFilter(), None
            wf = WatchFilter(
                mode=row["mode"],  # type: ignore[arg-type]
                chats=json.loads(row["chats_json"] or "[]"),
                exclude_chats=json.loads(row["exclude_json"] or "[]"),
                include_self=bool(row["include_self"]),
                msg_types=json.loads(row["msg_types_json"] or "[]"),
            )
            init_at = int(row["initialized_at"]) if row["initialized_at"] is not None else None
            return wf, init_at
        finally:
            conn.close()

    def save_watch_config(
        self,
        account_id: str,
        cursor_id: str,
        watch_filter: WatchFilter,
    ) -> None:
        now = int(time.time())
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO watch_configs (
                        account_id, cursor_id, mode, chats_json, exclude_json,
                        include_self, msg_types_json, initialized_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(account_id, cursor_id) DO UPDATE SET
                        mode = excluded.mode,
                        chats_json = excluded.chats_json,
                        exclude_json = excluded.exclude_json,
                        include_self = excluded.include_self,
                        msg_types_json = excluded.msg_types_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_id,
                        cursor_id,
                        watch_filter.mode,
                        json.dumps(watch_filter.chats, ensure_ascii=False),
                        json.dumps(watch_filter.exclude_chats, ensure_ascii=False),
                        1 if watch_filter.include_self else 0,
                        json.dumps(watch_filter.msg_types, ensure_ascii=False),
                        now,
                    ),
                )
        finally:
            conn.close()

    def get_watermarks(self, account_id: str, cursor_id: str = "default") -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT chat_id, last_sort_seq
                FROM chat_watermarks
                WHERE account_id = ? AND cursor_id = ?
                """,
                (account_id, cursor_id),
            ).fetchall()
            return {str(r["chat_id"]): int(r["last_sort_seq"]) for r in rows}
        finally:
            conn.close()

    def commit_watermarks(
        self,
        account_id: str,
        cursor_id: str,
        updates: dict[str, int],
        *,
        initialized_at: int | None = None,
    ) -> None:
        """Atomically persist baseline initialization and/or watermark advances."""
        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if initialized_at is not None:
                conn.execute(
                    """
                    INSERT INTO watch_configs (
                        account_id, cursor_id, mode, chats_json, exclude_json,
                        include_self, msg_types_json, initialized_at, updated_at
                    ) VALUES (?, ?, 'all', '[]', '[]', 0, '[]', ?, ?)
                    ON CONFLICT(account_id, cursor_id) DO UPDATE SET
                        initialized_at = COALESCE(watch_configs.initialized_at, excluded.initialized_at),
                        updated_at = excluded.updated_at
                    """,
                    (account_id, cursor_id, initialized_at, now),
                )
            for chat_id, seq in updates.items():
                conn.execute(
                    """
                    INSERT INTO chat_watermarks (
                        account_id, cursor_id, chat_id, last_sort_seq, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, cursor_id, chat_id) DO UPDATE SET
                        last_sort_seq = MAX(chat_watermarks.last_sort_seq, excluded.last_sort_seq),
                        updated_at = excluded.updated_at
                    """,
                    (account_id, cursor_id, chat_id, int(seq), now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_cursor(self, account_id: str, cursor_id: str = "default") -> None:
        """Clear watermarks and baseline initialization for one cursor only."""
        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM chat_watermarks WHERE account_id = ? AND cursor_id = ?",
                (account_id, cursor_id),
            )
            conn.execute(
                """
                UPDATE watch_configs
                SET initialized_at = NULL, updated_at = ?
                WHERE account_id = ? AND cursor_id = ?
                """,
                (now, account_id, cursor_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
