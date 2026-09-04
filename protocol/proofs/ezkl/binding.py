"""Bind an EZKL proof to the branch result it claims to prove.

An EZKL proof only proves that the fixed model maps the public inputs to the
public output. The proof does not say what the inputs mean. This module ties
the public instances in `proof.json` to the branch claim (line count, word
count, score). A verifier must call `bind_bundle` after `ezkl.verify`. If the
instances and the claim do not match, the proof does not cover the claim and
the verifier must reject it.

Encoding facts, verified against ezkl 23.0.5 on 2026-09-03:

- `proof["instances"]` is a list with one list per instance column. With
  `input_visibility = Public` and `output_visibility = Public` there is one
  column. It holds the inputs first, then the outputs.
- Each element is a BN254 scalar field element. It is encoded as the 32
  canonical little-endian bytes as 64 lowercase hex characters, no `0x`.
- Inputs and outputs are fixed-point integers. A float `x` at scale `s` is
  `round_half_away_from_zero(x * 2**s)`. Negative values wrap modulo the field.
- `settings.json` carries `run_args.input_visibility`, `run_args.output_visibility`,
  `run_args.param_visibility`, `model_instance_shapes`, `model_input_scales`,
  and `model_output_scales`.

This module has no dependency on the `ezkl` package so it can be unit tested
without the prover binary.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

BN254_SCALAR_MODULUS = 0x30644E72E131A029B85045B68181585D2833E84879B9709143E1F593F0000001
FELT_BYTE_LENGTH = 32
FELT_HEX_LENGTH = FELT_BYTE_LENGTH * 2
BUNDLE_VERSION = "kswarm-ezkl-proof-v1"
REQUIRED_VISIBILITY = {
    "input_visibility": "Public",
    "output_visibility": "Public",
    "param_visibility": "Fixed",
}
INPUT_COUNT = 2
OUTPUT_COUNT = 1
U32_MAX = 0xFFFFFFFF
BONSOL_COMMITTED_OUTPUTS_LENGTH = 32 + 4 + 4 + FELT_BYTE_LENGTH
_HEX_DIGITS = frozenset("0123456789abcdef")


class BindingError(ValueError):
    """The proof instances do not bind to the claim, or the inputs are malformed."""


@dataclass(frozen=True)
class BranchClaim:
    """The values a branch result claims the proof covers."""

    line_count: float
    word_count: float
    score_hex: str


@dataclass(frozen=True)
class BoundInstances:
    """The decoded public instances after they matched the claim."""

    line_count: int
    word_count: int
    score: int
    score_hex: str
    input_scale: int
    output_scale: int

    @property
    def score_value(self) -> float:
        return dequantize(self.score, self.output_scale)


def felt_hex_to_int(felt_hex: Any) -> int:
    """Decode one EZKL field element hex string into an unsigned integer."""
    if not isinstance(felt_hex, str):
        raise BindingError(f"field element must be a string, got {type(felt_hex).__name__}")
    if len(felt_hex) != FELT_HEX_LENGTH:
        raise BindingError(f"field element must be {FELT_HEX_LENGTH} hex characters, got {len(felt_hex)}")
    if any(char not in _HEX_DIGITS for char in felt_hex):
        raise BindingError("field element must be lowercase hex without a prefix")
    value = int.from_bytes(bytes.fromhex(felt_hex), "little")
    if value >= BN254_SCALAR_MODULUS:
        raise BindingError("field element is not reduced modulo the BN254 scalar field")
    return value


def felt_to_signed(value: int) -> int:
    """Map a field element to the signed integer EZKL encoded into it."""
    if value < 0 or value >= BN254_SCALAR_MODULUS:
        raise BindingError("field element out of range")
    if value > (BN254_SCALAR_MODULUS - 1) // 2:
        return value - BN254_SCALAR_MODULUS
    return value


def signed_to_felt_hex(value: int) -> str:
    """Encode a signed integer the way EZKL encodes an instance element."""
    if abs(value) > (BN254_SCALAR_MODULUS - 1) // 2:
        raise BindingError("integer does not fit the signed field range")
    return (value % BN254_SCALAR_MODULUS).to_bytes(FELT_BYTE_LENGTH, "little").hex()


def quantize(value: float, scale: int) -> int:
    """Fixed-point quantization with EZKL's round-half-away-from-zero rule."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BindingError(f"value must be numeric, got {type(value).__name__}")
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 0:
        raise BindingError("scale must be a non-negative integer")
    scaled = float(value) * float(2**scale)
    if not math.isfinite(scaled):
        raise BindingError("value is not finite")
    magnitude = abs(scaled)
    floored = math.floor(magnitude)
    if magnitude - floored >= 0.5:
        floored += 1
    return -floored if scaled < 0 else floored


def dequantize(value: int, scale: int) -> float:
    return value / float(2**scale)


def _read_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BindingError(f"settings.{key} must be an integer")
    return value


def _single_scale(settings: Mapping[str, Any], key: str) -> int:
    scales = settings.get(key)
    if not isinstance(scales, list) or len(scales) != 1:
        raise BindingError(f"settings.{key} must hold exactly one scale")
    return _read_int({key: scales[0]}, key)


def _shape_size(shape: Any) -> int:
    if not isinstance(shape, list) or not shape:
        raise BindingError("settings.model_instance_shapes entries must be non-empty lists")
    size = 1
    for dimension in shape:
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 0:
            raise BindingError("settings.model_instance_shapes dimensions must be non-negative integers")
        size *= dimension
    return size


def check_settings(settings: Mapping[str, Any]) -> tuple[int, int]:
    """Check the settings expose inputs and outputs and return (input_scale, output_scale)."""
    if not isinstance(settings, Mapping):
        raise BindingError("settings must be an object")
    run_args = settings.get("run_args")
    if not isinstance(run_args, Mapping):
        raise BindingError("settings.run_args missing")
    for key, required in REQUIRED_VISIBILITY.items():
        actual = run_args.get(key)
        if actual != required:
            raise BindingError(f"settings.run_args.{key} must be {required!r}, got {actual!r}")
    shapes = settings.get("model_instance_shapes")
    if not isinstance(shapes, list) or len(shapes) != 2:
        raise BindingError("settings.model_instance_shapes must list one input shape and one output shape")
    if _shape_size(shapes[0]) != INPUT_COUNT:
        raise BindingError(f"model input must hold {INPUT_COUNT} elements")
    if _shape_size(shapes[1]) != OUTPUT_COUNT:
        raise BindingError(f"model output must hold {OUTPUT_COUNT} element")
    return _single_scale(settings, "model_input_scales"), _single_scale(settings, "model_output_scales")


def _instance_column(instances: Any) -> Sequence[Any]:
    if not isinstance(instances, list) or len(instances) != 1:
        raise BindingError("proof instances must hold exactly one instance column")
    column = instances[0]
    if not isinstance(column, list):
        raise BindingError("proof instance column must be a list")
    expected = INPUT_COUNT + OUTPUT_COUNT
    if len(column) != expected:
        raise BindingError(f"proof instance column must hold {expected} elements, got {len(column)}")
    return column


def bind_instances(instances: Any, settings: Mapping[str, Any], claim: BranchClaim) -> BoundInstances:
    """Fail closed unless the public instances equal the encoded claim."""
    input_scale, output_scale = check_settings(settings)
    column = _instance_column(instances)
    decoded = [felt_to_signed(felt_hex_to_int(element)) for element in column]
    expected_line = quantize(claim.line_count, input_scale)
    expected_word = quantize(claim.word_count, input_scale)
    if decoded[0] != expected_line:
        raise BindingError(f"line_count instance {decoded[0]} != claim {expected_line} (scale {input_scale})")
    if decoded[1] != expected_word:
        raise BindingError(f"word_count instance {decoded[1]} != claim {expected_word} (scale {input_scale})")
    if not isinstance(claim.score_hex, str) or column[2] != claim.score_hex:
        raise BindingError("score_hex claim does not equal the output instance")
    return BoundInstances(
        line_count=decoded[0],
        word_count=decoded[1],
        score=decoded[2],
        score_hex=column[2],
        input_scale=input_scale,
        output_scale=output_scale,
    )


def claim_from_bundle(bundle: Mapping[str, Any]) -> BranchClaim:
    if not isinstance(bundle, Mapping):
        raise BindingError("bundle must be an object")
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise BindingError(f"bundle_version must be {BUNDLE_VERSION!r}")
    features = bundle.get("features")
    if not isinstance(features, Mapping):
        raise BindingError("bundle.features missing")
    for key in ("line_count", "word_count"):
        value = features.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BindingError(f"bundle.features.{key} must be numeric")
    score_hex = bundle.get("score_hex")
    if not isinstance(score_hex, str):
        raise BindingError("bundle.score_hex must be a string")
    return BranchClaim(
        line_count=float(features["line_count"]),
        word_count=float(features["word_count"]),
        score_hex=score_hex,
    )


def bind_bundle(bundle: Mapping[str, Any], proof: Mapping[str, Any], settings: Mapping[str, Any]) -> BoundInstances:
    """Bind proof.json to bundle.json. The bundle is the prover's claim."""
    claim = claim_from_bundle(bundle)
    if not isinstance(proof, Mapping):
        raise BindingError("proof must be an object")
    instances = proof.get("instances")
    if instances is None:
        raise BindingError("proof.instances missing")
    if bundle.get("public_instances") != instances:
        raise BindingError("bundle.public_instances does not equal proof.instances")
    return bind_instances(instances, settings, claim)


def check_expected(bound: BoundInstances, expected: BranchClaim) -> None:
    """Compare bound instances with a claim supplied by an outer manifest."""
    expected_line = quantize(expected.line_count, bound.input_scale)
    expected_word = quantize(expected.word_count, bound.input_scale)
    if bound.line_count != expected_line:
        raise BindingError(f"expected line_count {expected_line} != bound {bound.line_count}")
    if bound.word_count != expected_word:
        raise BindingError(f"expected word_count {expected_word} != bound {bound.word_count}")
    if bound.score_hex != expected.score_hex:
        raise BindingError("expected score_hex != bound score_hex")


# --- Bonsol reducer journal contract -------------------------------------
#
# Mirrors protocol/bonsol-branch-reducer/src/lib.rs. The guest journal is
# `input_digest (32) || committed_outputs (72)`. Bonsol forwards the committed
# outputs to the callback; the on-chain program hashes them and never parses
# them. Every predictor of the journal hash must use this exact layout.


def score_felt_bytes(score_hex: Any) -> bytes:
    """The 32 little-endian bytes the Bonsol guest commits for `score_hex`."""
    felt_hex_to_int(score_hex)
    return bytes.fromhex(score_hex)


def _u32(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > U32_MAX:
        raise BindingError(f"{label} must be an integer in [0, 2**32)")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise BindingError(f"{label} must be a string")
    return value


def bonsol_reducer_canonical_bytes(
    branch_key: str,
    child_job_id: str,
    parent_request_id: str,
    score_hex: str,
    line_count: int,
    word_count: int,
) -> bytes:
    """The bytes whose SHA-256 is the Bonsol guest's `reducer_digest`."""
    parts = (
        _text(branch_key, "branch_key"),
        _text(child_job_id, "child_job_id"),
        _text(parent_request_id, "parent_request_id"),
        _text(score_hex, "score_hex"),
        str(_u32(line_count, "line_count")),
        str(_u32(word_count, "word_count")),
    )
    return "|".join(parts).encode("utf-8")


def bonsol_committed_outputs(
    branch_key: str,
    child_job_id: str,
    parent_request_id: str,
    score_hex: str,
    line_count: int,
    word_count: int,
) -> bytes:
    """`reducer_digest (32) || line_count le32 || word_count le32 || score (32)`."""
    score = score_felt_bytes(score_hex)
    canonical = bonsol_reducer_canonical_bytes(branch_key, child_job_id, parent_request_id, score_hex, line_count, word_count)
    reducer_digest = hashlib.sha256(canonical).digest()
    outputs = reducer_digest + struct.pack("<I", line_count) + struct.pack("<I", word_count) + score
    if len(outputs) != BONSOL_COMMITTED_OUTPUTS_LENGTH:
        raise BindingError("committed outputs have the wrong length")
    return outputs


def bonsol_framed_input(payload: bytes) -> bytes:
    """Bonsol public input framing: `len le64 || payload`. Its SHA-256 is the input digest."""
    if not isinstance(payload, (bytes, bytearray)):
        raise BindingError("payload must be bytes")
    return struct.pack("<Q", len(payload)) + bytes(payload)


def bonsol_journal_hash(input_digest: bytes, committed_outputs: bytes) -> bytes:
    """The hash the on-chain program stores: `sha256(input_digest || committed_outputs)`."""
    if not isinstance(input_digest, (bytes, bytearray)) or len(input_digest) != 32:
        raise BindingError("input_digest must be 32 bytes")
    if not isinstance(committed_outputs, (bytes, bytearray)) or len(committed_outputs) != BONSOL_COMMITTED_OUTPUTS_LENGTH:
        raise BindingError(f"committed_outputs must be {BONSOL_COMMITTED_OUTPUTS_LENGTH} bytes")
    return hashlib.sha256(bytes(input_digest) + bytes(committed_outputs)).digest()
