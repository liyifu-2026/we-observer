"""DPAPI-protected per-user key cache and external key file loader."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from wx_observer.cipher_store import (
    PAGE_SIZE,
    derive_db_key_from_master,
    normalize_key_for_page1,
)
from wx_observer.exceptions import KeyUnavailableError

DPAPI_PREFIX = b"WXOBS-DPAPI-V1:"
DEFAULT_APP_DIR_NAME = "wx-observer"


def default_state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / DEFAULT_APP_DIR_NAME
    return Path.home() / f".{DEFAULT_APP_DIR_NAME}"


def _sanitize_account_name(account_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", account_id or "default")


def dpapi_protect(payload: bytes, description: str = "wx-observer-keys") -> bytes:
    """Encrypt bytes using Windows DPAPI scoped to the current Windows user."""
    import win32crypt  # type: ignore

    blob = win32crypt.CryptProtectData(payload, description, None, None, None, 0)
    return DPAPI_PREFIX + base64.b64encode(blob)


def dpapi_unprotect(raw: bytes) -> bytes:
    """Decrypt DPAPI blob; fail-closed by returning b'' on invalid or untrusted input."""
    if not raw.startswith(DPAPI_PREFIX):
        return b""
    try:
        import win32crypt  # type: ignore

        blob = base64.b64decode(raw[len(DPAPI_PREFIX) :], validate=True)
        _desc, plain = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return bytes(plain)
    except Exception:
        return b""


class KeyStore:
    """Persist and validate per-account SQLCipher 4 keys encrypted via Windows DPAPI."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.keys_dir = self.state_dir / "keys"

    def cache_path(self, account_id: str) -> Path:
        return self.keys_dir / f"{_sanitize_account_name(account_id)}.dpapi"

    def load_cached_keys(
        self,
        account_id: str,
        db_files: dict[str, Path],
    ) -> dict[str, bytes]:
        """Load cached keys for an account; fail-closed on DPAPI failure and HMAC-verify every key."""
        path = self.cache_path(account_id)
        if not path.is_file():
            return {}
        try:
            raw = path.read_bytes()
        except OSError:
            return {}

        plain = dpapi_unprotect(raw)
        if not plain:
            return {}

        try:
            decoded = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(decoded, dict):
            return {}

        raw_map = decoded.get("keys") if isinstance(decoded.get("keys"), dict) else decoded
        return validate_hex_key_map(raw_map, db_files)

    def save_cached_keys(self, account_id: str, keys: dict[str, bytes]) -> Path:
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        payload_dict = {
            "version": 1,
            "account_id": account_id,
            "keys": {rel: key.hex() for rel, key in sorted(keys.items())},
        }
        encoded = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        protected = dpapi_protect(encoded)
        target = self.cache_path(account_id)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(protected)
        os.replace(tmp, target)
        return target


def _read_page1(path: Path) -> bytes | None:
    try:
        with path.open("rb") as f:
            page1 = f.read(PAGE_SIZE)
        return page1 if len(page1) >= PAGE_SIZE else None
    except OSError:
        return None


def validate_hex_key_map(
    raw_map: dict[str, Any],
    db_files: dict[str, Path],
) -> dict[str, bytes]:
    """Normalize and HMAC-verify a {rel_path: hex_key} mapping against db_files."""
    validated: dict[str, bytes] = {}
    norm_lookup = {rel.replace("\\", "/"): rel for rel in db_files}
    for raw_rel, raw_hex in raw_map.items():
        if not isinstance(raw_rel, str) or not isinstance(raw_hex, str):
            continue
        rel = norm_lookup.get(raw_rel.replace("\\", "/"))
        if rel is None:
            continue
        cleaned = raw_hex.strip().lower()
        if cleaned.startswith("0x"):
            cleaned = cleaned[2:]
        if len(cleaned) not in (64, 96):
            continue
        try:
            key_bytes = bytes.fromhex(cleaned)
        except ValueError:
            continue
        page1 = _read_page1(db_files[rel])
        if page1 is None:
            continue
        normalized = normalize_key_for_page1(key_bytes, page1)
        if normalized is not None:
            validated[rel] = normalized
    return validated


def load_external_keys(
    keys_file: str | Path,
    db_files: dict[str, Path],
) -> dict[str, bytes]:
    """Load keys from an external JSON file (per-DB keys or master_key)."""
    path = Path(keys_file)
    if not path.is_file():
        raise KeyUnavailableError(
            "keys_file_not_found",
            f"External keys file does not exist: {path}",
            {"keys_file": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeyUnavailableError(
            "keys_file_invalid",
            f"Failed to parse external keys file {path}: {exc}",
            {"keys_file": str(path)},
        ) from exc

    if not isinstance(data, dict):
        raise KeyUnavailableError(
            "keys_file_invalid",
            f"External keys file must contain a JSON object: {path}",
            {"keys_file": str(path)},
        )

    out: dict[str, bytes] = {}
    master_hex = data.get("master_key")
    if isinstance(master_hex, str) and master_hex.strip():
        cleaned = master_hex.strip().lower()
        if cleaned.startswith("0x"):
            cleaned = cleaned[2:]
        if len(cleaned) != 64:
            raise KeyUnavailableError(
                "invalid_master_key",
                "master_key must be a 64-character hex string.",
            )
        try:
            master_bytes = bytes.fromhex(cleaned)
        except ValueError as exc:
            raise KeyUnavailableError("invalid_master_key", "master_key is not valid hex.") from exc
        for rel, db_path in db_files.items():
            page1 = _read_page1(db_path)
            if page1 is None:
                continue
            derived = derive_db_key_from_master(master_bytes, page1)
            if derived is not None:
                out[rel] = derived

    per_db = data.get("keys") if isinstance(data.get("keys"), dict) else data
    out.update(validate_hex_key_map(per_db, db_files))
    return out
