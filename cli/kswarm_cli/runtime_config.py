"""The `protocol.json` runtime file that the Node artifact gateway and watcher read.

`protocol/src/runtime.mjs` reads this file: `rpcUrl` (connection), `paymentMint`,
`tokenProgramId`, `paymentDecimals`, and `stakeFloors.<name>` (base units as decimal
strings, because JavaScript numbers cannot hold every u64). The values mirror the
on-chain `ProtocolConfig`; nothing here is a second source of truth.

`protocol/test/runtime-config.test.mjs` reads the fixture that
`cli/tests/test_runtime_config.py` writes from fixed inputs, so a change on either
side breaks one of the two suites.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey

from kswarm_cli.protocol import ProtocolConfigAccount


DEFAULT_ARTIFACT_GATEWAY_URL = "http://protocol-api:7001"


def runtime_config_payload(
    config: ProtocolConfigAccount,
    program_id: Pubkey,
    rpc_url: str,
    artifact_gateway_url: str = DEFAULT_ARTIFACT_GATEWAY_URL,
) -> dict[str, Any]:
    """The file content, keyed the way the Node readers expect."""

    return {
        "artifactGatewayUrl": artifact_gateway_url,
        "paymentMint": str(config.payment_mint),
        "tokenProgramId": str(config.token_program),
        "paymentDecimals": config.payment_decimals,
        "stakeFloors": {
            "tierOne": str(config.tier_one_stake_floor),
            "tierTwo": str(config.tier_two_stake_floor),
            "tierThree": str(config.tier_three_stake_floor),
            "verifier": str(config.verifier_stake_floor),
        },
        "programId": str(program_id),
        "rpcUrl": rpc_url,
    }


def write_runtime_config(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically: a reader polling for the file never sees a partial document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
