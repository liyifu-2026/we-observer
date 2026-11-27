"""Tests for SQLCipher 4 verification, 32B/48B decryption, WAL merging, and cleanup."""

from __future__ import annotations

import hashlib
import sqlite3
import struct

import pytest

from tests.fixtures import (
    build_encrypted_wal,
    create_reserve80_sqlite,
    encrypt_sqlite_image,
    modify_reserve80_sqlite,
)
from wx_observer.cipher_store import (
    EncryptedDatabasePool,
    PAGE_SIZE,
    WAL_HEADER_SIZE,
    verify_page1_hmac,
)
from wx_observer.exceptions import DatabaseCorruptError, KeyUnavailableError


def _make_sample_db() -> bytes:
    def pop(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
        conn.executemany(
            "INSERT INTO items VALUES (?, ?)",
            [(i, f"row-{i}-" + ("x" * 60)) for i in range(1, 50)],
        )

    return create_reserve80_sqlite(pop)


def test_standard_32b_decrypt_and_wrong_key_rejection(tmp_path) -> None:
    plain = _make_sample_db()
    key = bytes.fromhex("ab" * 32)
    wrong_key = bytes.fromhex("cd" * 32)
    salt = b"0123456789abcdef"
    enc = encrypt_sqlite_image(plain, key, salt=salt, plaintext_header=False)

    assert verify_page1_hmac(key, enc[:PAGE_SIZE]) is True
    assert verify_page1_hmac(wrong_key, enc[:PAGE_SIZE]) is False

    db_path = tmp_path / "sample.db"
    db_path.write_bytes(enc)

    pool = EncryptedDatabasePool(temp_root=tmp_path / "tmp_pool")
    sess_dir = pool.session_dir
    assert sess_dir.is_dir()
    pool.register_databases({"sample.db": db_path}, {"sample.db": key})
    conn = pool.open_readonly("sample.db")
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert cnt == 49
        assert conn.execute("SELECT label FROM items WHERE id=1").fetchone()[0].startswith("row-1-")
    finally:
        conn.close()

    pool.cleanup()
    assert not sess_dir.exists()


def test_plaintext_header_48b_decrypt(tmp_path) -> None:
    plain = _make_sample_db()
    enc_key = bytes.fromhex("ef" * 32)
    explicit_salt = b"ExplicitSalt1234"
    enc = encrypt_sqlite_image(plain, enc_key, salt=explicit_salt, plaintext_header=True)
    # Standard 32B check must NOT pass because header is SQLite format 3\x00 rather than salt
    assert verify_page1_hmac(enc_key, enc[:PAGE_SIZE]) is False
    assert verify_page1_hmac(enc_key + explicit_salt, enc[:PAGE_SIZE]) is True

    db_path = tmp_path / "pt.db"
    db_path.write_bytes(enc)

    pool = EncryptedDatabasePool(temp_root=tmp_path / "tmp_pool")
    try:
        pool.register_databases({"pt.db": db_path}, {"pt.db": enc_key + explicit_salt})
        conn = pool.open_readonly("pt.db")
        try:
            assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 49
        finally:
            conn.close()
    finally:
        pool.cleanup()


def test_wal_incremental_merge_and_salt_reset(tmp_path) -> None:
    plain_v1 = _make_sample_db()
    key = bytes.fromhex("42" * 32)
    salt = hashlib.sha256(b"wal-test").digest()[:16]
    enc_v1 = encrypt_sqlite_image(plain_v1, key, salt=salt)
    db_path = tmp_path / "waltest.db"
    db_path.write_bytes(enc_v1)

    # Modify in memory -> write WAL v1
    plain_v2 = modify_reserve80_sqlite(
        plain_v1,
        lambda c: c.execute("INSERT INTO items VALUES (999, 'live-wal-row')"),
    )
    wal_salt_1 = b"\x01\x01\x01\x01\x02\x02\x02\x02"
    wal_1 = build_encrypted_wal(plain_v2, plain_v1, key, db_salt=salt, wal_salt=wal_salt_1)
    wal_path = tmp_path / "waltest.db-wal"
    wal_path.write_bytes(wal_1)

    pool = EncryptedDatabasePool(temp_root=tmp_path / "tmp_pool")
    try:
        pool.register_databases({"waltest.db": db_path}, {"waltest.db": key})
        conn = pool.open_readonly("waltest.db")
        try:
            assert conn.execute("SELECT label FROM items WHERE id=999").fetchone()[0] == "live-wal-row"
        finally:
            conn.close()

        # Simulate WAL reset (new salt + stale old-salt frame appended at tail)
        plain_v3 = modify_reserve80_sqlite(
            plain_v2,
            lambda c: c.execute("INSERT INTO items VALUES (1000, 'post-reset-row')"),
        )
        wal_salt_2 = b"\x09\x09\x09\x09\x08\x08\x08\x08"
        wal_2_valid = build_encrypted_wal(
            plain_v3, plain_v1, key, db_salt=salt, wal_salt=wal_salt_2
        )
        # Append stale frame from wal_1 (with old salt) at tail: must be ignored!
        stale_tail = wal_1[WAL_HEADER_SIZE:]
        wal_path.write_bytes(wal_2_valid + stale_tail)

        conn2 = pool.open_readonly("waltest.db")
        try:
            assert conn2.execute("SELECT label FROM items WHERE id=1000").fetchone()[0] == "post-reset-row"
            assert conn2.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn2.close()
    finally:
        pool.cleanup()


def test_corrupt_main_db_raises_and_missing_key_fails_closed(tmp_path) -> None:
    bad_db = tmp_path / "bad.db"
    bad_db.write_bytes(b"\x00" * (PAGE_SIZE * 2))
    key = bytes.fromhex("11" * 32)

    pool = EncryptedDatabasePool(temp_root=tmp_path / "tmp_pool")
    try:
        pool.register_databases({"bad.db": bad_db}, {})
        with pytest.raises(KeyUnavailableError):
            pool.open_readonly("bad.db")

        pool.register_databases({"bad.db": bad_db}, {"bad.db": key})
        with pytest.raises(DatabaseCorruptError):
            pool.open_readonly("bad.db")
    finally:
        pool.cleanup()
