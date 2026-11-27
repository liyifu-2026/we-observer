"""Tests for multi-shard AccountRepository queries, Name2Id isolation, and fuzzy lookup."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.fixtures import SyntheticAccountEnv
from wx_observer.exceptions import AmbiguousChatError, ChatNotFoundError
from wx_observer.models import AccountInfo
from wx_observer.repository import AccountRepository


def _make_repo(tmp_path) -> tuple[SyntheticAccountEnv, AccountRepository]:
    env = SyntheticAccountEnv(tmp_path / "wx")
    acct = AccountInfo(
        account_id=env.account_id,
        wxid=env.own_wxid,
        data_dir=str(env.db_dir / env.account_id),
        db_storage_dir=str(env.storage_dir),
        online=False,
        pids=[],
    )
    repo = AccountRepository(
        acct,
        state_dir=tmp_path / "state",
        keys_file=env.keys_file,
        temp_root=tmp_path / "pool_tmp",
    )
    return env, repo


def test_list_chats_and_fail_closed_resolve(tmp_path) -> None:
    env, repo = _make_repo(tmp_path)
    try:
        all_chats = repo.list_chats(chat_type="all")
        assert [c.chat_id for c in all_chats] == [env.room_id, env.alice_id]

        groups = repo.list_chats(chat_type="group")
        assert len(groups) == 1 and groups[0].chat_id == env.room_id

        privates = repo.list_chats(chat_type="private")
        assert len(privates) == 1 and privates[0].chat_id == env.alice_id

        # Fuzzy lookup by remark / group name
        assert repo.resolve_chat("AliceRemark").chat_id == env.alice_id
        assert repo.resolve_chat("Project Alpha").chat_id == env.room_id

        # Ambiguous fuzzy match fails closed
        with pytest.raises(AmbiguousChatError) as exc_info:
            repo.resolve_chat("SameLabel")
        assert len(exc_info.value.details["candidates"]) == 2

        with pytest.raises(ChatNotFoundError):
            repo.resolve_chat("nonexistent_chat_xyz")
    finally:
        repo.close()


def test_cross_shard_merging_and_per_shard_name2id_isolation(tmp_path) -> None:
    """Prove shard 0 and shard 1 resolve real_sender_id=2 to DIFFERENT users accurately."""
    env, repo = _make_repo(tmp_path)
    try:
        alice_msgs = repo.get_messages(env.alice_id, limit=20)["messages"]
        assert [m.sort_seq for m in alice_msgs] == [100, 200, 300, 400, 500]
        # In shard 0, sender_id=2 is Alice (100, 300), sender_id=1 is Self (200)
        assert alice_msgs[0].sender_id == env.alice_id
        assert alice_msgs[0].is_self is False
        assert alice_msgs[1].sender_id == env.own_wxid
        assert alice_msgs[1].is_self is True
        assert "中文测试" in alice_msgs[2].text
        # Quote reply in shard 1
        assert alice_msgs[3].quote is not None
        assert alice_msgs[3].quote.excerpt == "Hello from Alice (shard 0)"
        # Image in shard 1
        assert alice_msgs[4].msg_type == "image"

        # Group messages across shard 0 and shard 1:
        # shard 0: rowid 2=Alice, rowid 3=Bob; shard 1: rowid 1=Bob, rowid 2=Self!
        group_msgs = repo.get_messages(env.room_id, limit=20)["messages"]
        assert [m.sort_seq for m in group_msgs] == [150, 250, 350, 450]
        assert [(m.sender_id, m.is_self) for m in group_msgs] == [
            (env.alice_id, False),
            (env.bob_id, False),
            (env.bob_id, False),
            (env.own_wxid, True),
        ]
        assert group_msgs[0].text == "Morning team!"
        assert group_msgs[1].msg_type == "file"
        assert group_msgs[1].metadata["filename"] == "spec.pdf"
        assert group_msgs[2].text == "Group msg in shard 1"
        assert group_msgs[3].text == "Self msg in shard 1"

        # Filters: include_self=False, msg_types=["quote"], keyword on zstd compressed text
        no_self = repo.get_messages(env.room_id, include_self=False)["messages"]
        assert all(not m.is_self for m in no_self) and len(no_self) == 3

        quotes = repo.get_messages(env.alice_id, msg_types=["quote"])["messages"]
        assert [m.sort_seq for m in quotes] == [400]

        zstd_hits = repo.search_messages("中文测试")
        assert len(zstd_hits) == 1 and zstd_hits[0].sort_seq == 300

        # Pagination via before_seq
        page1 = repo.get_messages(env.alice_id, limit=2)
        assert [m.sort_seq for m in page1["messages"]] == [400, 500]
        assert page1["has_more"] is True
        page2 = repo.get_messages(env.alice_id, limit=2, before_seq=page1["next_before_seq"])
        assert [m.sort_seq for m in page2["messages"]] == [200, 300]
    finally:
        repo.close()


def test_dynamic_new_shard_pickup(tmp_path) -> None:
    env, repo = _make_repo(tmp_path)
    try:
        # Dynamically add message_2.db with a new message for Alice
        alice_tbl = "Msg_" + hashlib.md5(env.alice_id.encode("utf-8")).hexdigest()

        from tests.fixtures import create_reserve80_sqlite

        def pop_msg2(conn):
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (9, ?)", (env.alice_id,))
            conn.execute(
                f"""
                CREATE TABLE "{alice_tbl}" (
                    local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
                    sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
                    message_content BLOB, compress_content BLOB, packed_info_data BLOB, source TEXT
                )
                """
            )
            conn.execute(
                f'INSERT INTO "{alice_tbl}" VALUES (1, 9999, 1, 900, 9, ?, "Shard 2 dynamic row", NULL, NULL, "")',
                (env.base_ts + 90,),
            )

        env._write_encrypted_db("message/message_2.db", create_reserve80_sqlite(pop_msg2), b"msg2-salt")
        env.keys_file.write_text(json.dumps({"keys": env.keys_hex}), encoding="utf-8")

        msgs = repo.get_messages(env.alice_id, limit=10)["messages"]
        assert msgs[-1].sort_seq == 900
        assert msgs[-1].text == "Shard 2 dynamic row"
    finally:
        repo.close()
