"""Real-time message observer with persistent multi-cursor watermark tracking."""

from __future__ import annotations

import threading
import time
from typing import Iterator

from wx_observer.models import MessageEvent, ObserverStatus, WatchFilter
from wx_observer.repository import AccountRepository
from wx_observer.state import CursorStateStore


class RealtimeMonitor:
    """Polls decrypted database snapshots + WAL updates and emits deduplicated new messages."""

    def __init__(self, repo: AccountRepository, state_store: CursorStateStore) -> None:
        self.repo = repo
        self.state_store = state_store
        self._stop_event = threading.Event()
        self._last_fingerprints: dict[str, dict[str, tuple[int, int, int, int]]] = {}

    @property
    def account_id(self) -> str:
        return self.repo.account.account_id

    def stop(self) -> None:
        self._stop_event.set()

    def configure_watch(
        self,
        *,
        cursor_id: str = "default",
        watch_filter: WatchFilter | None = None,
        reset_cursor: bool = False,
    ) -> ObserverStatus:
        if reset_cursor:
            self.state_store.reset_cursor(self.account_id, cursor_id)
        if watch_filter is not None:
            self.state_store.save_watch_config(self.account_id, cursor_id, watch_filter)
        self._last_fingerprints.pop(cursor_id, None)
        return self.get_status(cursor_id=cursor_id)

    def get_status(self, cursor_id: str = "default") -> ObserverStatus:
        wf, init_at = self.state_store.get_watch_config(self.account_id, cursor_id)
        watermarks = self.state_store.get_watermarks(self.account_id, cursor_id)
        return ObserverStatus(
            account_id=self.account_id,
            wxid=self.repo.get_own_wxid(),
            pid=self.repo.target_pids[0] if self.repo.target_pids else None,
            online=self.repo.account.online,
            db_dir=os_dirname_safe(self.repo.account.data_dir),
            cursor_id=cursor_id,
            initialized=init_at is not None,
            initialized_at=init_at,
            watch_filter=wf,
            tracked_chats=len(watermarks),
            total_dbs=self.repo.total_db_count,
            keyed_dbs=self.repo.keyed_db_count,
            unkeyed_dbs=self.repo.unkeyed_dbs,
            warnings=self.repo.warnings,
        )

    @staticmethod
    def _chat_matches_filter(chat_id: str, wf: WatchFilter) -> bool:
        is_group = chat_id.endswith("@chatroom")
        if chat_id in wf.exclude_chats:
            return False
        if wf.mode == "private" and is_group:
            return False
        if wf.mode == "group" and not is_group:
            return False
        if wf.mode == "allowlist" and chat_id not in wf.chats:
            return False
        return True

    @staticmethod
    def _event_matches_filter(event: MessageEvent, wf: WatchFilter) -> bool:
        if not wf.include_self and event.is_self:
            return False
        if wf.msg_types:
            allowed = {t.strip().lower() for t in wf.msg_types if t.strip()}
            if allowed:
                type_match = event.msg_type.lower() in allowed
                quote_alias = "quote" in allowed and event.quote is not None
                if not (type_match or quote_alias):
                    return False
        return True

    def _ensure_baseline(self, cursor_id: str) -> tuple[WatchFilter, int, dict[str, int], bool]:
        wf, init_at = self.state_store.get_watch_config(self.account_id, cursor_id)
        if init_at is not None:
            watermarks = self.state_store.get_watermarks(self.account_id, cursor_id)
            return wf, init_at, watermarks, False

        now = int(time.time())
        chat_seqs = self.repo.discover_chat_max_seqs()
        for cid in wf.chats:
            chat_seqs.setdefault(cid, 0)
        self.state_store.commit_watermarks(
            self.account_id,
            cursor_id,
            chat_seqs,
            initialized_at=now,
        )
        self._last_fingerprints[cursor_id] = self.repo.get_shard_fingerprints()
        return wf, now, chat_seqs, True

    def poll(self, *, cursor_id: str = "default", limit: int = 100) -> list[MessageEvent]:
        """Return newly arrived messages since this cursor's last committed watermark."""
        limit = max(1, min(int(limit), 1000))
        wf, init_at, watermarks, just_initialized = self._ensure_baseline(cursor_id)
        if just_initialized:
            return []

        fingerprints = self.repo.get_shard_fingerprints()
        if self._last_fingerprints.get(cursor_id) == fingerprints:
            return []

        chat_max_seqs = self.repo.discover_chat_max_seqs()
        for cid in wf.chats:
            chat_max_seqs.setdefault(cid, 0)

        candidate_chats = [
            cid for cid in sorted(chat_max_seqs) if self._chat_matches_filter(cid, wf)
        ]

        pending: list[MessageEvent] = []
        watermark_advances: dict[str, int] = {}

        for cid in candidate_chats:
            max_seq = chat_max_seqs[cid]
            if cid in watermarks:
                since_seq = watermarks[cid]
                if max_seq <= since_seq:
                    continue
                new_rows = self.repo.get_new_messages_for_chat(cid, since_seq=since_seq)
            else:
                # Brand-new chat created after watch baseline was captured
                new_rows = self.repo.get_new_messages_for_chat(
                    cid,
                    since_seq=0,
                    since_ts=max(0, init_at - 5),
                )
                if not new_rows and max_seq > 0:
                    # Existing historical chat that had not been recorded yet: advance watermark cleanly
                    watermark_advances[cid] = max_seq
                    continue

            if not new_rows:
                continue

            matched = [ev for ev in new_rows if self._event_matches_filter(ev, wf)]
            if matched:
                pending.extend(matched)
            else:
                watermark_advances[cid] = max(ev.sort_seq for ev in new_rows)

        pending.sort(key=lambda e: (e.timestamp, e.sort_seq, e.chat_id, e.local_id))

        if len(pending) > limit:
            chosen = pending[:limit]
            # Never split a (chat_id, sort_seq) group at the batch boundary
            last_pair = (chosen[-1].chat_id, chosen[-1].sort_seq)
            next_pair = (pending[limit].chat_id, pending[limit].sort_seq)
            if last_pair == next_pair:
                trimmed = [
                    ev for ev in chosen if (ev.chat_id, ev.sort_seq) != last_pair
                ]
                if trimmed:
                    chosen = trimmed
                else:
                    # Single oversized sort_seq group: deliver the whole group together
                    chosen = [
                        ev for ev in pending if (ev.chat_id, ev.sort_seq) == last_pair
                    ]
        else:
            chosen = pending

        for ev in chosen:
            prev = watermark_advances.get(ev.chat_id, -1)
            if ev.sort_seq > prev:
                watermark_advances[ev.chat_id] = ev.sort_seq

        if watermark_advances:
            self.state_store.commit_watermarks(
                self.account_id,
                cursor_id,
                watermark_advances,
            )

        if len(chosen) == len(pending):
            self._last_fingerprints[cursor_id] = fingerprints

        return chosen

    def wait_for_messages(
        self,
        *,
        cursor_id: str = "default",
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        limit: int = 100,
    ) -> list[MessageEvent]:
        """Long-poll for new messages until at least one arrives or timeout elapses."""
        timeout = max(0.0, min(float(timeout_seconds), 120.0))
        interval = max(0.2, min(float(poll_interval), 5.0))
        deadline = time.monotonic() + timeout

        while True:
            events = self.poll(cursor_id=cursor_id, limit=limit)
            if events:
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop_event.is_set():
                return []
            self._stop_event.wait(min(interval, remaining))
            if self._stop_event.is_set():
                return []

    def watch_stream(
        self,
        *,
        cursor_id: str = "default",
        poll_interval: float = 1.0,
        max_events: int | None = None,
        duration_seconds: float | None = None,
    ) -> Iterator[MessageEvent]:
        """Yield live MessageEvents continuously until stop, max_events, or duration_seconds."""
        interval = max(0.2, min(float(poll_interval), 5.0))
        deadline = (
            time.monotonic() + float(duration_seconds)
            if duration_seconds is not None
            else None
        )
        emitted = 0

        # Ensure baseline is initialized on start even before first sleep
        for ev in self.poll(cursor_id=cursor_id):
            yield ev
            emitted += 1
            if max_events is not None and emitted >= max_events:
                return

        while not self._stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return
            wait_time = interval
            if deadline is not None:
                wait_time = min(wait_time, max(0.0, deadline - time.monotonic()))
            if wait_time > 0:
                self._stop_event.wait(wait_time)
            if self._stop_event.is_set():
                return
            for ev in self.poll(cursor_id=cursor_id):
                yield ev
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return


def os_dirname_safe(path: str) -> str:
    import os

    return os.path.dirname(path) if path else ""
