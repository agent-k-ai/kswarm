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
    return BranchExecutor(ipfs, llm_base_url="http://llm.test/v1", llm_model_name=model, client=StubLlmClient(scripted))


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
