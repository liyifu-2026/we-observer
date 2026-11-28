"""Read-only plaintext local file attachment locator for WeChat 4.x (<account>/msg/file)."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from wx_observer.shaping import estimate_tokens, format_compact_time, format_local_iso

ATTACHMENT_NOT_DOWNLOADED_ACTION = (
    "Attachment is not downloaded locally; open or download it in WeChat desktop first."
)

_INVALID_WIN_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_YYYY_MM_RE = re.compile(r"^\d{4}-\d{2}$")
_MAX_HASH_BYTES = 50 * 1024 * 1024
_MD5_CHUNK_BYTES = 1_048_576
_MD5_CACHE_MAX = 2048
_MD5_CACHE_LOCK = threading.Lock()
_MD5_CACHE: dict[tuple[str, int, int], str] = {}


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
        st = path.stat()
        if st.st_size > _MAX_HASH_BYTES:
            return None
        cache_key = (str(path), st.st_mtime_ns, st.st_size)
        with _MD5_CACHE_LOCK:
            cached = _MD5_CACHE.get(cache_key)
        if cached is not None:
            return cached
        digest = hashlib.md5()
        with path.open("rb") as f:
            while True:
                chunk = f.read(_MD5_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        hex_digest = digest.hexdigest().lower()
        with _MD5_CACHE_LOCK:
            if len(_MD5_CACHE) >= _MD5_CACHE_MAX:
                _MD5_CACHE.pop(next(iter(_MD5_CACHE)))
            _MD5_CACHE[cache_key] = hex_digest
        return hex_digest
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
                    elif entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                        size = entry.stat(follow_symlinks=False).st_size
                        by_name_ci.setdefault(entry.name.casefold(), []).append(
                            ("", resolved_root / entry.name, size)
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
                        if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                        by_name_ci.setdefault(entry.name.casefold(), []).append(
                            (ym, mdir / entry.name, size)
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


def _resolve_within_root(target: Path, resolved_root: Path | None) -> Path | None:
    if resolved_root is None:
        return target
    try:
        resolved = target.resolve()
        if resolved.is_relative_to(resolved_root):
            return resolved
    except OSError:
        pass
    return None


def resolve_local_file(
    account_data_dir: str | Path,
    filename: str,
    timestamp: int = 0,
    *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    file_ext: str = "",
    file_index: LocalFileIndex | None = None,
    verify_md5: bool = True,
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
        check_root: Path | None = file_index.resolved_root
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
        check_root = None
        hit_iter = _iter_direct_hits(file_root, candidates, timestamp)

    norm_md5 = expected_md5.strip().lower() if (verify_md5 and expected_md5) else None
    matches: list[tuple[int, Path, int, str]] = []

    for target, actual_size, is_suffix in hit_iter:
        if norm_md5 and (expected_size is None or actual_size == expected_size):
            safe_target = _resolve_within_root(target, check_root)
            if safe_target is not None and _file_md5(safe_target) == norm_md5:
                return {
                    "available": True,
                    "local_path": str(safe_target),
                    "actual_size_bytes": actual_size,
                    "match_confidence": "md5",
                }

        size_match = expected_size is not None and actual_size == expected_size
        if not is_suffix and size_match:
            safe_target = _resolve_within_root(target, check_root)
            if safe_target is not None:
                return {
                    "available": True,
                    "local_path": str(safe_target),
                    "actual_size_bytes": actual_size,
                    "match_confidence": "name_and_size",
                }
        if not is_suffix:
            matches.append((2, target, actual_size, "name"))
        elif size_match:
            matches.append((1, target, actual_size, "suffix_and_size"))
        else:
            matches.append((3, target, actual_size, "suffix"))

    if matches:
        matches.sort(key=lambda item: item[0])
        for _rank, target, actual_size, confidence in matches:
            safe_target = _resolve_within_root(target, check_root)
            if safe_target is not None:
                return {
                    "available": True,
                    "local_path": str(safe_target),
                    "actual_size_bytes": actual_size,
                    "match_confidence": confidence,
                }

    return {
        "available": False,
        "local_path": None,
        "actual_size_bytes": None,
        "match_confidence": None,
        "reason": "not_downloaded_or_moved",
    }


def enrich_embedded_files(
    account_data_dir: str | Path,
    embedded_files: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    timestamp: int = 0,
    *,
    file_index: LocalFileIndex | None = None,
    verify_md5: bool = True,
) -> list[dict[str, Any]]:
    """Resolve local paths and availability flags for forwarded chat_history embedded files."""
    if file_index is None:
        file_index = build_file_index(account_data_dir)
    out: list[dict[str, Any]] = []
    for raw_item in embedded_files:
        item = dict(raw_item)
        fname = str(item.get("filename") or "").strip()
        if not fname:
            continue
        ext = str(item.get("file_ext") or Path(fname).suffix.lstrip(".")).lower().lstrip(".")
        resolved = resolve_local_file(
            account_data_dir,
            fname,
            timestamp,
            expected_size=item.get("size_bytes"),
            expected_md5=item.get("md5"),
            file_ext=ext,
            file_index=file_index,
            verify_md5=verify_md5,
        )
        item["available"] = bool(resolved["available"])
        item["local_path"] = resolved["local_path"] or None
        if resolved.get("match_confidence"):
            item["match_confidence"] = resolved["match_confidence"]
        if item.get("size_bytes") is None and resolved.get("actual_size_bytes") is not None:
            item["size_bytes"] = resolved["actual_size_bytes"]
        if not resolved["available"]:
            item["next_action"] = ATTACHMENT_NOT_DOWNLOADED_ACTION
        else:
            item.pop("next_action", None)
        out.append(item)
    return out


def collect_attachments(
    repo: Any,
    *,
    scope: str = "all",
    chats: list[str] | None = None,
    exclude_chats: list[str] | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
    extensions: list[str] | None = None,
    only_available: bool = False,
    include_embedded: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Scan file attachments across chats and resolve readable local paths."""
    limit = max(1, min(int(limit), 500))
    norm_exts = {
        ext.strip().lower().lstrip(".")
        for ext in (extensions or [])
        if ext and ext.strip()
    }

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

    file_index = build_file_index(repo.account.data_dir)
    eligible_chats = [
        chat
        for chat in target_chats
        if explicit_chats
        or since_ts is None
        or (chat.last_timestamp > 0 and chat.last_timestamp >= since_ts - 60)
    ]
    use_waves = (not explicit_chats) and (since_ts is not None)
    if use_waves:
        eligible_chats.sort(key=lambda c: c.last_timestamp, reverse=True)
        wave_size = max(32, min(limit * 2, 80))
    else:
        wave_size = max(1, len(eligible_chats))

    query_msg_types = ["file", "chat_history"] if include_embedded else ["file"]
    found: list[dict[str, Any]] = []
    offset = 0
    while offset < len(eligible_chats):
        chunk = eligible_chats[offset : offset + wave_size]
        offset += len(chunk)
        batch_by_chat = repo.get_messages_batch(
            chunk,
            limit_per_chat=500,
            since_ts=since_ts,
            until_ts=until_ts,
            msg_types=query_msg_types,
            include_self=True,
        )
        for chat in chunk:
            for ev in batch_by_chat.get(chat.chat_id, ()):
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
                elif include_embedded and ev.msg_type == "chat_history":
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
                    if only_available:
                        resolved = resolve_local_file(
                            repo.account.data_dir,
                            item["filename"],
                            ev.timestamp,
                            expected_size=item.get("size_bytes"),
                            expected_md5=item.get("md5"),
                            file_ext=ext,
                            file_index=file_index,
                            verify_md5=False,
                        )
                        if not resolved["available"]:
                            continue
                        avail = True
                        local_path = resolved["local_path"] or None
                        confidence = resolved.get("match_confidence")
                        size_bytes = item.get("size_bytes") or resolved.get("actual_size_bytes")
                    else:
                        avail = False
                        local_path = None
                        confidence = None
                        size_bytes = item.get("size_bytes")
                    found.append(
                        {
                            "filename": item["filename"],
                            "file_ext": ext,
                            "size_bytes": size_bytes,
                            "md5": item.get("md5"),
                            "source_type": item["source_type"],
                            "available": avail,
                            "local_path": local_path,
                            "match_confidence": confidence,
                            "t": format_compact_time(ev.timestamp, ev.iso_time),
                            "ts_iso": format_local_iso(ev.timestamp, ev.iso_time),
                            "timestamp": ev.timestamp,
                            "chat": chat.display_name,
                            "chat_id": chat.chat_id,
                            "sender": ev.sender_name or ev.sender_id or ("我" if ev.is_self else ""),
                            "seq": ev.sort_seq,
                        }
                    )

        if use_waves and offset < len(eligible_chats) and len(found) >= limit:
            found.sort(key=lambda r: (r["timestamp"], r["seq"], r["chat_id"]), reverse=True)
            if found[limit - 1]["timestamp"] > eligible_chats[offset].last_timestamp + 60:
                break

    found.sort(key=lambda r: (r["timestamp"], r["seq"], r["chat_id"]), reverse=True)
    trimmed = found[:limit]
    for entry in trimmed:
        if only_available and not entry.get("md5"):
            entry.pop("next_action", None)
            continue
        resolved = resolve_local_file(
            repo.account.data_dir,
            entry["filename"],
            entry["timestamp"],
            expected_size=entry.get("size_bytes"),
            expected_md5=entry.get("md5"),
            file_ext=entry.get("file_ext") or "",
            file_index=file_index,
            verify_md5=True,
        )
        entry["available"] = bool(resolved["available"])
        entry["local_path"] = resolved["local_path"] or None
        entry["match_confidence"] = resolved.get("match_confidence")
        if entry["size_bytes"] is None:
            entry["size_bytes"] = resolved.get("actual_size_bytes")
        if not entry["available"]:
            entry["next_action"] = ATTACHMENT_NOT_DOWNLOADED_ACTION
        else:
            entry.pop("next_action", None)
    if only_available:
        trimmed = [item for item in trimmed if item["available"]]
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
