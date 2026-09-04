"""The aggregate runner proves what the job already committed, or it submits nothing.

The runner does not decide the aggregate. `kswarm predict bind-aggregate` fixed the
job's `input_bundle_hash` and `expected_result_hash` against an MFA3 artifact built
from the settled branch receipts, so every test here starts from a real artifact and
asks what the runner does when some part of it does not add up.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from aggregator_runner.bonsol_hook import BonsolBinding, BonsolHookError, journal_hash_for, parse_hook_output, run_bonsol_hook
from aggregator_runner.run_store import RunStore
from aggregator_runner.runner import (
    ALLOW_UNBOUND_ENV,
    RECEIPT_BINDING_BONSOL,
    RECEIPT_BINDING_UNPROVEN,
    AggregateBinding,
    AggregationError,
    AggregatorRunner,
    check_binding_against_job,
)
from app.protocol.branch_schemas import BranchOutput
from app.protocol.canonical_hash import branch_output_result_bytes
from fakes import FakeIpfs, FakeJob, FakeProtocol, make_config
from kswarm_cli.aggregate import aggregate_journal, build_aggregate_artifact
from kswarm_cli.bonsol import framed_input_digest
from kswarm_cli.constants import JOB_CLASS, JOB_STATUS_BY_NAME, ZERO_HASH


IMAGE_ID = bytes.fromhex("11" * 32)


def _output(index: int, *, scalar: int | None = None, label: int | None = None) -> BranchOutput:
    kind = "categorical" if label is not None else "scalar"
    return BranchOutput(
        parent_job="parent-run",
        branch_index=index,
        output_kind=kind,
        scalar_value_bps=scalar,
        categorical_label_index=label,
        rng_seed=index,
        llm_model="stub-model",
        llm_version_hash="a" * 64,
        completed_at_unix=1,
        transcript_cid="bafktranscript",
    )


def _binding(execution_id: str, journal) -> BonsolBinding:
    return BonsolBinding(
        execution_id,
        IMAGE_ID,
        journal.input_digest,
        journal.output_digest,
        journal.journal_hash,
        journal.committed_outputs,
    )


def _hook_json(journal, **overrides: object) -> str:
    payload: dict[str, object] = {
        "execution_id": "exec-1",
        "image_id": "0x" + IMAGE_ID.hex(),
        "input_digest": journal.input_digest.hex(),
        "output_digest": journal.output_digest.hex(),
        "journal_hash": journal.journal_hash.hex(),
        "committed_outputs": journal.committed_outputs.hex(),
    }
    payload.update(overrides)
    return json.dumps({key: value for key, value in payload.items() if value is not None})


class _Fixture:
    """A bound aggregate job, its artifact, and the branch jobs the artifact names."""

    def __init__(self, tmp_path: Path, *, combiner: str = "weighted-mean", scalars=(4000, 6000), **runner_kwargs):
        self.ipfs = FakeIpfs()
        self.branch_keys = [Pubkey.new_unique() for _ in scalars]
        self.aggregate_key = Pubkey.new_unique()
        self.jobs: dict[Pubkey, FakeJob] = {}
        branches = []
        for index, (key, scalar) in enumerate(zip(self.branch_keys, scalars)):
            output = _output(index, scalar=scalar)
            result_bytes = branch_output_result_bytes(output)
            output_cid = self.ipfs.add_json("out", output.model_dump(mode="json", exclude_none=False))
            self.jobs[key] = FakeJob(
                status=JOB_STATUS_BY_NAME["settled"],
                output_cid=output_cid,
                result_bytes=result_bytes,
                submitted_result_hash=hashlib.sha256(result_bytes).digest(),
            )
            branches.append(
                {
                    "branch_index": index,
                    "job": str(key),
                    "output_cid": output_cid,
                    "result_bytes": result_bytes.hex(),
                    "weight": 1,
                }
            )
        self.artifact = build_aggregate_artifact(
            parent_run=str(self.aggregate_key),
            parent_manifest_cid="bafyparent",
            output_schema_hash="b" * 64,
            combiner=combiner,
            combiner_parameters={"trim_bps": 5000} if combiner == "trimmed-mean" else {},
            branches=branches,
        )
        self.journal = aggregate_journal(self.artifact)
        self.input_cid = self.ipfs.add_bytes("aggregate-input.json", self.artifact)
        self.jobs[self.aggregate_key] = FakeJob(
            status=JOB_STATUS_BY_NAME["open"],
            job_class=JOB_CLASS["aggregate-proof"],
            execute_deadline=5000,
            input_cid=self.input_cid,
            input_bundle_hash=self.journal.input_digest,
            required_software_digest=IMAGE_ID,
            expected_result_hash=self.journal.journal_hash,
        )
        self.protocol = FakeProtocol(self.jobs)
        runs_dir = tmp_path / "predict_runs"
        runs_dir.mkdir(exist_ok=True)
        self.run_path = runs_dir / f"{self.aggregate_key}.json"
        self.run_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "parent_run": str(self.aggregate_key),
                    "aggregate_job": str(self.aggregate_key),
                    "combiner": combiner,
                    "combiner_parameters": {},
                    "parent_manifest": {"output_schema_hash": "b" * 64},
                    "branch_jobs": [{"branch_index": i, "job": str(k)} for i, k in enumerate(self.branch_keys)],
                    "aggregate_submitted": False,
                }
            ),
            encoding="utf-8",
        )
        defaults = {
            "hook_command": "hook --production",
            "hook_runner": lambda *args, **kwargs: _binding("exec-1", self.journal),
            "environ": {},
        }
        defaults.update(runner_kwargs)
        self.runner = AggregatorRunner(
            make_config(keypair_name="aggregator"),
            protocol=self.protocol,
            ipfs=self.ipfs,
            store=RunStore(runs_dir),
            clock=lambda: 2_000.0,
            **defaults,
        )

    def saved(self) -> dict:
        return json.loads(self.run_path.read_text(encoding="utf-8"))


def test_runner_proves_the_committed_artifact_and_persists_atomically(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    seen: list[dict] = []

    fixture = _Fixture(tmp_path, hook_runner=lambda command, payload, **kwargs: (seen.append((command, payload)), None)[1])
    # Re-bind the hook now that the fixture has a journal to answer with.
    fixture.runner.hook_runner = lambda command, payload, **kwargs: (
        seen.append((command, payload)),
        _binding("exec-1", fixture.journal),
    )[1]

    with caplog.at_level(logging.INFO, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 1

    assert fixture.protocol.claims == [fixture.aggregate_key]
    job_key, output_cid, result_bytes = fixture.protocol.receipts[0]
    assert job_key == fixture.aggregate_key
    assert result_bytes == fixture.journal.committed_outputs
    assert len(result_bytes) == 73
    assert fixture.jobs[fixture.aggregate_key].submitted_result_hash == fixture.journal.output_digest

    saved = fixture.saved()
    assert saved["aggregate_submitted"] is True
    assert saved["aggregate_output_cid"] == output_cid
    assert saved["aggregate_result_hash"] == fixture.journal.output_digest.hex()
    assert saved["aggregate_claimed_at_unix"] == 2000
    assert saved["aggregate_journal"]["journal_hash"] == fixture.journal.journal_hash.hex()
    assert saved["aggregate_bonsol_execution"]["execution_id"] == "exec-1"
    assert sorted(p.name for p in fixture.run_path.parent.iterdir()) == [
        f"{fixture.aggregate_key}.json",
        f"{fixture.aggregate_key}.lock",
    ]

    artifact = fixture.ipfs.cat_json(output_cid)
    assert artifact["result"]["scalar_value_bps"] == 5000
    assert artifact["result"]["combiner_id"] == 1
    assert artifact["result"]["branch_count"] == 2
    assert artifact["receipt_binding"] == RECEIPT_BINDING_BONSOL
    assert artifact["bonsol"]["journal_hash"] == fixture.journal.journal_hash.hex()
    assert len(artifact["branch_outputs"]) == 2

    command, payload = seen[0]
    assert command == "hook --production"
    assert payload["aggregate_job"] == str(fixture.aggregate_key)
    assert payload["input_artifact_hex"] == fixture.artifact.hex()
    assert payload["committed_outputs"] == fixture.journal.committed_outputs.hex()
    assert fixture.runner.metrics.counters["bonsol_hook_proved"] == 1
    # The receipt lands before the proof: the callback needs a Completed job.
    assert f"result_hash={fixture.journal.output_digest.hex()}" in caplog.text
    assert fixture.runner.run_once() == 0
    assert len(fixture.protocol.receipts) == 1


def test_runner_waits_until_the_aggregate_job_is_opened(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    del fixture.jobs[fixture.aggregate_key]
    assert fixture.runner.run_once() == 0
    assert fixture.protocol.claims == []


def test_artifact_that_is_not_the_one_the_job_was_opened_against_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fixture = _Fixture(tmp_path)
    fixture.jobs[fixture.aggregate_key].input_bundle_hash = bytes.fromhex("99" * 32)
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.claims == []
    assert fixture.protocol.receipts == []
    assert "input_bundle_hash" in caplog.text


def test_an_artifact_that_departs_from_its_named_plan_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A second party confirms the run did not change its combiner after seeing the answers.

    The aggregate job's hashes are fixed at bind time, which is after every branch
    result is visible. The plan is pinned before any branch runs and its CID rides
    inside the artifact, so the aggregator can check the reduction it is being paid to
    prove is the one the run committed to.
    """

    fixture = _Fixture(tmp_path)
    plan = {
        "combiner": "trimmed-mean",
        "combiner_parameters": {"trim_bps": 2500},
        "branch_jobs": [{"job": str(key)} for key in fixture.branch_keys],
    }
    plan_cid = fixture.ipfs.add_json("aggregate-plan.json", plan)
    document = json.loads(fixture.artifact.decode("utf-8"))
    document["aggregate_plan_cid"] = plan_cid
    artifact = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fixture.ipfs.objects[fixture.input_cid] = artifact
    fixture.jobs[fixture.aggregate_key].input_bundle_hash = framed_input_digest(artifact)
    fixture.jobs[fixture.aggregate_key].expected_result_hash = aggregate_journal(artifact).journal_hash

    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []
    assert "departs from the aggregate plan" in caplog.text


def test_reduction_that_does_not_match_the_expected_journal_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fixture = _Fixture(tmp_path)
    fixture.jobs[fixture.aggregate_key].expected_result_hash = bytes.fromhex("77" * 32)
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []
    assert "expected_result_hash" in caplog.text


def test_a_zero_software_digest_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fixture = _Fixture(tmp_path)
    fixture.jobs[fixture.aggregate_key].required_software_digest = ZERO_HASH
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []
    assert "no Bonsol marker can ever match" in caplog.text


def test_a_receipt_no_branch_job_settled_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The artifact must describe real branch jobs, not just be internally consistent."""

    fixture = _Fixture(tmp_path)
    fixture.jobs[fixture.branch_keys[0]].submitted_result_hash = bytes.fromhex("55" * 32)
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []
    assert "differs from the on-chain submitted_result_hash" in caplog.text


def test_a_branch_job_that_does_not_exist_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fixture = _Fixture(tmp_path)
    del fixture.jobs[fixture.branch_keys[1]]
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []
    assert "does not exist" in caplog.text


def test_hook_failure_leaves_the_receipt_on_chain_and_no_marker(tmp_path: Path) -> None:
    """The receipt has to land first, so a failed proof is a Completed job with no marker."""

    def failing_hook(*args, **kwargs):
        raise BonsolHookError("bonsol execute timed out")

    fixture = _Fixture(tmp_path, hook_runner=failing_hook)
    assert fixture.runner.run_once() == 0
    assert fixture.protocol.claims == [fixture.aggregate_key]
    assert len(fixture.protocol.receipts) == 1
    assert fixture.runner.metrics.counters["bonsol_hook_failed"] == 1
    assert fixture.runner.metrics.counters["aggregate_failed"] == 1
    assert fixture.saved()["aggregate_submitted"] is True


def test_an_execution_that_proved_a_different_claim_is_refused(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    other = bytes([1]) + bytes(72)
    fixture.runner.hook_runner = lambda *args, **kwargs: BonsolBinding(
        "exec-1",
        IMAGE_ID,
        fixture.journal.input_digest,
        hashlib.sha256(other).digest(),
        journal_hash_for(fixture.journal.input_digest, other),
        other,
    )
    assert fixture.runner.run_once() == 0
    assert fixture.runner.metrics.counters["bonsol_hook_failed"] == 1


def test_a_missing_hook_refuses_to_leave_an_unprovable_receipt(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, hook_command="", environ={})
    assert fixture.runner.allow_unproven is False
    assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []


def test_the_unproven_escape_hatch_is_explicit_and_loud(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="kswarm.aggregator_runner"):
        fixture = _Fixture(tmp_path, hook_command="", environ={ALLOW_UNBOUND_ENV: "1"})
        assert fixture.runner.allow_unproven is True
        assert fixture.runner.run_once() == 1
    assert "local-development setting" in caplog.text
    assert "cannot settle" in caplog.text
    _, output_cid, result_bytes = fixture.protocol.receipts[0]
    assert result_bytes == fixture.journal.committed_outputs
    assert fixture.ipfs.cat_json(output_cid)["receipt_binding"] == RECEIPT_BINDING_UNPROVEN
    assert fixture.runner.metrics.counters["aggregate_unproven"] == 1


@pytest.mark.parametrize("cluster", ["devnet", "mainnet"])
def test_the_unproven_escape_hatch_is_refused_on_a_real_cluster(tmp_path: Path, cluster: str) -> None:
    ipfs = FakeIpfs()
    with pytest.raises(AggregationError, match="is refused on cluster"):
        AggregatorRunner(
            make_config(keypair_name="aggregator", cluster=cluster),
            protocol=FakeProtocol({}),
            ipfs=ipfs,
            store=RunStore(tmp_path),
            hook_command="",
            environ={ALLOW_UNBOUND_ENV: "1"},
        )


def test_trimmed_mean_artifact_reduces_with_its_committed_parameters(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, combiner="trimmed-mean", scalars=(4000, 6000))
    assert fixture.runner.run_once() == 1
    result = fixture.ipfs.cat_json(fixture.protocol.receipts[0][1])["result"]
    # Two branches, trim 50% -> one outlier: the lower median is 6000, so 4000 is dropped.
    assert result["scalar_value_bps"] == 6000
    assert result["combiner_parameters"]["trim_bps"] == 5000


def test_runner_resumes_its_own_claim_without_claiming_again(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    job = fixture.jobs[fixture.aggregate_key]
    job.status = JOB_STATUS_BY_NAME["claimed"]
    job.worker = fixture.protocol.wallet.pubkey
    assert fixture.runner.run_once() == 1
    assert fixture.protocol.claims == []
    assert fixture.protocol.receipts[0][0] == fixture.aggregate_key
    assert fixture.saved()["aggregate_submitted"] is True


def test_runner_skips_a_run_locked_by_another_runner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fixture = _Fixture(tmp_path)
    lock_path = fixture.run_path.with_name(f"{fixture.run_path.stem}.lock")
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with caplog.at_level(logging.INFO, logger="kswarm.aggregator_runner"):
            assert fixture.runner.run_once() == 0
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert fixture.protocol.claims == []
    assert "locked by another runner" in caplog.text
    assert fixture.runner.run_once() == 1


def test_hook_output_is_validated_field_by_field(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    journal = fixture.journal
    assert parse_hook_output(_hook_json(journal)) == _binding("exec-1", journal)
    wrong_output_digest = _hook_json(journal, output_digest="00" * 32)
    wrong_journal = _hook_json(journal, journal_hash="00" * 32)
    for broken in (
        "not json",
        "[]",
        _hook_json(journal, execution_id=None),
        _hook_json(journal, execution_id=""),
        _hook_json(journal, execution_id="x" * 33),
        _hook_json(journal, image_id="11" * 31),
        _hook_json(journal, image_id="zz" * 32),
        _hook_json(journal, journal_hash=None),
        _hook_json(journal, committed_outputs=None),
        _hook_json(journal, committed_outputs=""),
        _hook_json(journal, committed_outputs="ab" * 513),
        wrong_output_digest,
        wrong_journal,
    ):
        with pytest.raises(BonsolHookError):
            parse_hook_output(broken)
    with pytest.raises(BonsolHookError, match="output_digest is not sha256"):
        parse_hook_output(wrong_output_digest)
    with pytest.raises(BonsolHookError, match="journal_hash is not"):
        parse_hook_output(wrong_journal)


def test_binding_is_checked_against_the_aggregate_job(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    binding = AggregateBinding(artifact=fixture.artifact, journal=fixture.journal)
    execution = _binding("exec-1", fixture.journal)
    job = fixture.jobs[fixture.aggregate_key]
    check_binding_against_job(execution, job, binding)

    with pytest.raises(BonsolHookError, match="input_bundle_hash"):
        check_binding_against_job(execution, FakeJob(input_bundle_hash=bytes(32), required_software_digest=IMAGE_ID, expected_result_hash=fixture.journal.journal_hash), binding)
    with pytest.raises(BonsolHookError, match="required_software_digest"):
        check_binding_against_job(execution, FakeJob(input_bundle_hash=fixture.journal.input_digest, required_software_digest=bytes.fromhex("ff" * 32), expected_result_hash=fixture.journal.journal_hash), binding)
    with pytest.raises(BonsolHookError, match="expected_result_hash"):
        check_binding_against_job(execution, FakeJob(input_bundle_hash=fixture.journal.input_digest, required_software_digest=IMAGE_ID, expected_result_hash=bytes.fromhex("ff" * 32)), binding)


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_run_bonsol_hook_runs_the_command_with_the_payload(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path)
    expected = _binding("exec-1", fixture.journal)
    (tmp_path / "hook.json").write_text(_hook_json(fixture.journal), encoding="utf-8")
    good = _script(tmp_path, "good.sh", 'cat "$(dirname "$0")/hook.json"\n')
    assert run_bonsol_hook(good, {"run": "r"}, cwd=tmp_path, timeout_seconds=10) == expected
    echo = _script(tmp_path, "echo.sh", 'for arg in "$@"; do last="$arg"; done\nprintf \'%s\' "$last" > "$(dirname "$0")/payload.json"\ncat "$(dirname "$0")/hook.json"\n')
    run_bonsol_hook(echo + " --flag", {"run": "r", "result": {"x": 1}}, cwd=tmp_path, timeout_seconds=10)
    assert json.loads((tmp_path / "payload.json").read_text()) == {"run": "r", "result": {"x": 1}}
    failing = _script(tmp_path, "fail.sh", "echo boom >&2; exit 3\n")
    with pytest.raises(BonsolHookError, match="exited 3: boom"):
        run_bonsol_hook(failing, {"run": "r"}, cwd=tmp_path, timeout_seconds=10)
    with pytest.raises(BonsolHookError, match="did not run"):
        run_bonsol_hook(str(tmp_path / "missing.sh"), {"run": "r"}, cwd=tmp_path, timeout_seconds=10)
    with pytest.raises(BonsolHookError, match="empty"):
        run_bonsol_hook("   ", {"run": "r"}, cwd=tmp_path, timeout_seconds=10)


def test_run_store_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    path = tmp_path / "run.json"
    store.save(path, {"a": 1})
    store.save(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]
    assert store.paths() == [path]
    (tmp_path / ".run.json.abc.tmp").write_text("{}")
    assert store.paths() == [path]
    os.chmod(tmp_path, 0o500)
    try:
        with pytest.raises(OSError):
            store.save(path, {"a": 3})
    finally:
        os.chmod(tmp_path, 0o700)
    assert json.loads(path.read_text()) == {"a": 2}


def test_a_branch_that_did_not_settle_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A slashed or cancelled branch still has receipt bytes on chain."""

    fixture = _Fixture(tmp_path)
    fixture.jobs[fixture.branch_keys[0]].status = JOB_STATUS_BY_NAME["slashed"]
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert fixture.runner.run_once() == 0
    assert fixture.protocol.receipts == []
    assert "needs every branch settled" in caplog.text


def test_allow_completed_branches_accepts_an_attested_branch(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path, allow_completed_branches=True)
    branch = fixture.jobs[fixture.branch_keys[0]]
    branch.status = JOB_STATUS_BY_NAME["submitted"]
    branch.verifier_attestation_hash = branch.submitted_result_hash
    assert fixture.runner.run_once() == 1
    assert len(fixture.protocol.receipts) == 1
