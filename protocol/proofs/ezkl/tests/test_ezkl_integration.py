"""End-to-end check against the real ezkl package.

Skipped when `ezkl` is not importable. Runs prepare_assets.py, prove_branch.py
and verify_branch.py as subprocesses in a temporary directory, then tampers
with the bundle claim and confirms the verifier fails closed.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(importlib.util.find_spec("ezkl") is None, reason="ezkl package not installed")


def run(script, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    assets_dir = tmp_path_factory.mktemp("assets")
    completed = run("prepare_assets.py", "--output-dir", str(assets_dir))
    assert completed.returncode == 0, completed.stderr
    settings = json.loads((assets_dir / "settings.json").read_text())
    assert settings["run_args"]["input_visibility"] == "Public"
    assert settings["run_args"]["output_visibility"] == "Public"
    assert settings["run_args"]["param_visibility"] == "Fixed"
    return assets_dir


@pytest.fixture(scope="module")
def proven(assets, tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("proof")
    completed = run("prove_branch.py", "--assets-dir", str(assets), "--line-count", "3", "--word-count", "17", "--output-dir", str(out_dir))
    assert completed.returncode == 0, completed.stderr
    return out_dir


def verify_args(assets, bundle_path, proof_path, *extra):
    return ["--assets-dir", str(assets), "--bundle", str(bundle_path), "--proof", str(proof_path), *extra]


def test_real_proof_binds_to_claim(assets, proven):
    bundle = json.loads((proven / "bundle.json").read_text())
    assert bundle["score"] == 58.0
    completed = run("verify_branch.py", *verify_args(assets, proven / "bundle.json", proven / "proof.json"))
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["verified"] is True
    assert report["score"] == 58.0
    assert report["bound_instances"]["line_count"] == 3 * 256


def test_real_proof_matches_expected_claim(assets, proven):
    bundle = json.loads((proven / "bundle.json").read_text())
    completed = run(
        "verify_branch.py",
        *verify_args(assets, proven / "bundle.json", proven / "proof.json", "--expected-line-count", "3", "--expected-word-count", "17", "--expected-score-hex", bundle["score_hex"]),
    )
    assert completed.returncode == 0, completed.stderr


def test_real_proof_rejects_wrong_expected_claim(assets, proven):
    bundle = json.loads((proven / "bundle.json").read_text())
    completed = run(
        "verify_branch.py",
        *verify_args(assets, proven / "bundle.json", proven / "proof.json", "--expected-line-count", "4", "--expected-word-count", "17", "--expected-score-hex", bundle["score_hex"]),
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["verified"] is False


def test_tampered_bundle_claim_fails_closed(assets, proven, tmp_path):
    bundle = json.loads((proven / "bundle.json").read_text())
    bundle["features"]["word_count"] = 18.0
    tampered = tmp_path / "bundle.json"
    tampered.write_text(json.dumps(bundle))
    completed = run("verify_branch.py", *verify_args(assets, tampered, proven / "proof.json"))
    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["verified"] is False
    assert "word_count" in report["error"]
