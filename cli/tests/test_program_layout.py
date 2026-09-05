"""The hand-rolled encoders and decoders must match the program source.

There is no usable IDL on this branch (the program crate does not enable
`idl-build`, so `anchor idl build` cannot run), so this test derives every
instruction's argument layout, account order, signer/writable flags, and every
account struct's field layout from `solana/programs/kswarm_protocol/src/lib.rs`
and checks the CLI against them.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from kswarm_cli import protocol as cli_protocol
from kswarm_cli.constants import KAI_MAINNET_MINT, KSWARM_PROGRAM_ID, TOKEN_PROGRAM_ID
from kswarm_cli.encoding import anchor_account_discriminator, anchor_ix_discriminator
from kswarm_cli.protocol import (
    InitializeProtocolArgs,
    JobAccount,
    ProtocolAddresses,
    assign_verifier_ix,
    cancel_aggregate_proof_job_ix,
    cancel_open_job_ix,
    challenge_job_ix,
    claim_customer_slash_compensation_ix,
    claim_job_ix,
    claim_verifier_slash_reward_ix,
    commit_input_artifact_ix,
    decode_config,
    decode_job,
    decode_marker,
    decode_worker,
    deposit_worker_stake_ix,
    initialize_protocol_ix,
    open_job_ix,
    reassign_verifier_ix,
    record_aggregate_verification_raw_ix,
    refund_slashed_job_escrow_ix,
    register_worker_ix,
    settle_aggregate_proof_job_ix,
    settle_job_ix,
    slash_stale_job_ix,
    submit_receipt_ix,
    submit_verifier_attestation_ix,
    withdraw_unlocked_stake_ix,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_RS = REPO_ROOT / "solana" / "programs" / "kswarm_protocol" / "src" / "lib.rs"
CLI_PROTOCOL_PY = Path(cli_protocol.__file__)
# The one program id, so a rotation cannot leave a test behind.
PROGRAM_ID = KSWARM_PROGRAM_ID

pytestmark = pytest.mark.skipif(not LIB_RS.exists(), reason=f"program source not checked out: {LIB_RS}")


# --- Rust source parsing -------------------------------------------------------


def _source() -> str:
    lines = [line for line in LIB_RS.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("//")]
    return "\n".join(lines)


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([<{":
            depth += 1
        elif char in ")]>}":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def parse_program_instructions(source: str) -> dict[str, tuple[str | None, list[tuple[str, str]]]]:
    """`name -> (Context struct, [(param, type), ...])` for every `pub fn` in the `#[program]` module."""

    start = source.index("#[program]")
    end = source.index("\n}\n", start)
    block = source[start:end]
    instructions: dict[str, tuple[str | None, list[tuple[str, str]]]] = {}
    for match in re.finditer(r"pub fn (\w+)(?:<'info>)?\s*\((.*?)\)\s*->", block, re.S):
        name, params = match.group(1), match.group(2)
        context: str | None = None
        args: list[tuple[str, str]] = []
        for param in _split_top_level(params):
            param_name, param_type = (item.strip() for item in param.split(":", 1))
            if param_type.startswith("Context<"):
                context = param_type[len("Context<") : -1]
            elif context is not None:
                args.append((param_name, param_type))
        if context is not None:
            instructions[name] = (context, args)
    return instructions


FIELD_RE = re.compile(r"(?:#\[account\((?P<attr>.*?)\)\]\s*)?(?:#\[max_len\([^)]*\)\]\s*)?pub (?P<name>\w+): (?P<type>[^,\n]+),", re.S)


def parse_struct(source: str, name: str) -> list[dict[str, Any]]:
    match = re.search(rf"pub struct {name}(?:<'info>)? \{{\n(.*?)\n\}}", source, re.S)
    assert match, f"struct {name} not found in lib.rs"
    fields = []
    for field in FIELD_RE.finditer(match.group(1)):
        attr = " ".join((field.group("attr") or "").split())
        tokens = {token.strip() for token in _split_top_level(attr)}
        fields.append(
            {
                "name": field.group("name"),
                "type": field.group("type").strip(),
                "writable": bool(tokens & {"mut", "init", "init_if_needed"}),
                "signer": field.group("type").strip().startswith("Signer<"),
            }
        )
    assert fields, f"struct {name} has no fields"
    return fields


# --- Borsh by layout -------------------------------------------------------------


def _encode(source: str, type_name: str, value: Any) -> bytes:
    if type_name == "u8":
        return struct.pack("<B", value)
    if type_name == "u16":
        return struct.pack("<H", value)
    if type_name == "u32":
        return struct.pack("<I", value)
    if type_name == "u64":
        return struct.pack("<Q", value)
    if type_name == "i64":
        return struct.pack("<q", value)
    if type_name == "bool":
        return b"\x01" if value else b"\x00"
    if type_name == "Pubkey":
        return bytes(value)
    if type_name == "String":
        raw = value.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw
    if type_name == "Vec<u8>":
        return struct.pack("<I", len(value)) + bytes(value)
    array = re.fullmatch(r"\[u8; (\d+)\]", type_name)
    if array:
        assert len(value) == int(array.group(1))
        return bytes(value)
    option = re.fullmatch(r"Option<(.+)>", type_name)
    if option:
        return b"\x00" if value is None else b"\x01" + _encode(source, option.group(1), value)
    if re.fullmatch(r"\w+", type_name):
        return b"".join(_encode(source, field["type"], value[field["name"]]) for field in parse_struct(source, type_name))
    raise AssertionError(f"unsupported Rust type {type_name}")


def _decode(source: str, type_name: str, data: bytes, offset: int) -> tuple[Any, int]:
    scalars = {"u8": "<B", "u16": "<H", "u32": "<I", "u64": "<Q", "i64": "<q"}
    if type_name in scalars:
        fmt = scalars[type_name]
        return struct.unpack_from(fmt, data, offset)[0], offset + struct.calcsize(fmt)
    if type_name == "bool":
        return data[offset] == 1, offset + 1
    if type_name == "Pubkey":
        return Pubkey.from_bytes(data[offset : offset + 32]), offset + 32
    if type_name in {"String", "Vec<u8>"}:
        length = struct.unpack_from("<I", data, offset)[0]
        raw = data[offset + 4 : offset + 4 + length]
        return (raw.decode("utf-8") if type_name == "String" else raw), offset + 4 + length
    array = re.fullmatch(r"\[u8; (\d+)\]", type_name)
    if array:
        length = int(array.group(1))
        return data[offset : offset + length], offset + length
    option = re.fullmatch(r"Option<(.+)>", type_name)
    if option:
        if data[offset] == 0:
            return None, offset + 1
        return _decode(source, option.group(1), data, offset + 1)
    if re.fullmatch(r"\w+", type_name):
        out: dict[str, Any] = {}
        for field in parse_struct(source, type_name):
            out[field["name"]], offset = _decode(source, field["type"], data, offset)
        return out, offset
    raise AssertionError(f"unsupported Rust type {type_name}")


def decode_instruction_args(source: str, args: list[tuple[str, str]], data: bytes) -> dict[str, Any]:
    """Flat `field -> value` for the instruction data after the discriminator; every byte must be consumed."""

    offset = 8
    out: dict[str, Any] = {}
    for name, type_name in args:
        value, offset = _decode(source, type_name, data, offset)
        if isinstance(value, dict):
            out.update(value)
        else:
            out[name] = value
    assert offset == len(data), f"{len(data) - offset} trailing bytes"
    return out


# --- fixtures ------------------------------------------------------------------


H = lambda byte: bytes([byte]) * 32  # noqa: E731
PROTO = ProtocolAddresses(PROGRAM_ID, KAI_MAINNET_MINT, TOKEN_PROGRAM_ID)
CUSTOMER = Keypair().pubkey()
WORKER_AUTHORITY = Keypair().pubkey()
VERIFIER_AUTHORITY = Keypair().pubkey()
CHALLENGER = Keypair().pubkey()
JOB_KEY = PROTO.job_pda(CUSTOMER, 7)
MARKER = Keypair().pubkey()
FLOORS = InitializeProtocolArgs(1, 2, 3, 4, 5)


def _job(**overrides: Any) -> JobAccount:
    values: dict[str, Any] = {
        "bump": 1,
        "nonce": 7,
        "customer": CUSTOMER,
        "worker": WORKER_AUTHORITY,
        "status": 3,
        "reward_amount": 10,
        "required_stake": 20,
        "job_class": 2,
        "required_role": 2,
        "required_tier": 1,
        "required_capability_class_hash": H(1),
        "required_software_digest": H(2),
        "created_at": 100,
        "claim_deadline": 200,
        "execution_window_seconds": 30,
        "execute_deadline": 300,
        "challenge_window_seconds": 40,
        "challenge_deadline": 400,
        "challenge_bond": 50,
        "challenger": CHALLENGER,
        "slash_settled": False,
        "escrow_refunded": False,
        "verifier_reward_paid": False,
        "customer_slash_paid": False,
        "input_bundle_hash": H(3),
        "expected_result_hash": H(4),
        "submitted_result_hash": H(5),
        "input_cid": "bafyin",
        "output_cid": "bafyout",
        "result_bytes": b"\x0a\x0b",
        "verifier_authority": None,
        "verifier_attestation_hash": None,
        "verifier_evidence_cid": None,
        "verifier_attestation_unix": None,
        "assigned_verifier_authority": None,
        "assigned_verifier_unix": None,
        "reassignment_counter": 0,
    }
    values.update(overrides)
    return JobAccount(**values)


JOB = _job()

# instruction name -> (CLI instruction, expected decoded args keyed by the Rust field names)
CASES: dict[str, tuple[Any, dict[str, Any]]] = {
    "initialize_protocol": (
        initialize_protocol_ix(PROTO, CUSTOMER, FLOORS),
        {
            "tier_one_stake_floor": 1,
            "tier_two_stake_floor": 2,
            "tier_three_stake_floor": 3,
            "verifier_stake_floor": 4,
            "min_challenge_window_seconds": 5,
        },
    ),
    "register_worker": (
        register_worker_ix(PROTO, WORKER_AUTHORITY, 2, H(1), H(2)),
        {"role": 2, "capability_class_hash": H(1), "software_digest": H(2)},
    ),
    "deposit_worker_stake": (deposit_worker_stake_ix(PROTO, WORKER_AUTHORITY, 55), {"amount": 55}),
    "withdraw_unlocked_stake": (withdraw_unlocked_stake_ix(PROTO, WORKER_AUTHORITY, 66), {"amount": 66}),
    "open_job": (
        open_job_ix(PROTO, CUSTOMER, 7, H(3), H(4), 10, 20, 2, 2, 1, H(1), H(2), 30, 31, 32, 50),
        {
            "job_nonce": 7,
            "input_bundle_hash": H(3),
            "expected_result_hash": H(4),
            "reward_amount": 10,
            "required_stake": 20,
            "job_class": 2,
            "required_role": 2,
            "required_tier": 1,
            "required_capability_class_hash": H(1),
            "required_software_digest": H(2),
            "claim_window_seconds": 30,
            "execution_window_seconds": 31,
            "challenge_window_seconds": 32,
            "challenge_bond": 50,
        },
    ),
    "commit_input_artifact": (commit_input_artifact_ix(PROGRAM_ID, CUSTOMER, JOB_KEY, "bafyin"), {"input_cid": "bafyin"}),
    "claim_job": (claim_job_ix(PROTO, WORKER_AUTHORITY, JOB_KEY), {}),
    "submit_receipt": (
        submit_receipt_ix(PROGRAM_ID, WORKER_AUTHORITY, JOB_KEY, "bafyout", b"\x0a\x0b"),
        {"output_cid": "bafyout", "result_bytes": b"\x0a\x0b"},
    ),
    "submit_verifier_attestation": (
        submit_verifier_attestation_ix(PROTO, VERIFIER_AUTHORITY, JOB_KEY, H(5), "bafyev", H(2)),
        {"verifier_result_hash": H(5), "verifier_evidence_cid": "bafyev", "verifier_software_digest": H(2)},
    ),
    "assign_verifier": (assign_verifier_ix(PROGRAM_ID, CUSTOMER, JOB_KEY, VERIFIER_AUTHORITY), {"verifier_authority": VERIFIER_AUTHORITY}),
    "reassign_verifier": (reassign_verifier_ix(PROGRAM_ID, CUSTOMER, JOB_KEY), {}),
    "settle_aggregate_proof_job": (settle_aggregate_proof_job_ix(PROTO, CUSTOMER, JOB_KEY, JOB, MARKER), {}),
    "cancel_aggregate_proof_job": (cancel_aggregate_proof_job_ix(PROTO, CUSTOMER, JOB_KEY, JOB), {}),
    "settle_job": (settle_job_ix(PROTO, CUSTOMER, JOB_KEY, JOB), {}),
    "challenge_job": (challenge_job_ix(PROTO, VERIFIER_AUTHORITY, JOB_KEY, JOB), {}),
    "refund_slashed_job_escrow": (refund_slashed_job_escrow_ix(PROTO, CUSTOMER, JOB_KEY, JOB), {}),
    "claim_verifier_slash_reward": (claim_verifier_slash_reward_ix(PROTO, CHALLENGER, JOB_KEY, JOB), {}),
    "claim_customer_slash_compensation": (claim_customer_slash_compensation_ix(PROTO, CUSTOMER, JOB_KEY, JOB), {}),
    "cancel_open_job": (cancel_open_job_ix(PROTO, CUSTOMER, JOB_KEY), {}),
    "slash_stale_job": (slash_stale_job_ix(PROTO, CUSTOMER, JOB_KEY, JOB), {}),
}


@pytest.fixture(scope="module")
def source() -> str:
    return _source()


@pytest.fixture(scope="module")
def instructions(source: str) -> dict[str, tuple[str | None, list[tuple[str, str]]]]:
    return parse_program_instructions(source)


# --- tests ---------------------------------------------------------------------


def test_every_cli_instruction_name_exists_in_the_program(instructions: dict) -> None:
    cli_names = set(re.findall(r'anchor_ix_discriminator\("(\w+)"\)', CLI_PROTOCOL_PY.read_text(encoding="utf-8")))
    assert cli_names, "no instruction names found in protocol.py"
    assert cli_names <= set(instructions), f"CLI encodes instructions the program does not have: {cli_names - set(instructions)}"
    # The CLI wraps every Anchor-dispatched instruction. The Bonsol callback is not
    # Anchor-dispatched: PR-3 deleted the unreachable Anchor `record_aggregate_verification`,
    # so the raw fallback is the only path, and it is wrapped separately below.
    assert set(instructions) == cli_names


def test_every_cli_account_discriminator_names_a_program_account(source: str) -> None:
    cli_names = set(re.findall(r'anchor_account_discriminator\("(\w+)"\)', CLI_PROTOCOL_PY.read_text(encoding="utf-8")))
    program_accounts = set(re.findall(r"#\[account\]\s*(?:#\[derive\([^)]*\)\]\s*)?pub struct (\w+)", source))
    assert cli_names == program_accounts == {"ProtocolConfig", "Worker", "Job", "BonsolAggregateVerification"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_instruction_data_matches_program_args(source: str, instructions: dict, name: str) -> None:
    ix, expected = CASES[name]
    _, args = instructions[name]
    assert ix.program_id == PROGRAM_ID
    assert bytes(ix.data[:8]) == anchor_ix_discriminator(name)
    assert decode_instruction_args(source, args, bytes(ix.data)) == expected


@pytest.mark.parametrize("name", sorted(CASES))
def test_instruction_accounts_match_program_context(source: str, instructions: dict, name: str) -> None:
    ix, _ = CASES[name]
    context, _ = instructions[name]
    fields = parse_struct(source, context)
    assert len(ix.accounts) == len(fields), f"{name}: CLI passes {len(ix.accounts)} accounts, {context} declares {len(fields)}"
    for meta, field in zip(ix.accounts, fields):
        assert meta.is_signer == field["signer"], f"{name}.{field['name']}: signer flag"
        assert meta.is_writable == field["writable"], f"{name}.{field['name']}: writable flag"


def test_instruction_accounts_carry_the_expected_keys(source: str, instructions: dict) -> None:
    """Named accounts the CLI derives (PDAs, ATAs, programs) sit where the program expects them."""

    named = {
        "config": PROTO.config_pda(),
        "payment_mint": KAI_MAINNET_MINT,
        "token_program": TOKEN_PROGRAM_ID,
        "system_program": Pubkey.from_string("11111111111111111111111111111111"),
        "associated_token_program": Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
        "job": JOB_KEY,
        "job_escrow_vault": PROTO.job_escrow_vault(JOB_KEY),
        "bonsol_aggregate_verification": MARKER,
    }
    for name, (ix, _) in CASES.items():
        context, _ = instructions[name]
        for meta, field in zip(ix.accounts, parse_struct(source, context)):
            if field["name"] in named:
                assert meta.pubkey == named[field["name"]], f"{name}.{field['name']}"


def test_raw_record_aggregate_verification_matches_the_fallback(source: str) -> None:
    raw_ix_byte = int(re.search(r"const RECORD_AGGREGATE_VERIFICATION_RAW_IX: u8 = (\d+);", source).group(1))
    execution_id = b"p0g-test".ljust(32, b"\0")
    ix = record_aggregate_verification_raw_ix(PROGRAM_ID, VERIFIER_AUTHORITY, MARKER, JOB_KEY, execution_id, H(1), H(2), H(3), H(4), b"\xaa\xbb")
    data = bytes(ix.data)
    assert data[0] == raw_ix_byte
    args, offset = _decode(source, "RecordAggregateVerificationArgs", data, 1)
    assert args == {"execution_id": execution_id, "image_id": H(1), "input_digest": H(2), "output_digest": H(3), "journal_hash": H(4)}
    assert data[offset:] == b"\xaa\xbb"
    raw_fn = source[source.index("fn record_aggregate_verification_raw<'info>(") :]
    raw_fn = raw_fn[: raw_fn.index("\n}\n")]
    account_names = re.findall(r"let (\w+) = next_account_info", raw_fn)
    assert account_names[:2] == ["bonsol_execution_account", "aggregate_verification"]
    assert len(ix.accounts) == 4
    assert ix.accounts[0].is_signer and not ix.accounts[0].is_writable
    assert ix.accounts[1].is_writable and ix.accounts[1].pubkey == MARKER
    assert ix.accounts[2].pubkey == JOB_KEY


@pytest.mark.parametrize(
    "struct_name, decoder, sample",
    [
        (
            "ProtocolConfig",
            decode_config,
            {
                "bump": 254,
                "admin": CUSTOMER,
                "payment_mint": KAI_MAINNET_MINT,
                "token_program": TOKEN_PROGRAM_ID,
                "payment_decimals": 6,
                "tier_one_stake_floor": 1,
                "tier_two_stake_floor": 2,
                "tier_three_stake_floor": 3,
                "verifier_stake_floor": 4,
                "min_challenge_window_seconds": 5,
            },
        ),
        (
            "Worker",
            decode_worker,
            {
                "bump": 250,
                "authority": WORKER_AUTHORITY,
                "stake_vault": MARKER,
                "locked_stake": 500,
                "active_claims": 2,
                "registered_at": 1234,
                "status": 1,
                "role": 2,
                "capability_class_hash": H(1),
                "software_digest": H(2),
            },
        ),
        (
            "BonsolAggregateVerification",
            decode_marker,
            {
                "bump": 255,
                "aggregate_job": JOB_KEY,
                "execution_id": b"p0g".ljust(32, b"\0"),
                "image_id": H(1),
                "input_digest": H(2),
                "output_digest": H(3),
                "journal_hash": H(4),
                "callback_unix": 999,
                "status": 1,
            },
        ),
    ],
)
def test_account_decoders_match_program_layouts(source: str, struct_name: str, decoder, sample: dict[str, Any]) -> None:
    fields = parse_struct(source, struct_name)
    assert [field["name"] for field in fields] == list(sample)
    data = anchor_account_discriminator(struct_name) + _encode(source, struct_name, sample)
    account = decoder(data)
    for field in fields:
        assert getattr(account, field["name"]) == sample[field["name"]], field["name"]


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "verifier_authority": VERIFIER_AUTHORITY,
            "verifier_attestation_hash": H(9),
            "verifier_evidence_cid": "bafyev",
            "verifier_attestation_unix": 4321,
            "assigned_verifier_authority": VERIFIER_AUTHORITY,
            "assigned_verifier_unix": 4444,
            "reassignment_counter": 2,
            "slash_settled": True,
            "escrow_refunded": True,
            "verifier_reward_paid": True,
            "customer_slash_paid": True,
            "result_bytes": bytes(range(64)),
        },
    ],
)
def test_job_decoder_matches_program_layout(source: str, overrides: dict[str, Any]) -> None:
    job = _job(**overrides)
    fields = parse_struct(source, "Job")
    assert [field["name"] for field in fields] == list(JobAccount.__dataclass_fields__)
    sample = {field["name"]: getattr(job, field["name"]) for field in fields}
    data = anchor_account_discriminator("Job") + _encode(source, "Job", sample)
    assert decode_job(data) == job
