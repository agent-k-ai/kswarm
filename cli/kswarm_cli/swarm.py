"""`swarm bootstrap`: one idempotent command that brings a local or devnet swarm to "ready".

The containerized stack (`docker-compose.swarm.yml`) runs this once before the
workers start. Every step checks chain state first, so a rerun after a validator
reset, a partial failure, or a `docker compose up` on an already-bootstrapped
cluster converges to the same end state:

1. named wallets exist (created when missing) and hold at least the target SOL
2. the payment mint exists: an external mint, the initialized protocol's mint,
   the profile's stand-in mint if it still exists on chain, or a new stand-in
3. the protocol config is initialized with the requested floors
4. every non-admin wallet holds at least the target KAI (stand-in mints only)
5. every worker is registered with its role, capability, and software digest,
   and its vault holds at least its target stake

Mainnet is refused: registration and staking with real KAI are deliberate manual
`worker register` / `worker stake` steps.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from kswarm_cli.constants import (
    CAPABILITY_CLASS,
    LAMPORTS_PER_SOL,
    LOCAL_MINT_DECIMALS,
    MINT_CREATION_CLUSTERS,
    NODE_ROLE,
    SOFTWARE_DIGEST,
    TOKEN_PROGRAM_ID,
)
from kswarm_cli.encoding import format_token_amount, parse_base_units, parse_hash, parse_token_amount
from kswarm_cli.protocol import (
    ProtocolAddresses,
    ProtocolConfigAccount,
    deposit_worker_stake_ix,
    fetch_config,
    fetch_worker,
    initialize_protocol_ix,
    register_worker_ix,
    stake_floors_from_human,
)
from kswarm_cli.rpc import RpcClient, sign_and_send
from kswarm_cli.spl_token import create_mint, ensure_ata_ix_if_missing, fetch_mint_info, mint_to_checked_ix
from kswarm_cli.wallets import NamedWallet, create_wallet, load_wallet, wallet_path


AIRDROP_TIMEOUT_SECONDS = 60.0
AIRDROP_POLL_SECONDS = 0.5
VERIFIER_ROLE = "verifier"


class BootstrapError(ValueError):
    """A condition the operator must fix; the message says which."""


@dataclass(frozen=True)
class WorkerSpec:
    """One worker wallet to register and stake."""

    wallet: str
    role: str
    capability: str
    software_digest: str
    stake: str | None = None  # human KAI; None means the role's floor

    def role_value(self) -> int:
        try:
            return NODE_ROLE[self.role]
        except KeyError as exc:
            raise BootstrapError(f"unknown role for {self.wallet}: {self.role}") from exc

    def capability_hash(self) -> bytes:
        return _known_or_hash(self.capability, CAPABILITY_CLASS, f"capability for {self.wallet}")

    def software_digest_hash(self) -> bytes:
        return _known_or_hash(self.software_digest, SOFTWARE_DIGEST, f"software digest for {self.wallet}")


@dataclass(frozen=True)
class BootstrapPlan:
    admin: str
    customer: str
    workers: tuple[WorkerSpec, ...]
    create_wallets: bool
    airdrop_sol: float
    create_mint: bool
    payment_mint: str | None
    fund_kai: str | None
    tier_floors: tuple[str, str, str]
    verifier_floor: str
    min_challenge_window_seconds: int

    def wallet_names(self) -> list[str]:
        names = [self.admin, self.customer, *(worker.wallet for worker in self.workers)]
        unique: list[str] = []
        for name in names:
            if name not in unique:
                unique.append(name)
        return unique


@dataclass
class BootstrapContext:
    """Everything the steps need from the CLI invocation, without importing main."""

    cluster_name: str
    cluster_config: dict[str, Any]
    program_id: Pubkey
    rpc: RpcClient
    save_cluster: Callable[[str, dict[str, Any]], None]
    environ: Mapping[str, str]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def bootstrap(ctx: BootstrapContext, plan: BootstrapPlan) -> dict[str, Any]:
    """Run every step and return a summary of what was found and what was done."""

    if ctx.cluster_name not in MINT_CREATION_CLUSTERS:
        allowed = ", ".join(sorted(MINT_CREATION_CLUSTERS))
        raise BootstrapError(
            f"swarm bootstrap only runs on {allowed}; '{ctx.cluster_name}' holds real funds, "
            "register and stake there with explicit `worker register` and `worker stake` commands"
        )
    summary: dict[str, Any] = {"cluster": ctx.cluster_name, "program_id": str(ctx.program_id)}
    wallets = _ensure_wallets(ctx, plan, summary)
    admin = wallets[plan.admin]
    mint, mint_authority = _ensure_payment_mint(ctx, plan, admin, summary)
    config = _ensure_protocol(ctx, plan, admin, mint, summary)
    proto = config.addresses(ctx.program_id)
    if plan.fund_kai is not None:
        _fund_wallets(ctx, plan, proto, config.payment_decimals, wallets, mint_authority, summary)
    _ensure_workers(ctx, plan, proto, config, wallets, summary)
    return summary


# --- steps ---------------------------------------------------------------------


def _ensure_wallets(ctx: BootstrapContext, plan: BootstrapPlan, summary: dict[str, Any]) -> dict[str, NamedWallet]:
    wallets: dict[str, NamedWallet] = {}
    report: dict[str, Any] = {}
    target_lamports = int(plan.airdrop_sol * LAMPORTS_PER_SOL)
    for name in plan.wallet_names():
        created = False
        if wallet_path(name).exists():
            wallet = load_wallet(name)
        elif plan.create_wallets:
            wallet = create_wallet(name)
            created = True
        else:
            raise BootstrapError(f"wallet does not exist and --no-create-wallets was given: {name}")
        wallets[name] = wallet
        entry: dict[str, Any] = {"pubkey": str(wallet.pubkey), "created": created}
        if target_lamports > 0:
            entry["airdropped_lamports"] = _ensure_lamports(ctx, wallet.pubkey, target_lamports)
        report[name] = entry
    summary["wallets"] = report
    return wallets


def _ensure_lamports(ctx: BootstrapContext, pubkey: Pubkey, target: int) -> int:
    """Airdrop up to `target` lamports and wait for the balance to land."""

    balance = ctx.rpc.get_balance(str(pubkey))
    if balance >= target:
        return 0
    needed = target - balance
    ctx.rpc.request_airdrop(str(pubkey), needed)
    deadline = ctx.clock() + AIRDROP_TIMEOUT_SECONDS
    while ctx.rpc.get_balance(str(pubkey)) < target:
        if ctx.clock() >= deadline:
            raise BootstrapError(f"airdrop to {pubkey} did not land within {AIRDROP_TIMEOUT_SECONDS:.0f}s")
        ctx.sleep(AIRDROP_POLL_SECONDS)
    return needed


def _ensure_payment_mint(
    ctx: BootstrapContext, plan: BootstrapPlan, admin: NamedWallet, summary: dict[str, Any]
) -> tuple[Pubkey, Keypair | None]:
    """Return the mint and, when this bootstrap controls it, the mint authority."""

    existing = fetch_config(ctx.rpc, ctx.program_id)
    if plan.payment_mint:
        mint = Pubkey.from_string(plan.payment_mint)
        if existing and existing.payment_mint != mint:
            raise BootstrapError(f"protocol is initialized with mint {existing.payment_mint}, not {mint}")
        if not ctx.rpc.account_exists(str(mint)):
            raise BootstrapError(f"payment mint {mint} does not exist on {ctx.cluster_name}")
        summary["payment_mint"] = {"mint": str(mint), "source": "external"}
        return mint, _mint_authority_if_local(ctx, plan, mint)
    if existing:
        summary["payment_mint"] = {"mint": str(existing.payment_mint), "source": "protocol-config"}
        return existing.payment_mint, _mint_authority_if_local(ctx, plan, existing.payment_mint)
    profile_mint = ctx.cluster_config.get("payment_mint")
    if profile_mint and ctx.rpc.account_exists(str(profile_mint)):
        mint = Pubkey.from_string(str(profile_mint))
        summary["payment_mint"] = {"mint": str(mint), "source": "cluster-profile"}
        return mint, _mint_authority_if_local(ctx, plan, mint)
    if not plan.create_mint:
        raise BootstrapError("no payment mint: pass --payment-mint or allow --create-mint")
    mint, signature = create_mint(ctx.rpc, admin.keypair, admin.pubkey, LOCAL_MINT_DECIMALS, TOKEN_PROGRAM_ID)
    ctx.save_cluster(
        ctx.cluster_name,
        {
            "payment_mint": str(mint),
            "payment_decimals": LOCAL_MINT_DECIMALS,
            "token_program": str(TOKEN_PROGRAM_ID),
            "mint_authority_wallet": plan.admin,
        },
    )
    summary["payment_mint"] = {"mint": str(mint), "source": "created", "signature": signature}
    return mint, admin.keypair


def _mint_authority_if_local(ctx: BootstrapContext, plan: BootstrapPlan, mint: Pubkey) -> Keypair | None:
    """The mint authority keypair when the profile records a local wallet that holds it."""

    authority_wallet = ctx.cluster_config.get("mint_authority_wallet")
    if not authority_wallet or str(ctx.cluster_config.get("payment_mint")) != str(mint):
        return None
    if not wallet_path(str(authority_wallet)).exists():
        return None
    return load_wallet(str(authority_wallet)).keypair


def _ensure_protocol(
    ctx: BootstrapContext, plan: BootstrapPlan, admin: NamedWallet, mint: Pubkey, summary: dict[str, Any]
) -> ProtocolConfigAccount:
    mint_info = fetch_mint_info(ctx.rpc, mint)
    profile = {
        "payment_mint": str(mint),
        "payment_decimals": mint_info.decimals,
        "token_program": str(mint_info.token_program),
    }
    existing = fetch_config(ctx.rpc, ctx.program_id)
    if existing:
        if existing.payment_mint != mint:
            raise BootstrapError(f"protocol is initialized with mint {existing.payment_mint}, not {mint}")
        ctx.save_cluster(ctx.cluster_name, profile)
        summary["protocol"] = {"status": "already-initialized", **existing.to_json()}
        return existing
    floors = stake_floors_from_human(
        plan.tier_floors,
        plan.verifier_floor,
        mint_info.decimals,
        plan.min_challenge_window_seconds,
    )
    proto = ProtocolAddresses(ctx.program_id, mint, mint_info.token_program)
    signature = sign_and_send(ctx.rpc, admin.keypair, [initialize_protocol_ix(proto, admin.pubkey, floors)])
    ctx.save_cluster(ctx.cluster_name, {**profile, "admin_wallet": plan.admin})
    config = fetch_config(ctx.rpc, ctx.program_id)
    if config is None:
        raise BootstrapError("protocol config is still missing after initialize_protocol confirmed")
    summary["protocol"] = {"status": "initialized", "signature": signature, **config.to_json()}
    return config


def _fund_wallets(
    ctx: BootstrapContext,
    plan: BootstrapPlan,
    proto: ProtocolAddresses,
    decimals: int,
    wallets: dict[str, NamedWallet],
    mint_authority: Keypair | None,
    summary: dict[str, Any],
) -> None:
    if mint_authority is None:
        raise BootstrapError(
            "--fund-kai needs a stand-in mint whose authority is a local wallet; "
            "fund the wallets yourself on a cluster with an external mint"
        )
    target = parse_base_units(plan.fund_kai, decimals)
    worker_wallets = {spec.wallet for spec in plan.workers}
    report: dict[str, Any] = {}
    for name in plan.wallet_names():
        if name == plan.admin:
            continue
        wallet = wallets[name]
        # Holdings count the worker's own stake vault, so a staked worker is not
        # funded again. A customer who spent KAI on jobs is topped up: stand-in
        # tokens are free and the next run needs escrow.
        current = _token_balance(ctx.rpc, proto.ata(wallet.pubkey))
        if name in worker_wallets:
            current += _token_balance(ctx.rpc, proto.ata(proto.worker_pda(wallet.pubkey)))
        if current >= target:
            report[name] = {"holdings": format_token_amount(current, decimals), "minted": "0"}
            continue
        amount = target - current
        ata, create_ata = ensure_ata_ix_if_missing(
            ctx.rpc, mint_authority.pubkey(), proto.payment_mint, wallet.pubkey, proto.token_program
        )
        mint_ix = mint_to_checked_ix(proto.payment_mint, ata, mint_authority.pubkey(), amount, decimals, proto.token_program)
        signature = sign_and_send(ctx.rpc, mint_authority, [ix for ix in (create_ata, mint_ix) if ix])
        report[name] = {
            "holdings": format_token_amount(target, decimals),
            "minted": format_token_amount(amount, decimals),
            "signature": signature,
        }
    summary["funding"] = report


def _ensure_workers(
    ctx: BootstrapContext,
    plan: BootstrapPlan,
    proto: ProtocolAddresses,
    config: ProtocolConfigAccount,
    wallets: dict[str, NamedWallet],
    summary: dict[str, Any],
) -> None:
    report: dict[str, Any] = {}
    for spec in plan.workers:
        wallet = wallets[spec.wallet]
        worker = proto.worker_pda(wallet.pubkey)
        entry: dict[str, Any] = {"worker": str(worker), "authority": str(wallet.pubkey)}
        account = fetch_worker(ctx.rpc, worker)
        if account is None:
            ix = register_worker_ix(proto, wallet.pubkey, spec.role_value(), spec.capability_hash(), spec.software_digest_hash())
            entry["registered"] = sign_and_send(ctx.rpc, wallet.keypair, [ix])
        else:
            _check_registration(spec, account, entry)
            entry["registered"] = "already"
        target = _target_stake(spec, config)
        current = _token_balance(ctx.rpc, proto.ata(worker))
        if current < target:
            amount = target - current
            entry["staked"] = sign_and_send(ctx.rpc, wallet.keypair, [deposit_worker_stake_ix(proto, wallet.pubkey, amount)])
            entry["stake_deposited"] = format_token_amount(amount, config.payment_decimals)
        else:
            entry["staked"] = "already"
            entry["stake_deposited"] = "0"
        entry["stake"] = format_token_amount(max(current, target), config.payment_decimals)
        report[spec.wallet] = entry
    summary["workers"] = report


def _check_registration(spec: WorkerSpec, account: Any, entry: dict[str, Any]) -> None:
    """A wallet registered with other parameters cannot be re-registered; say so instead of staking blindly."""

    mismatches = []
    if account.role != spec.role_value():
        mismatches.append(f"role {account.role} != {spec.role}")
    if account.capability_class_hash != spec.capability_hash():
        mismatches.append("capability hash differs")
    if account.software_digest != spec.software_digest_hash():
        mismatches.append("software digest differs")
    if mismatches:
        raise BootstrapError(f"{spec.wallet} is already registered with different parameters: {'; '.join(mismatches)}")
    entry["role"] = spec.role


def _target_stake(spec: WorkerSpec, config: ProtocolConfigAccount) -> int:
    if spec.stake is not None:
        return parse_token_amount(spec.stake, config.payment_decimals)
    if spec.role == VERIFIER_ROLE:
        return config.verifier_stake_floor
    return config.tier_one_stake_floor


# --- helpers -------------------------------------------------------------------


def _token_balance(rpc: RpcClient, ata: Pubkey) -> int:
    balance = rpc.get_token_account_balance(str(ata))
    return int(balance["amount"]) if balance else 0


def _known_or_hash(value: str, known: Mapping[str, bytes], what: str) -> bytes:
    if value in known:
        return known[value]
    try:
        return parse_hash(value)
    except ValueError as exc:
        raise BootstrapError(f"{what} is neither a known name ({', '.join(sorted(known))}) nor 32-byte hex: {value}") from exc

