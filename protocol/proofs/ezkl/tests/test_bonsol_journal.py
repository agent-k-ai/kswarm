"""The Bonsol journal predictor must equal the guest and the harness bit for bit.

Golden values were computed independently with hashlib for the harness
DEFAULT_INPUT_JSON and are pinned in the Rust tests too.
"""

import hashlib
import struct

import pytest

from binding import (
    BONSOL_COMMITTED_OUTPUTS_LENGTH,
    BindingError,
    bonsol_committed_outputs,
    bonsol_framed_input,
    bonsol_journal_hash,
    bonsol_reducer_canonical_bytes,
    score_felt_bytes,
    signed_to_felt_hex,
)
from proof_fixtures import MINUS_ONE_FELT, SCORE_FELT, ZERO_FELT

LOW_BYTE_FELT = "3901000000000000000000000000000000000000000000000000000000000000"
DEFAULT_INPUT_JSON = (
    '{"branch_key":"baseline","child_job_id":"child-baseline-1","parent_request_id":"parent-bonsol-eval",'
    '"line_count":3,"word_count":17,"score_hex":"' + SCORE_FELT + '"}'
)
CLAIM = {
    "branch_key": "baseline",
    "child_job_id": "child-baseline-1",
    "parent_request_id": "parent-bonsol-eval",
    "score_hex": SCORE_FELT,
    "line_count": 3,
    "word_count": 17,
}


def test_score_felt_bytes_are_little_endian_in_string_order():
    assert score_felt_bytes(SCORE_FELT)[:2] == bytes([0x00, 0x3A])
    assert score_felt_bytes(LOW_BYTE_FELT)[:2] == bytes([0x39, 0x01])
    assert score_felt_bytes(ZERO_FELT) == bytes(32)
    assert score_felt_bytes(MINUS_ONE_FELT)[3] == 0xF0


def test_score_felt_regression_low_byte_is_the_first_byte():
    # The legacy decoder read the last two hex digits, which are "00" here.
    assert int(LOW_BYTE_FELT[-2:], 16) == 0
    assert score_felt_bytes(LOW_BYTE_FELT)[0] == 0x39
    assert int.from_bytes(score_felt_bytes(LOW_BYTE_FELT), "little") == 313


@pytest.mark.parametrize("bad", ["", "a", "zz", "0xff", "deadbeef", SCORE_FELT.upper(), "0x" + SCORE_FELT, 7, None])
def test_score_felt_bytes_rejects_malformed(bad):
    with pytest.raises(BindingError):
        score_felt_bytes(bad)


def test_canonical_bytes_pipe_join_with_decimal_counts():
    assert bonsol_reducer_canonical_bytes(**CLAIM) == f"baseline|child-baseline-1|parent-bonsol-eval|{SCORE_FELT}|3|17".encode()


def test_committed_outputs_match_golden_vector():
    outputs = bonsol_committed_outputs(**CLAIM)
    assert len(outputs) == BONSOL_COMMITTED_OUTPUTS_LENGTH == 72
    assert outputs.hex() == "015c09c8aeadb048416fe04d61b50cc187b34eb66e772ea4fff92cdbcf1c2aeb0300000011000000" + SCORE_FELT
    assert hashlib.sha256(outputs).hexdigest() == "76a8ed05cc918de950431cc891b1d316d6d7233b6f9fc951d7e36966e322c1ea"
    assert outputs[32:36] == struct.pack("<I", 3)
    assert outputs[36:40] == struct.pack("<I", 17)


def test_journal_hash_matches_golden_vector():
    framed = bonsol_framed_input(DEFAULT_INPUT_JSON.encode("utf-8"))
    assert framed[:8] == struct.pack("<Q", len(DEFAULT_INPUT_JSON))
    input_digest = hashlib.sha256(framed).digest()
    assert input_digest.hex() == "5ed697e4ca45a8ca9b12f1c439d27f81200bac28ef2b5c404dadb071a2bb2bc4"
    outputs = bonsol_committed_outputs(**CLAIM)
    assert bonsol_journal_hash(input_digest, outputs).hex() == "c1bb642e1996baa57be6534101ec54e6b43ef19252b1a19c48835f1c8f4c2363"


def test_committed_outputs_carry_the_true_low_byte():
    outputs = bonsol_committed_outputs(**{**CLAIM, "score_hex": LOW_BYTE_FELT})
    assert outputs[40] == 0x39
    assert outputs[41] == 0x01
    assert hashlib.sha256(outputs).hexdigest() == "7f955e6c0e172ab986ac3b3d0a09c9f965204fe3eb8d400db5ab41e1b1ba19f6"


def test_bps_score_encodes_as_felt():
    score_hex = signed_to_felt_hex(5000)
    assert score_felt_bytes(score_hex)[:2] == (5000).to_bytes(2, "little")
    assert bonsol_committed_outputs(**{**CLAIM, "score_hex": score_hex})[40:42] == (5000).to_bytes(2, "little")


@pytest.mark.parametrize(
    "patch",
    [
        {"line_count": -1},
        {"line_count": 2**32},
        {"word_count": 1.5},
        {"word_count": True},
        {"branch_key": 1},
        {"score_hex": "deadbeef"},
    ],
)
def test_committed_outputs_reject_bad_fields(patch):
    with pytest.raises(BindingError):
        bonsol_committed_outputs(**{**CLAIM, **patch})


def test_journal_hash_rejects_wrong_sizes():
    outputs = bonsol_committed_outputs(**CLAIM)
    with pytest.raises(BindingError):
        bonsol_journal_hash(b"\x00" * 31, outputs)
    with pytest.raises(BindingError):
        bonsol_journal_hash(b"\x00" * 32, outputs[:-1])
    with pytest.raises(BindingError):
        bonsol_framed_input("text")
