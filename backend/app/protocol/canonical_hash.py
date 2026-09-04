from __future__ import annotations

import hashlib
import json
import math
import struct
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import BaseModel

from .branch_schemas import BranchOutput, CanonicalHash


RESULT_MAGIC = b"MFB2"
RESULT_SCHEMA_VERSION = 2
OUTPUT_KIND_IDS = {"scalar": 1, "categorical": 2, "narrative_with_scalar": 3}
OUTPUT_KIND_BY_ID = {value: key for key, value in OUTPUT_KIND_IDS.items()}

FLAG_SCALAR = 1 << 0
FLAG_LOWER = 1 << 1
FLAG_UPPER = 1 << 2
FLAG_CATEGORY = 1 << 3
FLAG_SCORES = 1 << 4


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize JSON deterministically: sorted keys, no whitespace, UTF-8."""

    normalized = _normalize_for_json(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def snap_scalar_to_bps(x: float) -> int:
    """Clamp a scalar to [0, 1] and round to nearest basis point."""

    if not math.isfinite(x):
        raise ValueError("scalar must be finite")
    clamped = min(1.0, max(0.0, float(x)))
    snapped = Decimal(str(clamped)) * Decimal(10000)
    return int(snapped.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_scalar_to_bps(value: Any, *, field: str = "scalar") -> int:
    """Parse a JSON scalar number or numeric string and snap it to basis points."""

    if isinstance(value, bool):
        raise ValueError(f"{field} must be a JSON number or numeric string")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{field} must be numeric; got empty string")
        try:
            numeric = float(stripped)
        except ValueError as exc:
            raise ValueError(f"{field} must be numeric; got {value!r}") from exc
    elif isinstance(value, (int, float)):
        numeric = float(value)
    else:
        raise ValueError(f"{field} must be a JSON number or numeric string")
    return snap_scalar_to_bps(numeric)


def branch_output_result_bytes(output: BranchOutput) -> bytes:
    """Encode the compact MFB2 branch result submitted to `submit_receipt`."""

    canonical_digest = CanonicalHash.of(output)
    kind_id = OUTPUT_KIND_IDS[output.output_kind]
    flags = 0
    scalar_value = output.scalar_value_bps
    lower = output.scalar_confidence_lower_bps
    upper = output.scalar_confidence_upper_bps
    category = output.categorical_label_index
    scores = output.narrative_scores or {}

    if scalar_value is not None:
        flags |= FLAG_SCALAR
    if lower is not None:
        flags |= FLAG_LOWER
    if upper is not None:
        flags |= FLAG_UPPER
    if category is not None:
        flags |= FLAG_CATEGORY
    if scores:
        flags |= FLAG_SCORES

    out = bytearray()
    out.extend(RESULT_MAGIC)
    out.extend(struct.pack("<BBIB", RESULT_SCHEMA_VERSION, kind_id, output.branch_index, flags))
    if scalar_value is not None:
        out.extend(_u16_bps(scalar_value, "scalar_value_bps"))
    if lower is not None:
        out.extend(_u16_bps(lower, "scalar_confidence_lower_bps"))
    if upper is not None:
        out.extend(_u16_bps(upper, "scalar_confidence_upper_bps"))
    if category is not None:
        out.extend(struct.pack("<B", category))
    if scores:
        if len(scores) > 32:
            raise ValueError("too many narrative score guardrails for MFB2")
        out.extend(struct.pack("<B", len(scores)))
        for key in sorted(scores):
            out.extend(hashlib.sha256(key.encode("utf-8")).digest()[:4])
            out.extend(_u16_bps(scores[key], key))
    out.extend(canonical_digest)
    if len(out) > 512:
        raise ValueError("MFB2 result exceeds protocol MAX_RESULT_BYTES")
    return bytes(out)


def branch_result_hash(output: BranchOutput) -> bytes:
    """Return the hash the Solana program stores for `result_bytes`."""

    return hashlib.sha256(branch_output_result_bytes(output)).digest()


def parse_branch_output_result_bytes(data: bytes) -> dict[str, Any]:
    """Decode the compact MFB2 branch result for report and aggregator runners."""

    if len(data) < 4 + 1 + 1 + 4 + 1 + 32:
        raise ValueError("result bytes too short")
    if data[:4] != RESULT_MAGIC:
        raise ValueError("unsupported branch result magic")
    offset = 4
    version, kind_id, branch_index, flags = struct.unpack_from("<BBIB", data, offset)
    offset += struct.calcsize("<BBIB")
    if version != RESULT_SCHEMA_VERSION:
        raise ValueError(f"unsupported MFB result version: {version}")
    if kind_id not in OUTPUT_KIND_BY_ID:
        raise ValueError(f"unsupported output kind id: {kind_id}")
    decoded: dict[str, Any] = {
        "schema": RESULT_MAGIC.decode("ascii"),
        "schema_version": version,
        "output_kind": OUTPUT_KIND_BY_ID[kind_id],
        "branch_index": branch_index,
    }
    if flags & FLAG_SCALAR:
        decoded["scalar_value_bps"], offset = _read_u16(data, offset)
    if flags & FLAG_LOWER:
        decoded["scalar_confidence_lower_bps"], offset = _read_u16(data, offset)
    if flags & FLAG_UPPER:
        decoded["scalar_confidence_upper_bps"], offset = _read_u16(data, offset)
    if flags & FLAG_CATEGORY:
        decoded["categorical_label_index"] = data[offset]
        offset += 1
    if flags & FLAG_SCORES:
        count = data[offset]
        offset += 1
        decoded["narrative_score_hashes"] = []
        for _ in range(count):
            key_hash = data[offset : offset + 4].hex()
            offset += 4
            value, offset = _read_u16(data, offset)
            decoded["narrative_score_hashes"].append({"key_hash": key_hash, "value_bps": value})
    decoded["canonical_hash"] = data[offset : offset + 32].hex()
    offset += 32
    if offset != len(data):
        raise ValueError("trailing bytes in MFB2 result")
    return decoded


def _normalize_for_json(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _normalize_for_json(obj.model_dump(mode="json", exclude_none=False))
    if isinstance(obj, dict):
        return {str(key): _normalize_for_json(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_for_json(value) for value in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError("non-finite float cannot be canonicalized")
    return obj


def _u16_bps(value: int, field: str) -> bytes:
    if value < 0 or value > 10000:
        raise ValueError(f"{field} outside basis-point range")
    return struct.pack("<H", value)


def _read_u16(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(data):
        raise ValueError("truncated u16")
    return struct.unpack_from("<H", data, offset)[0], offset + 2
