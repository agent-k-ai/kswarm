from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from solders.keypair import Keypair

import kswarm_cli.wallets as wallets
from kswarm_cli.config import PRIVATE_DIR_MODE, ensure_private_dir
from kswarm_cli.wallets import (
    KEY_FILE_MODE,
    InsecureKeyFileError,
    activate_wallet,
    create_wallet,
    list_wallets,
    load_active_wallet,
    load_keypair_file,
    load_wallet,
    resolve_wallet,
    wallet_path,
    write_private_file,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture()
def wallet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated wallet directory with a permissive umask, so tests do not rely on it."""
    wallets_dir = tmp_path / "config" / "wallets"
    monkeypatch.setattr(wallets, "WALLETS_DIR", wallets_dir)
    monkeypatch.setattr(wallets, "ACTIVE_WALLET_PATH", tmp_path / "config" / "active")
    previous = os.umask(0o022)
    try:
        yield wallets_dir
    finally:
        os.umask(previous)


def test_create_wallet_writes_owner_only_file_and_directory(wallet_home: Path) -> None:
    wallet = create_wallet("alice")
    assert wallet.path == wallet_home / "alice.json"
    assert _mode(wallet.path) == KEY_FILE_MODE
    assert _mode(wallet_home) == PRIVATE_DIR_MODE
    assert load_wallet("alice").pubkey == wallet.pubkey


def test_create_wallet_keeps_an_existing_wallet(wallet_home: Path) -> None:
    first = create_wallet("bob")
    assert create_wallet("bob").pubkey == first.pubkey
    assert create_wallet("bob", overwrite=True).pubkey != first.pubkey


def test_create_wallet_tightens_a_loose_directory(wallet_home: Path) -> None:
    wallet_home.mkdir(parents=True)
    os.chmod(wallet_home, 0o755)
    create_wallet("carol")
    assert _mode(wallet_home) == PRIVATE_DIR_MODE


def test_write_private_file_tightens_an_existing_loose_file(tmp_path: Path) -> None:
    target = tmp_path / "key.json"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)
    write_private_file(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert _mode(target) == KEY_FILE_MODE


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
def test_load_wallet_refuses_group_or_world_readable_files(wallet_home: Path, mode: int) -> None:
    wallet = create_wallet("dave")
    os.chmod(wallet.path, mode)
    with pytest.raises(InsecureKeyFileError) as excinfo:
        load_wallet("dave")
    message = str(excinfo.value)
    assert f"mode {mode:04o}" in message
    assert f"chmod 600 {wallet.path}" in message
    assert isinstance(excinfo.value, PermissionError)
    assert not isinstance(excinfo.value, FileNotFoundError)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_load_wallet_accepts_owner_only_files(wallet_home: Path, mode: int) -> None:
    wallet = create_wallet("erin")
    os.chmod(wallet.path, mode)
    assert load_wallet("erin").pubkey == wallet.pubkey


def test_load_keypair_file_reads_solana_cli_and_byte_array_formats(tmp_path: Path) -> None:
    keypair = Keypair()
    solana_format = tmp_path / "solana.json"
    write_private_file(solana_format, keypair.to_json())
    assert load_keypair_file(solana_format).pubkey() == keypair.pubkey()

    byte_array = tmp_path / "bytes.json"
    write_private_file(byte_array, json.dumps(list(bytes(keypair))))
    assert load_keypair_file(byte_array).pubkey() == keypair.pubkey()


def test_load_keypair_file_reports_missing_and_insecure_paths(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_keypair_file(tmp_path / "absent.json")
    loose = tmp_path / "loose.json"
    loose.write_text(Keypair().to_json(), encoding="utf-8")
    os.chmod(loose, 0o644)
    with pytest.raises(InsecureKeyFileError, match="chmod 600"):
        load_keypair_file(loose)
    directory = tmp_path / "dir.json"
    directory.mkdir(mode=0o700)
    with pytest.raises(InsecureKeyFileError, match="not a regular file"):
        load_keypair_file(directory)


def test_list_and_activate_use_private_directories(wallet_home: Path) -> None:
    assert list_wallets() == []
    assert _mode(wallet_home) == PRIVATE_DIR_MODE
    create_wallet("zed")
    create_wallet("amy")
    assert [wallet.name for wallet in list_wallets()] == ["amy", "zed"]
    activate_wallet("amy")
    assert load_active_wallet().name == "amy"
    assert resolve_wallet("zed") == load_wallet("zed").pubkey


def test_list_wallets_refuses_an_insecure_wallet(wallet_home: Path) -> None:
    wallet = create_wallet("frank")
    os.chmod(wallet.path, 0o644)
    with pytest.raises(InsecureKeyFileError, match="frank.json"):
        list_wallets()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x"])
def test_wallet_path_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="invalid wallet name"):
        wallet_path(name)


def test_ensure_private_dir_creates_and_tightens(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        target = tmp_path / "nested" / "dir"
        ensure_private_dir(target)
        assert _mode(target) == PRIVATE_DIR_MODE
        os.chmod(target, 0o755)
        ensure_private_dir(target)
        assert _mode(target) == PRIVATE_DIR_MODE
    finally:
        os.umask(previous)
