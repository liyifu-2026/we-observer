"""Tests for real-time monitoring, WAL wakeups, multi-cursor isolation, and burst atomicity."""

from __future__ import annotations

import hashlib
import threading
import time

from tests.fixtures import SyntheticAccountEnv
from wx_observer.models import AccountInfo, WatchFilter
from wx_observer.monitor import RealtimeMonitor
from wx_observer.repository import AccountRepository
from wx_observer.state import CursorStateStore


def _build_monitor(tmp_path) -> tuple[SyntheticAccountEnv, AccountRepository, CursorStateStore, RealtimeMonitor]:
    env = SyntheticAccountEnv(tmp_path / "wx")
    acct = AccountInfo(
        account_id=env.account_id,
        wxid=env.own_wxid,
        data_dir=str(env.db_dir / env.account_id),
        db_storage_dir=str(env.storage_dir),
        online=False,
        pids=[],
    )
    state_dir = tmp_path / "state"
    repo = AccountRepository(
        acct,
        state_dir=state_dir,
        keys_file=env.keys_file,
        temp_root=tmp_path / "pool_tmp",
    )
    store = CursorStateStore(state_dir=state_dir)
    mon = RealtimeMonitor(repo, store)
    return env, repo, store, mon


def test_baseline_zero_flood_wal_delivery_and_restart_persistence(tmp_path) -> None:
    env, repo, store, mon = _build_monitor(tmp_path)
    try:
        # First poll establishes baseline and emits zero historical messages
        assert mon.poll(cursor_id="agent-a") == []
        assert mon.get_status(cursor_id="agent-a").initialized is True

        # Append live messages via WAL: one from Alice, one from Self, one in Group
        now = int(time.time())
        env.append_wal_messages(
            env.alice_id,
            [
                (10, 1, 600, 3, now, "Live Alice message"),
                (11, 1, 610, 2, now + 1, "Live Self message"),
            ],
        )
        env.append_wal_messages(
            env.room_id,
            [(10, 1, 620, 1, now + 2, f"{env.bob_id}:\nLive Bob group message")],
        )

        # Default watch excludes self (include_self=False)
        events_a = mon.poll(cursor_id="agent-a")
        assert [(e.chat_id, e.sort_seq, e.text) for e in events_a] == [
            (env.alice_id, 600, "Live Alice message"),
            (env.room_id, 620, "Live Bob group message"),
        ]
        # Second poll returns empty (no duplicates)
        assert mon.poll(cursor_id="agent-a") == []
    finally:
        repo.close()

    # Simulate restart: create fresh repo & monitor pointing to same state_dir
    acct2 = AccountInfo(
        account_id=env.account_id,
        wxid=env.own_wxid,
        data_dir=str(env.db_dir / env.account_id),
        db_storage_dir=str(env.storage_dir),
        online=False,
        pids=[],
    )
    repo2 = AccountRepository(
        acct2,
        state_dir=tmp_path / "state",
        keys_file=env.keys_file,
        temp_root=tmp_path / "pool_tmp2",
    )
    mon2 = RealtimeMonitor(repo2, CursorStateStore(state_dir=tmp_path / "state"))
    try:
        # Must NOT replay messages already consumed by agent-a before restart!
        assert mon2.poll(cursor_id="agent-a") == []
    finally:
        repo2.close()


def test_independent_cursors_filters_and_new_chat_arrival(tmp_path) -> None:
    env, repo, _store, mon = _build_monitor(tmp_path)
    try:
        mon.configure_watch(
            cursor_id="dm-only",
            watch_filter=WatchFilter(mode="private", include_self=False),
        )
        mon.configure_watch(
            cursor_id="group-with-self",
            watch_filter=WatchFilter(mode="group", include_self=True),
        )
        # Initialize baselines for both cursors
        assert mon.poll(cursor_id="dm-only") == []
        assert mon.poll(cursor_id="group-with-self") == []

        now = int(time.time())
        env.append_wal_messages(
            env.alice_id,
            [(10, 1, 700, 3, now, "DM ping")],
        )
        env.append_wal_messages(
            env.room_id,
            [(10, 1, 710, 2, now + 1, f"{env.own_wxid}:\nSelf group echo")],
        )

        dm_events = mon.poll(cursor_id="dm-only")
        assert [(e.chat_id, e.text) for e in dm_events] == [(env.alice_id, "DM ping")]

        # group-with-self cursor was NOT disturbed by dm-only poll!
        grp_events = mon.poll(cursor_id="group-with-self")
        assert [(e.chat_id, e.is_self, e.text) for e in grp_events] == [
            (env.room_id, True, "Self group echo")
        ]

        # Brand-new chat created AFTER baseline initialization
        new_friend = "wxid_newfriend77"
        env.add_new_contact_and_session(new_friend, "NewFriend", now + 5)
        env.append_wal_messages(
            new_friend,
            [(1, 1, 10, 3, now + 6, "First message from new friend")],
        )
        # Note: in message_1.db rowid 3=alice; add Name2Id row for new_friend or keep sender check flexible
        new_events = mon.poll(cursor_id="dm-only")
        assert len(new_events) == 1
        assert new_events[0].chat_id == new_friend
        assert new_events[0].text == "First message from new friend"
    finally:
        repo.close()


def test_sort_seq_group_atomicity_and_wait_for_messages(tmp_path) -> None:
    env, repo, _store, mon = _build_monitor(tmp_path)
    try:
        assert mon.poll(cursor_id="burst") == []

        now = int(time.time())
        # Insert 3 messages sharing identical sort_seq=800 plus 1 at sort_seq=801
        env.append_wal_messages(
            env.alice_id,
            [
                (20, 1, 800, 3, now, "burst-1"),
                (21, 1, 800, 3, now, "burst-2"),
                (22, 1, 800, 3, now, "burst-3"),
                (23, 1, 801, 3, now + 1, "after-burst"),
            ],
        )

        # Request limit=2: must NOT split sort_seq=800 across batches; delivers whole group of 3!
        batch1 = mon.poll(cursor_id="burst", limit=2)
        assert [e.text for e in batch1] == ["burst-1", "burst-2", "burst-3"]
        batch2 = mon.poll(cursor_id="burst", limit=2)
        assert [e.text for e in batch2] == ["after-burst"]

        # Test wait_for_messages: wakes up when WAL updates asynchronously, returns [] on timeout
        empty = mon.wait_for_messages(cursor_id="burst", timeout_seconds=0.3, poll_interval=0.2)
        assert empty == []

        def delayed_write():
            time.sleep(0.35)
            env.append_wal_messages(
                env.alice_id,
                [(24, 1, 900, 3, int(time.time()), "async-wakeup")],
            )

        t = threading.Thread(target=delayed_write, daemon=True)
        t.start()
        woke = mon.wait_for_messages(cursor_id="burst", timeout_seconds=3.0, poll_interval=0.2)
        t.join(timeout=2.0)
        assert [e.text for e in woke] == ["async-wakeup"]
    finally:
        repo.close()
