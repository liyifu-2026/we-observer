"""SQLCipher 4 verification, page decryption, WAL merging, and ephemeral snapshot pool."""

from __future__ import annotations

import atexit
import hashlib
import hmac
import logging
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path

import psutil
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wx_observer.exceptions import DatabaseCorruptError, KeyUnavailableError

logger = logging.getLogger(__name__)

PAGE_SIZE = 4096
RESERVE_SIZE = 80
IV_OFFSET = PAGE_SIZE - RESERVE_SIZE  # 4016
HMAC_OFFSET = PAGE_SIZE - 64          # 4032
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_FRAME_SIZE = WAL_FRAME_HEADER_SIZE + PAGE_SIZE  # 4120
WAL_MAGICS = (0x377F0682, 0x377F0683)
VALID_BTREE_PAGE_TYPES = frozenset({0, 2, 5, 10, 13})
MASTER_KDF_ITERS = 256_000


def sqlite_text_factory(raw: bytes) -> str | bytes:
    """Decode TEXT columns as UTF-8 when valid, otherwise preserve raw bytes."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw


def verify_page1_hmac(
    key: bytes,
    page1: bytes,
    explicit_salt: bytes | None = None,
) -> bool:
    """Verify SQLCipher 4 Page-1 HMAC-SHA512 using either 32B raw or 48B key+salt."""
    if len(page1) < PAGE_SIZE:
        return False

    if len(key) == 48:
        enc_key = key[:32]
        salt = key[32:48] if explicit_salt is None else explicit_salt
    elif len(key) == 32:
        enc_key = key
        salt = page1[:16] if explicit_salt is None else explicit_salt
    else:
        return False

    if len(salt) != 16:
        return False

    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    mac_input = page1[16:HMAC_OFFSET]
    stored_mac = page1[HMAC_OFFSET:PAGE_SIZE]
    digest = hmac.new(mac_key, mac_input, hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    return hmac.compare_digest(digest.digest(), stored_mac)


def derive_db_key_from_master(master_key: bytes, page1: bytes) -> bytes | None:
    """Derive a per-database 32-byte key from a 32-byte master key and verify it."""
    if len(master_key) != 32 or len(page1) < PAGE_SIZE:
        return None
    derived = hashlib.pbkdf2_hmac(
        "sha512", master_key, page1[:16], MASTER_KDF_ITERS, dklen=32
    )
    return derived if verify_page1_hmac(derived, page1) else None


def normalize_key_for_page1(key: bytes, page1: bytes) -> bytes | None:
    """Return validated 32B or 48B key matching Page 1 layout, preferring standard 32B."""
    if len(page1) < PAGE_SIZE:
        return None
    if len(key) not in (32, 48):
        return None
    if verify_page1_hmac(key[:32], page1):
        return key[:32]
    if len(key) == 48 and verify_page1_hmac(key, page1):
        return key
    return None


def decrypt_page(key: bytes, page: bytes, pgno: int) -> bytes:
    """Decrypt a single 4096-byte SQLCipher 4 page into standard SQLite page layout."""
    enc_key = key[:32]
    iv = page[IV_OFFSET : IV_OFFSET + 16]
    decryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    if pgno == 1:
        header = page[:16] if len(key) == 48 else SQLITE_HEADER
        decrypted = decryptor.update(page[16:IV_OFFSET]) + decryptor.finalize()
        return header + decrypted + (b"\x00" * RESERVE_SIZE)
    decrypted = decryptor.update(page[:IV_OFFSET]) + decryptor.finalize()
    return decrypted + (b"\x00" * RESERVE_SIZE)


def quick_check_sqlite(db_path: Path) -> bool:
    """Run PRAGMA quick_check on a decrypted SQLite file to verify structural integrity."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute("PRAGMA quick_check").fetchall()
        return bool(rows) and all(str(row[0]) == "ok" for row in rows)
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


@dataclass(slots=True)
class SnapshotState:
    src_mtime_ns: int
    src_size: int
    wal_mtime_ns: int
    wal_size: int
    wal_salt: bytes
    applied_frames: int
    snapshot_path: Path
    generation: int
    wal_warning: str | None = None


_SESS_DIR_RE = re.compile(r"^sess-(\d+)-[0-9a-f]+$")


def _cleanup_pool_ref(pool_ref: weakref.ReferenceType[EncryptedDatabasePool]) -> None:
    pool = pool_ref()
    if pool is not None:
        pool.cleanup()


class EncryptedDatabasePool:
    """Manages decrypted read-only SQLite snapshots in an ephemeral session temp dir."""

    def __init__(self, temp_root: str | Path | None = None) -> None:
        root = Path(temp_root) if temp_root else (Path(tempfile.gettempdir()) / "wx_observer_tmp")
        self._temp_root = root
        self._session_dir = root / f"sess-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._closed = False
        self._keys: dict[str, bytes] = {}
        self._paths: dict[str, Path] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._snapshots: dict[str, SnapshotState] = {}
        self._gen_counter = 0
        self._warnings: list[str] = []

        self._purge_dead_sessions()
        self._session_dir.mkdir(parents=True, exist_ok=True)
        atexit.register(_cleanup_pool_ref, weakref.ref(self))

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def register_databases(self, db_files: dict[str, Path], keys: dict[str, bytes]) -> None:
        self._paths = dict(db_files)
        self._keys = dict(keys)

    def has_key(self, rel_path: str) -> bool:
        return rel_path in self._keys

    def _purge_dead_sessions(self) -> None:
        if not self._temp_root.is_dir():
            return
        try:
            entries = list(self._temp_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            match = _SESS_DIR_RE.match(entry.name)
            if not match:
                continue
            pid = int(match.group(1))
            if pid == os.getpid():
                continue
            try:
                if psutil.pid_exists(pid):
                    continue
            except Exception:
                continue
            shutil.rmtree(entry, ignore_errors=True)

    def _lock_for(self, rel_path: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(rel_path, threading.Lock())

    def _safe_stem(self, rel_path: str) -> str:
        clean = rel_path.replace("\\", "/").replace("/", "__")
        return re.sub(r"[^A-Za-z0-9_.-]", "_", clean)

    def _next_generation_path(self, rel_path: str) -> tuple[int, Path]:
        self._gen_counter += 1
        stem = self._safe_stem(rel_path)
        return self._gen_counter, self._session_dir / f"{stem}.g{self._gen_counter}.db"

    def _decrypt_main_file(self, src: Path, dst: Path, key: bytes) -> None:
        size = src.stat().st_size
        pages = (size + PAGE_SIZE - 1) // PAGE_SIZE
        with src.open("rb") as fin, dst.open("wb") as fout:
            for pgno in range(1, pages + 1):
                raw = fin.read(PAGE_SIZE)
                if not raw:
                    break
                if len(raw) < PAGE_SIZE:
                    raw = raw + (b"\x00" * (PAGE_SIZE - len(raw)))
                fout.write(decrypt_page(key, raw, pgno))

    def _read_wal_meta(self, wal_path: Path) -> tuple[int, int, bytes, int]:
        if not wal_path.is_file():
            return 0, 0, b"", 0
        try:
            st = wal_path.stat()
            if st.st_size < WAL_HEADER_SIZE:
                return st.st_mtime_ns, st.st_size, b"", 0
            with wal_path.open("rb") as wf:
                hdr = wf.read(WAL_HEADER_SIZE)
            if len(hdr) < WAL_HEADER_SIZE:
                return st.st_mtime_ns, st.st_size, b"", 0
            magic = struct.unpack(">I", hdr[:4])[0]
            if magic not in WAL_MAGICS:
                return st.st_mtime_ns, st.st_size, b"", 0
            frame_count = max(0, (st.st_size - WAL_HEADER_SIZE) // WAL_FRAME_SIZE)
            return st.st_mtime_ns, st.st_size, hdr[16:24], frame_count
        except OSError:
            return 0, 0, b"", 0

    def _merge_wal(
        self,
        dst: Path,
        wal_path: Path,
        key: bytes,
        wal_salt: bytes,
        from_frame: int,
    ) -> int:
        if not wal_path.is_file() or not wal_salt:
            return from_frame

        wal_size = wal_path.stat().st_size
        total_frames = max(0, (wal_size - WAL_HEADER_SIZE) // WAL_FRAME_SIZE)
        if from_frame >= total_frames:
            return total_frames

        max_pgno = 0
        last_frame = from_frame
        with dst.open("r+b") as out, wal_path.open("rb") as wal:
            hdr = wal.read(WAL_HEADER_SIZE)
            if len(hdr) < WAL_HEADER_SIZE or hdr[16:24] != wal_salt:
                raise ValueError("wal_salt_changed_during_read")
            wal.seek(WAL_HEADER_SIZE + from_frame * WAL_FRAME_SIZE)
            for idx in range(from_frame, total_frames):
                fhdr = wal.read(WAL_FRAME_HEADER_SIZE)
                page = wal.read(PAGE_SIZE)
                if len(fhdr) < WAL_FRAME_HEADER_SIZE or len(page) < PAGE_SIZE:
                    break
                last_frame = idx + 1
                pgno = struct.unpack(">I", fhdr[:4])[0]
                if pgno == 0:
                    continue
                if fhdr[8:16] != wal_salt:
                    continue
                plain = decrypt_page(key, page, pgno)
                if pgno == 1:
                    if len(key) == 48:
                        if plain[16:18] != b"\x01\x01":
                            continue
                    elif plain[:16] != SQLITE_HEADER:
                        continue
                elif plain[0] not in VALID_BTREE_PAGE_TYPES:
                    continue
                out.seek((pgno - 1) * PAGE_SIZE)
                out.write(plain)
                if pgno > max_pgno:
                    max_pgno = pgno

            out.flush()
            db_pages = (dst.stat().st_size + PAGE_SIZE - 1) // PAGE_SIZE
            out.seek(0)
            page1 = out.read(PAGE_SIZE)
            if len(page1) == PAGE_SIZE:
                hdr_pages = struct.unpack(">I", page1[28:32])[0]
                new_pages = max(hdr_pages, max_pgno, db_pages)
                if new_pages != hdr_pages:
                    updated = page1[:28] + struct.pack(">I", new_pages) + page1[32:]
                    out.seek(0)
                    out.write(updated)
                    out.flush()
        return last_frame

    def _unlink_best_effort(self, path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _build_full_snapshot(self, rel_path: str, src: Path, key: bytes) -> SnapshotState:
        wal_path = Path(str(src) + "-wal")
        self._session_dir.mkdir(parents=True, exist_ok=True)
        tmp_base = self._session_dir / f"{self._safe_stem(rel_path)}.{os.getpid()}.{threading.get_ident()}.tmp"

        src_mtime_ns = 0
        src_size = 0
        base_ok = False
        for _attempt in range(4):
            try:
                st = src.stat()
                src_mtime_ns, src_size = st.st_mtime_ns, st.st_size
                self._decrypt_main_file(src, tmp_base, key)
                if quick_check_sqlite(tmp_base):
                    base_ok = True
                    break
            except OSError:
                pass
            time.sleep(0.25)

        if not base_ok:
            self._unlink_best_effort(tmp_base)
            prev = self._snapshots.get(rel_path)
            if prev and prev.snapshot_path.is_file() and quick_check_sqlite(prev.snapshot_path):
                msg = f"Main database busy during checkpoint; using previous snapshot for {rel_path}"
                self._record_warning(msg)
                return prev
            raise DatabaseCorruptError(
                "db_decrypt_failed",
                f"Failed to decrypt verified snapshot for {rel_path}.",
                {"db": rel_path},
            )

        wal_mtime_ns, wal_size, wal_salt, wal_frames = self._read_wal_meta(wal_path)
        applied_frames = 0
        wal_warning: str | None = None

        if wal_frames > 0 and wal_salt:
            tmp_wal = tmp_base.with_suffix(".wal.tmp")
            merged_ok = False
            for _attempt in range(3):
                try:
                    shutil.copyfile(tmp_base, tmp_wal)
                    wal_mtime_ns, wal_size, wal_salt, _ = self._read_wal_meta(wal_path)
                    applied = self._merge_wal(tmp_wal, wal_path, key, wal_salt, 0)
                    if quick_check_sqlite(tmp_wal):
                        os.replace(tmp_wal, tmp_base)
                        applied_frames = applied
                        merged_ok = True
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            self._unlink_best_effort(tmp_wal)
            if not merged_ok:
                wal_warning = f"WAL merge busy for {rel_path}; falling back to main snapshot."
                self._record_warning(wal_warning)
                applied_frames = -1

        gen, target = self._next_generation_path(rel_path)
        os.replace(tmp_base, target)
        old = self._snapshots.get(rel_path)
        state = SnapshotState(
            src_mtime_ns=src_mtime_ns,
            src_size=src_size,
            wal_mtime_ns=wal_mtime_ns,
            wal_size=wal_size,
            wal_salt=wal_salt,
            applied_frames=applied_frames,
            snapshot_path=target,
            generation=gen,
            wal_warning=wal_warning,
        )
        self._snapshots[rel_path] = state
        if old is not None and old.snapshot_path != target:
            self._unlink_best_effort(old.snapshot_path)
        return state

    def _record_warning(self, msg: str) -> None:
        if msg not in self._warnings:
            self._warnings.append(msg)
            logger.warning("%s", msg)

    def ensure_snapshot(self, rel_path: str, force_rebuild: bool = False) -> Path:
        if self._closed:
            raise RuntimeError("EncryptedDatabasePool is already closed.")
        if rel_path not in self._paths:
            raise KeyError(f"Unknown database: {rel_path}")
        if rel_path not in self._keys:
            raise KeyUnavailableError(
                "key_missing",
                f"No validated decryption key for {rel_path}.",
                {"db": rel_path},
            )

        src = self._paths[rel_path]
        key = self._keys[rel_path]
        wal_path = Path(str(src) + "-wal")

        with self._lock_for(rel_path):
            prev = self._snapshots.get(rel_path)
            if not force_rebuild and prev is not None and prev.snapshot_path.is_file():
                try:
                    st = src.stat()
                    src_mtime_ns, src_size = st.st_mtime_ns, st.st_size
                except OSError:
                    src_mtime_ns, src_size = -1, -1
                wal_mtime_ns, wal_size, wal_salt, wal_frames = self._read_wal_meta(wal_path)

                if (
                    src_mtime_ns == prev.src_mtime_ns
                    and src_size == prev.src_size
                    and wal_mtime_ns == prev.wal_mtime_ns
                    and wal_size == prev.wal_size
                ):
                    return prev.snapshot_path

                # Incremental WAL fast path: main DB untouched, same WAL salt, frames grew
                if (
                    src_mtime_ns == prev.src_mtime_ns
                    and src_size == prev.src_size
                    and prev.applied_frames >= 0
                    and wal_salt
                    and wal_salt == prev.wal_salt
                    and wal_frames >= prev.applied_frames
                ):
                    tmp_inc = (
                        self._session_dir
                        / f"{self._safe_stem(rel_path)}.inc.{os.getpid()}.{threading.get_ident()}.tmp"
                    )
                    try:
                        shutil.copyfile(prev.snapshot_path, tmp_inc)
                        applied = self._merge_wal(
                            tmp_inc, wal_path, key, wal_salt, prev.applied_frames
                        )
                        if quick_check_sqlite(tmp_inc):
                            gen, target = self._next_generation_path(rel_path)
                            os.replace(tmp_inc, target)
                            new_state = SnapshotState(
                                src_mtime_ns=src_mtime_ns,
                                src_size=src_size,
                                wal_mtime_ns=wal_mtime_ns,
                                wal_size=wal_size,
                                wal_salt=wal_salt,
                                applied_frames=applied,
                                snapshot_path=target,
                                generation=gen,
                                wal_warning=None,
                            )
                            self._snapshots[rel_path] = new_state
                            self._unlink_best_effort(prev.snapshot_path)
                            return target
                    except Exception:
                        pass
                    finally:
                        self._unlink_best_effort(tmp_inc)

            state = self._build_full_snapshot(rel_path, src, key)
            return state.snapshot_path

    def open_readonly(self, rel_path: str, force_rebuild: bool = False) -> sqlite3.Connection:
        snap = self.ensure_snapshot(rel_path, force_rebuild=force_rebuild)
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.text_factory = sqlite_text_factory
        return conn

    def invalidate_all(self) -> None:
        for rel, state in list(self._snapshots.items()):
            with self._lock_for(rel):
                self._unlink_best_effort(state.snapshot_path)
                self._snapshots.pop(rel, None)

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._snapshots.clear()
        for attempt in range(4):
            if not self._session_dir.exists():
                return
            try:
                shutil.rmtree(self._session_dir)
                return
            except OSError:
                if attempt < 3:
                    time.sleep(0.1)
        shutil.rmtree(self._session_dir, ignore_errors=True)
