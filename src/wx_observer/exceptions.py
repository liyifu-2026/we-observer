"""Structured domain exceptions for wx-observer."""

from __future__ import annotations

from typing import Any


class WxObserverError(RuntimeError):
    """Base structured error with machine-readable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_error_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
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
