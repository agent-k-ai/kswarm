"""Verifier daemon: re-execute the branch and attest to the verifier's own hash.

Re-execution mode (default): the verifier downloads the branch input, runs
`BranchExecutor.execute` with the identical model, seed and configuration
(bound to the worker's transcript CID through `verifier_reference`), encodes
its own canonical result bytes, and attests to their hash. A worker that did
not run the model gets a different hash. The on-chain rule in
`receipt_is_challengeable` (solana/programs/kswarm_protocol/src/lib.rs)
makes a receipt whose `submitted_result_hash` differs from the verifier
attestation hash challengeable, so the daemon then calls `challenge_job`.

`challenge_job` accepts only the verifier the customer or the protocol admin
assigned to the job with `assign_verifier`, for every job class (the
H2-Interim rule). A verifier that is not the assigned one still attests --
the attestation is what makes the receipt challengeable -- and reports that
it could not challenge. The attestation stands either way.

Hash-only mode (explicit `VERIFIER_HASH_ONLY=1`): the verifier re-hashes the
worker's own artifact. It can only catch a worker whose artifact does not
match its receipt. It cannot catch a lying worker and it logs a warning.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.protocol.branch_schemas import BranchInput, BranchOutput
from app.protocol.canonical_hash import branch_output_result_bytes

from branch_worker.executor import BranchExecutor, LlmEndpointError, ModelOutputRejectedError
from worker_common.cli_shim import JOB_CLASS, JOB_STATUS_BY_NAME, ZERO_HASH, RpcError
from worker_common.config import WorkerConfig
from worker_common.ipfs import IpfsClient, IpfsError
from worker_common.metrics import WorkerMetrics, start_metrics_server
from worker_common.protocol import ProtocolSession


LOGGER = logging.getLogger("kswarm.verifier_worker")

# `challenge_job` rejects a caller that is not the job's assigned verifier. Both
# codes mean the same thing here: this verifier may attest but may not challenge.
CHALLENGE_NOT_ASSIGNED_CODES = frozenset({"ChallengeRequiresAssignedVerifier", "VerifierNotAssigned"})

HASH_ONLY_WARNING = (
    "verifier runs in HASH-ONLY mode (VERIFIER_HASH_ONLY=1): it re-hashes the worker's artifact "
    "and cannot catch a worker that fabricated its output"
)
TIER_B_DISCLOSURE = (
    "For narrative_with_scalar outputs, the verifier re-executes the branch and compares the canonical "
    "guardrail commitment. Narrative text is hash-committed provenance, not verified narrative content."
)


@dataclass(frozen=True)
class Verification:
    verifier_result_bytes: bytes
    verifier_hash: bytes
    matched: bool
    validation_errors: list[str]
    surface: str
    verifier_output: BranchOutput
    verifier_transcript: dict[str, Any] | None


class VerifierWorkerDaemon:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        protocol: ProtocolSession | None = None,
        ipfs: IpfsClient | None = None,
        executor: BranchExecutor | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.protocol = protocol or ProtocolSession(config.rpc_url, config.keypair_name, config.program_id, config.wallet_file)
        self.ipfs = ipfs or IpfsClient(config.ipfs_api_url)
        self.metrics = WorkerMetrics("kswarm_verifier_worker")
        self.clock = clock
        self.executor: BranchExecutor | None = None
        if config.verifier_reexecute:
            self.executor = executor or BranchExecutor(self.ipfs, llm_base_url=config.llm_base_url, llm_model_name=config.llm_model_name)
        else:
            LOGGER.warning(HASH_ONLY_WARNING)

    def serve_forever(self) -> None:
        self.ipfs.check()
        start_metrics_server(self.metrics, self.config.metrics_host, self.config.metrics_port)
        LOGGER.info(
            "verifier worker started wallet=%s rpc=%s ipfs=%s mode=%s",
            self.config.keypair_name,
            self.config.rpc_url,
            self.config.ipfs_api_url,
            "reexecute" if self.executor else "hash-only",
        )
        while True:
            self.run_once()
            time.sleep(self.config.polling_interval_seconds)

    def run_once(self) -> int:
        processed = 0
        now = self.clock()
        for job_key, job in self.protocol.jobs():
            if not self._matches_attestable_branch_job(job, now):
                continue
            try:
                if self._verify_and_attest(job_key, job):
                    processed += 1
            except RpcError as exc:
                self.metrics.inc("attestation_races")
                LOGGER.info("attestation skipped job=%s code=%s", job_key, exc.code)
            except (LlmEndpointError, IpfsError, ModelOutputRejectedError) as exc:
                self.metrics.inc("reexecution_failures")
                LOGGER.warning("re-execution failed job=%s; no attestation submitted: %s", job_key, exc)
            except Exception:
                self.metrics.inc("attestations_failed")
                LOGGER.exception("verifier job failed job=%s", job_key)
        return processed

    def _verify_and_attest(self, job_key, job) -> bool:
        branch_input = BranchInput.model_validate_json(self.ipfs.cat_bytes(job.input_cid))
        submitted_output = BranchOutput.model_validate(self.ipfs.cat_json(job.output_cid))
        if self.executor is not None and submitted_output.llm_model != self.executor.llm_model_name:
            self.metrics.inc("reexecution_model_mismatches")
            LOGGER.warning(
                "cannot verify job=%s: worker claims model %r, this verifier runs %r; no attestation submitted",
                job_key,
                submitted_output.llm_model,
                self.executor.llm_model_name,
            )
            return False
        verification = self.verify(str(job_key), job.submitted_result_hash, branch_input, submitted_output)
        evidence_cid = self.ipfs.add_json(
            f"branch-{branch_input.branch_index}-verifier-evidence.json",
            self._evidence(str(job_key), job, submitted_output, verification),
        )
        self.protocol.submit_attestation(job_key, verification.verifier_result_bytes, evidence_cid, self.config.software_digest)
        self.metrics.inc("attestations_submitted")
        LOGGER.info(
            "attested job=%s worker_hash=%s verifier_hash=%s matched=%s evidence_cid=%s",
            job_key,
            job.submitted_result_hash.hex(),
            verification.verifier_hash.hex(),
            verification.matched,
            evidence_cid,
        )
        if not verification.matched:
            self.metrics.inc("mismatches")
            if self.config.challenge_on_mismatch:
                self._challenge(job_key)
        return True

    def _challenge(self, job_key) -> None:
        """Slash the worker. The attestation above stands even when the challenge is refused."""

        refreshed = self.protocol.job(job_key)
        if refreshed is None:
            LOGGER.warning("challenge skipped job=%s: job account vanished", job_key)
            return
        try:
            self.protocol.challenge_job(job_key, refreshed)
        except RpcError as exc:
            if exc.code not in CHALLENGE_NOT_ASSIGNED_CODES:
                raise
            # Not an error and not a race: the attestation above succeeded and stands,
            # and it is what makes the receipt challengeable. Only the verifier the
            # customer or admin assigned with `assign_verifier` may challenge, so an
            # unexpected RPC failure must not be reported as an assignment problem.
            self.metrics.inc("challenges_not_assigned")
            LOGGER.warning(
                "challenge refused job=%s code=%s: this verifier is not the job's assigned verifier, "
                "so it cannot challenge. The mismatching attestation is on chain; the customer or the "
                "admin must assign this verifier (assign_verifier) for the challenge to be accepted",
                job_key,
                exc.code,
            )
            return
        self.metrics.inc("challenges_submitted")
        LOGGER.warning("challenged job=%s: worker receipt hash differs from verifier re-execution", job_key)

    def verify(self, job_pubkey: str, submitted_result_hash: bytes, branch_input: BranchInput, submitted_output: BranchOutput) -> Verification:
        validation_errors = self._validate_output_matches_input(branch_input, submitted_output)
        if self.executor is not None:
            execution = self.executor.execute(job_pubkey, branch_input, verifier_reference=submitted_output)
            verifier_result_bytes = execution.result_bytes
            verifier_output = execution.output
            verifier_transcript: dict[str, Any] | None = execution.transcript
            surface = (
                "Verifier fetched the input and the worker's output from IPFS, re-executed the branch with the "
                "identical model, seed and configuration bound to the worker's transcript CID, encoded its own "
                "canonical result bytes, and compared their hash to the on-chain receipt."
            )
        else:
            LOGGER.warning(HASH_ONLY_WARNING)
            verifier_result_bytes = branch_output_result_bytes(submitted_output)
            verifier_output = submitted_output
            verifier_transcript = None
            surface = (
                "HASH-ONLY: verifier fetched the input and the worker's output from IPFS, validated the output/input "
                "binding, and re-hashed the worker's own artifact. This mode cannot detect a fabricated output."
            )
        verifier_hash = hashlib.sha256(verifier_result_bytes).digest()
        matched = verifier_hash == submitted_result_hash and not validation_errors
        return Verification(verifier_result_bytes, verifier_hash, matched, validation_errors, surface, verifier_output, verifier_transcript)

    def _evidence(self, job_pubkey: str, job, submitted_output: BranchOutput, verification: Verification) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "job": job_pubkey,
            "mode": "reexecute" if self.executor is not None else "hash-only",
            "submitted_output_cid": job.output_cid,
            "worker_result_hash": job.submitted_result_hash.hex(),
            "verifier_result_hash": verification.verifier_hash.hex(),
            "matched": verification.matched,
            "validation_errors": verification.validation_errors,
            "verification_surface": verification.surface,
            "tier_b_disclosure": TIER_B_DISCLOSURE,
            "worker_output": submitted_output.model_dump(mode="json", exclude_none=False),
            "verifier_output": verification.verifier_output.model_dump(mode="json", exclude_none=False),
            "verifier_transcript": verification.verifier_transcript,
        }

    def _matches_attestable_branch_job(self, job, now: float) -> bool:
        if job.status != JOB_STATUS_BY_NAME["submitted"]:
            return False
        if job.job_class != JOB_CLASS["branch-proof"]:
            return False
        if job.verifier_authority is not None:
            return False
        if job.challenge_deadline <= now:
            return False
        if job.required_software_digest not in {self.config.software_digest, ZERO_HASH}:
            return False
        if job.worker == self.protocol.wallet.pubkey:
            return False
        return True

    def _validate_output_matches_input(self, branch_input: BranchInput, output: BranchOutput) -> list[str]:
        errors: list[str] = []
        if output.parent_job != branch_input.parent_job:
            errors.append("parent_job mismatch")
        if output.branch_index != branch_input.branch_index:
            errors.append("branch_index mismatch")
        if output.rng_seed != branch_input.rng_seed:
            errors.append("rng_seed mismatch")
        if output.output_kind != branch_input.target_output_kind:
            errors.append("output_kind mismatch")
        return errors
