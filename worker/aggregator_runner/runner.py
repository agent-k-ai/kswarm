from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.protocol.branch_schemas import BranchOutput
from app.protocol.canonical_hash import canonical_json_bytes
from solders.pubkey import Pubkey

from worker_common.cli_shim import JOB_CLASS, JOB_STATUS_BY_NAME, ZERO_HASH, RpcError
from worker_common.config import WorkerConfig
from worker_common.ipfs import IpfsClient
from worker_common.metrics import WorkerMetrics, start_metrics_server
from worker_common.protocol import ProtocolSession

from .bonsol_hook import BONSOL_HOOK_ENV, BonsolBinding, BonsolHookError, run_bonsol_hook
from .combiners import (
    CategoricalVote,
    CombinerError,
    CombinerErrorKind,
    WeightedValue,
    combiner_id,
    majority_vote,
    mean_to_bps,
    trim_count_from_bps,
    trimmed_mean,
    weighted_mean,
)
from .run_store import RunStore


LOGGER = logging.getLogger("kswarm.aggregator_runner")
RESULT_MAGIC = b"MFA2"
RESULT_SCHEMA = "MFA2"
MAX_RESULT_BYTES = 512
RECEIPT_BINDING_BONSOL = "bonsol-committed-outputs"
RECEIPT_BINDING_CANONICAL = "mfa2-canonical-unbound"
SCALAR_KINDS = frozenset({"scalar", "narrative_with_scalar"})
DEFAULT_HOOK_TIMEOUT_SECONDS = 1800.0


class AggregationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AggregateResult:
    combiner: str
    combiner_id: int
    combiner_parameters: dict[str, Any]
    scalar_value_bps: int | None
    categorical_label_index: int | None
    rejected_count: int | None
    branch_count: int
    output_schema_hash: str
    aggregate_input_sha256: str
    bonsol: BonsolBinding | None = field(default=None)

    def to_json(self) -> dict[str, Any]:
        return {
            "combiner": self.combiner,
            "combiner_id": self.combiner_id,
            "combiner_parameters": self.combiner_parameters,
            "scalar_value_bps": self.scalar_value_bps,
            "categorical_label_index": self.categorical_label_index,
            "rejected_count": self.rejected_count,
            "branch_count": self.branch_count,
            "output_schema_hash": self.output_schema_hash,
            "aggregate_input_sha256": self.aggregate_input_sha256,
            "bonsol": self.bonsol.to_json() if self.bonsol else None,
        }

    def with_bonsol(self, binding: BonsolBinding) -> "AggregateResult":
        return dataclasses.replace(self, bonsol=binding)


def aggregate_result_bytes(result: AggregateResult) -> bytes:
    """Canonical MFA2 receipt bytes; hashed behind the magic when they exceed the on-chain cap."""

    payload = canonical_json_bytes({"schema": RESULT_SCHEMA, **result.to_json()})
    if len(payload) > MAX_RESULT_BYTES:
        return RESULT_MAGIC + hashlib.sha256(payload).digest()
    return payload


def combine(run: dict[str, Any], outputs: list[BranchOutput]) -> AggregateResult:
    """Dispatch on the manifest combiner. Unknown combiners and missing parameters fail closed."""

    combiner = run["combiner"]
    identifier = combiner_id(combiner)
    sorted_outputs = sorted(outputs, key=lambda item: item.branch_index)
    manifest = run["parent_manifest"]
    output_schema_hash = manifest["output_schema_hash"]
    aggregate_input = {
        "schema_version": 2,
        "parent_run": run["parent_run"],
        "combiner": combiner,
        "combiner_id": identifier,
        "output_schema_hash": output_schema_hash,
        "branches": [item.model_dump(mode="json", exclude_none=False) for item in sorted_outputs],
    }
    aggregate_input_sha256 = hashlib.sha256(canonical_json_bytes(aggregate_input)).hexdigest()
    parameters = dict(manifest.get("combiner_parameters") or {})
    scalar_value_bps: int | None = None
    label_index: int | None = None
    rejected_count: int | None = None
    if combiner == "weighted-mean":
        values = _scalar_values(sorted_outputs)
        if "weights" in parameters:
            raise AggregationError("per-branch weights are not supported by this runner; remove combiner_parameters.weights")
        parameters["weights"] = "uniform"
        scalar_value_bps = mean_to_bps(weighted_mean([WeightedValue(value, 1) for value in values]))
    elif combiner == "trimmed-mean":
        values = _scalar_values(sorted_outputs)
        if "trim_bps" not in parameters:
            raise AggregationError("trimmed-mean requires parent_manifest.combiner_parameters.trim_bps")
        outlier_count = trim_count_from_bps(len(values), parameters["trim_bps"])
        trimmed = trimmed_mean(values, outlier_count)
        parameters["outlier_count"] = outlier_count
        scalar_value_bps = mean_to_bps(trimmed.mean)
        rejected_count = trimmed.rejected_count
    elif combiner == "majority-vote":
        dictionary = manifest.get("output_schema", {}).get("category_dictionary")
        if not isinstance(dictionary, list) or not dictionary:
            raise AggregationError("majority-vote requires parent_manifest.output_schema.category_dictionary")
        votes = [CategoricalVote(_label_index(item, len(dictionary)), 1) for item in sorted_outputs]
        parameters["category_dictionary_size"] = len(dictionary)
        label_index = majority_vote(votes)
    else:
        raise CombinerError(CombinerErrorKind.UNKNOWN_COMBINER, repr(combiner))
    return AggregateResult(
        combiner=combiner,
        combiner_id=identifier,
        combiner_parameters=parameters,
        scalar_value_bps=scalar_value_bps,
        categorical_label_index=label_index,
        rejected_count=rejected_count,
        branch_count=len(sorted_outputs),
        output_schema_hash=output_schema_hash,
        aggregate_input_sha256=aggregate_input_sha256,
    )


def _scalar_values(outputs: list[BranchOutput]) -> list[int]:
    values: list[int] = []
    for output in outputs:
        if output.output_kind not in SCALAR_KINDS or output.scalar_value_bps is None:
            raise AggregationError(f"branch {output.branch_index} has no scalar value; scalar combiners need one from every branch")
        values.append(output.scalar_value_bps)
    return values


def _label_index(output: BranchOutput, dictionary_size: int) -> int:
    if output.output_kind != "categorical" or output.categorical_label_index is None:
        raise AggregationError(f"branch {output.branch_index} has no categorical label; majority-vote needs one from every branch")
    if output.categorical_label_index >= dictionary_size:
        raise AggregationError(f"branch {output.branch_index} label {output.categorical_label_index} is outside the committed category dictionary")
    return output.categorical_label_index


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
    ):
        self.config = config
        self.protocol = protocol or ProtocolSession(config.rpc_url, config.keypair_name, config.program_id, config.wallet_file)
        self.ipfs = ipfs or IpfsClient(config.ipfs_api_url)
        self.metrics = WorkerMetrics("kswarm_aggregator_runner")
        self.allow_completed_branches = allow_completed_branches
        self.store = store or RunStore(config.predict_runs_dir)
        self.hook_command = hook_command if hook_command is not None else os.environ.get(BONSOL_HOOK_ENV)
        self.hook_runner = hook_runner
        self.hook_timeout_seconds = float(os.environ.get("KSWARM_BONSOL_HOOK_TIMEOUT_SECONDS", str(DEFAULT_HOOK_TIMEOUT_SECONDS)))
        self.clock = clock

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
        branch_outputs = self._collect_branch_outputs(run)
        if branch_outputs is None:
            return False
        aggregate_job_key = Pubkey.from_string(run["aggregate_job"])
        aggregate_job = self.protocol.job(aggregate_job_key)
        if aggregate_job is None or aggregate_job.job_class != JOB_CLASS["aggregate-proof"]:
            return False
        ours = aggregate_job.status == JOB_STATUS_BY_NAME["claimed"] and aggregate_job.worker == self.protocol.wallet.pubkey
        if aggregate_job.status != JOB_STATUS_BY_NAME["open"] and not ours:
            return False

        result = combine(run, branch_outputs)
        if self.hook_command:
            binding = self._bind_bonsol(run, aggregate_job, result)
            result = result.with_bonsol(binding)
            result_bytes = binding.committed_outputs
            receipt_binding = RECEIPT_BINDING_BONSOL
        else:
            LOGGER.warning(
                "%s is not set: aggregate receipt for run=%s carries no Bonsol binding and cannot settle through "
                "settle_aggregate_proof_job (local development only)",
                BONSOL_HOOK_ENV,
                run["parent_run"],
            )
            result_bytes = aggregate_result_bytes(result)
            receipt_binding = RECEIPT_BINDING_CANONICAL
        result_cid = self.ipfs.add_json(
            "aggregate-output.json",
            {
                "schema_version": 2,
                "parent_run": run["parent_run"],
                "combiner": run["combiner"],
                "result": result.to_json(),
                "result_schema": RESULT_SCHEMA,
                "receipt_binding": receipt_binding,
                "result_hash": hashlib.sha256(result_bytes).hexdigest(),
                "branch_outputs": [output.model_dump(mode="json", exclude_none=False) for output in branch_outputs],
            },
        )
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
        run["updated_at_unix"] = int(self.clock())
        self.store.save(run_path, run)
        self.metrics.inc("aggregate_submitted")
        LOGGER.info(
            "submitted aggregate job=%s output_cid=%s result_hash=%s combiner=%s",
            aggregate_job_key,
            result_cid,
            run["aggregate_result_hash"],
            run["combiner"],
        )
        return True

    def _collect_branch_outputs(self, run: dict[str, Any]) -> list[BranchOutput] | None:
        outputs: list[BranchOutput] = []
        for branch_job in run["branch_jobs"]:
            job = self.protocol.job(Pubkey.from_string(branch_job["job"]))
            if job is None or not self._branch_ready(job):
                return None
            outputs.append(BranchOutput.model_validate(self.ipfs.cat_json(job.output_cid)))
        return outputs

    def _branch_ready(self, job) -> bool:
        if job.status == JOB_STATUS_BY_NAME["settled"]:
            return True
        return self.allow_completed_branches and job.status == JOB_STATUS_BY_NAME["submitted"] and job.verifier_attestation_hash is not None

    def _bind_bonsol(self, run: dict[str, Any], aggregate_job, result: AggregateResult) -> BonsolBinding:
        """Run the hook, then refuse any binding the program could never settle against this job."""

        payload = {
            "run": run["parent_run"],
            "aggregate_job": run["aggregate_job"],
            "result": result.to_json(),
            "aggregate_result_sha256": hashlib.sha256(aggregate_result_bytes(result)).hexdigest(),
        }
        try:
            binding = self.hook_runner(
                self.hook_command,
                payload,
                cwd=Path(__file__).resolve().parents[2],
                timeout_seconds=self.hook_timeout_seconds,
            )
            check_binding_against_job(binding, aggregate_job)
        except BonsolHookError:
            self.metrics.inc("bonsol_hook_failed")
            raise
        self.metrics.inc("bonsol_hook_bound")
        return binding


def check_binding_against_job(binding: BonsolBinding, job) -> None:
    """Mirror `validate_settle_aggregate_proof_job`: a receipt that cannot settle is not submitted."""

    if binding.input_digest != job.input_bundle_hash:
        raise BonsolHookError(
            f"hook input_digest {binding.input_digest.hex()} differs from the aggregate job input_bundle_hash "
            f"{job.input_bundle_hash.hex()}; settle_aggregate_proof_job would reject the marker"
        )
    if job.required_software_digest != ZERO_HASH and binding.image_id != job.required_software_digest:
        raise BonsolHookError(
            f"hook image_id {binding.image_id.hex()} differs from the aggregate job required_software_digest "
            f"{job.required_software_digest.hex()}"
        )
    if job.expected_result_hash != ZERO_HASH and binding.journal_hash != job.expected_result_hash:
        raise BonsolHookError(
            f"hook journal_hash {binding.journal_hash.hex()} differs from the aggregate job expected_result_hash "
            f"{job.expected_result_hash.hex()}"
        )
