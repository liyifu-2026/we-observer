"""Server-side cross-chat digest and reduction for token-efficient Agent consumption."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from wx_observer.attachments import (
    ATTACHMENT_NOT_DOWNLOADED_ACTION,
    build_file_index,
    resolve_local_file,
)
from wx_observer.models import MessageEvent
from wx_observer.shaping import (
    _truncate_text,
    defuse_markdown_links,
    estimate_tokens,
    format_compact_time,
    format_local_iso,
    is_noise_message,
    shape_message,
)

ALL_DIGEST_SECTIONS = frozenset(
    {"counts", "top_senders", "links", "attachments", "mentions", "highlights"}
)
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+")
_MENTION_RE = re.compile(r"(?:^|(?<=[\s,，。:：;；!！?？(（\[【]))@([^\s@\u2005]{1,40})")


def _bucket_label(ts: int, buckets: str) -> str:
    if ts <= 0 or buckets == "none":
        return ""
    try:
        dt = datetime.fromtimestamp(ts)
        if buckets == "day":
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT%H:00")
    except (OSError, OverflowError, ValueError):
        return ""


def _highlight_score(ev: MessageEvent) -> tuple[int, int, int]:
    if ev.msg_type == "system":
        return (0, len(ev.text or ""), ev.timestamp)
    priority = 1
    if ev.msg_type in ("file", "link", "chat_history"):
        priority = 4
    elif ev.quote is not None or "@" in (ev.text or ""):
        priority = 3
    elif ev.msg_type == "text" and len(ev.text or "") >= 15:
        priority = 2
    return (priority, len(ev.text or ""), ev.timestamp)


def _apply_digest_budget(result: dict[str, Any], token_budget: int) -> None:
    """Enforce output est_tokens <= token_budget (when > 0) while preserving global summary counts."""
    est = estimate_tokens(result)
    if token_budget <= 0 or est <= token_budget:
        result["est_tokens"] = est
        return

    result["summary"]["budget_trimmed"] = True

    def _under_budget() -> bool:
        return estimate_tokens(result) <= token_budget

    # Stage 1: Cap top-level cross-chat lists progressively
    for cap in (30, 15, 8, 4, 2):
        for key in ("links", "attachments", "mentions"):
            if key in result and len(result[key]) > cap:
                result[key] = result[key][:cap]
        if cap <= 8 and "top_senders" in result and len(result["top_senders"]) > 5:
            result["top_senders"] = result["top_senders"][:5]
        if _under_budget():
            result["est_tokens"] = estimate_tokens(result)
            return

    # Stage 2: Trim per-chat highlights count progressively
    per_chat: list[dict[str, Any]] = result["per_chat"]
    for hi_cap in (4, 2, 1, 0):
        for entry in per_chat:
            if "highlights" in entry:
                if hi_cap == 0:
                    entry.pop("highlights", None)
                elif len(entry["highlights"]) > hi_cap:
                    entry["highlights"] = entry["highlights"][:hi_cap]
        if _under_budget():
            result["est_tokens"] = estimate_tokens(result)
            return

    # Stage 3: Drop per-chat buckets & top_senders from lower-activity chats upward
    for idx in range(len(per_chat) - 1, -1, -1):
        entry = per_chat[idx]
        changed = False
        for k in ("buckets", "top_senders", "by_type"):
            if k in entry:
                entry.pop(k, None)
                changed = True
        if changed and _under_budget():
            result["est_tokens"] = estimate_tokens(result)
            return

    # Stage 4: Drop lowest-activity per_chat entries (keeping >= 1 if non-empty)
    while len(per_chat) > 1 and not _under_budget():
        per_chat.pop()
        result["summary"]["omitted_chats"] += 1

    # Stage 5: Final fallback for ultra-small explicit budgets
    for key in ("links", "attachments", "mentions", "top_senders"):
        if _under_budget():
            break
        if key in result and result[key]:
            result[key] = []

    result["est_tokens"] = estimate_tokens(result)


def build_digest(
    repo: Any,
    *,
    scope: str = "all",
    chats: list[str] | None = None,
    exclude_chats: list[str] | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
    buckets: str = "hour",
    include: list[str] | None = None,
    max_highlights_per_chat: int = 8,
    max_text_chars: int = 160,
    max_chats: int = 20,
    token_budget: int = 4000,
    include_self: bool = True,
) -> dict[str, Any]:
    """Aggregate messages across chats into a structured, token-budgeted digest."""
    if buckets not in ("hour", "day", "none"):
        raise ValueError(f"Unsupported buckets mode: {buckets}")

    max_chats_clamped = max(1, min(int(max_chats), 200))
    budget_clamped = max(0, int(token_budget))

    sections = (
        {s.strip().lower() for s in include if s and s.strip()}
        if include
        else set(ALL_DIGEST_SECTIONS)
    )

    excluded_ids = {
        repo.resolve_chat(ref).chat_id
        for ref in (exclude_chats or [])
        if ref and ref.strip()
    }
    explicit_chats = bool(chats)
    if chats:
        target_chats = [repo.resolve_chat(ref) for ref in chats]
    else:
        chat_type = scope if scope in ("private", "group") else "all"
        target_chats = repo.list_chats(
            chat_type=chat_type,
            limit=10_000,
            include_hidden=True,
            include_non_session=(since_ts is None),
        )
    if excluded_ids:
        target_chats = [c for c in target_chats if c.chat_id not in excluded_ids]

    file_index = build_file_index(repo.account.data_dir) if "attachments" in sections else None
    per_chat_out: list[dict[str, Any]] = []
    global_senders: Counter[tuple[str, bool]] = Counter()
    links_out: list[dict[str, Any]] = []
    seen_links: set[tuple[str, str]] = set()
    attachments_out: list[dict[str, Any]] = []
    mentions_out: list[dict[str, Any]] = []

    total_messages = 0
    total_noise = 0
    active_chats = 0

    eligible_chats = [
        chat
        for chat in target_chats
        if explicit_chats
        or since_ts is None
        or (chat.last_timestamp > 0 and chat.last_timestamp >= since_ts - 60)
    ]
    batch_by_chat = repo.get_messages_batch(
        eligible_chats,
        limit_per_chat=1000,
        since_ts=since_ts,
        until_ts=until_ts,
        include_self=include_self,
    )

    for chat in eligible_chats:
        msgs: list[MessageEvent] = batch_by_chat.get(chat.chat_id, [])
        if not msgs:
            continue

        active_chats += 1
        chat_senders: Counter[tuple[str, bool]] = Counter()
        chat_buckets: dict[str, int] = defaultdict(int)
        chat_type_counts: Counter[str] = Counter()
        signal_msgs: list[MessageEvent] = []
        chat_noise = 0

        for ev in msgs:
            total_messages += 1
            chat_type_counts[ev.msg_type] += 1
            sender_label = ev.sender_name or ev.sender_id or ("我" if ev.is_self else "未知")
            if is_noise_message(ev):
                chat_noise += 1
                total_noise += 1
                continue

            signal_msgs.append(ev)
            sender_key = (sender_label, bool(ev.is_self))
            chat_senders[sender_key] += 1
            global_senders[sender_key] += 1

            b_key = _bucket_label(ev.timestamp, buckets)
            if b_key:
                chat_buckets[b_key] += 1

            if "links" in sections:
                urls: list[tuple[str, str]] = []
                if ev.msg_type == "link" and ev.metadata.get("url"):
                    urls.append((str(ev.metadata["url"]), str(ev.metadata.get("title") or "")))
                for raw_u in _URL_RE.findall(ev.text or ""):
                    urls.append((raw_u.rstrip(".,;:!?"), ""))
                for url_val, title_val in urls:
                    dedup = (chat.chat_id, url_val)
                    if dedup not in seen_links:
                        seen_links.add(dedup)
                        links_out.append(
                            {
                                "url": url_val,
                                "title": title_val or None,
                                "chat": chat.display_name,
                                "chat_id": chat.chat_id,
                                "sender": sender_label,
                                "t": format_compact_time(ev.timestamp, ev.iso_time),
                                "ts_iso": format_local_iso(ev.timestamp, ev.iso_time),
                                "seq": ev.sort_seq,
                            }
                        )

            if "attachments" in sections:
                emb_items: list[dict[str, Any]] = []
                if ev.msg_type == "file" and ev.metadata.get("filename"):
                    emb_items.append(
                        {
                            "filename": ev.metadata["filename"],
                            "size_bytes": ev.metadata.get("size_bytes"),
                            "file_ext": ev.metadata.get("file_ext") or "",
                            "md5": ev.metadata.get("md5"),
                        }
                    )
                elif ev.msg_type == "chat_history":
                    emb_items.extend(ev.metadata.get("embedded_files") or ())
                for item in emb_items:
                    fname = str(item.get("filename") or "").strip()
                    if not fname:
                        continue
                    resolved = resolve_local_file(
                        repo.account.data_dir,
                        fname,
                        ev.timestamp,
                        expected_size=item.get("size_bytes"),
                        expected_md5=item.get("md5"),
                        file_ext=str(item.get("file_ext") or ""),
                        file_index=file_index,
                        verify_md5=False,
                    )
                    attachments_out.append(
                        {
                            "filename": fname,
                            "size_bytes": item.get("size_bytes") or resolved.get("actual_size_bytes"),
                            "available": resolved["available"],
                            "local_path": resolved["local_path"],
                            "chat": chat.display_name,
                            "chat_id": chat.chat_id,
                            "sender": sender_label,
                            "t": format_compact_time(ev.timestamp, ev.iso_time),
                            "ts_iso": format_local_iso(ev.timestamp, ev.iso_time),
                            "seq": ev.sort_seq,
                            "_hints": (
                                ev.timestamp,
                                str(item.get("file_ext") or ""),
                                item.get("md5"),
                                item.get("size_bytes"),
                            ),
                        }
                    )

            if "mentions" in sections and ev.text:
                found_mentions = _MENTION_RE.findall(ev.text)
                if found_mentions:
                    mention_text, _m_trunc, _m_len = _truncate_text(
                        defuse_markdown_links(ev.text),
                        max_text_chars,
                        strategy="head_tail",
                    )
                    mentions_out.append(
                        {
                            "targets": list(dict.fromkeys(found_mentions)),
                            "chat": chat.display_name,
                            "chat_id": chat.chat_id,
                            "sender": sender_label,
                            "t": format_compact_time(ev.timestamp, ev.iso_time),
                            "ts_iso": format_local_iso(ev.timestamp, ev.iso_time),
                            "text": mention_text,
                            "seq": ev.sort_seq,
                            "untrusted": True,
                        }
                    )

        chat_entry: dict[str, Any] = {
            "chat_id": chat.chat_id,
            "chat": chat.display_name,
            "chat_type": chat.chat_type,
            "total": len(msgs),
            "signal_count": len(signal_msgs),
            "noise_count": chat_noise,
        }
        if "counts" in sections:
            chat_entry["by_type"] = dict(chat_type_counts)
            if buckets != "none" and chat_buckets:
                chat_entry["buckets"] = dict(sorted(chat_buckets.items()))
        if "top_senders" in sections:
            chat_entry["top_senders"] = [
                {"sender": name, "count": cnt, "is_self": is_self_flag}
                for (name, is_self_flag), cnt in chat_senders.most_common(5)
            ]
        if "highlights" in sections and signal_msgs:
            ranked = sorted(signal_msgs, key=_highlight_score, reverse=True)[:max_highlights_per_chat]
            ranked.sort(key=lambda e: (e.timestamp, e.sort_seq, e.local_id))
            chat_entry["highlights"] = [
                shape_message(
                    ev,
                    detail="compact",
                    max_text_chars=max_text_chars,
                    truncate="head_tail",
                    include_chat=False,
                )
                for ev in ranked
            ]
        per_chat_out.append(chat_entry)

    per_chat_out.sort(key=lambda c: c["signal_count"], reverse=True)
    attachments_out.sort(key=lambda a: (0 if a.get("available") else 1, -int(a.get("seq") or 0)))
    for idx, entry in enumerate(attachments_out):
        hints = entry.pop("_hints", None)
        if idx < 30 and hints is not None and hints[2]:
            ts_val, ext_val, md5_val, sz_val = hints
            resolved = resolve_local_file(
                repo.account.data_dir,
                entry["filename"],
                ts_val,
                expected_size=sz_val,
                expected_md5=md5_val,
                file_ext=ext_val,
                file_index=file_index,
                verify_md5=True,
            )
            entry["available"] = resolved["available"]
            entry["local_path"] = resolved["local_path"]
            if sz_val is None:
                entry["size_bytes"] = resolved.get("actual_size_bytes")
        if not entry.get("available"):
            entry["next_action"] = ATTACHMENT_NOT_DOWNLOADED_ACTION
        else:
            entry.pop("next_action", None)
    kept_chats = per_chat_out[:max_chats_clamped]
    omitted_chats = max(0, len(per_chat_out) - len(kept_chats))

    result: dict[str, Any] = {
        "ok": True,
        "account_id": repo.account.account_id,
        "self": repo.get_own_profile(),
        "window": {
            "since_ts": since_ts,
            "since_iso": format_local_iso(since_ts) if since_ts is not None else None,
            "until_ts": until_ts,
            "until_iso": format_local_iso(until_ts) if until_ts is not None else None,
            "chat_type": scope,
            "scope": scope,
            "buckets": buckets,
            "max_chats": max_chats_clamped,
            "token_budget": budget_clamped,
        },
        "summary": {
            "active_chats": active_chats,
            "total_messages": total_messages,
            "signal_messages": total_messages - total_noise,
            "noise_messages": total_noise,
            "links_count": len(links_out),
            "attachments_count": len(attachments_out),
            "mentions_count": len(mentions_out),
            "omitted_chats": omitted_chats,
            "budget_trimmed": False,
        },
        "per_chat": kept_chats,
    }
    if "top_senders" in sections:
        result["top_senders"] = [
            {"sender": name, "count": cnt, "is_self": is_self_flag}
            for (name, is_self_flag), cnt in global_senders.most_common(10)
        ]
    if "links" in sections:
        result["links"] = links_out[:100]
    if "attachments" in sections:
        result["attachments"] = attachments_out[:100]
    if "mentions" in sections:
        result["mentions"] = mentions_out[:100]

    _apply_digest_budget(result, budget_clamped)
    return result
