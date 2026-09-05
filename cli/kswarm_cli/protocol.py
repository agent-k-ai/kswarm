from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID

from kswarm_cli.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    BPF_LOADER_UPGRADEABLE_PROGRAM_ID,
    DEFAULT_MIN_CHALLENGE_WINDOW_SECONDS,
    JOB_CLASS_NAME,
    JOB_STATUS,
    MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER,
    NODE_ROLE_NAME,
    STAKE_TIER_NAME,
)
from kswarm_cli.encoding import (
    anchor_account_discriminator,
    anchor_ix_discriminator,
    encode_string,
    encode_vec,
    hash_to_hex,
    parse_base_units,
    read_bytes,
    read_i64,
    read_option,
    read_string,
    read_u8,
    read_u16,
    read_u32,
    read_u64,
    u8,
    u32,
    u64,
)
from kswarm_cli.rpc import RpcClient
from kswarm_cli.spl_token import associated_token_address, validate_token_program


CONFIG_DISC = anchor_account_discriminator("ProtocolConfig")
WORKER_DISC = anchor_account_discriminator("Worker")
JOB_DISC = anchor_account_discriminator("Job")
MARKER_DISC = anchor_account_discriminator("BonsolAggregateVerification")


@dataclass(frozen=True)
class ProtocolAddresses:
    """Everything an instruction needs to address the protocol on one cluster."""

    program_id: Pubkey
    payment_mint: Pubkey
    token_program: Pubkey

    def __post_init__(self) -> None:
        validate_token_program(self.token_program)

    def config_pda(self) -> Pubkey:
        return config_pda(self.program_id)

    def worker_pda(self, authority: Pubkey) -> Pubkey:
        return worker_pda(self.program_id, authority)

    def job_pda(self, customer: Pubkey, nonce: int) -> Pubkey:
        return job_pda(self.program_id, customer, nonce)

    def ata(self, owner: Pubkey) -> Pubkey:
        """Associated token account for `owner` on the payment mint."""
        return associated_token_address(self.payment_mint, owner, self.token_program)

    def job_escrow_vault(self, job: Pubkey) -> Pubkey:
        return self.ata(job)


@dataclass(frozen=True)
class InitializeProtocolArgs:
    """`initialize_protocol` arguments.

    Stake floors are base units of the payment mint. `min_challenge_window_seconds` is
    the smallest challenge window `open_job` will accept; see
    `MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER` for the per-cluster values and why the
    bound is configuration rather than a program constant.
    """

    tier_one_stake_floor: int
    tier_two_stake_floor: int
    tier_three_stake_floor: int
    verifier_stake_floor: int
    min_challenge_window_seconds: int

    def to_bytes(self) -> bytes:
        return b"".join(
            [
                u64(self.tier_one_stake_floor),
                u64(self.tier_two_stake_floor),
                u64(self.tier_three_stake_floor),
                u64(self.verifier_stake_floor),
                u32(self.min_challenge_window_seconds),
            ]
        )

    def to_json(self) -> dict[str, int]:
        return {
            "tier_one_stake_floor": self.tier_one_stake_floor,
            "tier_two_stake_floor": self.tier_two_stake_floor,
            "tier_three_stake_floor": self.tier_three_stake_floor,
            "verifier_stake_floor": self.verifier_stake_floor,
            "min_challenge_window_seconds": self.min_challenge_window_seconds,
        }


def min_challenge_window_default(cluster_name: str) -> int:
    """The challenge-window floor to initialize `cluster_name` with.

    Unknown profiles get the mainnet value: a floor that is too high is a visible error
    at `open_job`, while one that is too low silently reopens the hole it closes.
    """
    return MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER.get(
        cluster_name, DEFAULT_MIN_CHALLENGE_WINDOW_SECONDS
    )


def validate_stake_floors(args: InitializeProtocolArgs) -> InitializeProtocolArgs:
    """Client-side mirror of the program's `initialize_protocol` argument checks."""
    if not 0 < args.tier_one_stake_floor < args.tier_two_stake_floor < args.tier_three_stake_floor:
        raise ValueError("stake floors must satisfy 0 < tier one < tier two < tier three")
    if args.verifier_stake_floor <= 0:
        raise ValueError("verifier stake floor must be greater than zero")
    for value in (args.tier_three_stake_floor, args.verifier_stake_floor):
        if value > 0xFFFF_FFFF_FFFF_FFFF:
            raise ValueError("stake floor does not fit in u64 base units")
    if not 0 < args.min_challenge_window_seconds <= 0xFFFF_FFFF:
        raise ValueError("minimum challenge window must be a positive number of seconds below 2^32")
    return args


def parse_tier_floors(text: str) -> tuple[str, str, str]:
    """Split `--tier-floors a,b,c` into three human-unit strings."""
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != 3 or any(not item for item in parts):
        raise ValueError("--tier-floors expects three comma-separated amounts: tier1,tier2,tier3")
    return parts[0], parts[1], parts[2]


def stake_floors_from_human(
    tier_floors: Sequence[str],
    verifier_floor: str,
    decimals: int,
    min_challenge_window_seconds: int,
) -> InitializeProtocolArgs:
    """Convert human-unit floors to base units with the mint's decimals, then validate."""
    if len(tier_floors) != 3:
        raise ValueError("expected three tier floors")
    tier_one, tier_two, tier_three = (parse_base_units(value, decimals) for value in tier_floors)
    return validate_stake_floors(
        InitializeProtocolArgs(
            tier_one_stake_floor=tier_one,
            tier_two_stake_floor=tier_two,
            tier_three_stake_floor=tier_three,
            verifier_stake_floor=parse_base_units(verifier_floor, decimals),
            min_challenge_window_seconds=min_challenge_window_seconds,
        )
    )


@dataclass(frozen=True)
class ProtocolConfigAccount:
    bump: int
    admin: Pubkey
    payment_mint: Pubkey
    token_program: Pubkey
    payment_decimals: int
    tier_one_stake_floor: int
    tier_two_stake_floor: int
    tier_three_stake_floor: int
    verifier_stake_floor: int
    min_challenge_window_seconds: int

    def addresses(self, program_id: Pubkey) -> ProtocolAddresses:
        return ProtocolAddresses(program_id, self.payment_mint, self.token_program)

    def to_json(self) -> dict[str, Any]:
        return {
            "bump": self.bump,
            "admin": str(self.admin),
            "payment_mint": str(self.payment_mint),
            "token_program": str(self.token_program),
            "payment_decimals": self.payment_decimals,
            "tier_one_stake_floor": self.tier_one_stake_floor,
            "tier_two_stake_floor": self.tier_two_stake_floor,
            "tier_three_stake_floor": self.tier_three_stake_floor,
            "verifier_stake_floor": self.verifier_stake_floor,
            "min_challenge_window_seconds": self.min_challenge_window_seconds,
        }


@dataclass(frozen=True)
class WorkerAccount:
    bump: int
    authority: Pubkey
    stake_vault: Pubkey
    locked_stake: int
    active_claims: int
    registered_at: int
    status: int
    role: int
    capability_class_hash: bytes
    software_digest: bytes

    def to_json(self) -> dict[str, Any]:
        return {
            "bump": self.bump,
            "authority": str(self.authority),
            "stake_vault": str(self.stake_vault),
            "locked_stake": self.locked_stake,
            "active_claims": self.active_claims,
            "registered_at": self.registered_at,
            "status": self.status,
            "status_name": "active" if self.status == 1 else f"unknown-{self.status}",
            "role": self.role,
            "role_name": NODE_ROLE_NAME.get(self.role, f"unknown-{self.role}"),
            "capability_class_hash": self.capability_class_hash.hex(),
            "software_digest": self.software_digest.hex(),
        }


@dataclass(frozen=True)
class JobAccount:
    bump: int
    nonce: int
    customer: Pubkey
    worker: Pubkey
    status: int
    reward_amount: int
    required_stake: int
    job_class: int
    required_role: int
    required_tier: int
    required_capability_class_hash: bytes
    required_software_digest: bytes
    created_at: int
    claim_deadline: int
    execution_window_seconds: int
    execute_deadline: int
    challenge_window_seconds: int
    challenge_deadline: int
    challenge_bond: int
    challenger: Pubkey
    slash_settled: bool
    escrow_refunded: bool
    verifier_reward_paid: bool
    customer_slash_paid: bool
    input_bundle_hash: bytes
    expected_result_hash: bytes
    submitted_result_hash: bytes
    input_cid: str
    output_cid: str
    result_bytes: bytes
    verifier_authority: Pubkey | None
    verifier_attestation_hash: bytes | None
    verifier_evidence_cid: str | None
    verifier_attestation_unix: int | None
    assigned_verifier_authority: Pubkey | None
    assigned_verifier_unix: int | None
    reassignment_counter: int

    def to_json(self) -> dict[str, Any]:
        return {
            "bump": self.bump,
            "nonce": self.nonce,
            "customer": str(self.customer),
            "worker": str(self.worker),
            "status": self.status,
            "status_name": JOB_STATUS.get(self.status, f"unknown-{self.status}"),
            "reward_amount": self.reward_amount,
            "required_stake": self.required_stake,
            "job_class": self.job_class,
            "job_class_name": JOB_CLASS_NAME.get(self.job_class, f"unknown-{self.job_class}"),
            "required_role": self.required_role,
            "required_role_name": NODE_ROLE_NAME.get(self.required_role, f"unknown-{self.required_role}"),
            "required_tier": self.required_tier,
            "required_tier_name": STAKE_TIER_NAME.get(self.required_tier, f"unknown-{self.required_tier}"),
            "required_capability_class_hash": self.required_capability_class_hash.hex(),
            "required_software_digest": self.required_software_digest.hex(),
            "created_at": self.created_at,
            "claim_deadline": self.claim_deadline,
            "execution_window_seconds": self.execution_window_seconds,
            "execute_deadline": self.execute_deadline,
            "challenge_window_seconds": self.challenge_window_seconds,
            "challenge_deadline": self.challenge_deadline,
            "challenge_bond": self.challenge_bond,
            "challenger": str(self.challenger),
            "slash_settled": self.slash_settled,
            "escrow_refunded": self.escrow_refunded,
            "verifier_reward_paid": self.verifier_reward_paid,
            "customer_slash_paid": self.customer_slash_paid,
            "input_bundle_hash": self.input_bundle_hash.hex(),
            "expected_result_hash": self.expected_result_hash.hex(),
            "submitted_result_hash": self.submitted_result_hash.hex(),
            "input_cid": self.input_cid,
            "output_cid": self.output_cid,
            "result_bytes": self.result_bytes.hex(),
            "verifier_authority": str(self.verifier_authority) if self.verifier_authority else None,
            "verifier_attestation_hash": hash_to_hex(self.verifier_attestation_hash),
            "verifier_evidence_cid": self.verifier_evidence_cid,
            "verifier_attestation_unix": self.verifier_attestation_unix,
            "assigned_verifier_authority": str(self.assigned_verifier_authority)
            if self.assigned_verifier_authority
            else None,
            "assigned_verifier_unix": self.assigned_verifier_unix,
            "reassignment_counter": self.reassignment_counter,
        }


@dataclass(frozen=True)
class BonsolAggregateVerificationAccount:
    bump: int
    aggregate_job: Pubkey
    execution_id: bytes
    image_id: bytes
    input_digest: bytes
    output_digest: bytes
    journal_hash: bytes
    callback_unix: int
    status: int

    def to_json(self) -> dict[str, Any]:
        return {
            "bump": self.bump,
            "aggregate_job": str(self.aggregate_job),
            "execution_id": self.execution_id.rstrip(b"\0").decode("utf-8", errors="replace"),
            "execution_id_hex": self.execution_id.hex(),
            "image_id": self.image_id.hex(),
            "input_digest": self.input_digest.hex(),
            "output_digest": self.output_digest.hex(),
            "journal_hash": self.journal_hash.hex(),
            "callback_unix": self.callback_unix,
            "status": self.status,
            "status_name": "verified" if self.status == 1 else f"unknown-{self.status}",
        }


def program_data_pda(program_id: Pubkey) -> Pubkey:
    """ProgramData account of an upgradeable program (seed: the program id)."""
    return Pubkey.find_program_address([bytes(program_id)], BPF_LOADER_UPGRADEABLE_PROGRAM_ID)[0]


def config_pda(program_id: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"config"], program_id)[0]


def worker_pda(program_id: Pubkey, authority: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([b"worker", bytes(authority)], program_id)[0]


def job_pda(program_id: Pubkey, customer: Pubkey, nonce: int) -> Pubkey:
    return Pubkey.find_program_address([b"job", bytes(customer), u64(nonce)], program_id)[0]


def bonsol_marker_pda(
    program_id: Pubkey,
    aggregate_job: Pubkey,
    execution_id: bytes,
    image_id: bytes,
    input_digest: bytes,
    journal_hash: bytes,
) -> Pubkey:
    return Pubkey.find_program_address(
        [b"bonsol_aggregate_verification", bytes(aggregate_job), execution_id, image_id, input_digest, journal_hash],
        program_id,
    )[0]


def initialize_protocol_ix(proto: ProtocolAddresses, admin: Pubkey, args: InitializeProtocolArgs) -> Instruction:
    """`admin` must be the program's upgrade authority; the program rejects any other signer."""
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("initialize_protocol") + args.to_bytes(),
        [
            AccountMeta(admin, True, True),
            AccountMeta(proto.config_pda(), False, True),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(proto.token_program, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(proto.program_id, False, False),
            AccountMeta(program_data_pda(proto.program_id), False, False),
        ],
    )


def register_worker_ix(proto: ProtocolAddresses, authority: Pubkey, role: int, capability: bytes, digest: bytes) -> Instruction:
    worker = proto.worker_pda(authority)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("register_worker") + u8(role) + capability + digest,
        [
            AccountMeta(authority, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(worker, False, True),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.token_program, False, False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        ],
    )


def deposit_worker_stake_ix(proto: ProtocolAddresses, authority: Pubkey, amount: int) -> Instruction:
    worker = proto.worker_pda(authority)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("deposit_worker_stake") + u64(amount),
        [
            AccountMeta(authority, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(worker, False, False),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.ata(authority), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def withdraw_unlocked_stake_ix(proto: ProtocolAddresses, authority: Pubkey, amount: int) -> Instruction:
    worker = proto.worker_pda(authority)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("withdraw_unlocked_stake") + u64(amount),
        [
            AccountMeta(authority, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(worker, False, True),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.ata(authority), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def open_job_ix(
    proto: ProtocolAddresses,
    customer: Pubkey,
    nonce: int,
    input_bundle_hash: bytes,
    expected_result_hash: bytes,
    reward_amount: int,
    required_stake: int,
    job_class: int,
    required_role: int,
    required_tier: int,
    required_capability: bytes,
    required_digest: bytes,
    claim_window_seconds: int,
    execution_window_seconds: int,
    challenge_window_seconds: int,
    challenge_bond: int,
) -> Instruction:
    job = proto.job_pda(customer, nonce)
    data = b"".join(
        [
            anchor_ix_discriminator("open_job"),
            u64(nonce),
            input_bundle_hash,
            expected_result_hash,
            u64(reward_amount),
            u64(required_stake),
            u8(job_class),
            u8(required_role),
            u8(required_tier),
            required_capability,
            required_digest,
            u32(claim_window_seconds),
            u32(execution_window_seconds),
            u32(challenge_window_seconds),
            u64(challenge_bond),
        ]
    )
    return Instruction(
        proto.program_id,
        data,
        [
            AccountMeta(customer, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job, False, True),
            AccountMeta(proto.job_escrow_vault(job), False, True),
            AccountMeta(proto.ata(customer), False, True),
            AccountMeta(proto.token_program, False, False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        ],
    )


def commit_input_artifact_ix(program_id: Pubkey, customer: Pubkey, job: Pubkey, cid: str) -> Instruction:
    return Instruction(
        program_id,
        anchor_ix_discriminator("commit_input_artifact") + encode_string(cid),
        [AccountMeta(customer, True, True), AccountMeta(job, False, True)],
    )


def claim_job_ix(proto: ProtocolAddresses, authority: Pubkey, job: Pubkey) -> Instruction:
    worker = proto.worker_pda(authority)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("claim_job"),
        [
            AccountMeta(authority, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(worker, False, True),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(job, False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def submit_receipt_ix(program_id: Pubkey, authority: Pubkey, job: Pubkey, output_cid: str, result_bytes: bytes) -> Instruction:
    return Instruction(
        program_id,
        anchor_ix_discriminator("submit_receipt") + encode_string(output_cid) + encode_vec(result_bytes),
        [
            AccountMeta(authority, True, True),
            AccountMeta(worker_pda(program_id, authority), False, True),
            AccountMeta(job, False, True),
        ],
    )


def submit_verifier_attestation_ix(
    proto: ProtocolAddresses,
    verifier_authority: Pubkey,
    job: Pubkey,
    result_hash: bytes,
    evidence_cid: str,
    software_digest: bytes,
) -> Instruction:
    verifier = proto.worker_pda(verifier_authority)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("submit_verifier_attestation")
        + result_hash
        + encode_string(evidence_cid)
        + software_digest,
        [
            AccountMeta(verifier_authority, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(verifier, False, False),
            AccountMeta(proto.ata(verifier), False, False),
            AccountMeta(job, False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def record_aggregate_verification_raw_ix(
    program_id: Pubkey,
    bonsol_execution_account: Pubkey,
    marker: Pubkey,
    aggregate_job: Pubkey,
    execution_id: bytes,
    image_id: bytes,
    input_digest: bytes,
    output_digest: bytes,
    journal_hash: bytes,
    forwarded_payload: bytes,
) -> Instruction:
    data = bytes([1]) + execution_id + image_id + input_digest + output_digest + journal_hash + forwarded_payload
    return Instruction(
        program_id,
        data,
        [
            AccountMeta(bonsol_execution_account, True, False),
            AccountMeta(marker, False, True),
            AccountMeta(aggregate_job, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        ],
    )


def settle_job_ix(proto: ProtocolAddresses, caller: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("settle_job"),
        _settlement_accounts(proto, caller, job_key, job, include_marker=None),
    )


def settle_aggregate_proof_job_ix(
    proto: ProtocolAddresses,
    caller: Pubkey,
    job_key: Pubkey,
    job: JobAccount,
    marker: Pubkey,
) -> Instruction:
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("settle_aggregate_proof_job"),
        _settlement_accounts(proto, caller, job_key, job, include_marker=marker),
    )


def _settlement_accounts(
    proto: ProtocolAddresses,
    caller: Pubkey,
    job_key: Pubkey,
    job: JobAccount,
    include_marker: Pubkey | None,
) -> list[AccountMeta]:
    worker = proto.worker_pda(job.worker)
    accounts = [
        AccountMeta(caller, True, True),
        AccountMeta(proto.config_pda(), False, False),
        AccountMeta(proto.payment_mint, False, False),
        AccountMeta(job_key, False, True),
    ]
    if include_marker is not None:
        accounts.append(AccountMeta(include_marker, False, False))
    accounts.extend(
        [
            AccountMeta(worker, False, True),
            AccountMeta(job.worker, False, False),
            AccountMeta(proto.job_escrow_vault(job_key), False, True),
            AccountMeta(proto.ata(job.worker), False, True),
            AccountMeta(proto.token_program, False, False),
        ]
    )
    return accounts


def challenge_job_ix(proto: ProtocolAddresses, caller: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    verifier = proto.worker_pda(caller)
    worker = proto.worker_pda(job.worker)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("challenge_job"),
        [
            AccountMeta(caller, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(verifier, False, True),
            AccountMeta(proto.ata(verifier), False, True),
            AccountMeta(job_key, False, True),
            AccountMeta(worker, False, True),
            AccountMeta(job.worker, False, False),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def refund_slashed_job_escrow_ix(proto: ProtocolAddresses, caller: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("refund_slashed_job_escrow"),
        [
            AccountMeta(caller, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job_key, False, True),
            AccountMeta(job.customer, False, False),
            AccountMeta(proto.ata(job.customer), False, True),
            AccountMeta(proto.job_escrow_vault(job_key), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def claim_verifier_slash_reward_ix(proto: ProtocolAddresses, caller: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    worker = proto.worker_pda(job.worker)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("claim_verifier_slash_reward"),
        [
            AccountMeta(caller, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job_key, False, True),
            AccountMeta(job.challenger, False, False),
            AccountMeta(proto.ata(job.challenger), False, True),
            AccountMeta(worker, False, True),
            AccountMeta(job.worker, False, False),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def claim_customer_slash_compensation_ix(proto: ProtocolAddresses, caller: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    worker = proto.worker_pda(job.worker)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("claim_customer_slash_compensation"),
        [
            AccountMeta(caller, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job_key, False, True),
            AccountMeta(job.customer, False, False),
            AccountMeta(proto.ata(job.customer), False, True),
            AccountMeta(worker, False, True),
            AccountMeta(job.worker, False, False),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def assign_verifier_ix(program_id: Pubkey, caller: Pubkey, job: Pubkey, verifier: Pubkey) -> Instruction:
    return Instruction(
        program_id,
        anchor_ix_discriminator("assign_verifier") + bytes(verifier),
        [AccountMeta(caller, True, True), AccountMeta(config_pda(program_id), False, False), AccountMeta(job, False, True)],
    )


def reassign_verifier_ix(program_id: Pubkey, caller: Pubkey, job: Pubkey) -> Instruction:
    return Instruction(
        program_id,
        anchor_ix_discriminator("reassign_verifier"),
        [AccountMeta(caller, True, True), AccountMeta(job, False, True)],
    )


def cancel_aggregate_proof_job_ix(proto: ProtocolAddresses, customer: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    """Cancels a completed aggregate job (registry exhausted, or marker timeout) and
    releases the job worker's locked stake, so the worker account is required."""
    worker = proto.worker_pda(job.worker)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("cancel_aggregate_proof_job"),
        [
            AccountMeta(customer, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job_key, False, True),
            AccountMeta(proto.job_escrow_vault(job_key), False, True),
            AccountMeta(proto.ata(customer), False, True),
            AccountMeta(proto.token_program, False, False),
            AccountMeta(worker, False, True),
            AccountMeta(job.worker, False, False),
        ],
    )


def cancel_open_job_ix(proto: ProtocolAddresses, customer: Pubkey, job_key: Pubkey) -> Instruction:
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("cancel_open_job"),
        [
            AccountMeta(customer, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job_key, False, True),
            AccountMeta(proto.job_escrow_vault(job_key), False, True),
            AccountMeta(proto.ata(customer), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def slash_stale_job_ix(proto: ProtocolAddresses, caller: Pubkey, job_key: Pubkey, job: JobAccount) -> Instruction:
    worker = proto.worker_pda(job.worker)
    return Instruction(
        proto.program_id,
        anchor_ix_discriminator("slash_stale_job"),
        [
            AccountMeta(caller, True, True),
            AccountMeta(proto.config_pda(), False, False),
            AccountMeta(proto.payment_mint, False, False),
            AccountMeta(job_key, False, True),
            AccountMeta(job.customer, False, False),
            AccountMeta(proto.ata(job.customer), False, True),
            AccountMeta(worker, False, True),
            AccountMeta(job.worker, False, False),
            AccountMeta(proto.ata(worker), False, True),
            AccountMeta(proto.job_escrow_vault(job_key), False, True),
            AccountMeta(proto.token_program, False, False),
        ],
    )


def decode_config(data: bytes) -> ProtocolConfigAccount:
    _check_disc(data, CONFIG_DISC, "ProtocolConfig")
    offset = 8
    bump, offset = read_u8(data, offset)
    admin, offset = _read_pubkey(data, offset)
    payment_mint, offset = _read_pubkey(data, offset)
    token_program, offset = _read_pubkey(data, offset)
    payment_decimals, offset = read_u8(data, offset)
    tier_one, offset = read_u64(data, offset)
    tier_two, offset = read_u64(data, offset)
    tier_three, offset = read_u64(data, offset)
    verifier, offset = read_u64(data, offset)
    min_challenge_window_seconds, offset = read_u32(data, offset)
    return ProtocolConfigAccount(
        bump,
        admin,
        payment_mint,
        token_program,
        payment_decimals,
        tier_one,
        tier_two,
        tier_three,
        verifier,
        min_challenge_window_seconds,
    )


def decode_worker(data: bytes) -> WorkerAccount:
    _check_disc(data, WORKER_DISC, "Worker")
    offset = 8
    bump, offset = read_u8(data, offset)
    authority, offset = _read_pubkey(data, offset)
    stake_vault, offset = _read_pubkey(data, offset)
    locked_stake, offset = read_u64(data, offset)
    active_claims, offset = read_u16(data, offset)
    registered_at, offset = read_i64(data, offset)
    status, offset = read_u8(data, offset)
    role, offset = read_u8(data, offset)
    capability, offset = read_bytes(data, offset, 32)
    digest, offset = read_bytes(data, offset, 32)
    return WorkerAccount(bump, authority, stake_vault, locked_stake, active_claims, registered_at, status, role, capability, digest)


def decode_job(data: bytes) -> JobAccount:
    _check_disc(data, JOB_DISC, "Job")
    offset = 8
    bump, offset = read_u8(data, offset)
    nonce, offset = read_u64(data, offset)
    customer, offset = _read_pubkey(data, offset)
    worker, offset = _read_pubkey(data, offset)
    status, offset = read_u8(data, offset)
    reward_amount, offset = read_u64(data, offset)
    required_stake, offset = read_u64(data, offset)
    job_class, offset = read_u8(data, offset)
    required_role, offset = read_u8(data, offset)
    required_tier, offset = read_u8(data, offset)
    required_capability, offset = read_bytes(data, offset, 32)
    required_digest, offset = read_bytes(data, offset, 32)
    created_at, offset = read_i64(data, offset)
    claim_deadline, offset = read_i64(data, offset)
    execution_window_seconds, offset = read_u32(data, offset)
    execute_deadline, offset = read_i64(data, offset)
    challenge_window_seconds, offset = read_u32(data, offset)
    challenge_deadline, offset = read_i64(data, offset)
    challenge_bond, offset = read_u64(data, offset)
    challenger, offset = _read_pubkey(data, offset)
    slash_settled_raw, offset = read_u8(data, offset)
    escrow_refunded_raw, offset = read_u8(data, offset)
    verifier_reward_paid_raw, offset = read_u8(data, offset)
    customer_slash_paid_raw, offset = read_u8(data, offset)
    input_bundle_hash, offset = read_bytes(data, offset, 32)
    expected_result_hash, offset = read_bytes(data, offset, 32)
    submitted_result_hash, offset = read_bytes(data, offset, 32)
    input_cid, offset = read_string(data, offset)
    output_cid, offset = read_string(data, offset)
    result_bytes, offset = _read_vec(data, offset)
    verifier_authority, offset = read_option(data, offset, _read_pubkey)
    verifier_attestation_hash, offset = read_option(data, offset, _read_hash)
    verifier_evidence_cid, offset = read_option(data, offset, read_string)
    verifier_attestation_unix, offset = read_option(data, offset, read_i64)
    assigned_verifier_authority, offset = read_option(data, offset, _read_pubkey)
    assigned_verifier_unix, offset = read_option(data, offset, read_i64)
    reassignment_counter, offset = read_u8(data, offset)
    return JobAccount(
        bump,
        nonce,
        customer,
        worker,
        status,
        reward_amount,
        required_stake,
        job_class,
        required_role,
        required_tier,
        required_capability,
        required_digest,
        created_at,
        claim_deadline,
        execution_window_seconds,
        execute_deadline,
        challenge_window_seconds,
        challenge_deadline,
        challenge_bond,
        challenger,
        bool(slash_settled_raw),
        bool(escrow_refunded_raw),
        bool(verifier_reward_paid_raw),
        bool(customer_slash_paid_raw),
        input_bundle_hash,
        expected_result_hash,
        submitted_result_hash,
        input_cid,
        output_cid,
        result_bytes,
        verifier_authority,
        verifier_attestation_hash,
        verifier_evidence_cid,
        verifier_attestation_unix,
        assigned_verifier_authority,
        assigned_verifier_unix,
        reassignment_counter,
    )


def decode_marker(data: bytes) -> BonsolAggregateVerificationAccount:
    _check_disc(data, MARKER_DISC, "BonsolAggregateVerification")
    offset = 8
    bump, offset = read_u8(data, offset)
    aggregate_job, offset = _read_pubkey(data, offset)
    execution_id, offset = read_bytes(data, offset, 32)
    image_id, offset = read_bytes(data, offset, 32)
    input_digest, offset = read_bytes(data, offset, 32)
    output_digest, offset = read_bytes(data, offset, 32)
    journal_hash, offset = read_bytes(data, offset, 32)
    callback_unix, offset = read_i64(data, offset)
    status, offset = read_u8(data, offset)
    return BonsolAggregateVerificationAccount(
        bump, aggregate_job, execution_id, image_id, input_digest, output_digest, journal_hash, callback_unix, status
    )


def fetch_config(rpc: RpcClient, program_id: Pubkey) -> ProtocolConfigAccount | None:
    data = rpc.get_account_data(str(config_pda(program_id)))
    return decode_config(data) if data else None


def fetch_job(rpc: RpcClient, address: Pubkey) -> JobAccount | None:
    data = rpc.get_account_data(str(address))
    return decode_job(data) if data else None


def fetch_worker(rpc: RpcClient, address: Pubkey) -> WorkerAccount | None:
    data = rpc.get_account_data(str(address))
    return decode_worker(data) if data else None


def iter_program_accounts(rpc: RpcClient, program_id: Pubkey, discriminator: bytes) -> list[tuple[Pubkey, bytes]]:
    out: list[tuple[Pubkey, bytes]] = []
    for entry in rpc.get_program_accounts(str(program_id)):
        data = base64.b64decode(entry["account"]["data"][0])
        if data.startswith(discriminator):
            out.append((Pubkey.from_string(entry["pubkey"]), data))
    return out


def fetch_all_jobs(rpc: RpcClient, program_id: Pubkey) -> list[tuple[Pubkey, JobAccount]]:
    return [(pubkey, decode_job(data)) for pubkey, data in iter_program_accounts(rpc, program_id, JOB_DISC)]


def fetch_all_markers(rpc: RpcClient, program_id: Pubkey) -> list[tuple[Pubkey, BonsolAggregateVerificationAccount]]:
    return [(pubkey, decode_marker(data)) for pubkey, data in iter_program_accounts(rpc, program_id, MARKER_DISC)]


def _read_pubkey(data: bytes, offset: int) -> tuple[Pubkey, int]:
    raw, offset = read_bytes(data, offset, 32)
    return Pubkey.from_bytes(raw), offset


def _read_hash(data: bytes, offset: int) -> tuple[bytes, int]:
    return read_bytes(data, offset, 32)


def _read_vec(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = read_u32(data, offset)
    return read_bytes(data, offset, length)


def _check_disc(data: bytes, expected: bytes, name: str) -> None:
    if not data.startswith(expected):
        raise ValueError(f"unexpected discriminator for {name}")
