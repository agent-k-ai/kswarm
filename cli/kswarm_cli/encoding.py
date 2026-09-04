from __future__ import annotations

import base64
import hashlib
import struct
from decimal import Decimal, ROUND_DOWN
from typing import Any


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def anchor_ix_discriminator(name: str) -> bytes:
    return sha256(f"global:{name}".encode("utf-8"))[:8]


def anchor_account_discriminator(name: str) -> bytes:
    return sha256(f"account:{name}".encode("utf-8"))[:8]


def u8(value: int) -> bytes:
    return struct.pack("<B", value)


def u16(value: int) -> bytes:
    return struct.pack("<H", value)


def u32(value: int) -> bytes:
    return struct.pack("<I", value)


def u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def encode_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return u32(len(data)) + data


def encode_vec(data: bytes) -> bytes:
    return u32(len(data)) + data


def read_u8(data: bytes, offset: int) -> tuple[int, int]:
    return data[offset], offset + 1


def read_u16(data: bytes, offset: int) -> tuple[int, int]:
    return struct.unpack_from("<H", data, offset)[0], offset + 2


def read_u32(data: bytes, offset: int) -> tuple[int, int]:
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def read_u64(data: bytes, offset: int) -> tuple[int, int]:
    return struct.unpack_from("<Q", data, offset)[0], offset + 8


def read_i64(data: bytes, offset: int) -> tuple[int, int]:
    return struct.unpack_from("<q", data, offset)[0], offset + 8


def read_bytes(data: bytes, offset: int, length: int) -> tuple[bytes, int]:
    return data[offset : offset + length], offset + length


def read_string(data: bytes, offset: int) -> tuple[str, int]:
    length, offset = read_u32(data, offset)
    raw, offset = read_bytes(data, offset, length)
    return raw.decode("utf-8"), offset


def read_vec(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = read_u32(data, offset)
    return read_bytes(data, offset, length)


def read_option(data: bytes, offset: int, reader: Any) -> tuple[Any | None, int]:
    if offset >= len(data):
        return None, offset
    tag, offset = read_u8(data, offset)
    if tag == 0:
        return None, offset
    if tag != 1:
        raise ValueError(f"invalid option tag: {tag}")
    return reader(data, offset)


def parse_hash(value: str | None, *, default: bytes | None = None) -> bytes:
    if value is None:
        if default is None:
            raise ValueError("missing 32-byte hash")
        return default
    normalized = value.removeprefix("0x")
    if len(normalized) == 64:
        return bytes.fromhex(normalized)
    if len(value.encode("utf-8")) <= 32:
        out = bytearray(32)
        raw = value.encode("utf-8")
        out[: len(raw)] = raw
        return bytes(out)
    raise ValueError(f"expected 32-byte hex hash or <=32 byte identifier: {value}")


def hash_to_hex(value: bytes | None) -> str | None:
    return value.hex() if value is not None else None


def parse_base_units(value: str | int | float, decimals: int) -> int:
    """Human amount -> base units (truncating extra precision). Zero is allowed."""
    scale = Decimal(10) ** decimals
    amount = (Decimal(str(value)) * scale).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if amount < 0:
        raise ValueError("amount must not be negative")
    return int(amount)


def parse_token_amount(value: str | int | float, decimals: int) -> int:
    """Human amount -> base units for a transfer; must be greater than zero."""
    amount = parse_base_units(value, decimals)
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    return amount


def format_token_amount(amount: int, decimals: int) -> str:
    value = Decimal(amount) / (Decimal(10) ** decimals)
    return format(value.normalize(), "f")


def b64_to_bytes(payload: str) -> bytes:
    return base64.b64decode(payload)
