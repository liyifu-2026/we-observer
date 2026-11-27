"""Read-only Windows WeChat 4.x process, window, and data directory discovery."""

from __future__ import annotations

import ctypes
import glob
import os
import re
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil

from wx_observer.models import AccountInfo, ProcessInfo

WEIXIN_PROCESS_NAME = "weixin.exe"
LEGACY_PROCESS_NAME = "wechat.exe"
SUPPORTED_PROCESS_NAMES = frozenset({WEIXIN_PROCESS_NAME, LEGACY_PROCESS_NAME})
MAIN_TITLE_HINTS = ("微信", "WeChat")
MIN_MAIN_WINDOW_EXTENT = 480

_PATH_TOKEN_RE = re.compile(r"([A-Za-z]:\\[^\r\n\"'<>|*?\x00]+)")


def normalize_win_path(raw: str) -> str:
    """Normalize extended Windows paths (\\\\?\\...) and bare drive roots (D: -> D:\\)."""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    norm = os.path.normpath(text)
    if re.match(r"^[A-Za-z]:$", norm):
        norm += os.sep
    return norm


def _path_variants(path: str) -> list[str]:
    clean = normalize_win_path(path)
    if not clean:
        return []
    variants = [os.path.normcase(clean)]
    try:
        real = os.path.normcase(os.path.realpath(clean))
        if real and real not in variants:
            variants.append(real)
    except OSError:
        pass
    return variants


def _query_process_name_win32(pid: int) -> str:
    if not pid or os.name != "nt":
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def enumerate_windows() -> list[dict[str, Any]]:
    """Enumerate top-level WeChat windows on Windows without interacting with UI."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    found: list[dict[str, Any]] = []
    cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _visit(hwnd: int, _lparam: int) -> bool:
        pid_val = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
        pid = int(pid_val.value)
        if not pid:
            return True
        proc_name = _query_process_name_win32(pid)
        if proc_name not in SUPPORTED_PROCESS_NAMES:
            return True

        title_buf = ctypes.create_unicode_buffer(256)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        title = title_buf.value.strip()
        class_name = class_buf.value

        if "TrayIcon" in class_name or "TrayIcon" in title:
            return True
        if not any(hint in title for hint in MAIN_TITLE_HINTS) and not class_name.startswith("Qt"):
            return True

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        visible = bool(user32.IsWindowVisible(hwnd))
        minimized = max(width, height) < MIN_MAIN_WINDOW_EXTENT or rect.left <= -20000 or rect.top <= -20000
        if max(width, height) <= 0 and not minimized:
            return True

        found.append(
            {
                "hwnd": int(hwnd),
                "pid": pid,
                "process_name": proc_name,
                "title": title,
                "class_name": class_name,
                "visible": visible,
                "usable": not minimized,
            }
        )
        return True

    callback = cb_type(_visit)
    user32.EnumWindows(callback, 0)
    found.sort(key=lambda w: (0 if w["usable"] else 1, 0 if w["visible"] else 1, w["pid"]))
    return found


def list_running_pids(process_names: frozenset[str] = frozenset({WEIXIN_PROCESS_NAME})) -> list[tuple[int, str]]:
    """Return sorted (pid, lowercase_name) pairs for running WeChat processes."""
    hits: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in process_names:
                hits.append((int(proc.info["pid"]), name))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    hits.sort(key=lambda item: item[0])
    return hits


def collect_process_open_files(pid: int) -> tuple[list[str], str | None]:
    """Read open file paths for a WeChat PID and its direct WeChat child processes."""
    try:
        proc = psutil.Process(pid)
        opened = [normalize_win_path(item.path) for item in proc.open_files()]
    except psutil.AccessDenied:
        return [], "access_denied"
    except psutil.NoSuchProcess:
        return [], "process_exited"
    except Exception:
        return [], "open_files_error"

    try:
        for child in proc.children(recursive=False):
            try:
                if (child.name() or "").lower() not in SUPPORTED_PROCESS_NAMES:
                    continue
                opened.extend(normalize_win_path(item.path) for item in child.open_files())
            except Exception:
                continue
    except Exception:
        pass
    return [p for p in opened if p], None


def extract_live_db_entries(opened_paths: list[str]) -> list[tuple[str, str]]:
    """Extract verified (db_dir, account_id) pairs from open db_storage handles."""
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in opened_paths:
        clean = normalize_win_path(raw)
        if not clean:
            continue
        parts = Path(clean).parts
        for idx, part in enumerate(parts):
            if part.lower() != "db_storage" or idx < 2:
                continue
            account_id = parts[idx - 1]
            db_dir = normalize_win_path(str(Path(*parts[: idx - 1])))
            if not account_id or not db_dir:
                continue
            if not os.path.isdir(os.path.join(db_dir, account_id, "db_storage")):
                continue
            dedup = (os.path.normcase(db_dir), os.path.normcase(account_id))
            if dedup not in seen:
                seen.add(dedup)
                hits.append((db_dir, account_id))
            break
    return hits


def _locate_account_root(candidate: str | None) -> str | None:
    if not candidate:
        return None
    base = normalize_win_path(candidate)
    if not base or not os.path.isdir(base):
        return None
    probes = [
        base,
        os.path.join(base, "xwechat_files"),
        os.path.join(base, "WeChat Files"),
        os.path.join(base, "xwechat_files_data"),
    ]
    seen: set[str] = set()
    for root in probes:
        root = normalize_win_path(root)
        norm_key = os.path.normcase(root)
        if norm_key in seen or not os.path.isdir(root):
            continue
        seen.add(norm_key)
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        if any(os.path.isdir(os.path.join(root, name, "db_storage")) for name in entries):
            return root
    return None


def _windows_documents_dir() -> str | None:
    if os.name != "nt":
        return None
    try:
        from win32com.shell import shell  # type: ignore

        return normalize_win_path(shell.SHGetKnownFolderPath(shell.FOLDERID_Documents))
    except Exception:
        return None


def _config_dir_candidates() -> list[str]:
    out: list[str] = []
    for env_key in ("APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(env_key, "")
        if root:
            out.append(os.path.join(root, "Tencent", "xwechat", "config"))
    return out


def _registry_candidate_dirs() -> list[str]:
    if os.name != "nt":
        return []
    import winreg

    out: list[str] = []
    reg_paths = (
        r"Software\Tencent\Weixin",
        r"Software\Tencent\WeChat",
        r"Software\Tencent\bugReport\WechatWindows",
    )
    for subkey in reg_paths:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey)
        except OSError:
            continue
        try:
            idx = 0
            while True:
                try:
                    name, value, _vtype = winreg.EnumValue(key, idx)
                except OSError:
                    break
                idx += 1
                if isinstance(value, str) and value.strip():
                    low = name.lower()
                    if any(tok in low for tok in ("path", "dir", "save", "data")):
                        out.append(value.strip())
        finally:
            winreg.CloseKey(key)
    return out


def _extract_paths_from_config(raw: str) -> list[str]:
    found: list[str] = []
    text = raw.strip()
    if not text:
        return found
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" in stripped:
            rhs = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if ":" in rhs or "\\" in rhs or "/" in rhs:
                found.append(rhs)
        for match in _PATH_TOKEN_RE.finditer(stripped):
            found.append(match.group(1))
    return found


def auto_detect_db_dir(
    explicit_db_dir: str | None = None,
    live_db_dirs: list[str] | None = None,
) -> str | None:
    """Resolve the WeChat 4.x account parent directory (containing <account>/db_storage)."""
    for candidate in (explicit_db_dir, os.environ.get("WX_OBSERVER_DB_DIR")):
        if candidate:
            hit = _locate_account_root(candidate)
            if hit:
                return hit

    if live_db_dirs:
        for d in live_db_dirs:
            hit = _locate_account_root(d)
            if hit:
                return hit

    for cfg_dir in _config_dir_candidates():
        if not os.path.isdir(cfg_dir):
            continue
        for fp in glob.glob(os.path.join(cfg_dir, "*")):
            if os.path.isdir(fp):
                continue
            raw = ""
            for enc in ("utf-8", "gbk"):
                try:
                    with open(fp, "r", encoding=enc) as f:
                        raw = f.read(16384)
                    break
                except (UnicodeDecodeError, OSError):
                    continue
            for path in _extract_paths_from_config(raw):
                hit = _locate_account_root(path)
                if hit:
                    return hit

    for reg_dir in _registry_candidate_dirs():
        hit = _locate_account_root(reg_dir)
        if hit:
            return hit

    userprofile = os.environ.get("USERPROFILE", "")
    docs = _windows_documents_dir()
    for base in (docs, os.path.join(userprofile, "Documents") if userprofile else "", userprofile):
        hit = _locate_account_root(base)
        if hit:
            return hit
    return None


def _account_last_activity(db_storage_dir: str) -> float:
    latest = 0.0
    for root, _dirs, files in os.walk(db_storage_dir):
        rel_root = os.path.normcase(os.path.relpath(root, db_storage_dir))
        if rel_root == "migrate" or rel_root.startswith("migrate" + os.sep):
            continue
        for fname in files:
            if not fname.endswith(".db"):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(root, fname))
                if mtime > latest:
                    latest = mtime
            except OSError:
                continue
    return latest


def derive_wxid_from_account_dir(account_id: str) -> str:
    """Strip the 4-character directory hash suffix appended by WeChat 4.x."""
    return re.sub(r"_[0-9A-Za-z]{4}$", "", account_id)


def _find_account_dirs(db_dir: str) -> list[str]:
    if not db_dir or not os.path.isdir(db_dir):
        return []
    out: list[str] = []
    try:
        for name in sorted(os.listdir(db_dir)):
            full = os.path.join(db_dir, name)
            if os.path.isdir(os.path.join(full, "db_storage")):
                out.append(full)
    except OSError:
        return []
    return out


def discover_environment(
    db_dir: str | None = None,
    selected_account: str | None = None,
) -> dict[str, Any]:
    """Discover running Weixin.exe processes, windows, and all local account directories."""
    windows = enumerate_windows()
    windows_by_pid: dict[int, list[dict[str, Any]]] = {}
    for w in windows:
        windows_by_pid.setdefault(int(w["pid"]), []).append(w)

    weixin_pids = list_running_pids(frozenset({WEIXIN_PROCESS_NAME}))
    legacy_pids = list_running_pids(frozenset({LEGACY_PROCESS_NAME}))

    all_pids = {pid: name for pid, name in weixin_pids}
    for w in windows:
        if w["process_name"] == WEIXIN_PROCESS_NAME:
            all_pids.setdefault(int(w["pid"]), WEIXIN_PROCESS_NAME)

    process_infos: list[ProcessInfo] = []
    live_accounts_map: dict[tuple[str, str], list[int]] = {}
    live_db_dirs: list[str] = []
    seen_live_dirs: set[str] = set()
    access_denied = False

    for pid in sorted(all_pids):
        opened, err = collect_process_open_files(pid)
        if err == "access_denied":
            access_denied = True
        entries = extract_live_db_entries(opened)
        open_db_dir = entries[0][0] if entries else None
        open_account = entries[0][1] if entries else None
        for entry_dir, entry_acct in entries:
            norm_dir = os.path.normcase(entry_dir)
            if norm_dir not in seen_live_dirs:
                seen_live_dirs.add(norm_dir)
                live_db_dirs.append(entry_dir)
            key = (norm_dir, entry_acct)
            live_accounts_map.setdefault(key, []).append(pid)

        process_infos.append(
            ProcessInfo(
                pid=pid,
                process_name=all_pids[pid],
                windows=windows_by_pid.get(pid, []),
                open_db_dir=open_db_dir,
                open_account=open_account,
                access_error=err,
            )
        )

    explicit_scope = bool(db_dir or os.environ.get("WX_OBSERVER_DB_DIR"))
    resolved_db_dir = auto_detect_db_dir(explicit_db_dir=db_dir, live_db_dirs=live_db_dirs)

    candidate_dirs: list[str] = []
    seen_dirs: set[str] = set()
    roots_to_scan = [resolved_db_dir] if explicit_scope else ([resolved_db_dir] if resolved_db_dir else []) + live_db_dirs
    for d in roots_to_scan:
        if not d:
            continue
        norm_d = os.path.normcase(normalize_win_path(d))
        if norm_d not in seen_dirs and os.path.isdir(d):
            seen_dirs.add(norm_d)
            candidate_dirs.append(normalize_win_path(d))

    accounts: list[AccountInfo] = []
    seen_account_keys: set[tuple[str, str]] = set()
    for root_dir in candidate_dirs:
        norm_root = os.path.normcase(root_dir)
        for acct_path in _find_account_dirs(root_dir):
            acct_id = os.path.basename(acct_path)
            dedup_key = (norm_root, acct_id)
            if dedup_key in seen_account_keys:
                continue
            seen_account_keys.add(dedup_key)
            storage_dir = os.path.join(acct_path, "db_storage")
            pids = sorted(live_accounts_map.get(dedup_key, []))
            online = bool(pids)
            accounts.append(
                AccountInfo(
                    account_id=acct_id,
                    wxid=derive_wxid_from_account_dir(acct_id),
                    data_dir=acct_path,
                    db_storage_dir=storage_dir,
                    online=online,
                    pids=pids,
                    display_name="",
                    last_activity=_account_last_activity(storage_dir),
                    selected=(acct_id == selected_account),
                )
            )

    accounts.sort(key=lambda a: (0 if a.online else 1, -a.last_activity, a.account_id))

    legacy_only = bool(legacy_pids) and not process_infos and not accounts
    reason: str | None = None
    if not any(a.online for a in accounts):
        if access_denied:
            reason = (
                "Detected Weixin.exe, but open file handles could not be read (AccessDenied). "
                "Ensure wx-observer and WeChat run under the same Windows privilege level (do not run WeChat elevated unless wx-observer is elevated)."
            )
        elif legacy_only:
            reason = "Detected legacy WeChat.exe (3.x). wx-observer only supports WeChat 4.x (Weixin.exe)."
        elif process_infos and not accounts:
            reason = (
                "Detected Weixin.exe, but no <account>/db_storage directory was found. "
                "Specify --db-dir explicitly if WeChat stores files in a custom folder."
            )
        elif process_infos and accounts:
            reason = (
                "Detected Weixin.exe and local account directories, but no account is actively holding db_storage open yet "
                "(WeChat may still be at the login screen)."
            )
        elif not accounts:
            reason = "No running Weixin.exe process or WeChat 4.x data directory was found."

    return {
        "db_dir": resolved_db_dir,
        "accounts": accounts,
        "processes": process_infos,
        "access_denied": access_denied,
        "legacy_only": legacy_only,
        "reason": reason,
    }
