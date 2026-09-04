"""The legacy branch-reducer binding, computed the way the callback harness computes it.

This module mirrors `protocol/bonsol-branch-reducer`, the guest that commits the
statistics its caller supplied. That guest is not on the aggregate path any more: it
cannot consume an aggregate artifact, so every aggregate job bound to it was opened
unbound and could never settle. `kswarm_cli.aggregate` is the live path.

What is left here is still used:

* `framed_input`, `framed_input_digest` and `journal_hash` are the Bonsol framing and
  journal rules, shared by both guests and by the on-chain program.
* `parse_image_id` and `resolve_aggregate_image_id` resolve the aggregate reducer image
  id from the flag, the environment, or the checked-in pin.
* `decode_score_felt` and `reducer_committed_outputs` mirror the legacy guest for
  `protocol/bonsol-callback-harness`, which still drives the Bonsol callback,
  marker-PDA and replay smoke tests. Those semantics do not depend on which guest ran,
  so the smoke test was left on the guest whose vectors are already recorded.

`settle_aggregate_proof_job` (solana/programs/kswarm_protocol/src/lib.rs) settles an
aggregate-proof job only when the Bonsol marker's `image_id` equals the job's
`required_software_digest`, its `input_digest` equals the job's `input_bundle_hash`,
and its `journal_hash` equals the job's `expected_result_hash`. The program derives
`journal_hash` as `sha256(input_digest || committed_outputs)` from the forwarded Bonsol
payload.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from typing import Any

from kswarm_cli.encoding import sha256
from kswarm_cli.reducer_image import AGGREGATE_REDUCER_IMAGE_ID


_HEX_DIGIT_BYTES = frozenset(b"0123456789abcdef")


IMAGE_ID_ENV = "KSWARM_AGGREGATE_IMAGE_ID"
IMAGE_ID_LEN = 32
FRAME_LEN_BYTES = 8
SCORE_FELT_LEN = 32
COMMITTED_OUTPUTS_LEN = 32 + 4 + 4 + SCORE_FELT_LEN
# BN254 scalar field modulus `r`. `score_hex` is a field element, so it must be reduced.
BN254_SCALAR_MODULUS = 0x30644E72E131A029B85045B68181585D2833E84879B9709143E1F593F0000001
U32_MASK = 0xFFFF_FFFF
U64_MAX = 0xFFFF_FFFF_FFFF_FFFF
# The public input of an aggregate job is the job's own input artifact, framed.
PUBLIC_INPUT_RULE = "input-artifact"
FRAMING_RULE = "u64le-length-prefix"


def parse_image_id(value: str) -> bytes:
    """Harness `decode_image_id`: hex that decodes to exactly 32 bytes."""

    text = value.strip().removeprefix("0x")
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"image id is not hex: {value!r}") from exc
    if len(raw) != IMAGE_ID_LEN:
        raise ValueError(f"image id must decode to {IMAGE_ID_LEN} bytes; got {len(raw)}")
    return raw


def resolve_aggregate_image_id(flag_value: str | None, environ: Mapping[str, str]) -> bytes:
    """`--aggregate-image-id`, then `KSWARM_AGGREGATE_IMAGE_ID`, then the checked-in pin.

    An unset pin is an error, not a zero id. A zero `required_software_digest` lets any
    worker claim the aggregate job and lets no Bonsol marker ever match it, so the job
    would be funded and unsettleable. Rebuild and re-pin with
    `protocol/scripts/build-aggregate-reducer.sh`.
    """

    if flag_value is not None and flag_value.strip():
        return parse_image_id(flag_value)
    env_value = environ.get(IMAGE_ID_ENV, "").strip()
    if env_value:
        try:
            return parse_image_id(env_value)
        except ValueError as exc:
            raise ValueError(f"{IMAGE_ID_ENV}: {exc}") from exc
    if not AGGREGATE_REDUCER_IMAGE_ID.strip():
        raise ValueError(
            "no aggregate reducer image id is pinned: build the guest with "
            "protocol/scripts/build-aggregate-reducer.sh, or pass --aggregate-image-id / set "
            f"{IMAGE_ID_ENV}"
        )
    return parse_image_id(AGGREGATE_REDUCER_IMAGE_ID)


def framed_input(payload: bytes) -> bytes:
    """Harness `framed_input`: little-endian u64 byte length, then the bytes."""

    if len(payload) > U64_MAX:
        raise ValueError("input does not fit a u64 length prefix")
    return struct.pack("<Q", len(payload)) + payload


def framed_input_digest(payload: bytes) -> bytes:
    """`input_digest` = sha256 over the framed input (RISC Zero `Impl::hash_bytes` is SHA-256)."""

    return sha256(framed_input(payload))


def decode_score_felt(score_hex: str) -> bytes:
    """`decode_score_felt` from `protocol/bonsol-branch-reducer/src/lib.rs`.

    `score_hex` is exactly 64 lowercase hex digits, no prefix, the little-endian
    canonical bytes of a BN254 scalar field element, reduced modulo the field.
    The checks run in the Rust order -- every
    byte a lowercase hex digit, then the digit count, then the reduction -- so the
    CLI reports the same reason the guest and the harness would.

    The predecessor of this function read only the last two hex digits. Because the
    encoding is little-endian those are the *most* significant byte, which is 0 for
    every realistic score, so the journal committed 0 (fixed in `fix/proof-binding`).
    """

    digits = score_hex.encode("ascii", errors="replace")
    for byte in digits:
        if byte not in _HEX_DIGIT_BYTES:
            raise ValueError("score_hex rejected: InvalidHexDigit")
    if len(digits) != SCORE_FELT_LEN * 2:
        raise ValueError(f"score_hex rejected: WrongLength {{ digits: {len(digits)} }}")
    score = bytes.fromhex(score_hex)
    # Rust compares little-endian bytes against the modulus from the most significant end.
    if int.from_bytes(score, "little") >= BN254_SCALAR_MODULUS:
        raise ValueError("score_hex rejected: NotReduced")
    return score


def reducer_committed_outputs(input_json: bytes) -> bytes:
    """Harness `reducer_committed_outputs`: the guest journal minus its leading input digest.

    Field defaults follow serde for the four descriptive fields: a missing or
    non-string field reads as "", and a missing, negative, or fractional count
    reads as 0, with a count above u32 truncated (`as u32`). `score_hex` is not
    defaulted: the harness requires it to be a string and to decode as a field
    element. Inputs must be a JSON object; anything else is an error, as in the
    harness.
    """

    try:
        value = json.loads(input_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"reducer input is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("reducer input must be a JSON object")
    branch_key = _json_str(value, "branch_key")
    child_job_id = _json_str(value, "child_job_id")
    parent_request_id = _json_str(value, "parent_request_id")
    line_count = _json_u32(value, "line_count")
    word_count = _json_u32(value, "word_count")
    # `score_hex` is the one field the harness refuses to default: it must be a
    # string and it must decode to a field element.
    raw_score = value.get("score_hex")
    if not isinstance(raw_score, str):
        raise ValueError("score_hex must be a string of 64 lowercase hex digits")
    score = decode_score_felt(raw_score)
    canonical = f"{branch_key}|{child_job_id}|{parent_request_id}|{raw_score}|{line_count}|{word_count}"
    outputs = bytearray(sha256(canonical.encode("utf-8")))
    outputs += struct.pack("<I", line_count)
    outputs += struct.pack("<I", word_count)
    outputs += score
    assert len(outputs) == COMMITTED_OUTPUTS_LEN
    return bytes(outputs)


def journal_hash(input_digest: bytes, committed_outputs: bytes) -> bytes:
    """Program and harness rule: `sha256(input_digest || committed_outputs)`."""

    if len(input_digest) != 32:
        raise ValueError("input digest must be 32 bytes")
    if not committed_outputs:
        raise ValueError("committed outputs must not be empty")
    return sha256(input_digest + committed_outputs)


def _json_str(value: dict[str, Any], key: str) -> str:
    field = value.get(key)
    return field if isinstance(field, str) else ""


def _json_u32(value: dict[str, Any], key: str) -> int:
    field = value.get(key)
    # serde `as_u64`: integers in [0, u64::MAX] only; bool is not a number in JSON.
    if isinstance(field, bool) or not isinstance(field, int) or field < 0 or field > U64_MAX:
        return 0
    return field & U32_MASK
