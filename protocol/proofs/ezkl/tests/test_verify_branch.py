"""CLI-level tests for verify_branch.py with a stub `ezkl` module.

The stub records the call and returns the configured verdict. The tests cover
the hash checks, the binding check, the expected-claim arguments, and the
fail-closed JSON report. No prover binary is needed.
"""

import copy
import hashlib
import json
import sys
import types

import pytest

import verify_branch
from binding import BindingError, signed_to_felt_hex
from proof_fixtures import INSTANCES, SCORE_FELT, ZERO_FELT, make_bundle, make_settings


class StubEzkl:
    def __init__(self, verdict=True):
        self.verdict = verdict
        self.calls = []

    def verify(self, proof_path, settings_path, vk_path, srs_path, reduced):
        self.calls.append((proof_path, settings_path, vk_path, srs_path, reduced))
        return self.verdict


@pytest.fixture
def stub_ezkl(monkeypatch):
    stub = StubEzkl()
    monkeypatch.setitem(sys.modules, "ezkl", types.SimpleNamespace(verify=stub.verify))
    return stub


@pytest.fixture
def workspace(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "settings.json").write_text(json.dumps(make_settings()))
    (assets / "kzg.srs").write_bytes(b"srs")
    vk_bytes = b"verification-key"
    (assets / "vk.key").write_bytes(vk_bytes)

    proof = {"protocol": None, "instances": copy.deepcopy(INSTANCES), "proof": "00"}
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(proof))

    bundle = make_bundle(
        proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest(),
        vk_sha256=hashlib.sha256(vk_bytes).hexdigest(),
    )
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle))
    return types.SimpleNamespace(assets=assets, proof_path=proof_path, bundle_path=bundle_path, bundle=bundle, proof=proof)


def base_args(workspace, *extra):
    return [
        "--assets-dir",
        str(workspace.assets),
        "--bundle",
        str(workspace.bundle_path),
        "--proof",
        str(workspace.proof_path),
        *extra,
    ]


def test_verify_reports_bound_instances(workspace, stub_ezkl):
    report = verify_branch.verify(verify_branch.parse_args(base_args(workspace)))
    assert report["verified"] is True
    assert report["score"] == 58.0
    assert report["score_hex"] == SCORE_FELT
    assert report["bound_instances"] == {
        "input_scale": 8,
        "line_count": 768,
        "output_scale": 8,
        "score": 14848,
        "word_count": 4352,
    }
    assert len(stub_ezkl.calls) == 1
    assert stub_ezkl.calls[0][4] is False


def test_verify_accepts_matching_expected_claim(workspace, stub_ezkl):
    args = verify_branch.parse_args(
        base_args(workspace, "--expected-line-count", "3", "--expected-word-count", "17", "--expected-score-hex", SCORE_FELT)
    )
    assert verify_branch.verify(args)["verified"] is True


@pytest.mark.parametrize(
    "extra,field",
    [
        (["--expected-line-count", "4", "--expected-word-count", "17", "--expected-score-hex", SCORE_FELT], "line_count"),
        (["--expected-line-count", "3", "--expected-word-count", "1", "--expected-score-hex", SCORE_FELT], "word_count"),
        (["--expected-line-count", "3", "--expected-word-count", "17", "--expected-score-hex", ZERO_FELT], "score_hex"),
    ],
)
def test_verify_rejects_expected_claim_mismatch(workspace, stub_ezkl, extra, field):
    with pytest.raises(BindingError, match=field):
        verify_branch.verify(verify_branch.parse_args(base_args(workspace, *extra)))


def test_partial_expected_arguments_are_rejected(workspace):
    with pytest.raises(SystemExit):
        verify_branch.parse_args(base_args(workspace, "--expected-line-count", "3"))


def test_verify_fails_when_ezkl_rejects(workspace, stub_ezkl):
    stub_ezkl.verdict = False
    with pytest.raises(BindingError, match="ezkl.verify returned false"):
        verify_branch.verify(verify_branch.parse_args(base_args(workspace)))


def test_verify_fails_on_proof_hash_mismatch(workspace, stub_ezkl):
    workspace.proof_path.write_text(json.dumps(workspace.proof, indent=1))
    with pytest.raises(BindingError, match="proof sha256"):
        verify_branch.verify(verify_branch.parse_args(base_args(workspace)))
    assert stub_ezkl.calls == []


def test_verify_fails_on_vk_hash_mismatch(workspace, stub_ezkl):
    (workspace.assets / "vk.key").write_bytes(b"other-key")
    with pytest.raises(BindingError, match="verification key sha256"):
        verify_branch.verify(verify_branch.parse_args(base_args(workspace)))


def test_verify_fails_when_instances_do_not_match_bundle_claim(workspace, stub_ezkl):
    proof = copy.deepcopy(workspace.proof)
    proof["instances"][0][2] = signed_to_felt_hex(59 * 256)
    workspace.proof_path.write_text(json.dumps(proof))
    bundle = copy.deepcopy(workspace.bundle)
    bundle["proof_sha256"] = hashlib.sha256(workspace.proof_path.read_bytes()).hexdigest()
    bundle["public_instances"] = proof["instances"]
    workspace.bundle_path.write_text(json.dumps(bundle))
    with pytest.raises(BindingError, match="score_hex"):
        verify_branch.verify(verify_branch.parse_args(base_args(workspace)))


def test_verify_fails_when_instances_missing(workspace, stub_ezkl):
    proof = {"protocol": None, "proof": "00"}
    workspace.proof_path.write_text(json.dumps(proof))
    bundle = copy.deepcopy(workspace.bundle)
    bundle["proof_sha256"] = hashlib.sha256(workspace.proof_path.read_bytes()).hexdigest()
    workspace.bundle_path.write_text(json.dumps(bundle))
    with pytest.raises(BindingError, match="proof.instances missing"):
        verify_branch.verify(verify_branch.parse_args(base_args(workspace)))


def test_main_prints_error_json_and_exits_1(workspace, stub_ezkl, monkeypatch, capsys):
    stub_ezkl.verdict = False
    monkeypatch.setattr(sys, "argv", ["verify_branch.py", *base_args(workspace)])
    with pytest.raises(SystemExit) as exit_info:
        verify_branch.main()
    assert exit_info.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {"error": "ezkl.verify returned false", "verified": False}


def test_main_prints_report_and_exits_0(workspace, stub_ezkl, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify_branch.py", *base_args(workspace)])
    verify_branch.main()
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is True
    assert report["score_hex"] == SCORE_FELT
