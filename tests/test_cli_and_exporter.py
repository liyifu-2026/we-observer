"""Tests for CLI commands and JSON/JSONL/CSV/Markdown exporters."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import threading
import time

from tests.fixtures import SyntheticAccountEnv
from wx_observer.exporter import format_csv, format_markdown


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wx_observer", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_exporter_csv_and_markdown() -> None:
    events = [
        {
            "iso_time": "2026-11-26T10:00:00Z",
            "timestamp": 1795687200,
            "account_id": "wxid_self_1234",
            "chat_id": "wxid_alice",
            "chat_name": "Alice",
            "chat_type": "private",
            "sender_id": "wxid_alice",
            "sender_name": "Alice",
            "is_self": False,
            "msg_type": "text",
            "text": "Hello CSV & MD",
            "quote": {"quoted_type": "text", "excerpt": "Prev line", "sender_name": "Self"},
            "sort_seq": 10,
            "local_id": 1,
            "shard": "message_0.db",
        }
    ]
    csv_text = format_csv(events)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 1
    assert rows[0]["text"] == "Hello CSV & MD"
    assert rows[0]["quote_excerpt"] == "Prev line"

    md_text = format_markdown(events, title="Chat Export")
    assert "# Chat Export" in md_text
    assert "## Alice (`wxid_alice`)" in md_text
    assert "> Self: Prev line" in md_text
    assert "Hello CSV & MD" in md_text


def test_cli_accounts_chats_history_search_and_watch(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    state_dir = tmp_path / "state"
    base_args = [
        "--db-dir",
        str(env.db_dir),
        "--keys-file",
        str(env.keys_file),
        "--state-dir",
        str(state_dir),
    ]

    # 1. accounts --json (verify flags work both before and after subcommand)
    r_acct = _run_cli([*base_args, "accounts", "--json"])
    assert r_acct.returncode == 0, r_acct.stderr
    acct_payload = json.loads(r_acct.stdout)
    assert acct_payload["accounts"][0]["account_id"] == env.account_id

    r_acct_after = _run_cli(["accounts", *base_args, "--json"])
    assert r_acct_after.returncode == 0, r_acct_after.stderr
    acct_payload_after = json.loads(r_acct_after.stdout)
    assert acct_payload_after["accounts"][0]["account_id"] == env.account_id

    # 2. chats --json
    r_chats = _run_cli([*base_args, "chats", "--account", env.account_id, "--json"])
    assert r_chats.returncode == 0, r_chats.stderr
    chats_payload = json.loads(r_chats.stdout)
    assert chats_payload["count"] == 2

    # 3. history --export csv & md to disk
    csv_out = tmp_path / "out" / "alice.csv"
    md_out = tmp_path / "out" / "alice.md"
    r_csv = _run_cli([*base_args, "history", "AliceRemark", "--account", env.account_id, "--export", "csv", "--output", str(csv_out)])
    assert r_csv.returncode == 0, r_csv.stderr
    assert "Zstd compressed long message 中文测试" in csv_out.read_text(encoding="utf-8")

    r_md = _run_cli([*base_args, "history", "AliceRemark", "--account", env.account_id, "--export", "md", "--output", str(md_out)])
    assert r_md.returncode == 0, r_md.stderr
    assert "## AliceRemark (`wxid_alice99`)" in md_out.read_text(encoding="utf-8")

    # 4. search --json
    r_search = _run_cli([*base_args, "search", "spec.pdf", "--account", env.account_id, "--json"])
    assert r_search.returncode == 0, r_search.stderr
    search_payload = json.loads(r_search.stdout)
    assert search_payload["count"] == 1
    assert search_payload["messages"][0]["chat_id"] == env.room_id

    # 5. watch --jsonl --max-events 1 while background thread waits for baseline init then appends WAL
    import sqlite3

    def emit_after_baseline():
        deadline = time.monotonic() + 15.0
        state_db = state_dir / "state.db"
        while time.monotonic() < deadline:
            if state_db.is_file():
                try:
                    conn = sqlite3.connect(str(state_db))
                    row = conn.execute(
                        "SELECT initialized_at FROM watch_configs WHERE account_id=? AND cursor_id='cli-test'",
                        (env.account_id,),
                    ).fetchone()
                    conn.close()
                    if row and row[0] is not None:
                        break
                except sqlite3.Error:
                    pass
            time.sleep(0.05)
        time.sleep(0.15)
        env.append_wal_messages(
            env.alice_id,
            [(30, 1, 950, 3, int(time.time()), "CLI live watch hit")],
        )

    t = threading.Thread(target=emit_after_baseline, daemon=True)
    t.start()
    r_watch = _run_cli(
        [
            *base_args,
            "watch",
            "--account",
            env.account_id,
            "--cursor-id",
            "cli-test",
            "--interval",
            "0.2",
            "--max-events",
            "1",
            "--duration-seconds",
            "15",
            "--jsonl",
        ]
    )
    t.join(timeout=2.0)
    assert r_watch.returncode == 0, r_watch.stderr
    lines = [json.loads(line) for line in r_watch.stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["text"] == "CLI live watch hit"

    # Negative test: unknown chat exits non-zero with structured JSON error on stderr
    r_err = _run_cli([*base_args, "history", "no_such_contact_999", "--account", env.account_id, "--json"])
    assert r_err.returncode == 2
    err_json = json.loads(r_err.stderr)
    assert err_json["ok"] is False
    assert err_json["error"] == "chat_not_found"
