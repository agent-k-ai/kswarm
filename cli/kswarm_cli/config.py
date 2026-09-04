from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kswarm_cli.constants import KAI_DECIMALS, KAI_MAINNET_MINT, KSWARM_PROGRAM_ID, TOKEN_PROGRAM_ID


APP_DIR = Path.home() / ".config" / "kswarm"
WALLETS_DIR = APP_DIR / "wallets"
CLUSTERS_DIR = APP_DIR / "clusters"
ACTIVE_WALLET_PATH = APP_DIR / "active"
# Wallet files hold secret keys: the directory is owner-only and every key file is 0600.
PRIVATE_DIR_MODE = 0o700

BONSOL_VERIFIER_PROGRAM_ID_STR = "BoNsHRcyLLNdtnoDf8hiCNZpyehMC4FDMxs6NTxFi3ew"
MAINNET_RPC_URL_ENV = "SOLANA_RPC_URL"
DEFAULT_MAINNET_RPC_URL = "https://api.mainnet-beta.solana.com"
# Generic overrides, honored on every cluster. The worker daemons read the same names.
CLUSTER_ENV = "KSWARM_CLUSTER"
RPC_URL_ENV = "KSWARM_RPC_URL"
# Where `predict open` writes run manifests and the aggregator runner reads them.
# Containers set this to a volume shared by the CLI and the aggregator only.
PREDICT_RUNS_DIR_ENV = "KSWARM_PREDICT_RUNS_DIR"

# Cluster profile keys:
#   rpc_url, rpc_url_env (optional env override), program_id (absent until deployed),
#   payment_mint, payment_decimals, token_program (populated from chain),
#   bonsol_verifier_program_id, admin_wallet, mint_authority_wallet (local mints only).
DEFAULT_CLUSTERS: dict[str, dict[str, Any]] = {
    "local": {
        "name": "local",
        "rpc_url": "http://127.0.0.1:38899",
        "bonsol_verifier_program_id": BONSOL_VERIFIER_PROGRAM_ID_STR,
        "program_id": str(KSWARM_PROGRAM_ID),
    },
    "devnet": {
        "name": "devnet",
        "rpc_url": "https://api.devnet.solana.com",
        "bonsol_verifier_program_id": BONSOL_VERIFIER_PROGRAM_ID_STR,
        "program_id": str(KSWARM_PROGRAM_ID),
    },
    # Real funds. KAI is the payment mint. No program_id until the program is deployed to
    # mainnet (needs the external audit first); commands that need it fail with a clear message.
    "mainnet": {
        "name": "mainnet",
        "rpc_url": DEFAULT_MAINNET_RPC_URL,
        "rpc_url_env": MAINNET_RPC_URL_ENV,
        "bonsol_verifier_program_id": BONSOL_VERIFIER_PROGRAM_ID_STR,
        "payment_mint": str(KAI_MAINNET_MINT),
        "payment_decimals": KAI_DECIMALS,
        "token_program": str(TOKEN_PROGRAM_ID),
    },
}


def ensure_private_dir(path: Path) -> None:
    """Create `path` as an owner-only directory, tightening it when it already exists."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if stat.S_IMODE(path.stat().st_mode) != PRIVATE_DIR_MODE:
        os.chmod(path, PRIVATE_DIR_MODE)


def ensure_base_config() -> None:
    ensure_private_dir(WALLETS_DIR)
    CLUSTERS_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in DEFAULT_CLUSTERS.items():
        path = cluster_path(name)
        if not path.exists():
            write_json(path, payload)


def cluster_path(name: str) -> Path:
    return CLUSTERS_DIR / f"{name}.json"


def load_cluster(name: str) -> dict[str, Any]:
    path = cluster_path(name)
    if not path.exists():
        raise ValueError(f"unknown cluster profile: {name}")
    return read_json(path)


def save_cluster(name: str, payload: dict[str, Any]) -> None:
    existing = load_cluster(name)
    existing.update(payload)
    write_json(cluster_path(name), existing)


def resolve_rpc_url(cluster_config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> str:
    """The RPC URL for one invocation.

    Precedence: `KSWARM_RPC_URL`, then the env var the profile names in
    `rpc_url_env` (`SOLANA_RPC_URL` on mainnet), then the profile's `rpc_url`.
    """
    env = os.environ if environ is None else environ
    generic = env.get(RPC_URL_ENV, "").strip()
    if generic:
        return generic
    env_key = cluster_config.get("rpc_url_env")
    if env_key:
        override = env.get(str(env_key), "").strip()
        if override:
            return override
    return str(cluster_config["rpc_url"])


def predict_runs_dir(environ: Mapping[str, str] | None = None) -> Path:
    """`KSWARM_PREDICT_RUNS_DIR` when set, else `~/.config/kswarm/predict_runs`."""
    env = os.environ if environ is None else environ
    value = env.get(PREDICT_RUNS_DIR_ENV, "").strip()
    if value:
        return Path(value).expanduser()
    return APP_DIR / "predict_runs"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
