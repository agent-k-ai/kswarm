"""The CLI's aggregate binding must equal what the Bonsol callback harness computes.

`tests/vectors/bonsol_harness_vectors.json` was produced by running
`protocol/bonsol-callback-harness prepare` (offline) for several inputs; see
`vectors/README.md` for the exact commands. The on-chain program derives the
journal hash as `sha256(input_digest || committed_outputs)`; the harness uses
the same rule for `prepare-production`.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

import pytest

from kswarm_cli.bonsol import (
    COMMITTED_OUTPUTS_LEN,
    FRAMING_RULE,
    IMAGE_ID_ENV,
    PUBLIC_INPUT_RULE,
    AggregateBinding,
    bind_aggregate_input,
    SCORE_FELT_LEN,
    decode_score_felt,
    try_bind_aggregate_input,
    framed_input,
    framed_input_digest,
    journal_hash,
    parse_image_id,
    reducer_committed_outputs,
    resolve_aggregate_image_id,
)
from kswarm_cli.reducer_image import AGGREGATE_REDUCER_IMAGE_ID


VECTORS = Path(__file__).with_name("vectors") / "bonsol_harness_vectors.json"
VALID_SCORE_HEX = "3901000000000000000000000000000000000000000000000000000000000000"
HARNESS_DEFAULT_INPUT = (
    '{"branch_key":"baseline","child_job_id":"child-baseline-1","parent_request_id":"parent-bonsol-eval",'
    f'"line_count":3,"word_count":17,"score_hex":"{VALID_SCORE_HEX}"}}'
)


def _vectors() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]


def test_vectors_file_is_present_and_non_empty() -> None:
    assert VECTORS.exists(), f"missing harness vectors: {VECTORS}"
    assert len(_vectors()) >= 4


def _accepted() -> list[dict]:
    return [vector for vector in _vectors() if vector["accepted"]]


def _rejected() -> list[dict]:
    return [vector for vector in _vectors() if not vector["accepted"]]


def test_vectors_cover_both_outcomes() -> None:
    assert len(_accepted()) >= 4, "need accepted vectors"
    assert len(_rejected()) >= 4, "need rejected vectors: the rejection taxonomy is part of the contract"


@pytest.mark.parametrize("vector", _rejected() if VECTORS.exists() else [], ids=lambda vector: vector["name"])
def test_cli_rejects_exactly_what_the_harness_rejects(vector: dict) -> None:
    """A harness rejection must be a CLI rejection, with the same stated reason.

    `score_hex` became a required BN254 field element in `fix/proof-binding`. If the
    CLI kept the old lenient rule it would compute a journal hash for an input the
    reducer refuses to run, and `settle_aggregate_proof_job` would reject the job
    forever. The reason string is asserted so the two implementations cannot drift
    into agreeing by accident.
    """

    with pytest.raises(ValueError) as excinfo:
        reducer_committed_outputs(vector["input_json"].encode("utf-8"))
    harness_reason = vector["error"].removeprefix("Error: ").strip('"')
    assert str(excinfo.value) == harness_reason, f"{vector['name']}: CLI said {excinfo.value!r}, harness said {harness_reason!r}"


@pytest.mark.parametrize("vector", _accepted() if VECTORS.exists() else [], ids=lambda vector: vector["name"])
def test_binding_matches_harness_prepare(vector: dict) -> None:
    input_json = vector["input_json"].encode("utf-8")
    prepared = vector["prepared"]
    assert framed_input(input_json).hex() == prepared["framedInputHex"]
    assert framed_input_digest(input_json).hex() == prepared["executionConfigInputHash"]
    assert framed_input_digest(input_json).hex() == prepared["callbackInputDigest"]
    outputs = reducer_committed_outputs(input_json)
    assert outputs.hex() == prepared["committedOutputs"]
    assert hashlib.sha256(outputs).hexdigest() == prepared["committedOutputsDigest"]
    image_id = bytes.fromhex(prepared["imageIdBytesHex"])
    binding = bind_aggregate_input(image_id, input_json)
    assert binding.image_id.hex() == prepared["imageIdBytesHex"]
    assert binding.input_digest.hex() == prepared["callbackInputDigest"]
    assert binding.output_digest.hex() == prepared["committedOutputsDigest"]
    expected_journal = hashlib.sha256(bytes.fromhex(prepared["callbackInputDigest"]) + bytes.fromhex(prepared["committedOutputs"])).hexdigest()
    assert binding.journal_hash.hex() == expected_journal
    if "journalHash" in vector:
        assert binding.journal_hash.hex() == vector["journalHash"]


def test_framed_input_is_u64_le_length_then_bytes() -> None:
    assert framed_input(b"") == bytes(8)
    assert framed_input(b"abc") == struct.pack("<Q", 3) + b"abc"
    assert framed_input_digest(b"abc") == hashlib.sha256(struct.pack("<Q", 3) + b"abc").digest()


def test_reducer_outputs_layout_for_the_harness_default_input() -> None:
    outputs = reducer_committed_outputs(HARNESS_DEFAULT_INPUT.encode("utf-8"))
    assert len(outputs) == COMMITTED_OUTPUTS_LEN == 32 + 4 + 4 + SCORE_FELT_LEN == 72
    canonical = f"baseline|child-baseline-1|parent-bonsol-eval|{VALID_SCORE_HEX}|3|17".encode("utf-8")
    assert outputs[:32] == hashlib.sha256(canonical).digest()
    assert outputs[32:36] == struct.pack("<I", 3)
    assert outputs[36:40] == struct.pack("<I", 17)
    # The whole field element, little-endian, not one byte of it.
    assert outputs[40:] == bytes.fromhex(VALID_SCORE_HEX)
    assert outputs[40] == 0x39


def test_decode_score_felt_accepts_a_reduced_64_digit_value() -> None:
    assert decode_score_felt(VALID_SCORE_HEX) == bytes.fromhex(VALID_SCORE_HEX)
    assert decode_score_felt("00" * 32) == bytes(32)


@pytest.mark.parametrize(
    "score_hex, reason",
    [
        ("0x" + "39" + "00" * 31, "InvalidHexDigit"),
        ("f", "WrongLength { digits: 1 }"),
        ("", "WrongLength { digits: 0 }"),
        ("z" * 64, "InvalidHexDigit"),
        ("DEADBEEF" + "00" * 28, "InvalidHexDigit"),
        ("ff" * 32, "NotReduced"),
        ("de" * 31, "WrongLength { digits: 62 }"),
    ],
)
def test_decode_score_felt_rejects_what_the_reducer_rejects(score_hex: str, reason: str) -> None:
    """The old decoder read the last two hex digits and defaulted to 0 for all of these."""

    with pytest.raises(ValueError, match=re.escape(reason)):
        decode_score_felt(score_hex)


def test_reducer_outputs_use_serde_defaults_for_the_descriptive_fields_only() -> None:
    """Only the four descriptive fields default; `score_hex` is required, as in the harness."""

    minimal = json.dumps({"score_hex": VALID_SCORE_HEX}).encode()
    outputs = reducer_committed_outputs(minimal)
    assert outputs[:32] == hashlib.sha256(f"|||{VALID_SCORE_HEX}|0|0".encode()).digest()
    assert outputs[32:40] == bytes(8)
    mistyped = reducer_committed_outputs(json.dumps({"line_count": -1, "word_count": 2.5, "branch_key": None, "score_hex": VALID_SCORE_HEX}).encode())
    assert mistyped == outputs
    truncated = reducer_committed_outputs(json.dumps({"line_count": 2**32 + 5, "word_count": 2**64 - 1, "score_hex": VALID_SCORE_HEX}).encode())
    assert truncated[32:36] == struct.pack("<I", 5)
    assert truncated[36:40] == struct.pack("<I", 0xFFFF_FFFF)
    assert truncated[:32] == hashlib.sha256(f"|||{VALID_SCORE_HEX}|5|4294967295".encode()).digest()
    assert reducer_committed_outputs(json.dumps({"line_count": 2**64, "score_hex": VALID_SCORE_HEX}).encode()) == outputs
    assert reducer_committed_outputs(json.dumps({"line_count": True, "score_hex": VALID_SCORE_HEX}).encode()) == outputs


def test_score_hex_is_required_like_the_harness() -> None:
    for payload in (b"{}", b'{"branch_key":"only"}', b'{"score_hex": 12}', b'{"score_hex": null}'):
        with pytest.raises(ValueError, match="score_hex must be a string of 64 lowercase hex digits"):
            reducer_committed_outputs(payload)


def test_reducer_outputs_reject_non_object_inputs() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        reducer_committed_outputs(b"[1, 2]")
    with pytest.raises(ValueError, match="not JSON"):
        reducer_committed_outputs(b"{")
    with pytest.raises(ValueError, match="not JSON"):
        reducer_committed_outputs(b"\xff")


def test_journal_hash_rule() -> None:
    digest = bytes(range(32))
    assert journal_hash(digest, b"\x01") == hashlib.sha256(digest + b"\x01").digest()
    with pytest.raises(ValueError, match="32 bytes"):
        journal_hash(b"short", b"\x01")
    with pytest.raises(ValueError, match="empty"):
        journal_hash(digest, b"")


def test_bind_aggregate_input_is_internally_consistent() -> None:
    image_id = bytes.fromhex(AGGREGATE_REDUCER_IMAGE_ID)
    payload = json.dumps({"schema_version": 2, "combiner": "weighted-mean", "score_hex": VALID_SCORE_HEX}).encode()
    binding = bind_aggregate_input(image_id, payload)
    assert isinstance(binding, AggregateBinding)
    assert binding.input_digest == framed_input_digest(payload)
    assert binding.committed_outputs == reducer_committed_outputs(payload)
    assert binding.output_digest == hashlib.sha256(binding.committed_outputs).digest()
    assert binding.journal_hash == journal_hash(binding.input_digest, binding.committed_outputs)
    as_json = binding.to_json()
    assert as_json["image_id"] == AGGREGATE_REDUCER_IMAGE_ID
    assert as_json["public_input"] == PUBLIC_INPUT_RULE == "input-artifact"
    assert as_json["framing"] == FRAMING_RULE == "u64le-length-prefix"
    assert set(as_json) == {"image_id", "input_digest", "committed_outputs", "output_digest", "journal_hash", "public_input", "framing"}
    with pytest.raises(ValueError, match="32 bytes"):
        bind_aggregate_input(b"\x00" * 31, payload)


def test_try_bind_reports_an_input_the_reducer_would_reject() -> None:
    """`predict open`'s own aggregate artifact is not a branch-reducer input.

    It carries the branch job list, the combiner and its parameters, and no
    `score_hex`. Binding it to the branch reducer produced a value only while the
    reducer defaulted every missing field; the reducer now refuses it, so there is no
    binding to write and `predict open` opens the job unbound instead of funding a job
    that provably cannot settle.
    """

    image_id = bytes.fromhex(AGGREGATE_REDUCER_IMAGE_ID)
    aggregate_artifact = next(v for v in _vectors() if v["name"] == "cli-aggregate-input")
    assert aggregate_artifact["accepted"] is False
    binding, reason = try_bind_aggregate_input(image_id, aggregate_artifact["input_json"].encode("utf-8"))
    assert binding is None
    assert reason == aggregate_artifact["error"].removeprefix("Error: ").strip('"')

    bound, reason = try_bind_aggregate_input(image_id, HARNESS_DEFAULT_INPUT.encode("utf-8"))
    assert bound is not None and reason == ""


def test_parse_image_id_matches_harness_decode_image_id() -> None:
    raw = parse_image_id(AGGREGATE_REDUCER_IMAGE_ID)
    assert raw.hex() == AGGREGATE_REDUCER_IMAGE_ID
    assert parse_image_id("0x" + AGGREGATE_REDUCER_IMAGE_ID.upper()) == raw
    assert parse_image_id(f"  {AGGREGATE_REDUCER_IMAGE_ID}\n") == raw
    with pytest.raises(ValueError, match="32 bytes"):
        parse_image_id("abcd")
    with pytest.raises(ValueError, match="not hex"):
        parse_image_id("zz" * 32)


def test_resolve_aggregate_image_id_precedence() -> None:
    default = bytes.fromhex(AGGREGATE_REDUCER_IMAGE_ID)
    other = "11" * 32
    third = "22" * 32
    assert resolve_aggregate_image_id(None, {}) == default
    assert resolve_aggregate_image_id("", {}) == default
    assert resolve_aggregate_image_id(None, {IMAGE_ID_ENV: other}) == bytes.fromhex(other)
    assert resolve_aggregate_image_id(third, {IMAGE_ID_ENV: other}) == bytes.fromhex(third)
    with pytest.raises(ValueError, match=IMAGE_ID_ENV):
        resolve_aggregate_image_id(None, {IMAGE_ID_ENV: "nope"})
    with pytest.raises(ValueError, match="not hex"):
        resolve_aggregate_image_id("nope", {})
    assert len(default) == 32
