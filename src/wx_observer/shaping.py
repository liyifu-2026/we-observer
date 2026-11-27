"""Token-efficient message shaping, noise filtering, snippets, and token estimation."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from wx_observer.models import MessageEvent

NOISE_MSG_TYPES = frozenset({"emoji"})
LOW_SIGNAL_MSG_TYPES = frozenset({"system"})

_COMPACT_META_KEYS = (
    "filename",
    "size_bytes",
    "file_ext",
    "md5",
    "url",
    "title",
    "local_path",
    "available",
    "embedded_files",
)


def is_noise_message(event: MessageEvent) -> bool:
    """Return True for pure chat noise such as animated sticker/emoji placeholders."""
    return event.msg_type in NOISE_MSG_TYPES


def estimate_tokens(payload: Any) -> int:
    """Estimate LLM token footprint for serialized JSON output (mixed CJK / ASCII)."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if not raw:
        return 0
    cjk = sum(1 for ch in raw if "\u4e00" <= ch <= "\u9fff")
    other = len(raw) - cjk
    return max(1, int(round(cjk * 1.1 + other * 0.28)))


def format_compact_time(ts: int, iso_fallback: str = "") -> str:
    if ts > 0:
        try:
            dt = datetime.fromtimestamp(ts)
            if dt.year == datetime.now().year:
                return dt.strftime("%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            pass
    return iso_fallback[:16] if iso_fallback else ""


def extract_snippet(text: str, keyword: str, *, radius: int = 80) -> tuple[str, int, bool]:
    """Return (snippet, match_offset, truncated) centered around the first keyword match."""
    body = text or ""
    kw = (keyword or "").strip()
    if not body or not kw:
        return body, -1, False
    idx = body.casefold().find(kw.casefold())
    if idx < 0:
        limit = radius * 2
        if len(body) <= limit:
            return body, -1, False
        return body[:limit].rstrip() + "...", -1, True

    start = max(0, idx - radius)
    end = min(len(body), idx + len(kw) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(body) else ""
    return f"{prefix}{body[start:end]}{suffix}", idx, (start > 0 or end < len(body))


def _truncate_text(text: str, max_text_chars: int | None) -> tuple[str, bool, int]:
    orig = text or ""
    orig_len = len(orig)
    if max_text_chars is not None and max_text_chars > 0 and orig_len > max_text_chars:
        return orig[:max_text_chars].rstrip() + "...", True, orig_len
    return orig, False, orig_len


def shape_message(
    event: MessageEvent,
    *,
    detail: str = "compact",
    max_text_chars: int | None = 300,
    include_chat: bool = False,
    snippet_keyword: str | None = None,
) -> dict[str, Any]:
    """Project a MessageEvent into compact (LLM-friendly) or full (raw) dictionary form."""
    if detail not in ("compact", "full"):
        raise ValueError(f"Unsupported detail mode: {detail}")

    if snippet_keyword:
        radius = max(40, (max_text_chars // 2) if (max_text_chars and max_text_chars > 0) else 80)
        rendered_text, match_offset, was_truncated = extract_snippet(
            event.text, snippet_keyword, radius=radius
        )
        orig_len = len(event.text or "")
    else:
        rendered_text, was_truncated, orig_len = _truncate_text(event.text, max_text_chars)
        match_offset = -1

    if detail == "full":
        out = event.to_dict()
        if was_truncated:
            out["text"] = rendered_text
            out["truncated"] = True
            out["full_length"] = orig_len
        if snippet_keyword and match_offset >= 0:
            out["match_offset"] = match_offset
        return out

    sender = event.sender_name or event.sender_id or ("我" if event.is_self else "")
    out = {
        "t": format_compact_time(event.timestamp, event.iso_time),
        "sender": sender,
        "type": event.msg_type,
        "text": rendered_text,
        "seq": event.sort_seq,
    }
    if include_chat:
        out["chat"] = event.chat_name or event.chat_id
        out["chat_id"] = event.chat_id
    if event.is_self:
        out["self"] = True
    if event.msg_type in LOW_SIGNAL_MSG_TYPES:
        out["noise"] = True
    if was_truncated:
        out["truncated"] = True
        out["full_length"] = orig_len
    if snippet_keyword and match_offset >= 0:
        out["match_offset"] = match_offset
    if event.quote is not None and event.quote.excerpt:
        q_who = event.quote.sender_name or event.quote.sender_id or ""
        excerpt = event.quote.excerpt[:100]
        out["quote"] = f"{q_who}: {excerpt}" if q_who else excerpt
    if event.metadata:
        compact_meta = {
            k: event.metadata[k]
            for k in _COMPACT_META_KEYS
            if k in event.metadata and event.metadata[k] not in (None, "", [])
        }
        if compact_meta:
            out["meta"] = compact_meta
    return out


def shape_messages_batch(
    events: list[MessageEvent],
    *,
    detail: str = "compact",
    max_text_chars: int | None = 300,
    skip_noise: bool = True,
    include_chat: bool = False,
    snippet_keyword: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Filter optional noise messages and project events into target detail format."""
    skipped_noise = 0
    shaped: list[dict[str, Any]] = []
    for ev in events:
        if skip_noise and is_noise_message(ev):
            skipped_noise += 1
            continue
        shaped.append(
            shape_message(
                ev,
                detail=detail,
                max_text_chars=max_text_chars,
                include_chat=include_chat,
                snippet_keyword=snippet_keyword,
            )
        )
    return shaped, skipped_noise
