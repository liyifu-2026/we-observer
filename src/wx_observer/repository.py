"""High-level read-only WeChat 4.x account database repository."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wx_observer.cipher_store import EncryptedDatabasePool, PAGE_SIZE, quick_check_sqlite
from wx_observer.discovery import derive_wxid_from_account_dir
from wx_observer.exceptions import (
    AmbiguousChatError,
    ChatNotFoundError,
    KeyUnavailableError,
    WxObserverError,
)
from wx_observer.key_store import KeyStore, load_external_keys
from wx_observer.memory_scanner import extract_keys_from_pids
from wx_observer.models import AccountInfo, ChatInfo, ChatType, MessageEvent
from wx_observer.parser import decode_payload, parse_message_row, strip_group_sender_prefix

_MESSAGE_SHARD_RE = re.compile(r"^(?:message/)?message_(\d+)\.db$", re.I)
_MSG_TYPE_TO_BASE_TYPES: dict[str, tuple[int, ...]] = {
    "text": (1, 49),
    "image": (3,),
    "voice": (34,),
    "video": (43,),
    "emoji": (47, 11000),
    "location": (48,),
    "file": (49,),
    "link": (49,),
    "miniprogram": (49,),
    "chat_history": (49,),
    "transfer": (49,),
    "red_packet": (49,),
    "card": (49,),
    "quote": (49,),
    "voip": (50,),
    "system": (10000,),
}
_APPMSG_TYPE_TO_SUBTYPES: dict[str, tuple[int, ...]] = {
    "file": (6,),
    "chat_history": (19,),
    "link": (5,),
    "quote": (57,),
    "miniprogram": (33, 36),
    "transfer": (2000,),
    "red_packet": (2001,),
}


def _is_malformed_error(exc: sqlite3.DatabaseError) -> bool:
    return "malformed" in str(exc).lower()


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _format_iso_utc(ts: int) -> str:
    if ts <= 0:
        return ""
    try:
        return (
            datetime.fromtimestamp(ts, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError):
        return ""


def _collect_account_db_files(db_storage_dir: str | Path) -> dict[str, Path]:
    base = Path(db_storage_dir)
    found: dict[str, Path] = {}
    if not base.is_dir():
        return found
    for root, dirs, files in os.walk(base):
        rel_root = os.path.relpath(root, base).replace("\\", "/")
        if rel_root == "migrate" or rel_root.startswith("migrate/"):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d.lower() != "migrate"]
        for fname in sorted(files):
            if not fname.endswith(".db"):
                continue
            if fname.endswith("-wal") or fname.endswith("-shm"):
                continue
            full = Path(root) / fname
            rel = os.path.relpath(full, base).replace("\\", "/")
            found[rel] = full
    return found


class AccountRepository:
    """Read-only repository over a single WeChat 4.x account's decrypted databases."""

    def __init__(
        self,
        account: AccountInfo,
        *,
        state_dir: str | Path | None = None,
        keys_file: str | Path | None = None,
        temp_root: str | Path | None = None,
        target_pids: list[int] | None = None,
        use_key_cache: bool = True,
    ) -> None:
        self.account = account
        self.key_store = KeyStore(state_dir=state_dir)
        self.use_key_cache = bool(use_key_cache)
        self.keys_file = Path(keys_file) if keys_file else (
            Path(os.environ["WX_OBSERVER_KEYS_FILE"])
            if os.environ.get("WX_OBSERVER_KEYS_FILE")
            else None
        )
        self.target_pids = list(target_pids) if target_pids is not None else list(account.pids)
        self.pool = EncryptedDatabasePool(temp_root=temp_root)

        self._db_files: dict[str, Path] = {}
        self._keys: dict[str, bytes] = {}
        self._key_sources: dict[str, str] = {}
        self._last_msg_dir_listing: tuple[str, ...] = ()
        self._contacts_mtime: tuple[int, int] = (-1, -1)
        self._contacts_cache: dict[str, dict[str, str]] = {}
        self._cache_lock = threading.RLock()
        self._table_cols_cache: dict[tuple[str, str], set[str]] = {}
        self._shard_tables_cache: dict[str, tuple[tuple[int, int, int, int], frozenset[str]]] = {}
        self._name2id_cache: dict[str, tuple[tuple[int, int, int, int], dict[int, str]]] = {}
        self._own_wxid_cache: str | None = None

        self._refresh_db_inventory(force=True)
        self._resolve_keys(allow_memory_scan=True)

    @property
    def warnings(self) -> list[str]:
        return self.pool.warnings

    @property
    def key_sources(self) -> dict[str, str]:
        return dict(self._key_sources)

    @property
    def total_db_count(self) -> int:
        return len(self._db_files)

    @property
    def keyed_db_count(self) -> int:
        return sum(1 for rel in self._db_files if rel in self._keys)

    @property
    def unkeyed_dbs(self) -> list[str]:
        return sorted(rel for rel in self._db_files if rel not in self._keys)

    def close(self) -> None:
        self.pool.cleanup()

    def _collect_db_files(self) -> dict[str, Path]:
        return _collect_account_db_files(self.account.db_storage_dir)

    def _refresh_db_inventory(self, force: bool = False) -> bool:
        msg_dir = Path(self.account.db_storage_dir) / "message"
        if msg_dir.is_dir():
            try:
                cur_listing = tuple(
                    sorted(
                        name
                        for name in os.listdir(msg_dir)
                        if name.endswith(".db") and not name.endswith(("-wal", "-shm"))
                    )
                )
            except OSError:
                cur_listing = ()
        else:
            cur_listing = ()

        if not force and cur_listing == self._last_msg_dir_listing:
            return False

        self._last_msg_dir_listing = cur_listing
        self._db_files = self._collect_db_files()
        self.pool.register_databases(self._db_files, self._keys)
        return True

    def _resolve_keys(self, allow_memory_scan: bool = True) -> None:
        if not self._db_files:
            raise KeyUnavailableError(
                "no_databases_found",
                f"No SQLite database files found under {self.account.db_storage_dir}.",
                {"account_id": self.account.account_id},
            )

        # 1. Explicit external keys file
        if self.keys_file is not None:
            ext_keys = load_external_keys(self.keys_file, self._db_files)
            for rel, key_bytes in ext_keys.items():
                self._keys[rel] = key_bytes
                self._key_sources[rel] = "file"

        # 2. DPAPI user-scoped key cache
        if self.use_key_cache:
            missing_subset = {rel: p for rel, p in self._db_files.items() if rel not in self._keys}
            if missing_subset:
                cached = self.key_store.load_cached_keys(self.account.account_id, missing_subset)
                for rel, key_bytes in cached.items():
                    self._keys[rel] = key_bytes
                    self._key_sources.setdefault(rel, "dpapi")

        # 3. Read-only process memory scan for remaining databases
        missing_subset = {rel: p for rel, p in self._db_files.items() if rel not in self._keys}
        if missing_subset and allow_memory_scan and self.target_pids:
            scanned = extract_keys_from_pids(self.target_pids, missing_subset)
            if scanned:
                for rel, key_bytes in scanned.items():
                    self._keys[rel] = key_bytes
                    self._key_sources[rel] = "scan"
                if self.use_key_cache:
                    self.key_store.save_cached_keys(self.account.account_id, self._keys)

        self.pool.register_databases(self._db_files, self._keys)

        if not self._keys:
            if not self.target_pids and self.keys_file is None:
                raise KeyUnavailableError(
                    "account_offline_no_keys",
                    f"Account '{self.account.account_id}' is offline and has no cached or external keys. "
                    "Log into this account in WeChat or provide --keys-file.",
                    {"account_id": self.account.account_id},
                )
            raise KeyUnavailableError(
                "keys_unavailable",
                f"Unable to obtain valid SQLCipher keys for account '{self.account.account_id}'. "
                "Verify WeChat is logged into this account or provide --keys-file.",
                {"account_id": self.account.account_id},
            )

    def _ensure_shards_up_to_date(self) -> None:
        if self._refresh_db_inventory(force=False):
            missing = {rel: p for rel, p in self._db_files.items() if rel not in self._keys}
            if missing:
                self._resolve_keys(allow_memory_scan=True)

    def _find_rel_by_basename(self, basename: str) -> str | None:
        for rel in sorted(self._db_files):
            if os.path.basename(rel).lower() == basename.lower():
                return rel
        return None

    def _message_shard_rels(self) -> list[str]:
        self._ensure_shards_up_to_date()
        shards: list[tuple[int, str]] = []
        for rel in self._db_files:
            match = _MESSAGE_SHARD_RE.match(rel)
            if match:
                shards.append((int(match.group(1)), rel))
        shards.sort(key=lambda item: (item[0], item[1]))
        return [rel for _, rel in shards]

    def _invalidate_caches(self) -> None:
        with self._cache_lock:
            self._shard_tables_cache.clear()
            self._name2id_cache.clear()
            self._table_cols_cache.clear()

    def _invalidate_all(self) -> None:
        self.pool.invalidate_all()
        self._invalidate_caches()

    def _single_db_fingerprint(self, rel: str) -> tuple[int, int, int, int]:
        path = self._db_files.get(rel)
        if path is None:
            return (0, 0, 0, 0)
        try:
            st = path.stat()
            db_m, db_s = st.st_mtime_ns, st.st_size
        except OSError:
            db_m, db_s = 0, 0
        wal = Path(str(path) + "-wal")
        try:
            wst = wal.stat()
            wal_m, wal_s = wst.st_mtime_ns, wst.st_size
        except OSError:
            wal_m, wal_s = 0, 0
        return (db_m, db_s, wal_m, wal_s)

    def get_shard_fingerprints(self) -> dict[str, tuple[int, int, int, int]]:
        """Return cheap (mtime_ns, size, wal_mtime_ns, wal_size) stats for shards + session DB."""
        self._ensure_shards_up_to_date()
        rels = list(self._message_shard_rels())
        session_rel = self._find_rel_by_basename("session.db")
        if session_rel:
            rels.append(session_rel)

        stats: dict[str, tuple[int, int, int, int]] = {}
        for rel in rels:
            if rel in self._db_files:
                stats[rel] = self._single_db_fingerprint(rel)
        return stats

    def _get_shard_tables(self, rel: str, *, force_rebuild: bool = False) -> frozenset[str]:
        fp = self._single_db_fingerprint(rel)
        with self._cache_lock:
            cached = self._shard_tables_cache.get(rel)
            if not force_rebuild and cached is not None and cached[0] == fp:
                return cached[1]
        conn = self.pool.open_readonly(rel, force_rebuild=force_rebuild)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            tables = frozenset(str(r[0]) for r in rows if r[0])
        finally:
            conn.close()
        with self._cache_lock:
            self._shard_tables_cache[rel] = (fp, tables)
        return tables

    def _open_with_recovery(self, rel: str) -> sqlite3.Connection:
        return self.pool.open_readonly(rel)

    def _table_columns(self, conn: sqlite3.Connection, shard: str, table: str) -> set[str]:
        key = (shard, table)
        with self._cache_lock:
            cached = self._table_cols_cache.get(key)
        if cached is not None:
            return cached
        cols = {
            str(row["name"])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        with self._cache_lock:
            self._table_cols_cache[key] = cols
        return cols

    def _load_contacts(self, force: bool = False) -> dict[str, dict[str, str]]:
        contact_rel = self._find_rel_by_basename("contact.db")
        if not contact_rel or not self.pool.has_key(contact_rel):
            return self._contacts_cache

        path = self._db_files[contact_rel]
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (-1, -1)

        if not force and stamp == self._contacts_mtime and self._contacts_cache:
            return self._contacts_cache

        for attempt in (0, 1):
            conn = self.pool.open_readonly(contact_rel, force_rebuild=(attempt == 1))
            try:
                cols = self._table_columns(conn, contact_rel, "contact")
                select_cols = ["username"]
                for col in ("nick_name", "remark", "alias"):
                    if col in cols:
                        select_cols.append(col)
                rows = conn.execute(
                    f"SELECT {', '.join(select_cols)} FROM contact"
                ).fetchall()
                cache: dict[str, dict[str, str]] = {}
                for row in rows:
                    uname = str(row["username"] or "")
                    if not uname:
                        continue
                    nick = str(row["nick_name"] or "") if "nick_name" in cols else ""
                    remark = str(row["remark"] or "") if "remark" in cols else ""
                    alias = str(row["alias"] or "") if "alias" in cols else ""
                    cache[uname] = {
                        "username": uname,
                        "nick_name": nick,
                        "remark": remark,
                        "alias": alias,
                        "display_name": remark or nick or uname,
                    }
                self._contacts_cache = cache
                self._contacts_mtime = stamp
                return cache
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and _is_malformed_error(exc):
                    self._invalidate_all()
                    continue
                return self._contacts_cache
            finally:
                conn.close()
        return self._contacts_cache

    def get_own_wxid(self) -> str:
        if self._own_wxid_cache is not None:
            return self._own_wxid_cache
        contacts = self._load_contacts()
        if self.account.account_id in contacts:
            self._own_wxid_cache = self.account.account_id
            return self._own_wxid_cache
        derived = derive_wxid_from_account_dir(self.account.account_id)
        self._own_wxid_cache = derived
        return derived

    def get_own_display_name(self) -> str:
        wxid = self.get_own_wxid()
        contacts = self._load_contacts()
        entry = contacts.get(wxid)
        if entry:
            return entry["display_name"]
        return wxid

    def get_own_profile(self) -> dict[str, str]:
        """Return {wxid, display_name, alias} for the local WeChat account."""
        wxid = self.get_own_wxid()
        contacts = self._load_contacts()
        entry = contacts.get(wxid) or contacts.get(self.account.account_id)
        if entry:
            return {
                "wxid": wxid,
                "display_name": entry.get("display_name") or wxid,
                "alias": entry.get("alias") or "",
            }
        return {
            "wxid": wxid,
            "display_name": wxid,
            "alias": "",
        }

    def get_display_name(self, username: str) -> str:
        if not username:
            return ""
        contacts = self._load_contacts()
        entry = contacts.get(username)
        if entry:
            return entry["display_name"]
        return username

    def list_chats(
        self,
        *,
        chat_type: str = "all",
        query: str | None = None,
        limit: int = 50,
        include_hidden: bool = False,
        include_non_session: bool | None = None,
    ) -> list[ChatInfo]:
        contacts = self._load_contacts()
        session_rel = self._find_rel_by_basename("session.db")
        chats: list[ChatInfo] = []
        seen_ids: set[str] = set()

        if session_rel and self.pool.has_key(session_rel):
            for attempt in (0, 1):
                conn = self.pool.open_readonly(session_rel, force_rebuild=(attempt == 1))
                try:
                    cols = self._table_columns(conn, session_rel, "SessionTable")
                    where = "" if (include_hidden or "is_hidden" not in cols) else "WHERE is_hidden = 0"
                    order = (
                        "ORDER BY sort_timestamp DESC, last_timestamp DESC"
                        if "sort_timestamp" in cols and "last_timestamp" in cols
                        else ("ORDER BY last_timestamp DESC" if "last_timestamp" in cols else "")
                    )
                    rows = conn.execute(f"SELECT * FROM SessionTable {where} {order}").fetchall()
                    for r in rows:
                        uname = str(r["username"] or "")
                        if not uname or uname in seen_ids:
                            continue
                        seen_ids.add(uname)
                        ctype: ChatType = "group" if uname.endswith("@chatroom") else "private"
                        cinfo = contacts.get(uname, {})
                        nick = cinfo.get("nick_name", "")
                        remark = cinfo.get("remark", "")
                        display = cinfo.get("display_name") or uname
                        unread = int(r["unread_count"] or 0) if "unread_count" in cols else 0
                        last_ts = int(r["last_timestamp"] or 0) if "last_timestamp" in cols else 0
                        summary_raw = r["summary"] if "summary" in cols else ""
                        summary = summary_raw if isinstance(summary_raw, str) else ""
                        last_sender = ""
                        if "last_sender_display_name" in cols and r["last_sender_display_name"]:
                            last_sender = str(r["last_sender_display_name"])
                        elif "last_msg_sender" in cols and r["last_msg_sender"]:
                            last_sender = self.get_display_name(str(r["last_msg_sender"]))
                        chats.append(
                            ChatInfo(
                                chat_id=uname,
                                chat_type=ctype,
                                display_name=display,
                                nick_name=nick,
                                remark=remark,
                                unread_count=unread,
                                last_timestamp=last_ts,
                                summary=summary,
                                last_sender=last_sender,
                            )
                        )
                    break
                except sqlite3.DatabaseError as exc:
                    if attempt == 0 and _is_malformed_error(exc):
                        self._invalidate_all()
                        continue
                    raise
                finally:
                    conn.close()

        # Also surface known contacts/groups that may have Msg_ tables even if not in active sessions
        append_contacts = include_hidden if include_non_session is None else include_non_session
        if append_contacts or not chats:
            for uname, cinfo in contacts.items():
                if uname in seen_ids:
                    continue
                ctype = "group" if uname.endswith("@chatroom") else "private"
                chats.append(
                    ChatInfo(
                        chat_id=uname,
                        chat_type=ctype,
                        display_name=cinfo.get("display_name") or uname,
                        nick_name=cinfo.get("nick_name", ""),
                        remark=cinfo.get("remark", ""),
                    )
                )

        if chat_type in ("private", "group"):
            chats = [c for c in chats if c.chat_type == chat_type]

        if query:
            q = query.strip().casefold()
            chats = [
                c
                for c in chats
                if q in c.chat_id.casefold()
                or q in c.display_name.casefold()
                or q in c.nick_name.casefold()
                or q in c.remark.casefold()
            ]

        return chats[: max(0, int(limit))]

    def resolve_chat(self, chat_ref: str) -> ChatInfo:
        ref = (chat_ref or "").strip()
        if not ref:
            raise ChatNotFoundError(chat_ref)

        all_chats = self.list_chats(chat_type="all", limit=100_000, include_hidden=True)
        # 1. Exact chat_id match
        for chat in all_chats:
            if chat.chat_id == ref:
                return chat

        # 2. Exact remark / nick_name / display_name match
        exact_name_matches = [
            c
            for c in all_chats
            if ref in {c.remark, c.nick_name, c.display_name} and ref != ""
        ]
        if len(exact_name_matches) == 1:
            return exact_name_matches[0]
        if len(exact_name_matches) > 1:
            raise AmbiguousChatError(
                ref,
                [
                    {"chat_id": c.chat_id, "display_name": c.display_name, "chat_type": c.chat_type}
                    for c in exact_name_matches[:10]
                ],
            )

        # 3. Case-insensitive substring match
        q = ref.casefold()
        fuzzy = [
            c
            for c in all_chats
            if q in c.chat_id.casefold()
            or q in c.remark.casefold()
            or q in c.nick_name.casefold()
            or q in c.display_name.casefold()
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise AmbiguousChatError(
                ref,
                [
                    {"chat_id": c.chat_id, "display_name": c.display_name, "chat_type": c.chat_type}
                    for c in fuzzy[:10]
                ],
            )
        raise ChatNotFoundError(ref)

    def _open_msg_shards_for_chat(
        self,
        chat_id: str,
    ) -> list[tuple[sqlite3.Connection, str, str]]:
        table = "Msg_" + _md5_hex(chat_id)
        shard_rels = [rel for rel in self._message_shard_rels() if self.pool.has_key(rel)]
        if not shard_rels:
            return []

        for attempt in (0, 1):
            force_rebuild = attempt == 1
            matched: list[tuple[sqlite3.Connection, str, str]] = []
            try:
                if len(shard_rels) >= 2:
                    self.pool.prefetch_snapshots(shard_rels, force_rebuild=force_rebuild)
                matching_rels = [
                    rel
                    for rel in shard_rels
                    if table in self._get_shard_tables(rel, force_rebuild=force_rebuild)
                ]
                if not matching_rels:
                    return []
                for rel in matching_rels:
                    conn = self.pool.open_readonly(rel, force_rebuild=force_rebuild)
                    matched.append((conn, rel, table))
                return matched
            except sqlite3.DatabaseError as exc:
                for conn, _, _ in matched:
                    conn.close()
                if attempt == 0 and _is_malformed_error(exc):
                    self._invalidate_all()
                    continue
                raise
            except Exception:
                for conn, _, _ in matched:
                    conn.close()
                raise
        return []

    def _load_shard_name2id(self, conn: sqlite3.Connection, rel: str | None = None) -> dict[int, str]:
        fp = self._single_db_fingerprint(rel) if rel else None
        if rel and fp is not None:
            with self._cache_lock:
                cached = self._name2id_cache.get(rel)
                if cached is not None and cached[0] == fp:
                    return cached[1]
        try:
            mapping = {
                int(rowid): str(uname or "")
                for rowid, uname in conn.execute("SELECT rowid, user_name FROM Name2Id")
            }
        except sqlite3.Error:
            return {}
        if rel and fp is not None:
            with self._cache_lock:
                self._name2id_cache[rel] = (fp, mapping)
        return mapping

    def _row_to_event(
        self,
        row: sqlite3.Row,
        *,
        shard: str,
        chat_id: str,
        chat_type: ChatType,
        chat_name: str,
        name_map: dict[int, str],
        own_wxid: str,
        own_display: str,
        contacts: dict[str, dict[str, str]],
    ) -> MessageEvent:
        keys = set(row.keys())
        local_id = int(row["local_id"])
        raw_type = int(row["local_type"] or 0)
        server_id = int(row["server_id"] or 0) if "server_id" in keys else 0
        sort_seq = int(row["sort_seq"] or 0)
        ts = int(row["create_time"] or 0)

        real_sender_id = row["real_sender_id"] if "real_sender_id" in keys else None
        sender_wxid = ""
        if real_sender_id is not None:
            try:
                sender_wxid = name_map.get(int(real_sender_id), "")
            except (TypeError, ValueError):
                sender_wxid = ""

        is_self = bool(sender_wxid and own_wxid and sender_wxid == own_wxid)
        if is_self:
            sender_name = own_display
        elif sender_wxid:
            sender_name = (contacts.get(sender_wxid) or {}).get("display_name") or sender_wxid
        else:
            sender_name = ""

        msg_content = row["message_content"] if "message_content" in keys else None
        comp_content = row["compress_content"] if "compress_content" in keys else None
        parsed = parse_message_row(
            local_type=raw_type,
            message_content=msg_content,
            compress_content=comp_content,
            sender_id=sender_wxid or None,
            is_group=(chat_type == "group"),
        )

        event_id = f"{self.account.account_id}:{chat_id}:{shard}:{local_id}:{sort_seq}"
        return MessageEvent(
            event_id=event_id,
            account_id=self.account.account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            local_id=local_id,
            server_id=server_id,
            sort_seq=sort_seq,
            shard=shard,
            timestamp=ts,
            iso_time=_format_iso_utc(ts),
            msg_type=parsed.msg_type,
            raw_type=parsed.raw_type,
            sender_id=sender_wxid,
            sender_name=sender_name,
            is_self=is_self,
            text=parsed.text,
            quote=parsed.quote,
            metadata=parsed.metadata,
        )

    @staticmethod
    def _matches_event_filters(
        event: MessageEvent,
        *,
        msg_types: list[str] | None,
        sender: str | None,
        keyword: str | None,
        include_self: bool,
    ) -> bool:
        if not include_self and event.is_self:
            return False
        if msg_types:
            allowed = {t.strip().lower() for t in msg_types if t.strip()}
            if allowed:
                type_match = event.msg_type.lower() in allowed
                quote_alias = "quote" in allowed and event.quote is not None
                if not (type_match or quote_alias):
                    return False
        if sender:
            s_q = sender.strip().casefold()
            if s_q not in event.sender_id.casefold() and s_q not in event.sender_name.casefold():
                return False
        if keyword:
            k_q = keyword.strip().casefold()
            haystack = event.text.casefold()
            quote_text = event.quote.excerpt.casefold() if event.quote else ""
            if k_q not in haystack and k_q not in quote_text:
                return False
        return True

    _MSG_COLS = (
        "local_id",
        "local_type",
        "real_sender_id",
        "create_time",
        "message_content",
        "compress_content",
        "server_id",
        "sort_seq",
    )

    @staticmethod
    def _build_message_where(
        *,
        before_seq: int | None = None,
        after_seq: int | None = None,
        since_ts: int | None = None,
        until_ts: int | None = None,
        msg_types: list[str] | None = None,
        keyword: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if before_seq is not None:
            clauses.append("sort_seq < ?")
            params.append(int(before_seq))
        if after_seq is not None:
            clauses.append("sort_seq > ?")
            params.append(int(after_seq))
        if since_ts is not None:
            clauses.append("create_time >= ?")
            params.append(int(since_ts))
        if until_ts is not None:
            clauses.append("create_time <= ?")
            params.append(int(until_ts))

        if msg_types:
            norm_types = [t.strip().lower() for t in msg_types if t and t.strip()]
            if norm_types and all(t in _MSG_TYPE_TO_BASE_TYPES for t in norm_types):
                base_codes = sorted(
                    {code for t in norm_types for code in _MSG_TYPE_TO_BASE_TYPES[t]}
                )
                if len(base_codes) == 1:
                    clauses.append("(local_type & 4294967295) = ?")
                    params.append(base_codes[0])
                else:
                    placeholders = ", ".join("?" for _ in base_codes)
                    clauses.append(f"(local_type & 4294967295) IN ({placeholders})")
                    params.extend(base_codes)
            if norm_types and all(t in _APPMSG_TYPE_TO_SUBTYPES for t in norm_types):
                sub_codes = sorted(
                    {0}
                    | {
                        sc
                        for t in norm_types
                        for sc in _APPMSG_TYPE_TO_SUBTYPES[t]
                    }
                )
                sub_placeholders = ", ".join("?" for _ in sub_codes)
                clauses.append(f"(local_type >> 32) IN ({sub_placeholders})")
                params.extend(sub_codes)

        if keyword:
            kw = keyword.strip()
            if kw and all(ch.isascii() or ch.lower() == ch.upper() for ch in kw):
                escaped = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                kw_cf = kw.casefold()
                has_synth_kw = any(
                    tok in kw_cf
                    for tok in (
                        "[",
                        "图片",
                        "语音",
                        "视频",
                        "表情",
                        "位置",
                        "文件",
                        "链接",
                        "小程序",
                        "聊天记录",
                        "转账",
                        "红包",
                        "卡片",
                        "通话",
                        "系统",
                        "消息",
                    )
                )
                has_xml_char = any(ch in kw for ch in "&<>'\"")
                blob_or_like = (
                    "(typeof(message_content) = 'blob' "
                    "OR typeof(compress_content) = 'blob' "
                    "OR message_content LIKE ? ESCAPE '\\' "
                    "OR compress_content LIKE ? ESCAPE '\\')"
                )
                if not has_synth_kw and not has_xml_char:
                    clauses.append(
                        f"((local_type & 4294967295) NOT IN (3, 34, 43, 47, 11000, 50) AND {blob_or_like})"
                    )
                else:
                    clauses.append(f"((local_type & 4294967295) != 1 OR {blob_or_like})")
                params.extend([pattern, pattern])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def _query_chat_table_events(
        self,
        conn: sqlite3.Connection,
        *,
        rel: str,
        shard_name: str,
        table: str,
        chat_id: str,
        chat_type: ChatType,
        chat_name: str,
        where_sql: str,
        params: list[Any],
        order_sql: str,
        limit_sql: str,
        name_map: dict[int, str],
        own_wxid: str,
        own_display: str,
        contacts: dict[str, dict[str, str]],
        msg_types: list[str] | None = None,
        sender: str | None = None,
        keyword: str | None = None,
        include_self: bool = True,
    ) -> list[MessageEvent]:
        cols = self._table_columns(conn, rel, table)
        wanted_cols = [c for c in self._MSG_COLS if c in cols]
        sql = f'SELECT {", ".join(wanted_cols)} FROM "{table}" {where_sql} {order_sql}{limit_sql}'
        out: list[MessageEvent] = []
        for r in conn.execute(sql, tuple(params)):
            ev = self._row_to_event(
                r,
                shard=shard_name,
                chat_id=chat_id,
                chat_type=chat_type,
                chat_name=chat_name,
                name_map=name_map,
                own_wxid=own_wxid,
                own_display=own_display,
                contacts=contacts,
            )
            if self._matches_event_filters(
                ev,
                msg_types=msg_types,
                sender=sender,
                keyword=keyword,
                include_self=include_self,
            ):
                out.append(ev)
        return out

    def get_messages(
        self,
        chat_id: str,
        *,
        chat_name: str | None = None,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
        since_ts: int | None = None,
        until_ts: int | None = None,
        msg_types: list[str] | None = None,
        sender: str | None = None,
        keyword: str | None = None,
        include_self: bool = True,
    ) -> dict[str, Any]:
        """Fetch historical messages for a single chat in chronological ascending order."""
        limit = max(1, min(int(limit), 1000))
        ctype: ChatType = "group" if chat_id.endswith("@chatroom") else "private"
        resolved_name = chat_name or self.get_display_name(chat_id)
        contacts = self._load_contacts()
        own_wxid = self.get_own_wxid()
        own_display = self.get_own_display_name()

        where_sql, params = self._build_message_where(
            before_seq=before_seq,
            after_seq=after_seq,
            since_ts=since_ts,
            until_ts=until_ts,
            msg_types=msg_types,
            keyword=keyword,
        )
        has_post_filter = bool(msg_types or sender or keyword or not include_self)
        ascending_fetch = after_seq is not None and before_seq is None

        events: list[MessageEvent] = []
        has_more = False

        total_matched = 0
        for attempt in (0, 1):
            shards = self._open_msg_shards_for_chat(chat_id)
            if not shards:
                return {
                    "chat_id": chat_id,
                    "chat_name": resolved_name,
                    "chat_type": ctype,
                    "messages": [],
                    "total_matched": 0,
                    "has_more": False,
                    "next_before_seq": None,
                    "latest_sort_seq": None,
                }
            try:
                raw_collected: list[MessageEvent] = []
                order_sql = (
                    "ORDER BY sort_seq ASC, local_id ASC"
                    if ascending_fetch
                    else "ORDER BY sort_seq DESC, local_id ASC"
                )
                per_shard_cap = None if has_post_filter else (limit + 1)
                limit_sql = f" LIMIT {per_shard_cap}" if per_shard_cap is not None else ""

                for conn, rel, table in shards:
                    shard_name = os.path.basename(rel)
                    name_map = self._load_shard_name2id(conn, rel)
                    raw_collected.extend(
                        self._query_chat_table_events(
                            conn,
                            rel=rel,
                            shard_name=shard_name,
                            table=table,
                            chat_id=chat_id,
                            chat_type=ctype,
                            chat_name=resolved_name,
                            where_sql=where_sql,
                            params=params,
                            order_sql=order_sql,
                            limit_sql=limit_sql,
                            name_map=name_map,
                            own_wxid=own_wxid,
                            own_display=own_display,
                            contacts=contacts,
                            msg_types=msg_types,
                            sender=sender,
                            keyword=keyword,
                            include_self=include_self,
                        )
                    )

                total_matched = len(raw_collected)
                if ascending_fetch:
                    raw_collected.sort(key=lambda e: (e.sort_seq, e.shard, e.local_id))
                    has_more = len(raw_collected) > limit
                    events = raw_collected[:limit]
                else:
                    raw_collected.sort(key=lambda e: (-e.sort_seq, e.shard, e.local_id))
                    has_more = len(raw_collected) > limit
                    events = list(reversed(raw_collected[:limit]))
                    events.sort(key=lambda e: (e.sort_seq, e.shard, e.local_id))
                break
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and _is_malformed_error(exc):
                    self._invalidate_all()
                    continue
                raise
            finally:
                for conn, _, _ in shards:
                    conn.close()

        next_before = events[0].sort_seq if (has_more and events) else None
        latest_seq = max((e.sort_seq for e in events), default=None)
        return {
            "chat_id": chat_id,
            "chat_name": resolved_name,
            "chat_type": ctype,
            "messages": events,
            "total_matched": total_matched,
            "has_more": has_more,
            "next_before_seq": next_before,
            "latest_sort_seq": latest_seq,
        }

    def get_messages_batch(
        self,
        chats: list[ChatInfo],
        *,
        limit_per_chat: int = 500,
        since_ts: int | None = None,
        until_ts: int | None = None,
        msg_types: list[str] | None = None,
        keyword: str | None = None,
        include_self: bool = True,
    ) -> dict[str, list[MessageEvent]]:
        """Fetch latest messages across multiple chats by opening each matching shard at most once."""
        if not chats:
            return {}
        limit_per_chat = max(1, min(int(limit_per_chat), 1000))
        unique_chats: dict[str, ChatInfo] = {}
        for c in chats:
            if c.chat_id not in unique_chats:
                unique_chats[c.chat_id] = c
        table_to_chat = {"Msg_" + _md5_hex(cid): c for cid, c in unique_chats.items()}
        results: dict[str, list[MessageEvent]] = {cid: [] for cid in unique_chats}

        shard_rels = [rel for rel in self._message_shard_rels() if self.pool.has_key(rel)]
        if not shard_rels:
            return results

        contacts = self._load_contacts()
        own_wxid = self.get_own_wxid()
        own_display = self.get_own_display_name()
        where_sql, params = self._build_message_where(
            since_ts=since_ts,
            until_ts=until_ts,
            msg_types=msg_types,
            keyword=keyword,
        )
        has_post_filter = bool(msg_types or keyword or not include_self)
        per_shard_cap = None if has_post_filter else (limit_per_chat + 1)
        limit_sql = f" LIMIT {per_shard_cap}" if per_shard_cap is not None else ""
        order_sql = "ORDER BY sort_seq DESC, local_id ASC"

        for attempt in (0, 1):
            force_rebuild = attempt == 1
            per_chat_raw: dict[str, list[MessageEvent]] = {cid: [] for cid in unique_chats}
            try:
                if len(shard_rels) >= 2:
                    self.pool.prefetch_snapshots(shard_rels, force_rebuild=force_rebuild)
                for rel in shard_rels:
                    shard_tables = self._get_shard_tables(rel, force_rebuild=force_rebuild)
                    matching_tables = [tbl for tbl in table_to_chat if tbl in shard_tables]
                    if not matching_tables:
                        continue
                    conn = self.pool.open_readonly(rel, force_rebuild=force_rebuild)
                    try:
                        name_map = self._load_shard_name2id(conn, rel)
                        shard_name = os.path.basename(rel)
                        for tbl in matching_tables:
                            chat = table_to_chat[tbl]
                            per_chat_raw[chat.chat_id].extend(
                                self._query_chat_table_events(
                                    conn,
                                    rel=rel,
                                    shard_name=shard_name,
                                    table=tbl,
                                    chat_id=chat.chat_id,
                                    chat_type=chat.chat_type,
                                    chat_name=chat.display_name,
                                    where_sql=where_sql,
                                    params=params,
                                    order_sql=order_sql,
                                    limit_sql=limit_sql,
                                    name_map=name_map,
                                    own_wxid=own_wxid,
                                    own_display=own_display,
                                    contacts=contacts,
                                    msg_types=msg_types,
                                    keyword=keyword,
                                    include_self=include_self,
                                )
                            )
                    finally:
                        conn.close()

                for cid, raw_collected in per_chat_raw.items():
                    if not raw_collected:
                        continue
                    raw_collected.sort(key=lambda e: (-e.sort_seq, e.shard, e.local_id))
                    chosen = list(reversed(raw_collected[:limit_per_chat]))
                    chosen.sort(key=lambda e: (e.sort_seq, e.shard, e.local_id))
                    results[cid] = chosen
                return results
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and _is_malformed_error(exc):
                    self._invalidate_all()
                    continue
                raise
        return results

    def search_messages_detailed(
        self,
        keyword: str,
        *,
        chat_id: str | None = None,
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
        chat_type: str = "all",
        since_ts: int | None = None,
        until_ts: int | None = None,
        min_activity_ts: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search messages across target or filtered chats and return matches plus scan telemetry."""
        limit = max(1, min(int(limit), 500))
        excluded_ids = {
            self.resolve_chat(ref).chat_id
            for ref in (exclude_chats or [])
            if ref and ref.strip()
        }

        explicit_refs: list[str] = []
        if chat_id and chat_id.strip():
            explicit_refs.append(chat_id)
        for ref in chats or []:
            if ref and ref.strip():
                explicit_refs.append(ref)

        if explicit_refs:
            resolved_map: dict[str, ChatInfo] = {}
            for ref in explicit_refs:
                target = self.resolve_chat(ref)
                if target.chat_id in excluded_ids:
                    continue
                if chat_type in ("private", "group") and target.chat_type != chat_type:
                    continue
                if (
                    min_activity_ts is not None
                    and target.last_timestamp > 0
                    and target.last_timestamp < min_activity_ts - 60
                ):
                    continue
                resolved_map.setdefault(target.chat_id, target)

            target_list = list(resolved_map.values())
            if not target_list:
                return {
                    "messages": [],
                    "total_matched": 0,
                    "scanned_chats": 0,
                    "truncated": False,
                }
            if len(target_list) == 1:
                only = target_list[0]
                res = self.get_messages(
                    only.chat_id,
                    chat_name=only.display_name,
                    limit=limit,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    keyword=keyword,
                    include_self=True,
                )
                return {
                    "messages": res["messages"],
                    "total_matched": res["total_matched"],
                    "scanned_chats": 1,
                    "truncated": res["has_more"],
                }
            eligible_chats = target_list
            use_waves = False
            wave_size = len(eligible_chats)
        else:
            all_chats = self.list_chats(
                chat_type=chat_type,
                limit=10_000,
                include_hidden=True,
                include_non_session=(since_ts is None and min_activity_ts is None),
            )
            if excluded_ids:
                all_chats = [c for c in all_chats if c.chat_id not in excluded_ids]
            activity_cutoffs = [x for x in (since_ts, min_activity_ts) if x is not None]
            activity_cutoff = max(activity_cutoffs) if activity_cutoffs else None
            eligible_chats = [
                chat
                for chat in all_chats
                if activity_cutoff is None
                or (chat.last_timestamp > 0 and chat.last_timestamp >= activity_cutoff - 60)
            ]
            use_waves = activity_cutoff is not None
            if use_waves:
                eligible_chats.sort(key=lambda c: c.last_timestamp, reverse=True)
                wave_size = max(32, min(limit * 2, 80))
            else:
                wave_size = max(1, len(eligible_chats))

        hits: list[MessageEvent] = []
        offset = 0
        scanned_chats = 0
        while offset < len(eligible_chats):
            chunk = eligible_chats[offset : offset + wave_size]
            offset += len(chunk)
            scanned_chats += len(chunk)
            batch_by_chat = self.get_messages_batch(
                chunk,
                limit_per_chat=min(1000, limit + 1),
                since_ts=since_ts,
                until_ts=until_ts,
                keyword=keyword,
                include_self=True,
            )
            for chat in chunk:
                hits.extend(batch_by_chat.get(chat.chat_id, ()))
            if use_waves and offset < len(eligible_chats) and len(hits) >= limit:
                hits.sort(key=lambda e: (e.timestamp, e.sort_seq, e.chat_id, e.local_id), reverse=True)
                if hits[limit - 1].timestamp > eligible_chats[offset].last_timestamp + 60:
                    break

        hits.sort(key=lambda e: (e.timestamp, e.sort_seq, e.chat_id, e.local_id), reverse=True)
        total_matched = len(hits)
        chosen = hits[:limit]
        chosen.sort(key=lambda e: (e.timestamp, e.sort_seq, e.chat_id, e.local_id))
        return {
            "messages": chosen,
            "total_matched": total_matched,
            "scanned_chats": scanned_chats,
            "truncated": total_matched > len(chosen),
        }

    def search_messages(
        self,
        keyword: str,
        *,
        chat_id: str | None = None,
        chats: list[str] | None = None,
        exclude_chats: list[str] | None = None,
        chat_type: str = "all",
        since_ts: int | None = None,
        until_ts: int | None = None,
        min_activity_ts: int | None = None,
        limit: int = 50,
    ) -> list[MessageEvent]:
        """Search messages across one chat or all chats matching chat_type."""
        return self.search_messages_detailed(
            keyword,
            chat_id=chat_id,
            chats=chats,
            exclude_chats=exclude_chats,
            chat_type=chat_type,
            since_ts=since_ts,
            until_ts=until_ts,
            min_activity_ts=min_activity_ts,
            limit=limit,
        )["messages"]

    def get_new_messages_for_chat(
        self,
        chat_id: str,
        *,
        since_seq: int = 0,
        since_ts: int | None = None,
        limit: int = 200,
    ) -> list[MessageEvent]:
        """Fetch messages strictly after since_seq without ever splitting a sort_seq group."""
        ctype: ChatType = "group" if chat_id.endswith("@chatroom") else "private"
        chat_name = self.get_display_name(chat_id)
        contacts = self._load_contacts()
        own_wxid = self.get_own_wxid()
        own_display = self.get_own_display_name()

        page_limit = max(1, int(limit))
        while True:
            collected: list[MessageEvent] = []
            truncated_hit = False
            for attempt in (0, 1):
                shards = self._open_msg_shards_for_chat(chat_id)
                if not shards:
                    return []
                try:
                    clauses = ["sort_seq > ?"]
                    params: list[Any] = [int(since_seq)]
                    if since_ts is not None:
                        clauses.append("create_time >= ?")
                        params.append(int(since_ts))
                    where_sql = "WHERE " + " AND ".join(clauses)
                    for conn, rel, table in shards:
                        shard_name = os.path.basename(rel)
                        name_map = self._load_shard_name2id(conn, rel)
                        cols = self._table_columns(conn, rel, table)
                        wanted_cols = [
                            c
                            for c in (
                                "local_id",
                                "local_type",
                                "real_sender_id",
                                "create_time",
                                "message_content",
                                "compress_content",
                                "server_id",
                                "sort_seq",
                            )
                            if c in cols
                        ]
                        sql = (
                            f'SELECT {", ".join(wanted_cols)} FROM "{table}" '
                            f"{where_sql} ORDER BY sort_seq ASC, local_id ASC LIMIT {page_limit}"
                        )
                        rows = conn.execute(sql, tuple(params)).fetchall()
                        if len(rows) >= page_limit:
                            truncated_hit = True
                        for r in rows:
                            collected.append(
                                self._row_to_event(
                                    r,
                                    shard=shard_name,
                                    chat_id=chat_id,
                                    chat_type=ctype,
                                    chat_name=chat_name,
                                    name_map=name_map,
                                    own_wxid=own_wxid,
                                    own_display=own_display,
                                    contacts=contacts,
                                )
                            )
                    break
                except sqlite3.DatabaseError as exc:
                    if attempt == 0 and _is_malformed_error(exc):
                        self._invalidate_all()
                        continue
                    raise
                finally:
                    for conn, _, _ in shards:
                        conn.close()

            collected.sort(key=lambda e: (e.sort_seq, e.shard, e.local_id))
            if not truncated_hit or not collected:
                return collected

            # Avoid returning a partial trailing sort_seq group when a shard hit page_limit
            tail_seq = collected[-1].sort_seq
            if collected[0].sort_seq != tail_seq:
                return [e for e in collected if e.sort_seq != tail_seq]
            if page_limit >= 3200:
                return collected
            page_limit *= 2

    def discover_chat_max_seqs(self) -> dict[str, int]:
        """Map all known chat_ids (from contacts & sessions) to their current max sort_seq."""
        known_chats: set[str] = set()
        for c in self.list_chats(chat_type="all", limit=100_000, include_hidden=True):
            known_chats.add(c.chat_id)
        for uname in self._load_contacts():
            known_chats.add(uname)

        md5_to_chat = {_md5_hex(cid): cid for cid in known_chats}
        max_seqs: dict[str, int] = {}
        shard_rels = [rel for rel in self._message_shard_rels() if self.pool.has_key(rel)]

        for attempt in (0, 1):
            conns: list[sqlite3.Connection] = []
            try:
                if len(shard_rels) >= 2:
                    self.pool.prefetch_snapshots(shard_rels, force_rebuild=(attempt == 1))
                max_seqs = {}
                for rel in shard_rels:
                    conn = self.pool.open_readonly(rel, force_rebuild=(attempt == 1))
                    conns.append(conn)
                    tables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall()
                    for (tname,) in tables:
                        md5_part = str(tname)[4:].lower()
                        chat_id = md5_to_chat.get(md5_part)
                        if not chat_id:
                            continue
                        row = conn.execute(f'SELECT MAX(sort_seq) FROM "{tname}"').fetchone()
                        seq = int(row[0] or 0) if row and row[0] is not None else 0
                        if seq > max_seqs.get(chat_id, -1):
                            max_seqs[chat_id] = seq
                return max_seqs
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and _is_malformed_error(exc):
                    self._invalidate_all()
                    continue
                raise
            finally:
                for conn in conns:
                    conn.close()
        return max_seqs

    def get_chat_history_raw(
        self,
        chat_id: str,
        *,
        chat_name: str | None = None,
        seq: int | None = None,
        message_id: str | int | None = None,
    ) -> dict[str, Any]:
        """Locate a single forwarded chat_history message row and decode its raw XML payload."""
        msg_id_str = str(message_id).strip() if message_id is not None else ""
        if seq is None and not msg_id_str:
            raise WxObserverError(
                "invalid_message_selector",
                "Provide either seq (sort_seq) or message_id.",
            )

        ctype: ChatType = "group" if chat_id.endswith("@chatroom") else "private"
        resolved_name = chat_name or self.get_display_name(chat_id)
        contacts = self._load_contacts()
        own_wxid = self.get_own_wxid()
        own_display = self.get_own_display_name()

        shard_filter: str | None = None
        clauses: list[str] = ["(local_type & 4294967295) = 49", "(local_type >> 32) IN (0, 19)"]
        params: list[Any] = []

        if seq is not None:
            clauses.append("sort_seq = ?")
            params.append(int(seq))
        else:
            parts = msg_id_str.split(":")
            if len(parts) == 5 and parts[3].isdigit() and parts[4].lstrip("-").isdigit():
                shard_filter = parts[2]
                clauses.append("local_id = ? AND sort_seq = ?")
                params.extend([int(parts[3]), int(parts[4])])
            elif msg_id_str.lstrip("-").isdigit():
                num = int(msg_id_str)
                clauses.append("(sort_seq = ? OR server_id = ? OR local_id = ?)")
                params.extend([num, num, num])
            else:
                raise WxObserverError(
                    "message_not_found",
                    f"No chat_history message matched selector '{msg_id_str}' in chat '{chat_id}'.",
                    {"chat_id": chat_id, "message_id": msg_id_str},
                )

        where_sql = "WHERE " + " AND ".join(clauses)

        for attempt in (0, 1):
            shards = self._open_msg_shards_for_chat(chat_id)
            if not shards:
                break
            try:
                for conn, rel, table in shards:
                    shard_name = os.path.basename(rel)
                    if shard_filter and shard_name != shard_filter:
                        continue
                    cols = self._table_columns(conn, rel, table)
                    wanted_cols = [c for c in self._MSG_COLS if c in cols]
                    sql = (
                        f'SELECT {", ".join(wanted_cols)} FROM "{table}" '
                        f"{where_sql} ORDER BY sort_seq DESC, local_id ASC"
                    )
                    name_map = self._load_shard_name2id(conn, rel)
                    for row in conn.execute(sql, tuple(params)):
                        ev = self._row_to_event(
                            row,
                            shard=shard_name,
                            chat_id=chat_id,
                            chat_type=ctype,
                            chat_name=resolved_name,
                            name_map=name_map,
                            own_wxid=own_wxid,
                            own_display=own_display,
                            contacts=contacts,
                        )
                        if ev.msg_type != "chat_history":
                            continue
                        keys = set(row.keys())
                        msg_content = row["message_content"] if "message_content" in keys else None
                        comp_content = row["compress_content"] if "compress_content" in keys else None
                        primary = decode_payload(msg_content)
                        secondary = decode_payload(comp_content) if comp_content else ""
                        if ctype == "group":
                            if primary:
                                primary = strip_group_sender_prefix(primary, ev.sender_id or None)
                            if secondary:
                                secondary = strip_group_sender_prefix(secondary, ev.sender_id or None)
                        if primary.lstrip().startswith("<"):
                            xml_body = primary
                        elif secondary.lstrip().startswith("<"):
                            xml_body = secondary
                        else:
                            xml_body = primary or secondary
                        return {
                            "event": ev,
                            "xml_body": xml_body,
                        }
                break
            except sqlite3.DatabaseError as exc:
                if attempt == 0 and _is_malformed_error(exc):
                    self._invalidate_all()
                    continue
                raise
            finally:
                for conn, _, _ in shards:
                    conn.close()

        raise WxObserverError(
            "message_not_found",
            f"No chat_history message matched in chat '{chat_id}'.",
            {"chat_id": chat_id, "seq": seq, "message_id": message_id},
        )

    def verify_health(self) -> dict[str, Any]:
        """Verify decryption, WAL merge, PRAGMA quick_check, and latest message timestamp."""
        self._ensure_shards_up_to_date()
        keyed_rels = sorted(rel for rel in self._db_files if rel in self._keys)
        if len(keyed_rels) >= 2:
            self.pool.prefetch_snapshots(keyed_rels)

        verified_dbs = 0
        failed_dbs: list[str] = []
        wal_frames_merged = 0
        for rel in keyed_rels:
            try:
                snap = self.pool.ensure_snapshot(rel)
                if quick_check_sqlite(snap):
                    verified_dbs += 1
                    st = self.pool.get_snapshot_state(rel)
                    if st is not None and st.applied_frames > 0:
                        wal_frames_merged += st.applied_frames
                else:
                    failed_dbs.append(rel)
            except Exception:
                failed_dbs.append(rel)

        chats_count = 0
        latest_ts = 0
        try:
            chats = self.list_chats(chat_type="all", limit=100_000, include_hidden=True)
            chats_count = len(chats)
            latest_ts = max((c.last_timestamp for c in chats), default=0)
        except Exception:
            pass

        if latest_ts <= 0:
            for rel in self._message_shard_rels():
                if rel not in self._keys:
                    continue
                try:
                    conn = self.pool.open_readonly(rel)
                    try:
                        for tname in self._get_shard_tables(rel):
                            row = conn.execute(f'SELECT MAX(create_time) FROM "{tname}"').fetchone()
                            if row and row[0]:
                                latest_ts = max(latest_ts, int(row[0]))
                    finally:
                        conn.close()
                except Exception:
                    continue

        return {
            "total_dbs": self.total_db_count,
            "keyed_dbs": self.keyed_db_count,
            "verified_dbs": verified_dbs,
            "failed_dbs": failed_dbs,
            "quick_check_passed": (
                verified_dbs == self.total_db_count and not failed_dbs and self.total_db_count > 0
            ),
            "wal_frames_merged": wal_frames_merged,
            "chats_count": chats_count,
            "latest_timestamp": latest_ts,
            "warnings": self.warnings,
        }

    def inspect_db_metadata(self) -> list[dict[str, Any]]:
        """Collect strictly redacted per-database health, schema version, and WAL metadata."""
        self._ensure_shards_up_to_date()
        entries: list[dict[str, Any]] = []
        for rel in sorted(self._db_files):
            path = self._db_files[rel]
            wal_path = Path(str(path) + "-wal")
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = 0
            wal_exists = wal_path.is_file()
            try:
                wal_size = wal_path.stat().st_size if wal_exists else 0
            except OSError:
                wal_size = 0

            has_key = rel in self._keys
            key_source = self._key_sources.get(rel)
            quick_ok: bool | None = None
            applied_frames: int | None = None
            wal_warning: str | None = None
            user_version: int | None = None
            schema_version: int | None = None
            msg_table_count: int | None = None

            if has_key:
                try:
                    snap = self.pool.ensure_snapshot(rel)
                    quick_ok = quick_check_sqlite(snap)
                    st = self.pool.get_snapshot_state(rel)
                    if st is not None:
                        applied_frames = st.applied_frames
                        wal_warning = st.wal_warning
                    conn = self.pool.open_readonly(rel)
                    try:
                        uv = conn.execute("PRAGMA user_version").fetchone()
                        sv = conn.execute("PRAGMA schema_version").fetchone()
                        user_version = int(uv[0]) if uv else None
                        schema_version = int(sv[0]) if sv else None
                        if _MESSAGE_SHARD_RE.match(rel):
                            cnt_row = conn.execute(
                                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                            ).fetchone()
                            msg_table_count = int(cnt_row[0]) if cnt_row else 0
                    finally:
                        conn.close()
                except Exception as exc:
                    quick_ok = False
                    wal_warning = f"{type(exc).__name__}"

            entries.append(
                {
                    "rel_path": rel,
                    "size_bytes": size_bytes,
                    "wal_exists": wal_exists,
                    "wal_size_bytes": wal_size,
                    "has_key": has_key,
                    "key_source": key_source,
                    "quick_check_ok": quick_ok,
                    "applied_wal_frames": applied_frames,
                    "wal_warning": wal_warning,
                    "user_version": user_version,
                    "schema_version": schema_version,
                    "msg_table_count": msg_table_count,
                }
            )
        return entries


def probe_account_profile(
    account: AccountInfo,
    *,
    keys_file: str | Path | None = None,
    state_dir: str | Path | None = None,
    use_key_cache: bool = True,
    temp_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Best-effort lightweight probe of own {wxid, display_name, alias} from contact.db."""
    db_files = _collect_account_db_files(account.db_storage_dir)
    contact_rel = None
    for rel in sorted(db_files):
        if os.path.basename(rel).lower() == "contact.db":
            contact_rel = rel
            break
    if not contact_rel:
        return None

    contact_path = db_files[contact_rel]
    try:
        st = contact_path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (-1, -1)

    keys: dict[str, bytes] = {}
    eff_keys_file = Path(keys_file) if keys_file else (
        Path(os.environ["WX_OBSERVER_KEYS_FILE"])
        if os.environ.get("WX_OBSERVER_KEYS_FILE")
        else None
    )
    if eff_keys_file is not None:
        try:
            keys.update(load_external_keys(eff_keys_file, {contact_rel: contact_path}))
        except Exception:
            pass

    key_store = KeyStore(state_dir=state_dir)
    if contact_rel not in keys and use_key_cache:
        try:
            keys.update(
                key_store.load_cached_keys(
                    account.account_id,
                    {contact_rel: contact_path},
                )
            )
        except Exception:
            pass

    if contact_rel not in keys and account.online and account.pids:
        try:
            target_map = db_files if use_key_cache else {contact_rel: contact_path}
            scanned = extract_keys_from_pids(account.pids, target_map)
            if scanned:
                keys.update(scanned)
                if use_key_cache:
                    key_store.save_cached_keys(account.account_id, keys)
        except Exception:
            return None

    if contact_rel not in keys:
        return None

    pool = EncryptedDatabasePool(temp_root=temp_root)
    try:
        pool.register_databases({contact_rel: contact_path}, {contact_rel: keys[contact_rel]})
        conn = pool.open_readonly(contact_rel)
        try:
            cols = {
                str(r["name"])
                for r in conn.execute('PRAGMA table_info("contact")').fetchall()
            }
            if "username" not in cols:
                return None
            select_cols = ["username"]
            for c in ("nick_name", "remark", "alias"):
                if c in cols:
                    select_cols.append(c)
            candidates = [account.account_id, derive_wxid_from_account_dir(account.account_id)]
            for cand in dict.fromkeys(c for c in candidates if c):
                row = conn.execute(
                    f"SELECT {', '.join(select_cols)} FROM contact WHERE username = ? LIMIT 1",
                    (cand,),
                ).fetchone()
                if row is not None:
                    wxid = str(row["username"] or cand)
                    nick = str(row["nick_name"] or "") if "nick_name" in cols else ""
                    remark = str(row["remark"] or "") if "remark" in cols else ""
                    alias = str(row["alias"] or "") if "alias" in cols else ""
                    return {
                        "wxid": wxid,
                        "display_name": remark or nick or wxid,
                        "alias": alias,
                        "stamp": stamp,
                    }
        finally:
            conn.close()
    except Exception:
        return None
    finally:
        pool.cleanup()
    return None
