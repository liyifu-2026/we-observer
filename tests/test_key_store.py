"""Tests for Windows DPAPI key storage and external key import."""

from __future__ import annotations

import json
import sys

import pytest

from tests.fixtures import SyntheticAccountEnv
from wx_observer.exceptions import KeyUnavailableError
from wx_observer.key_store import KeyStore, load_external_keys


def test_dpapi_round_trip_and_tamper_fail_closed(tmp_path) -> None:
    if sys.platform != "win32":
        pytest.skip("DPAPI is Windows-only")

    env = SyntheticAccountEnv(tmp_path / "wx")
    db_files = {
        rel: env.storage_dir / rel
        for rel in ("contact/contact.db", "session/session.db", "message/message_0.db", "message/message_1.db")
    }
    raw_keys = {rel: bytes.fromhex(h) for rel, h in env.keys_hex.items()}

    ks = KeyStore(state_dir=tmp_path / "state")
    cache_file = ks.save_cached_keys(env.account_id, raw_keys)
    assert cache_file.is_file()
    raw_bytes = cache_file.read_bytes()
    assert b"contact/contact.db" not in raw_bytes  # must not be plaintext on disk

    loaded = ks.load_cached_keys(env.account_id, db_files)
    assert loaded == raw_keys

    # Tampered DPAPI blob must fail closed and return empty dict
    cache_file.write_bytes(raw_bytes[:-8] + b"XXXXXXXX")
    assert ks.load_cached_keys(env.account_id, db_files) == {}

    # Plaintext JSON pretending to be a cache without DPAPI prefix must NOT be accepted
    cache_file.write_text(json.dumps({"keys": env.keys_hex}), encoding="utf-8")
    assert ks.load_cached_keys(env.account_id, db_files) == {}


def test_external_master_key_derivation_and_invalid_file(tmp_path) -> None:
    env = SyntheticAccountEnv(tmp_path / "wx")
    db_files = {rel: env.storage_dir / rel for rel in env.keys_hex}

    master_file = tmp_path / "master_keys.json"
    master_file.write_text(
        json.dumps({"master_key": env.master_key.hex()}),
        encoding="utf-8",
    )
    derived = load_external_keys(master_file, db_files)
    assert set(derived.keys()) == set(db_files.keys())
    for rel, k in derived.items():
        assert k.hex() == env.keys_hex[rel]

    # Wrong master key produces empty validated map
    bad_master = tmp_path / "bad_master.json"
    bad_master.write_text(json.dumps({"master_key": "99" * 32}), encoding="utf-8")
    assert load_external_keys(bad_master, db_files) == {}

    with pytest.raises(KeyUnavailableError):
        load_external_keys(tmp_path / "missing.json", db_files)
