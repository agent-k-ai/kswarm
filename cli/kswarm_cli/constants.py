from __future__ import annotations

from solders.pubkey import Pubkey


BONSOL_VERIFIER_PROGRAM_ID = Pubkey.from_string("BoNsHRcyLLNdtnoDf8hiCNZpyehMC4FDMxs6NTxFi3ew")
# Classic SPL Token program. Owns the KAI mint on mainnet.
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
# Token-2022. Supported by the program; used only for local tests.
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
KNOWN_TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID})
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
# Upgradeable BPF loader. `initialize_protocol` requires the program's ProgramData
# account (derived under this loader) and the upgrade authority as signer.
BPF_LOADER_UPGRADEABLE_PROGRAM_ID = Pubkey.from_string("BPFLoaderUpgradeab1e11111111111111111111111")
SYSVAR_INSTRUCTIONS_ID = Pubkey.from_string("Sysvar1nstructions1111111111111111111111111")

# The kswarm_protocol program id: `declare_id!` in solana/programs/kswarm_protocol/src/lib.rs.
# Rotated 2026-09-03 because the previous id's keypair had been tracked in git. The keypair for
# this id is held in the project's secret store and is never checked in; SECURITY.md says where.
KSWARM_PROGRAM_ID = Pubkey.from_string("ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM")

# KAI: the payment and stake mint (Solana mainnet). Classic SPL Token, 6 decimals,
# fixed supply, no mint/freeze authority, no extensions. See docs/kai-payment-token.md.
KAI_MAINNET_MINT = Pubkey.from_string("CZHcDHQZerSch8Fhhi2KgV4cLiD2KtdwjJBrb8fypump")
KAI_DECIMALS = 6
PAYMENT_TOKEN_SYMBOL = "KAI"

# Stand-in mints on local/devnet copy the KAI layout so amounts read the same everywhere.
LOCAL_MINT_DECIMALS = 6
MINT_CREATION_CLUSTERS = frozenset({"local", "devnet"})

# Default stake floors in human units (owner decision 2026-09-03). The program stores
# base units, converted with the mint's on-chain decimals at `protocol initialize`.
DEFAULT_TIER_STAKE_FLOORS = ("50000", "250000", "1000000")
DEFAULT_VERIFIER_STAKE_FLOOR = "100000"

# `ProtocolConfig.min_challenge_window_seconds`: the smallest challenge window `open_job`
# accepts. Like the stake floors it is an `initialize_protocol` argument, not a program
# constant, because the smallest window in which verification is genuinely reachable
# differs by cluster.
#
# The unit is one attestation rung, `ATTESTATION_WINDOW_SECONDS` = 7200 s: the time an
# assigned verifier has to attest before `reassign_verifier` may replace it. The clock for
# that rung starts at the receipt, so a window has to hold at least one whole rung before
# any verifier can be expected to attest, plus a tail in which the resulting challenge can
# still land -- `challenge_deadline` bounds `challenge_job` as well.
#
#   local    5 s   Not a real bound. The suite, `scripts/swarm-smoke.sh` and the demos run
#                  whole jobs in seconds, and a real floor would make every local run wait
#                  hours. Local clusters carry no value.
#   devnet   14400 One rung for the assigned verifier plus one full window of challenge
#                  tail. Verification is genuinely reachable; the full reassignment ladder
#                  is not guaranteed to fit, which is the price of devnet turnaround.
#   mainnet  36000 `MAX_REASSIGNMENTS + 2 = 5` rungs: one per verifier the ladder can hold
#                  (the initial assignment plus three replacements) and one window of
#                  challenge tail. This is the multiple the design review for requiring a
#                  verifier attestation before branch settlement derives; that review
#                  proposes enforcing the multiple inside the program, against a per-job
#                  attestation window, which is NOT adopted here. The program compares
#                  only against the configured floor.
#
# Override at initialization with `--min-challenge-window`.
MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER = {"local": 5, "devnet": 14400, "mainnet": 36000}
# Used for a cluster profile that is none of the three above.
DEFAULT_MIN_CHALLENGE_WINDOW_SECONDS = MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER["mainnet"]

ZERO_HASH = bytes(32)
LAMPORTS_PER_SOL = 1_000_000_000

JOB_STATUS = {
    1: "awaiting-artifact",
    2: "open",
    3: "claimed",
    4: "completed",
    5: "settled",
    6: "cancelled",
    7: "slashed",
    8: "cancelled-on-exhaustion",
    9: "cancelled-on-timeout",
}
JOB_STATUS_BY_NAME = {value: key for key, value in JOB_STATUS.items()}
JOB_STATUS_BY_NAME["submitted"] = 4

NODE_ROLE = {
    "worker-basic": 1,
    "worker-proof": 2,
    "worker-premium": 3,
    "verifier": 10,
    "artifact-peer": 20,
    "watcher": 30,
}
NODE_ROLE_NAME = {value: key for key, value in NODE_ROLE.items()}

STAKE_TIER = {"T1": 1, "T2": 2, "T3": 3}
STAKE_TIER_NAME = {value: key for key, value in STAKE_TIER.items()}

JOB_CLASS = {
    "deterministic-basic": 1,
    "branch-proof": 2,
    "branch-replicated": 3,
    "aggregate-proof": 4,
    "artifact-retention": 5,
}
JOB_CLASS_NAME = {value: key for key, value in JOB_CLASS.items()}

CAPABILITY_CLASS = {
    "any": ZERO_HASH,
    "deterministic-basic": bytes.fromhex(
        "5c84e3d2f6d0d1eda757a21250a2f5221390a693a5361655c261a4b6a62e2e05"
    ),
    "worker-proof": bytes.fromhex(
        "d558df10f6ce213db52e6365bae4a60ef162d1b6df61eb3b7888a5d1d81a1736"
    ),
    "bonsol-reducer": bytes.fromhex(
        "59e2dc55c1cea220d7df10ac5b5145012ad431e415547147da8fa6d98b2c8383"
    ),
    "branch-aggregator-bonsol": bytes.fromhex(
        "15ba06eac12f0de3834c5aec1534377da674445c2f5fa1d0ce698399e9e8d789"
    ),
}
SOFTWARE_DIGEST = {
    "worker-canonical": bytes.fromhex(
        "182f747cb7689c34ba5132a1ab7c6c735a83a613eaecf36ca346f229424bdd9b"
    ),
}

PROTOCOL_ERRORS = [
    "InvalidAmount",
    "InvalidDeadline",
    "MathOverflow",
    "InvalidJobState",
    "ClaimWindowExpired",
    "ExecutionWindowExpired",
    "ExecutionWindowOpen",
    "ChallengeWindowOpen",
    "ChallengeWindowExpired",
    "InsufficientAvailableStake",
    "InvalidWorkerRole",
    "InvalidJobClass",
    "InvalidStakeTier",
    "WorkerRoleMismatch",
    "InsufficientStakeTier",
    "CapabilityClassMismatch",
    "SoftwareDigestMismatch",
    "MaxConcurrentClaimsReached",
    "InactiveWorker",
    "InvalidVerifierRole",
    "InsufficientVerifierBond",
    "ChallengeRejected",
    "WrongWorker",
    "WrongWorkerAuthority",
    "UnexpectedResultHash",
    "ArtifactLocatorTooLong",
    "EmptyArtifactLocator",
    "ResultTooLarge",
    "SlashEscrowAlreadyRefunded",
    "SlashVerifierRewardAlreadyPaid",
    "SlashCustomerCompAlreadyPaid",
    "AttestationRoleRequired",
    "AttestationStakeTooLow",
    "AttestationDigestMismatch",
    "AttestationAlreadyExists",
    "AttestationJobNotAttestable",
    "AttestationWindowClosed",
    "AttestationEmptyResultHash",
    "SelfAttestationForbidden",
    "InvalidBonsolVerifierProgram",
    "InvalidBonsolExecutionSigner",
    "InvalidBonsolExecutionAccount",
    "BonsolExecutionIdMismatch",
    "BonsolImageIdMismatch",
    "BonsolInputDigestMismatch",
    "BonsolOutputDigestMismatch",
    "BonsolJournalHashMismatch",
    "BonsolCommittedOutputMissing",
    "InvalidBonsolMarkerAccount",
    "BonsolMarkerAlreadyExists",
    "BonsolMarkerMissing",
    "BonsolMarkerMismatch",
    "JobNotAggregateProof",
    "AggregateCapabilityMismatch",
    "VerifierAttestationRequired",
    "VerifierStillInWindow",
    "ReassignmentLimitReached",
    "AttestationAlreadySubmitted",
    "AssignedVerifierRequired",
    "RegistryNotExhausted",
    "AttestationAlreadyPresent",
    "WrongCustomer",
    "VerifierAssignmentUnauthorized",
    "VerifierAssignmentPendingRequired",
    "VerifierCannotBeWorker",
    "VerifierNotAssigned",
    "AggregateProofRequiresAggregateSettlement",
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
]
