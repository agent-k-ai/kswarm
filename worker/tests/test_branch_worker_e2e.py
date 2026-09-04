"""Tier 2: real validator, real IPFS, real LLM.

Two scenarios share one bootstrapped protocol:

1. An honest branch worker executes and the verifier's re-execution attests
   to the same hash.
2. A lying worker claims a branch, never calls the model, submits a fabricated
   scalar, and the verifier's re-execution produces a different hash, attests
   to it, and challenges. The job ends slashed.
"""

from __future__ import annotations

import hashlib
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
from solders.pubkey import Pubkey

from app.protocol.branch_schemas import BranchInput, BranchOutput
from app.protocol.canonical_hash import branch_output_result_bytes
from branch_worker.executor import BranchExecutor
from kswarm_cli.protocol import assign_verifier_ix, claim_job_ix, fetch_config, fetch_job, submit_receipt_ix
from kswarm_cli.rpc import RpcClient, sign_and_send
from kswarm_cli.wallets import load_keypair_file
from worker_common.ipfs import IpfsClient


# Both scenarios drive a real validator, a real Kubo node, and a real LLM
# endpoint, so they belong to the `integration` marker and are excluded wherever
# those are not available. Without it they fail at fixture setup rather than
# deselecting, which breaks any run of the suite that does not have them.
pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAM_SO = REPO_ROOT / "solana" / "target" / "deploy" / "kswarm_protocol.so"
PROGRAM_ID = "ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM"
QUESTION = "Will sentiment around the seeded public news item be net-negative?"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _require_local_llm() -> tuple[str, str]:
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL_NAME")
    if not base_url or not model:
        pytest.fail("LLM_ENDPOINT_UNREACHABLE: LLM_BASE_URL and LLM_MODEL_NAME must be set for Tier 2")
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'local-llm')}"},
            timeout=5,
        )
        if response.status_code >= 500:
            pytest.fail(f"LLM_ENDPOINT_UNREACHABLE: {base_url} returned {response.status_code}")
    except httpx.HTTPError as exc:
        pytest.fail(f"LLM_ENDPOINT_UNREACHABLE: {base_url}: {exc}")
    return base_url, model


def _require_ipfs() -> str:
    api_url = os.environ.get("KSWARM_IPFS_API_URL") or os.environ.get("PROTOCOL_IPFS_API_URL") or "http://127.0.0.1:5001"
    try:
        response = httpx.post(f"{api_url.rstrip('/')}/api/v0/version", timeout=5)
        payload = response.json()
        if response.status_code != 200 or ("Version" not in payload and "version" not in payload):
            pytest.fail(f"IPFS_UNREACHABLE: {api_url}")
    except Exception as exc:
        pytest.fail(f"IPFS_UNREACHABLE: {api_url}: {exc}")
    return api_url


@pytest.fixture(scope="session")
def admin_keypair_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The program upgrade authority. `initialize_protocol` accepts only this signer."""
    path = tmp_path_factory.mktemp("upgrade-authority") / "admin.json"
    path.write_text(Keypair().to_json(), encoding="utf-8")
    # The CLI refuses a group- or world-readable key file (`InsecureKeyFileError`),
    # and `shutil.copyfile` preserves the mode, so seed it the way an operator must.
    path.chmod(0o600)
    return path


def seed_admin_wallet(home: Path, admin_keypair_path: Path) -> None:
    """Installs the upgrade-authority keypair as the CLI wallet named `admin`.

    `create_wallet` returns an existing wallet untouched, so a later
    `wallet create admin --airdrop` funds this key instead of replacing it.
    """
    wallets = home / ".config" / "kswarm" / "wallets"
    wallets.mkdir(parents=True, exist_ok=True)
    wallets.chmod(0o700)
    destination = wallets / "admin.json"
    shutil.copyfile(admin_keypair_path, destination)
    destination.chmod(0o600)


@pytest.fixture(scope="session")
def validator_rpc(tmp_path_factory: pytest.TempPathFactory, admin_keypair_path: Path) -> str:
    if not PROGRAM_SO.exists():
        pytest.fail(f"SOLANA_PROGRAM_ARTIFACT_MISSING: {PROGRAM_SO}")
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
        while time.time() < deadline:
            try:
                response = httpx.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []}, timeout=1)
                if response.status_code == 200 and response.json().get("result") == "ok":
                    yield rpc_url
                    return
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(0.5)
        output = process.stdout.read() if process.stdout else ""
        raise RuntimeError(f"validator did not become healthy\n{output}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def run_cli(home: Path, rpc_url: str, *args: str) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'cli'}:{REPO_ROOT / 'backend'}:{REPO_ROOT / 'worker'}"
    command = [sys.executable, "-m", "kswarm_cli.main", "--json", "--rpc-url", rpc_url, *args]
    result = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def run_worker(home: Path, rpc_url: str, module: str, keypair: str, ipfs_url: str) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'cli'}:{REPO_ROOT / 'backend'}:{REPO_ROOT / 'worker'}"
    env["KSWARM_RPC_URL"] = rpc_url
    env["KSWARM_WORKER_KEYPAIR"] = keypair
    env["KSWARM_IPFS_API_URL"] = ipfs_url
    command = [sys.executable, "-m", module, "--once"]
    result = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    assert " job failed " not in f" {result.stderr} ", result.stdout + result.stderr
    return result.stderr


def _wallet_pubkey(home: Path, name: str) -> Pubkey:
    return load_keypair_file(home / ".config" / "kswarm" / "wallets" / f"{name}.json").pubkey()


@pytest.fixture(scope="module")
def protocol_env(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    llm_base_url, llm_model = _require_local_llm()
    ipfs_url = _require_ipfs()
    rpc_url = request.getfixturevalue("validator_rpc")
    home = tmp_path_factory.mktemp("home")
    seed_admin_wallet(home, request.getfixturevalue("admin_keypair_path"))
    for wallet in ["admin", "customer", "worker-a", "verifier", "liar"]:
        run_cli(home, rpc_url, "wallet", "create", wallet, "--airdrop", "20")
    # Stand-in payment mint: classic SPL Token, 6 decimals (the KAI layout).
    mint = run_cli(home, rpc_url, "token", "create-mint", "--authority", "admin")
    assert mint["decimals"] == 6
    run_cli(home, rpc_url, "protocol", "initialize", "--admin", "admin", "--payment-mint", mint["mint"])
    for wallet in ["customer", "worker-a", "verifier", "liar"]:
        run_cli(home, rpc_url, "token", "mint", "200000", "--to", wallet)
    for wallet in ["worker-a", "liar"]:
        run_cli(home, rpc_url, "worker", "register", "--as", wallet, "--role", "worker-proof", "--capability", "worker-proof", "--software-digest", "worker-canonical")
        # Tier-one floor is 50,000 KAI.
        run_cli(home, rpc_url, "worker", "stake", "100000", "--as", wallet)
    run_cli(home, rpc_url, "worker", "register", "--as", "verifier", "--role", "verifier", "--capability", "worker-proof", "--software-digest", "worker-canonical")
    # The verifier floor is 100,000 KAI.
    run_cli(home, rpc_url, "worker", "stake", "150000", "--as", "verifier")
    return {"home": home, "rpc_url": rpc_url, "ipfs_url": ipfs_url, "llm_base_url": llm_base_url, "llm_model": llm_model}


def _open_single_branch_run(env: dict) -> str:
    opened = run_cli(
        env["home"],
        env["rpc_url"],
        "predict",
        "open",
        "--question",
        QUESTION,
        "--output-kind",
        "scalar",
        "--branches",
        "1",
        "--combiner",
        "weighted-mean",
        "--reward-per-branch",
        "1KAI",
        "--aggregator-reward",
        "1KAI",
        "--challenge-window",
        "600",
        "--persona-set",
        "builtin-public-opinion-v1",
        "--ipfs-api-url",
        env["ipfs_url"],
    )
    return opened["branch_jobs"][0]["job"]


def test_branch_worker_and_verifier_real_llm_cycle(protocol_env: dict) -> None:
    home, rpc_url, ipfs_url = protocol_env["home"], protocol_env["rpc_url"], protocol_env["ipfs_url"]
    branch_job = _open_single_branch_run(protocol_env)

    run_worker(home, rpc_url, "branch_worker.cli", "worker-a", ipfs_url)
    completed = run_cli(home, rpc_url, "inspect", "job", branch_job)
    assert completed["account"]["status_name"] == "completed"
    assert completed["account"]["result_bytes"]

    run_worker(home, rpc_url, "verifier_worker.cli", "verifier", ipfs_url)
    attested = run_cli(home, rpc_url, "inspect", "job", branch_job)
    assert attested["account"]["verifier_attestation_hash"] == attested["account"]["submitted_result_hash"]
    assert attested["account"]["verifier_evidence_cid"]
    assert attested["account"]["status_name"] == "completed"
    evidence = IpfsClient(ipfs_url).cat_json(attested["account"]["verifier_evidence_cid"])
    assert evidence["mode"] == "reexecute"
    assert evidence["matched"] is True
    assert evidence["verifier_transcript"]["llm"]["model"] == protocol_env["llm_model"]


def test_verifier_reexecution_catches_a_lying_worker(protocol_env: dict) -> None:
    home, rpc_url, ipfs_url = protocol_env["home"], protocol_env["rpc_url"], protocol_env["ipfs_url"]
    branch_job = _open_single_branch_run(protocol_env)
    job_key = Pubkey.from_string(branch_job)
    rpc = RpcClient(rpc_url)
    ipfs = IpfsClient(ipfs_url)
    liar = load_keypair_file(home / ".config" / "kswarm" / "wallets" / "liar.json")
    program_id = Pubkey.from_string(PROGRAM_ID)
    proto = fetch_config(rpc, program_id).addresses(program_id)

    # Only the verifier assigned to the job may challenge it, for every job class
    # (the H2-Interim rule). The customer assigns ours before the liar claims.
    customer = load_keypair_file(home / ".config" / "kswarm" / "wallets" / "customer.json")
    sign_and_send(rpc, customer, [assign_verifier_ix(program_id, customer.pubkey(), job_key, _wallet_pubkey(home, "verifier"))])
    assert run_cli(home, rpc_url, "inspect", "job", branch_job)["account"]["assigned_verifier_authority"] == str(_wallet_pubkey(home, "verifier"))

    # The liar claims the job like any worker would.
    sign_and_send(rpc, liar, [claim_job_ix(proto, liar.pubkey(), job_key)])
    claimed = fetch_job(rpc, job_key)
    assert claimed.worker == liar.pubkey()
    branch_input = BranchInput.model_validate_json(ipfs.cat_bytes(claimed.input_cid))

    # What the model actually says, so the lie is guaranteed to differ from it.
    executor = BranchExecutor(ipfs, llm_base_url=protocol_env["llm_base_url"], llm_model_name=protocol_env["llm_model"])
    honest = executor.execute(branch_job, branch_input)
    lie_bps = 5000 if honest.output.scalar_value_bps != 5000 else 2500

    # The lie: a fabricated scalar with honest-looking metadata, no model call.
    fake_transcript_cid = ipfs.add_json("fake-transcript.json", {"note": "the liar never called the model"})
    lie = BranchOutput(
        parent_job=branch_input.parent_job,
        branch_index=branch_input.branch_index,
        output_kind="scalar",
        scalar_value_bps=lie_bps,
        rng_seed=branch_input.rng_seed,
        llm_model=protocol_env["llm_model"],
        llm_version_hash=executor.llm_version_hash(branch_input),
        completed_at_unix=int(time.time()),
        transcript_cid=fake_transcript_cid,
    )
    lie_output_cid = ipfs.add_json("lie-output.json", lie.model_dump(mode="json", exclude_none=False))
    lie_bytes = branch_output_result_bytes(lie)
    sign_and_send(rpc, liar, [submit_receipt_ix(program_id, liar.pubkey(), job_key, lie_output_cid, lie_bytes)])
    submitted = run_cli(home, rpc_url, "inspect", "job", branch_job)
    assert submitted["account"]["status_name"] == "completed"
    assert submitted["account"]["submitted_result_hash"] == hashlib.sha256(lie_bytes).hexdigest()

    verifier_log = run_worker(home, rpc_url, "verifier_worker.cli", "verifier", ipfs_url)
    assert "matched=False" in verifier_log
    assert f"challenged job={branch_job}" in verifier_log

    slashed = run_cli(home, rpc_url, "inspect", "job", branch_job)
    account = slashed["account"]
    assert account["verifier_attestation_hash"] != account["submitted_result_hash"]
    assert account["status_name"] == "slashed"
    assert account["challenger"] == str(_wallet_pubkey(home, "verifier"))
    evidence = ipfs.cat_json(account["verifier_evidence_cid"])
    assert evidence["mode"] == "reexecute"
    assert evidence["matched"] is False
    assert evidence["worker_output"]["scalar_value_bps"] == lie_bps
    assert evidence["verifier_output"]["scalar_value_bps"] != lie_bps
    assert evidence["verifier_output"]["scalar_value_bps"] == honest.output.scalar_value_bps
    assert evidence["verifier_output"]["transcript_cid"] == fake_transcript_cid
