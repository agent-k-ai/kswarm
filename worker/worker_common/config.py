from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from solders.pubkey import Pubkey

from kswarm_cli.config import APP_DIR, DEFAULT_CLUSTERS, ensure_base_config, load_cluster, predict_runs_dir
from kswarm_cli.constants import CAPABILITY_CLASS, NODE_ROLE, SOFTWARE_DIGEST, STAKE_TIER, ZERO_HASH
from kswarm_cli.encoding import parse_hash

from .ipfs import default_api_url


WORKER_CONFIG_PATH = APP_DIR / "worker.toml"
TRUE_VALUES = frozenset({"1", "true", "yes"})
FALSE_VALUES = frozenset({"0", "false", "no"})


@dataclass(frozen=True)
class WorkerConfig:
    cluster: str
    rpc_url: str
    program_id: Pubkey
    keypair_name: str
    # A keypair file outside the wallet directory (KSWARM_WALLET_FILE). When set it
    # wins over keypair_name, so a container can mount one key without the CLI's config.
    wallet_file: Path | None
    capabilities: tuple[bytes, ...]
    software_digest: bytes
    role: int
    tier: int
    polling_interval_seconds: float
    max_concurrent_claims: int
    ipfs_api_url: str
    metrics_host: str
    metrics_port: int
    llm_base_url: str | None
    llm_model_name: str | None
    challenge_on_mismatch: bool
    # Branch worker claim discipline (item 1 of the 2026-09-03 review).
    claim_cooldown_seconds: float
    execute_deadline_margin_seconds: float
    execute_retry_initial_seconds: float
    execute_retry_max_seconds: float
    # Verifier mode: re-execute (default) or hash-only (explicit opt-out).
    verifier_reexecute: bool
    # Where `predict open` writes run manifests; the aggregator runner reads them here.
    predict_runs_dir: Path


def load_worker_config(kind: str) -> WorkerConfig:
    ensure_base_config()
    file_data = _read_config_file(WORKER_CONFIG_PATH).get(kind, {})
    cluster = _value(file_data, "cluster", "KSWARM_CLUSTER", "local")
    cluster_config = DEFAULT_CLUSTERS.get(cluster, {})
    try:
        cluster_config = load_cluster(cluster)
    except Exception:
        pass
    rpc_url = _value(file_data, "rpc_url", "KSWARM_RPC_URL", cluster_config.get("rpc_url", "http://127.0.0.1:38899"))
    program_id = _resolve_program_id(cluster, _value(file_data, "program_id", "KSWARM_PROGRAM_ID", str(cluster_config.get("program_id", ""))))
    default_keypair = "worker-a" if kind == "branch_worker" else "verifier"
    if kind == "aggregator_runner":
        default_keypair = "aggregator"
    keypair_name = _value(file_data, "keypair_name", "KSWARM_WORKER_KEYPAIR", default_keypair)
    wallet_file = _optional_path(_value(file_data, "wallet_file", "KSWARM_WALLET_FILE", ""), "wallet_file")
    capabilities = tuple(_parse_hash_list(_value(file_data, "capabilities", "KSWARM_WORKER_CAPABILITIES", "worker-proof")))
    software_digest = _parse_known_hash(_value(file_data, "software_digest", "KSWARM_WORKER_SOFTWARE_DIGEST", "worker-canonical"), SOFTWARE_DIGEST)
    default_role = "worker-proof" if kind != "verifier_worker" else "verifier"
    role = NODE_ROLE[_value(file_data, "role", "KSWARM_WORKER_ROLE", default_role)]
    tier = STAKE_TIER[_value(file_data, "tier", "KSWARM_WORKER_TIER", "T1").upper()]
    polling_interval_seconds = float(_value(file_data, "polling_interval_seconds", "KSWARM_WORKER_POLL_SECONDS", "2.0"))
    max_concurrent_claims = _positive_int(_value(file_data, "max_concurrent_claims", "KSWARM_WORKER_MAX_CLAIMS", "1"), "max_concurrent_claims")
    ipfs_api_url = _value(file_data, "ipfs_api_url", "KSWARM_IPFS_API_URL", default_api_url())
    metrics_host = _value(file_data, "metrics_host", "KSWARM_WORKER_METRICS_HOST", "127.0.0.1")
    default_metrics_port = "9461" if kind == "branch_worker" else "9462"
    metrics_port = int(_value(file_data, "metrics_port", "KSWARM_WORKER_METRICS_PORT", default_metrics_port))
    challenge_on_mismatch = _boolean(_value(file_data, "challenge_on_mismatch", "KSWARM_CHALLENGE_ON_MISMATCH", "true"), "challenge_on_mismatch")
    claim_cooldown_seconds = _non_negative_float(
        _value(file_data, "claim_cooldown_seconds", "KSWARM_CLAIM_COOLDOWN_SECONDS", "300"), "claim_cooldown_seconds"
    )
    execute_deadline_margin_seconds = _non_negative_float(
        _value(file_data, "execute_deadline_margin_seconds", "KSWARM_EXECUTE_DEADLINE_MARGIN_SECONDS", "120"),
        "execute_deadline_margin_seconds",
    )
    execute_retry_initial_seconds = _positive_float(
        _value(file_data, "execute_retry_initial_seconds", "KSWARM_EXECUTE_RETRY_INITIAL_SECONDS", "5"), "execute_retry_initial_seconds"
    )
    execute_retry_max_seconds = _positive_float(
        _value(file_data, "execute_retry_max_seconds", "KSWARM_EXECUTE_RETRY_MAX_SECONDS", "60"), "execute_retry_max_seconds"
    )
    if execute_retry_initial_seconds > execute_retry_max_seconds:
        raise ValueError("execute_retry_initial_seconds must not exceed execute_retry_max_seconds")
    verifier_reexecute = resolve_verifier_mode(
        _value(file_data, "verifier_reexecute", "VERIFIER_REEXECUTE", "true"),
        _value(file_data, "verifier_hash_only", "VERIFIER_HASH_ONLY", "false"),
    )
    return WorkerConfig(
        cluster=cluster,
        rpc_url=str(rpc_url),
        program_id=program_id,
        keypair_name=str(keypair_name),
        wallet_file=wallet_file,
        capabilities=capabilities,
        software_digest=software_digest,
        role=role,
        tier=tier,
        polling_interval_seconds=polling_interval_seconds,
        max_concurrent_claims=max_concurrent_claims,
        ipfs_api_url=str(ipfs_api_url).rstrip("/"),
        metrics_host=str(metrics_host),
        metrics_port=metrics_port,
        llm_base_url=os.environ.get("LLM_BASE_URL"),
        llm_model_name=os.environ.get("LLM_MODEL_NAME"),
        challenge_on_mismatch=challenge_on_mismatch,
        claim_cooldown_seconds=claim_cooldown_seconds,
        execute_deadline_margin_seconds=execute_deadline_margin_seconds,
        execute_retry_initial_seconds=execute_retry_initial_seconds,
        execute_retry_max_seconds=execute_retry_max_seconds,
        verifier_reexecute=verifier_reexecute,
        predict_runs_dir=predict_runs_dir(),
    )


def _optional_path(value: str, name: str) -> Path | None:
    """An absolute or `~` path, or None when unset. The file must exist: a wrong path fails at start."""
    text = value.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{name} does not exist or is not a file: {path}")
    return path


def _resolve_program_id(cluster: str, value: str) -> Pubkey:
    """The protocol program id for `cluster`; fail closed when the profile has none."""
    if not value.strip():
        raise RuntimeError(
            f"cluster '{cluster}' has no program_id; the protocol program is not deployed there yet. "
            "Set KSWARM_PROGRAM_ID or add program_id to the cluster profile."
        )
    return Pubkey.from_string(value.strip())


def resolve_verifier_mode(reexecute_raw: str, hash_only_raw: str) -> bool:
    """Return True for re-execution. Hash-only needs the explicit VERIFIER_HASH_ONLY=1.

    Setting VERIFIER_REEXECUTE=0 without VERIFIER_HASH_ONLY=1 is refused: the
    weaker mode must be chosen by name, not reached by turning the strong one off.
    """

    reexecute = _boolean(reexecute_raw, "VERIFIER_REEXECUTE")
    hash_only = _boolean(hash_only_raw, "VERIFIER_HASH_ONLY")
    if hash_only:
        return False
    if not reexecute:
        raise ValueError("VERIFIER_REEXECUTE=0 requires the explicit VERIFIER_HASH_ONLY=1; hash-only verification is not a silent default")
    return True


def _read_config_file(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _value(data: dict, key: str, env_key: str, default: str) -> str:
    if env_key in os.environ and os.environ[env_key] != "":
        return os.environ[env_key]
    value = data.get(key, default)
    return str(value)


def _boolean(raw: str, name: str) -> bool:
    lowered = str(raw).strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of {sorted(TRUE_VALUES | FALSE_VALUES)}; got {raw!r}")


def _positive_int(raw: str, name: str) -> int:
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be at least 1; got {value}")
    return value


def _positive_float(raw: str, name: str) -> float:
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive; got {value}")
    return value


def _non_negative_float(raw: str, name: str) -> float:
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} must not be negative; got {value}")
    return value


def _parse_hash_list(raw: str) -> list[bytes]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return [_parse_known_hash(value, CAPABILITY_CLASS) for value in values]


def _parse_known_hash(value: str, known: dict[str, bytes]) -> bytes:
    if value in known:
        return known[value]
    if value in {"", "any", "zero"}:
        return ZERO_HASH
    return parse_hash(value)
