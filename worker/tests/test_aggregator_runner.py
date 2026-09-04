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
from aggregator_runner.combiners import CombinerError
from aggregator_runner.run_store import RunStore
from aggregator_runner.runner import (
    RECEIPT_BINDING_BONSOL,
    RECEIPT_BINDING_CANONICAL,
    RESULT_MAGIC,
    AggregationError,
    AggregatorRunner,
    aggregate_result_bytes,
    check_binding_against_job,
    combine,
)
from app.protocol.branch_schemas import BranchOutput
from app.protocol.canonical_hash import canonical_json_bytes
from fakes import FakeIpfs, FakeJob, FakeProtocol, make_config
from kswarm_cli.constants import JOB_CLASS, JOB_STATUS_BY_NAME, ZERO_HASH


COMMITTED = bytes.fromhex("ab" * 32) + (3).to_bytes(4, "little") + (17).to_bytes(4, "little") + b"\x2a"
INPUT_DIGEST = bytes.fromhex("22" * 32)
IMAGE_ID = bytes.fromhex("11" * 32)
BINDING = BonsolBinding(
    "exec-1",
    IMAGE_ID,
    INPUT_DIGEST,
    hashlib.sha256(COMMITTED).digest(),
    journal_hash_for(INPUT_DIGEST, COMMITTED),
    COMMITTED,
)


def _hook_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "execution_id": "exec-1",
        "image_id": "0x" + IMAGE_ID.hex(),
        "input_digest": INPUT_DIGEST.hex(),
        "output_digest": BINDING.output_digest.hex(),
        "journal_hash": BINDING.journal_hash.hex(),
        "committed_outputs": COMMITTED.hex(),
    }
    payload.update(overrides)
    return json.dumps({key: value for key, value in payload.items() if value is not None})


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


def _run(combiner: str, branch_keys: list[Pubkey], aggregate_key: Pubkey, **manifest_extra) -> dict:
    manifest = {"question": "q", "combiner": combiner, "output_schema_hash": "b" * 64, "output_schema": {"output_kind": "scalar"}}
    manifest.update(manifest_extra)
    return {
        "schema_version": 1,
        "parent_run": str(aggregate_key),
        "aggregate_job": str(aggregate_key),
        "combiner": combiner,
        "parent_manifest": manifest,
        "branch_jobs": [{"branch_index": index, "job": str(key)} for index, key in enumerate(branch_keys)],
        "aggregate_submitted": False,
    }


def test_combine_dispatches_on_the_manifest_combiner() -> None:
    aggregate = Pubkey.new_unique()
    outputs = [_output(0, scalar=1), _output(1, scalar=2), _output(2, scalar=2)]
    weighted = combine(_run("weighted-mean", [], aggregate), outputs)
    assert (weighted.combiner_id, weighted.scalar_value_bps, weighted.combiner_parameters) == (1, 2, {"weights": "uniform"})

    trimmed_outputs = [_output(index, scalar=value) for index, value in enumerate([7000, 1000, 9000, 5000, 5100])]
    trimmed = combine(_run("trimmed-mean", [], aggregate, combiner_parameters={"trim_bps": 4000}), trimmed_outputs)
    assert (trimmed.combiner_id, trimmed.scalar_value_bps, trimmed.rejected_count) == (2, 5700, 2)
    assert trimmed.combiner_parameters == {"trim_bps": 4000, "outlier_count": 2}

    votes = [_output(0, label=1), _output(1, label=0), _output(2, label=1)]
    majority = combine(_run("majority-vote", [], aggregate, output_schema={"category_dictionary": ["no", "yes"]}), votes)
    assert (majority.combiner_id, majority.categorical_label_index, majority.scalar_value_bps) == (3, 1, None)
    assert majority.branch_count == 3
    assert majority.output_schema_hash == "b" * 64


def test_combine_fails_closed() -> None:
    aggregate = Pubkey.new_unique()
    scalars = [_output(0, scalar=1000), _output(1, scalar=3000)]
    with pytest.raises(CombinerError, match="UnknownCombiner"):
        combine(_run("median", [], aggregate), scalars)
    with pytest.raises(AggregationError, match="trim_bps"):
        combine(_run("trimmed-mean", [], aggregate), scalars)
    with pytest.raises(AggregationError, match="weights"):
        combine(_run("weighted-mean", [], aggregate, combiner_parameters={"weights": [1, 2]}), scalars)
    with pytest.raises(AggregationError, match="category_dictionary"):
        combine(_run("majority-vote", [], aggregate), [_output(0, label=0)])
    with pytest.raises(AggregationError, match="outside the committed category dictionary"):
        combine(_run("majority-vote", [], aggregate, output_schema={"category_dictionary": ["a", "b"]}), [_output(0, label=2)])
    with pytest.raises(AggregationError, match="no scalar value"):
        combine(_run("weighted-mean", [], aggregate), [_output(0, label=0)])
    with pytest.raises(AggregationError, match="no categorical label"):
        combine(_run("majority-vote", [], aggregate, output_schema={"category_dictionary": ["a", "b"]}), scalars)


def test_aggregate_result_bytes_are_canonical_mfa2() -> None:
    aggregate = Pubkey.new_unique()
    result = combine(_run("weighted-mean", [], aggregate), [_output(0, scalar=4000), _output(1, scalar=6000)])
    unbound = aggregate_result_bytes(result)
    decoded = json.loads(unbound.decode("utf-8"))
    assert decoded["schema"] == "MFA2"
    assert decoded["scalar_value_bps"] == 5000
    assert decoded["bonsol"] is None
    bound = aggregate_result_bytes(result.with_bonsol(BINDING))
    payload = canonical_json_bytes({"schema": "MFA2", **result.with_bonsol(BINDING).to_json()})
    assert len(payload) > 512
    assert bound == RESULT_MAGIC + hashlib.sha256(payload).digest()


def test_hook_output_is_validated_field_by_field() -> None:
    assert parse_hook_output(_hook_json()) == BINDING
    wrong_output_digest = _hook_json(output_digest="00" * 32)
    wrong_journal = _hook_json(journal_hash="00" * 32)
    for broken in (
        "not json",
        "[]",
        _hook_json(execution_id=None),
        _hook_json(execution_id=""),
        _hook_json(execution_id="x" * 33),
        _hook_json(image_id="11" * 31),
        _hook_json(image_id="zz" * 32),
        _hook_json(journal_hash=None),
        _hook_json(committed_outputs=None),
        _hook_json(committed_outputs=""),
        _hook_json(committed_outputs="ab" * 513),
        wrong_output_digest,
        wrong_journal,
    ):
        with pytest.raises(BonsolHookError):
            parse_hook_output(broken)
    with pytest.raises(BonsolHookError, match="output_digest is not sha256"):
        parse_hook_output(wrong_output_digest)
    with pytest.raises(BonsolHookError, match="journal_hash is not"):
        parse_hook_output(wrong_journal)


def test_binding_is_checked_against_the_aggregate_job() -> None:
    job = FakeJob(job_class=JOB_CLASS["aggregate-proof"], input_bundle_hash=INPUT_DIGEST, required_software_digest=IMAGE_ID, expected_result_hash=BINDING.journal_hash)
    check_binding_against_job(BINDING, job)
    check_binding_against_job(BINDING, FakeJob(input_bundle_hash=INPUT_DIGEST, required_software_digest=ZERO_HASH, expected_result_hash=ZERO_HASH))
    with pytest.raises(BonsolHookError, match="input_bundle_hash"):
        check_binding_against_job(BINDING, FakeJob(input_bundle_hash=bytes(32), required_software_digest=IMAGE_ID, expected_result_hash=BINDING.journal_hash))
    with pytest.raises(BonsolHookError, match="required_software_digest"):
        check_binding_against_job(BINDING, FakeJob(input_bundle_hash=INPUT_DIGEST, required_software_digest=bytes.fromhex("ff" * 32), expected_result_hash=BINDING.journal_hash))
    with pytest.raises(BonsolHookError, match="expected_result_hash"):
        check_binding_against_job(BINDING, FakeJob(input_bundle_hash=INPUT_DIGEST, required_software_digest=IMAGE_ID, expected_result_hash=bytes.fromhex("ff" * 32)))


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_run_bonsol_hook_runs_the_command_with_the_payload(tmp_path: Path) -> None:
    (tmp_path / "hook.json").write_text(_hook_json(), encoding="utf-8")
    good = _script(tmp_path, "good.sh", 'cat "$(dirname "$0")/hook.json"\n')
    assert run_bonsol_hook(good, {"run": "r"}, cwd=tmp_path, timeout_seconds=10) == BINDING
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


def _runner(
    tmp_path: Path,
    *,
    combiner: str = "weighted-mean",
    hook_command: str = "",
    hook_runner=None,
    aggregate_status: int = JOB_STATUS_BY_NAME["open"],
    branch_status: int = JOB_STATUS_BY_NAME["settled"],
    aggregate_input_bundle_hash: bytes = INPUT_DIGEST,
    **manifest_extra,
):
    ipfs = FakeIpfs()
    branch_keys = [Pubkey.new_unique(), Pubkey.new_unique()]
    aggregate_key = Pubkey.new_unique()
    jobs = {}
    for index, key in enumerate(branch_keys):
        output = _output(index, scalar=4000 + 2000 * index)
        jobs[key] = FakeJob(status=branch_status, output_cid=ipfs.add_json("out", output.model_dump(mode="json", exclude_none=False)))
    jobs[aggregate_key] = FakeJob(
        status=aggregate_status,
        job_class=JOB_CLASS["aggregate-proof"],
        execute_deadline=5000,
        input_bundle_hash=aggregate_input_bundle_hash,
        required_software_digest=IMAGE_ID,
        expected_result_hash=BINDING.journal_hash,
    )
    protocol = FakeProtocol(jobs)
    runs_dir = tmp_path / "predict_runs"
    runs_dir.mkdir()
    run_path = runs_dir / f"{aggregate_key}.json"
    run_path.write_text(json.dumps(_run(combiner, branch_keys, aggregate_key, **manifest_extra)), encoding="utf-8")
    runner = AggregatorRunner(
        make_config(keypair_name="aggregator"),
        protocol=protocol,
        ipfs=ipfs,
        store=RunStore(runs_dir),
        hook_command=hook_command,
        hook_runner=hook_runner or (lambda *args, **kwargs: BINDING),
        clock=lambda: 2_000.0,
    )
    return runner, protocol, ipfs, run_path, aggregate_key


def test_runner_binds_the_bonsol_outputs_and_persists_atomically(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    seen_payloads: list[dict] = []

    def hook(command: str, payload: dict, *, cwd: Path, timeout_seconds: float) -> BonsolBinding:
        seen_payloads.append(payload)
        assert command == "hook --production"
        return BINDING

    runner, protocol, ipfs, run_path, aggregate_key = _runner(tmp_path, hook_command="hook --production", hook_runner=hook)
    with caplog.at_level(logging.INFO, logger="kswarm.aggregator_runner"):
        assert runner.run_once() == 1

    assert protocol.claims == [aggregate_key]
    job_key, output_cid, result_bytes = protocol.receipts[0]
    assert job_key == aggregate_key
    assert result_bytes == COMMITTED
    assert protocol.jobs_by_key[aggregate_key].submitted_result_hash == BINDING.output_digest
    saved = json.loads(run_path.read_text(encoding="utf-8"))
    assert saved["aggregate_submitted"] is True
    assert saved["aggregate_output_cid"] == output_cid
    assert saved["aggregate_result_hash"] == BINDING.output_digest.hex()
    assert saved["aggregate_claimed_at_unix"] == 2000
    assert sorted(p.name for p in run_path.parent.iterdir()) == [f"{aggregate_key}.json", f"{aggregate_key}.lock"]
    artifact = ipfs.cat_json(output_cid)
    assert artifact["result"]["scalar_value_bps"] == 5000
    assert artifact["result"]["combiner_id"] == 1
    assert artifact["result"]["bonsol"] == BINDING.to_json()
    assert artifact["receipt_binding"] == RECEIPT_BINDING_BONSOL
    assert artifact["result_hash"] == BINDING.output_digest.hex()
    assert seen_payloads[0]["aggregate_job"] == str(aggregate_key)
    assert seen_payloads[0]["result"]["scalar_value_bps"] == 5000
    assert len(seen_payloads[0]["aggregate_result_sha256"]) == 64
    assert runner.metrics.counters["bonsol_hook_bound"] == 1
    assert f"result_hash={BINDING.output_digest.hex()}" in caplog.text
    assert runner.run_once() == 0
    assert len(protocol.receipts) == 1


def test_hook_failure_fails_the_aggregation_before_any_claim(tmp_path: Path) -> None:
    def failing_hook(*args, **kwargs):
        raise BonsolHookError("harness exited 1")

    runner, protocol, _, run_path, _ = _runner(tmp_path, hook_command="hook", hook_runner=failing_hook)
    before = run_path.read_text(encoding="utf-8")

    assert runner.run_once() == 0
    assert protocol.claims == []
    assert protocol.receipts == []
    assert run_path.read_text(encoding="utf-8") == before
    assert runner.metrics.counters["bonsol_hook_failed"] == 1
    assert runner.metrics.counters["aggregate_failed"] == 1


def test_binding_that_cannot_settle_is_rejected_before_any_claim(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runner, protocol, _, _, _ = _runner(tmp_path, hook_command="hook", aggregate_input_bundle_hash=bytes.fromhex("99" * 32))
    with caplog.at_level(logging.ERROR, logger="kswarm.aggregator_runner"):
        assert runner.run_once() == 0
    assert protocol.claims == []
    assert runner.metrics.counters["bonsol_hook_failed"] == 1
    assert "input_bundle_hash" in caplog.text


def test_missing_hook_is_a_loud_unbound_receipt(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runner, protocol, ipfs, _, _ = _runner(tmp_path, hook_command="")
    with caplog.at_level(logging.WARNING, logger="kswarm.aggregator_runner"):
        assert runner.run_once() == 1
    assert "carries no Bonsol binding" in caplog.text
    _, output_cid, result_bytes = protocol.receipts[0]
    artifact = ipfs.cat_json(output_cid)
    assert artifact["result"]["bonsol"] is None
    assert artifact["receipt_binding"] == RECEIPT_BINDING_CANONICAL
    assert json.loads(result_bytes.decode("utf-8"))["schema"] == "MFA2"


def test_unknown_combiner_submits_nothing(tmp_path: Path) -> None:
    runner, protocol, _, _, _ = _runner(tmp_path, combiner="median")
    assert runner.run_once() == 0
    assert protocol.claims == []
    assert runner.metrics.counters["aggregate_failed"] == 1


def test_trimmed_mean_uses_the_manifest_trim_parameter(tmp_path: Path) -> None:
    runner, protocol, ipfs, _, _ = _runner(tmp_path, combiner="trimmed-mean", combiner_parameters={"trim_bps": 5000})
    assert runner.run_once() == 1
    result = ipfs.cat_json(protocol.receipts[0][1])["result"]
    # Two branches (4000, 6000), trim 50% -> one outlier: the lower median is 6000, so 4000 is dropped.
    assert (result["scalar_value_bps"], result["rejected_count"], result["combiner_parameters"]) == (6000, 1, {"trim_bps": 5000, "outlier_count": 1})


def test_runner_waits_for_unready_branches(tmp_path: Path) -> None:
    runner, protocol, _, _, _ = _runner(tmp_path, branch_status=JOB_STATUS_BY_NAME["completed"])
    assert runner.run_once() == 0
    assert protocol.claims == []


def test_runner_resumes_its_own_claim_without_claiming_again(tmp_path: Path) -> None:
    runner, protocol, _, run_path, aggregate_key = _runner(tmp_path, aggregate_status=JOB_STATUS_BY_NAME["claimed"])
    protocol.jobs_by_key[aggregate_key].worker = protocol.wallet.pubkey
    assert runner.run_once() == 1
    assert protocol.claims == []
    assert protocol.receipts[0][0] == aggregate_key
    assert json.loads(run_path.read_text())["aggregate_submitted"] is True


def test_runner_skips_a_run_locked_by_another_runner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runner, protocol, _, run_path, _ = _runner(tmp_path)
    lock_path = run_path.with_name(f"{run_path.stem}.lock")
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with caplog.at_level(logging.INFO, logger="kswarm.aggregator_runner"):
            assert runner.run_once() == 0
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert protocol.claims == []
    assert "locked by another runner" in caplog.text
    assert runner.run_once() == 1


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
