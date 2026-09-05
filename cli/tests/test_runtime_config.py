"""`protocol runtime-config`: the protocol.json the Node api and watcher read.

The fixture under `protocol/test/fixtures/protocol.json` is the output of
`runtime_config_payload` for the fixed inputs below. `protocol/test/runtime-config.test.mjs`
reads that same file through `protocol/src/runtime.mjs`, so the two suites pin the
contract from both sides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from solders.pubkey import Pubkey
from typer.testing import CliRunner

from kswarm_cli import main as cli_main
from kswarm_cli.constants import KAI_MAINNET_MINT, KSWARM_PROGRAM_ID, TOKEN_PROGRAM_ID
from kswarm_cli.context import CliContext
from kswarm_cli.protocol import ProtocolConfigAccount
from kswarm_cli.runtime_config import DEFAULT_ARTIFACT_GATEWAY_URL, runtime_config_payload, write_runtime_config


REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_FIXTURE = REPO_ROOT / "protocol" / "test" / "fixtures" / "protocol.json"
# The one program id, so a rotation cannot leave a test behind.
PROGRAM_ID = KSWARM_PROGRAM_ID
ADMIN = Pubkey.from_string("2v5j6w7SYJkwyySGAUprSFcJHN2Dk1dc8ktncy4UFXSK")
RPC_URL = "http://solana-validator:8899"

CONFIG = ProtocolConfigAccount(
    bump=254,
    admin=ADMIN,
    payment_mint=KAI_MAINNET_MINT,
    token_program=TOKEN_PROGRAM_ID,
    payment_decimals=6,
    tier_one_stake_floor=50_000_000_000,
    tier_two_stake_floor=250_000_000_000,
    tier_three_stake_floor=1_000_000_000_000,
    verifier_stake_floor=100_000_000_000,
    min_challenge_window_seconds=36_000,
)


def _expected() -> dict[str, Any]:
    return {
        "artifactGatewayUrl": DEFAULT_ARTIFACT_GATEWAY_URL,
        "paymentMint": str(KAI_MAINNET_MINT),
        "tokenProgramId": str(TOKEN_PROGRAM_ID),
        "paymentDecimals": 6,
        "stakeFloors": {
            "tierOne": "50000000000",
            "tierTwo": "250000000000",
            "tierThree": "1000000000000",
            "verifier": "100000000000",
        },
        "programId": str(PROGRAM_ID),
        "rpcUrl": RPC_URL,
    }


def test_payload_mirrors_the_on_chain_config_with_u64_floors_as_strings() -> None:
    assert runtime_config_payload(CONFIG, PROGRAM_ID, RPC_URL) == _expected()


def test_node_fixture_is_this_payload() -> None:
    """Regenerate with: python -c 'from tests.test_runtime_config import regenerate; regenerate()' (from cli/)."""

    assert json.loads(NODE_FIXTURE.read_text(encoding="utf-8")) == _expected()


def regenerate() -> None:
    write_runtime_config(NODE_FIXTURE, _expected())


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "protocol.json"
    write_runtime_config(target, _expected())
    assert json.loads(target.read_text(encoding="utf-8")) == _expected()
    assert sorted(path.name for path in target.parent.iterdir()) == ["protocol.json"]
    assert target.read_text(encoding="utf-8").endswith("}\n")


def test_command_writes_the_file_and_fails_before_initialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = CliContext(
        cluster_name="local",
        rpc_url=RPC_URL,
        commitment="confirmed",
        keypair_path=None,
        json_output=True,
        console=Console(),
        cluster_config={"name": "local", "rpc_url": RPC_URL, "program_id": str(PROGRAM_ID)},
    )
    monkeypatch.setattr(cli_main.CliContext, "load", classmethod(lambda cls, **kwargs: context))
    monkeypatch.setattr(cli_main, "_rpc", lambda ctx: object())
    state: dict[str, Any] = {"config": None}
    monkeypatch.setattr(cli_main, "fetch_config", lambda rpc, program_id: state["config"])
    runner = CliRunner()
    output = tmp_path / "protocol.json"

    missing = runner.invoke(cli_main.app, ["--json", "protocol", "runtime-config", "--output", str(output)])
    assert missing.exit_code != 0
    assert "not initialized" in missing.output
    assert not output.exists()

    state["config"] = CONFIG
    written = runner.invoke(
        cli_main.app,
        ["--json", "protocol", "runtime-config", "--output", str(output), "--artifact-gateway-url", "http://gateway:7001"],
    )
    assert written.exit_code == 0, written.output
    payload = json.loads(written.stdout)
    assert payload["path"] == str(output)
    assert json.loads(output.read_text(encoding="utf-8")) == {**_expected(), "artifactGatewayUrl": "http://gateway:7001"}
