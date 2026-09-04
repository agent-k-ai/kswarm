from __future__ import annotations

import hashlib
import logging

import pytest
from solders.pubkey import Pubkey

from app.protocol.branch_schemas import BranchInput, BranchOutput
from app.protocol.canonical_hash import branch_output_result_bytes
from branch_worker.executor import BranchExecutor, LlmEndpointError
from fakes import FakeIpfs, FakeJob, FakeProtocol, StubLlmClient, make_config
from kswarm_cli.constants import JOB_STATUS_BY_NAME
from verifier_worker.daemon import HASH_ONLY_WARNING, VerifierWorkerDaemon


HONEST_ANSWER = {"scalar_value": 0.73, "confidence_lower": 0.6, "confidence_upper": 0.8, "rationale": "the evidence points to yes"}


def _branch_input() -> BranchInput:
    return BranchInput(parent_job="parent-run", branch_index=3, seed="Will it happen?", parameters={}, rng_seed=99, target_output_kind="scalar")


def _executor(ipfs: FakeIpfs, scripted: list, *, model: str = "stub-model") -> BranchExecutor:
    # `zkvm_host=""` so these tests never shell out to a prover: the receipts under test
    # are constructed directly, and a verifier re-execution must not prove one anyway.
    return BranchExecutor(ipfs, llm_base_url="http://llm.test/v1", llm_model_name=model, client=StubLlmClient(scripted), zkvm_host="")


def _lying_output(ipfs: FakeIpfs, executor: BranchExecutor, branch_input: BranchInput) -> BranchOutput:
    """A worker that never called the model but copied every honest-looking metadata field."""

    fake_transcript_cid = ipfs.add_json("fake-transcript.json", {"note": "no model was called"})
    return BranchOutput(
        parent_job=branch_input.parent_job,
        branch_index=branch_input.branch_index,
        output_kind="scalar",
        scalar_value_bps=5000,
        rng_seed=branch_input.rng_seed,
        llm_model=executor.llm_model_name,
        llm_version_hash=executor.llm_version_hash(branch_input),
        completed_at_unix=1_000,
        transcript_cid=fake_transcript_cid,
    )


def _completed_job(ipfs: FakeIpfs, branch_input: BranchInput, output: BranchOutput) -> tuple[Pubkey, FakeJob]:
    job = FakeJob(
        status=JOB_STATUS_BY_NAME["completed"],
        worker=Pubkey.new_unique(),
        input_cid=ipfs.add_bytes("input", branch_input.model_dump_json().encode("utf-8")),
        output_cid=ipfs.add_json("output", output.model_dump(mode="json", exclude_none=False)),
        submitted_result_hash=hashlib.sha256(branch_output_result_bytes(output)).digest(),
    )
    return Pubkey.new_unique(), job


def _daemon(ipfs: FakeIpfs, jobs: dict, *, executor: BranchExecutor | None, assign_verifier: bool = True, **config_overrides):
    """A daemon over `jobs`. By default this verifier is the one assigned to every job,
    which is what the program requires before it will accept a challenge."""

    config = make_config(keypair_name="verifier", **config_overrides)
    protocol = FakeProtocol(jobs)
    if assign_verifier:
        for job in jobs.values():
            job.assigned_verifier_authority = protocol.wallet.pubkey
    daemon = VerifierWorkerDaemon(config, protocol=protocol, ipfs=ipfs, executor=executor, clock=lambda: 1_000.0)
    return daemon, protocol


def test_lying_worker_is_caught_by_reexecution_and_challenged(caplog: pytest.LogCaptureFixture) -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    verifier_executor = _executor(ipfs, [HONEST_ANSWER])
    lie = _lying_output(ipfs, verifier_executor, branch_input)
    job_key, job = _completed_job(ipfs, branch_input, lie)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=verifier_executor)

    with caplog.at_level(logging.INFO, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 1

    attested_key, verifier_bytes, evidence_cid, _ = protocol.attestations[0]
    verifier_hash = hashlib.sha256(verifier_bytes).digest()
    assert attested_key == job_key
    assert verifier_hash != job.submitted_result_hash
    assert job.verifier_attestation_hash == verifier_hash
    assert protocol.challenges == [job_key]
    assert job.status == JOB_STATUS_BY_NAME["slashed"]
    evidence = ipfs.cat_json(evidence_cid)
    assert evidence["mode"] == "reexecute"
    assert evidence["matched"] is False
    assert evidence["worker_output"]["scalar_value_bps"] == 5000
    assert evidence["verifier_output"]["scalar_value_bps"] == 7300
    assert evidence["verifier_output"]["transcript_cid"] == lie.transcript_cid
    assert evidence["verifier_transcript"]["raw_response"] == HONEST_ANSWER
    assert "matched=False" in caplog.text
    assert daemon.metrics.counters["mismatches"] == 1
    assert daemon.metrics.counters["challenges_submitted"] == 1


def test_unassigned_verifier_still_attests_and_says_why_it_cannot_challenge(caplog: pytest.LogCaptureFixture) -> None:
    """Only the assigned verifier may challenge; an unassigned one must still attest.

    The attestation is what makes a lying worker's receipt challengeable, so it must
    land even when this verifier cannot follow it with a challenge. Before this was
    handled where it happens, the rejection escaped `_challenge`, was caught by
    `run_once` as an attestation race, and the operator was told the attestation had
    been skipped when it had in fact succeeded.
    """

    ipfs = FakeIpfs()
    branch_input = _branch_input()
    verifier_executor = _executor(ipfs, [HONEST_ANSWER])
    lie = _lying_output(ipfs, verifier_executor, branch_input)
    job_key, job = _completed_job(ipfs, branch_input, lie)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=verifier_executor, assign_verifier=False)

    with caplog.at_level(logging.INFO, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 1

    assert job.verifier_attestation_hash is not None
    assert job.verifier_attestation_hash != job.submitted_result_hash
    assert protocol.challenges == []
    assert job.status == JOB_STATUS_BY_NAME["completed"]
    assert daemon.metrics.counters["mismatches"] == 1
    assert daemon.metrics.counters["challenges_not_assigned"] == 1
    assert "challenges_submitted" not in daemon.metrics.counters
    assert "attestation_races" not in daemon.metrics.counters
    assert "challenge refused" in caplog.text
    assert "ChallengeRequiresAssignedVerifier" in caplog.text
    assert "assign_verifier" in caplog.text
    assert "attestation skipped" not in caplog.text


def test_verifier_assigned_to_someone_else_cannot_challenge(caplog: pytest.LogCaptureFixture) -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    verifier_executor = _executor(ipfs, [HONEST_ANSWER])
    lie = _lying_output(ipfs, verifier_executor, branch_input)
    job_key, job = _completed_job(ipfs, branch_input, lie)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=verifier_executor, assign_verifier=False)
    job.assigned_verifier_authority = Pubkey.new_unique()

    with caplog.at_level(logging.INFO, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 1

    assert protocol.challenges == []
    assert daemon.metrics.counters["challenges_not_assigned"] == 1
    assert "VerifierNotAssigned" in caplog.text


def test_honest_worker_matches_and_is_not_challenged() -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    worker_execution = _executor(ipfs, [HONEST_ANSWER]).execute("job", branch_input)
    job_key, job = _completed_job(ipfs, branch_input, worker_execution.output)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))

    assert daemon.run_once() == 1
    assert job.verifier_attestation_hash == job.submitted_result_hash
    assert protocol.challenges == []
    assert ipfs.cat_json(protocol.attestations[0][2])["matched"] is True


def test_challenge_can_be_disabled_while_the_mismatch_is_still_attested() -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    verifier_executor = _executor(ipfs, [HONEST_ANSWER])
    job_key, job = _completed_job(ipfs, branch_input, _lying_output(ipfs, verifier_executor, branch_input))
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=verifier_executor, challenge_on_mismatch=False)

    assert daemon.run_once() == 1
    assert job.verifier_attestation_hash != job.submitted_result_hash
    assert protocol.challenges == []


def test_worker_claiming_another_model_is_skipped_not_attested(caplog: pytest.LogCaptureFixture) -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    other_model = _executor(ipfs, [], model="other-model")
    job_key, job = _completed_job(ipfs, branch_input, _lying_output(ipfs, other_model, branch_input))
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))

    with caplog.at_level(logging.WARNING, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 0
    assert protocol.attestations == []
    assert daemon.metrics.counters["reexecution_model_mismatches"] == 1
    assert "worker claims model 'other-model'" in caplog.text


def test_hash_only_mode_cannot_catch_the_lie_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    lie = _lying_output(ipfs, _executor(ipfs, []), branch_input)
    job_key, job = _completed_job(ipfs, branch_input, lie)
    with caplog.at_level(logging.WARNING, logger="kswarm.verifier_worker"):
        daemon, protocol = _daemon(ipfs, {job_key: job}, executor=None, verifier_reexecute=False)
        assert daemon.executor is None
        assert daemon.run_once() == 1

    assert HASH_ONLY_WARNING in caplog.text
    assert job.verifier_attestation_hash == job.submitted_result_hash
    assert protocol.challenges == []
    evidence = ipfs.cat_json(protocol.attestations[0][2])
    assert evidence["mode"] == "hash-only"
    assert evidence["matched"] is True


def test_llm_outage_leaves_the_job_unattested_for_a_later_pass(caplog: pytest.LogCaptureFixture) -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    executor = _executor(ipfs, [LlmEndpointError("down", retryable=True)])
    job_key, job = _completed_job(ipfs, branch_input, _lying_output(ipfs, executor, branch_input))
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=executor)

    with caplog.at_level(logging.WARNING, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 0
    assert protocol.attestations == []
    assert job.verifier_authority is None
    assert daemon.metrics.counters["reexecution_failures"] == 1
    assert "no attestation submitted" in caplog.text


def test_closed_window_own_job_and_attested_jobs_are_skipped() -> None:
    ipfs = FakeIpfs()
    branch_input = _branch_input()
    executor = _executor(ipfs, [HONEST_ANSWER, HONEST_ANSWER, HONEST_ANSWER])
    lie = _lying_output(ipfs, executor, branch_input)
    closed_key, closed = _completed_job(ipfs, branch_input, lie)
    closed.challenge_deadline = 999
    attested_key, attested = _completed_job(ipfs, branch_input, lie)
    attested.verifier_authority = Pubkey.new_unique()
    own_key, own = _completed_job(ipfs, branch_input, lie)
    daemon, protocol = _daemon(ipfs, {closed_key: closed, attested_key: attested, own_key: own}, executor=executor)
    own.worker = protocol.wallet.pubkey

    assert daemon.run_once() == 0
    assert protocol.attestations == []


# --- aggregate jobs -------------------------------------------------------------
#
# The verifier attests to aggregate-proof jobs too. It does not run the guest: it
# re-reduces the committed artifact with the Python mirror and attests to the outputs
# the guest would commit, which is what `settle_aggregate_proof_job` compares against
# the receipt. A receipt that does not match its own artifact becomes challengeable.


def _aggregate_fixture(ipfs: FakeIpfs, *, scalars=(4000, 6000)) -> tuple[dict, Pubkey, bytes, object]:
    from kswarm_cli.aggregate import aggregate_journal, build_aggregate_artifact

    jobs: dict[Pubkey, FakeJob] = {}
    branches = []
    for index, scalar in enumerate(scalars):
        output = BranchOutput(
            parent_job="parent-run",
            branch_index=index,
            output_kind="scalar",
            scalar_value_bps=scalar,
            rng_seed=index,
            llm_model="stub-model",
            llm_version_hash="a" * 64,
            completed_at_unix=1,
            transcript_cid="bafktranscript",
        )
        result_bytes = branch_output_result_bytes(output)
        key = Pubkey.new_unique()
        jobs[key] = FakeJob(
            status=JOB_STATUS_BY_NAME["settled"],
            result_bytes=result_bytes,
            submitted_result_hash=hashlib.sha256(result_bytes).digest(),
        )
        branches.append(
            {
                "branch_index": index,
                "job": str(key),
                "output_cid": ipfs.add_json("out", output.model_dump(mode="json", exclude_none=False)),
                "result_bytes": result_bytes.hex(),
                "weight": 1,
            }
        )
    aggregate_key = Pubkey.new_unique()
    artifact = build_aggregate_artifact(
        parent_run=str(aggregate_key),
        parent_manifest_cid="bafyparent",
        output_schema_hash="b" * 64,
        combiner="weighted-mean",
        combiner_parameters={},
        branches=branches,
    )
    journal = aggregate_journal(artifact)
    jobs[aggregate_key] = FakeJob(
        status=JOB_STATUS_BY_NAME["completed"],
        job_class=4,  # aggregate-proof
        worker=Pubkey.new_unique(),
        input_cid=ipfs.add_bytes("aggregate-input.json", artifact),
        input_bundle_hash=journal.input_digest,
        expected_result_hash=journal.journal_hash,
        result_bytes=journal.committed_outputs,
        submitted_result_hash=journal.output_digest,
    )
    return jobs, aggregate_key, artifact, journal


def test_aggregate_receipt_that_matches_its_artifact_is_attested() -> None:
    ipfs = FakeIpfs()
    jobs, aggregate_key, _, journal = _aggregate_fixture(ipfs)
    jobs[aggregate_key].required_software_digest = bytes.fromhex("77" * 32)
    daemon, protocol = _daemon(ipfs, jobs, executor=None, verifier_reexecute=False)

    assert daemon.run_once() == 1
    job_key, result_bytes, evidence_cid, software_digest = protocol.attestations[0]
    # `submit_verifier_attestation` refuses any other digest on a job that names one.
    assert software_digest == bytes.fromhex("77" * 32)
    assert job_key == aggregate_key
    assert result_bytes == journal.committed_outputs
    # The program settles only when the attestation hash equals the receipt hash.
    assert jobs[aggregate_key].verifier_attestation_hash == jobs[aggregate_key].submitted_result_hash
    evidence = ipfs.cat_json(evidence_cid)
    assert evidence["matched"] is True
    assert evidence["validation_errors"] == []
    assert evidence["reduction"]["result_value"] == 5000
    assert protocol.challenges == []


def test_aggregate_receipt_that_is_not_the_reduction_is_attested_against_and_challenged(caplog: pytest.LogCaptureFixture) -> None:
    ipfs = FakeIpfs()
    jobs, aggregate_key, _, journal = _aggregate_fixture(ipfs)
    # A worker that submitted committed outputs claiming a different result value.
    forged = bytearray(journal.committed_outputs)
    forged[33:37] = (9999).to_bytes(4, "little")
    jobs[aggregate_key].result_bytes = bytes(forged)
    jobs[aggregate_key].submitted_result_hash = hashlib.sha256(bytes(forged)).digest()
    daemon, protocol = _daemon(ipfs, jobs, executor=None, verifier_reexecute=False)

    with caplog.at_level(logging.WARNING, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 1
    _, result_bytes, evidence_cid, _ = protocol.attestations[0]
    assert result_bytes == journal.committed_outputs
    assert jobs[aggregate_key].verifier_attestation_hash != jobs[aggregate_key].submitted_result_hash
    evidence = ipfs.cat_json(evidence_cid)
    assert evidence["matched"] is False
    assert any("submitted_result_hash" in error for error in evidence["validation_errors"])
    assert protocol.challenges == [aggregate_key]


def test_aggregate_artifact_that_is_not_the_one_the_job_committed_is_caught() -> None:
    ipfs = FakeIpfs()
    jobs, aggregate_key, _, _ = _aggregate_fixture(ipfs)
    jobs[aggregate_key].input_bundle_hash = bytes.fromhex("99" * 32)
    daemon, protocol = _daemon(ipfs, jobs, executor=None, verifier_reexecute=False)

    assert daemon.run_once() == 1
    _, _, evidence_cid, _ = protocol.attestations[0]
    errors = ipfs.cat_json(evidence_cid)["validation_errors"]
    assert any("input_bundle_hash" in error for error in errors)


def test_aggregate_branches_are_checked_against_the_chain() -> None:
    """An artifact can be internally consistent and still name receipts nothing settled."""

    ipfs = FakeIpfs()
    jobs, aggregate_key, artifact, _ = _aggregate_fixture(ipfs)
    import json as _json

    branch_key = Pubkey.from_string(_json.loads(artifact)["branches"][0]["job"])
    jobs[branch_key].submitted_result_hash = bytes.fromhex("55" * 32)
    daemon, protocol = _daemon(ipfs, jobs, executor=None, verifier_reexecute=False)

    assert daemon.run_once() == 1
    _, _, evidence_cid, _ = protocol.attestations[0]
    errors = ipfs.cat_json(evidence_cid)["validation_errors"]
    assert any("differs from the on-chain submitted_result_hash" in error for error in errors)


# --- branch canonicalization receipts -------------------------------------------


def _stub_zkvm(monkeypatch: pytest.MonkeyPatch, journal, *, error: str | None = None) -> None:
    from worker_common import branch_receipt

    def fake_verify(binary, bundle, *, timeout):
        if error:
            raise branch_receipt.BranchReceiptError(error)
        return journal

    monkeypatch.setattr(branch_receipt, "verify", fake_verify)


def _receipt_bundle(image_id: str = "ee" * 32) -> dict:
    return {
        "bundle_version": "kswarm-branch-receipt-v1",
        "image_id_hex": image_id,
        "journal": {"input_digest": "00" * 32, "result_hash": "00" * 32, "output_len": 0},
        "journal_hex": "00" * 68,
        "receipt_b64": "AA==",
    }


def _branch_receipt_job(ipfs: FakeIpfs, monkeypatch: pytest.MonkeyPatch, *, receipt_cid: bool = True):
    """An honest branch whose worker also published a receipt bundle."""

    from worker_common import branch_receipt

    branch_input = _branch_input()
    executor = _executor(ipfs, [HONEST_ANSWER])
    execution = executor.execute("job", branch_input)
    output = execution.output
    if receipt_cid:
        cid = ipfs.add_json("receipt", _receipt_bundle())
        output = output.model_copy(update={"zkvm_receipt_cid": cid})
    job_key, job = _completed_job(ipfs, branch_input, output)
    expected = branch_receipt.expected_journal(
        branch_input.model_dump(mode="json", exclude_none=False),
        output.model_dump(mode="json", exclude_none=False),
        job.submitted_result_hash,
    )
    return branch_input, output, job_key, job, expected


def test_a_verified_branch_receipt_is_recorded_in_the_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    ipfs = FakeIpfs()
    monkeypatch.setenv("KSWARM_ZKVM_HOST", "/usr/bin/true")
    branch_input, output, job_key, job, expected = _branch_receipt_job(ipfs, monkeypatch)
    _stub_zkvm(monkeypatch, expected)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))

    assert daemon.run_once() == 1
    _, _, evidence_cid, _ = protocol.attestations[0]
    report = ipfs.cat_json(evidence_cid)["branch_receipt"]
    assert report["mode"] == "verify"
    assert report["errors"] == []
    assert daemon.metrics.counters["receipts_verified"] == 1
    assert protocol.challenges == []


def test_a_receipt_that_does_not_bind_blocks_the_attestation(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Re-execution agrees, the receipt does not: attesting would settle an unproven job."""

    from worker_common.branch_receipt import ReceiptJournal

    ipfs = FakeIpfs()
    monkeypatch.setenv("KSWARM_ZKVM_HOST", "/usr/bin/true")
    branch_input, output, job_key, job, expected = _branch_receipt_job(ipfs, monkeypatch)
    _stub_zkvm(monkeypatch, ReceiptJournal(bytes(32), bytes(32), 0))
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))

    with caplog.at_level(logging.ERROR, logger="kswarm.verifier_worker"):
        assert daemon.run_once() == 0
    assert protocol.attestations == []
    assert daemon.metrics.counters["receipt_verification_refusals"] == 1
    assert "cannot settle" in caplog.text


def test_a_receipt_naming_another_guest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    ipfs = FakeIpfs()
    monkeypatch.setenv("KSWARM_ZKVM_HOST", "/usr/bin/true")
    monkeypatch.setenv("KSWARM_ZKVM_IMAGE_ID", "ab" * 32)
    branch_input, output, job_key, job, expected = _branch_receipt_job(ipfs, monkeypatch)
    _stub_zkvm(monkeypatch, expected)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))

    assert daemon.run_once() == 0
    assert protocol.attestations == []
    assert daemon.metrics.counters["receipt_binding_failures"] == 1


def test_a_missing_receipt_is_only_fatal_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    ipfs = FakeIpfs()
    monkeypatch.setenv("KSWARM_ZKVM_HOST", "/usr/bin/true")
    branch_input, output, job_key, job, _ = _branch_receipt_job(ipfs, monkeypatch, receipt_cid=False)
    daemon, protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))
    assert daemon.run_once() == 1
    assert len(protocol.attestations) == 1
    assert ipfs.cat_json(protocol.attestations[0][2])["branch_receipt"]["mode"] == "absent"

    monkeypatch.setenv("KSWARM_ZKVM_REQUIRE_RECEIPT", "1")
    job.verifier_authority = None
    job.verifier_attestation_hash = None
    strict, strict_protocol = _daemon(ipfs, {job_key: job}, executor=_executor(ipfs, [HONEST_ANSWER]))
    assert strict.run_once() == 0
    assert strict_protocol.attestations == []


def test_requiring_receipts_without_a_host_binary_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KSWARM_ZKVM_HOST", raising=False)
    monkeypatch.setenv("KSWARM_ZKVM_REQUIRE_RECEIPT", "1")
    with pytest.raises(ValueError, match="KSWARM_ZKVM_HOST"):
        _daemon(FakeIpfs(), {}, executor=None, verifier_reexecute=False)
