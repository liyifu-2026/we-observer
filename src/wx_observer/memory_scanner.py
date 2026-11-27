"""Pure read-only Win32 memory scanner for WCDB SQLCipher configuration objects."""

from __future__ import annotations

import ctypes
import os
import re
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

from wx_observer.cipher_store import PAGE_SIZE, verify_page1_hmac
from wx_observer.exceptions import MemoryAccessError

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE_PROTECTIONS = frozenset({0x02, 0x04, 0x08, 0x20, 0x40, 0x80})

CIPHER_CONFIG_MARKER = b"com.Tencent.WCDB.Config.Cipher"
CIPHER_XOR_PAD = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_KEY_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

SCAN_CHUNK_BYTES = 8 * 1024 * 1024
SCAN_OVERLAP_BYTES = 256
MAX_USER_PTR = 0x800000000000
MIN_USER_PTR = 0x10000


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__pad1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__pad2", wintypes.DWORD),
    ]


def _get_kernel32():
    if os.name != "nt":
        raise MemoryAccessError(
            "unsupported_platform",
            "Process memory scanning is only supported on Windows 10/11.",
        )
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _has_sufficient_entropy(key_bytes: bytes) -> bool:
    return len(set(key_bytes)) >= 8


def _iter_readable_regions(k32, handle: int) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    addr = 0
    mbi = _MEMORY_BASIC_INFORMATION()
    mbi_size = ctypes.sizeof(mbi)
    while addr < MAX_USER_PTR:
        queried = k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), mbi_size)
        if not queried:
            break
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize or 0)
        if size <= 0:
            break
        prot = int(mbi.Protect or 0)
        if (
            mbi.State == MEM_COMMIT
            and not (prot & (PAGE_GUARD | PAGE_NOACCESS))
            and (prot & 0xFF) in READABLE_PROTECTIONS
        ):
            regions.append((base, size))
        next_addr = base + size
        if next_addr <= addr:
            break
        addr = next_addr
    return regions


def _read_bytes(k32, handle: int, addr: int, size: int) -> bytes | None:
    if size <= 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(bytes_read)
    )
    if ok and bytes_read.value > 0:
        return buf.raw[: bytes_read.value]
    return None


def _scan_regions_for_patterns(
    k32,
    handle: int,
    regions: list[tuple[int, int]],
    patterns: list[bytes],
    deadline: float,
) -> dict[bytes, list[int]]:
    hits: dict[bytes, list[int]] = {p: [] for p in patterns}
    if not patterns:
        return hits

    for base, size in regions:
        if time.monotonic() > deadline:
            break
        offset = 0
        while offset < size:
            if time.monotonic() > deadline:
                break
            read_len = min(SCAN_CHUNK_BYTES, size - offset)
            chunk = _read_bytes(k32, handle, base + offset, read_len)
            if chunk:
                for pat in patterns:
                    pos = chunk.find(pat)
                    while pos != -1:
                        hits[pat].append(base + offset + pos)
                        pos = chunk.find(pat, pos + 1)
            step = max(1, read_len - SCAN_OVERLAP_BYTES) if read_len == SCAN_CHUNK_BYTES else read_len
            offset += step
    return hits


def scan_pid_for_candidates(
    pid: int,
    timeout_seconds: float = 20.0,
) -> list[tuple[bytes, bytes | None]]:
    """Scan a single Weixin.exe PID read-only for WCDB cipher key candidates."""
    if sys.maxsize <= 2**32:
        raise MemoryAccessError(
            "python_32bit_unsupported",
            "64-bit Python is required to inspect 64-bit Weixin.exe memory.",
        )

    k32 = _get_kernel32()
    handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, int(pid))
    if not handle:
        err = ctypes.get_last_error()
        if err == 5:
            raise MemoryAccessError(
                "access_denied",
                f"Permission denied reading memory of Weixin.exe PID {pid} (Win32 error 5). "
                "Ensure wx-observer and WeChat run at the same privilege level.",
                {"pid": pid, "win32_error": err},
            )
        raise MemoryAccessError(
            "open_process_failed",
            f"Failed to open Weixin.exe PID {pid} for read-only inspection (Win32 error {err}).",
            {"pid": pid, "win32_error": err},
        )

    deadline = time.monotonic() + timeout_seconds
    candidates: list[tuple[bytes, bytes | None]] = []
    seen: set[tuple[bytes, bytes | None]] = set()
    try:
        regions = _iter_readable_regions(k32, handle)
        marker_hits = _scan_regions_for_patterns(
            k32, handle, regions, [CIPHER_CONFIG_MARKER], deadline
        )
        needles = set(marker_hits.get(CIPHER_CONFIG_MARKER, []))
        if not needles:
            return []

        descriptors = [
            struct.pack("<QQ", addr, len(CIPHER_CONFIG_MARKER))
            for addr in sorted(needles)
        ]
        desc_hits = _scan_regions_for_patterns(
            k32, handle, regions, descriptors, deadline
        )

        for desc_addrs in desc_hits.values():
            for qaddr in desc_addrs:
                node = _read_bytes(k32, handle, qaddr - 0x10, 0x50)
                if not node or len(node) < 0x40:
                    continue
                ptr_val, len_val = struct.unpack_from("<QQ", node, 0x10)
                if ptr_val not in needles or len_val != len(CIPHER_CONFIG_MARKER):
                    continue
                config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                if not (MIN_USER_PTR <= config_ptr < MAX_USER_PTR):
                    continue
                obj = _read_bytes(k32, handle, config_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    continue
                data_ptr, data_len = struct.unpack_from("<QQ", obj, 0x8)
                if not (0 < data_len <= 1024 and MIN_USER_PTR <= data_ptr < MAX_USER_PTR):
                    continue
                blob = _read_bytes(k32, handle, data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    continue

                decoded = bytes(
                    byte ^ CIPHER_XOR_PAD[idx % len(CIPHER_XOR_PAD)]
                    for idx, byte in enumerate(blob)
                )
                for match in HEX_KEY_RE.finditer(decoded):
                    hex_str = match.group(1).decode("ascii").lower()
                    offsets = [0]
                    if len(hex_str) > 96:
                        offsets.extend(range(0, len(hex_str) - 63, 32))
                        offsets.append(len(hex_str) - 64)
                    for start in dict.fromkeys(offsets):
                        if start + 64 > len(hex_str):
                            continue
                        key_bytes = bytes.fromhex(hex_str[start : start + 64])
                        if not _has_sufficient_entropy(key_bytes):
                            continue
                        pair_std = (key_bytes, None)
                        if pair_std not in seen:
                            seen.add(pair_std)
                            candidates.append(pair_std)
                        if start + 96 <= len(hex_str):
                            salt = bytes.fromhex(hex_str[start + 64 : start + 96])
                            pair_salted = (key_bytes, salt)
                            if pair_salted not in seen:
                                seen.add(pair_salted)
                                candidates.append(pair_salted)
        return candidates
    finally:
        k32.CloseHandle(handle)


def match_candidates_to_databases(
    candidates: list[tuple[bytes, bytes | None]],
    db_files: dict[str, Path],
) -> dict[str, bytes]:
    """Validate memory key candidates against target databases via Page-1 HMAC."""
    page1_cache: dict[str, bytes] = {}
    for rel, path in db_files.items():
        try:
            with path.open("rb") as f:
                p1 = f.read(PAGE_SIZE)
            if len(p1) >= PAGE_SIZE:
                page1_cache[rel] = p1
        except OSError:
            continue

    matched: dict[str, bytes] = {}
    # Pass 1: standard 32-byte keys (salt from Page 1 header)
    for cand_key, salt in candidates:
        if salt is not None:
            continue
        for rel, page1 in page1_cache.items():
            if rel in matched:
                continue
            if verify_page1_hmac(cand_key, page1):
                matched[rel] = cand_key

    # Pass 2: 48-byte key+salt only for databases that did not verify as standard 32B
    for cand_key, salt in candidates:
        if salt is None:
            continue
        for rel, page1 in page1_cache.items():
            if rel in matched:
                continue
            if verify_page1_hmac(cand_key, page1, explicit_salt=salt):
                matched[rel] = cand_key + salt

    return matched


def extract_keys_from_pids(
    pids: list[int],
    db_files: dict[str, Path],
    timeout_seconds: float = 20.0,
) -> dict[str, bytes]:
    """Scan specified Weixin.exe PIDs read-only and return HMAC-verified per-DB keys."""
    if not pids or not db_files:
        return {}

    merged: dict[str, bytes] = {}
    last_access_error: MemoryAccessError | None = None
    remaining = set(db_files.keys())

    for pid in pids:
        if not remaining:
            break
        try:
            candidates = scan_pid_for_candidates(pid, timeout_seconds=timeout_seconds)
        except MemoryAccessError as exc:
            last_access_error = exc
            continue
        subset = {rel: db_files[rel] for rel in remaining}
        found = match_candidates_to_databases(candidates, subset)
        merged.update(found)
        remaining -= set(found.keys())

    if not merged and last_access_error is not None:
        raise last_access_error
    return merged
