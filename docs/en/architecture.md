<p align="right">
  <a href="../../README.md">README</a> · <strong>English</strong> · <a href="../zh-CN/architecture.md">简体中文</a> · <a href="./guide.md">Usage Guide</a>
</p>

# Architecture & Boundaries

How `wx-observer` reads Windows WeChat 4.x (`Weixin.exe`) databases locally without touching WeChat memory writes, UI state, or message sending.

## Sequence Overview

```mermaid
sequenceDiagram
    autonumber
    participant Client as Agent / CLI
    participant Svc as ObserverService
    participant Disc as Discovery & KeyStore
    participant Pool as SQLCipher4 + WAL Pool
    participant Reduce as Shaping / Digest / Attachments
    participant State as CursorStateStore

    Client->>Svc: Query accounts, chats, history, digest, or attachments (optional explicit account)
    Svc->>Disc: On alias-cache miss, enumerate Weixin.exe handles & dirs lock-free; load DPAPI cache or read-only memory keys
    Disc-->>Svc: Initialize & reuse per-account repository (without mutating default account)
    Svc->>Pool: Check main DB + -wal mutations and merge incrementally into per-session temp directory
    Pool-->>Reduce: Return normalized messages, forwarded records, and file metadata
    Reduce-->>Client: Emit compact/full items, snippets, est_tokens, or verified local attachment paths
    Client->>Svc: poll_new_messages / wait_for_messages (MCP defaults to peek=True)
    Svc-->>Client: Deliver new batch + opaque ack_token (watermarks not yet committed)
    Client->>Svc: ack_messages(cursor_id, ack_token)
    Svc->>State: Atomically and idempotently commit sort_seq watermarks for cursor_id
```

## Key Mechanisms

1. **Process & Directory Discovery (`discovery.py`, `service.py`)**: Enumerates Windows handles held by `Weixin.exe` to locate custom/migrated `xwechat_files` roots and distinguish online vs. offline accounts. Explicit `account` lookups are cached with a 30s TTL and PID-liveness check; cache misses execute discovery outside `_registry_lock`.
2. **DPAPI Key Cache & Read-Only Memory Scan (`key_store.py`, `memory_scanner.py`)**: Per-database SQLCipher4 keys validated against Page-1 HMAC are cached via Windows DPAPI (`CryptProtectData`), scoped to the current Windows user and tagged with the creator Windows SID (`owner_sid`). **Important trust boundary**: DPAPI is bound to the Windows login user, not the `wx-observer` process; other programs running under the same Windows user can theoretically decrypt this cache. Use `--no-key-cache` (`WX_OBSERVER_NO_KEY_CACHE=1`) to disable disk caching entirely.

### Key Sources & Risk Boundaries

| Mode | Persistence | Trust Boundary & Tradeoff |
| --- | --- | --- |
| Default DPAPI cache (`%LOCALAPPDATA%\wx-observer\keys\`) | Encrypted per-account file with `owner_sid` check | Works even after WeChat exits; readable by other processes running as the same Windows user. |
| `--no-key-cache` (memory-only scan) | None (RAM only during process lifetime) | Zero key persistence on disk; requires live `Weixin.exe` on startup. |
| `--keys-file <path>` (`WX_OBSERVER_KEYS_FILE`) | User-managed JSON file | Full user control over file placement/ACLs; plaintext unless encrypted externally by user. |
3. **Incremental WAL Snapshot Pool (`cipher_store.py`)**: Copies changed pages and merges `-wal` frames into isolated `%TEMP%\wx_observer_tmp\sess-<pid>-<uuid>\` SQLite snapshots in parallel, verified with `PRAGMA quick_check`. Live WeChat files are never locked or modified.
4. **Cross-Shard Parsing & Server-Side Reduction (`repository.py`, `shaping.py`, `digest.py`, `attachments.py`)**: Queries shards (`message_*.db`) in single-pass batches with SQL C-layer keyword prefiltering and 64-bit `local_type` subtype pushdown, zstd blob decompression, compact shaping, token-budget trimming, and MD5/size-verified attachment lookup.
5. **At-Least-Once Idempotent Cursors (`state.py`, `monitor.py`)**: Persists independent watermarks per `(account_id, cursor_id)` in `state.db`. `peek=True` returns an `ack_token`; replays of `ack_messages` with the same token return `already_committed: true` without error or watermark regression.

## Strict Read-Only Guarantees & Local Files

- **No Write or UI Path to WeChat**: Zero message sending, zero keyboard/mouse/UI automation, zero DLL injection, zero process memory writes, and zero version-hardcoded binary signatures/RVAs.
- **Untrusted Content Boundary**: MCP `instructions` explicitly declare all message text, filenames, link titles, and quoted replies as untrusted data; shaped items include `"untrusted": true` and defuse Markdown links (`](http`) to mitigate prompt-injection and URL auto-fetch exfiltration.
- **Local Files Created by `wx-observer`**:
  - DPAPI-encrypted key cache: `%LOCALAPPDATA%\wx-observer\keys\` (omitted under `--no-key-cache`)
  - Watch cursor SQLite database: `%LOCALAPPDATA%\wx-observer\state.db` (or `--state-dir`)
  - Read audit trail: `%LOCALAPPDATA%\wx-observer\audit.jsonl` (10MB × 3 rotation, SHA-256[:12] digests only; `--audit off|on|<path>`)
  - Temporary decrypted SQLite snapshots: `%TEMP%\wx_observer_tmp\sess-*` (cleaned automatically on normal exit; orphaned sessions cleaned on startup)
  - User-requested exports (via `history --export ... --output ...`)
- **MCP Scope**: Over MCP, returned tool payloads flow to the host client according to that client's retention settings. Streamable HTTP binds to `127.0.0.1` by default and supports constant-time Bearer token authentication (prefer `WX_OBSERVER_MCP_TOKEN` or `--token-file`).

## Disclaimer & License

Provided solely for personal local data backup, technical learning, and research. Do not use for commercial marketing, bulk scraping, or any activity violating applicable laws or software terms of service. Open-source under the [MIT License](../../LICENSE).
