from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from rich.console import Console

from kswarm_cli.config import DEFAULT_CLUSTERS, predict_runs_dir, resolve_rpc_url
from kswarm_cli.constants import KAI_DECIMALS, KAI_MAINNET_MINT, TOKEN_PROGRAM_ID
from kswarm_cli.context import CliContext
from kswarm_cli.main import _program_id, _require_mint_cluster, _strip_token_suffix


REPO_ROOT = Path(__file__).resolve().parents[2]


def _context(cluster: str) -> CliContext:
    return CliContext(
        cluster_name=cluster,
        rpc_url="http://127.0.0.1:1",
        commitment="confirmed",
        keypair_path=None,
        json_output=True,
        console=Console(),
        cluster_config=dict(DEFAULT_CLUSTERS[cluster]),
    )


def test_mainnet_profile_pins_kai_and_has_no_program_id() -> None:
    mainnet = DEFAULT_CLUSTERS["mainnet"]
    assert mainnet["payment_mint"] == str(KAI_MAINNET_MINT)
    assert mainnet["payment_decimals"] == KAI_DECIMALS == 6
    assert mainnet["token_program"] == str(TOKEN_PROGRAM_ID)
    assert "program_id" not in mainnet
    assert mainnet["rpc_url"] == "https://api.mainnet-beta.solana.com"
    assert mainnet["rpc_url_env"] == "SOLANA_RPC_URL"


def test_local_and_devnet_profiles_have_program_id() -> None:
    for name in ("local", "devnet"):
        assert DEFAULT_CLUSTERS[name]["program_id"]


def test_resolve_rpc_url_prefers_env_for_mainnet() -> None:
    mainnet = DEFAULT_CLUSTERS["mainnet"]
    assert resolve_rpc_url(mainnet, {}) == "https://api.mainnet-beta.solana.com"
    assert resolve_rpc_url(mainnet, {"SOLANA_RPC_URL": "https://rpc.example/x"}) == "https://rpc.example/x"
    assert resolve_rpc_url(mainnet, {"SOLANA_RPC_URL": "   "}) == "https://api.mainnet-beta.solana.com"


def test_resolve_rpc_url_ignores_env_without_rpc_url_env_key() -> None:
    assert resolve_rpc_url(DEFAULT_CLUSTERS["local"], {"SOLANA_RPC_URL": "https://rpc.example/x"}) == "http://127.0.0.1:38899"


def test_program_id_fails_clearly_when_profile_lacks_it() -> None:
    with pytest.raises(typer.BadParameter, match="has no program_id"):
        _program_id(_context("mainnet"))


def test_program_id_reads_profile() -> None:
    assert str(_program_id(_context("local"))) == DEFAULT_CLUSTERS["local"]["program_id"]


def test_require_mint_cluster_rejects_mainnet() -> None:
    with pytest.raises(typer.BadParameter, match="only works on devnet, local"):
        _require_mint_cluster(_context("mainnet"), "token create-mint")
    _require_mint_cluster(_context("local"), "token create-mint")
    _require_mint_cluster(_context("devnet"), "token mint")


@pytest.mark.parametrize("value, expected", [("1KAI", "1"), ("1 kai", "1"), ("25", "25"), (" 2.5KAI ", "2.5")])
def test_strip_token_suffix(value: str, expected: str) -> None:
    assert _strip_token_suffix(value) == expected


def test_cli_mainnet_command_without_program_id_fails_without_rpc(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "kswarm_cli.main", "--json", "--cluster", "mainnet", "protocol", "show"],
        cwd=REPO_ROOT / "cli",
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode != 0
    assert "has no program_id" in result.stdout + result.stderr
    profile = json.loads((tmp_path / ".config" / "kswarm" / "clusters" / "mainnet.json").read_text())
    assert profile["payment_mint"] == str(KAI_MAINNET_MINT)


def test_cli_refuses_create_mint_on_mainnet(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "kswarm_cli.main", "--json", "--cluster", "mainnet", "token", "create-mint", "--authority", "admin"],
        cwd=REPO_ROOT / "cli",
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode != 0
    assert "only works on devnet, local" in result.stdout + result.stderr


def test_resolve_rpc_url_generic_env_wins_on_every_cluster() -> None:
    for name in ("local", "devnet", "mainnet"):
        assert resolve_rpc_url(DEFAULT_CLUSTERS[name], {"KSWARM_RPC_URL": "http://validator:8899"}) == "http://validator:8899"
    assert (
        resolve_rpc_url(DEFAULT_CLUSTERS["mainnet"], {"KSWARM_RPC_URL": "http://a", "SOLANA_RPC_URL": "http://b"}) == "http://a"
    )
    assert resolve_rpc_url(DEFAULT_CLUSTERS["mainnet"], {"KSWARM_RPC_URL": " ", "SOLANA_RPC_URL": "http://b"}) == "http://b"


def test_predict_runs_dir_env_override(tmp_path: Path) -> None:
    assert predict_runs_dir({}) == Path.home() / ".config" / "kswarm" / "predict_runs"
    assert predict_runs_dir({"KSWARM_PREDICT_RUNS_DIR": str(tmp_path / "runs")}) == tmp_path / "runs"
    assert predict_runs_dir({"KSWARM_PREDICT_RUNS_DIR": "  "}) == Path.home() / ".config" / "kswarm" / "predict_runs"


def test_cli_reads_cluster_and_rpc_url_from_env(tmp_path: Path) -> None:
    """Containers select the profile and RPC with KSWARM_CLUSTER and KSWARM_RPC_URL, no flags."""

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["KSWARM_CLUSTER"] = "mainnet"
    env["KSWARM_RPC_URL"] = "http://127.0.0.1:1"
    result = subprocess.run(
        [sys.executable, "-m", "kswarm_cli.main", "--json", "protocol", "show"],
        cwd=REPO_ROOT / "cli",
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # The mainnet profile has no program id, which proves the env selected it before any RPC call.
    assert result.returncode != 0
    assert "profile 'mainnet' has no program_id" in result.stdout + result.stderr
