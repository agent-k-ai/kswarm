from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from solders.keypair import Keypair

from kswarm_cli.constants import KSWARM_PROGRAM_ID, MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAM_SO = REPO_ROOT / "solana" / "target" / "deploy" / "kswarm_protocol.so"
PROGRAM_ID = str(KSWARM_PROGRAM_ID)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def admin_keypair_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The program upgrade authority. `initialize_protocol` accepts only this signer."""
    path = tmp_path_factory.mktemp("upgrade-authority") / "admin.json"
    path.write_text(Keypair().to_json(), encoding="utf-8")
    # The CLI refuses a group- or world-readable key file (`InsecureKeyFileError`),
    # and `shutil.copyfile` preserves the mode, so seed it the way an operator must.
    path.chmod(0o600)
    return path


def seed_admin_wallet(cli_home: Path, admin_keypair_path: Path) -> None:
    """Installs the upgrade-authority keypair as the CLI wallet named `admin`."""
    wallets = cli_home / ".config" / "kswarm" / "wallets"
    wallets.mkdir(parents=True, exist_ok=True)
    wallets.chmod(0o700)
    destination = wallets / "admin.json"
    shutil.copyfile(admin_keypair_path, destination)
    destination.chmod(0o600)


@pytest.fixture(scope="session")
def validator_rpc(tmp_path_factory: pytest.TempPathFactory, admin_keypair_path: Path) -> str:
    if not PROGRAM_SO.exists():
        pytest.skip(f"program artifact missing: {PROGRAM_SO}")
    upgrade_authority = str(Keypair.from_json(admin_keypair_path.read_text(encoding="utf-8")).pubkey())

    rpc_port = _free_port()
    faucet_port = _free_port()
    gossip_port = _free_port()
    dynamic_start = _free_port()
    ledger = tmp_path_factory.mktemp("validator-ledger")
    command = [
        "solana-test-validator",
        "--reset",
        "--quiet",
        "--ledger",
        str(ledger),
        "--rpc-port",
        str(rpc_port),
        "--faucet-port",
        str(faucet_port),
        "--gossip-port",
        str(gossip_port),
        "--dynamic-port-range",
        f"{dynamic_start}-{dynamic_start + 40}",
        # Deployed as an upgradeable program, as `solana program deploy` does; the
        # admin wallet is its upgrade authority, which `initialize_protocol` requires.
        "--upgradeable-program",
        PROGRAM_ID,
        str(PROGRAM_SO),
        upgrade_authority,
    ]
    process = subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    rpc_url = f"http://127.0.0.1:{rpc_port}"
    try:
        deadline = time.time() + 45
        healthy = False
        while time.time() < deadline:
            try:
                response = httpx.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []},
                    timeout=1,
                )
                healthy = response.status_code == 200 and response.json().get("result") == "ok"
                if healthy:
                    break
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(0.5)
        if not healthy:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"validator did not become healthy\n{output}")
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"validator exited early\n{output}")
        yield rpc_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture(scope="module")
def cli_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One CLI home per module: the validator (and its single protocol config) is shared too."""

    return tmp_path_factory.mktemp("cli-home") / "home"


def run_cli_raw(cli_home: Path, rpc_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(cli_home)
    command = [sys.executable, "-m", "kswarm_cli.main", "--json", "--rpc-url", rpc_url, *args]
    return subprocess.run(command, cwd=REPO_ROOT / "cli", env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_cli(cli_home: Path, rpc_url: str, *args: str) -> dict:
    result = run_cli_raw(cli_home, rpc_url, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def run_cli_expect_failure(cli_home: Path, rpc_url: str, *args: str) -> str:
    result = run_cli_raw(cli_home, rpc_url, *args)
    assert result.returncode != 0, result.stdout + result.stderr
    return result.stdout + result.stderr


@pytest.fixture(scope="session")
def ipfs_api_url() -> str:
    url = os.environ.get("KSWARM_IPFS_API_URL") or os.environ.get("PROTOCOL_IPFS_API_URL")
    if not url:
        pytest.skip("set KSWARM_IPFS_API_URL to a Kubo API for the predict lifecycle test")
    try:
        response = httpx.post(f"{url.rstrip('/')}/api/v0/version", timeout=5)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"IPFS_UNREACHABLE: {url}: {exc}")
    return url.rstrip("/")


TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# The local cluster profile's challenge-window floor: small so this suite, the smoke
# script and the demos still run a job to its challenge deadline in seconds.
LOCAL_MIN_CHALLENGE_WINDOW = MIN_CHALLENGE_WINDOW_SECONDS_BY_CLUSTER["local"]
EXPECTED_FLOORS = {
    "tier_one_stake_floor": 50_000_000_000,
    "tier_two_stake_floor": 250_000_000_000,
    "tier_three_stake_floor": 1_000_000_000_000,
    "verifier_stake_floor": 100_000_000_000,
    "min_challenge_window_seconds": LOCAL_MIN_CHALLENGE_WINDOW,
}


@pytest.mark.integration
def test_classic_spl_mint_job_lifecycle(cli_home: Path, validator_rpc: str, admin_keypair_path: Path) -> None:
    seed_admin_wallet(cli_home, admin_keypair_path)
    admin = run_cli(cli_home, validator_rpc, "wallet", "create", "admin", "--airdrop", "20")
    customer = run_cli(cli_home, validator_rpc, "wallet", "create", "customer", "--airdrop", "20")
    worker = run_cli(cli_home, validator_rpc, "wallet", "create", "worker-a", "--airdrop", "20")

    assert admin["pubkey"]
    assert customer["pubkey"]
    assert worker["pubkey"]

    # Default stand-in mint: classic SPL Token, 6 decimals, the KAI layout.
    mint = run_cli(cli_home, validator_rpc, "token", "create-mint", "--authority", "admin")
    assert mint["decimals"] == 6
    assert mint["token_program"] == TOKEN_PROGRAM_ID

    # Only the program upgrade authority may initialize; `customer` is not it.
    rejected = run_cli_expect_failure(
        cli_home, validator_rpc, "protocol", "initialize", "--admin", "customer", "--payment-mint", mint["mint"]
    )
    assert "AdminNotUpgradeAuthority" in rejected

    initialized = run_cli(cli_home, validator_rpc, "protocol", "initialize", "--admin", "admin", "--payment-mint", mint["mint"])
    assert initialized["payment_decimals"] == 6
    assert initialized["token_program"] == TOKEN_PROGRAM_ID
    assert initialized["stake_floors"] == EXPECTED_FLOORS

    shown = run_cli(cli_home, validator_rpc, "protocol", "show")["account"]
    assert shown["payment_mint"] == mint["mint"]
    assert shown["token_program"] == TOKEN_PROGRAM_ID
    assert shown["payment_decimals"] == 6
    assert {key: shown[key] for key in EXPECTED_FLOORS} == EXPECTED_FLOORS

    again = run_cli(cli_home, validator_rpc, "protocol", "initialize", "--admin", "admin", "--payment-mint", mint["mint"])
    assert again["status"] == "already-initialized"

    run_cli(cli_home, validator_rpc, "token", "mint", "200000", "--to", "customer")
    run_cli(cli_home, validator_rpc, "token", "mint", "200000", "--to", "worker-a")
    balance = run_cli(cli_home, validator_rpc, "token", "balance", "customer")
    assert balance["amount"] == 200_000 * 10**6
    assert balance["ui_amount"] == "200000"

    run_cli(
        cli_home,
        validator_rpc,
        "worker",
        "register",
        "--as",
        "worker-a",
        "--role",
        "worker-proof",
        "--capability",
        "worker-proof",
        "--software-digest",
        "worker-canonical",
    )
    # Tier-one floor is 50,000 KAI.
    staked = run_cli(cli_home, validator_rpc, "worker", "stake", "100000", "--as", "worker-a")
    assert staked["amount"] == 100_000 * 10**6

    opened = run_cli(
        cli_home,
        validator_rpc,
        "job",
        "open",
        "--as",
        "customer",
        "--class",
        "branch-proof",
        "--reward",
        "10",
        "--required-stake",
        "500",
        "--challenge-window",
        str(LOCAL_MIN_CHALLENGE_WINDOW),
        "--capability",
        "worker-proof",
        "--required-tier",
        "T1",
        "--nonce",
        "1",
    )
    assert opened["job"]

    run_cli(cli_home, validator_rpc, "job", "commit-input", "--job", opened["job"], "--cid", "bafkreitestinput", "--as", "customer")
    inspected = run_cli(cli_home, validator_rpc, "inspect", "job", opened["job"])
    assert inspected["account"]["status_name"] == "open"
    assert inspected["account"]["input_cid"] == "bafkreitestinput"

    run_cli(cli_home, validator_rpc, "job", "claim", opened["job"], "--as", "worker-a")
    claimed = run_cli(cli_home, validator_rpc, "inspect", "job", opened["job"])
    assert claimed["account"]["status_name"] == "claimed"
    assert claimed["account"]["worker"] == worker["pubkey"]

    run_cli(
        cli_home,
        validator_rpc,
        "job",
        "submit-receipt",
        opened["job"],
        "--output-cid",
        "bafkreitestoutput",
        "--result-bytes",
        "0a0b0c",
        "--as",
        "worker-a",
    )
    completed = run_cli(cli_home, validator_rpc, "inspect", "job", opened["job"])
    assert completed["account"]["status_name"] == "completed"

    time.sleep(4)
    run_cli(cli_home, validator_rpc, "settle", opened["job"])
    settled = run_cli(cli_home, validator_rpc, "inspect", "job", opened["job"])
    assert settled["account"]["status_name"] == "settled"

    paid = run_cli(cli_home, validator_rpc, "token", "balance", "worker-a")
    assert paid["amount"] == (200_000 - 100_000 + 10) * 10**6
    assert paid["ui_amount"] == "100010"


def _ensure_wallet(cli_home: Path, rpc_url: str, name: str) -> dict:
    shown = run_cli_raw(cli_home, rpc_url, "wallet", "show", name)
    if shown.returncode == 0:
        return json.loads(shown.stdout)
    return run_cli(cli_home, rpc_url, "wallet", "create", name, "--airdrop", "20")


def _ensure_protocol(cli_home: Path, rpc_url: str, admin_keypair_path: Path) -> str:
    """Initialize the protocol with a stand-in mint unless an earlier test already did; return the mint.

    Seeds the upgrade-authority keypair as the `admin` wallet first, so the helper works
    whichever test in this module runs first: `initialize_protocol` accepts only that
    signer, and `_ensure_wallet` would otherwise create a fresh random key.
    """

    seed_admin_wallet(cli_home, admin_keypair_path)
    # The seeded file makes `wallet show` succeed, so `_ensure_wallet` would not fund it.
    run_cli(cli_home, rpc_url, "wallet", "airdrop", "admin", "20")
    existing = run_cli(cli_home, rpc_url, "protocol", "show")["account"]
    if existing:
        return existing["payment_mint"]
    mint = run_cli(cli_home, rpc_url, "token", "create-mint", "--authority", "admin")["mint"]
    run_cli(cli_home, rpc_url, "protocol", "initialize", "--admin", "admin", "--payment-mint", mint)
    return mint


def _manifest(cli_home: Path, parent_run: str) -> dict:
    return json.loads((cli_home / ".config" / "kswarm" / "predict_runs" / f"{parent_run}.json").read_text(encoding="utf-8"))


def _framed_sha256(payload: bytes) -> str:
    import hashlib
    import struct

    return hashlib.sha256(struct.pack("<Q", len(payload)) + payload).hexdigest()


@pytest.mark.integration
def test_predict_open_interrupt_resume_cancel(cli_home: Path, validator_rpc: str, ipfs_api_url: str, admin_keypair_path: Path) -> None:
    """A real mid-loop failure: the customer can pay for two branch rewards, the third `open_job` fails on chain."""

    from kswarm_cli.reducer_image import AGGREGATE_REDUCER_IMAGE_ID

    _ensure_protocol(cli_home, validator_rpc, admin_keypair_path)
    # A fresh customer that can pay for exactly two branch rewards.
    run_cli(cli_home, validator_rpc, "wallet", "create", "customer-b", "--airdrop", "20")
    run_cli(cli_home, validator_rpc, "token", "mint", "2", "--to", "customer-b")

    open_args = [
        "predict",
        "open",
        "--question",
        "Will the seeded item be net-negative?",
        "--branches",
        "4",
        "--combiner",
        "trimmed-mean",
        "--trim-bps",
        "2500",
        "--reward-per-branch",
        "1",
        "--aggregator-reward",
        "1",
        "--challenge-window",
        "5",
        "--claim-window",
        "600",
        "--execution-window",
        "600",
        "--ipfs-api-url",
        ipfs_api_url,
        "--as",
        "customer-b",
    ]
    interrupted = run_cli_raw(cli_home, validator_rpc, *open_args)
    assert interrupted.returncode != 0, interrupted.stdout + interrupted.stderr
    first_line = interrupted.stderr.splitlines()[0]
    assert first_line.startswith("parent_run=") and " base_nonce=" in first_line and " run_manifest=" in first_line, first_line
    fields = dict(item.split("=", 1) for item in first_line.split())
    parent_run = fields["parent_run"]
    base_nonce = int(fields["base_nonce"])
    assert 0 <= base_nonce <= 2**64 - 1 - 4
    error = json.loads(interrupted.stdout)["error"]
    assert error["code"] == "CustomProgramError(1)", error  # SPL Token InsufficientFunds on the third escrow transfer

    run = _manifest(cli_home, parent_run)
    assert run["status"] == "opening"
    assert run["base_nonce"] == base_nonce
    assert [entry["status"] for entry in run["branch_jobs"]] == ["committed", "committed", "planned", "planned"]
    assert run["aggregate"]["status"] == "deferred"
    assert run["parent_manifest"]["combiner_parameters"] == {"trim_bps": 2500}

    status = run_cli(cli_home, validator_rpc, "predict", "status", parent_run)
    assert status["run_status"] == "opening"
    assert [(row["manifest_status"], row["status"]) for row in status["jobs"]] == [
        ("committed", "open"),
        ("committed", "open"),
        ("planned", "missing"),
        ("planned", "missing"),
        ("deferred", "missing"),
    ]
    branch0 = run_cli(cli_home, validator_rpc, "inspect", "job", run["branch_jobs"][0]["job"])["account"]
    assert branch0["nonce"] == base_nonce
    assert branch0["input_cid"] == run["branch_jobs"][0]["input_cid"]

    run_cli(cli_home, validator_rpc, "token", "mint", "100", "--to", "customer-b")
    resumed = run_cli(cli_home, validator_rpc, "predict", "resume", parent_run)
    assert resumed["run_status"] == "open"
    assert resumed["parent_run"] == parent_run
    run = _manifest(cli_home, parent_run)
    assert run["status"] == "open"
    assert all(entry["status"] == "committed" for entry in run["branch_jobs"])
    # The aggregate job is opened by `predict bind-aggregate`, once these branches
    # settle: its input artifact carries their receipts.
    assert run["aggregate"]["status"] == "deferred"
    for entry in run["branch_jobs"]:
        account = run_cli(cli_home, validator_rpc, "inspect", "job", entry["job"])["account"]
        assert account["status_name"] == "open"
        assert account["input_cid"] == entry["input_cid"]
        assert account["nonce"] == entry["nonce"]
        assert account["required_software_digest"] == entry["required_software_digest"]

    assert run_cli(cli_home, validator_rpc, "inspect", "job", run["aggregate_job"]).get("account") is None
    assert run["aggregate_input_cid"] is None
    assert run["aggregate_image_id"] == AGGREGATE_REDUCER_IMAGE_ID
    assert run["bonsol"] == {
        "bound": False,
        "reason": "aggregate job is opened by predict bind-aggregate",
        "image_id": AGGREGATE_REDUCER_IMAGE_ID,
    }
    # The plan is pinned even though the job is not open: it names the branch jobs, the
    # combiner and the reducer image this run committed to before any branch ran.
    pinned = httpx.post(f"{ipfs_api_url}/api/v0/cat", params={"arg": run["aggregate_plan_cid"]}, timeout=30)
    pinned.raise_for_status()
    aggregate_plan = json.loads(pinned.content)
    assert aggregate_plan["combiner_parameters"] == {"trim_bps": 2500}
    assert aggregate_plan["bonsol"]["image_id"] == AGGREGATE_REDUCER_IMAGE_ID
    assert [item["job"] for item in aggregate_plan["branch_jobs"]] == [entry["job"] for entry in run["branch_jobs"]]

    again = run_cli(cli_home, validator_rpc, "predict", "resume", parent_run)
    assert again["status"] == "already-open"

    cancelled = run_cli(cli_home, validator_rpc, "predict", "cancel", parent_run, "--as", "customer-b")
    assert cancelled["run_status"] == "cancelled"
    # Four branch jobs on chain; the aggregate was never opened, so it is only retired
    # in the manifest.
    assert sorted(cancelled["cancelled_jobs"]) == sorted(entry["job"] for entry in run["branch_jobs"])
    assert cancelled["skipped_jobs"] == []
    for entry in run["branch_jobs"]:
        assert run_cli(cli_home, validator_rpc, "inspect", "job", entry["job"])["account"]["status_name"] == "cancelled"
    assert _manifest(cli_home, parent_run)["aggregate"]["status"] == "cancelled"
    assert _manifest(cli_home, parent_run)["status"] == "cancelled"
    refused = run_cli_raw(cli_home, validator_rpc, "predict", "resume", parent_run)
    assert refused.returncode != 0
    assert "cancelled" in refused.stdout + refused.stderr
    refund = run_cli(cli_home, validator_rpc, "token", "balance", "customer-b")
    # The aggregate reward never left the customer: that job was never opened.
    assert refund["amount"] == 102 * 10**6, "every escrow was returned"
