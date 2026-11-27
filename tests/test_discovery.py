"""Tests for Weixin.exe discovery, multi-account mapping, and Windows path edge cases."""

from __future__ import annotations

import os
from pathlib import Path

from wx_observer import discovery


def _touch_account(db_dir: Path, account_id: str) -> Path:
    st = db_dir / account_id / "db_storage" / "message"
    st.mkdir(parents=True, exist_ok=True)
    db = st / "message_0.db"
    db.write_bytes(b"stub")
    return db


def test_drive_root_normalization_and_live_handle_mapping(tmp_path, monkeypatch) -> None:
    assert discovery.normalize_win_path("D:") == "D:\\"
    assert discovery.normalize_win_path(r"\\?\D:\xwechat_files\wxid_a\db_storage\contact.db") == r"D:\xwechat_files\wxid_a\db_storage\contact.db"

    db_dir = tmp_path / "xwechat_files"
    db_a = _touch_account(db_dir, "wxid_alpha_1122")
    _touch_account(db_dir, "wxid_offline_3344")
    db_b = _touch_account(db_dir, "wxid_beta_5566")

    monkeypatch.setattr(discovery, "enumerate_windows", lambda: [
        {"hwnd": 100, "pid": 4001, "process_name": "weixin.exe", "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True, "usable": True},
        {"hwnd": 200, "pid": 4002, "process_name": "weixin.exe", "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True, "usable": True},
    ])
    monkeypatch.setattr(discovery, "list_running_pids", lambda names: [(4001, "weixin.exe"), (4002, "weixin.exe")] if "weixin.exe" in names else [])

    def fake_open_files(pid: int):
        if pid == 4001:
            return [str(db_a)], None
        if pid == 4002:
            return [str(db_b)], None
        return [], None

    monkeypatch.setattr(discovery, "collect_process_open_files", fake_open_files)

    env = discovery.discover_environment(db_dir=str(db_dir))
    by_id = {a.account_id: a for a in env["accounts"]}
    assert by_id["wxid_alpha_1122"].online is True
    assert by_id["wxid_alpha_1122"].pids == [4001]
    assert by_id["wxid_alpha_1122"].wxid == "wxid_alpha"
    assert by_id["wxid_beta_5566"].online is True
    assert by_id["wxid_beta_5566"].pids == [4002]
    assert by_id["wxid_offline_3344"].online is False
    assert by_id["wxid_offline_3344"].pids == []


def test_access_denied_and_legacy_wechat_diagnostics(tmp_path, monkeypatch) -> None:
    db_dir = tmp_path / "xwechat_files"
    _touch_account(db_dir, "wxid_only_0001")

    monkeypatch.setattr(discovery, "enumerate_windows", lambda: [])
    monkeypatch.setattr(discovery, "list_running_pids", lambda names: [(9001, "weixin.exe")] if "weixin.exe" in names else [])
    monkeypatch.setattr(discovery, "collect_process_open_files", lambda pid: ([], "access_denied"))

    env = discovery.discover_environment(db_dir=str(db_dir))
    assert env["access_denied"] is True
    assert "AccessDenied" in (env["reason"] or "")

    # Legacy WeChat 3.x only
    monkeypatch.setattr(discovery, "list_running_pids", lambda names: [(888, "wechat.exe")] if "wechat.exe" in names else [])
    monkeypatch.setattr(discovery, "auto_detect_db_dir", lambda **kw: None)
    env_legacy = discovery.discover_environment()
    assert env_legacy["legacy_only"] is True
    assert "WeChat.exe (3.x)" in (env_legacy["reason"] or "")
