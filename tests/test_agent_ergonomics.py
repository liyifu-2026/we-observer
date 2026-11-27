"""Offline unit and integration tests for v0.2.0 agent-friendly ergonomics."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.fixtures import SyntheticAccountEnv, make_file_xml, make_link_xml, make_quote_xml
import wx_observer.attachments as attachments_mod
from wx_observer.attachments import build_file_index, resolve_local_file
from wx_observer.cipher_store import EncryptedDatabasePool, quick_check_sqlite
from wx_observer.cli import main as cli_main
from wx_observer.exceptions import WxObserverError
from wx_observer.models import ChatInfo
from wx_observer.service import ObserverService, parse_timestamp


def test_compact_shaping_truncation_noise_and_tokens(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    long_text = "A" * 200 + " 中间核心内容 " + "B" * 200
    env.append_wal_messages(
        env.alice_id,
        [
            (30, 1, 600, 3, now - 30, long_text),
            (31, 47, 610, 3, now - 20, '<msg><emoji md5="abcd" /></msg>'),
            (32, 10000, 620, 3, now - 10, "<sysmsg><revokemsg><replacemsg>撤回了一条消息</replacemsg></revokemsg></sysmsg>"),
        ],
    )

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        compact = service.get_messages(
            "AliceRemark",
            limit=20,
            detail="compact",
            max_text_chars=80,
            skip_noise=True,
            account=env.account_id,
        )
        assert compact["ok"] is True
        assert compact["detail"] == "compact"
        assert compact["skipped_noise"] == 2
        assert compact["est_tokens"] > 0

        long_items = [m for m in compact["messages"] if m["seq"] == 600]
        assert len(long_items) == 1
        assert long_items[0]["truncated"] is True
        assert long_items[0]["full_length"] == len(long_text)
        assert len(long_items[0]["text"]) <= 83
        assert "t" in long_items[0] and "sender" in long_items[0] and "type" in long_items[0]
        assert "local_id" not in long_items[0]

        full = service.get_messages(
            "AliceRemark",
            limit=20,
            detail="full",
            max_text_chars=None,
            skip_noise=False,
            account=env.account_id,
        )
        assert full["detail"] == "full"
        assert full["skipped_noise"] == 0
        assert full["est_tokens"] > compact["est_tokens"]
        full_long = [m for m in full["messages"] if m["sort_seq"] == 600][0]
        assert full_long["text"] == long_text
    finally:
        service.close()


def test_search_snippets_and_relative_timestamps(tmp_path) -> None:
    now = int(time.time())
    ts_24h = parse_timestamp("24h")
    assert ts_24h is not None
    assert abs((now - ts_24h) - 86400) <= 5
    ts_7d = parse_timestamp("7d")
    assert ts_7d is not None and abs((now - ts_7d) - 7 * 86400) <= 5
    with pytest.raises(WxObserverError):
        parse_timestamp("not-a-timestamp")

    env = SyntheticAccountEnv(tmp_path / "wx")
    haystack = ("prefix padding " * 18) + "CRITICAL_KEYWORD_TARGET" + (" suffix padding" * 18)
    env.append_wal_messages(
        env.alice_id,
        [(35, 1, 700, 3, now - 60, haystack)],
    )

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        res = service.search_messages(
            "CRITICAL_KEYWORD_TARGET",
            since="2h",
            detail="compact",
            max_text_chars=100,
            account=env.account_id,
        )
        assert res["count"] == 1
        assert res["est_tokens"] > 0
        hit = res["messages"][0]
        assert "CRITICAL_KEYWORD_TARGET" in hit["text"]
        assert hit["text"].startswith("...")
        assert hit["truncated"] is True
        assert hit["match_offset"] > 0

        # Negative window check: since=10s excludes 60s-old message
        empty = service.search_messages(
            "CRITICAL_KEYWORD_TARGET",
            since="10s",
            account=env.account_id,
        )
        assert empty["count"] == 0
    finally:
        service.close()


def test_stateless_multi_account_selection_and_isolation(tmp_path) -> None:
    root = tmp_path / "shared_wx"
    env_a = SyntheticAccountEnv(root, account_id="wxid_acct_alpha_1111")
    env_b = SyntheticAccountEnv(root, account_id="wxid_acct_beta_2222")
    env_b.append_wal_messages(
        env_b.alice_id,
        [(30, 1, 800, 3, int(time.time()), "Only on Beta account")],
    )
    master_keys = tmp_path / "master_keys.json"
    master_keys.write_text(json.dumps({"master_key": "11" * 32}), encoding="utf-8")

    service = ObserverService(
        db_dir=str(env_a.db_dir),
        keys_file=str(master_keys),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        sel = service.select_account(account=env_a.account_id)
        assert sel["account"]["account_id"] == env_a.account_id

        # Explicitly reading Beta must NOT mutate the process default selected account (Alpha)
        beta_hits = service.search_messages("Only on Beta account", account=env_b.account_id)
        assert beta_hits["account_id"] == env_b.account_id
        assert beta_hits["count"] == 1

        status_default = service.get_status()
        assert status_default["status"]["account_id"] == env_a.account_id

        alpha_hits = service.search_messages("Only on Beta account")
        assert alpha_hits["account_id"] == env_a.account_id
        assert alpha_hits["count"] == 0
    finally:
        service.close()


def test_peek_ack_at_least_once_and_invalid_token_negative(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        service.configure_watch(cursor_id="agent-c1", mode="private", account=env.account_id)
        base = service.poll_new_messages(cursor_id="agent-c1", peek=True, account=env.account_id)
        assert base["initialized"] is True and base["count"] == 0

        env.append_wal_messages(
            env.alice_id,
            [
                (40, 1, 900, 3, now - 2, "First pending msg"),
                (41, 1, 910, 3, now - 1, "Second pending msg"),
            ],
        )

        p1 = service.poll_new_messages(
            cursor_id="agent-c1", peek=True, detail="compact", account=env.account_id
        )
        assert p1["peek"] is True
        assert p1["count"] == 2
        assert p1["ack_token"]

        # Without ack, another poll still re-delivers the exact un-committed batch
        p2 = service.poll_new_messages(
            cursor_id="agent-c1", peek=True, detail="compact", account=env.account_id
        )
        assert p2["count"] == 2

        # Wrong cursor_id or unknown token must fail closed
        with pytest.raises(WxObserverError) as exc_wrong_cursor:
            service.ack_messages(
                cursor_id="other-cursor", ack_token=p1["ack_token"], account=env.account_id
            )
        assert exc_wrong_cursor.value.code == "invalid_ack_token"

        with pytest.raises(WxObserverError) as exc_bad_tok:
            service.ack_messages(
                cursor_id="agent-c1", ack_token="missing-token-xyz", account=env.account_id
            )
        assert exc_bad_tok.value.code == "invalid_ack_token"

        # Partial ack up to first message leaves second message available
        partial = service.ack_messages(
            cursor_id="agent-c1",
            upto={env.alice_id: 900},
            account=env.account_id,
        )
        assert partial["ok"] is True

        p3 = service.poll_new_messages(
            cursor_id="agent-c1", peek=True, detail="compact", account=env.account_id
        )
        assert p3["count"] == 1
        assert p3["messages"][0]["text"] == "Second pending msg"

        # Full ack via token drains cursor completely
        ack_res = service.ack_messages(
            cursor_id="agent-c1", ack_token=p3["ack_token"], account=env.account_id
        )
        assert ack_res["ok"] is True
        assert ack_res["committed_chats"] == 1
        assert ack_res["committed"] == {env.alice_id: 910}
        p4 = service.poll_new_messages(cursor_id="agent-c1", peek=True, account=env.account_id)
        assert p4["count"] == 0
    finally:
        service.close()


def test_digest_cross_chat_reduction_and_filters(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    month_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / "spec.pdf").write_bytes(b"X" * 12345)

    env.append_wal_messages(
        env.room_id,
        [
            (10, 49, 500, 3, now - 120, f"{env.alice_id}:\n{make_link_xml('Release Notes', 'v0.2', 'https://example.com/release')}"),
            (11, 1, 510, 1, now - 60, f"{env.bob_id}:\n@AliceReal check https://docs.example.org/guide please"),
            (12, 47, 520, 1, now - 30, '<msg><emoji md5="eeee" /></msg>'),
        ],
    )

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        dig = service.digest(scope="all", since="24h", buckets="hour", account=env.account_id)
        assert dig["ok"] is True
        assert dig["est_tokens"] > 0
        summary = dig["summary"]
        assert summary["active_chats"] >= 2
        assert summary["noise_messages"] >= 1
        assert summary["links_count"] >= 2
        assert summary["attachments_count"] >= 1
        assert summary["mentions_count"] >= 1
        assert dig["top_senders"]
        assert any(a["filename"] == "spec.pdf" and a["available"] for a in dig["attachments"])
        assert any("AliceReal" in m["targets"] for m in dig["mentions"])

        room_only = service.digest(scope="group", since="24h", buckets="day", account=env.account_id)
        assert all(c["chat_type"] == "group" for c in room_only["per_chat"])

        with pytest.raises(WxObserverError) as exc_bad:
            service.digest(buckets="weekly", account=env.account_id)
        assert exc_bad.value.code == "invalid_buckets"
    finally:
        service.close()


def test_list_attachments_resolution_suffix_forwarded_and_path_traversal(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    month_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    # Duplicate suffix match spec(1).pdf for spec.pdf (size=12345)
    (month_dir / "spec(1).pdf").write_bytes(b"P" * 12345)

    # Forwarded record embedded file with matching MD5
    doc_bytes = b"DOCX_PAYLOAD_" * 50
    doc_md5 = hashlib.md5(doc_bytes).hexdigest()
    (month_dir / "report.docx").write_bytes(doc_bytes)

    # Outside file for path traversal negative check
    outside_evil = env.db_dir / env.account_id / "msg" / "evil.txt"
    outside_evil.write_text("SECRET", encoding="utf-8")

    fwd_xml = (
        "<msg><appmsg><type>19</type><title>Forwarded Bundle</title><recorditem><![CDATA["
        "<recordinfo><datalist>"
        f'<dataitem datatype="8"><datatitle>report.docx</datatitle><datasize>{len(doc_bytes)}</datasize>'
        f"<datafmt>docx</datafmt><fullmd5>{doc_md5}</fullmd5></dataitem>"
        "</datalist></recordinfo>]]></recorditem></appmsg></msg>"
    )
    env.append_wal_messages(
        env.alice_id,
        [
            (50, 49, 950, 3, now - 40, fwd_xml),
            (51, 49, 960, 3, now - 30, make_file_xml("ghost.zip", 9999, ext="zip")),
            (52, 49, 970, 3, now - 20, make_file_xml(r"..\..\evil.txt", 6, ext="txt")),
        ],
    )

    # Direct unit test of path traversal guard
    trav = resolve_local_file(
        env.db_dir / env.account_id,
        r"..\..\evil.txt",
        now,
        expected_size=6,
    )
    assert trav["available"] is False
    assert trav["local_path"] is None

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        all_att = service.list_attachments(scope="all", since="7d", account=env.account_id)
        assert all_att["ok"] is True
        by_name = {item["filename"]: item for item in all_att["attachments"]}
        assert by_name["spec.pdf"]["available"] is True
        assert by_name["spec.pdf"]["match_confidence"] == "suffix_and_size"
        assert by_name["spec.pdf"]["local_path"].endswith("spec(1).pdf")

        assert by_name["report.docx"]["available"] is True
        assert by_name["report.docx"]["source_type"] == "forwarded_record"
        assert by_name["report.docx"]["match_confidence"] == "md5"

        assert by_name["ghost.zip"]["available"] is False
        assert by_name["ghost.zip"]["local_path"] is None

        assert by_name[r"..\..\evil.txt"]["available"] is False
        assert by_name[r"..\..\evil.txt"]["local_path"] is None

        only_pdf = service.list_attachments(
            extensions=["pdf"], only_available=True, since="7d", account=env.account_id
        )
        assert [x["filename"] for x in only_pdf["attachments"]] == ["spec.pdf"]
    finally:
        service.close()


def test_concurrent_queries_no_deadlock_and_cli_commands(tmp_path, capsys) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    month_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / "spec.pdf").write_bytes(b"Z" * 12345)

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        service.configure_watch(cursor_id="c1", account=env.account_id)
        service.configure_watch(cursor_id="c2", account=env.account_id)

        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def run_worker(kind: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                if kind == 0:
                    res = service.get_messages("AliceRemark", detail="compact", account=env.account_id)
                    assert res["ok"] is True
                elif kind == 1:
                    res = service.search_messages("Alice", detail="compact", account=env.account_id)
                    assert res["ok"] is True
                elif kind == 2:
                    res = service.poll_new_messages(cursor_id="c1", peek=True, account=env.account_id)
                    assert res["ok"] is True
                else:
                    res = service.digest(scope="all", since="24h", account=env.account_id)
                    assert res["ok"] is True
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(run_worker, range(4)))
        assert not errors
    finally:
        service.close()

    # CLI subcommands end-to-end
    common = ["--db-dir", str(env.db_dir), "--keys-file", str(env.keys_file), "--state-dir", str(tmp_path / "cli_state")]
    rc1 = cli_main([*common, "history", "AliceRemark", "--compact", "--skip-noise", "--json"])
    assert rc1 == 0
    hist_json = json.loads(capsys.readouterr().out)
    assert hist_json["detail"] == "compact"
    assert hist_json["est_tokens"] > 0

    rc2 = cli_main([*common, "digest", "--since", "24h", "--json"])
    assert rc2 == 0
    dig_json = json.loads(capsys.readouterr().out)
    assert dig_json["ok"] is True and dig_json["summary"]["total_messages"] >= 9

    rc3 = cli_main([*common, "attachments", "--only-available", "--json"])
    assert rc3 == 0
    att_json = json.loads(capsys.readouterr().out)
    assert att_json["available_count"] == 1


def test_shard_table_index_and_sql_type_pushdown(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        sel = service.select_account(account=env.account_id)
        assert sel["ok"] is True
        repo = service._repos[(env.account_id, None)]

        # include_non_session=False includes hidden sessions (bob_id) but excludes non-session contacts
        sess_only = repo.list_chats(include_hidden=True, include_non_session=False, limit=100)
        sess_ids = {c.chat_id for c in sess_only}
        assert env.bob_id in sess_ids
        assert "wxid_duplicate1" not in sess_ids

        with_contacts = repo.list_chats(include_hidden=True, include_non_session=True, limit=100)
        assert "wxid_duplicate1" in {c.chat_id for c in with_contacts}

        # Warm shard table index via initial read
        room_files = repo.get_messages(env.room_id, msg_types=["file"])
        assert len(room_files["messages"]) == 1
        assert room_files["messages"][0].metadata["filename"] == "spec.pdf"

        # Wrap pool.open_readonly to count opens on subsequent reads
        orig_open = repo.pool.open_readonly
        opened_rels: list[str] = []

        def tracked_open(rel: str, *, force_rebuild: bool = False):
            opened_rels.append(rel)
            return orig_open(rel, force_rebuild=force_rebuild)

        repo.pool.open_readonly = tracked_open  # type: ignore[method-assign]
        try:
            # Contact with no Msg_ table must open ZERO message shards
            empty_contact = repo.get_messages("wxid_duplicate1")
            assert empty_contact["messages"] == []
            assert not any(r.startswith("message/") for r in opened_rels)

            # Time-window digest older than cutoff skips stale/zero-session contacts with 0 message opens
            opened_rels.clear()
            recent = service.digest(scope="all", since="5m", account=env.account_id)
            assert recent["summary"]["active_chats"] == 0
            assert not any(r.startswith("message/") for r in opened_rels)
        finally:
            repo.pool.open_readonly = orig_open  # type: ignore[method-assign]
    finally:
        service.close()


def test_64bit_subtype_sql_pushdown_and_file_index(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    month_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    exact_bytes = b"EXACT_BYTES_64BIT"
    exact_md5 = hashlib.md5(exact_bytes).hexdigest()
    (month_dir / "manual64.pdf").write_bytes(exact_bytes)
    (month_dir / "spec(1).pdf").write_bytes(b"S" * 12345)
    (env.db_dir / env.account_id / "msg" / "evil.txt").write_text("NO", encoding="utf-8")

    # Insert 64-bit local_type rows: file=(6<<32)|49 and quote=(57<<32)|49
    lt_file_64 = (6 << 32) | 49
    lt_quote_64 = (57 << 32) | 49
    file64_xml = (
        "<msg><appmsg><type>6</type><title>manual64.pdf</title>"
        f"<appattach><totallen>{len(exact_bytes)}</totallen><fileext>pdf</fileext>"
        f"<md5>{exact_md5}</md5></appattach></appmsg></msg>"
    )
    quote64_xml = make_quote_xml(
        "Quote64Reply",
        ref_type=1,
        ref_content="RefText",
        display_name="BobReal",
        from_usr=env.bob_id,
    )
    env.append_wal_messages(
        env.room_id,
        [
            (60, lt_file_64, 900, 3, now - 20, f"{env.alice_id}:\n{file64_xml}"),
            (61, lt_quote_64, 910, 1, now - 10, f"{env.bob_id}:\n{quote64_xml}"),
        ],
    )

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        # Both 32-bit legacy spec.pdf (type=49) and 64-bit manual64.pdf (type=(6<<32)|49) are returned;
        # 64-bit quote row (57<<32)|49 is excluded cleanly by SQL subtype pushdown.
        files = service.get_messages(
            env.room_id,
            msg_types=["file"],
            detail="compact",
            account=env.account_id,
        )
        names = [m.get("meta", {}).get("filename") for m in files["messages"]]
        assert names == ["spec.pdf", "manual64.pdf"]

        quotes = service.get_messages(
            env.room_id,
            msg_types=["quote"],
            detail="compact",
            account=env.account_id,
        )
        assert [m["text"] for m in quotes["messages"]] == ["Quote64Reply"]
    finally:
        service.close()

    # LocalFileIndex parity checks: exact MD5, duplicate suffix, and path-traversal rejection
    idx = build_file_index(env.db_dir / env.account_id)
    r_md5 = resolve_local_file(
        env.db_dir / env.account_id,
        "manual64.pdf",
        now,
        expected_size=len(exact_bytes),
        expected_md5=exact_md5,
        file_index=idx,
    )
    assert r_md5["available"] is True and r_md5["match_confidence"] == "md5"

    r_suf = resolve_local_file(
        env.db_dir / env.account_id,
        "spec.pdf",
        now,
        expected_size=12345,
        file_index=idx,
    )
    assert r_suf["available"] is True and r_suf["match_confidence"] == "suffix_and_size"

    r_evil = resolve_local_file(
        env.db_dir / env.account_id,
        r"..\..\evil.txt",
        now,
        expected_size=2,
        file_index=idx,
    )
    assert r_evil["available"] is False and r_evil["local_path"] is None


def test_get_messages_batch_single_pass_and_digest_budget(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    extra_rows = [
        (70 + i, 1, 950 + i, 3, now - (100 - i), f"High volume discussion point #{i} https://example.org/p/{i}")
        for i in range(6)
    ]
    env.append_wal_messages(env.room_id, extra_rows)

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        sel = service.select_account(account=env.account_id)
        assert sel["ok"] is True
        repo = service._repos[(env.account_id, None)]

        # Warm shard table index first
        chats = repo.list_chats(chat_type="all", limit=10, include_hidden=False)
        assert len(chats) == 2

        orig_open = repo.pool.open_readonly
        opened_rels: list[str] = []

        def tracked_open(rel: str, *, force_rebuild: bool = False):
            opened_rels.append(rel)
            return orig_open(rel, force_rebuild=force_rebuild)

        # Warm table cache cleanly via one batch call, then assert single-open-per-shard on repeated call
        first_batch = repo.get_messages_batch(chats, limit_per_chat=50)
        assert len(first_batch[env.alice_id]) == 5
        assert len(first_batch[env.room_id]) == 10

        repo.pool.open_readonly = tracked_open  # type: ignore[method-assign]
        try:
            opened_rels.clear()
            batch = repo.get_messages_batch(chats, limit_per_chat=50)
            assert len(batch[env.alice_id]) == 5
            assert len(batch[env.room_id]) == 10
            assert opened_rels.count("message/message_0.db") <= 1
            assert opened_rels.count("message/message_1.db") <= 1
        finally:
            repo.pool.open_readonly = orig_open  # type: ignore[method-assign]

        unbounded = service.digest(scope="all", since="24h", token_budget=0, account=env.account_id)
        assert unbounded["est_tokens"] > 500
        assert unbounded["summary"]["budget_trimmed"] is False

        capped = service.digest(
            scope="all",
            since="24h",
            max_chats=1,
            token_budget=350,
            account=env.account_id,
        )
        assert len(capped["per_chat"]) == 1
        assert capped["summary"]["omitted_chats"] >= 1
        assert capped["summary"]["budget_trimmed"] is True
        assert capped["summary"]["active_chats"] >= 2
        assert capped["summary"]["total_messages"] >= 15
        assert capped["est_tokens"] <= 400
    finally:
        service.close()


def test_cli_ui_removed_and_mcp_extra_error(tmp_path, monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit) as exc_ui:
        cli_main(["ui"])
    assert exc_ui.value.code != 0
    capsys.readouterr()

    monkeypatch.setitem(sys.modules, "wx_observer.mcp_server", None)
    rc = cli_main(["--state-dir", str(tmp_path / "cli_state"), "mcp", "--transport", "stdio"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "mcp_not_installed" in err
    assert ".[mcp]" in err


def test_attachments_wave_early_stop(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    month_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / "latest_wave.pdf").write_bytes(b"W" * 2048)
    env.append_wal_messages(
        env.room_id,
        [(80, (6 << 32) | 49, 999, 3, now - 5, f"{env.alice_id}:\n{make_file_xml('latest_wave.pdf', 2048, ext='pdf')}")],
    )

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        sel = service.select_account(account=env.account_id)
        assert sel["ok"] is True
        repo = service._repos[(env.account_id, None)]

        real_chats = repo.list_chats(chat_type="all", limit=10, include_hidden=True, include_non_session=False)
        filler_chats = [
            ChatInfo(
                chat_id=f"wxid_filler_{i}",
                chat_type="private",
                display_name=f"Filler {i}",
                last_timestamp=now - 7200 - i,
            )
            for i in range(40)
        ]

        orig_list = repo.list_chats
        orig_batch = repo.get_messages_batch
        wave_calls: list[int] = []

        def stub_list_chats(**_kwargs):
            return [*real_chats, *filler_chats]

        def spy_batch(chats, **kwargs):
            wave_calls.append(len(chats))
            return orig_batch(chats, **kwargs)

        repo.list_chats = stub_list_chats  # type: ignore[method-assign]
        repo.get_messages_batch = spy_batch  # type: ignore[method-assign]
        try:
            att = service.list_attachments(
                scope="all",
                since="24h",
                extensions=["pdf"],
                only_available=True,
                limit=1,
                account=env.account_id,
            )
        finally:
            repo.list_chats = orig_list  # type: ignore[method-assign]
            repo.get_messages_batch = orig_batch  # type: ignore[method-assign]

        assert len(wave_calls) == 1
        assert wave_calls[0] == 32
        assert att["count"] == 1
        assert att["available_count"] == 1
        assert att["attachments"][0]["filename"] == "latest_wave.pdf"
    finally:
        service.close()


def test_search_sql_prefilter_and_wave_exact_recall(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    quote_amp = make_quote_xml(
        "Check A & B spec",
        ref_type=1,
        ref_content="Spec",
        display_name="AliceRemark",
        from_usr=env.alice_id,
    )
    env.append_wal_messages(
        env.alice_id,
        [
            (70, 1, 910, 3, now - 30, "Build Status GREEN"),
            (71, 1, 920, 3, now - 20, "明天下午交作业"),
            (72, (57 << 32) | 49, 930, 3, now - 10, quote_amp),
        ],
    )

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        r_ascii = service.search_messages("build status green", since="24h", account=env.account_id)
        assert r_ascii["count"] == 1 and r_ascii["messages"][0]["text"] == "Build Status GREEN"

        r_cjk = service.search_messages("交作业", since="24h", account=env.account_id)
        assert r_cjk["count"] == 1 and "交作业" in r_cjk["messages"][0]["text"]

        r_zstd = service.search_messages("中文测试", since="24h", account=env.account_id)
        assert r_zstd["count"] == 1 and "中文测试" in r_zstd["messages"][0]["text"]

        r_amp = service.search_messages("A & B", since="24h", account=env.account_id)
        assert r_amp["count"] == 1 and "A & B" in r_amp["messages"][0]["text"]

        r_img = service.search_messages("[图片]", since="24h", account=env.account_id)
        assert r_img["count"] >= 1 and any(m["msg_type"] == "image" for m in r_img["messages"])

        # Wave early-stop across >32 chats
        repo = service._repos[(env.account_id, None)]
        real_chats = repo.list_chats(chat_type="all", limit=10, include_hidden=True, include_non_session=False)
        filler_chats = [
            ChatInfo(
                chat_id=f"wxid_search_filler_{i}",
                chat_type="private",
                display_name=f"SearchFiller {i}",
                last_timestamp=now - 7200 - i,
            )
            for i in range(40)
        ]
        orig_list = repo.list_chats
        orig_batch = repo.get_messages_batch
        wave_calls: list[int] = []

        def stub_list(**_kwargs):
            return [*real_chats, *filler_chats]

        def spy_batch(chats, **kwargs):
            wave_calls.append(len(chats))
            return orig_batch(chats, **kwargs)

        repo.list_chats = stub_list  # type: ignore[method-assign]
        repo.get_messages_batch = spy_batch  # type: ignore[method-assign]
        try:
            wave_res = service.search_messages("交作业", since="24h", limit=1, account=env.account_id)
        finally:
            repo.list_chats = orig_list  # type: ignore[method-assign]
            repo.get_messages_batch = orig_batch  # type: ignore[method-assign]

        assert len(wave_calls) == 1
        assert wave_calls[0] == 32
        assert wave_res["count"] == 1 and "交作业" in wave_res["messages"][0]["text"]
    finally:
        service.close()


def test_attachments_lazy_md5_only_hashes_top_limit(tmp_path, monkeypatch) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    now = int(time.time())
    month_dir = env.db_dir / env.account_id / "msg" / "file" / time.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for i in range(1, 6):
        fname = f"doc{i}.pdf"
        payload = (f"DOC_DATA_{i}_").encode("ascii") * 64
        md5_hex = hashlib.md5(payload).hexdigest()
        (month_dir / fname).write_bytes(payload)
        xml = (
            f"<msg><appmsg><type>6</type><title>{fname}</title>"
            f"<appattach><totallen>{len(payload)}</totallen><fileext>pdf</fileext>"
            f"<md5>{md5_hex}</md5></appattach></appmsg></msg>"
        )
        rows.append((90 + i, (6 << 32) | 49, 950 + i, 3, now - (60 - i * 10), f"{env.alice_id}:\n{xml}"))
    env.append_wal_messages(env.room_id, rows)

    service = ObserverService(
        db_dir=str(env.db_dir),
        keys_file=str(env.keys_file),
        state_dir=tmp_path / "state",
        temp_root=tmp_path / "pool",
    )
    try:
        orig_md5 = attachments_mod._file_md5
        hashed_paths: list[str] = []

        def spy_md5(path: Path):
            hashed_paths.append(str(path))
            return orig_md5(path)

        monkeypatch.setattr(attachments_mod, "_file_md5", spy_md5)
        attachments_mod._MD5_CACHE.clear()

        res_all = service.list_attachments(
            scope="all", since="24h", only_available=False, limit=2, account=env.account_id
        )
        assert res_all["count"] == 2
        assert [x["filename"] for x in res_all["attachments"]] == ["doc5.pdf", "doc4.pdf"]
        assert all(x["match_confidence"] == "md5" for x in res_all["attachments"])
        assert len(hashed_paths) <= 2

        hashed_paths.clear()
        attachments_mod._MD5_CACHE.clear()
        res_avail = service.list_attachments(
            scope="all", since="24h", only_available=True, limit=2, account=env.account_id
        )
        assert res_avail["count"] == 2
        assert res_avail["available_count"] == 2
        assert [x["filename"] for x in res_avail["attachments"]] == ["doc5.pdf", "doc4.pdf"]
        assert all(x["match_confidence"] == "md5" for x in res_avail["attachments"])
        assert len(hashed_paths) <= 2
    finally:
        service.close()


def test_parallel_prefetch_and_chunked_decrypt(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    pool = EncryptedDatabasePool(temp_root=tmp_path / "pool")
    try:
        db_files = {rel: env.storage_dir / rel for rel in env.keys_hex}
        keys = {rel: bytes.fromhex(h) for rel, h in env.keys_hex.items()}
        pool.register_databases(db_files, keys)
        rels = sorted(db_files)
        pool.prefetch_snapshots(rels)

        gens = []
        for rel in rels:
            state = pool._snapshots.get(rel)
            assert state is not None
            assert state.snapshot_path.is_file()
            assert quick_check_sqlite(state.snapshot_path)
            gens.append(state.generation)
        assert len(set(gens)) == len(rels)
    finally:
        pool.cleanup()





