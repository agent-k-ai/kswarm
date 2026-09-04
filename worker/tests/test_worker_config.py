from __future__ import annotations

import json
from pathlib import Path

import pytest
from solders.keypair import Keypair

from worker_common import config as config_module
from worker_common.config import load_worker_config, resolve_verifier_mode
from worker_common.ipfs import DEFAULT_IPFS_API_URL
from worker_common.protocol import load_session_wallet


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(config_module, "WORKER_CONFIG_PATH", tmp_path / "worker.toml")
    monkeypatch.setattr(config_module, "ensure_base_config", lambda: None)
    for name in (
        "KSWARM_WORKER_MAX_CLAIMS",
        "KSWARM_IPFS_API_URL",
        "PROTOCOL_IPFS_API_URL",
        "KSWARM_CLAIM_COOLDOWN_SECONDS",
        "KSWARM_EXECUTE_DEADLINE_MARGIN_SECONDS",
        "KSWARM_EXECUTE_RETRY_INITIAL_SECONDS",
        "KSWARM_EXECUTE_RETRY_MAX_SECONDS",
        "VERIFIER_REEXECUTE",
        "VERIFIER_HASH_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "worker.toml"


@pytest.mark.parametrize(
    ("reexecute", "hash_only", "expected"),
    [("true", "false", True), ("1", "0", True), ("true", "true", False), ("false", "true", False), ("0", "1", False)],
)
def test_verifier_mode_resolution(reexecute: str, hash_only: str, expected: bool) -> None:
    assert resolve_verifier_mode(reexecute, hash_only) is expected


def test_verifier_mode_refuses_to_turn_reexecution_off_without_naming_hash_only() -> None:
    with pytest.raises(ValueError, match="VERIFIER_HASH_ONLY=1"):
        resolve_verifier_mode("false", "false")
    with pytest.raises(ValueError, match="must be one of"):
        resolve_verifier_mode("maybe", "false")


def test_defaults_are_the_documented_claim_discipline(isolated_config) -> None:
    config = load_worker_config("branch_worker")

    assert config.max_concurrent_claims == 1
    assert config.claim_cooldown_seconds == 300.0
    assert config.execute_deadline_margin_seconds == 120.0
    assert config.execute_retry_initial_seconds == 5.0
    assert config.execute_retry_max_seconds == 60.0
    assert config.verifier_reexecute is True
    assert config.ipfs_api_url == DEFAULT_IPFS_API_URL == "http://127.0.0.1:5001"


def test_environment_overrides_and_validation(isolated_config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSWARM_WORKER_MAX_CLAIMS", "3")
    monkeypatch.setenv("PROTOCOL_IPFS_API_URL", "http://protocol-ipfs:5001/")
    monkeypatch.setenv("VERIFIER_HASH_ONLY", "1")
    config = load_worker_config("verifier_worker")
    assert config.max_concurrent_claims == 3
    assert config.ipfs_api_url == "http://protocol-ipfs:5001"
    assert config.verifier_reexecute is False

    monkeypatch.setenv("KSWARM_WORKER_MAX_CLAIMS", "0")
    with pytest.raises(ValueError, match="max_concurrent_claims"):
        load_worker_config("branch_worker")
    monkeypatch.setenv("KSWARM_WORKER_MAX_CLAIMS", "1")

    monkeypatch.setenv("KSWARM_EXECUTE_RETRY_INITIAL_SECONDS", "90")
    with pytest.raises(ValueError, match="execute_retry_initial_seconds"):
        load_worker_config("branch_worker")
    monkeypatch.delenv("KSWARM_EXECUTE_RETRY_INITIAL_SECONDS")

    monkeypatch.setenv("KSWARM_CLAIM_COOLDOWN_SECONDS", "-1")
    with pytest.raises(ValueError, match="claim_cooldown_seconds"):
        load_worker_config("branch_worker")


def test_toml_file_values_are_read_per_kind(isolated_config) -> None:
    isolated_config.write_text(
        '[branch_worker]\nmax_concurrent_claims = 2\nclaim_cooldown_seconds = 30\n\n[verifier_worker]\nverifier_hash_only = true\n',
        encoding="utf-8",
    )
    branch = load_worker_config("branch_worker")
    verifier = load_worker_config("verifier_worker")
    assert branch.max_concurrent_claims == 2
    assert branch.claim_cooldown_seconds == 30.0
    assert branch.verifier_reexecute is True
    assert verifier.verifier_reexecute is False


def test_wallet_file_and_predict_runs_dir_come_from_the_environment(isolated_config, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in ("KSWARM_WALLET_FILE", "KSWARM_PREDICT_RUNS_DIR"):
        monkeypatch.delenv(name, raising=False)
    config = load_worker_config("branch_worker")
    assert config.wallet_file is None
    assert config.keypair_name == "worker-a"
    assert config.predict_runs_dir == Path.home() / ".config" / "kswarm" / "predict_runs"

    key_file = tmp_path / "keys" / "worker-a.json"
    key_file.parent.mkdir()
    key_file.write_text(Keypair().to_json(), encoding="utf-8")
    monkeypatch.setenv("KSWARM_WALLET_FILE", str(key_file))
    monkeypatch.setenv("KSWARM_PREDICT_RUNS_DIR", str(tmp_path / "runs"))
    config = load_worker_config("aggregator_runner")
    assert config.wallet_file == key_file
    assert config.predict_runs_dir == tmp_path / "runs"

    monkeypatch.setenv("KSWARM_WALLET_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="wallet_file does not exist"):
        load_worker_config("branch_worker")


def test_session_wallet_prefers_the_mounted_file(tmp_path) -> None:
    keypair = Keypair()
    key_file = tmp_path / "branch-worker.json"
    key_file.write_text(keypair.to_json(), encoding="utf-8")
    # The CLI refuses a group- or world-readable key file, so a mounted wallet must
    # arrive the way an operator is told to create one.
    key_file.chmod(0o600)
    wallet = load_session_wallet("worker-a", key_file)
    assert wallet.pubkey == keypair.pubkey()
    assert wallet.name == "branch-worker"
    assert wallet.path == key_file

    # The Node runtime writes keys as a JSON byte array; that format loads too.
    array_file = tmp_path / "array.json"
    array_file.write_text(json.dumps(list(bytes(keypair))), encoding="utf-8")
    array_file.chmod(0o600)
    assert load_session_wallet("ignored", array_file).pubkey == keypair.pubkey()

    with pytest.raises(FileNotFoundError, match="wallet does not exist"):
        load_session_wallet("no-such-wallet", None)
