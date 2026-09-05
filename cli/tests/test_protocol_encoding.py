from __future__ import annotations

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID

from kswarm_cli.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    BPF_LOADER_UPGRADEABLE_PROGRAM_ID,
    JOB_STATUS,
    KAI_MAINNET_MINT,
    KSWARM_PROGRAM_ID,
    PROTOCOL_ERRORS,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    ZERO_HASH,
)
from kswarm_cli.encoding import (
    anchor_account_discriminator,
    anchor_ix_discriminator,
    format_token_amount,
    parse_base_units,
    parse_token_amount,
    u32,
    u64,
    u8,
)
from kswarm_cli.protocol import (
    InitializeProtocolArgs,
    JobAccount,
    ProtocolAddresses,
    ProtocolConfigAccount,
    cancel_aggregate_proof_job_ix,
    claim_job_ix,
    config_pda,
    decode_config,
    initialize_protocol_ix,
    job_pda,
    min_challenge_window_default,
    parse_tier_floors,
    program_data_pda,
    register_worker_ix,
    stake_floors_from_human,
    validate_stake_floors,
    worker_pda,
)
from kswarm_cli.rpc import _structured_error_code
from kswarm_cli.spl_token import associated_token_address


PROGRAM_ID = KSWARM_PROGRAM_ID
OTHER_PROGRAM_ID = Pubkey.from_string("BoNsHRcyLLNdtnoDf8hiCNZpyehMC4FDMxs6NTxFi3ew")
DEFAULT_FLOORS = InitializeProtocolArgs(50_000_000_000, 250_000_000_000, 1_000_000_000_000, 100_000_000_000, 5)


def _proto(token_program: Pubkey = TOKEN_PROGRAM_ID) -> ProtocolAddresses:
    return ProtocolAddresses(PROGRAM_ID, KAI_MAINNET_MINT, token_program)


def test_initialize_args_encode_as_four_little_endian_u64_then_the_window_floor() -> None:
    data = DEFAULT_FLOORS.to_bytes()
    assert len(data) == 36
    assert data == (
        u64(50_000_000_000)
        + u64(250_000_000_000)
        + u64(1_000_000_000_000)
        + u64(100_000_000_000)
        + u32(5)
    )
    assert DEFAULT_FLOORS.to_json() == {
        "tier_one_stake_floor": 50_000_000_000,
        "tier_two_stake_floor": 250_000_000_000,
        "tier_three_stake_floor": 1_000_000_000_000,
        "verifier_stake_floor": 100_000_000_000,
        "min_challenge_window_seconds": 5,
    }


def test_program_data_pda_derives_under_upgradeable_loader() -> None:
    expected, _ = Pubkey.find_program_address([bytes(PROGRAM_ID)], BPF_LOADER_UPGRADEABLE_PROGRAM_ID)
    assert program_data_pda(PROGRAM_ID) == expected
    assert program_data_pda(PROGRAM_ID) != program_data_pda(OTHER_PROGRAM_ID)


def test_initialize_protocol_ix_layout() -> None:
    admin = Keypair().pubkey()
    proto = _proto()
    ix = initialize_protocol_ix(proto, admin, DEFAULT_FLOORS)
    assert ix.program_id == PROGRAM_ID
    assert ix.data == anchor_ix_discriminator("initialize_protocol") + DEFAULT_FLOORS.to_bytes()
    assert [meta.pubkey for meta in ix.accounts] == [
        admin,
        config_pda(PROGRAM_ID),
        KAI_MAINNET_MINT,
        TOKEN_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
        PROGRAM_ID,
        program_data_pda(PROGRAM_ID),
    ]
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.accounts[1].is_writable
    assert not ix.accounts[5].is_writable and not ix.accounts[5].is_signer
    assert not ix.accounts[6].is_writable and not ix.accounts[6].is_signer


def _job_account(*, customer: Pubkey, worker: Pubkey, status: int = 4) -> JobAccount:
    return JobAccount(
        bump=255,
        nonce=1,
        customer=customer,
        worker=worker,
        status=status,
        reward_amount=10,
        required_stake=20,
        job_class=4,
        required_role=2,
        required_tier=1,
        required_capability_class_hash=ZERO_HASH,
        required_software_digest=ZERO_HASH,
        created_at=0,
        claim_deadline=60,
        execution_window_seconds=60,
        execute_deadline=120,
        challenge_window_seconds=5,
        challenge_deadline=125,
        challenge_bond=5,
        challenger=Pubkey.default(),
        slash_settled=False,
        escrow_refunded=False,
        verifier_reward_paid=False,
        customer_slash_paid=False,
        input_bundle_hash=ZERO_HASH,
        expected_result_hash=ZERO_HASH,
        submitted_result_hash=ZERO_HASH,
        input_cid="",
        output_cid="",
        result_bytes=b"",
        verifier_authority=None,
        verifier_attestation_hash=None,
        verifier_evidence_cid=None,
        verifier_attestation_unix=None,
        assigned_verifier_authority=None,
        assigned_verifier_unix=None,
        reassignment_counter=0,
    )


def test_cancel_aggregate_proof_job_ix_layout_binds_worker() -> None:
    customer = Keypair().pubkey()
    worker_authority = Keypair().pubkey()
    proto = _proto()
    job_key = job_pda(PROGRAM_ID, customer, 1)
    job = _job_account(customer=customer, worker=worker_authority)
    ix = cancel_aggregate_proof_job_ix(proto, customer, job_key, job)
    assert ix.data == anchor_ix_discriminator("cancel_aggregate_proof_job")
    worker = worker_pda(PROGRAM_ID, worker_authority)
    assert [meta.pubkey for meta in ix.accounts] == [
        customer,
        config_pda(PROGRAM_ID),
        KAI_MAINNET_MINT,
        job_key,
        proto.job_escrow_vault(job_key),
        proto.ata(customer),
        TOKEN_PROGRAM_ID,
        worker,
        worker_authority,
    ]
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.accounts[7].is_writable and not ix.accounts[7].is_signer
    assert not ix.accounts[8].is_writable and not ix.accounts[8].is_signer


def test_job_status_names_cover_cancelled_on_timeout() -> None:
    assert JOB_STATUS[9] == "cancelled-on-timeout"
    assert JOB_STATUS[8] == "cancelled-on-exhaustion"


def _config_bytes(
    *,
    bump: int = 254,
    admin: Pubkey,
    payment_mint: Pubkey = KAI_MAINNET_MINT,
    token_program: Pubkey = TOKEN_PROGRAM_ID,
    decimals: int = 6,
    floors: InitializeProtocolArgs = DEFAULT_FLOORS,
) -> bytes:
    return (
        anchor_account_discriminator("ProtocolConfig")
        + u8(bump)
        + bytes(admin)
        + bytes(payment_mint)
        + bytes(token_program)
        + u8(decimals)
        + floors.to_bytes()
    )


def test_decode_config_reads_new_layout() -> None:
    admin = Keypair().pubkey()
    account = decode_config(_config_bytes(admin=admin))
    assert account == ProtocolConfigAccount(
        bump=254,
        admin=admin,
        payment_mint=KAI_MAINNET_MINT,
        token_program=TOKEN_PROGRAM_ID,
        payment_decimals=6,
        tier_one_stake_floor=50_000_000_000,
        tier_two_stake_floor=250_000_000_000,
        tier_three_stake_floor=1_000_000_000_000,
        verifier_stake_floor=100_000_000_000,
        min_challenge_window_seconds=5,
    )
    assert account.to_json()["token_program"] == str(TOKEN_PROGRAM_ID)
    assert account.addresses(PROGRAM_ID) == _proto()


def test_decode_config_reads_token_2022_layout() -> None:
    account = decode_config(_config_bytes(admin=Keypair().pubkey(), token_program=TOKEN_2022_PROGRAM_ID, decimals=9))
    assert account.token_program == TOKEN_2022_PROGRAM_ID
    assert account.payment_decimals == 9


def test_decode_config_rejects_wrong_discriminator() -> None:
    data = anchor_account_discriminator("Worker") + _config_bytes(admin=Keypair().pubkey())[8:]
    with pytest.raises(ValueError, match="ProtocolConfig"):
        decode_config(data)


def test_parse_tier_floors_accepts_three_values() -> None:
    assert parse_tier_floors("50000,250000,1000000") == ("50000", "250000", "1000000")
    assert parse_tier_floors(" 1 , 2 , 3 ") == ("1", "2", "3")


@pytest.mark.parametrize("text", ["1,2", "1,2,3,4", "1,,3", ""])
def test_parse_tier_floors_rejects_wrong_arity(text: str) -> None:
    with pytest.raises(ValueError, match="three"):
        parse_tier_floors(text)


def test_stake_floors_from_human_uses_mint_decimals() -> None:
    assert stake_floors_from_human(("50000", "250000", "1000000"), "100000", 6, 5) == DEFAULT_FLOORS
    nine = stake_floors_from_human(("500", "2500", "10000"), "1000", 9, 5)
    assert nine.tier_one_stake_floor == 500 * 10**9
    assert nine.verifier_stake_floor == 1000 * 10**9
    fractional = stake_floors_from_human(("0.5", "1", "1.5"), "0.25", 6, 5)
    assert fractional.tier_one_stake_floor == 500_000
    assert fractional.verifier_stake_floor == 250_000


@pytest.mark.parametrize(
    "tiers, verifier, message",
    [
        (("0", "2", "3"), "1", "0 < tier one"),
        (("2", "2", "3"), "1", "tier one < tier two"),
        (("1", "3", "2"), "1", "tier two < tier three"),
        (("1", "2", "3"), "0", "verifier"),
    ],
)
def test_stake_floors_from_human_rejects_bad_floors(tiers: tuple[str, str, str], verifier: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        stake_floors_from_human(tiers, verifier, 6, 5)


def test_validate_stake_floors_rejects_u64_overflow() -> None:
    with pytest.raises(ValueError, match="u64"):
        validate_stake_floors(InitializeProtocolArgs(1, 2, 2**64, 1, 5))


@pytest.mark.parametrize("seconds", [0, -1, 2**32])
def test_validate_stake_floors_rejects_an_unusable_challenge_window_floor(seconds: int) -> None:
    """A zero floor would restore the unbounded `challenge_window_seconds > 0` behaviour."""
    with pytest.raises(ValueError, match="minimum challenge window"):
        validate_stake_floors(InitializeProtocolArgs(1, 2, 3, 4, seconds))


def test_min_challenge_window_default_is_small_locally_and_a_full_ladder_on_mainnet() -> None:
    # Local stays fast; devnet holds one attestation rung plus a challenge tail; mainnet
    # holds one rung per verifier the ladder can carry, plus the tail.
    attestation_window = 7200
    max_reassignments = 3
    assert min_challenge_window_default("local") == 5
    assert min_challenge_window_default("devnet") == 2 * attestation_window
    assert min_challenge_window_default("mainnet") == (max_reassignments + 2) * attestation_window
    # An unrecognized profile must not silently get the local value.
    assert min_challenge_window_default("staging") == min_challenge_window_default("mainnet")


def test_initialize_protocol_args_encode_the_challenge_window_floor() -> None:
    """The floor is the trailing u32 of the instruction payload."""
    args = InitializeProtocolArgs(1, 2, 3, 4, 36_000)
    assert args.to_bytes()[-4:] == u32(36_000)
    assert len(args.to_bytes()) == 4 * 8 + 4


def test_protocol_addresses_derive_atas_with_their_token_program() -> None:
    owner = Keypair().pubkey()
    classic = _proto(TOKEN_PROGRAM_ID)
    token_2022 = _proto(TOKEN_2022_PROGRAM_ID)
    assert classic.ata(owner) == associated_token_address(KAI_MAINNET_MINT, owner, TOKEN_PROGRAM_ID)
    assert token_2022.ata(owner) == associated_token_address(KAI_MAINNET_MINT, owner, TOKEN_2022_PROGRAM_ID)
    assert classic.ata(owner) != token_2022.ata(owner)
    job = classic.job_pda(owner, 7)
    assert classic.job_escrow_vault(job) == classic.ata(job)


def test_protocol_addresses_reject_unknown_token_program() -> None:
    with pytest.raises(ValueError, match="unsupported token program"):
        ProtocolAddresses(PROGRAM_ID, KAI_MAINNET_MINT, SYSTEM_PROGRAM_ID)


def test_pdas_depend_on_program_id() -> None:
    authority = Keypair().pubkey()
    assert config_pda(PROGRAM_ID) != config_pda(OTHER_PROGRAM_ID)
    assert worker_pda(PROGRAM_ID, authority) != worker_pda(OTHER_PROGRAM_ID, authority)
    assert job_pda(PROGRAM_ID, authority, 1) != job_pda(OTHER_PROGRAM_ID, authority, 1)
    proto = _proto()
    assert proto.config_pda() == config_pda(PROGRAM_ID)
    assert proto.worker_pda(authority) == worker_pda(PROGRAM_ID, authority)
    assert proto.job_pda(authority, 1) == job_pda(PROGRAM_ID, authority, 1)


def test_register_worker_ix_carries_config_token_program() -> None:
    authority = Keypair().pubkey()
    proto = _proto(TOKEN_2022_PROGRAM_ID)
    ix = register_worker_ix(proto, authority, 2, bytes(32), bytes(32))
    keys = [meta.pubkey for meta in ix.accounts]
    worker = proto.worker_pda(authority)
    assert ix.program_id == PROGRAM_ID
    assert keys == [
        authority,
        proto.config_pda(),
        KAI_MAINNET_MINT,
        worker,
        proto.ata(worker),
        TOKEN_2022_PROGRAM_ID,
        ASSOCIATED_TOKEN_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
    ]


def test_claim_job_ix_carries_config_token_program() -> None:
    authority = Keypair().pubkey()
    job = Keypair().pubkey()
    proto = _proto()
    ix = claim_job_ix(proto, authority, job)
    keys = [meta.pubkey for meta in ix.accounts]
    worker = proto.worker_pda(authority)
    assert keys == [authority, proto.config_pda(), KAI_MAINNET_MINT, worker, proto.ata(worker), job, TOKEN_PROGRAM_ID]


@pytest.mark.parametrize(
    "name",
    [
        "JobWorkerMismatch",
        "WrongWorkerStakeVault",
        "SelfChallengeForbidden",
        "WrongTokenProgram",
        "PaymentMintOwnerMismatch",
        "InvalidStakeFloors",
        "InvalidVerifierStakeFloor",
        "ForbiddenMintExtension",
        "SlashAlreadySettled",
        "ChallengeRequiresAssignedVerifier",
        "ProgramDataMismatch",
        "AdminNotUpgradeAuthority",
        "ChallengeWindowBelowFloor",
        "InvalidChallengeWindowFloor",
    ],
)
def test_new_program_errors_decode_by_anchor_index(name: str) -> None:
    code = 6000 + PROTOCOL_ERRORS.index(name)
    message = f"custom program error: 0x{code:x}"
    assert _structured_error_code(message, {"code": -32002}) == name


def test_program_error_order_is_append_only() -> None:
    # Anchor error codes are positional; the PR-1, PR-2, and PR-3 variants must stay at the tail.
    tail = PROTOCOL_ERRORS[-14:]
    assert tail[0] == "JobWorkerMismatch"
    assert tail[7] == "ForbiddenMintExtension"
    assert tail[8:] == [
        "SlashAlreadySettled",
        "ChallengeRequiresAssignedVerifier",
        "ProgramDataMismatch",
        "AdminNotUpgradeAuthority",
        "ChallengeWindowBelowFloor",
        "InvalidChallengeWindowFloor",
    ]
    assert PROTOCOL_ERRORS.index("AggregateProofRequiresAggregateSettlement") == len(PROTOCOL_ERRORS) - 15


def test_parse_base_units_truncates_and_allows_zero() -> None:
    assert parse_base_units("0", 6) == 0
    assert parse_base_units("50000", 6) == 50_000_000_000
    assert parse_base_units("1.2345678", 6) == 1_234_567
    assert parse_base_units(2.5, 6) == 2_500_000
    with pytest.raises(ValueError, match="negative"):
        parse_base_units("-1", 6)


def test_parse_token_amount_requires_positive_and_uses_decimals() -> None:
    assert parse_token_amount("1", 6) == 1_000_000
    assert parse_token_amount("1", 9) == 1_000_000_000
    with pytest.raises(ValueError, match="greater than zero"):
        parse_token_amount("0", 6)
    with pytest.raises(ValueError, match="greater than zero"):
        parse_token_amount("0.0000001", 6)


def test_format_token_amount_round_trips_six_decimals() -> None:
    assert format_token_amount(50_000_000_000, 6) == "50000"
    assert format_token_amount(1_234_567, 6) == "1.234567"
    assert format_token_amount(0, 6) == "0"
