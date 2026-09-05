from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from kswarm_cli.config import CLUSTER_ENV, cluster_path, predict_runs_dir, save_cluster
from kswarm_cli.aggregate import (
    AggregateError,
    aggregate_journal,
    build_aggregate_artifact,
)
from kswarm_cli.bonsol import (
    FRAMING_RULE,
    IMAGE_ID_ENV,
    PUBLIC_INPUT_RULE,
    resolve_aggregate_image_id,
)
from kswarm_cli.constants import (
    CAPABILITY_CLASS,
    DEFAULT_TIER_STAKE_FLOORS,
    DEFAULT_VERIFIER_STAKE_FLOOR,
    JOB_CLASS,
    JOB_STATUS,
    JOB_STATUS_BY_NAME,
    LAMPORTS_PER_SOL,
    LOCAL_MINT_DECIMALS,
    MINT_CREATION_CLUSTERS,
    MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER,
    NODE_ROLE,
    PAYMENT_TOKEN_SYMBOL,
    SOFTWARE_DIGEST,
    STAKE_TIER,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    ZERO_HASH,
)
from kswarm_cli.context import CliContext
from kswarm_cli.encoding import format_token_amount, parse_hash, parse_token_amount, sha256
from kswarm_cli.ipfs import IpfsError
from kswarm_cli.ipfs import add_bytes as ipfs_add_bytes
from kswarm_cli.ipfs import api_url as ipfs_api_url_for
from kswarm_cli.ipfs import cat_json as ipfs_cat_json
from kswarm_cli.ipfs import check as ipfs_check
from kswarm_cli.ipfs import max_artifact_bytes
from kswarm_cli.output import emit, emit_signature, emit_table
from kswarm_cli.prediction import (
    ALREADY_OPEN_REASON,
    BPS_SCALE,
    DEFAULT_TRIM_BPS,
    JOB_CANCELLED,
    JOB_COMMITTED,
    JOB_DEFERRED,
    JOB_OPENED,
    JOB_PLANNED,
    PENDING_JOB_STATUSES,
    RUN_CANCELLED,
    RUN_OPEN,
    RUN_OPENING,
    RUN_SCHEMA_VERSION,
    SCALAR_OUTPUT_KINDS,
    combiner_parameters,
    job_entry_status,
    load_run_manifest,
    pending_job_entries,
    planned_nonces,
    random_base_nonce,
    run_is_resumable,
    run_job_entries,
    run_status,
    save_run_manifest,
    scalar_bps_to_probability,
    validate_output_kind,
)
from kswarm_cli.protocol import (
    ProtocolAddresses,
    assign_verifier_ix,
    bonsol_marker_pda,
    cancel_aggregate_proof_job_ix,
    cancel_open_job_ix,
    challenge_job_ix,
    claim_customer_slash_compensation_ix,
    claim_job_ix,
    claim_verifier_slash_reward_ix,
    commit_input_artifact_ix,
    config_pda,
    deposit_worker_stake_ix,
    fetch_all_jobs,
    fetch_all_markers,
    fetch_config,
    fetch_job,
    fetch_worker,
    initialize_protocol_ix,
    min_challenge_window_default,
    open_job_ix,
    parse_tier_floors,
    record_aggregate_verification_raw_ix,
    refund_slashed_job_escrow_ix,
    register_worker_ix,
    reassign_verifier_ix,
    settle_aggregate_proof_job_ix,
    settle_job_ix,
    slash_stale_job_ix,
    stake_floors_from_human,
    submit_receipt_ix,
    submit_verifier_attestation_ix,
    withdraw_unlocked_stake_ix,
    worker_pda,
)
from kswarm_cli.rpc import RpcClient, RpcError, sign_and_send
from kswarm_cli.runtime_config import DEFAULT_ARTIFACT_GATEWAY_URL, runtime_config_payload, write_runtime_config
from kswarm_cli.swarm import BootstrapContext, BootstrapError, BootstrapPlan, WorkerSpec
from kswarm_cli.swarm import bootstrap as run_swarm_bootstrap
from kswarm_cli.spl_token import (
    create_mint,
    ensure_ata_ix_if_missing,
    fetch_mint_info,
    mint_to_checked_ix,
    transfer_checked_ix,
)
from kswarm_cli.wallets import (
    activate_wallet,
    active_wallet_name,
    create_wallet,
    list_wallets,
    load_active_wallet,
    load_keypair_file,
    load_wallet,
)


console = Console()

app = typer.Typer(
    name="kswarm",
    help="Hands-on operator CLI for the kswarm Solana protocol.",
    no_args_is_help=True,
)
wallet_app = typer.Typer(help="Create, inspect, activate, fund, and balance local operator wallets.")
protocol_app = typer.Typer(help="Initialize and inspect protocol configuration.")
token_app = typer.Typer(help="Create stand-in payment mints (local/devnet) and move KAI balances.")
worker_app = typer.Typer(help="Register workers and verifiers, then manage staked KAI.")
job_app = typer.Typer(help="Open, inspect, claim, submit, and list protocol jobs.")
inspect_app = typer.Typer(help="Read decoded protocol accounts and recent job logs.")
admin_app = typer.Typer(help="Administrative and local-validator maintenance commands.")
predict_app = typer.Typer(help="Open, inspect, report, and cancel prediction parent runs.")

app.add_typer(wallet_app, name="wallet")
app.add_typer(protocol_app, name="protocol")
app.add_typer(token_app, name="token")
app.add_typer(worker_app, name="worker")
app.add_typer(job_app, name="job")
app.add_typer(inspect_app, name="inspect")
app.add_typer(admin_app, name="admin")
app.add_typer(predict_app, name="predict")
swarm_app = typer.Typer(help="Bring a local or devnet swarm to ready: wallets, mint, protocol, registrations, stake.")
app.add_typer(swarm_app, name="swarm")


@app.callback()
def root(
    ctx: typer.Context,
    cluster: str = typer.Option(
        "local",
        "--cluster",
        envvar=CLUSTER_ENV,
        help="Cluster profile to use: local, devnet, or mainnet.",
        rich_help_panel="Global",
    ),
    rpc_url: str | None = typer.Option(
        None,
        "--rpc-url",
        help="Override the RPC URL for this invocation. KSWARM_RPC_URL overrides the profile too.",
        rich_help_panel="Global",
    ),
    commitment: str = typer.Option(
        "confirmed",
        "--commitment",
        help="RPC commitment: processed, confirmed, or finalized.",
        rich_help_panel="Global",
    ),
    keypair: str | None = typer.Option(
        None,
        "--keypair",
        help="Signer keypair path. Defaults to ~/.config/kswarm/wallets/<active>.json.",
        rich_help_panel="Global",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON where supported.",
        rich_help_panel="Global",
    ),
) -> None:
    """Configure one CLI invocation."""
    if commitment not in {"processed", "confirmed", "finalized"}:
        raise typer.BadParameter("commitment must be processed, confirmed, or finalized")
    try:
        ctx.obj = CliContext.load(
            cluster_name=cluster,
            rpc_url=rpc_url,
            commitment=commitment,
            keypair_path=keypair,
            json_output=json_output,
            console=console,
        )
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc


@wallet_app.command("create")
def wallet_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Wallet name to create."),
    airdrop: float | None = typer.Option(None, "--airdrop", help="Request this many SOL after creation."),
) -> None:
    """Create a local wallet.

    Example: kswarm wallet create customer --airdrop 10
    """
    c = _ctx(ctx)
    wallet = create_wallet(name)
    payload: dict[str, Any] = {"name": wallet.name, "pubkey": str(wallet.pubkey), "path": str(wallet.path)}
    if airdrop:
        rpc = _rpc(c)
        lamports = int(airdrop * LAMPORTS_PER_SOL)
        payload["airdrop_signature"] = rpc.request_airdrop(str(wallet.pubkey), lamports)
    if active_wallet_name() is None:
        activate_wallet(name)
        payload["activated"] = True
    emit(c, payload)


@wallet_app.command("list")
def wallet_list(ctx: typer.Context) -> None:
    """List local wallets.

    Example: kswarm wallet list
    """
    c = _ctx(ctx)
    active = active_wallet_name()
    rows = [
        {
            "name": wallet.name,
            "pubkey": str(wallet.pubkey),
            "active": "yes" if wallet.name == active else "",
            "path": str(wallet.path),
        }
        for wallet in list_wallets()
    ]
    emit_table(c, "kswarm Wallets", rows, ["name", "pubkey", "active", "path"])


@wallet_app.command("show")
def wallet_show(ctx: typer.Context, name: str = typer.Argument(..., help="Wallet name.")) -> None:
    """Show a local wallet public key.

    Example: kswarm wallet show worker-a
    """
    c = _ctx(ctx)
    wallet = load_wallet(name)
    emit(c, {"name": wallet.name, "pubkey": str(wallet.pubkey), "path": str(wallet.path)})


@wallet_app.command("activate")
def wallet_activate(ctx: typer.Context, name: str = typer.Argument(..., help="Wallet name.")) -> None:
    """Make a wallet the default signer for commands without --as.

    Example: kswarm wallet activate admin
    """
    c = _ctx(ctx)
    activate_wallet(name)
    wallet = load_wallet(name)
    emit(c, {"active": name, "pubkey": str(wallet.pubkey)})


@wallet_app.command("airdrop")
def wallet_airdrop(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Wallet name."),
    sol: float = typer.Argument(..., help="SOL to request."),
) -> None:
    """Airdrop SOL to a wallet on local cluster or devnet.

    Example: kswarm wallet airdrop customer 10
    """
    c = _ctx(ctx)
    wallet = load_wallet(name)
    signature = _rpc(c).request_airdrop(str(wallet.pubkey), int(sol * LAMPORTS_PER_SOL))
    emit_signature(c, signature, {"wallet": name, "pubkey": str(wallet.pubkey)})


@wallet_app.command("balance")
def wallet_balance(ctx: typer.Context, name: str = typer.Argument(..., help="Wallet name.")) -> None:
    """Show SOL balance.

    Example: kswarm wallet balance customer
    """
    c = _ctx(ctx)
    wallet = load_wallet(name)
    lamports = _rpc(c).get_balance(str(wallet.pubkey))
    emit(c, {"name": name, "pubkey": str(wallet.pubkey), "lamports": lamports, "sol": lamports / LAMPORTS_PER_SOL})


@protocol_app.command("initialize")
def protocol_initialize(
    ctx: typer.Context,
    admin: str | None = typer.Option(
        None, "--admin", help="Admin wallet name. Default: the --keypair file, else the active wallet."
    ),
    payment_mint: str = typer.Option(..., "--payment-mint", help="Payment and stake mint pubkey (KAI on mainnet)."),
    tier_floors: str = typer.Option(
        ",".join(DEFAULT_TIER_STAKE_FLOORS),
        "--tier-floors",
        help="Tier one, two, and three stake floors in human units, comma-separated.",
    ),
    verifier_floor: str = typer.Option(
        DEFAULT_VERIFIER_STAKE_FLOOR, "--verifier-floor", help="Verifier stake floor in human units."
    ),
    min_challenge_window: int | None = typer.Option(
        None,
        "--min-challenge-window",
        min=1,
        help=(
            "Smallest challenge window open_job will accept, in seconds. Default: by cluster "
            f"({', '.join(f'{name} {value}' for name, value in MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER.items())}). "
            "The unit is one attestation rung (ATTESTATION_WINDOW_SECONDS = 7200 s), the time an "
            "assigned verifier has to attest before reassign_verifier may replace it; that clock "
            "starts at the receipt. A window must hold at least one whole rung plus a tail in which "
            "the resulting challenge can still land. The mainnet default is MAX_REASSIGNMENTS + 2 = 5 "
            "rungs: one per verifier the reassignment ladder can hold, plus the tail. That multiple "
            "comes from the design review for requiring a verifier attestation before branch "
            "settlement, which proposes enforcing it inside the program; that gate is not "
            "implemented and only this floor is. Local clusters keep a few seconds so tests and "
            "demos stay fast."
        ),
    ),
    i_understand_real_funds: bool = typer.Option(
        False, "--i-understand-real-funds", help="Required on mainnet, where the mint is real KAI."
    ),
) -> None:
    """Initialize the kswarm config PDA.

    Floors are converted to base units with the mint's on-chain decimals.

    The signer must be the program's upgrade authority.

    Example: kswarm protocol initialize --admin admin --payment-mint <mint> --tier-floors 50000,250000,1000000 --verifier-floor 100000
    Example: kswarm --keypair /runtime/protocol/admin.json protocol initialize --payment-mint <mint>
    Example: kswarm --cluster devnet protocol initialize --payment-mint <mint> --min-challenge-window 14400
    """
    c = _ctx(ctx)
    if c.cluster_name == "mainnet" and not i_understand_real_funds:
        raise typer.BadParameter("mainnet initialize binds the protocol to real KAI; pass --i-understand-real-funds")
    rpc = _rpc(c)
    program_id = _program_id(c)
    mint = Pubkey.from_string(payment_mint)
    try:
        mint_info = fetch_mint_info(rpc, mint)
        floors = stake_floors_from_human(
            parse_tier_floors(tier_floors),
            verifier_floor,
            mint_info.decimals,
            min_challenge_window
            if min_challenge_window is not None
            else min_challenge_window_default(c.cluster_name),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    profile = {
        "payment_mint": str(mint),
        "payment_decimals": mint_info.decimals,
        "token_program": str(mint_info.token_program),
    }
    existing = fetch_config(rpc, program_id)
    if existing:
        if existing.payment_mint != mint:
            raise typer.BadParameter(f"protocol already initialized with mint {existing.payment_mint}")
        save_cluster(c.cluster_name, profile)
        emit(c, {"status": "already-initialized", "config": str(config_pda(program_id)), **existing.to_json()})
        return
    signer = load_wallet(admin).keypair if admin else _default_signer(c)
    proto = ProtocolAddresses(program_id, mint, mint_info.token_program)
    signature = sign_and_send(rpc, signer, [initialize_protocol_ix(proto, signer.pubkey(), floors)])
    save_cluster(c.cluster_name, {**profile, "admin_wallet": admin} if admin else profile)
    emit_signature(
        c,
        signature,
        {
            "config": str(proto.config_pda()),
            "payment_mint": str(mint),
            "token_program": str(mint_info.token_program),
            "payment_decimals": mint_info.decimals,
            # `to_json()` carries the challenge-window floor alongside the four stake floors.
            "stake_floors": floors.to_json(),
        },
    )


@protocol_app.command("show")
def protocol_show(ctx: typer.Context) -> None:
    """Show decoded protocol config.

    Example: kswarm protocol show
    """
    c = _ctx(ctx)
    program_id = _program_id(c)
    config = fetch_config(_rpc(c), program_id)
    emit(c, {"config": str(config_pda(program_id)), "account": config.to_json() if config else None})


@protocol_app.command("runtime-config")
def protocol_runtime_config(
    ctx: typer.Context,
    output: Path = typer.Option(..., "--output", help="Where to write protocol.json."),
    artifact_gateway_url: str = typer.Option(
        DEFAULT_ARTIFACT_GATEWAY_URL, "--artifact-gateway-url", help="URL of the Node artifact gateway (protocol-api)."
    ),
) -> None:
    """Write the on-chain protocol config as the `protocol.json` the Node api and watcher read.

    The file mirrors the chain: mint, token program, decimals, stake floors, program id,
    and the RPC URL of this invocation. Fails when the protocol is not initialized.

    Example: kswarm --rpc-url http://solana-validator:8899 protocol runtime-config --output /runtime/protocol/protocol.json
    """
    c = _ctx(ctx)
    program_id = _program_id(c)
    config = fetch_config(_rpc(c), program_id)
    if config is None:
        raise typer.BadParameter("protocol config not initialized; run `protocol initialize` first")
    payload = runtime_config_payload(config, program_id, c.rpc_url, artifact_gateway_url)
    write_runtime_config(output, payload)
    emit(c, {"path": str(output), **payload})


@token_app.command("create-mint")
def token_create_mint(
    ctx: typer.Context,
    decimals: int = typer.Option(LOCAL_MINT_DECIMALS, "--decimals", min=0, max=18, help="Mint decimals. KAI uses 6."),
    authority: str = typer.Option(..., "--authority", help="Mint authority wallet name."),
    token_2022: bool = typer.Option(
        False, "--token-2022", help="Create a Token-2022 mint instead of a classic SPL Token mint (tests only)."
    ),
) -> None:
    """Create a stand-in payment mint on local or devnet.

    Default is a classic SPL Token mint with 6 decimals, the same layout as KAI.

    Example: kswarm token create-mint --authority admin
    """
    c = _ctx(ctx)
    _require_mint_cluster(c, "token create-mint")
    rpc = _rpc(c)
    token_program = TOKEN_2022_PROGRAM_ID if token_2022 else TOKEN_PROGRAM_ID
    signer = load_wallet(authority).keypair
    mint, signature = create_mint(rpc, signer, signer.pubkey(), decimals, token_program)
    save_cluster(
        c.cluster_name,
        {
            "payment_mint": str(mint),
            "payment_decimals": decimals,
            "token_program": str(token_program),
            "mint_authority_wallet": authority,
        },
    )
    emit_signature(
        c,
        signature,
        {
            "mint": str(mint),
            "decimals": decimals,
            "token_program": str(token_program),
            "authority": str(signer.pubkey()),
        },
    )


@token_app.command("mint")
def token_mint(
    ctx: typer.Context,
    amount: str = typer.Argument(..., help="Human KAI amount."),
    to: str = typer.Option(..., "--to", help="Destination wallet name."),
) -> None:
    """Mint stand-in tokens to a wallet on local or devnet.

    Example: kswarm token mint 5000 --to customer
    """
    c = _ctx(ctx)
    _require_mint_cluster(c, "token mint")
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    authority = _mint_authority(c)
    destination = load_wallet(to)
    base_amount = parse_token_amount(amount, decimals)
    ata, create_ata_ix = ensure_ata_ix_if_missing(
        rpc, authority.pubkey(), proto.payment_mint, destination.pubkey, proto.token_program
    )
    mint_ix = mint_to_checked_ix(proto.payment_mint, ata, authority.pubkey(), base_amount, decimals, proto.token_program)
    instructions = [ix for ix in [create_ata_ix, mint_ix] if ix]
    signature = sign_and_send(rpc, authority, instructions)
    emit_signature(c, signature, {"to": to, "ata": str(ata), "amount": base_amount, "ui_amount": amount})


@token_app.command("transfer")
def token_transfer(
    ctx: typer.Context,
    amount: str = typer.Argument(..., help="Human KAI amount."),
    from_wallet: str = typer.Option(..., "--from", help="Source wallet name."),
    to: str = typer.Option(..., "--to", help="Destination wallet name."),
) -> None:
    """Transfer KAI between local wallets.

    Example: kswarm token transfer 25 --from customer --to worker-a
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    sender = load_wallet(from_wallet)
    recipient = load_wallet(to)
    source = proto.ata(sender.pubkey)
    destination, create_ata_ix = ensure_ata_ix_if_missing(
        rpc, sender.pubkey, proto.payment_mint, recipient.pubkey, proto.token_program
    )
    base_amount = parse_token_amount(amount, decimals)
    transfer_ix = transfer_checked_ix(
        source, proto.payment_mint, destination, sender.pubkey, base_amount, decimals, proto.token_program
    )
    instructions = [ix for ix in [create_ata_ix, transfer_ix] if ix]
    signature = sign_and_send(rpc, sender.keypair, instructions)
    emit_signature(c, signature, {"from": from_wallet, "to": to, "amount": base_amount})


@token_app.command("balance")
def token_balance(ctx: typer.Context, name: str = typer.Argument(..., help="Wallet name.")) -> None:
    """Show KAI balance for a wallet.

    Example: kswarm token balance worker-a
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    wallet = load_wallet(name)
    ata = proto.ata(wallet.pubkey)
    balance = rpc.get_token_account_balance(str(ata))
    amount = int(balance["amount"]) if balance else 0
    emit(c, {"name": name, "ata": str(ata), "amount": amount, "ui_amount": format_token_amount(amount, decimals)})


@worker_app.command("register")
def worker_register(
    ctx: typer.Context,
    wallet_name: str = typer.Option(..., "--as", help="Wallet name to register."),
    role: str = typer.Option(..., "--role", help="worker-basic, worker-proof, worker-premium, or verifier."),
    capability: str = typer.Option(..., "--capability", help="32-byte hash or known capability name."),
    software_digest: str = typer.Option(..., "--software-digest", help="32-byte hash or known digest name."),
) -> None:
    """Register a worker or verifier.

    Example: kswarm worker register --as worker-a --role worker-proof --capability worker-proof --software-digest worker-canonical
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, _ = _payment_context(c, rpc)
    signer = load_wallet(wallet_name)
    worker = proto.worker_pda(signer.pubkey)
    existing = fetch_worker(rpc, worker)
    if existing:
        emit(c, {"status": "already-registered", "worker": str(worker), "account": existing.to_json()})
        return
    signature = sign_and_send(
        rpc,
        signer.keypair,
        [register_worker_ix(proto, signer.pubkey, _role(role), _hash_or_known(capability, CAPABILITY_CLASS), _hash_or_known(software_digest, SOFTWARE_DIGEST))],
    )
    emit_signature(c, signature, {"worker": str(worker), "authority": str(signer.pubkey)})


@worker_app.command("show")
def worker_show(ctx: typer.Context, pubkey_or_name: str = typer.Argument(..., help="Worker PDA, authority pubkey, or wallet name.")) -> None:
    """Show decoded worker state.

    Example: kswarm worker show worker-a
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    address = _worker_address(_program_id(c), pubkey_or_name)
    worker = fetch_worker(rpc, address)
    emit(c, {"worker": str(address), "account": worker.to_json() if worker else None})


@worker_app.command("stake")
def worker_stake(
    ctx: typer.Context,
    amount: str = typer.Argument(..., help="Human KAI amount."),
    wallet_name: str = typer.Option(..., "--as", help="Worker wallet name."),
) -> None:
    """Deposit unlocked KAI stake into the worker PDA vault.

    Example: kswarm worker stake 2500 --as worker-a
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    signer = load_wallet(wallet_name)
    base_amount = parse_token_amount(amount, decimals)
    signature = sign_and_send(rpc, signer.keypair, [deposit_worker_stake_ix(proto, signer.pubkey, base_amount)])
    emit_signature(c, signature, {"worker": str(proto.worker_pda(signer.pubkey)), "amount": base_amount})


@worker_app.command("withdraw-stake")
def worker_withdraw_stake(
    ctx: typer.Context,
    amount: str = typer.Argument(..., help="Human KAI amount."),
    wallet_name: str = typer.Option(..., "--as", help="Worker wallet name."),
) -> None:
    """Withdraw unlocked stake from the worker PDA vault.

    Example: kswarm worker withdraw-stake 100 --as worker-a
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    signer = load_wallet(wallet_name)
    destination, create_ata_ix = ensure_ata_ix_if_missing(
        rpc, signer.pubkey, proto.payment_mint, signer.pubkey, proto.token_program
    )
    base_amount = parse_token_amount(amount, decimals)
    instructions = [ix for ix in [create_ata_ix, withdraw_unlocked_stake_ix(proto, signer.pubkey, base_amount)] if ix]
    signature = sign_and_send(rpc, signer.keypair, instructions)
    emit_signature(c, signature, {"worker": str(proto.worker_pda(signer.pubkey)), "destination": str(destination), "amount": base_amount})


@job_app.command("open")
def job_open(
    ctx: typer.Context,
    customer: str = typer.Option(..., "--as", help="Customer wallet name."),
    job_class: str = typer.Option(..., "--class", help="Job class, for example branch-proof or aggregate-proof."),
    reward: str = typer.Option(..., "--reward", help="Human KAI reward."),
    required_stake: str = typer.Option(..., "--required-stake", help="Human KAI worker stake requirement."),
    challenge_window: int = typer.Option(..., "--challenge-window", help="Challenge window in seconds."),
    capability: str = typer.Option(..., "--capability", help="Required capability hash or known capability name."),
    required_software_digest: str | None = typer.Option(None, "--required-software-digest", help="Optional software digest hash or known name."),
    required_tier: str = typer.Option("T1", "--required-tier", help="Required tier: T1, T2, or T3."),
    nonce: int | None = typer.Option(None, "--nonce", help="Explicit job nonce (u64). Defaults to a random value."),
    input_hash: str | None = typer.Option(None, "--input-hash", help="Input bundle hash. Defaults to zero hash."),
    expected_result_hash: str | None = typer.Option(None, "--expected-result-hash", help="Expected result/journal hash. Defaults to zero hash."),
    claim_window: int = typer.Option(3600, "--claim-window", help="Claim window in seconds."),
    execution_window: int = typer.Option(3600, "--execution-window", help="Execution window in seconds."),
    challenge_bond: str | None = typer.Option(None, "--challenge-bond", help="Human KAI verifier challenge bond. Defaults to required stake."),
    required_role: str = typer.Option("worker-proof", "--required-role", help="Required worker role."),
) -> None:
    """Open and escrow a job.

    Example: kswarm job open --as customer --class branch-proof --reward 25 --required-stake 500 --challenge-window 30 --capability worker-proof
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    signer = load_wallet(customer)
    job_nonce = nonce if nonce is not None else random_base_nonce(0)
    job_key = proto.job_pda(signer.pubkey, job_nonce)
    existing = fetch_job(rpc, job_key)
    if existing:
        emit(c, {"status": "already-opened", "job": str(job_key), "account": existing.to_json()})
        return
    reward_amount = parse_token_amount(reward, decimals)
    stake_amount = parse_token_amount(required_stake, decimals)
    bond_amount = parse_token_amount(challenge_bond, decimals) if challenge_bond else stake_amount
    ix = open_job_ix(
        proto,
        signer.pubkey,
        job_nonce,
        parse_hash(input_hash, default=ZERO_HASH),
        parse_hash(expected_result_hash, default=ZERO_HASH),
        reward_amount,
        stake_amount,
        _job_class(job_class),
        _role(required_role),
        _tier(required_tier),
        _hash_or_known(capability, CAPABILITY_CLASS),
        _hash_or_known(required_software_digest, SOFTWARE_DIGEST, default=ZERO_HASH),
        claim_window,
        execution_window,
        challenge_window,
        bond_amount,
    )
    signature = sign_and_send(rpc, signer.keypair, [ix])
    emit_signature(c, signature, {"job": str(job_key), "nonce": job_nonce})


@job_app.command("commit-input")
def job_commit_input(
    ctx: typer.Context,
    job: str = typer.Option(..., "--job", help="Job account pubkey."),
    cid: str = typer.Option(..., "--cid", help="IPFS CID to attach."),
    customer: str = typer.Option(..., "--as", help="Customer wallet name."),
) -> None:
    """Commit an input artifact CID to an awaiting job.

    Example: kswarm job commit-input --job <job> --cid bafkre... --as customer
    """
    c = _ctx(ctx)
    signer = load_wallet(customer)
    job_key = Pubkey.from_string(job)
    signature = sign_and_send(_rpc(c), signer.keypair, [commit_input_artifact_ix(_program_id(c), signer.pubkey, job_key, cid)])
    emit_signature(c, signature, {"job": str(job_key), "cid": cid})


@job_app.command("show")
def job_show(ctx: typer.Context, pubkey: str = typer.Argument(..., help="Job account pubkey.")) -> None:
    """Show decoded job state.

    Example: kswarm job show <job>
    """
    c = _ctx(ctx)
    job_key = Pubkey.from_string(pubkey)
    job = fetch_job(_rpc(c), job_key)
    emit(c, {"job": str(job_key), "account": job.to_json() if job else None})


@job_app.command("list")
def job_list(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status", help="Filter by status: open, claimed, submitted, settled, slashed, cancelled."),
    customer: str | None = typer.Option(None, "--customer", help="Filter by customer wallet name."),
) -> None:
    """List decoded kswarm jobs.

    Example: kswarm job list --status open --customer customer
    """
    c = _ctx(ctx)
    status_id = JOB_STATUS_BY_NAME.get(status) if status else None
    customer_pubkey = str(load_wallet(customer).pubkey) if customer else None
    rows = []
    for pubkey, account in fetch_all_jobs(_rpc(c), _program_id(c)):
        data = account.to_json()
        if status_id is not None and account.status != status_id:
            continue
        if customer_pubkey and str(account.customer) != customer_pubkey:
            continue
        rows.append(
            {
                "job": str(pubkey),
                "status": data["status_name"],
                "class": data["job_class_name"],
                "customer": data["customer"],
                "worker": data["worker"],
                "reward": data["reward_amount"],
            }
        )
    emit_table(c, "kswarm Jobs", rows, ["job", "status", "class", "customer", "worker", "reward"])


@job_app.command("claim")
def job_claim(
    ctx: typer.Context,
    pubkey: str = typer.Argument(..., help="Job account pubkey."),
    worker: str = typer.Option(..., "--as", help="Worker wallet name."),
) -> None:
    """Claim an open job as a registered worker.

    Example: kswarm job claim <job> --as worker-a
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, _ = _payment_context(c, rpc)
    signer = load_wallet(worker)
    job_key = Pubkey.from_string(pubkey)
    signature = sign_and_send(rpc, signer.keypair, [claim_job_ix(proto, signer.pubkey, job_key)])
    emit_signature(c, signature, {"job": str(job_key), "worker": str(signer.pubkey)})


@job_app.command("submit-receipt")
def job_submit_receipt(
    ctx: typer.Context,
    pubkey: str = typer.Argument(..., help="Job account pubkey."),
    output_cid: str = typer.Option(..., "--output-cid", help="Output artifact CID."),
    result_bytes: str = typer.Option(..., "--result-bytes", help="Result bytes as hex."),
    worker: str = typer.Option(..., "--as", help="Worker wallet name."),
) -> None:
    """Submit a worker receipt for a claimed job.

    Example: kswarm job submit-receipt <job> --output-cid bafkre... --result-bytes 0a0b --as worker-a
    """
    c = _ctx(ctx)
    signer = load_wallet(worker)
    job_key = Pubkey.from_string(pubkey)
    result = bytes.fromhex(result_bytes.removeprefix("0x"))
    signature = sign_and_send(_rpc(c), signer.keypair, [submit_receipt_ix(_program_id(c), signer.pubkey, job_key, output_cid, result)])
    emit_signature(c, signature, {"job": str(job_key), "submitted_result_hash": sha256(result).hex()})


@app.command("attest")
def attest(
    ctx: typer.Context,
    job_pubkey: str = typer.Argument(..., help="Job account pubkey."),
    result_hash: str = typer.Option(..., "--result-hash", help="Verifier result hash hex."),
    evidence_cid: str = typer.Option(..., "--evidence-cid", help="Verifier evidence CID."),
    software_digest: str = typer.Option(..., "--software-digest", help="Verifier software digest hash or known name."),
    verifier: str = typer.Option(..., "--as", help="Verifier wallet name."),
) -> None:
    """Submit verifier attestation for a completed job.

    Example: kswarm attest <job> --result-hash <hex> --evidence-cid bafkre... --software-digest worker-canonical --as verifier
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, _ = _payment_context(c, rpc)
    signer = load_wallet(verifier)
    job_key = Pubkey.from_string(job_pubkey)
    signature = sign_and_send(
        rpc,
        signer.keypair,
        [
            submit_verifier_attestation_ix(
                proto,
                signer.pubkey,
                job_key,
                parse_hash(result_hash),
                evidence_cid,
                _hash_or_known(software_digest, SOFTWARE_DIGEST),
            )
        ],
    )
    emit_signature(c, signature, {"job": str(job_key), "verifier": str(signer.pubkey)})


@app.command("settle")
def settle(ctx: typer.Context, job_pubkey: str = typer.Argument(..., help="Job account pubkey.")) -> None:
    """Settle a non-aggregate completed job after the challenge window.

    Example: kswarm settle <job>
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = _default_signer(c)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(job_pubkey)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer, [settle_job_ix(proto, signer.pubkey(), job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@app.command("settle-aggregate")
def settle_aggregate(ctx: typer.Context, job_pubkey: str = typer.Argument(..., help="Aggregate job account pubkey.")) -> None:
    """Settle an aggregate-proof job using a Bonsol marker PDA.

    Example: kswarm settle-aggregate <job>
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = _default_signer(c)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(job_pubkey)
    job = _require_job(rpc, job_key)
    marker = _find_marker_for_job(rpc, proto.program_id, job_key)
    signature = sign_and_send(rpc, signer, [settle_aggregate_proof_job_ix(proto, signer.pubkey(), job_key, job, marker)])
    emit_signature(c, signature, {"job": str(job_key), "marker": str(marker)})


@app.command("challenge")
def challenge(
    ctx: typer.Context,
    job_pubkey: str = typer.Argument(..., help="Job account pubkey."),
    verifier: str = typer.Option(..., "--as", help="Verifier wallet name."),
) -> None:
    """Challenge a bad receipt during the challenge window.

    Example: kswarm challenge <job> --as verifier
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = load_wallet(verifier)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(job_pubkey)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer.keypair, [challenge_job_ix(proto, signer.pubkey, job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@app.command("refund-slashed")
def refund_slashed(ctx: typer.Context, job_pubkey: str = typer.Argument(..., help="Slashed job account pubkey.")) -> None:
    """Refund slashed job escrow to the customer pool.

    Example: kswarm refund-slashed <job>
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = _default_signer(c)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(job_pubkey)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer, [refund_slashed_job_escrow_ix(proto, signer.pubkey(), job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@app.command("claim-verifier-slash-reward")
def claim_verifier_slash_reward(
    ctx: typer.Context,
    job_pubkey: str = typer.Argument(..., help="Slashed job account pubkey."),
    verifier: str = typer.Option(..., "--as", help="Verifier wallet name."),
) -> None:
    """Claim verifier reward from a slashed worker stake.

    Example: kswarm claim-verifier-slash-reward <job> --as verifier
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = load_wallet(verifier)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(job_pubkey)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer.keypair, [claim_verifier_slash_reward_ix(proto, signer.pubkey, job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@app.command("claim-customer-slash-compensation")
def claim_customer_slash_compensation(
    ctx: typer.Context,
    job_pubkey: str = typer.Argument(..., help="Slashed job account pubkey."),
    customer: str = typer.Option(..., "--as", help="Customer wallet name."),
) -> None:
    """Claim customer compensation from a slashed worker stake.

    Example: kswarm claim-customer-slash-compensation <job> --as customer
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = load_wallet(customer)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(job_pubkey)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer.keypair, [claim_customer_slash_compensation_ix(proto, signer.pubkey, job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@app.command("assign-verifier")
def assign_verifier(
    ctx: typer.Context,
    aggregate_job: str = typer.Argument(..., help="Job account pubkey (any class)."),
    verifier: str = typer.Option(..., "--verifier", help="Verifier wallet name."),
) -> None:
    """Assign a verifier to a job. Only the assigned verifier may challenge it.

    Any staked verifier can attest, but `challenge_job` accepts only the verifier the
    customer (or the protocol admin) assigned, for every job class. Assign before the
    first attestation lands.

    Example: kswarm assign-verifier <job> --verifier verifier --as customer
    """
    c = _ctx(ctx)
    signer = _default_signer(c)
    verifier_pubkey = load_wallet(verifier).pubkey
    job_key = Pubkey.from_string(aggregate_job)
    signature = sign_and_send(_rpc(c), signer, [assign_verifier_ix(_program_id(c), signer.pubkey(), job_key, verifier_pubkey)])
    emit_signature(c, signature, {"job": str(job_key), "verifier": str(verifier_pubkey)})


@app.command("reassign-verifier")
def reassign_verifier(ctx: typer.Context, aggregate_job: str = typer.Argument(..., help="Aggregate job account pubkey.")) -> None:
    """Request verifier reassignment after the attestation window expires.

    Example: kswarm reassign-verifier <aggregate-job>
    """
    c = _ctx(ctx)
    signer = _default_signer(c)
    job_key = Pubkey.from_string(aggregate_job)
    signature = sign_and_send(_rpc(c), signer, [reassign_verifier_ix(_program_id(c), signer.pubkey(), job_key)])
    emit_signature(c, signature, {"job": str(job_key)})


@app.command("cancel-aggregate")
def cancel_aggregate(
    ctx: typer.Context,
    aggregate_job: str = typer.Argument(..., help="Aggregate job account pubkey."),
    customer: str = typer.Option(..., "--as", help="Customer wallet name."),
) -> None:
    """Cancel a completed aggregate-proof job: verifier registry exhausted, or no
    settlement 24h after the challenge window closed (marker timeout). Refunds the
    escrow and unlocks the worker's stake.

    Example: kswarm cancel-aggregate <aggregate-job> --as customer
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = load_wallet(customer)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(aggregate_job)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer.keypair, [cancel_aggregate_proof_job_ix(proto, signer.pubkey, job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@swarm_app.command("bootstrap")
def swarm_bootstrap(
    ctx: typer.Context,
    admin: str = typer.Option("admin", "--admin", help="Admin and stand-in mint authority wallet name."),
    customer: str = typer.Option("customer", "--customer", help="Customer wallet name."),
    branch_workers: list[str] = typer.Option(["worker-a"], "--branch-worker", help="Branch worker wallet name (repeatable)."),
    verifier: str | None = typer.Option("verifier", "--verifier", help="Verifier wallet name; empty to skip."),
    aggregator: str | None = typer.Option("aggregator", "--aggregator", help="Aggregator wallet name; empty to skip."),
    create_wallets: bool = typer.Option(True, "--create-wallets/--no-create-wallets", help="Create missing wallets."),
    airdrop_sol: float = typer.Option(20.0, "--airdrop-sol", min=0.0, help="Target SOL per wallet, airdropped when below. 0 disables."),
    create_mint: bool = typer.Option(True, "--create-mint/--no-create-mint", help="Create a stand-in mint when none exists."),
    payment_mint: str | None = typer.Option(None, "--payment-mint", help="Use this existing mint instead."),
    fund_kai: str | None = typer.Option("300000", "--fund-kai", help="Target KAI per non-admin wallet (stand-in mints only). Empty disables."),
    tier_floors: str = typer.Option(",".join(DEFAULT_TIER_STAKE_FLOORS), "--tier-floors", help="Tier floors in human units, comma-separated."),
    verifier_floor: str = typer.Option(DEFAULT_VERIFIER_STAKE_FLOOR, "--verifier-floor", help="Verifier floor in human units."),
    min_challenge_window: int | None = typer.Option(
        None,
        "--min-challenge-window",
        min=1,
        help="Smallest challenge window open_job will accept, in seconds. Default: by cluster. See protocol initialize --help.",
    ),
    worker_stake: str | None = typer.Option(None, "--worker-stake", help="Target branch-worker stake in KAI. Default: tier-one floor."),
    verifier_stake: str | None = typer.Option(None, "--verifier-stake", help="Target verifier stake in KAI. Default: verifier floor."),
    aggregator_stake: str | None = typer.Option(None, "--aggregator-stake", help="Target aggregator stake in KAI. Default: tier-one floor."),
    aggregate_image_id: str | None = typer.Option(
        None, "--aggregate-image-id", help=f"Reducer image id the aggregator registers with. Defaults to ${IMAGE_ID_ENV}, then the checked-in id."
    ),
) -> None:
    """Bring a local or devnet swarm to ready. Safe to rerun; every step checks the chain first.

    Branch workers register as `worker-proof` with the `worker-proof` capability and the
    `worker-canonical` digest. The verifier registers as `verifier` with the same capability
    and digest. The aggregator registers as `worker-proof` with the `branch-aggregator-bonsol`
    capability and the reducer image id, so it can claim the aggregate jobs `predict open` creates.

    Example: kswarm --rpc-url http://validator:8899 swarm bootstrap
    Example: kswarm --cluster devnet swarm bootstrap --payment-mint <mint> --airdrop-sol 0 --fund-kai ""
    """
    c = _ctx(ctx)
    try:
        image_id = resolve_aggregate_image_id(aggregate_image_id, os.environ)
        floors = parse_tier_floors(tier_floors)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    workers = [WorkerSpec(name, "worker-proof", "worker-proof", "worker-canonical", worker_stake) for name in branch_workers]
    if verifier:
        workers.append(WorkerSpec(verifier, "verifier", "worker-proof", "worker-canonical", verifier_stake))
    if aggregator:
        workers.append(WorkerSpec(aggregator, "worker-proof", "branch-aggregator-bonsol", image_id.hex(), aggregator_stake))
    plan = BootstrapPlan(
        admin=admin,
        customer=customer,
        workers=tuple(workers),
        create_wallets=create_wallets,
        airdrop_sol=airdrop_sol,
        create_mint=create_mint,
        payment_mint=payment_mint or None,
        fund_kai=fund_kai or None,
        tier_floors=floors,
        verifier_floor=verifier_floor,
        min_challenge_window_seconds=(
            min_challenge_window
            if min_challenge_window is not None
            else min_challenge_window_default(c.cluster_name)
        ),
    )
    context = BootstrapContext(
        cluster_name=c.cluster_name,
        cluster_config=dict(c.cluster_config),
        program_id=_program_id(c),
        rpc=_rpc(c),
        save_cluster=save_cluster,
        environ=os.environ,
    )
    try:
        summary = run_swarm_bootstrap(context, plan)
    except BootstrapError as exc:
        raise typer.BadParameter(str(exc)) from exc
    emit(c, summary)


@predict_app.command("open")
def predict_open(
    ctx: typer.Context,
    question: str = typer.Option(..., "--question", help="Customer prediction question."),
    output_kind: str = typer.Option("scalar", "--output-kind", help="scalar, categorical, or narrative_with_scalar."),
    branches: int = typer.Option(16, "--branches", min=1, max=128, help="Number of branch-proof jobs to open."),
    combiner: str = typer.Option("weighted-mean", "--combiner", help="weighted-mean, trimmed-mean, or majority-vote."),
    trim_bps: int | None = typer.Option(
        None,
        "--trim-bps",
        help=f"trimmed-mean only: basis points of branches to trim, in [0, {BPS_SCALE}). Default {DEFAULT_TRIM_BPS}.",
    ),
    reward_per_branch: str = typer.Option(..., "--reward-per-branch", help="Human KAI reward per branch."),
    aggregator_reward: str = typer.Option(..., "--aggregator-reward", help="Human KAI aggregate reward."),
    challenge_window: int = typer.Option(600, "--challenge-window", help="Challenge window in seconds."),
    persona_set: str | None = typer.Option(None, "--persona-set", help="Persona-set CID or builtin identifier."),
    customer: str = typer.Option("customer", "--as", help="Customer wallet name."),
    branch_required_stake: str = typer.Option("500", "--branch-required-stake", help="Human KAI branch stake requirement."),
    aggregate_required_stake: str = typer.Option("500", "--aggregate-required-stake", help="Human KAI aggregate stake requirement."),
    claim_window: int = typer.Option(3600, "--claim-window", help="Claim window in seconds."),
    execution_window: int = typer.Option(3600, "--execution-window", help="Execution window in seconds."),
    labels: str | None = typer.Option(None, "--labels", help="Comma-separated labels for categorical output."),
    forecast_horizon: str = typer.Option("operator-defined", "--forecast-horizon", help="Forecast horizon or event-resolution rule."),
    ipfs_api_url: str | None = typer.Option(None, "--ipfs-api-url", help="Override local IPFS API URL."),
    context_file: Path | None = typer.Option(None, "--context-file", help="UTF-8 seed/context file to embed in each branch input."),
    num_ctx: int | None = typer.Option(None, "--num-ctx", min=1, help="Per-request Ollama context window passed as options.num_ctx."),
    personas_file: Path | None = typer.Option(None, "--personas-file", help="JSON persona library to assign deterministically across branches."),
    aggregate_image_id: str | None = typer.Option(
        None,
        "--aggregate-image-id",
        help=f"Bonsol reducer image id (32-byte hex) for the aggregate job. Defaults to ${IMAGE_ID_ENV}, then the checked-in id.",
    ),
    defer_aggregate_open: bool = typer.Option(
        False,
        "--defer-aggregate-open",
        help="Accepted and ignored: the aggregate job is always opened later, by predict bind-aggregate.",
    ),
) -> None:
    """Open a parent prediction run and its branch jobs.

    The aggregate job is planned here and opened by `predict bind-aggregate` once the
    branches have settled. It cannot be opened now: `open_job` fixes a job's
    `input_bundle_hash` and `expected_result_hash` for good, and both are functions of
    the branch receipts, which do not exist until the branches run. Opening it now
    would fund a job that no Bonsol marker could ever match.

    The run manifest is written before the first transaction and after every
    confirmed one. If the command stops early, `predict resume <parent-run>`
    continues it and `predict cancel <parent-run>` unwinds the opened jobs.
    """

    del defer_aggregate_open  # every run defers now; the flag is kept for older callers

    try:
        validate_output_kind(output_kind)
        parameters = combiner_parameters(combiner, output_kind, trim_bps)
        image_id = resolve_aggregate_image_id(aggregate_image_id, os.environ)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    c = _ctx(ctx)
    rpc = _rpc(c)
    proto, decimals = _payment_context(c, rpc)
    signer = load_wallet(customer)
    branch_reward_amount = parse_token_amount(_strip_token_suffix(reward_per_branch), decimals)
    aggregate_reward_amount = parse_token_amount(_strip_token_suffix(aggregator_reward), decimals)
    branch_stake_amount = parse_token_amount(_strip_token_suffix(branch_required_stake), decimals)
    aggregate_stake_amount = parse_token_amount(_strip_token_suffix(aggregate_required_stake), decimals)
    ipfs_url = _ipfs_api_url(ipfs_api_url)
    _ipfs_check(ipfs_url)

    base_nonce = random_base_nonce(branches)
    branch_nonces, aggregate_nonce = planned_nonces(base_nonce, branches)
    aggregate_job = proto.job_pda(signer.pubkey, aggregate_nonce)
    output_schema = _prediction_output_schema(output_kind, labels, forecast_horizon)
    output_schema_hash = sha256(_canonical_json_bytes(output_schema)).hex()
    context_text, context_hash = _read_optional_text(context_file)
    personas, personas_hash = _read_optional_personas(personas_file)
    parent_manifest: dict[str, Any] = {
        "schema_version": 1,
        "question": question,
        "branches": branches,
        "combiner": combiner,
        "combiner_parameters": parameters,
        "persona_set": persona_set,
        "output_schema": output_schema,
        "output_schema_hash": output_schema_hash,
        "aggregate_job": str(aggregate_job),
        "aggregate_image_id": image_id.hex(),
        "created_at_unix": int(time.time()),
    }
    if context_file:
        parent_manifest["context"] = {"path": str(context_file), "sha256": context_hash}
    if num_ctx is not None:
        parent_manifest["llm_options"] = {"num_ctx": num_ctx}
    if personas_file:
        parent_manifest["persona_library"] = {"path": str(personas_file), "sha256": personas_hash, "count": len(personas)}
    parent_manifest_cid = _ipfs_add_json(ipfs_url, "parent-manifest.json", parent_manifest)
    run_seed_commitment = sha256(_canonical_json_bytes({"question": question, "parent_manifest_cid": parent_manifest_cid, "aggregate_job": str(aggregate_job)}))

    # Phase 1: every artifact is pinned and every job is planned before any escrow moves.
    branch_jobs: list[dict[str, Any]] = []
    for branch_index, nonce in enumerate(branch_nonces):
        branch_job = proto.job_pda(signer.pubkey, nonce)
        rng_seed = int.from_bytes(sha256(bytes(aggregate_job) + branch_index.to_bytes(4, "little") + run_seed_commitment)[:8], "little")
        branch_input = {
            "schema_version": 1,
            "parent_job": str(aggregate_job),
            "branch_index": branch_index,
            "seed": question,
            "parameters": {
                "combiner": combiner,
                "combiner_parameters": parameters,
                "forecast_horizon": forecast_horizon,
                "labels": [item.strip() for item in labels.split(",")] if labels else [],
                "output_schema_hash": output_schema_hash,
                "parent_manifest_cid": parent_manifest_cid,
                "context": context_text,
                "context_sha256": context_hash,
                "num_ctx": num_ctx,
                "persona": personas[branch_index % len(personas)] if personas else None,
                "persona_library_sha256": personas_hash,
            },
            "persona_set_cid": persona_set,
            "rng_seed": rng_seed,
            "target_output_kind": output_kind,
            "scalar_grid_bps": 1 if output_kind in SCALAR_OUTPUT_KINDS else None,
        }
        input_bytes = _canonical_json_bytes(branch_input)
        input_cid = _ipfs_add_bytes(ipfs_url, f"branch-{branch_index}-input.json", input_bytes)
        branch_jobs.append(
            {
                "kind": "branch",
                "branch_index": branch_index,
                "nonce": nonce,
                "job": str(branch_job),
                "input_cid": input_cid,
                "input_bundle_hash": sha256(input_bytes).hex(),
                "expected_result_hash": ZERO_HASH.hex(),
                "job_class": "branch-proof",
                "required_capability": "worker-proof",
                "required_software_digest": SOFTWARE_DIGEST["worker-canonical"].hex(),
                "reward_amount": branch_reward_amount,
                "required_stake": branch_stake_amount,
                "challenge_bond": branch_stake_amount,
                "status": JOB_PLANNED,
            }
        )

    # The aggregate PLAN, not the aggregate job's input artifact. The artifact the
    # reducer consumes carries the branch receipts, which do not exist yet, and
    # `open_job` fixes `input_bundle_hash` and `expected_result_hash` for good. So the
    # aggregate job is opened later, by `predict bind-aggregate`, once its branches have
    # settled. The plan is pinned now as provenance: it names the branch jobs, the
    # combiner and the reducer image this run committed to before any branch ran.
    aggregate_plan = {
        "schema_version": 3,
        "parent_run": str(aggregate_job),
        "parent_manifest_cid": parent_manifest_cid,
        "branch_jobs": [{"branch_index": item["branch_index"], "job": item["job"], "nonce": item["nonce"], "input_cid": item["input_cid"]} for item in branch_jobs],
        "combiner": combiner,
        "combiner_parameters": parameters,
        "output_schema_hash": output_schema_hash,
        "bonsol": {"image_id": image_id.hex(), "public_input": PUBLIC_INPUT_RULE, "framing": FRAMING_RULE},
    }
    aggregate_plan_cid = _ipfs_add_bytes(ipfs_url, "aggregate-plan.json", _canonical_json_bytes(aggregate_plan))
    aggregate_entry = {
        "kind": "aggregate",
        "branch_index": None,
        "nonce": aggregate_nonce,
        "job": str(aggregate_job),
        # Filled in by `predict bind-aggregate` from the settled branch receipts.
        "input_cid": None,
        "input_bundle_hash": None,
        "expected_result_hash": None,
        "job_class": "aggregate-proof",
        "required_capability": "branch-aggregator-bonsol",
        "required_software_digest": image_id.hex(),
        "reward_amount": aggregate_reward_amount,
        "required_stake": aggregate_stake_amount,
        "challenge_bond": aggregate_stake_amount,
        "status": JOB_DEFERRED,
    }

    run: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": RUN_OPENING,
        "parent_run": str(aggregate_job),
        "base_nonce": base_nonce,
        "customer": str(signer.pubkey),
        "customer_wallet": customer,
        "cluster": c.cluster_name,
        "rpc_url": c.rpc_url,
        "ipfs_api_url": ipfs_url,
        "parent_manifest_cid": parent_manifest_cid,
        "parent_manifest": parent_manifest,
        "aggregate_plan_cid": aggregate_plan_cid,
        "aggregate_plan": aggregate_plan,
        "aggregate_input_cid": None,
        "aggregate_job": str(aggregate_job),
        "aggregate_nonce": aggregate_nonce,
        "aggregate": aggregate_entry,
        "aggregate_image_id": image_id.hex(),
        "bonsol": {"bound": False, "reason": "aggregate job is opened by predict bind-aggregate", "image_id": image_id.hex()},
        "branch_jobs": branch_jobs,
        "combiner": combiner,
        "combiner_parameters": parameters,
        "open_parameters": {
            "required_role": "worker-proof",
            "required_tier": "T1",
            "claim_window": claim_window,
            "execution_window": execution_window,
            "challenge_window": challenge_window,
        },
        "aggregate_submitted": False,
        "aggregate_open_deferred": True,
        "created_at_unix": int(time.time()),
        "updated_at_unix": int(time.time()),
    }
    path = _predict_run_path(run["parent_run"])
    if path.exists():
        raise typer.BadParameter(f"run manifest already exists: {path}")
    _assert_jobs_absent(rpc, [entry["job"] for entry in run_job_entries(run)])
    save_run_manifest(path, run)
    _announce_run(run, path)
    typer.echo(
        f"aggregate job {aggregate_job} is planned, not opened: its input artifact carries the "
        "branch receipts, which do not exist yet. Run `kswarm predict bind-aggregate "
        f"{aggregate_job}` once the branches have settled.",
        err=True,
    )
    _drive_run_open(rpc, signer, proto, run, path)
    emit(c, _run_open_payload(run, path))


@predict_app.command("resume")
def predict_resume(
    ctx: typer.Context,
    parent_run: str = typer.Argument(..., help="Parent run pubkey printed by predict open."),
    customer: str | None = typer.Option(None, "--as", help="Customer wallet name. Defaults to the wallet recorded in the run manifest."),
) -> None:
    """Continue an interrupted `predict open` from its run manifest.

    Every planned job is checked on chain first, so a transaction that was
    confirmed after the manifest was last written is not sent twice.
    """

    c = _ctx(ctx)
    run, path = _load_predict_run(parent_run)
    resumable, reason = run_is_resumable(run)
    if not resumable:
        if reason == ALREADY_OPEN_REASON:
            emit(c, {"status": "already-open", **_run_open_payload(run, path)})
            return
        raise typer.BadParameter(reason)
    if run["cluster"] != c.cluster_name:
        raise typer.BadParameter(f"run belongs to cluster {run['cluster']!r}; pass --cluster {run['cluster']}")
    rpc = _rpc(c)
    proto, _ = _payment_context(c, rpc)
    signer = load_wallet(customer or str(run["customer_wallet"]))
    if str(signer.pubkey) != run["customer"]:
        raise typer.BadParameter(f"wallet {signer.name} is {signer.pubkey}, not the run customer {run['customer']}")
    _reconcile_run_with_chain(rpc, run)
    save_run_manifest(path, run)
    _announce_run(run, path)
    _drive_run_open(rpc, signer, proto, run, path)
    emit(c, _run_open_payload(run, path))


@predict_app.command("bind-aggregate")
def predict_bind_aggregate(
    ctx: typer.Context,
    parent_run: str = typer.Argument(..., help="Parent run pubkey printed by predict open."),
    customer: str | None = typer.Option(None, "--as", help="Customer wallet name. Defaults to the wallet recorded in the run manifest."),
    allow_completed_branches: bool = typer.Option(
        False,
        "--allow-completed-branches",
        help="Bind against branches that are Completed and attested but not yet settled.",
    ),
    aggregate_image_id: str | None = typer.Option(
        None,
        "--aggregate-image-id",
        help=f"Bonsol aggregate reducer image id (32-byte hex). Defaults to ${IMAGE_ID_ENV}, then the checked-in pin.",
    ),
    ipfs_api_url: str | None = typer.Option(None, "--ipfs-api-url", help="Override local IPFS API URL."),
) -> None:
    """Open the run's aggregate job, bound to the artifact its branch receipts produce.

    The artifact carries every branch's on-chain receipt bytes. The aggregate reducer
    guest rehashes those bytes, decodes the branch values out of them, applies the
    combiner and commits the result, so the journal this command predicts is a claim
    about receipts that already settled -- not about a summary anyone typed.

    `open_job` fixes `input_bundle_hash` and `expected_result_hash` for good, so this
    is the moment the aggregate job becomes provable. If the prediction here and the
    guest's reduction ever disagreed, `settle_aggregate_proof_job` would refuse the
    marker forever; `cli/tests/test_aggregate_artifact.py` and
    `protocol/bonsol-aggregate-reducer/tests/cross_language_vectors.rs` pin both to one
    set of vectors so they cannot.
    """

    c = _ctx(ctx)
    run, path = _load_predict_run(parent_run)
    if run["cluster"] != c.cluster_name:
        raise typer.BadParameter(f"run belongs to cluster {run['cluster']!r}; pass --cluster {run['cluster']}")
    entry = run.get("aggregate")
    if entry is None:
        raise typer.BadParameter("run manifest has no aggregate job")
    if job_entry_status(entry) == JOB_COMMITTED:
        emit(c, {"status": "already-bound", **_run_open_payload(run, path)})
        return
    if job_entry_status(entry) not in {JOB_DEFERRED, JOB_PLANNED, JOB_OPENED}:
        raise typer.BadParameter(f"aggregate job is {job_entry_status(entry)!r}; nothing to bind")

    try:
        image_id = resolve_aggregate_image_id(aggregate_image_id, os.environ)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    rpc = _rpc(c)
    proto, _ = _payment_context(c, rpc)
    signer = load_wallet(customer or str(run["customer_wallet"]))
    if str(signer.pubkey) != run["customer"]:
        raise typer.BadParameter(f"wallet {signer.name} is {signer.pubkey}, not the run customer {run['customer']}")
    ipfs_url = _ipfs_api_url(ipfs_api_url or run.get("ipfs_api_url"))
    _ipfs_check(ipfs_url)

    branches = _collect_branch_receipts(rpc, run, allow_completed_branches)
    plan_cid = _check_against_aggregate_plan(ipfs_url, run, branches, image_id)
    manifest = run["parent_manifest"]
    parameters = dict(run["combiner_parameters"])
    if run["combiner"] == "majority-vote":
        dictionary = manifest.get("output_schema", {}).get("category_dictionary")
        if not isinstance(dictionary, list) or not dictionary:
            raise typer.BadParameter("majority-vote runs need parent_manifest.output_schema.category_dictionary")
        parameters["category_dictionary_size"] = len(dictionary)

    try:
        artifact = build_aggregate_artifact(
            parent_run=run["parent_run"],
            parent_manifest_cid=run["parent_manifest_cid"],
            output_schema_hash=manifest["output_schema_hash"],
            combiner=run["combiner"],
            combiner_parameters=parameters,
            branches=branches,
            aggregate_plan_cid=plan_cid,
        )
        journal = aggregate_journal(artifact)
    except AggregateError as exc:
        # No binding exists, so opening the job would fund one that can never settle.
        raise typer.BadParameter(f"the aggregate reducer would reject this artifact: {exc}") from exc

    aggregate_input_cid = _ipfs_add_bytes(ipfs_url, "aggregate-input.json", artifact)
    entry["input_cid"] = aggregate_input_cid
    entry["input_bundle_hash"] = journal.input_digest.hex()
    entry["expected_result_hash"] = journal.journal_hash.hex()
    entry["required_software_digest"] = image_id.hex()
    entry["status"] = JOB_PLANNED
    run["aggregate_input_cid"] = aggregate_input_cid
    run["aggregate_image_id"] = image_id.hex()
    run["bonsol"] = {
        "bound": True,
        "image_id": image_id.hex(),
        "public_input": PUBLIC_INPUT_RULE,
        "framing": FRAMING_RULE,
        **journal.to_json(),
    }
    run["status"] = RUN_OPENING
    run["updated_at_unix"] = int(time.time())
    save_run_manifest(path, run)
    _announce_run(run, path)
    _assert_jobs_absent(rpc, [entry["job"]])
    _drive_run_open(rpc, signer, proto, run, path)
    emit(c, _run_open_payload(run, path))


def _collect_branch_receipts(rpc: RpcClient, run: dict[str, Any], allow_completed: bool) -> list[dict[str, Any]]:
    """The on-chain receipt bytes of every branch, or a refusal naming the branch.

    The bytes come from the branch job accounts, not from IPFS: `submitted_result_hash`
    is `sha256(result_bytes)`, so binding the aggregate to these bytes binds it to what
    the chain already accepted.
    """

    settled = JOB_STATUS_BY_NAME["settled"]
    completed = JOB_STATUS_BY_NAME["submitted"]
    branches: list[dict[str, Any]] = []
    for item in run["branch_jobs"]:
        job_key = Pubkey.from_string(item["job"])
        job = fetch_job(rpc, job_key)
        if job is None:
            raise typer.BadParameter(f"branch {item['branch_index']} job {job_key} does not exist")
        ready = job.status == settled or (allow_completed and job.status == completed and job.verifier_attestation_hash is not None)
        if not ready:
            raise typer.BadParameter(
                f"branch {item['branch_index']} job {job_key} is {JOB_STATUS.get(job.status, job.status)!r}; "
                "aggregate binding needs every branch settled (or --allow-completed-branches with an attestation)"
            )
        if not job.result_bytes:
            raise typer.BadParameter(f"branch {item['branch_index']} job {job_key} carries no receipt bytes")
        if sha256(bytes(job.result_bytes)) != job.submitted_result_hash:
            raise typer.BadParameter(
                f"branch {item['branch_index']} job {job_key}: on-chain result_bytes do not hash to submitted_result_hash"
            )
        branches.append(
            {
                "branch_index": int(item["branch_index"]),
                "job": str(job_key),
                "output_cid": job.output_cid,
                "result_bytes": bytes(job.result_bytes).hex(),
                "weight": 1,
            }
        )
    return branches


def _check_against_aggregate_plan(
    ipfs_url: str,
    run: dict[str, Any],
    branches: list[dict[str, Any]],
    image_id: bytes,
) -> str | None:
    """Refuse a binding that departs from the plan `predict open` pinned.

    `open_job` fixes the aggregate job's hashes at bind time, which is after every
    branch result is visible. Without this check the combiner, its parameters and the
    branch set are whatever the local run manifest says at that moment, and nothing
    outside this machine records what the run committed to beforehand. The plan is
    content-addressed and was pinned before any branch ran, so comparing against it --
    and carrying its CID inside the artifact, where `input_bundle_hash` commits it --
    is what makes the answer checkable by someone who did not run this command.

    A run with no pinned plan (an older manifest) is allowed through with a warning
    rather than refused: the plan is provenance, and refusing would strand a run that
    was opened before the field existed.
    """

    plan_cid = run.get("aggregate_plan_cid")
    if not plan_cid:
        typer.echo(
            "warning: this run has no pinned aggregate plan, so the combiner and the branch set "
            "cannot be checked against what it committed to before its branches ran",
            err=True,
        )
        return None

    try:
        plan = _ipfs_cat_json(ipfs_url, str(plan_cid))
    except Exception as exc:  # noqa: BLE001 - any fetch failure is a refusal
        raise typer.BadParameter(
            f"cannot read the pinned aggregate plan {plan_cid}: {exc}. The plan is what says which "
            "combiner and which branches this run committed to before its branches ran; binding "
            "without it would fix the job's hashes against an unrecorded choice."
        ) from exc

    differences: list[str] = []
    if plan.get("combiner") != run["combiner"]:
        differences.append(f"combiner {run['combiner']!r} is not the planned {plan.get('combiner')!r}")
    if dict(plan.get("combiner_parameters") or {}) != dict(run["combiner_parameters"]):
        differences.append(
            f"combiner_parameters {dict(run['combiner_parameters'])!r} are not the planned "
            f"{dict(plan.get('combiner_parameters') or {})!r}"
        )
    planned_image = str((plan.get("bonsol") or {}).get("image_id", ""))
    if planned_image and planned_image != image_id.hex():
        differences.append(f"reducer image {image_id.hex()} is not the planned {planned_image}")
    planned_jobs = [str(item["job"]) for item in (plan.get("branch_jobs") or [])]
    bound_jobs = [str(item["job"]) for item in branches]
    if planned_jobs and planned_jobs != bound_jobs:
        missing = [job for job in planned_jobs if job not in bound_jobs]
        extra = [job for job in bound_jobs if job not in planned_jobs]
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"extra {extra}")
        differences.append("branch set is not the planned set" + (": " + ", ".join(detail) if detail else ""))
    if differences:
        raise typer.BadParameter(
            "this binding departs from the aggregate plan pinned at "
            f"{plan_cid} before any branch ran: " + "; ".join(differences)
        )
    return str(plan_cid)


@predict_app.command("status")
def predict_status(ctx: typer.Context, parent_run: str = typer.Argument(..., help="Parent run pubkey returned by predict open.")) -> None:
    """Show on-chain and manifest status for every job of a prediction run."""

    c = _ctx(ctx)
    rpc = _rpc(c)
    run, path = _load_predict_run(parent_run)
    rows = []
    for entry in run_job_entries(run):
        job = fetch_job(rpc, Pubkey.from_string(entry["job"]))
        rows.append(_prediction_status_row(entry, job))
    emit(
        c,
        {
            "parent_run": parent_run,
            "run_status": run_status(run),
            "base_nonce": run.get("base_nonce"),
            "aggregate_open_deferred": bool(run.get("aggregate_open_deferred", False)),
            "run_manifest": str(path),
            "jobs": rows,
        },
    )


@predict_app.command("report")
def predict_report(
    ctx: typer.Context,
    parent_run: str = typer.Argument(..., help="Parent run pubkey returned by predict open."),
    branch_excerpts: int = typer.Option(3, "--branch-excerpts", help="Number of branch narratives to include."),
) -> None:
    """Fetch the aggregate output and selected branch narratives."""

    c = _ctx(ctx)
    rpc = _rpc(c)
    run, _ = _load_predict_run(parent_run)
    ipfs_url = run["ipfs_api_url"]
    aggregate_job = fetch_job(rpc, Pubkey.from_string(run["aggregate_job"]))
    aggregate_output = _ipfs_cat_json(ipfs_url, aggregate_job.output_cid) if aggregate_job and aggregate_job.output_cid else None
    excerpts = []
    for branch in run["branch_jobs"]:
        if len(excerpts) >= branch_excerpts:
            break
        job = fetch_job(rpc, Pubkey.from_string(branch["job"]))
        if not job or not job.output_cid:
            continue
        output = _ipfs_cat_json(ipfs_url, job.output_cid)
        text = output.get("narrative_text") if isinstance(output, dict) else None
        if text:
            excerpts.append({"branch_index": branch["branch_index"], "job": branch["job"], "narrative_text": text[:600]})
    final_scalar_bps = None
    if isinstance(aggregate_output, dict):
        result = aggregate_output.get("result") or {}
        final_scalar_bps = result.get("scalar_value_bps") if isinstance(result, dict) else None
    emit(
        c,
        {
            "parent_run": parent_run,
            "question": run["parent_manifest"]["question"],
            "final_scalar_bps": final_scalar_bps,
            "final_scalar": scalar_bps_to_probability(final_scalar_bps),
            "aggregate_job": aggregate_job.to_json() if aggregate_job else None,
            "aggregate_output": aggregate_output,
            "branch_narrative_excerpts": excerpts,
        },
    )


@predict_app.command("cancel")
def predict_cancel(
    ctx: typer.Context,
    parent_run: str = typer.Argument(..., help="Parent run pubkey returned by predict open."),
    customer: str = typer.Option("customer", "--as", help="Customer wallet name."),
) -> None:
    """Cancel every job of a run that is still awaiting artifact or open, then mark the run cancelled.

    Works on a partially opened run: jobs that never reached the chain are
    skipped and marked cancelled in the manifest; claimed or settled jobs are
    left alone and reported.
    """

    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = load_wallet(customer)
    proto, _ = _payment_context(c, rpc)
    run, path = _load_predict_run(parent_run)
    cancelled: list[str] = []
    skipped: list[dict[str, Any]] = []
    for entry in run_job_entries(run):
        job_key = Pubkey.from_string(entry["job"])
        job = fetch_job(rpc, job_key)
        if job is None:
            # A deferred aggregate never reached the chain either, so cancelling the run
            # must retire it in the manifest; otherwise `predict bind-aggregate` would
            # still offer to open it against branches that were cancelled.
            if job_entry_status(entry) in PENDING_JOB_STATUSES or job_entry_status(entry) == JOB_DEFERRED:
                entry["status"] = JOB_CANCELLED
            continue
        if job.status in {JOB_STATUS_BY_NAME["awaiting-artifact"], JOB_STATUS_BY_NAME["open"]}:
            sign_and_send(rpc, signer.keypair, [cancel_open_job_ix(proto, signer.pubkey, job_key)])
            entry["status"] = JOB_CANCELLED
            cancelled.append(entry["job"])
            save_run_manifest(path, run)
        else:
            skipped.append({"job": entry["job"], "status": JOB_STATUS.get(job.status, f"unknown-{job.status}")})
    run["status"] = RUN_CANCELLED
    run["updated_at_unix"] = int(time.time())
    save_run_manifest(path, run)
    emit(c, {"parent_run": parent_run, "run_status": RUN_CANCELLED, "cancelled_jobs": cancelled, "skipped_jobs": skipped})


@inspect_app.command("job")
def inspect_job(ctx: typer.Context, pubkey: str = typer.Argument(..., help="Job account pubkey.")) -> None:
    """Inspect full decoded job state with interpreted status.

    Example: kswarm inspect job <job>
    """
    job_show(ctx, pubkey)


@inspect_app.command("worker")
def inspect_worker(ctx: typer.Context, pubkey: str = typer.Argument(..., help="Worker account pubkey.")) -> None:
    """Inspect full decoded worker state.

    Example: kswarm inspect worker <worker-pda>
    """
    worker_show(ctx, pubkey)


@inspect_app.command("marker")
def inspect_marker(
    ctx: typer.Context,
    execution_id: str | None = typer.Argument(None, help="Bonsol execution id or 32-byte hex id."),
    image_id: str | None = typer.Option(None, "--image-id", help="Optional image id hex filter."),
    job: str | None = typer.Option(None, "--job", help="Aggregate job pubkey. Lists every marker recorded for that job."),
) -> None:
    """Inspect BonsolAggregateVerification marker PDAs.

    By execution id, or by job when the caller knows which job it funded but not which
    execution proved it -- the execution id is chosen by whatever requested the proof.

    Example: kswarm inspect marker p0b-happy-1700000000
    Example: kswarm inspect marker --job <aggregate-job>
    """
    c = _ctx(ctx)
    if (execution_id is None) == (job is None):
        raise typer.BadParameter("pass an execution id or --job, not both and not neither")
    execution = parse_hash(execution_id) if execution_id else None
    aggregate_job = Pubkey.from_string(job) if job else None
    image = parse_hash(image_id) if image_id else None
    rows = []
    for pubkey, marker in fetch_all_markers(_rpc(c), _program_id(c)):
        if execution is not None and marker.execution_id != execution:
            continue
        if aggregate_job is not None and marker.aggregate_job != aggregate_job:
            continue
        if image and marker.image_id != image:
            continue
        payload = marker.to_json()
        payload["marker"] = str(pubkey)
        rows.append(payload)
    emit(c, rows)


@inspect_app.command("protocol-config")
def inspect_protocol_config(ctx: typer.Context) -> None:
    """Inspect the protocol config PDA.

    Example: kswarm inspect protocol-config
    """
    protocol_show(ctx)


@inspect_app.command("events")
def inspect_events(
    ctx: typer.Context,
    job: str = typer.Option(..., "--job", help="Job account pubkey."),
    limit: int = typer.Option(25, "--limit", help="Signature scan limit."),
) -> None:
    """Show recent transaction logs for a job account.

    Example: kswarm inspect events --job <job>
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    events = []
    for item in rpc.get_signatures_for_address(job, limit):
        tx = rpc.get_transaction(item["signature"])
        logs = (((tx or {}).get("meta") or {}).get("logMessages") or [])
        events.append({"signature": item["signature"], "slot": item.get("slot"), "logs": logs})
    emit(c, events)


@admin_app.command("slash-stale")
def admin_slash_stale(ctx: typer.Context, pubkey: str = typer.Argument(..., help="Claimed job account pubkey.")) -> None:
    """Slash a stale claimed job whose execution window expired.

    Example: kswarm admin slash-stale <job>
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = _default_signer(c)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(pubkey)
    job = _require_job(rpc, job_key)
    signature = sign_and_send(rpc, signer, [slash_stale_job_ix(proto, signer.pubkey(), job_key, job)])
    emit_signature(c, signature, {"job": str(job_key)})


@admin_app.command("cancel-open")
def admin_cancel_open(
    ctx: typer.Context,
    pubkey: str = typer.Argument(..., help="Awaiting-artifact or open job account pubkey."),
    customer: str = typer.Option(..., "--as", help="Customer wallet name."),
) -> None:
    """Cancel an awaiting-artifact or open job.

    Example: kswarm admin cancel-open <job> --as customer
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    signer = load_wallet(customer)
    proto, _ = _payment_context(c, rpc)
    job_key = Pubkey.from_string(pubkey)
    signature = sign_and_send(rpc, signer.keypair, [cancel_open_job_ix(proto, signer.pubkey, job_key)])
    emit_signature(c, signature, {"job": str(job_key)})


@admin_app.command("record-aggregate-verification")
def admin_record_aggregate_verification(
    ctx: typer.Context,
    aggregate_job: str = typer.Option(..., "--aggregate-job", help="Aggregate job account pubkey."),
    bonsol_execution_keypair: Path = typer.Option(..., "--bonsol-execution-keypair", help="Bonsol execution account keypair path."),
    execution_id: str = typer.Option(..., "--execution-id", help="Execution id or 32-byte hex."),
    image_id: str = typer.Option(..., "--image-id", help="Image id hex."),
    input_digest: str = typer.Option(..., "--input-digest", help="Input digest hex."),
    output_digest: str = typer.Option(..., "--output-digest", help="Output digest hex."),
    journal_hash: str = typer.Option(..., "--journal-hash", help="Journal hash hex."),
    forwarded_payload: str = typer.Option(..., "--forwarded-payload", help="Forwarded Bonsol payload hex."),
) -> None:
    """Debug wrapper for the raw Bonsol marker callback instruction.

    Example: kswarm admin record-aggregate-verification --aggregate-job <job> --bonsol-execution-keypair execution.json --execution-id p0b --image-id <hex> --input-digest <hex> --output-digest <hex> --journal-hash <hex> --forwarded-payload <hex>
    """
    c = _ctx(ctx)
    rpc = _rpc(c)
    payer = _default_signer(c)
    program_id = _program_id(c)
    execution = load_keypair_file(bonsol_execution_keypair)
    job_key = Pubkey.from_string(aggregate_job)
    execution_bytes = parse_hash(execution_id)
    image = parse_hash(image_id)
    input_hash = parse_hash(input_digest)
    output_hash = parse_hash(output_digest)
    journal = parse_hash(journal_hash)
    marker = bonsol_marker_pda(program_id, job_key, execution_bytes, image, input_hash, journal)
    ix = record_aggregate_verification_raw_ix(
        program_id,
        execution.pubkey(),
        marker,
        job_key,
        execution_bytes,
        image,
        input_hash,
        output_hash,
        journal,
        bytes.fromhex(forwarded_payload.removeprefix("0x")),
    )
    signature = sign_and_send(rpc, payer, [ix], [execution])
    emit_signature(c, signature, {"marker": str(marker), "job": str(job_key)})


PREDICT_RUNS_DIR = predict_runs_dir()


def _predict_run_path(parent_run: str) -> Path:
    return PREDICT_RUNS_DIR / f"{parent_run}.json"


def _load_predict_run(parent_run: str) -> tuple[dict[str, Any], Path]:
    path = _predict_run_path(parent_run)
    if not path.exists():
        raise typer.BadParameter(f"unknown local prediction run: {parent_run}")
    return load_run_manifest(path), path


def _announce_run(run: dict[str, Any], path: Path) -> None:
    """First line of output, on stderr so `--json` stdout stays one document."""

    typer.echo(f"parent_run={run['parent_run']} base_nonce={run['base_nonce']} run_manifest={path}", err=True)


def _assert_jobs_absent(rpc: RpcClient, job_keys: list[str]) -> None:
    """Nonce collision guard: no planned job PDA may exist before escrow is spent."""

    existing = [key for key, info in zip(job_keys, rpc.get_multiple_account_infos(job_keys)) if info is not None]
    if existing:
        raise typer.BadParameter(f"planned job account already exists (nonce collision): {', '.join(existing)}; run predict open again")


def _open_planned_job(rpc: RpcClient, signer, proto: ProtocolAddresses, run: dict[str, Any], entry: dict[str, Any]) -> str:
    params = run["open_parameters"]
    expected_job = proto.job_pda(signer.pubkey, int(entry["nonce"]))
    if str(expected_job) != entry["job"]:
        raise typer.BadParameter(f"manifest job {entry['job']} does not derive from nonce {entry['nonce']} for {signer.pubkey}")
    ix = open_job_ix(
        proto,
        signer.pubkey,
        int(entry["nonce"]),
        bytes.fromhex(entry["input_bundle_hash"]),
        bytes.fromhex(entry["expected_result_hash"]),
        int(entry["reward_amount"]),
        int(entry["required_stake"]),
        JOB_CLASS[entry["job_class"]],
        NODE_ROLE[params["required_role"]],
        STAKE_TIER[params["required_tier"]],
        CAPABILITY_CLASS[entry["required_capability"]],
        bytes.fromhex(entry["required_software_digest"]),
        int(params["claim_window"]),
        int(params["execution_window"]),
        int(params["challenge_window"]),
        int(entry["challenge_bond"]),
    )
    return sign_and_send(rpc, signer.keypair, [ix])


def _drive_run_open(rpc: RpcClient, signer, proto: ProtocolAddresses, run: dict[str, Any], path: Path) -> None:
    """Open and commit every pending job, saving the manifest after each confirmed transaction."""

    for entry in pending_job_entries(run):
        label = "aggregate" if entry["kind"] == "aggregate" else f"branch {entry['branch_index']}"
        if entry["status"] == JOB_PLANNED:
            signature = _open_planned_job(rpc, signer, proto, run, entry)
            entry["status"] = JOB_OPENED
            entry["open_signature"] = signature
            run["updated_at_unix"] = int(time.time())
            save_run_manifest(path, run)
            typer.echo(f"{label} opened job={entry['job']}", err=True)
        if entry["status"] == JOB_OPENED:
            signature = sign_and_send(
                rpc,
                signer.keypair,
                [commit_input_artifact_ix(proto.program_id, signer.pubkey, Pubkey.from_string(entry["job"]), entry["input_cid"])],
            )
            entry["status"] = JOB_COMMITTED
            entry["commit_signature"] = signature
            run["updated_at_unix"] = int(time.time())
            save_run_manifest(path, run)
            typer.echo(f"{label} committed input_cid={entry['input_cid']}", err=True)
    run["status"] = RUN_OPEN
    run["updated_at_unix"] = int(time.time())
    save_run_manifest(path, run)


def _reconcile_run_with_chain(rpc: RpcClient, run: dict[str, Any]) -> None:
    """Set each pending entry's status from the job account, so resume never repeats a confirmed step."""

    for entry in pending_job_entries(run):
        job = fetch_job(rpc, Pubkey.from_string(entry["job"]))
        if job is None:
            entry["status"] = JOB_PLANNED
            continue
        if str(job.customer) != run["customer"]:
            raise typer.BadParameter(f"job {entry['job']} belongs to {job.customer}, not the run customer {run['customer']}")
        entry["status"] = JOB_OPENED if job.status == JOB_STATUS_BY_NAME["awaiting-artifact"] else JOB_COMMITTED


def _run_open_payload(run: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "parent_run": run["parent_run"],
        "base_nonce": run["base_nonce"],
        "run_status": run_status(run),
        "run_manifest": str(path),
        "parent_manifest_cid": run["parent_manifest_cid"],
        "aggregate_job": run["aggregate_job"],
        "aggregate_input_cid": run["aggregate_input_cid"],
        "aggregate": run["aggregate"],
        "bonsol": run["bonsol"],
        "combiner": run["combiner"],
        "combiner_parameters": run["combiner_parameters"],
        "branch_jobs": run["branch_jobs"],
    }


def _read_optional_text(path: Path | None) -> tuple[str | None, str | None]:
    if path is None:
        return None, None
    payload = path.read_text(encoding="utf-8")
    return payload, sha256(payload.encode("utf-8")).hex()


def _read_optional_personas(path: Path | None) -> tuple[list[Any], str | None]:
    if path is None:
        return [], None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise typer.BadParameter("--personas-file must contain a non-empty JSON array")
    encoded = _canonical_json_bytes(payload)
    return payload, sha256(encoded).hex()


def _prediction_output_schema(output_kind: str, labels: str | None, forecast_horizon: str) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "schema_version": 1,
        "output_kind": output_kind,
        "forecast_horizon": forecast_horizon,
    }
    if output_kind in {"scalar", "narrative_with_scalar"}:
        schema.update({"units": "probability", "scale": "basis_points", "valid_range": [0, 10000]})
    if output_kind == "categorical":
        parsed_labels = [item.strip() for item in labels.split(",")] if labels else []
        if not parsed_labels:
            raise typer.BadParameter("categorical output requires --labels")
        schema.update({"category_dictionary": parsed_labels, "tie_break": "lowest_label_index"})
    if output_kind == "narrative_with_scalar":
        schema.update(
            {
                "tier": "B",
                "guardrail_scores": ["severity_bps", "quality_bps", "ood_bps"],
                "disclosure": "Narrative text is hash-committed provenance; guardrail scalars are the verified surface.",
            }
        )
    return schema


def _prediction_status_row(entry: dict[str, Any], job) -> dict[str, Any]:
    data = job.to_json() if job else {}
    return {
        "kind": entry["kind"] if "kind" in entry else ("aggregate" if entry.get("branch_index") is None else "branch"),
        "branch_index": entry.get("branch_index"),
        "job": entry["job"],
        "manifest_status": job_entry_status(entry),
        "status": data.get("status_name", "missing"),
        "worker": data.get("worker"),
        "input_cid": data.get("input_cid"),
        "output_cid": data.get("output_cid"),
        "verifier": data.get("verifier_authority"),
        "verifier_hash": data.get("verifier_attestation_hash"),
    }


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _strip_token_suffix(value: str) -> str:
    """Accept `1KAI` as well as `1` for human amounts."""
    normalized = value.strip()
    suffix = PAYMENT_TOKEN_SYMBOL.lower()
    if normalized.lower().endswith(suffix):
        return normalized[: -len(suffix)].strip()
    return normalized


def _ipfs_api_url(value: str | None) -> str:
    return ipfs_api_url_for(value)


def _ipfs_check(api_url: str) -> None:
    try:
        ipfs_check(api_url)
    except IpfsError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _ipfs_add_bytes(api_url: str, filename: str, payload: bytes) -> str:
    try:
        return ipfs_add_bytes(api_url, filename, payload)
    except IpfsError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _ipfs_add_json(api_url: str, filename: str, payload: Any) -> str:
    return _ipfs_add_bytes(api_url, filename, _canonical_json_bytes(payload))


def _ipfs_cat_json(api_url: str, cid: str) -> Any:
    try:
        return ipfs_cat_json(api_url, cid, max_bytes=max_artifact_bytes())
    except IpfsError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _ctx(ctx: typer.Context) -> CliContext:
    if not isinstance(ctx.obj, CliContext):
        raise RuntimeError("CLI context was not initialized")
    return ctx.obj


def _rpc(ctx: CliContext) -> RpcClient:
    return RpcClient(ctx.rpc_url, ctx.commitment)


def _default_signer(ctx: CliContext) -> Keypair:
    if ctx.keypair_path:
        return load_keypair_file(ctx.keypair_path)
    return load_active_wallet().keypair


def _mint_authority(ctx: CliContext) -> Keypair:
    authority_wallet = ctx.cluster_config.get("mint_authority_wallet")
    if authority_wallet:
        return load_wallet(str(authority_wallet)).keypair
    return _default_signer(ctx)


def _program_id(ctx: CliContext) -> Pubkey:
    value = ctx.cluster_config.get("program_id")
    if not value:
        raise typer.BadParameter(
            f"cluster profile '{ctx.cluster_name}' has no program_id: the protocol program is not deployed there yet. "
            f"Add \"program_id\" to {cluster_path(ctx.cluster_name)} once it is."
        )
    return Pubkey.from_string(str(value))


def _payment_context(ctx: CliContext, rpc: RpcClient) -> tuple[ProtocolAddresses, int]:
    """Protocol addresses plus payment decimals.

    The on-chain config is authoritative once the protocol is initialized. Before that,
    the cluster profile supplies the mint; its token program and decimals are read from
    chain once and cached in the profile.
    """
    program_id = _program_id(ctx)
    config = fetch_config(rpc, program_id)
    try:
        if config:
            return config.addresses(program_id), config.payment_decimals
        mint_value = ctx.cluster_config.get("payment_mint")
        if not mint_value:
            raise typer.BadParameter("payment mint is unknown; run token create-mint or protocol initialize first")
        mint = Pubkey.from_string(str(mint_value))
        token_program_value = ctx.cluster_config.get("token_program")
        decimals_value = ctx.cluster_config.get("payment_decimals")
        if token_program_value is None or decimals_value is None:
            info = fetch_mint_info(rpc, mint)
            save_cluster(
                ctx.cluster_name,
                {"token_program": str(info.token_program), "payment_decimals": info.decimals},
            )
            return ProtocolAddresses(program_id, mint, info.token_program), info.decimals
        return ProtocolAddresses(program_id, mint, Pubkey.from_string(str(token_program_value))), int(decimals_value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _require_mint_cluster(ctx: CliContext, command: str) -> None:
    if ctx.cluster_name not in MINT_CREATION_CLUSTERS:
        allowed = ", ".join(sorted(MINT_CREATION_CLUSTERS))
        raise typer.BadParameter(
            f"`{command}` only works on {allowed}; '{ctx.cluster_name}' uses a real, fixed-supply payment mint"
        )


def _role(value: str) -> int:
    try:
        return NODE_ROLE[value]
    except KeyError as exc:
        raise typer.BadParameter(f"unknown role: {value}") from exc


def _job_class(value: str) -> int:
    try:
        return JOB_CLASS[value]
    except KeyError as exc:
        raise typer.BadParameter(f"unknown job class: {value}") from exc


def _tier(value: str) -> int:
    try:
        return STAKE_TIER[value.upper()]
    except KeyError as exc:
        raise typer.BadParameter(f"unknown stake tier: {value}") from exc


def _hash_or_known(value: str | None, known: dict[str, bytes], *, default: bytes | None = None) -> bytes:
    if value is None:
        if default is None:
            raise typer.BadParameter("missing hash value")
        return default
    if value in known:
        return known[value]
    try:
        return parse_hash(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _worker_address(program_id: Pubkey, value: str) -> Pubkey:
    try:
        wallet_pubkey = load_wallet(value).pubkey
        return worker_pda(program_id, wallet_pubkey)
    except FileNotFoundError:
        pubkey = Pubkey.from_string(value)
        return pubkey


def _require_job(rpc: RpcClient, job_key: Pubkey):
    job = fetch_job(rpc, job_key)
    if not job:
        raise typer.BadParameter(f"job not found: {job_key}")
    return job


def _find_marker_for_job(rpc: RpcClient, program_id: Pubkey, job_key: Pubkey) -> Pubkey:
    matches = [pubkey for pubkey, marker in fetch_all_markers(rpc, program_id) if marker.aggregate_job == job_key]
    if not matches:
        raise typer.BadParameter(f"no BonsolAggregateVerification marker found for job {job_key}")
    if len(matches) > 1:
        raise typer.BadParameter(f"multiple markers found for job {job_key}; inspect marker and settle with a narrower flow")
    return matches[0]


def main() -> None:
    try:
        app()
    except RpcError as exc:
        console.print_json(json.dumps({"error": {"code": exc.code, "message": str(exc), "payload": exc.payload}}, default=str))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
