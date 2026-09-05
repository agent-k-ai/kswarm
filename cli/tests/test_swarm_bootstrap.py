"""`swarm bootstrap` against a fake chain: convergence, idempotence, and refusals.

The fake interprets every instruction the bootstrap sends by program id and
discriminator (`initialize_protocol`, `register_worker`, `deposit_worker_stake`,
SPL `MintToChecked`, ATA creation) and keeps accounts in memory, so a rerun and a
validator reset are exercised without a validator.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from typer.testing import CliRunner

from kswarm_cli import main as cli_main
from kswarm_cli import swarm
from kswarm_cli import wallets as wallets_module
from kswarm_cli.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    CAPABILITY_CLASS,
    LAMPORTS_PER_SOL,
    KSWARM_PROGRAM_ID,
    MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER,
    NODE_ROLE,
    SOFTWARE_DIGEST,
    TOKEN_PROGRAM_ID,
)
from kswarm_cli.context import CliContext
from kswarm_cli.encoding import anchor_ix_discriminator
from kswarm_cli.protocol import ProtocolAddresses, ProtocolConfigAccount, WorkerAccount
from kswarm_cli.reducer_image import AGGREGATE_REDUCER_IMAGE_ID
from kswarm_cli.spl_token import MintInfo
from kswarm_cli.swarm import BootstrapContext, BootstrapError, BootstrapPlan, WorkerSpec, bootstrap


# The one program id, so a rotation cannot leave a test behind.
PROGRAM_ID = KSWARM_PROGRAM_ID
INITIALIZE = anchor_ix_discriminator("initialize_protocol")
REGISTER = anchor_ix_discriminator("register_worker")
DEPOSIT = anchor_ix_discriminator("deposit_worker_stake")
MINT_TO_CHECKED = 14
KAI = 10**6


class FakeRpc:
    def __init__(self) -> None:
        self.lamports: dict[str, int] = {}
        self.token_balances: dict[str, int] = {}
        self.accounts: set[str] = set()
        self.airdrops: list[tuple[str, int]] = []
        self.airdrop_lands_after_polls = 0
        self._pending: dict[str, tuple[int, int]] = {}

    def get_balance(self, pubkey: str) -> int:
        pending = self._pending.get(pubkey)
        if pending:
            lamports, polls_left = pending
            if polls_left <= 0:
                self.lamports[pubkey] = self.lamports.get(pubkey, 0) + lamports
                del self._pending[pubkey]
            else:
                self._pending[pubkey] = (lamports, polls_left - 1)
        return self.lamports.get(pubkey, 0)

    def request_airdrop(self, pubkey: str, lamports: int) -> str:
        self.airdrops.append((pubkey, lamports))
        self._pending[pubkey] = (lamports, self.airdrop_lands_after_polls)
        return f"airdrop{len(self.airdrops)}"

    def account_exists(self, pubkey: str) -> bool:
        return pubkey in self.accounts

    def get_token_account_balance(self, ata: str) -> dict[str, Any] | None:
        if ata not in self.token_balances:
            return None
        return {"amount": str(self.token_balances[ata])}


class FakeChain:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.config: ProtocolConfigAccount | None = None
        self.workers: dict[str, WorkerAccount] = {}
        self.mints: dict[str, MintInfo] = {}
        self.signatures: list[str] = []

    # --- patched module functions ---
    def fetch_config(self, rpc: Any, program_id: Pubkey) -> ProtocolConfigAccount | None:
        return self.config

    def fetch_worker(self, rpc: Any, address: Pubkey) -> WorkerAccount | None:
        return self.workers.get(str(address))

    def fetch_mint_info(self, rpc: Any, mint: Pubkey) -> MintInfo:
        return self.mints[str(mint)]

    def create_mint(self, rpc: Any, payer: Keypair, authority: Pubkey, decimals: int, token_program: Pubkey) -> tuple[Pubkey, str]:
        mint = Pubkey.new_unique()
        self.mints[str(mint)] = MintInfo(mint=mint, token_program=token_program, decimals=decimals)
        self.rpc.accounts.add(str(mint))
        return mint, self._sign()

    def sign_and_send(self, rpc: Any, payer: Keypair, instructions: list[Any], extra_signers: Any = None) -> str:
        for ix in instructions:
            data = bytes(ix.data)
            if ix.program_id == PROGRAM_ID:
                self._protocol_ix(ix, data, payer)
            elif ix.program_id == TOKEN_PROGRAM_ID:
                assert data[0] == MINT_TO_CHECKED, "only MintToChecked is expected"
                amount = struct.unpack_from("<Q", data, 1)[0]
                destination = str(ix.accounts[1].pubkey)
                self.rpc.accounts.add(destination)
                self.rpc.token_balances[destination] = self.rpc.token_balances.get(destination, 0) + amount
            elif ix.program_id == ASSOCIATED_TOKEN_PROGRAM_ID:
                self.rpc.accounts.add(str(ix.accounts[1].pubkey))
            else:
                raise AssertionError(f"unexpected program {ix.program_id}")
        return self._sign()

    def _protocol_ix(self, ix: Any, data: bytes, payer: Keypair) -> None:
        disc = data[:8]
        if disc == INITIALIZE:
            assert self.config is None, "initialize_protocol twice"
            floors = struct.unpack_from("<QQQQI", data, 8)
            mint = ix.accounts[2].pubkey
            self.config = ProtocolConfigAccount(
                bump=255,
                admin=ix.accounts[0].pubkey,
                payment_mint=mint,
                token_program=ix.accounts[3].pubkey,
                payment_decimals=self.mints[str(mint)].decimals,
                tier_one_stake_floor=floors[0],
                tier_two_stake_floor=floors[1],
                tier_three_stake_floor=floors[2],
                verifier_stake_floor=floors[3],
                min_challenge_window_seconds=floors[4],
            )
        elif disc == REGISTER:
            worker = str(ix.accounts[3].pubkey)
            assert worker not in self.workers, "register_worker twice"
            vault = ix.accounts[4].pubkey
            self.rpc.accounts.add(str(vault))
            self.rpc.token_balances.setdefault(str(vault), 0)
            self.workers[worker] = WorkerAccount(
                bump=254,
                authority=ix.accounts[0].pubkey,
                stake_vault=vault,
                locked_stake=0,
                active_claims=0,
                registered_at=1,
                status=1,
                role=data[8],
                capability_class_hash=data[9:41],
                software_digest=data[41:73],
            )
        elif disc == DEPOSIT:
            amount = struct.unpack_from("<Q", data, 8)[0]
            vault, source = str(ix.accounts[4].pubkey), str(ix.accounts[5].pubkey)
            assert self.rpc.token_balances.get(source, 0) >= amount, "deposit exceeds the wallet balance"
            self.rpc.token_balances[source] -= amount
            self.rpc.token_balances[vault] = self.rpc.token_balances.get(vault, 0) + amount
        else:
            raise AssertionError(f"unexpected protocol instruction {disc.hex()}")

    def _sign(self) -> str:
        self.signatures.append(f"sig{len(self.signatures) + 1}")
        return self.signatures[-1]


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeChain:
    rpc = FakeRpc()
    fake = FakeChain(rpc)
    monkeypatch.setattr(wallets_module, "WALLETS_DIR", tmp_path / "wallets")
    for name in ("fetch_config", "fetch_worker", "fetch_mint_info", "create_mint", "sign_and_send"):
        monkeypatch.setattr(swarm, name, getattr(fake, name))
    return fake


def _context(chain: FakeChain, cluster: str = "local", profile: dict[str, Any] | None = None) -> BootstrapContext:
    saved: dict[str, Any] = dict(profile or {})

    def save_cluster(name: str, payload: dict[str, Any]) -> None:
        assert name == cluster
        saved.update(payload)

    ctx = BootstrapContext(
        cluster_name=cluster,
        cluster_config=saved,
        program_id=PROGRAM_ID,
        rpc=chain.rpc,  # type: ignore[arg-type]
        save_cluster=save_cluster,
        environ={},
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    return ctx


def _plan(**overrides: Any) -> BootstrapPlan:
    values: dict[str, Any] = {
        "admin": "admin",
        "customer": "customer",
        "workers": (
            WorkerSpec("worker-a", "worker-proof", "worker-proof", "worker-canonical"),
            WorkerSpec("verifier", "verifier", "worker-proof", "worker-canonical"),
            WorkerSpec("aggregator", "worker-proof", "branch-aggregator-bonsol", AGGREGATE_REDUCER_IMAGE_ID),
        ),
        "create_wallets": True,
        "airdrop_sol": 20.0,
        "create_mint": True,
        "payment_mint": None,
        "fund_kai": "300000",
        "tier_floors": ("50000", "250000", "1000000"),
        "verifier_floor": "100000",
        "min_challenge_window_seconds": 5,
    }
    values.update(overrides)
    return BootstrapPlan(**values)


def _proto(chain: FakeChain) -> ProtocolAddresses:
    assert chain.config is not None
    return chain.config.addresses(PROGRAM_ID)


def _wallet(name: str) -> Pubkey:
    return wallets_module.load_wallet(name).pubkey


def test_fresh_cluster_converges_to_ready(chain: FakeChain) -> None:
    ctx = _context(chain)
    summary = bootstrap(ctx, _plan())

    # 1. wallets exist and hold the target SOL
    assert sorted(summary["wallets"]) == ["admin", "aggregator", "customer", "verifier", "worker-a"]
    assert all(entry["created"] for entry in summary["wallets"].values())
    assert all(entry["airdropped_lamports"] == 20 * LAMPORTS_PER_SOL for entry in summary["wallets"].values())
    assert len(chain.rpc.airdrops) == 5

    # 2. a stand-in mint was created and recorded in the profile
    assert summary["payment_mint"]["source"] == "created"
    mint = summary["payment_mint"]["mint"]
    assert ctx.cluster_config["payment_mint"] == mint
    assert ctx.cluster_config["mint_authority_wallet"] == "admin"
    assert ctx.cluster_config["payment_decimals"] == 6
    assert ctx.cluster_config["token_program"] == str(TOKEN_PROGRAM_ID)

    # 3. the protocol is initialized with the KAI floors in 6-decimal base units
    assert summary["protocol"]["status"] == "initialized"
    assert chain.config is not None
    assert chain.config.admin == _wallet("admin")
    assert chain.config.tier_one_stake_floor == 50_000 * KAI
    assert chain.config.tier_two_stake_floor == 250_000 * KAI
    assert chain.config.tier_three_stake_floor == 1_000_000 * KAI
    assert chain.config.verifier_stake_floor == 100_000 * KAI
    assert chain.config.min_challenge_window_seconds == 5
    assert ctx.cluster_config["admin_wallet"] == "admin"

    # 4. every non-admin wallet was funded to the target
    assert sorted(summary["funding"]) == ["aggregator", "customer", "verifier", "worker-a"]
    assert summary["funding"]["customer"] == {"holdings": "300000", "minted": "300000", "signature": summary["funding"]["customer"]["signature"]}
    proto = _proto(chain)
    assert chain.rpc.token_balances[str(proto.ata(_wallet("customer")))] == 300_000 * KAI

    # 5. workers are registered with the documented roles and staked at their floors
    workers = summary["workers"]
    for name, role, capability, digest in [
        ("worker-a", "worker-proof", "worker-proof", SOFTWARE_DIGEST["worker-canonical"]),
        ("verifier", "verifier", "worker-proof", SOFTWARE_DIGEST["worker-canonical"]),
        ("aggregator", "worker-proof", "branch-aggregator-bonsol", bytes.fromhex(AGGREGATE_REDUCER_IMAGE_ID)),
    ]:
        account = chain.workers[str(proto.worker_pda(_wallet(name)))]
        assert account.role == NODE_ROLE[role]
        assert account.capability_class_hash == CAPABILITY_CLASS[capability]
        assert account.software_digest == digest
        assert workers[name]["registered"].startswith("sig")
    assert workers["worker-a"]["stake"] == "50000"
    assert workers["verifier"]["stake"] == "100000"
    assert workers["aggregator"]["stake"] == "50000"
    assert chain.rpc.token_balances[str(proto.ata(proto.worker_pda(_wallet("verifier"))))] == 100_000 * KAI
    assert chain.rpc.token_balances[str(proto.ata(_wallet("verifier")))] == 200_000 * KAI


def test_rerun_sends_nothing(chain: FakeChain) -> None:
    ctx = _context(chain)
    bootstrap(ctx, _plan())
    sent = len(chain.signatures)
    airdrops = len(chain.rpc.airdrops)

    summary = bootstrap(ctx, _plan())

    assert len(chain.signatures) == sent
    assert len(chain.rpc.airdrops) == airdrops
    assert summary["payment_mint"]["source"] == "protocol-config"
    assert summary["protocol"]["status"] == "already-initialized"
    assert all(not entry["created"] and entry["airdropped_lamports"] == 0 for entry in summary["wallets"].values())
    assert all(entry["minted"] == "0" for entry in summary["funding"].values())
    assert all(entry["registered"] == "already" and entry["staked"] == "already" for entry in summary["workers"].values())


def test_customer_spend_is_topped_up_but_worker_stake_is_not(chain: FakeChain) -> None:
    ctx = _context(chain)
    bootstrap(ctx, _plan())
    proto = _proto(chain)
    customer_ata = str(proto.ata(_wallet("customer")))
    chain.rpc.token_balances[customer_ata] -= 12 * KAI  # escrow for a run

    summary = bootstrap(ctx, _plan())
    assert summary["funding"]["customer"]["minted"] == "12"
    assert chain.rpc.token_balances[customer_ata] == 300_000 * KAI
    # worker-a holds 250,000 in its wallet and 50,000 in its vault: 300,000 in total.
    assert summary["funding"]["worker-a"] == {"holdings": "300000", "minted": "0"}


def test_stake_top_up_and_explicit_targets(chain: FakeChain) -> None:
    ctx = _context(chain)
    bootstrap(ctx, _plan())
    plan = _plan(
        workers=(
            WorkerSpec("worker-a", "worker-proof", "worker-proof", "worker-canonical", stake="250000"),
            WorkerSpec("verifier", "verifier", "worker-proof", "worker-canonical"),
            WorkerSpec("aggregator", "worker-proof", "branch-aggregator-bonsol", AGGREGATE_REDUCER_IMAGE_ID),
        )
    )
    summary = bootstrap(ctx, plan)
    assert summary["workers"]["worker-a"]["stake_deposited"] == "200000"
    assert summary["workers"]["worker-a"]["stake"] == "250000"
    proto = _proto(chain)
    assert chain.rpc.token_balances[str(proto.ata(proto.worker_pda(_wallet("worker-a"))))] == 250_000 * KAI


def test_validator_reset_recreates_the_mint_and_reinitializes(chain: FakeChain) -> None:
    ctx = _context(chain)
    bootstrap(ctx, _plan())
    old_mint = ctx.cluster_config["payment_mint"]

    # Everything on chain is gone; wallets and the cluster profile survive in the volume.
    chain.config = None
    chain.workers.clear()
    chain.mints.clear()
    chain.rpc.accounts.clear()
    chain.rpc.token_balances.clear()
    chain.rpc.lamports.clear()

    summary = bootstrap(ctx, _plan())
    assert summary["payment_mint"]["source"] == "created"
    assert ctx.cluster_config["payment_mint"] != old_mint
    assert summary["protocol"]["status"] == "initialized"
    assert all(entry["airdropped_lamports"] == 20 * LAMPORTS_PER_SOL for entry in summary["wallets"].values())
    assert all(entry["registered"].startswith("sig") for entry in summary["workers"].values())


def test_profile_mint_is_reused_when_it_still_exists(chain: FakeChain) -> None:
    ctx = _context(chain)
    bootstrap(ctx, _plan())
    mint = ctx.cluster_config["payment_mint"]
    chain.config = None  # protocol gone, mint account still there
    chain.workers.clear()

    summary = bootstrap(ctx, _plan())
    assert summary["payment_mint"] == {"mint": mint, "source": "cluster-profile"}
    assert summary["protocol"]["status"] == "initialized"
    assert chain.config is not None and str(chain.config.payment_mint) == mint


def test_external_mint_is_used_and_funding_is_refused(chain: FakeChain) -> None:
    ctx = _context(chain)
    external = Pubkey.new_unique()
    chain.mints[str(external)] = MintInfo(mint=external, token_program=TOKEN_PROGRAM_ID, decimals=6)
    chain.rpc.accounts.add(str(external))

    with pytest.raises(BootstrapError, match="--fund-kai needs a stand-in mint"):
        bootstrap(ctx, _plan(payment_mint=str(external)))

    # The operator funds the wallets from the external mint before registering and staking.
    chain.config = None
    airdrops_before = len(chain.rpc.airdrops)
    for name in ("worker-a", "verifier", "aggregator"):
        wallet_pubkey = wallets_module.load_wallet(name).pubkey
        ata = str(ProtocolAddresses(PROGRAM_ID, external, TOKEN_PROGRAM_ID).ata(wallet_pubkey))
        chain.rpc.accounts.add(ata)
        chain.rpc.token_balances[ata] = 100_000 * KAI
    summary = bootstrap(ctx, _plan(payment_mint=str(external), fund_kai=None, airdrop_sol=0.0, create_mint=False))
    assert summary["payment_mint"] == {"mint": str(external), "source": "external"}
    assert summary["workers"]["verifier"]["stake"] == "100000"
    assert "funding" not in summary
    assert len(chain.rpc.airdrops) == airdrops_before
    assert "airdropped_lamports" not in summary["wallets"]["admin"]


def test_refusals(chain: FakeChain) -> None:
    with pytest.raises(BootstrapError, match="only runs on devnet, local"):
        bootstrap(_context(chain, cluster="mainnet"), _plan())

    with pytest.raises(BootstrapError, match="--no-create-wallets"):
        bootstrap(_context(chain), _plan(create_wallets=False))

    with pytest.raises(BootstrapError, match="no payment mint"):
        bootstrap(_context(chain), _plan(create_mint=False))

    missing = Pubkey.new_unique()
    with pytest.raises(BootstrapError, match="does not exist on local"):
        bootstrap(_context(chain), _plan(payment_mint=str(missing)))

    ctx = _context(chain)
    bootstrap(ctx, _plan())
    other = Pubkey.new_unique()
    chain.mints[str(other)] = MintInfo(mint=other, token_program=TOKEN_PROGRAM_ID, decimals=6)
    chain.rpc.accounts.add(str(other))
    with pytest.raises(BootstrapError, match="initialized with mint"):
        bootstrap(ctx, _plan(payment_mint=str(other)))

    changed = _plan(workers=(WorkerSpec("worker-a", "verifier", "worker-proof", "worker-canonical"),))
    with pytest.raises(BootstrapError, match="already registered with different parameters: role"):
        bootstrap(ctx, changed)

    # Identifiers up to 32 bytes are padded like the CLI does; longer non-hex values are rejected.
    bad_digest = _plan(workers=(WorkerSpec("worker-b", "worker-proof", "worker-proof", "x" * 40),))
    with pytest.raises(BootstrapError, match="software digest for worker-b is neither a known name"):
        bootstrap(ctx, bad_digest)


def test_airdrop_waits_for_the_balance_and_times_out(chain: FakeChain) -> None:
    chain.rpc.airdrop_lands_after_polls = 3
    ctx = _context(chain)
    polls: list[float] = []
    ctx.sleep = polls.append
    summary = bootstrap(ctx, _plan(workers=(), fund_kai=None))
    assert summary["wallets"]["admin"]["airdropped_lamports"] == 20 * LAMPORTS_PER_SOL
    assert len(polls) >= 3

    chain.rpc.airdrop_lands_after_polls = 10**6
    ticks = iter(range(0, 10_000, 30))
    ctx.clock = lambda: float(next(ticks))
    with pytest.raises(BootstrapError, match="did not land"):
        bootstrap(ctx, _plan(admin="late", customer="late", workers=(), fund_kai=None))


def test_command_builds_the_documented_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    context = CliContext(
        cluster_name="local",
        rpc_url="http://fake-rpc",
        commitment="confirmed",
        keypair_path=None,
        json_output=True,
        console=Console(),
        cluster_config={"name": "local", "rpc_url": "http://fake-rpc", "program_id": str(PROGRAM_ID)},
    )
    monkeypatch.setattr(cli_main.CliContext, "load", classmethod(lambda cls, **kwargs: context))
    monkeypatch.setattr(cli_main, "_rpc", lambda ctx: object())
    captured: dict[str, Any] = {}

    def fake_bootstrap(ctx: BootstrapContext, plan: BootstrapPlan) -> dict[str, Any]:
        captured["ctx"] = ctx
        captured["plan"] = plan
        return {"cluster": ctx.cluster_name}

    monkeypatch.setattr(cli_main, "run_swarm_bootstrap", fake_bootstrap)
    result = CliRunner().invoke(
        cli_main.app,
        ["--json", "swarm", "bootstrap", "--branch-worker", "worker-a", "--branch-worker", "worker-b", "--worker-stake", "250000"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"cluster": "local"}
    plan: BootstrapPlan = captured["plan"]
    assert plan.admin == "admin" and plan.customer == "customer"
    assert plan.airdrop_sol == 20.0 and plan.create_mint and plan.create_wallets
    assert plan.fund_kai == "300000"
    assert plan.tier_floors == ("50000", "250000", "1000000") and plan.verifier_floor == "100000"
    # The challenge-window floor defaults from the cluster profile, not from a constant.
    assert plan.min_challenge_window_seconds == MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER["local"]
    assert [spec.wallet for spec in plan.workers] == ["worker-a", "worker-b", "verifier", "aggregator"]
    assert plan.workers[0] == WorkerSpec("worker-a", "worker-proof", "worker-proof", "worker-canonical", "250000")
    assert plan.workers[2] == WorkerSpec("verifier", "verifier", "worker-proof", "worker-canonical", None)
    assert plan.workers[3] == WorkerSpec("aggregator", "worker-proof", "branch-aggregator-bonsol", AGGREGATE_REDUCER_IMAGE_ID, None)
    assert captured["ctx"].program_id == PROGRAM_ID

    devnet = CliRunner().invoke(
        cli_main.app,
        ["--json", "swarm", "bootstrap", "--verifier", "", "--aggregator", "", "--fund-kai", "", "--airdrop-sol", "0"],
    )
    assert devnet.exit_code == 0, devnet.output
    plan = captured["plan"]
    assert [spec.wallet for spec in plan.workers] == ["worker-a"]
    assert plan.fund_kai is None and plan.airdrop_sol == 0.0

    overridden = CliRunner().invoke(
        cli_main.app,
        ["--json", "swarm", "bootstrap", "--min-challenge-window", "900"],
    )
    assert overridden.exit_code == 0, overridden.output
    assert captured["plan"].min_challenge_window_seconds == 900

    below_one = CliRunner().invoke(
        cli_main.app,
        ["--json", "swarm", "bootstrap", "--min-challenge-window", "0"],
    )
    assert below_one.exit_code != 0

    refused = CliRunner().invoke(cli_main.app, ["--json", "swarm", "bootstrap", "--aggregate-image-id", "zz"])
    assert refused.exit_code != 0
    assert "image id is not hex" in refused.output
