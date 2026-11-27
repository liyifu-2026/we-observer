"""Read-only plaintext local file attachment locator for WeChat 4.x (<account>/msg/file)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from wx_observer.shaping import estimate_tokens, format_compact_time

_INVALID_WIN_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_YYYY_MM_RE = re.compile(r"^\d{4}-\d{2}$")
_MAX_HASH_BYTES = 50 * 1024 * 1024


def _month_candidates(timestamp: int) -> list[str]:
    if timestamp <= 0:
        return []
    try:
        dt = datetime.fromtimestamp(timestamp)
    except (OSError, OverflowError, ValueError):
        return []
    year, month = dt.year, dt.month
    cur = f"{year:04d}-{month:02d}"
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return [cur, f"{py:04d}-{pm:02d}", f"{ny:04d}-{nm:02d}"]


def _file_md5(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_HASH_BYTES:
            return None
        digest = hashlib.md5()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()
    except OSError:
        return None


def _is_unsafe_filename(raw_name: str) -> bool:
    if not raw_name or not raw_name.strip():
        return True
    if "/" in raw_name or "\\" in raw_name or "\x00" in raw_name:
        return True
    if raw_name.strip() in (".", "..") or ".." in Path(raw_name).parts:
        return True
    return False


def _candidate_names(filename: str, file_ext: str = "") -> list[tuple[str, bool]]:
    """Return ordered (name, is_duplicate_suffix) candidates for a WeChat file."""
    clean = _INVALID_WIN_CHARS_RE.sub("_", filename).strip()
    if not clean:
        return []
    bases = [clean]
    ext_clean = _INVALID_WIN_CHARS_RE.sub("", (file_ext or "").lstrip(".")).strip()
    if ext_clean and not clean.lower().endswith(f".{ext_clean.lower()}"):
        bases.append(f"{clean}.{ext_clean}")

    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for b in bases:
        if b not in seen:
            seen.add(b)
            out.append((b, False))
        stem, dot, ext = b.rpartition(".")
        if not dot:
            stem, ext_part = b, ""
        else:
            ext_part = f".{ext}"
        for idx in range(1, 21):
            variant = f"{stem}({idx}){ext_part}"
            if variant not in seen:
                seen.add(variant)
                out.append((variant, True))
    return out


@dataclass(slots=True)
class LocalFileIndex:
    """In-memory case-insensitive filename index under `<account_data_dir>/msg/file/`."""

    resolved_root: Path
    root_exists: bool
    month_dirs: tuple[str, ...]
    by_name_ci: dict[str, list[tuple[str, Path, int]]]


def build_file_index(account_data_dir: str | Path) -> LocalFileIndex:
    """Index all files inside `<account_data_dir>/msg/file/` in a single scandir pass."""
    file_root = Path(account_data_dir) / "msg" / "file"
    if not file_root.is_dir():
        return LocalFileIndex(
            resolved_root=file_root,
            root_exists=False,
            month_dirs=(),
            by_name_ci={},
        )

    resolved_root = file_root.resolve()
    by_name_ci: dict[str, list[tuple[str, Path, int]]] = {}
    months: list[tuple[str, Path]] = []

    try:
        with os.scandir(resolved_root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and _YYYY_MM_RE.match(entry.name):
                        rdir = Path(entry.path).resolve()
                        if rdir.is_relative_to(resolved_root):
                            months.append((entry.name, rdir))
                    elif entry.is_file(follow_symlinks=False):
                        rpath = Path(entry.path).resolve()
                        if rpath.is_relative_to(resolved_root):
                            size = entry.stat(follow_symlinks=False).st_size
                            by_name_ci.setdefault(entry.name.casefold(), []).append(
                                ("", rpath, size)
                            )
                except OSError:
                    continue
    except OSError:
        return LocalFileIndex(
            resolved_root=resolved_root,
            root_exists=False,
            month_dirs=(),
            by_name_ci={},
        )

    months.sort(key=lambda item: item[0], reverse=True)
    for ym, mdir in months:
        try:
            with os.scandir(mdir) as it:
                for entry in it:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        rpath = Path(entry.path).resolve()
                        if rpath.is_relative_to(resolved_root):
                            size = entry.stat(follow_symlinks=False).st_size
                            by_name_ci.setdefault(entry.name.casefold(), []).append(
                                (ym, rpath, size)
                            )
                    except OSError:
                        continue
        except OSError:
            continue

    return LocalFileIndex(
        resolved_root=resolved_root,
        root_exists=True,
        month_dirs=tuple(ym for ym, _ in months),
        by_name_ci=by_name_ci,
    )


def _iter_indexed_hits(
    file_index: LocalFileIndex,
    candidates: list[tuple[str, bool]],
    timestamp: int,
) -> Iterator[tuple[Path, int, bool]]:
    dir_order: dict[str, int] = {}
    for ym in _month_candidates(timestamp):
        if ym not in dir_order:
            dir_order[ym] = len(dir_order)
    for ym in file_index.month_dirs:
        if ym not in dir_order:
            dir_order[ym] = len(dir_order)
    dir_order[""] = len(dir_order)

    ranked_hits: list[tuple[int, int, Path, int, bool]] = []
    for c_idx, (cand_name, is_suffix) in enumerate(candidates):
        entries = file_index.by_name_ci.get(cand_name.casefold())
        if not entries:
            continue
        for folder_key, target, actual_size in entries:
            d_ord = dir_order.get(folder_key, 999_999)
            ranked_hits.append((d_ord, c_idx, target, actual_size, is_suffix))

    ranked_hits.sort(key=lambda item: (item[0], item[1]))
    seen_paths: set[Path] = set()
    for _d_ord, _c_idx, target, actual_size, is_suffix in ranked_hits:
        if target in seen_paths:
            continue
        seen_paths.add(target)
        yield target, actual_size, is_suffix


def _iter_direct_hits(
    file_root: Path,
    candidates: list[tuple[str, bool]],
    timestamp: int,
) -> Iterator[tuple[Path, int, bool]]:
    resolved_root = file_root.resolve()
    ordered_dirs: list[Path] = []
    seen_dirs: set[Path] = set()

    for ym in _month_candidates(timestamp):
        d = file_root / ym
        if d.is_dir():
            rd = d.resolve()
            if rd.is_relative_to(resolved_root) and rd not in seen_dirs:
                seen_dirs.add(rd)
                ordered_dirs.append(rd)

    try:
        month_dirs = sorted(
            (p for p in file_root.iterdir() if p.is_dir() and _YYYY_MM_RE.match(p.name)),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        month_dirs = []
    for d in month_dirs:
        rd = d.resolve()
        if rd.is_relative_to(resolved_root) and rd not in seen_dirs:
            seen_dirs.add(rd)
            ordered_dirs.append(rd)

    if resolved_root not in seen_dirs:
        ordered_dirs.append(resolved_root)

    for folder in ordered_dirs:
        for cand_name, is_suffix in candidates:
            target = (folder / cand_name).resolve()
            if not target.is_relative_to(resolved_root):
                continue
            if not target.is_file():
                continue
            try:
                actual_size = target.stat().st_size
            except OSError:
                continue
            yield target, actual_size, is_suffix


def resolve_local_file(
    account_data_dir: str | Path,
    filename: str,
    timestamp: int = 0,
    *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    file_ext: str = "",
    file_index: LocalFileIndex | None = None,
) -> dict[str, Any]:
    """Locate a downloaded plaintext attachment under <account_data_dir>/msg/file/."""
    if _is_unsafe_filename(filename):
        return {
            "available": False,
            "local_path": None,
            "actual_size_bytes": None,
            "match_confidence": None,
            "reason": "invalid_filename",
        }

    candidates = _candidate_names(filename, file_ext=file_ext)
    if file_index is not None:
        if not file_index.root_exists:
            return {
                "available": False,
                "local_path": None,
                "actual_size_bytes": None,
                "match_confidence": None,
                "reason": "file_root_missing",
            }
        hit_iter = _iter_indexed_hits(file_index, candidates, timestamp)
    else:
        file_root = Path(account_data_dir) / "msg" / "file"
        if not file_root.is_dir():
            return {
                "available": False,
                "local_path": None,
                "actual_size_bytes": None,
                "match_confidence": None,
                "reason": "file_root_missing",
            }
        hit_iter = _iter_direct_hits(file_root, candidates, timestamp)

    norm_md5 = expected_md5.strip().lower() if expected_md5 else None
    matches: list[tuple[int, dict[str, Any]]] = []

    for target, actual_size, is_suffix in hit_iter:
        if norm_md5 and (expected_size is None or actual_size == expected_size):
            if _file_md5(target) == norm_md5:
                return {
                    "available": True,
                    "local_path": str(target),
                    "actual_size_bytes": actual_size,
                    "match_confidence": "md5",
                }

        size_match = expected_size is not None and actual_size == expected_size
        if not is_suffix and size_match:
            return {
                "available": True,
                "local_path": str(target),
                "actual_size_bytes": actual_size,
                "match_confidence": "name_and_size",
            }
        if not is_suffix:
            matches.append(
                (
                    2,
                    {
                        "available": True,
                        "local_path": str(target),
                        "actual_size_bytes": actual_size,
                        "match_confidence": "name",
                    },
                )
            )
        elif size_match:
            matches.append(
                (
                    1,
                    {
                        "available": True,
                        "local_path": str(target),
                        "actual_size_bytes": actual_size,
                        "match_confidence": "suffix_and_size",
                    },
                )
            )
        else:
            matches.append(
                (
                    3,
                    {
                        "available": True,
                        "local_path": str(target),
                        "actual_size_bytes": actual_size,
                        "match_confidence": "suffix",
                    },
                )
            )

    if matches:
        matches.sort(key=lambda item: item[0])
        return matches[0][1]

    return {
        "available": False,
        "local_path": None,
        "actual_size_bytes": None,
        "match_confidence": None,
        "reason": "not_downloaded_or_moved",
    }


def collect_attachments(
    repo: Any,
    *,
    scope: str = "all",
    chats: list[str] | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
    extensions: list[str] | None = None,
    only_available: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Scan file attachments across chats and resolve readable local paths."""
    limit = max(1, min(int(limit), 500))
    norm_exts = {
        ext.strip().lower().lstrip(".")
        for ext in (extensions or [])
        if ext and ext.strip()
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

    file_index = build_file_index(repo.account.data_dir)
    found: list[dict[str, Any]] = []
    for chat in target_chats:
        if (
            not explicit_chats
            and since_ts is not None
            and (chat.last_timestamp <= 0 or chat.last_timestamp < since_ts - 60)
        ):
            continue
        res = repo.get_messages(
            chat.chat_id,
            chat_name=chat.display_name,
            limit=500,
            since_ts=since_ts,
            until_ts=until_ts,
            msg_types=["file", "chat_history"],
            include_self=True,
        )
        for ev in res["messages"]:
            file_items: list[dict[str, Any]] = []
            if ev.msg_type == "file":
                fname = str(ev.metadata.get("filename") or "").strip()
                if fname:
                    file_items.append(
                        {
                            "filename": fname,
                            "size_bytes": ev.metadata.get("size_bytes"),
                            "file_ext": ev.metadata.get("file_ext") or Path(fname).suffix.lstrip(".").lower(),
                            "md5": ev.metadata.get("md5"),
                            "source_type": "file",
                        }
                    )
            elif ev.msg_type == "chat_history":
                for emb in ev.metadata.get("embedded_files") or ():
                    fname = str(emb.get("filename") or "").strip()
                    if fname:
                        file_items.append(
                            {
                                "filename": fname,
                                "size_bytes": emb.get("size_bytes"),
                                "file_ext": emb.get("file_ext") or Path(fname).suffix.lstrip(".").lower(),
                                "md5": emb.get("md5"),
                                "source_type": "forwarded_record",
                            }
                        )

            for item in file_items:
                ext = str(item.get("file_ext") or "").lower().lstrip(".")
                if norm_exts and ext not in norm_exts:
                    continue
                resolved = resolve_local_file(
                    repo.account.data_dir,
                    item["filename"],
                    ev.timestamp,
                    expected_size=item.get("size_bytes"),
                    expected_md5=item.get("md5"),
                    file_ext=ext,
                    file_index=file_index,
                )
                if only_available and not resolved["available"]:
                    continue
                found.append(
                    {
                        "filename": item["filename"],
                        "file_ext": ext,
                        "size_bytes": item.get("size_bytes") or resolved.get("actual_size_bytes"),
                        "md5": item.get("md5"),
                        "source_type": item["source_type"],
                        "available": resolved["available"],
                        "local_path": resolved["local_path"],
                        "match_confidence": resolved.get("match_confidence"),
                        "t": format_compact_time(ev.timestamp, ev.iso_time),
                        "timestamp": ev.timestamp,
                        "chat": chat.display_name,
                        "chat_id": chat.chat_id,
                        "sender": ev.sender_name or ev.sender_id or ("我" if ev.is_self else ""),
                        "seq": ev.sort_seq,
                    }
                )

    found.sort(key=lambda r: (r["timestamp"], r["seq"], r["chat_id"]), reverse=True)
    trimmed = found[:limit]
    available_count = sum(1 for item in trimmed if item["available"])
    payload: dict[str, Any] = {
        "ok": True,
        "account_id": repo.account.account_id,
        "count": len(trimmed),
        "available_count": available_count,
        "attachments": trimmed,
    }
    payload["est_tokens"] = estimate_tokens(payload)
    return payload
