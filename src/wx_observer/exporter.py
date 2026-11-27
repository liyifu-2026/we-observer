"""Message formatting and export helpers (JSON, JSONL, CSV, Markdown)."""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from wx_observer.models import MessageEvent


def _event_dicts(events: Iterable[MessageEvent | dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in events:
        out.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    return out


def format_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def format_jsonl(events: Iterable[MessageEvent | dict[str, Any]]) -> str:
    lines = [json.dumps(ev, ensure_ascii=False) for ev in _event_dicts(events)]
    return "\n".join(lines) + ("\n" if lines else "")


def format_csv(events: Iterable[MessageEvent | dict[str, Any]]) -> str:
    buf = io.StringIO()
    fieldnames = [
        "iso_time",
        "timestamp",
        "account_id",
        "chat_id",
        "chat_name",
        "chat_type",
        "sender_id",
        "sender_name",
        "is_self",
        "msg_type",
        "text",
        "quote_excerpt",
        "sort_seq",
        "local_id",
        "shard",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for ev in _event_dicts(events):
        quote = ev.get("quote") or {}
        writer.writerow(
            {
                "iso_time": ev.get("iso_time", ""),
                "timestamp": ev.get("timestamp", 0),
                "account_id": ev.get("account_id", ""),
                "chat_id": ev.get("chat_id", ""),
                "chat_name": ev.get("chat_name", ""),
                "chat_type": ev.get("chat_type", ""),
                "sender_id": ev.get("sender_id", ""),
                "sender_name": ev.get("sender_name", ""),
                "is_self": ev.get("is_self", False),
                "msg_type": ev.get("msg_type", ""),
                "text": ev.get("text", ""),
                "quote_excerpt": quote.get("excerpt", "") if isinstance(quote, dict) else "",
                "sort_seq": ev.get("sort_seq", 0),
                "local_id": ev.get("local_id", 0),
                "shard": ev.get("shard", ""),
            }
        )
    return buf.getvalue()


def _format_local_time(ts: int, iso_fallback: str) -> str:
    if ts > 0:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            pass
    return iso_fallback or str(ts)


def format_markdown(
    events: Iterable[MessageEvent | dict[str, Any]],
    *,
    title: str | None = None,
) -> str:
    items = _event_dicts(events)
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chat_titles: dict[str, str] = {}
    for ev in items:
        cid = str(ev.get("chat_id") or "")
        grouped[cid].append(ev)
        chat_titles[cid] = str(ev.get("chat_name") or cid)

    for cid, group_events in grouped.items():
        lines.append(f"## {chat_titles[cid]} (`{cid}`)")
        lines.append("")
        for ev in group_events:
            when = _format_local_time(int(ev.get("timestamp") or 0), str(ev.get("iso_time") or ""))
            sender = ev.get("sender_name") or ev.get("sender_id") or ("Me" if ev.get("is_self") else "Unknown")
            mtype = ev.get("msg_type") or "text"
            lines.append(f"- **{when}** `{sender}` ({mtype}):")
            quote = ev.get("quote")
            if isinstance(quote, dict) and quote.get("excerpt"):
                q_sender = quote.get("sender_name") or quote.get("sender_id") or ""
                prefix = f"{q_sender}: " if q_sender else ""
                lines.append(f"  > {prefix}{quote['excerpt']}")
            body = str(ev.get("text") or "").replace("\r\n", "\n")
            if body:
                for subline in body.split("\n"):
                    lines.append(f"  {subline}")
        lines.append("")

    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def export_messages(
    events: Iterable[MessageEvent | dict[str, Any]],
    fmt: str,
    *,
    output_path: str | Path | None = None,
    title: str | None = None,
) -> str:
    norm = (fmt or "json").strip().lower()
    if norm == "json":
        rendered = format_json(_event_dicts(events))
    elif norm == "jsonl":
        rendered = format_jsonl(events)
    elif norm == "csv":
        rendered = format_csv(events)
    elif norm in ("md", "markdown"):
        rendered = format_markdown(events, title=title)
    else:
        raise ValueError(f"Unsupported export format: {fmt}")

    if output_path is not None:
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
    return rendered
