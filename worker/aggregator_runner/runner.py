"""Aggregate runner: prove the aggregate the customer already bound, then submit it.

The aggregate job's `input_bundle_hash` and `expected_result_hash` are fixed when the
job is opened, by `kswarm predict bind-aggregate`, against the MFA3 artifact built from
the settled branch receipts. So this runner does not decide what the aggregate is. It

1. fetches the artifact the job committed and checks it is the one the job was opened
   against (`sha256(len_le64 || artifact) == input_bundle_hash`),
2. reduces it with `kswarm_cli.aggregate`, the Python mirror of the guest, and checks
   the journal it predicts is the one the job expects,
3. cross-checks every branch receipt in the artifact against that branch job on chain,
4. claims the job and submits the guest's committed outputs as the receipt, and
5. runs the Bonsol execution so the callback writes a `Verified` marker.

Order matters: `record_aggregate_verification` requires the job to be `Completed` with
`submitted_result_hash == output_digest`, so the receipt is submitted before the proof
is requested. If the proof then fails, the job sits `Completed` with no marker and
`cancel_aggregate_proof_job` refunds the customer after the marker timeout, with no
slash. An aggregate that cannot be proven does not settle.

`KSWARM_ALLOW_UNBOUND_AGGREGATE=1` submits the receipt without requesting a proof. It
exists for a local stack with no Bonsol node, it logs a warning on every run, and it is
refused outright on the `devnet` and `mainnet` cluster profiles.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.protocol.branch_schemas import BranchOutput
from kswarm_cli.aggregate import (
    AggregateError,
    AggregateJournal,
    aggregate_journal,
    parse_branch_result_bytes,
)
from kswarm_cli.bonsol import framed_input_digest
from solders.pubkey import Pubkey

from worker_common.cli_shim import JOB_CLASS, JOB_STATUS_BY_NAME, ZERO_HASH, RpcError
from worker_common.config import WorkerConfig
from worker_common.ipfs import IpfsClient
from worker_common.metrics import WorkerMetrics, start_metrics_server
from worker_common.protocol import ProtocolSession

from .bonsol_hook import BONSOL_HOOK_ENV, BonsolBinding, BonsolHookError, run_bonsol_hook
from .run_store import RunStore


LOGGER = logging.getLogger("kswarm.aggregator_runner")
RECEIPT_BINDING_BONSOL = "bonsol-committed-outputs"
RECEIPT_BINDING_UNPROVEN = "mfa3-committed-outputs-unproven"
DEFAULT_HOOK_TIMEOUT_SECONDS = 1800.0
ALLOW_UNBOUND_ENV = "KSWARM_ALLOW_UNBOUND_AGGREGATE"
# Cluster profiles where an unproven aggregate receipt is never acceptable.
PROVING_REQUIRED_CLUSTERS = frozenset({"devnet", "mainnet"})


class AggregationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AggregateBinding:
    """What the runner derived from the committed artifact, before anything is sent."""

    artifact: bytes
    journal: AggregateJournal

    @property
    def committed_outputs(self) -> bytes:
        return self.journal.committed_outputs

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "MFA3",
            "artifact_sha256": hashlib.sha256(self.artifact).hexdigest(),
            **self.journal.to_json(),
        }


class AggregatorRunner:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        allow_completed_branches: bool = False,
        protocol: ProtocolSession | None = None,
        ipfs: IpfsClient | None = None,
        store: RunStore | None = None,
        hook_command: str | None = None,
        hook_runner: Callable[..., BonsolBinding] = run_bonsol_hook,
        clock: Callable[[], float] = time.time,
        environ: dict[str, str] | None = None,
    ):
        self.config = config
        self.protocol = protocol or ProtocolSession(config.rpc_url, config.keypair_name, config.program_id, config.wallet_file)
        self.ipfs = ipfs or IpfsClient(config.ipfs_api_url)
        self.metrics = WorkerMetrics("kswarm_aggregator_runner")
        self.allow_completed_branches = allow_completed_branches
        self.store = store or RunStore(config.predict_runs_dir)
        self.environ = dict(os.environ if environ is None else environ)
        self.hook_command = hook_command if hook_command is not None else self.environ.get(BONSOL_HOOK_ENV)
        self.hook_runner = hook_runner
        self.hook_timeout_seconds = float(self.environ.get("KSWARM_BONSOL_HOOK_TIMEOUT_SECONDS", str(DEFAULT_HOOK_TIMEOUT_SECONDS)))
        self.clock = clock
        self.allow_unproven = self._resolve_allow_unproven()

    def _resolve_allow_unproven(self) -> bool:
        """`KSWARM_ALLOW_UNBOUND_AGGREGATE=1`, and never on a real cluster."""

        if self.hook_command:
            return False
        if self.environ.get(ALLOW_UNBOUND_ENV, "").strip() != "1":
            return False
        if self.config.cluster in PROVING_REQUIRED_CLUSTERS:
            raise AggregationError(
                f"{ALLOW_UNBOUND_ENV}=1 is refused on cluster {self.config.cluster!r}: an aggregate that "
                f"cannot be proven must not settle. Set {BONSOL_HOOK_ENV} to a Bonsol execution hook."
            )
        LOGGER.warning(
            "%s=1: aggregate receipts will be submitted without a Bonsol proof. They cannot settle through "
            "settle_aggregate_proof_job and the customer will have to cancel them after the marker timeout. "
            "This is a local-development setting.",
            ALLOW_UNBOUND_ENV,
        )
        return True

    def serve_forever(self) -> None:
        self.ipfs.check()
        start_metrics_server(self.metrics, self.config.metrics_host, self.config.metrics_port)
        LOGGER.info("aggregator runner started wallet=%s", self.config.keypair_name)
        while True:
            self.run_once()
            time.sleep(self.config.polling_interval_seconds)

    def run_once(self) -> int:
        processed = 0
        for run_path in self.store.paths():
            with self.store.lock(run_path) as acquired:
                if not acquired:
                    LOGGER.info("run locked by another runner; skipping path=%s", run_path)
                    continue
                payload = self.store.load(run_path)
                if payload.get("aggregate_submitted"):
                    continue
                try:
                    if self._try_aggregate(run_path, payload):
                        processed += 1
                except Exception:
                    self.metrics.inc("aggregate_failed")
                    LOGGER.exception("aggregate run failed path=%s", run_path)
        return processed

    def _try_aggregate(self, run_path: Path, run: dict[str, Any]) -> bool:
        aggregate_job_key = Pubkey.from_string(run["aggregate_job"])
        aggregate_job = self.protocol.job(aggregate_job_key)
        if aggregate_job is None:
            # `predict bind-aggregate` has not run yet: the artifact does not exist
            # until the branches settle, so the job cannot have been opened.
            return False
        if aggregate_job.job_class != JOB_CLASS["aggregate-proof"]:
            return False
        ours = aggregate_job.status == JOB_STATUS_BY_NAME["claimed"] and aggregate_job.worker == self.protocol.wallet.pubkey
        if aggregate_job.status != JOB_STATUS_BY_NAME["open"] and not ours:
            return False
        if not aggregate_job.input_cid:
            return False
        if not self.hook_command and not self.allow_unproven:
            # Refuse before the claim, not after the receipt: a receipt that can never
            # be proven leaves the customer to cancel and the worker's stake locked
            # until the execute deadline.
            self.metrics.inc("aggregate_no_hook")
            LOGGER.error(
                "%s is not set: aggregate job=%s is not claimed, because its receipt could never be proven and "
                "settle_aggregate_proof_job would refuse it. Set the hook, or set %s=1 on a local cluster.",
                BONSOL_HOOK_ENV,
                aggregate_job_key,
                ALLOW_UNBOUND_ENV,
            )
            return False

        binding = self._bind_committed_artifact(aggregate_job)
        branch_outputs = self._branch_outputs_for_report(run, binding)

        if not ours:
            try:
                self.protocol.claim_job(aggregate_job_key)
            except RpcError as exc:
                self.metrics.inc("claim_races")
                LOGGER.info("aggregate claim skipped job=%s code=%s", aggregate_job_key, exc.code)
                return False
            run["aggregate_claimed_at_unix"] = int(self.clock())
            self.store.save(run_path, run)
            self.metrics.inc("aggregate_claimed")

        result_bytes = binding.committed_outputs
        receipt_binding = RECEIPT_BINDING_BONSOL if not self.allow_unproven else RECEIPT_BINDING_UNPROVEN
        result_cid = self.ipfs.add_json(
            "aggregate-output.json",
            {
                "schema_version": 3,
                "parent_run": run["parent_run"],
                "combiner": run["combiner"],
                "result": self._result_report(run, binding),
                "result_schema": "MFA3",
                "receipt_binding": receipt_binding,
                "result_hash": hashlib.sha256(result_bytes).hexdigest(),
                "aggregate_input_cid": aggregate_job.input_cid,
                "bonsol": binding.to_json(),
                "branch_outputs": [output.model_dump(mode="json", exclude_none=False) for output in branch_outputs],
            },
        )
        try:
            self.protocol.submit_receipt(aggregate_job_key, result_cid, result_bytes)
        except RpcError as exc:
            self.metrics.inc("aggregate_submit_failed")
            LOGGER.error(
                "aggregate submit failed job=%s code=%s; claim stays open (no on-chain release exists): "
                "stake at risk=%d base units of required_stake until execute_deadline=%d",
                aggregate_job_key,
                exc.code,
                aggregate_job.required_stake,
                aggregate_job.execute_deadline,
            )
            raise
        run["aggregate_submitted"] = True
        run["aggregate_output_cid"] = result_cid
        run["aggregate_result_hash"] = hashlib.sha256(result_bytes).hexdigest()
        run["aggregate_journal"] = binding.journal.to_json()
        run["updated_at_unix"] = int(self.clock())
        self.store.save(run_path, run)
        self.metrics.inc("aggregate_submitted")
        LOGGER.info(
            "submitted aggregate job=%s output_cid=%s result_hash=%s combiner=%s result_value=%s branch_count=%d",
            aggregate_job_key,
            result_cid,
            run["aggregate_result_hash"],
            run["combiner"],
            binding.journal.reduction.result_value,
            binding.journal.reduction.branch_count,
        )

        if self.allow_unproven:
            self.metrics.inc("aggregate_unproven")
            LOGGER.warning(
                "aggregate job=%s carries no Bonsol proof (%s=1); settle_aggregate_proof_job will refuse it",
                aggregate_job_key,
                ALLOW_UNBOUND_ENV,
            )
            return True

        execution = self._prove_bonsol(run, aggregate_job, aggregate_job_key, binding)
        run["aggregate_bonsol_execution"] = execution.to_json()
        run["updated_at_unix"] = int(self.clock())
        self.store.save(run_path, run)
        LOGGER.info(
            "aggregate proof requested job=%s execution_id=%s image_id=%s",
            aggregate_job_key,
            execution.execution_id,
            execution.image_id.hex(),
        )
        return True

    def _bind_committed_artifact(self, aggregate_job) -> AggregateBinding:
        """Reduce the artifact the job committed, and refuse anything the job cannot settle."""

        artifact = self.ipfs.cat_bytes(aggregate_job.input_cid)
        digest = framed_input_digest(artifact)
        if digest != aggregate_job.input_bundle_hash:
            raise AggregationError(
                f"aggregate input artifact {aggregate_job.input_cid} framed digest {digest.hex()} differs from the "
                f"job input_bundle_hash {aggregate_job.input_bundle_hash.hex()}: the committed artifact is not the "
                "one the job was opened against"
            )
        try:
            journal = aggregate_journal(artifact)
        except AggregateError as exc:
            raise AggregationError(f"the aggregate reducer would reject the committed artifact: {exc}") from exc
        if journal.journal_hash != aggregate_job.expected_result_hash:
            raise AggregationError(
                f"reduction journal hash {journal.journal_hash.hex()} differs from the job expected_result_hash "
                f"{aggregate_job.expected_result_hash.hex()}: the guest would commit a journal this job cannot settle"
            )
        if aggregate_job.required_software_digest == ZERO_HASH:
            raise AggregationError(
                "aggregate job carries a zero required_software_digest, so no Bonsol marker can ever match it"
            )
        self._check_branches_against_chain(artifact)
        self._check_artifact_against_plan(artifact)
        return AggregateBinding(artifact=artifact, journal=journal)

    def _check_branches_against_chain(self, artifact: bytes) -> None:
        """Every receipt in the artifact must be the receipt that branch job settled.

        The reduction already proves the artifact is internally consistent. This proves
        it describes real jobs: a customer cannot bind an aggregate to receipts that no
        branch of this protocol ever produced.
        """

        settled = JOB_STATUS_BY_NAME["settled"]
        completed = JOB_STATUS_BY_NAME["submitted"]
        for branch in json.loads(artifact.decode("utf-8"))["branches"]:
            job_key = Pubkey.from_string(branch["job"])
            job = self.protocol.job(job_key)
            if job is None:
                raise AggregationError(f"aggregate artifact names branch job {job_key}, which does not exist")
            if job.job_class != JOB_CLASS["branch-proof"]:
                raise AggregationError(f"aggregate artifact names {job_key}, which is not a branch-proof job")
            # A slashed or cancelled branch still has receipt bytes on chain. Reducing
            # one would pay for an aggregate over work the protocol already rejected.
            ready = job.status == settled or (
                self.allow_completed_branches and job.status == completed and job.verifier_attestation_hash is not None
            )
            if not ready:
                raise AggregationError(
                    f"aggregate artifact names branch job {job_key} in status {job.status}; the aggregate needs every "
                    "branch settled (or --allow-completed-branches with an attestation)"
                )
            receipt = parse_branch_result_bytes(bytes.fromhex(branch["result_bytes"]))
            if receipt.result_hash != job.submitted_result_hash:
                raise AggregationError(
                    f"aggregate artifact branch {branch['branch_index']} receipt hash {receipt.result_hash.hex()} "
                    f"differs from the on-chain submitted_result_hash {job.submitted_result_hash.hex()} of {job_key}"
                )

    def _check_artifact_against_plan(self, artifact: bytes) -> None:
        """When the artifact names a plan, the reduction must be the planned one.

        `open_job` fixes the aggregate hashes at bind time, which is after every branch
        result is visible, so the combiner and the branch set are whatever the customer
        chose at that moment. The plan is content-addressed and was pinned before any
        branch ran, and its CID is inside the artifact, so `input_bundle_hash` commits
        to it. Checking it here means a second party -- not the customer -- confirms the
        run did not change its mind after seeing the answers.

        An artifact with no plan CID is reduced as before: the field is provenance the
        chain carries, not a value the guest reads.
        """

        document = json.loads(artifact.decode("utf-8"))
        plan_cid = document.get("aggregate_plan_cid")
        if not plan_cid:
            return
        try:
            plan = self.ipfs.cat_json(str(plan_cid))
        except Exception as exc:  # noqa: BLE001 - any fetch failure is a refusal
            raise AggregationError(
                f"aggregate artifact names plan {plan_cid} but it cannot be read: {exc}"
            ) from exc
        differences: list[str] = []
        if plan.get("combiner") != document.get("combiner"):
            differences.append(
                f"combiner {document.get('combiner')!r} is not the planned {plan.get('combiner')!r}"
            )
        if dict(plan.get("combiner_parameters") or {}) != dict(document.get("combiner_parameters") or {}):
            differences.append("combiner_parameters differ from the plan")
        planned_jobs = [str(item["job"]) for item in (plan.get("branch_jobs") or [])]
        bound_jobs = [str(branch["job"]) for branch in document["branches"]]
        if planned_jobs and planned_jobs != bound_jobs:
            differences.append(f"branch set {bound_jobs} is not the planned {planned_jobs}")
        if differences:
            raise AggregationError(
                f"the committed artifact departs from the aggregate plan {plan_cid} pinned before any "
                "branch ran: " + "; ".join(differences)
            )

    def _branch_outputs_for_report(self, run: dict[str, Any], binding: AggregateBinding) -> list[BranchOutput]:
        """The branch documents, for the human-readable aggregate output artifact.

        These are provenance only. Nothing here reaches the journal, so a branch output
        that cannot be fetched degrades the report and never the proof.
        """

        outputs: list[BranchOutput] = []
        for branch in json.loads(binding.artifact.decode("utf-8"))["branches"]:
            try:
                outputs.append(BranchOutput.model_validate(self.ipfs.cat_json(branch["output_cid"])))
            except Exception:
                LOGGER.warning(
                    "branch %s output %s could not be fetched for the aggregate report",
                    branch["branch_index"],
                    branch["output_cid"],
                )
        return outputs

    def _result_report(self, run: dict[str, Any], binding: AggregateBinding) -> dict[str, Any]:
        reduction = binding.journal.reduction
        scalar = reduction.result_value if run["combiner"] != "majority-vote" else None
        label = reduction.result_value if run["combiner"] == "majority-vote" else None
        return {
            "combiner": run["combiner"],
            "combiner_id": reduction.combiner_id,
            "combiner_parameters": {
                "trim_bps": reduction.trim_bps,
                "category_dictionary_size": reduction.category_dictionary_size,
            },
            "scalar_value_bps": scalar,
            "categorical_label_index": label,
            "branch_count": reduction.branch_count,
            "merkle_root": reduction.merkle_root.hex(),
            "output_schema_hash": run["parent_manifest"]["output_schema_hash"],
            "aggregate_input_sha256": hashlib.sha256(binding.artifact).hexdigest(),
        }

    def _prove_bonsol(self, run: dict[str, Any], aggregate_job, aggregate_job_key, binding: AggregateBinding) -> BonsolBinding:
        """Run the hook, then refuse any execution that does not match what we computed."""

        if not self.hook_command:
            raise AggregationError(
                f"{BONSOL_HOOK_ENV} is not set: the aggregate receipt is on chain but cannot be proven, so it can "
                f"never settle. Set the hook, or set {ALLOW_UNBOUND_ENV}=1 on a local cluster to accept that."
            )
        payload = {
            "run": run["parent_run"],
            "aggregate_job": run["aggregate_job"],
            "image_id": aggregate_job.required_software_digest.hex(),
            "input_cid": aggregate_job.input_cid,
            "input_artifact_hex": binding.artifact.hex(),
            "input_digest": binding.journal.input_digest.hex(),
            "committed_outputs": binding.committed_outputs.hex(),
            "output_digest": binding.journal.output_digest.hex(),
            "journal_hash": binding.journal.journal_hash.hex(),
            "result": self._result_report(run, binding),
        }
        try:
            execution = self.hook_runner(
                self.hook_command,
                payload,
                cwd=Path(__file__).resolve().parents[2],
                timeout_seconds=self.hook_timeout_seconds,
            )
            check_binding_against_job(execution, aggregate_job, binding)
        except BonsolHookError:
            self.metrics.inc("bonsol_hook_failed")
            raise
        self.metrics.inc("bonsol_hook_proved")
        return execution


def check_binding_against_job(execution: BonsolBinding, job, binding: AggregateBinding) -> None:
    """Mirror `validate_settle_aggregate_proof_job`: an execution that cannot settle is an error.

    The runner already knows every value the marker must carry, because it reduced the
    artifact itself. So this is not "trust the hook and check the job"; it is "the hook
    must have executed the same claim".
    """

    if execution.committed_outputs != binding.committed_outputs:
        raise BonsolHookError(
            f"hook committed_outputs {execution.committed_outputs.hex()} differ from the reduction "
            f"{binding.committed_outputs.hex()}: the execution proved a different claim"
        )
    if execution.input_digest != binding.journal.input_digest:
        raise BonsolHookError(
            f"hook input_digest {execution.input_digest.hex()} differs from the artifact digest "
            f"{binding.journal.input_digest.hex()}"
        )
    if execution.input_digest != job.input_bundle_hash:
        raise BonsolHookError(
            f"hook input_digest {execution.input_digest.hex()} differs from the aggregate job input_bundle_hash "
            f"{job.input_bundle_hash.hex()}; settle_aggregate_proof_job would reject the marker"
        )
    if execution.image_id != job.required_software_digest:
        raise BonsolHookError(
            f"hook image_id {execution.image_id.hex()} differs from the aggregate job required_software_digest "
            f"{job.required_software_digest.hex()}"
        )
    if execution.journal_hash != job.expected_result_hash:
        raise BonsolHookError(
            f"hook journal_hash {execution.journal_hash.hex()} differs from the aggregate job expected_result_hash "
            f"{job.expected_result_hash.hex()}"
        )
