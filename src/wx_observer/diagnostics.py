"""Redacted diagnostic report and zip bundle builder for wx-observer doctor."""

from __future__ import annotations

import json
import os
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from wx_observer import __version__
from wx_observer.audit import sha256_prefix
from wx_observer.cipher_store import count_residual_sessions
from wx_observer.discovery import WEIXIN_PROCESS_NAME, discover_environment
from wx_observer.exceptions import WxObserverError

if TYPE_CHECKING:
    from wx_observer.service import ObserverService


def get_wechat_version() -> str | None:
    """Return the installed Weixin.exe file version on Windows when a process is running."""
    if os.name != "nt":
        return None
    exe_path: str | None = None
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == WEIXIN_PROCESS_NAME:
                exe = proc.info.get("exe")
                if exe:
                    exe_path = str(exe)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    if not exe_path:
        return None
    try:
        import win32api  # type: ignore

        info = win32api.GetFileVersionInfo(exe_path, "\\")
        ms = int(info["FileVersionMS"])
        ls = int(info["FileVersionLS"])
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def build_doctor_report(
    service: ObserverService,
    *,
    account: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """Build a strictly redacted environment, database, key-source, and WAL health report."""
    env = discover_environment(db_dir=service.db_dir)
    accounts_summary = [
        {
            "account_hash": sha256_prefix(acct.account_id),
            "online": acct.online,
            "pids_count": len(acct.pids),
            "db_storage_exists": os.path.isdir(acct.db_storage_dir),
            "last_activity": acct.last_activity,
        }
        for acct in env["accounts"]
    ]

    databases: list[dict[str, Any]] = []
    warnings: list[str] = []
    repo_error: dict[str, Any] | None = None
    target_account_hash: str | None = None
    keyed_dbs = 0
    total_dbs = 0

    try:
        repo, _mon, chosen = service._open_or_reuse(
            account=account,
            pid=pid,
            make_default=False,
        )
        target_account_hash = sha256_prefix(chosen.account_id)
        databases = repo.inspect_db_metadata()
        warnings = list(repo.warnings)
        total_dbs = repo.total_db_count
        keyed_dbs = repo.keyed_db_count
    except WxObserverError as exc:
        repo_error = exc.to_error_dict()

    verified_dbs = sum(1 for db in databases if db.get("quick_check_ok") is True)
    failed_required = [
        db["rel_path"]
        for db in databases
        if db.get("required") and db.get("quick_check_ok") is not True
    ]
    failed_optional = [
        db["rel_path"]
        for db in databases
        if not db.get("required") and db.get("quick_check_ok") is not True
    ]

    return {
        "ok": repo_error is None,
        "wx_observer_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "environment": {
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "python_bits": 64 if sys.maxsize > 2**32 else 32,
            "wechat_version": get_wechat_version(),
        },
        "discovery": {
            "db_dir_detected": bool(env["db_dir"]),
            "accounts_count": len(env["accounts"]),
            "online_accounts_count": sum(1 for a in env["accounts"] if a.online),
            "processes_count": len(env["processes"]),
            "access_denied": env["access_denied"],
            "legacy_only": env["legacy_only"],
            "reason": env["reason"],
            "accounts": accounts_summary,
        },
        "target_account_hash": target_account_hash,
        "key_cache_mode": service.key_cache_mode,
        "keys_file_configured": bool(service.keys_file or os.environ.get("WX_OBSERVER_KEYS_FILE")),
        "audit": {
            "enabled": service.audit_logger.enabled,
            "calls_today": service.audit_logger.calls_today(),
        },
        "temp_dir": {
            "residual_session_dirs": count_residual_sessions(service.temp_root),
        },
        "total_dbs": total_dbs,
        "keyed_dbs": keyed_dbs,
        "verified_dbs": verified_dbs,
        "failed_required": failed_required,
        "failed_optional": failed_optional,
        "databases": databases,
        "warnings": warnings,
        "repo_error": repo_error,
    }


def write_doctor_bundle(report: dict[str, Any], bundle_path: str | Path) -> Path:
    """Write a redacted diagnostic report and notice into a shareable zip bundle."""
    dest = Path(bundle_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    notice = (
        "wx-observer redacted diagnostic bundle\n"
        "Contains: versions, OS/Python info, shard filenames, schema versions, key source flags, WAL status.\n"
        "Excludes: message content, contact/chat names, raw wxids, decryption keys, and Bearer tokens.\n"
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doctor_report.json", payload)
        zf.writestr("REDACTION_NOTICE.txt", notice)
    return dest
