from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from kswarm_cli.config import ACTIVE_WALLET_PATH, WALLETS_DIR, ensure_private_dir

KEY_FILE_MODE = 0o600
_LOOSE_PERMISSION_BITS = 0o077


class InsecureKeyFileError(PermissionError):
    """A key file is readable by group or others, or is not a regular file."""


@dataclass(frozen=True)
class NamedWallet:
    name: str
    path: Path
    keypair: Keypair

    @property
    def pubkey(self) -> Pubkey:
        return self.keypair.pubkey()


def wallet_path(name: str) -> Path:
    if "/" in name or name in {"", ".", ".."}:
        raise ValueError(f"invalid wallet name: {name}")
    return WALLETS_DIR / f"{name}.json"


def create_wallet(name: str, *, overwrite: bool = False) -> NamedWallet:
    path = wallet_path(name)
    if path.exists() and not overwrite:
        return load_wallet(name)
    keypair = Keypair()
    ensure_private_dir(path.parent)
    write_private_file(path, keypair.to_json())
    return NamedWallet(name=name, path=path, keypair=keypair)


def load_wallet(name: str) -> NamedWallet:
    path = wallet_path(name)
    if not path.exists():
        raise FileNotFoundError(f"wallet does not exist: {name}")
    return NamedWallet(name=name, path=path, keypair=load_keypair_file(path))


def write_private_file(path: Path, text: str) -> None:
    """Write `text` so that only the owner can read it, even when the file already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KEY_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        # O_CREAT applies the umask and leaves an existing file's mode alone; fchmod fixes both.
        os.fchmod(fd, KEY_FILE_MODE)
        handle.write(text)


def assert_private_key_file(path: Path) -> None:
    """Refuse a key file that anyone but its owner can read."""
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise InsecureKeyFileError(f"key file is not a regular file: {path}")
    if os.name != "posix":
        return
    mode = stat.S_IMODE(info.st_mode)
    if mode & _LOOSE_PERMISSION_BITS:
        raise InsecureKeyFileError(
            f"key file {path} is readable by group or others (mode {mode:04o}); refusing to load it. "
            f"Fix with: chmod 600 {path}"
        )


def load_keypair_file(path: Path) -> Keypair:
    path = path.expanduser()
    if not path.exists():
        raise FileNotFoundError(f"key file does not exist: {path}")
    assert_private_key_file(path)
    raw = path.read_text(encoding="utf-8")
    try:
        return Keypair.from_json(raw)
    except ValueError:
        data = json.loads(raw)
        return Keypair.from_bytes(bytes(data))


def list_wallets() -> list[NamedWallet]:
    ensure_private_dir(WALLETS_DIR)
    return [load_wallet(path.stem) for path in sorted(WALLETS_DIR.glob("*.json"))]


def activate_wallet(name: str) -> None:
    load_wallet(name)
    ensure_private_dir(ACTIVE_WALLET_PATH.parent)
    ACTIVE_WALLET_PATH.write_text(f"{name}\n", encoding="utf-8")


def active_wallet_name() -> str | None:
    if not ACTIVE_WALLET_PATH.exists():
        return None
    value = ACTIVE_WALLET_PATH.read_text(encoding="utf-8").strip()
    return value or None


def load_active_wallet() -> NamedWallet:
    name = active_wallet_name()
    if not name:
        raise FileNotFoundError("no active wallet; run `kswarm wallet activate <name>`")
    return load_wallet(name)


def resolve_wallet(name_or_pubkey: str) -> Pubkey:
    path = wallet_path(name_or_pubkey)
    if path.exists():
        return load_wallet(name_or_pubkey).pubkey
    return Pubkey.from_string(name_or_pubkey)
