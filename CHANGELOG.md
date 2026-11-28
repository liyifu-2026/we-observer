# Changelog

All notable changes to `wx-observer` are documented in this file.

## [0.4.0] - 2026-11-28

### Security & Trust Boundaries
- **Untrusted Content Declaration & Link Defusal** (Closes #1): Added untrusted-data and no-URL-auto-fetch declarations to MCP `instructions`; added `defuse_markdown_links()` in `shaping.py` (`](http` -> `] (http`) and `"untrusted": true` tags on shaped messages, highlights, mentions, and forwarded items.
- **HTTP Bearer Secret Hygiene** (Closes #7): Added `WX_OBSERVER_MCP_TOKEN` env var and `--token-file` (with Windows DACL / POSIX permission warnings); deprecated CLI `--token` with a stderr warning.
- **DPAPI Boundary Documentation & `--no-key-cache`** (Closes #12): Documented Windows user-scope DPAPI boundaries and key-source risk tradeoffs; added `--no-key-cache` (`WX_OBSERVER_NO_KEY_CACHE=1`) and `owner_sid` verification on cached `.dpapi` blobs.
- **Privacy-Preserving Audit Log** (Closes #10): Added rotating (`10MB x 3`) append-only `audit.jsonl` recording whitelisted parameters, `rows`/`rows_kind`, `est_tokens`, and 12-char SHA-256 hashes of `account`/`chat`/`keyword`; configurable via `--audit off|on|<path>` (`WX_OBSERVER_AUDIT`) and surfaced in `get_status`.

### Agent Ergonomics & New Tools
- **Self Identity & `is_self` Attribution** (Closes #2): Populated `display_name`/`alias` and top-level `self` on `list_accounts` and `get_status` without requiring `select_account` first; added `self` and `top_senders[].is_self` to `digest`.
- **Forwarded Record Expansion (`expand_chat_history`)** (Closes #3): Added the 13th MCP tool `expand_chat_history` (with bounded `limit=20` default and `0` to expand all) plus `meta.embedded_files` enrichment and consistent `available`/`local_path` (`null` when missing)/`next_action` parity with `list_attachments(include_embedded=...)`.
- **Scoped Multi-Chat Search** (Closes #4): Added `chats`, `exclude_chats`, `min_activity`, and `total_matched`/`scanned_chats`/`truncated` telemetry to `search_messages`.
- **`head_tail` Compact Truncation** (Closes #5): Raised default compact cap from `300` to `450` chars using 70% head + `…` + 30% tail (`head_tail`) so trailing deadlines are preserved; added `truncate="head|tail|head_tail|none"`.
- **Year-Aware Timestamps** (Closes #6): Added timezone-aware `ts_iso` across messages, digests, and attachments; updated `digest` hourly buckets to `%Y-%m-%dT%H:00` and `window` with `since_iso`/`until_iso`.
- **Unified Schema Naming** (Closes #13): Standardized `name_query`, `chat_type`, `exclude_chats`, `watch_initialized`, and `ready` (preserving deprecated `query`, `scope`, and `initialized` aliases); added a parameter glossary to MCP `instructions`.

### Diagnostics, Onboarding & CLI
- **Resilient `verify` & Redacted `doctor --bundle`** (Closes #8): Added `wx-observer verify` (with DB role classification: required `message`/`support` vs optional `fts`/`aux`, exit `0` all ok / `1` optional degraded / `2` required or `--strict` failed) and `wx-observer doctor --bundle <out.zip>` (reporting both `keyed` and `verified` DB counts without leaking content or keys).
- **MCP Client Config Generator** (Closes #9): Added `wx-observer mcp --print-config cursor|claude|vscode|codex|cordis|generic`.
- **Install & CLI Polish** (Closes #11): Added `ensurepip`-free host-pip bootstrap instructions to both EN/ZH usage guides and added `wx-observer --version` (`-V`).

## [0.3.2] - 2026-11-27

- Added idempotent `ack_messages` replays (`already_committed`), 30s explicit-account alias cache with lock-free discovery, `system` message retention tagged `noise: true`, and structured `retryable`/`next_action` error contracts.

## [0.3.1] - 2026-11-27

- Added shard table indexing, 64-bit `local_type` SQL pushdown, wave early-stop for `search_messages`/`list_attachments`, lazy MD5 hashing, and parallel snapshot prefetching.
