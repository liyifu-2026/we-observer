"""Synthetic SQLCipher 4 + WAL test fixtures matching WeChat 4.x database schemas."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import sqlite3
import struct
import time
from pathlib import Path
from typing import Callable

import zstandard as zstd
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wx_observer.cipher_store import MASTER_KDF_ITERS, PAGE_SIZE, RESERVE_SIZE, WAL_HEADER_SIZE


def create_reserve80_sqlite(populate_fn: Callable[[sqlite3.Connection], None]) -> bytes:
    """Create a standard SQLite DB image whose every 4096B page reserves the last 80 bytes."""
    seed = sqlite3.connect(":memory:")
    seed.execute("PRAGMA page_size = 4096")
    seed.execute("CREATE TABLE _seed_tmp (x INTEGER)")
    seed.execute("DROP TABLE _seed_tmp")
    seed.commit()
    raw = bytearray(seed.serialize())
    seed.close()

    raw[20] = RESERVE_SIZE
    raw[105:107] = struct.pack(">H", PAGE_SIZE - RESERVE_SIZE)
    raw[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE] = b"\x00" * RESERVE_SIZE

    conn = sqlite3.connect(":memory:")
    conn.deserialize(bytes(raw))
    populate_fn(conn)
    conn.commit()
    out = conn.serialize()
    conn.close()
    return out


def modify_reserve80_sqlite(
    base_sqlite: bytes,
    mutate_fn: Callable[[sqlite3.Connection], None],
) -> bytes:
    """Apply mutations to an existing 80-byte-reserved SQLite image and return updated bytes."""
    conn = sqlite3.connect(":memory:")
    conn.deserialize(base_sqlite)
    mutate_fn(conn)
    conn.commit()
    out = conn.serialize()
    conn.close()
    return out


def _encrypt_page(
    enc_key: bytes,
    mac_key: bytes,
    plain_page: bytes,
    pgno: int,
    iv: bytes,
    *,
    salt: bytes,
    plaintext_header: bool,
) -> bytes:
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    if pgno == 1:
        prefix = plain_page[:16] if plaintext_header else salt
        body = cipher.update(plain_page[16 : PAGE_SIZE - RESERVE_SIZE]) + cipher.finalize()
        mac_input = body + iv
    else:
        prefix = b""
        body = cipher.update(plain_page[: PAGE_SIZE - RESERVE_SIZE]) + cipher.finalize()
        mac_input = body + iv
    hm = hmac.new(mac_key, mac_input, hashlib.sha512)
    hm.update(struct.pack("<I", pgno))
    return prefix + body + iv + hm.digest()


def encrypt_sqlite_image(
    plain_sqlite: bytes,
    key: bytes,
    *,
    salt: bytes | None = None,
    plaintext_header: bool = False,
) -> bytes:
    """Encrypt an entire 80B-reserved SQLite image using SQLCipher 4 AES-256-CBC + HMAC-SHA512."""
    assert len(plain_sqlite) % PAGE_SIZE == 0
    if salt is None:
        salt = hashlib.sha256(plain_sqlite[:64]).digest()[:16]
    enc_key = key[:32]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)

    pages = len(plain_sqlite) // PAGE_SIZE
    out = bytearray()
    for pgno in range(1, pages + 1):
        chunk = plain_sqlite[(pgno - 1) * PAGE_SIZE : pgno * PAGE_SIZE]
        iv = hashlib.sha256(salt + struct.pack("<I", pgno)).digest()[:16]
        out.extend(
            _encrypt_page(
                enc_key,
                mac_key,
                chunk,
                pgno,
                iv,
                salt=salt,
                plaintext_header=plaintext_header,
            )
        )
    return bytes(out)


def build_encrypted_wal_frames(
    updated_sqlite: bytes,
    base_sqlite: bytes | None,
    key: bytes,
    *,
    db_salt: bytes,
    wal_salt: bytes,
    frame_seq_start: int = 1,
    plaintext_header: bool = False,
) -> bytes:
    """Build encrypted 4120-byte WAL frame bytes for pages changed vs base_sqlite."""
    enc_key = key[:32]
    mac_salt = bytes(b ^ 0x3A for b in db_salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)

    total_pages = len(updated_sqlite) // PAGE_SIZE
    changed_pgnos: list[int] = []
    for pgno in range(1, total_pages + 1):
        new_page = updated_sqlite[(pgno - 1) * PAGE_SIZE : pgno * PAGE_SIZE]
        if base_sqlite is None or pgno * PAGE_SIZE > len(base_sqlite):
            changed_pgnos.append(pgno)
        else:
            old_page = base_sqlite[(pgno - 1) * PAGE_SIZE : pgno * PAGE_SIZE]
            if new_page != old_page:
                changed_pgnos.append(pgno)

    if not changed_pgnos:
        changed_pgnos = [1]

    frames = bytearray()
    for idx, pgno in enumerate(changed_pgnos):
        is_last = idx == len(changed_pgnos) - 1
        commit_pages = total_pages if is_last else 0
        frame_hdr = struct.pack(">II", pgno, commit_pages) + wal_salt + b"\x00" * 8
        plain_page = updated_sqlite[(pgno - 1) * PAGE_SIZE : pgno * PAGE_SIZE]
        iv = hashlib.sha256(wal_salt + struct.pack("<II", pgno, frame_seq_start + idx)).digest()[:16]
        enc_page = _encrypt_page(
            enc_key,
            mac_key,
            plain_page,
            pgno,
            iv,
            salt=db_salt,
            plaintext_header=plaintext_header,
        )
        frames.extend(frame_hdr)
        frames.extend(enc_page)
    return bytes(frames)


def build_encrypted_wal(
    updated_sqlite: bytes,
    base_sqlite: bytes | None,
    key: bytes,
    *,
    db_salt: bytes,
    wal_salt: bytes,
    plaintext_header: bool = False,
) -> bytes:
    """Create a valid SQLite WAL file containing encrypted changed pages vs base_sqlite."""
    wal = bytearray()
    wal.extend(struct.pack(">IIII", 0x377F0682, 3007000, PAGE_SIZE, 1))
    wal.extend(wal_salt)
    wal.extend(b"\x00" * 8)
    assert len(wal) == WAL_HEADER_SIZE
    wal.extend(
        build_encrypted_wal_frames(
            updated_sqlite,
            base_sqlite,
            key,
            db_salt=db_salt,
            wal_salt=wal_salt,
            plaintext_header=plaintext_header,
        )
    )
    return bytes(wal)


def make_quote_xml(reply_text: str, *, ref_type: int, ref_content: str, display_name: str, from_usr: str, svrid: str = "9001") -> str:
    return (
        "<msg><appmsg>"
        "<type>57</type>"
        f"<title>{html.escape(reply_text)}</title>"
        "<refermsg>"
        f"<type>{ref_type}</type>"
        f"<svrid>{html.escape(svrid)}</svrid>"
        f"<displayname>{html.escape(display_name)}</displayname>"
        f"<chatusr>{html.escape(from_usr)}</chatusr>"
        f"<content>{html.escape(ref_content)}</content>"
        "</refermsg>"
        "</appmsg></msg>"
    )


def make_file_xml(filename: str, size_bytes: int, ext: str = "pdf") -> str:
    return (
        "<msg><appmsg>"
        "<type>6</type>"
        f"<title>{filename}</title>"
        f"<appattach><totallen>{size_bytes}</totallen><fileext>{ext}</fileext></appattach>"
        "</appmsg></msg>"
    )


def make_link_xml(title: str, desc: str, url: str, source: str = "TechNews") -> str:
    return (
        "<msg><appmsg>"
        "<type>5</type>"
        f"<title>{title}</title>"
        f"<des>{desc}</des>"
        f"<url>{url}</url>"
        f"<sourcedisplayname>{source}</sourcedisplayname>"
        "</appmsg></msg>"
    )


class SyntheticAccountEnv:
    """Full synthetic WeChat 4.x directory tree with multi-shard encrypted DBs & live WAL updates."""

    def __init__(self, root_dir: Path, account_id: str = "wxid_self001_ab12") -> None:
        self.root_dir = Path(root_dir)
        self.db_dir = self.root_dir / "xwechat_files"
        self.account_id = account_id
        self.own_wxid = "wxid_self001"
        self.storage_dir = self.db_dir / account_id / "db_storage"
        (self.storage_dir / "contact").mkdir(parents=True, exist_ok=True)
        (self.storage_dir / "session").mkdir(parents=True, exist_ok=True)
        (self.storage_dir / "message").mkdir(parents=True, exist_ok=True)

        self.master_key = bytes.fromhex("11" * 32)
        self.keys_hex: dict[str, str] = {}
        self._plain_dbs: dict[str, bytes] = {}
        self._salts: dict[str, bytes] = {}
        self._wal_salts: dict[str, bytes] = {}
        self._db_keys: dict[str, bytes] = {}

        self.alice_id = "wxid_alice99"
        self.bob_id = "wxid_bob88"
        self.room_id = "555123456@chatroom"
        self.base_ts = int(time.time()) - 3600

        self._build_initial_databases()
        self.keys_file = self.root_dir / "keys.json"
        self.keys_file.write_text(
            json.dumps({"keys": self.keys_hex}, indent=2),
            encoding="utf-8",
        )

    def _write_encrypted_db(self, rel: str, plain_bytes: bytes, salt_seed: bytes) -> None:
        salt = hashlib.sha256(salt_seed).digest()[:16]
        # Derive per-DB key from master_key via SQLCipher 4 256k-iter PBKDF2
        db_key = hashlib.pbkdf2_hmac("sha512", self.master_key, salt, MASTER_KDF_ITERS, dklen=32)
        enc = encrypt_sqlite_image(plain_bytes, db_key, salt=salt, plaintext_header=False)
        dest = self.storage_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(enc)
        self._plain_dbs[rel] = plain_bytes
        self._salts[rel] = salt
        self._db_keys[rel] = db_key
        self.keys_hex[rel] = db_key.hex()

    def _build_initial_databases(self) -> None:
        # 1. contact/contact.db
        def pop_contact(conn: sqlite3.Connection) -> None:
            conn.execute(
                "CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT UNIQUE, nick_name TEXT, remark TEXT, alias TEXT)"
            )
            conn.execute("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT UNIQUE, owner TEXT)")
            conn.execute("CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)")
            conn.executemany(
                "INSERT INTO contact (id, username, nick_name, remark, alias) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, self.own_wxid, "SelfNick", "", "self_alias"),
                    (2, self.alice_id, "AliceReal", "AliceRemark", "alice_a"),
                    (3, self.bob_id, "BobReal", "", "bob_b"),
                    (4, self.room_id, "Project Alpha Group", "", ""),
                    (5, "wxid_duplicate1", "DupNick", "SameLabel", ""),
                    (6, "wxid_duplicate2", "DupNick2", "SameLabel", ""),
                ],
            )
            conn.execute("INSERT INTO chat_room VALUES (1, ?, ?)", (self.room_id, self.alice_id))
            conn.executemany("INSERT INTO chatroom_member VALUES (?, ?)", [(1, 1), (1, 2), (1, 3)])

        self._write_encrypted_db("contact/contact.db", create_reserve80_sqlite(pop_contact), b"contact-salt")

        # 2. session/session.db
        t0 = self.base_ts
        def pop_session(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE SessionTable (
                    username TEXT PRIMARY KEY,
                    unread_count INTEGER DEFAULT 0,
                    summary TEXT DEFAULT '',
                    last_timestamp INTEGER DEFAULT 0,
                    last_msg_sender TEXT DEFAULT '',
                    last_sender_display_name TEXT DEFAULT '',
                    sort_timestamp INTEGER DEFAULT 0,
                    is_hidden INTEGER DEFAULT 0
                )
                """
            )
            conn.executemany(
                "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (self.alice_id, 1, "See you tomorrow", t0 + 50, self.alice_id, "AliceRemark", t0 + 50, 0),
                    (self.room_id, 2, "Group update", t0 + 60, self.bob_id, "BobReal", t0 + 60, 0),
                    (self.bob_id, 0, "Hidden note", t0 + 10, self.bob_id, "BobReal", t0 + 10, 1),
                ],
            )

        self._write_encrypted_db("session/session.db", create_reserve80_sqlite(pop_session), b"session-salt")

        alice_tbl = "Msg_" + hashlib.md5(self.alice_id.encode("utf-8")).hexdigest()
        room_tbl = "Msg_" + hashlib.md5(self.room_id.encode("utf-8")).hexdigest()

        msg_ddl = """
            CREATE TABLE "{tbl}" (
                local_id INTEGER PRIMARY KEY,
                server_id INTEGER,
                local_type INTEGER,
                sort_seq INTEGER,
                real_sender_id INTEGER,
                create_time INTEGER,
                message_content BLOB,
                compress_content BLOB,
                packed_info_data BLOB,
                source TEXT
            )
        """

        # 3. message/message_0.db: real_sender_id 1=self, 2=alice, 3=bob
        def pop_msg0(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.executemany(
                "INSERT INTO Name2Id (rowid, user_name) VALUES (?, ?)",
                [(1, self.own_wxid), (2, self.alice_id), (3, self.bob_id)],
            )
            conn.execute(msg_ddl.format(tbl=alice_tbl))
            conn.execute(msg_ddl.format(tbl=room_tbl))

            compressed = zstd.ZstdCompressor().compress("Zstd compressed long message 中文测试".encode("utf-8"))
            conn.executemany(
                f'INSERT INTO "{alice_tbl}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (1, 1001, 1, 100, 2, t0 + 10, "Hello from Alice (shard 0)", None, None, ""),
                    (2, 1002, 1, 200, 1, t0 + 20, "Reply from Self (shard 0)", None, None, ""),
                    (3, 1003, 1, 300, 2, t0 + 30, compressed, None, None, ""),
                ],
            )
            conn.executemany(
                f'INSERT INTO "{room_tbl}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (1, 2001, 1, 150, 2, t0 + 15, f"{self.alice_id}:\nMorning team!", None, None, ""),
                    (2, 2002, 49, 250, 3, t0 + 25, f"{self.bob_id}:\n{make_file_xml('spec.pdf', 12345)}", None, None, ""),
                ],
            )

        self._write_encrypted_db("message/message_0.db", create_reserve80_sqlite(pop_msg0), b"msg0-salt")

        # 4. message/message_1.db: INTENTIONALLY DIFFERENT Name2Id rowids! 1=bob, 2=self, 3=alice
        def pop_msg1(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.executemany(
                "INSERT INTO Name2Id (rowid, user_name) VALUES (?, ?)",
                [(1, self.bob_id), (2, self.own_wxid), (3, self.alice_id)],
            )
            conn.execute(msg_ddl.format(tbl=alice_tbl))
            conn.execute(msg_ddl.format(tbl=room_tbl))

            quote_xml = make_quote_xml(
                "Got it!",
                ref_type=1,
                ref_content="Hello from Alice (shard 0)",
                display_name="AliceRemark",
                from_usr=self.alice_id,
            )
            conn.executemany(
                f'INSERT INTO "{alice_tbl}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (1, 1004, 49, 400, 3, t0 + 40, quote_xml, None, None, ""),
                    (2, 1005, 3, 500, 3, t0 + 50, '<msg><img md5="0123456789abcdef0123456789abcdef" length="4096"/></msg>', None, None, ""),
                ],
            )
            conn.executemany(
                f'INSERT INTO "{room_tbl}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (1, 2003, 1, 350, 1, t0 + 35, f"{self.bob_id}:\nGroup msg in shard 1", None, None, ""),
                    (2, 2004, 1, 450, 2, t0 + 45, f"{self.own_wxid}:\nSelf msg in shard 1", None, None, ""),
                ],
            )

        self._write_encrypted_db("message/message_1.db", create_reserve80_sqlite(pop_msg1), b"msg1-salt")

    def append_wal_messages(
        self,
        chat_id: str,
        rows: list[tuple[int, int, int, int, int, str | bytes]],
        *,
        rel: str = "message/message_1.db",
        wal_salt: bytes | None = None,
    ) -> None:
        """Append encrypted WAL frames to message_1.db-wal without modifying main .db."""
        tbl = "Msg_" + hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        base_plain = self._plain_dbs[rel]

        def mutate(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{tbl}" (
                    local_id INTEGER PRIMARY KEY,
                    server_id INTEGER,
                    local_type INTEGER,
                    sort_seq INTEGER,
                    real_sender_id INTEGER,
                    create_time INTEGER,
                    message_content BLOB,
                    compress_content BLOB,
                    packed_info_data BLOB,
                    source TEXT
                )
                """
            )
            for local_id, local_type, sort_seq, sender_rowid, create_time, content in rows:
                conn.execute(
                    f'INSERT INTO "{tbl}" VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, "")',
                    (local_id, 5000 + local_id, local_type, sort_seq, sender_rowid, create_time, content),
                )

        updated = modify_reserve80_sqlite(base_plain, mutate)
        self._plain_dbs[rel] = updated
        wal_path = Path(str(self.storage_dir / rel) + "-wal")
        if wal_salt is None and rel in self._wal_salts and wal_path.is_file():
            salt_to_use = self._wal_salts[rel]
            existing = wal_path.read_bytes()
            frame_start = max(1, (len(existing) - WAL_HEADER_SIZE) // 4120 + 1)
            extra = build_encrypted_wal_frames(
                updated,
                base_plain,
                self._db_keys[rel],
                db_salt=self._salts[rel],
                wal_salt=salt_to_use,
                frame_seq_start=frame_start,
            )
            wal_bytes = existing + extra
        else:
            salt_to_use = wal_salt or b"\xaa\xbb\xcc\xdd\x11\x22\x33\x44"
            self._wal_salts[rel] = salt_to_use
            wal_bytes = build_encrypted_wal(
                updated,
                base_plain,
                self._db_keys[rel],
                db_salt=self._salts[rel],
                wal_salt=salt_to_use,
            )
        wal_path.write_bytes(wal_bytes)
        os.utime(wal_path, None)

    def add_new_contact_and_session(self, chat_id: str, nick_name: str, now_ts: int) -> None:
        """Simulate a brand-new contact & session arriving via WAL after watch baseline."""
        def mut_contact(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO contact (username, nick_name, remark, alias) VALUES (?, ?, '', '')",
                (chat_id, nick_name),
            )
        upd_c = modify_reserve80_sqlite(self._plain_dbs["contact/contact.db"], mut_contact)
        self._plain_dbs["contact/contact.db"] = upd_c
        wal_c = build_encrypted_wal(
            upd_c, None, self._db_keys["contact/contact.db"],
            db_salt=self._salts["contact/contact.db"],
            wal_salt=b"\x01\x02\x03\x04\x05\x06\x07\x08",
        )
        Path(str(self.storage_dir / "contact/contact.db") + "-wal").write_bytes(wal_c)

        def mut_sess(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO SessionTable VALUES (?, 1, 'New chat', ?, ?, ?, ?, 0)",
                (chat_id, now_ts, chat_id, nick_name, now_ts),
            )
        upd_s = modify_reserve80_sqlite(self._plain_dbs["session/session.db"], mut_sess)
        self._plain_dbs["session/session.db"] = upd_s
        wal_s = build_encrypted_wal(
            upd_s, None, self._db_keys["session/session.db"],
            db_salt=self._salts["session/session.db"],
            wal_salt=b"\x09\x08\x07\x06\x05\x04\x03\x02",
        )
        Path(str(self.storage_dir / "session/session.db") + "-wal").write_bytes(wal_s)
