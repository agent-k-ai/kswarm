from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console


@dataclass(frozen=True)
class CliContext:
    cluster_name: str
    rpc_url: str
    commitment: str
    keypair_path: Path | None
    json_output: bool
    console: Console
    cluster_config: dict[str, Any]

    @classmethod
    def load(
        cls,
        cluster_name: str,
        rpc_url: str | None,
        commitment: str,
        keypair_path: str | None,
        json_output: bool,
        console: Console,
    ) -> "CliContext":
        from kswarm_cli.config import ensure_base_config, load_cluster, resolve_rpc_url

        ensure_base_config()
        cluster_config = load_cluster(cluster_name)
        effective_rpc_url = rpc_url or resolve_rpc_url(cluster_config)
        return cls(
            cluster_name=cluster_name,
            rpc_url=effective_rpc_url,
            commitment=commitment,
            keypair_path=Path(keypair_path).expanduser() if keypair_path else None,
            json_output=json_output,
            console=console,
            cluster_config=cluster_config,
        )
