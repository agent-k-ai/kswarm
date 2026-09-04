"""pytest wrapper for scripts/swarm-smoke.sh: the containerized stack end to end.

Runs only with KSWARM_SWARM_SMOKE=1 on a host with Docker, a built program
artifact, and a reachable LLM endpoint (see the script header for the
environment). Everything else about the run lives in the script so the same
path is used by hand and by CI.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "swarm-smoke.sh"


@pytest.mark.integration
def test_swarm_smoke_end_to_end() -> None:
    if os.environ.get("KSWARM_SWARM_SMOKE") != "1":
        pytest.skip("set KSWARM_SWARM_SMOKE=1 to run the containerized swarm smoke test")
    if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_MODEL_NAME"):
        pytest.fail("LLM_ENDPOINT_UNREACHABLE: LLM_BASE_URL and LLM_MODEL_NAME must be set for the swarm smoke test")
    result = subprocess.run(
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=int(os.environ.get("KSWARM_SMOKE_TIMEOUT", "900")) + 900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in reversed(result.stdout.splitlines()) if line.startswith('{"swarm_smoke"'))
    payload = json.loads(line)["swarm_smoke"]
    assert payload["parent_run"]
    assert payload["final_scalar_bps"] is not None
    assert payload["branch_count"] == 2
    assert len(payload["settled_branch_jobs"]) == 2


def test_smoke_script_is_executable_and_uses_strict_mode() -> None:
    assert os.access(SCRIPT, os.X_OK), "scripts/swarm-smoke.sh must be executable"
    head = SCRIPT.read_text(encoding="utf-8").splitlines()[:40]
    assert head[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in head
