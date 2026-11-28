"""Typed data models for wx-observer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ChatType = Literal["private", "group"]
WatchMode = Literal["all", "private", "group", "allowlist"]


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    process_name: str
    windows: list[dict[str, Any]] = field(default_factory=list)
    open_db_dir: str | None = None
    open_account: str | None = None
    access_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AccountInfo:
    account_id: str
    wxid: str
    data_dir: str
    db_storage_dir: str
    online: bool
    pids: list[int] = field(default_factory=list)
    display_name: str = ""
    alias: str = ""
    last_activity: float = 0.0
    selected: bool = False
    access_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChatInfo:
    chat_id: str
    chat_type: ChatType
    display_name: str
    nick_name: str = ""
    remark: str = ""
    unread_count: int = 0
    last_timestamp: int = 0
    summary: str = ""
    last_sender: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class QuoteInfo:
    quoted_type: str
    excerpt: str
    sender_id: str | None = None
    sender_name: str | None = None
    server_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(slots=True)
class MessageEvent:
    event_id: str
    account_id: str
    chat_id: str
    chat_type: ChatType
    chat_name: str
    local_id: int
    server_id: int
    sort_seq: int
    shard: str
    timestamp: int
    iso_time: str
    msg_type: str
    raw_type: int
    sender_id: str
    sender_name: str
    is_self: bool
    text: str
    quote: QuoteInfo | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "event_id": self.event_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "chat_name": self.chat_name,
            "local_id": self.local_id,
            "server_id": self.server_id,
            "sort_seq": self.sort_seq,
            "shard": self.shard,
            "timestamp": self.timestamp,
            "iso_time": self.iso_time,
            "msg_type": self.msg_type,
            "raw_type": self.raw_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "is_self": self.is_self,
            "text": self.text,
        }
        if self.quote is not None:
            data["quote"] = self.quote.to_dict()
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(slots=True)
class WatchFilter:
    mode: WatchMode = "all"
    chats: list[str] = field(default_factory=list)
    exclude_chats: list[str] = field(default_factory=list)
    include_self: bool = False
    msg_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ObserverStatus:
    account_id: str | None
    wxid: str | None
    pid: int | None
    online: bool
    db_dir: str | None
    cursor_id: str
    initialized: bool
    initialized_at: int | None
    watch_filter: WatchFilter
    tracked_chats: int
    total_dbs: int
    keyed_dbs: int
    unkeyed_dbs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    watch_initialized: bool = False
    ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["watch_initialized"] = bool(self.watch_initialized or self.initialized)
        payload["ready"] = bool(self.ready)
        return payload
