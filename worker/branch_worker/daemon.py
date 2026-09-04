"""Branch worker daemon: claim discipline around an LLM that can fail.

There is no on-chain "release claim" instruction. Once `claim_job` lands, the
worker's `required_stake` is locked and the only exits are `submit_receipt`
before `execute_deadline` or `slash_stale_job` after it. Every claim is
therefore a bet that this process can execute the job. The daemon protects
that bet in five ways:

1. A pre-claim health check of the LLM endpoint and IPFS before every claim.
2. At most `max_concurrent_claims` claims, measured against the on-chain
   `Worker.active_claims` (which counts every claim not yet settled, challenged
   or slashed, including completed jobs waiting out their challenge window).
3. After a claim, execution is retried with exponential backoff until
   `execute_deadline - execute_deadline_margin_seconds`.
4. A circuit breaker: after an abandoned claim, no new claims for
   `claim_cooldown_seconds`.
5. An explicit log line with the stake at risk whenever a claim is abandoned.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.protocol.branch_schemas import BranchInput
from pydantic import ValidationError

from worker_common.cli_shim import JOB_CLASS, JOB_STATUS_BY_NAME, ZERO_HASH, RpcError
from worker_common.config import WorkerConfig
from worker_common.ipfs import IpfsClient, IpfsError
from worker_common.metrics import WorkerMetrics, start_metrics_server
from worker_common.protocol import ProtocolSession

from .executor import BranchExecutor, LlmEndpointError, ModelOutputRejectedError


LOGGER = logging.getLogger("kswarm.branch_worker")


@dataclass
class ClaimCircuitBreaker:
    """Pauses claiming for `cooldown_seconds` after a failed execution."""

    cooldown_seconds: float
    open_until: float = 0.0

    def trip(self, now: float) -> None:
        self.open_until = now + self.cooldown_seconds

    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def remaining(self, now: float) -> float:
        return max(0.0, self.open_until - now)


@dataclass(frozen=True)
class ExecutionOutcome:
    submitted: bool
    reason: str
    attempts: int


def backoff_seconds(attempt: int, initial: float, maximum: float) -> float:
    """Exponential backoff: initial * 2**(attempt-1), capped at maximum."""

    if attempt < 1:
        raise ValueError("attempt starts at 1")
    return min(maximum, initial * (2 ** (attempt - 1)))


class BranchWorkerDaemon:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        protocol: ProtocolSession | None = None,
        ipfs: IpfsClient | None = None,
        executor: BranchExecutor | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.protocol = protocol or ProtocolSession(config.rpc_url, config.keypair_name, config.program_id, config.wallet_file)
        self.ipfs = ipfs or IpfsClient(config.ipfs_api_url)
        self.metrics = WorkerMetrics("kswarm_branch_worker")
        self.executor = executor or BranchExecutor(self.ipfs, llm_base_url=config.llm_base_url, llm_model_name=config.llm_model_name)
        self.breaker = ClaimCircuitBreaker(config.claim_cooldown_seconds)
        self.clock = clock
        self.sleep = sleep

    def serve_forever(self) -> None:
        self.ipfs.check()
        start_metrics_server(self.metrics, self.config.metrics_host, self.config.metrics_port)
        LOGGER.info(
            "branch worker started wallet=%s rpc=%s ipfs=%s max_concurrent_claims=%d",
            self.config.keypair_name,
            self.config.rpc_url,
            self.config.ipfs_api_url,
            self.config.max_concurrent_claims,
        )
        while True:
            self.run_once()
            self.sleep(self.config.polling_interval_seconds)

    def run_once(self) -> int:
        now = self.clock()
        if self.breaker.is_open(now):
            self.metrics.inc("claim_pauses")
            LOGGER.warning("claiming paused after a failed execution; resumes in %.0fs", self.breaker.remaining(now))
            return 0
        budget = self.claim_budget()
        if budget <= 0:
            LOGGER.info("no claim capacity: max_concurrent_claims=%d is fully used by unsettled claims", self.config.max_concurrent_claims)
            return 0
        processed = 0
        for job_key, job in self.protocol.jobs():
            if not self._matches_open_branch_job(job, now):
                continue
            if budget <= 0:
                LOGGER.info("claim budget for this pass is exhausted; remaining open jobs wait for the next pass")
                break
            if not self.preflight_ok():
                break
            if not self._claim(job_key):
                continue
            budget -= 1
            outcome = self.execute_claimed(job_key)
            if outcome.submitted:
                processed += 1
                continue
            self._abandon(job_key, job, outcome)
            break
        return processed

    def claim_budget(self) -> int:
        """Claims this pass may make: the local cap minus the on-chain active claims."""

        worker = self.protocol.worker_account()
        if worker is None:
            raise RuntimeError(f"worker {self.config.keypair_name} is not registered on-chain")
        return self.config.max_concurrent_claims - worker.active_claims

    def preflight_ok(self) -> bool:
        """Cheap liveness checks of everything a claim commits us to use."""

        try:
            self.executor.check_endpoint()
            self.ipfs.check()
        except (LlmEndpointError, IpfsError) as exc:
            self.metrics.inc("preflight_failures")
            LOGGER.warning("pre-claim health check failed; not claiming: %s", exc)
            return False
        return True

    def execute_claimed(self, job_key) -> ExecutionOutcome:
        """Execute a job this wallet holds, retrying transient failures until the deadline margin."""

        claimed = self.protocol.job(job_key)
        if claimed is None or claimed.status != JOB_STATUS_BY_NAME["claimed"] or claimed.worker != self.protocol.wallet.pubkey:
            return ExecutionOutcome(False, "claim is not visible on-chain as ours", 0)
        if claimed.input_cid == "":
            return ExecutionOutcome(False, "claimed job has no input CID", 0)
        latest_start = claimed.execute_deadline - self.config.execute_deadline_margin_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                branch_input = BranchInput.model_validate_json(self.ipfs.cat_bytes(claimed.input_cid))
                execution = self.executor.execute(str(job_key), branch_input)
                self.protocol.submit_receipt(job_key, execution.output_cid, execution.result_bytes)
            except (LlmEndpointError, IpfsError) as exc:
                if isinstance(exc, LlmEndpointError) and not exc.retryable:
                    return ExecutionOutcome(False, f"non-retryable LLM error: {exc}", attempt)
                wait = backoff_seconds(attempt, self.config.execute_retry_initial_seconds, self.config.execute_retry_max_seconds)
                if self.clock() + wait > latest_start:
                    return ExecutionOutcome(False, f"execute deadline margin reached after {attempt} attempt(s): {exc}", attempt)
                self.metrics.inc("execution_retries")
                LOGGER.warning("branch job=%s attempt %d failed (%s); retrying in %.0fs", job_key, attempt, exc, wait)
                self.sleep(wait)
                continue
            except ModelOutputRejectedError as exc:
                return ExecutionOutcome(False, f"model output rejected; nothing submitted: {exc}", attempt)
            except ValidationError as exc:
                return ExecutionOutcome(False, f"branch input is not a valid BranchInput: {exc}", attempt)
            except RpcError as exc:
                return ExecutionOutcome(False, f"submit_receipt failed code={exc.code}: {exc}", attempt)
            self.metrics.inc("jobs_succeeded")
            self.metrics.inc("llm_latency_ms", int(execution.llm_latency_seconds * 1000))
            LOGGER.info(
                "submitted branch job=%s output_cid=%s result_hash=%s attempts=%d",
                job_key,
                execution.output_cid,
                hashlib.sha256(execution.result_bytes).hexdigest(),
                execution.attempts,
            )
            return ExecutionOutcome(True, "submitted", attempt)

    def _claim(self, job_key) -> bool:
        try:
            self.protocol.claim_job(job_key)
        except RpcError as exc:
            self.metrics.inc("claim_races")
            LOGGER.info("claim skipped job=%s code=%s", job_key, exc.code)
            return False
        self.metrics.inc("jobs_claimed")
        LOGGER.info("claimed branch job=%s", job_key)
        return True

    def _abandon(self, job_key, job: Any, outcome: ExecutionOutcome) -> None:
        now = self.clock()
        self.metrics.inc("jobs_failed")
        self.metrics.inc("claims_abandoned")
        self.breaker.trip(now)
        LOGGER.error(
            "branch job failed job=%s reason=%s; claim abandoned (no on-chain release exists): "
            "stake at risk=%d base units of required_stake, slashable by slash_stale_job once execute_deadline passes; "
            "claiming paused for %.0fs",
            job_key,
            outcome.reason,
            job.required_stake,
            self.config.claim_cooldown_seconds,
        )

    def _matches_open_branch_job(self, job, now: float) -> bool:
        if job.status != JOB_STATUS_BY_NAME["open"]:
            return False
        if job.job_class != JOB_CLASS["branch-proof"]:
            return False
        if job.claim_deadline < now:
            return False
        if job.required_capability_class_hash not in self.config.capabilities and job.required_capability_class_hash != ZERO_HASH:
            return False
        if job.required_software_digest not in {self.config.software_digest, ZERO_HASH}:
            return False
        if job.required_role > self.config.role and job.required_role in {1, 2, 3}:
            return False
        if job.required_role not in {self.config.role, 1, 2, 3} and job.required_role != self.config.role:
            return False
        if job.required_tier > self.config.tier:
            return False
        return True
