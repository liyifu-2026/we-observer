"""Structured domain exceptions for wx-observer."""

from __future__ import annotations

from typing import Any


_ERROR_RECOVERY: dict[str, tuple[bool, str]] = {
    "account_not_found": (
        False,
        "Call list_accounts and retry with a discovered account_id or wxid.",
    ),
    "pid_account_not_found": (
        False,
        "Call list_accounts to inspect active Weixin.exe PIDs and account_ids.",
    ),
    "pid_account_mismatch": (
        False,
        "Call list_accounts and pass the matching account and pid pair.",
    ),
    "ambiguous_accounts": (
        False,
        "Choose one candidate from details.candidates and pass its exact account_id or pid.",
    ),
    "no_online_account": (
        True,
        "Ask the user to log into WeChat or specify account/keys_file, then call list_accounts.",
    ),
    "chat_not_found": (
        False,
        "Call list_chats with a query or check the chat_id before retrying.",
    ),
    "ambiguous_chat": (
        False,
        "Choose one candidate from details.candidates and pass its exact chat_id.",
    ),
    "invalid_ack_token": (
        False,
        "Re-poll with poll_new_messages/wait_for_messages or ack using upto={chat_id: seq}.",
    ),
    "invalid_ack": (
        False,
        "Pass ack_token from poll_new_messages or a non-empty upto {chat_id: seq} mapping.",
    ),
    "invalid_timestamp": (
        False,
        "Pass a relative window (e.g. '24h', '7d'), ISO-8601 string, or Unix epoch seconds.",
    ),
    "invalid_scope": (
        False,
        "Use scope='all', 'private', or 'group'.",
    ),
    "invalid_buckets": (
        False,
        "Use buckets='hour', 'day', or 'none'.",
    ),
    "invalid_watch_mode": (
        False,
        "Use mode='all', 'private', 'group', or 'allowlist'.",
    ),
    "invalid_watch_config": (
        False,
        "Provide at least one chat in chats when mode='allowlist'.",
    ),
    "no_databases_found": (
        False,
        "Check --db-dir / WeChat data directory via list_accounts.",
    ),
    "keys_file_not_found": (
        False,
        "Verify the external keys_file path exists before retrying.",
    ),
    "keys_file_invalid": (
        False,
        "Fix the external keys JSON syntax or schema and retry.",
    ),
    "invalid_master_key": (
        False,
        "Provide a valid 64-character hex master_key and retry.",
    ),
    "account_offline_no_keys": (
        True,
        "Ask the user to log into this account or provide keys_file, then retry.",
    ),
    "keys_unavailable": (
        True,
        "Verify WeChat is unlocked and retry, or pass --keys-file.",
    ),
    "key_missing": (
        True,
        "Re-select account or verify WeChat is running to refresh database keys.",
    ),
    "access_denied": (
        False,
        "Run wx-observer under the same Windows privilege level as Weixin.exe.",
    ),
    "open_process_failed": (
        True,
        "Call list_accounts to confirm the active PID and retry.",
    ),
    "unsupported_platform": (
        False,
        "Run wx-observer on Windows 10/11 where WeChat 4.x local storage resides.",
    ),
    "python_32bit_unsupported": (
        False,
        "Use a 64-bit Python interpreter to inspect 64-bit Weixin.exe.",
    ),
    "db_decrypt_failed": (
        True,
        "WeChat may be writing or checkpointing the database; wait briefly and retry.",
    ),
    "mcp_not_installed": (
        False,
        "Install optional MCP dependencies with: pip install -e \".[mcp]\".",
    ),
}
_DEFAULT_RECOVERY = (False, "Inspect message and details before retrying.")


class WxObserverError(RuntimeError):
    """Base structured error with machine-readable error code and recovery hints."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        retryable: bool | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        default_retryable, default_action = _ERROR_RECOVERY.get(code, _DEFAULT_RECOVERY)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = default_retryable if retryable is None else bool(retryable)
        self.next_action = default_action if next_action is None else next_action

    def to_error_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next_action": self.next_action,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class AccountSelectionError(WxObserverError):
    """Raised when no target account can be uniquely selected or verified."""


class ChatNotFoundError(WxObserverError):
    """Raised when a chat reference matches no known conversation."""

    def __init__(self, chat_ref: str) -> None:
        super().__init__(
            "chat_not_found",
            f"No conversation matched '{chat_ref}'.",
            {"chat_ref": chat_ref},
        )


class AmbiguousChatError(WxObserverError):
    """Raised when a fuzzy chat reference matches multiple conversations."""

    def __init__(self, chat_ref: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(
            "ambiguous_chat",
            f"Conversation reference '{chat_ref}' matched {len(candidates)} chats; specify an exact chat_id.",
            {"chat_ref": chat_ref, "candidates": candidates},
        )


class KeyUnavailableError(WxObserverError):
    """Raised when decryption keys are missing or fail Page-1 HMAC validation."""


class MemoryAccessError(WxObserverError):
    """Raised when read-only process memory inspection is blocked by OS permissions or architecture."""


class DatabaseCorruptError(WxObserverError):
    """Raised when an encrypted database snapshot remains unreadable after retries."""
