from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_public_opinion_demo_script() -> None:
    if os.environ.get("RUN_TIER3_DEMO") != "1":
        pytest.skip("set RUN_TIER3_DEMO=1 to run the Bonsol-stack public-opinion demo")
    if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_MODEL_NAME"):
        pytest.fail("LLM_ENDPOINT_UNREACHABLE: LLM_BASE_URL and LLM_MODEL_NAME must be set for Tier 3")
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "demo-public-opinion.sh")],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1200,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "public_opinion_report" in result.stdout
    report_line = next(
        line for line in reversed(result.stdout.splitlines())
        if line.startswith('{"public_opinion_report"')
    )
    payload = json.loads(report_line)
    report = payload["public_opinion_report"]
    assert report["final_scalar_bps"] is not None
    assert report["aggregate_output"]["result"]["branch_count"] == 2
    assert report["branch_narrative_excerpts"]
