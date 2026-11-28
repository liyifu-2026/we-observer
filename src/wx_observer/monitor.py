"""Real-time message observer with persistent multi-cursor watermark tracking and peek/ack."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

from wx_observer.exceptions import WxObserverError
from wx_observer.models import MessageEvent, ObserverStatus, WatchFilter
from wx_observer.repository import AccountRepository
from wx_observer.shaping import is_noise_message
from wx_observer.state import CursorStateStore


@dataclass(slots=True)
class PollBatch:
    events: list[MessageEvent]
    ack_token: str | None = None
    pending_watermarks: dict[str, int] = field(default_factory=dict)
    skipped_noise: int = 0


class RealtimeMonitor:
    """Polls decrypted database snapshots + WAL updates and emits deduplicated new messages."""

    def __init__(self, repo: AccountRepository, state_store: CursorStateStore) -> None:
        self.repo = repo
        self.state_store = state_store
        self._stop_event = threading.Event()
        self._last_fingerprints: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        self._cursor_locks: dict[str, threading.Lock] = {}
        self._cursor_locks_guard = threading.Lock()

    @property
    def account_id(self) -> str:
        return self.repo.account.account_id

    def _cursor_lock(self, cursor_id: str) -> threading.Lock:
        with self._cursor_locks_guard:
            return self._cursor_locks.setdefault(cursor_id, threading.Lock())

    def stop(self) -> None:
        self._stop_event.set()

    def configure_watch(
        self,
        *,
        cursor_id: str = "default",
        watch_filter: WatchFilter | None = None,
        reset_cursor: bool = False,
    ) -> ObserverStatus:
        with self._cursor_lock(cursor_id):
            if reset_cursor:
                self.state_store.reset_cursor(self.account_id, cursor_id)
            if watch_filter is not None:
                self.state_store.save_watch_config(self.account_id, cursor_id, watch_filter)
            self._last_fingerprints.pop(cursor_id, None)
            return self.get_status(cursor_id=cursor_id)

    def get_status(self, cursor_id: str = "default") -> ObserverStatus:
        wf, init_at = self.state_store.get_watch_config(self.account_id, cursor_id)
        watermarks = self.state_store.get_watermarks(self.account_id, cursor_id)
        init_done = init_at is not None
        return ObserverStatus(
            account_id=self.account_id,
            wxid=self.repo.get_own_wxid(),
            pid=self.repo.target_pids[0] if self.repo.target_pids else None,
            online=self.repo.account.online,
            db_dir=os_dirname_safe(self.repo.account.data_dir),
            cursor_id=cursor_id,
            initialized=init_done,
            initialized_at=init_at,
            watch_filter=wf,
            tracked_chats=len(watermarks),
            total_dbs=self.repo.total_db_count,
            keyed_dbs=self.repo.keyed_db_count,
            unkeyed_dbs=self.repo.unkeyed_dbs,
            warnings=self.repo.warnings,
            watch_initialized=init_done,
            ready=self.repo.keyed_db_count > 0,
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

    def poll_batch(
        self,
        *,
        cursor_id: str = "default",
        limit: int = 100,
        peek: bool = False,
        skip_noise: bool = False,
    ) -> PollBatch:
        """Poll newly arrived messages, optionally holding watermarks pending explicit ack()."""
        limit = max(1, min(int(limit), 1000))
        with self._cursor_lock(cursor_id):
            wf, init_at, watermarks, just_initialized = self._ensure_baseline(cursor_id)
            if just_initialized:
                return PollBatch(events=[])

            fingerprints = self.repo.get_shard_fingerprints()
            if self._last_fingerprints.get(cursor_id) == fingerprints:
                return PollBatch(events=[])

            chat_max_seqs = self.repo.discover_chat_max_seqs()
            for cid in wf.chats:
                chat_max_seqs.setdefault(cid, 0)

            candidate_chats = [
                cid for cid in sorted(chat_max_seqs) if self._chat_matches_filter(cid, wf)
            ]

            pending: list[MessageEvent] = []
            filtered_advances: dict[str, int] = {}
            skipped_noise = 0

            for cid in candidate_chats:
                max_seq = chat_max_seqs[cid]
                if cid in watermarks:
                    since_seq = watermarks[cid]
                    if max_seq <= since_seq:
                        continue
                    new_rows = self.repo.get_new_messages_for_chat(cid, since_seq=since_seq)
                else:
                    new_rows = self.repo.get_new_messages_for_chat(
                        cid,
                        since_seq=0,
                        since_ts=max(0, init_at - 5),
                    )
                    if not new_rows and max_seq > 0:
                        filtered_advances[cid] = max_seq
                        continue

                if not new_rows:
                    continue

                matched: list[MessageEvent] = []
                for ev in new_rows:
                    if not self._event_matches_filter(ev, wf):
                        continue
                    if skip_noise and is_noise_message(ev):
                        skipped_noise += 1
                        continue
                    matched.append(ev)

                if matched:
                    pending.extend(matched)
                else:
                    filtered_advances[cid] = max(ev.sort_seq for ev in new_rows)

            pending.sort(key=lambda e: (e.timestamp, e.sort_seq, e.chat_id, e.local_id))

            if len(pending) > limit:
                chosen = pending[:limit]
                last_pair = (chosen[-1].chat_id, chosen[-1].sort_seq)
                next_pair = (pending[limit].chat_id, pending[limit].sort_seq)
                if last_pair == next_pair:
                    trimmed = [
                        ev for ev in chosen if (ev.chat_id, ev.sort_seq) != last_pair
                    ]
                    if trimmed:
                        chosen = trimmed
                    else:
                        chosen = [
                            ev for ev in pending if (ev.chat_id, ev.sort_seq) == last_pair
                        ]
            else:
                chosen = pending

            chosen_watermarks: dict[str, int] = {}
            for ev in chosen:
                prev = chosen_watermarks.get(ev.chat_id, -1)
                if ev.sort_seq > prev:
                    chosen_watermarks[ev.chat_id] = ev.sort_seq

            if peek:
                if filtered_advances:
                    self.state_store.commit_watermarks(
                        self.account_id,
                        cursor_id,
                        filtered_advances,
                    )
                ack_token: str | None = None
                if chosen_watermarks:
                    ack_token = self.state_store.save_pending_ack(
                        self.account_id,
                        cursor_id,
                        chosen_watermarks,
                    )
                elif not pending:
                    self._last_fingerprints[cursor_id] = fingerprints
                return PollBatch(
                    events=chosen,
                    ack_token=ack_token,
                    pending_watermarks=chosen_watermarks,
                    skipped_noise=skipped_noise,
                )

            all_advances = dict(filtered_advances)
            for cid, seq in chosen_watermarks.items():
                if seq > all_advances.get(cid, -1):
                    all_advances[cid] = seq

            if all_advances:
                self.state_store.commit_watermarks(
                    self.account_id,
                    cursor_id,
                    all_advances,
                )

            if len(chosen) == len(pending):
                self._last_fingerprints[cursor_id] = fingerprints

            return PollBatch(
                events=chosen,
                ack_token=None,
                pending_watermarks={},
                skipped_noise=skipped_noise,
            )

    def poll(self, *, cursor_id: str = "default", limit: int = 100) -> list[MessageEvent]:
        """Auto-committing poll preserving backward compatibility for CLI and direct callers."""
        return self.poll_batch(cursor_id=cursor_id, limit=limit, peek=False).events

    def ack_detailed(
        self,
        *,
        cursor_id: str = "default",
        ack_token: str | None = None,
        upto: dict[str, int] | None = None,
    ) -> tuple[dict[str, int], bool]:
        """Explicitly and idempotently commit watermarks from a previous peek=True poll."""
        with self._cursor_lock(cursor_id):
            if ack_token:
                committed, already_committed = self.state_store.apply_pending_ack(
                    self.account_id,
                    cursor_id,
                    ack_token,
                )
                if not already_committed:
                    self._last_fingerprints.pop(cursor_id, None)
                return committed, already_committed
            elif upto:
                clean_upto = {str(k): int(v) for k, v in upto.items()}
                self.state_store.commit_watermarks(
                    self.account_id,
                    cursor_id,
                    clean_upto,
                )
                self._last_fingerprints.pop(cursor_id, None)
                return clean_upto, False
            else:
                raise WxObserverError(
                    "invalid_ack",
                    "Provide either ack_token or a non-empty upto {chat_id: sort_seq} mapping.",
                )

    def ack(
        self,
        *,
        cursor_id: str = "default",
        ack_token: str | None = None,
        upto: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """Explicitly commit watermarks from a previous peek=True poll."""
        committed, _already_committed = self.ack_detailed(
            cursor_id=cursor_id,
            ack_token=ack_token,
            upto=upto,
        )
        return committed

    def wait_for_batch(
        self,
        *,
        cursor_id: str = "default",
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        limit: int = 100,
        peek: bool = False,
        skip_noise: bool = False,
    ) -> PollBatch:
        """Long-poll for new messages until at least one signal message arrives or timeout elapses."""
        timeout = max(0.0, min(float(timeout_seconds), 120.0))
        interval = max(0.2, min(float(poll_interval), 5.0))
        deadline = time.monotonic() + timeout
        total_noise = 0

        while True:
            batch = self.poll_batch(
                cursor_id=cursor_id,
                limit=limit,
                peek=peek,
                skip_noise=skip_noise,
            )
            total_noise += batch.skipped_noise
            if batch.events:
                batch.skipped_noise = total_noise
                return batch
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop_event.is_set():
                return PollBatch(events=[], skipped_noise=total_noise)
            self._stop_event.wait(min(interval, remaining))
            if self._stop_event.is_set():
                return PollBatch(events=[], skipped_noise=total_noise)

    def wait_for_messages(
        self,
        *,
        cursor_id: str = "default",
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        limit: int = 100,
    ) -> list[MessageEvent]:
        """Auto-committing long-poll returning MessageEvent list."""
        return self.wait_for_batch(
            cursor_id=cursor_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            limit=limit,
            peek=False,
        ).events

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
