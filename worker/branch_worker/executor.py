from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import openai

from app.protocol.branch_schemas import BranchInput, BranchOutput
from app.protocol.canonical_hash import branch_output_result_bytes, branch_result_hash, canonical_json_bytes
from app.utils.llm_client import LLMClient, LLMJsonResult

from worker_common import branch_receipt
from worker_common.ipfs import IpfsClient

from .parsing import (
    InvalidModelOutputError,
    ParsedCategorical,
    ParsedNarrative,
    ParsedResponse,
    ParsedScalar,
    parse_model_response,
)


import logging

LOGGER = logging.getLogger("kswarm.branch_worker.executor")

LLM_SEED_MODULUS = 2_147_483_647

# Completion-token budget for a single branch prediction.
#
# The branch worker's strong arm is a REASONING model (the vLLM Qwen3 MoE): it
# spends thousands of tokens THINKING before emitting the final JSON, and more on
# evidence-rich inputs. A stingy budget truncates the reasoning, yielding an empty
# or partial `content` field -> a crash or a starved prediction. The old 1200/2048
# constants were far too low for such a model. This default is therefore large and
# reasoning-appropriate; it is a CAP, not a target (the model stops when done,
# ~2-3k tokens typically), so the cheap Ollama arm is unaffected in practice.
# The served context (vLLM max_model_len=32768) comfortably fits 12k output plus
# a normal branch input. Override per deployment with LLM_MAX_TOKENS.
DEFAULT_LLM_MAX_TOKENS = 12000

# Retries of the identical request (same messages, same seed) after the model
# returns output that fails strict parsing. Override with LLM_INVALID_OUTPUT_RETRIES.
DEFAULT_INVALID_OUTPUT_RETRIES = 2

# HTTP statuses that mean "try the same endpoint again later". Every other
# status is a request or configuration problem that a retry cannot fix.
RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# The system prompt is part of the committed software surface. Its SHA-256 is
# folded into `llm_version_hash`, so a prompt change is visible on-chain as a
# different version hash (and therefore a verifier mismatch).
SYSTEM_PROMPT = (
    "You execute one deterministic kswarm branch prediction. "
    "Return exactly one JSON object and no prose. "
    "For target_output_kind=scalar, output exactly these keys: scalar_value, confidence_lower, confidence_upper, rationale. "
    "scalar_value is your best probability estimate, in [0,1], that the answer to the question is yes, judged from the provided evidence. "
    "confidence_lower and confidence_upper are numeric bounds in [0,1]. "
    "Commit to a value across the full range as the evidence warrants; use values near 0.5 only when the evidence is genuinely balanced or absent, and do not default to 0.5. "
    "Do not return null for scalar_value. Do not include categorical_label_index for scalar jobs. "
    "For target_output_kind=categorical, output exactly categorical_label_index as an integer. "
    "For target_output_kind=narrative_with_scalar, include narrative_text, scalar_value, and narrative_scores with severity, quality, and ood in [0,1]."
)


class ExecutionError(RuntimeError):
    """Base class for branch execution failures the daemon must handle."""


class OasisEngineUnavailable(RuntimeError):
    """The branch needs `app.services.simulation_runner`, which only the `engine` extra provides."""


class LlmEndpointError(ExecutionError):
    """The LLM endpoint is unreachable, misconfigured, or returned an HTTP error.

    `retryable` is True when a later attempt against the same endpoint can
    succeed (connection errors, timeouts, 5xx, rate limits). It is False for
    request or configuration errors (4xx other than the retryable set).
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class BranchReceiptFailed(ExecutionError):
    """Proving the branch canonicalization receipt failed.

    The branch fails closed: nothing is submitted. A worker that proves receipts must
    not settle one it could not prove, because the verifier will refuse to attest to a
    branch whose receipt is missing or wrong and the job would time out anyway.
    """


class ModelOutputRejectedError(ExecutionError):
    """Every bounded attempt produced output that failed strict parsing.

    The daemon must not submit anything for the job. `attempts` records each
    rejected attempt (error text plus raw content) for the transcript and logs.
    """

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


def normalize_llm_seed(rng_seed: int) -> int:
    """Map branch RNG seeds into the signed seed range accepted by local LLMs."""

    return int(rng_seed) % LLM_SEED_MODULUS


def system_prompt_sha256() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


@dataclass
class BranchExecution:
    output: BranchOutput
    output_cid: str
    result_bytes: bytes
    transcript_cid: str
    transcript: dict[str, Any]
    llm_latency_seconds: float
    completion_tokens: int | None
    prompt_tokens: int | None
    total_tokens: int | None
    attempts: int
    # Set only when this worker proved a branch canonicalization receipt.
    zkvm_receipt_cid: str | None = None
    zkvm_prove_seconds: float | None = None


@dataclass(frozen=True)
class _ModelAnswer:
    result: LLMJsonResult
    parsed: ParsedResponse
    rejected_attempts: list[dict[str, Any]]
    latency_seconds: float


class BranchExecutor:
    def __init__(
        self,
        ipfs: IpfsClient,
        *,
        llm_base_url: str | None = None,
        llm_model_name: str | None = None,
        client: Any | None = None,
        zkvm_host: str | None = None,
        zkvm_timeout_seconds: float | None = None,
    ):
        self.ipfs = ipfs
        # Proving is minutes of CPU, so it is opt-in per worker. A worker with the
        # binary configured proves every branch and fails the branch closed when
        # proving fails; a worker without it publishes no receipt and says so.
        self.zkvm_host = zkvm_host if zkvm_host is not None else branch_receipt.host_binary()
        self.zkvm_timeout_seconds = zkvm_timeout_seconds if zkvm_timeout_seconds is not None else branch_receipt.timeout_seconds()
        self.llm_base_url = llm_base_url or os.environ.get("LLM_BASE_URL")
        self.llm_model_name = llm_model_name or os.environ.get("LLM_MODEL_NAME")
        if not self.llm_base_url:
            raise LlmEndpointError("LLM_ENDPOINT_UNREACHABLE: LLM_BASE_URL is unset", retryable=False)
        if not self.llm_model_name:
            raise LlmEndpointError("LLM_ENDPOINT_UNREACHABLE: LLM_MODEL_NAME is unset", retryable=False)
        self.llm_max_tokens = int(os.environ.get("LLM_MAX_TOKENS", str(DEFAULT_LLM_MAX_TOKENS)))
        if self.llm_max_tokens <= 0:
            raise ValueError("LLM_MAX_TOKENS must be positive")
        self.invalid_output_retries = int(os.environ.get("LLM_INVALID_OUTPUT_RETRIES", str(DEFAULT_INVALID_OUTPUT_RETRIES)))
        if self.invalid_output_retries < 0:
            raise ValueError("LLM_INVALID_OUTPUT_RETRIES must not be negative")
        self.client = client or LLMClient(
            api_key=os.environ.get("LLM_API_KEY") or "local-llm",
            base_url=self.llm_base_url,
            model=self.llm_model_name,
        )

    def check_endpoint(self) -> None:
        """Cheap liveness probe: list models. Raises LlmEndpointError when the endpoint is down."""

        try:
            self.client.client.models.list()
        except openai.APIStatusError as exc:
            raise LlmEndpointError(
                f"LLM_ENDPOINT_UNREACHABLE: {self.llm_base_url} returned HTTP {exc.status_code}",
                retryable=exc.status_code in RETRYABLE_HTTP_STATUSES,
            ) from exc
        except openai.APIConnectionError as exc:
            raise LlmEndpointError(f"LLM_ENDPOINT_UNREACHABLE: {self.llm_base_url}: {exc}", retryable=True) from exc

    def llm_version_preimage(self, branch_input: BranchInput) -> dict[str, Any]:
        """Everything that fixes the model's behaviour, and nothing that does not.

        The endpoint URL is deliberately absent: two hosts serving the same model
        with the same parameters and prompt must agree on the version hash.
        """

        return {
            "model": self.llm_model_name,
            "provider": "openai-compatible",
            "response_format": "json_object",
            "temperature": 0,
            "max_tokens": self.llm_max_tokens,
            "num_ctx": branch_input.parameters.get("num_ctx"),
            "system_prompt_sha256": system_prompt_sha256(),
        }

    def llm_version_hash(self, branch_input: BranchInput) -> str:
        return hashlib.sha256(canonical_json_bytes(self.llm_version_preimage(branch_input))).hexdigest()

    def execute(self, job_pubkey: str, branch_input: BranchInput, *, verifier_reference: BranchOutput | None = None) -> BranchExecution:
        """Run one branch. Returns only when the model produced a valid, committed output.

        With `verifier_reference`, this is a verifier re-execution: the output is
        bound to the worker's transcript CID so the canonical hash is comparable
        with the worker's receipt, and no new transcript is pinned.
        """

        oasis_trace = self._run_oasis_if_requested(branch_input)
        messages = self._messages(branch_input, oasis_trace)
        llm_seed = normalize_llm_seed(branch_input.rng_seed)
        extra_body = self._extra_body(branch_input)
        answer = self._ask_model(branch_input, messages, llm_seed, extra_body)
        parsed_output = self._build_output(branch_input, answer.parsed, verifier_reference=verifier_reference)
        transcript = {
            "schema_version": 2,
            "job": job_pubkey,
            "branch_input_sha256": hashlib.sha256(canonical_json_bytes(branch_input)).hexdigest(),
            "llm": {
                "base_url": self.llm_base_url,
                "model": self.llm_model_name,
                "temperature": 0,
                "max_tokens": self.llm_max_tokens,
                "branch_rng_seed": branch_input.rng_seed,
                "request_seed": llm_seed,
                "extra_body": extra_body,
                "latency_seconds": answer.latency_seconds,
                "usage": {
                    "completion_tokens": answer.result.completion_tokens,
                    "prompt_tokens": answer.result.prompt_tokens,
                    "total_tokens": answer.result.total_tokens,
                },
                "version_preimage": self.llm_version_preimage(branch_input),
                "version_hash": parsed_output.llm_version_hash,
                "system_prompt_sha256": system_prompt_sha256(),
            },
            "messages": messages,
            "raw_content": answer.result.raw_content,
            "raw_response": answer.result.payload,
            "rejected_attempts": answer.rejected_attempts,
            "attempts": len(answer.rejected_attempts) + 1,
            "oasis_trace": oasis_trace,
            "completed_at_unix": int(time.time()),
        }
        transcript_cid = verifier_reference.transcript_cid if verifier_reference else self.ipfs.add_json(
            f"branch-{branch_input.branch_index}-transcript.json",
            transcript,
        )
        output = parsed_output.model_copy(update={"transcript_cid": transcript_cid, "completed_at_unix": int(time.time())})
        receipt_cid: str | None = None
        receipt_seconds: float | None = None
        if self.zkvm_host and verifier_reference is None:
            receipt_cid, receipt_seconds = self._prove_branch_receipt(branch_input, output)
            output = output.model_copy(update={"zkvm_receipt_cid": receipt_cid})
        output_cid = self.ipfs.add_json(f"branch-{branch_input.branch_index}-output.json", output.model_dump(mode="json", exclude_none=False))
        result_bytes = branch_output_result_bytes(output)
        return BranchExecution(
            output,
            output_cid,
            result_bytes,
            transcript_cid,
            transcript,
            answer.latency_seconds,
            answer.result.completion_tokens,
            answer.result.prompt_tokens,
            answer.result.total_tokens,
            len(answer.rejected_attempts) + 1,
            receipt_cid,
            receipt_seconds,
        )

    def _prove_branch_receipt(self, branch_input: BranchInput, output: BranchOutput) -> tuple[str, float]:
        """Prove that this document encodes to the receipt this branch will submit.

        The guest is shown the document without `zkvm_receipt_cid`, which is set from
        the CID this returns. A failure raises: the branch fails closed and submits
        nothing, rather than settling a receipt nothing proved.
        """

        started = time.time()
        try:
            bundle = branch_receipt.prove(
                self.zkvm_host,
                branch_input.model_dump(mode="json", exclude_none=False),
                output.model_dump(mode="json", exclude_none=False),
                timeout=self.zkvm_timeout_seconds,
            )
        except branch_receipt.BranchReceiptError as exc:
            raise BranchReceiptFailed(f"branch receipt proving failed: {exc}") from exc
        elapsed = time.time() - started
        expected = branch_receipt.expected_journal(
            branch_input.model_dump(mode="json", exclude_none=False),
            output.model_dump(mode="json", exclude_none=False),
            branch_result_hash(output),
        )
        journal = branch_receipt.ReceiptJournal.from_json(bundle["journal"])
        if journal != expected:
            raise BranchReceiptFailed(
                f"the receipt this worker just produced does not describe its own output: "
                f"journal={journal.to_json()} expected={expected.to_json()}"
            )
        cid = self.ipfs.add_json(f"branch-{branch_input.branch_index}-zkvm-receipt.json", bundle)
        LOGGER.info(
            "branch %s receipt proved in %.1fs image_id=%s cid=%s",
            branch_input.branch_index,
            elapsed,
            bundle["image_id_hex"],
            cid,
        )
        return cid, elapsed

    def _ask_model(
        self,
        branch_input: BranchInput,
        messages: list[dict[str, str]],
        llm_seed: int,
        extra_body: dict[str, Any] | None,
    ) -> _ModelAnswer:
        """Send the identical request until the answer parses, at most `retries + 1` times."""

        rejected: list[dict[str, Any]] = []
        total_latency = 0.0
        for attempt in range(1, self.invalid_output_retries + 2):
            started = time.time()
            try:
                result = self._request(messages, llm_seed, extra_body)
            except ValueError as exc:
                total_latency += time.time() - started
                rejected.append({"attempt": attempt, "error": f"model output is not a JSON object: {exc}", "raw_content": None})
                continue
            total_latency += time.time() - started
            try:
                parsed = parse_model_response(branch_input, result.payload)
            except InvalidModelOutputError as exc:
                rejected.append({"attempt": attempt, "error": str(exc), "raw_content": result.raw_content})
                continue
            return _ModelAnswer(result, parsed, rejected, total_latency)
        raise ModelOutputRejectedError(
            f"model output rejected after {len(rejected)} attempt(s) with seed {llm_seed}: {rejected[-1]['error']}",
            rejected,
        )

    def _request(self, messages: list[dict[str, str]], llm_seed: int, extra_body: dict[str, Any] | None) -> LLMJsonResult:
        try:
            return self.client.chat_json_with_metadata(
                messages,
                temperature=0.0,
                max_tokens=self.llm_max_tokens,
                seed=llm_seed,
                extra_body=extra_body,
            )
        except openai.APIStatusError as exc:
            raise LlmEndpointError(
                f"LLM endpoint {self.llm_base_url} returned HTTP {exc.status_code}: {exc.message}",
                retryable=exc.status_code in RETRYABLE_HTTP_STATUSES,
            ) from exc
        except openai.APIConnectionError as exc:
            raise LlmEndpointError(f"LLM endpoint {self.llm_base_url} unreachable: {exc}", retryable=True) from exc

    def _build_output(
        self,
        branch_input: BranchInput,
        parsed: ParsedResponse,
        *,
        verifier_reference: BranchOutput | None,
    ) -> BranchOutput:
        common = {
            "parent_job": branch_input.parent_job,
            "branch_index": branch_input.branch_index,
            "rng_seed": branch_input.rng_seed,
            "llm_model": self.llm_model_name or "",
            "llm_version_hash": self.llm_version_hash(branch_input),
            "completed_at_unix": int(time.time()),
            "transcript_cid": verifier_reference.transcript_cid if verifier_reference else "pending",
        }
        if isinstance(parsed, ParsedCategorical):
            return BranchOutput(output_kind="categorical", categorical_label_index=parsed.label_index, **common)
        if isinstance(parsed, ParsedNarrative):
            return BranchOutput(
                output_kind="narrative_with_scalar",
                scalar_value_bps=parsed.scalar_value_bps,
                narrative_text=parsed.narrative_text,
                narrative_scores=parsed.narrative_scores,
                **common,
            )
        if isinstance(parsed, ParsedScalar):
            return BranchOutput(
                output_kind="scalar",
                scalar_value_bps=parsed.scalar_value_bps,
                scalar_confidence_lower_bps=parsed.confidence_lower_bps,
                scalar_confidence_upper_bps=parsed.confidence_upper_bps,
                narrative_text=parsed.narrative_text,
                **common,
            )
        raise TypeError(f"unsupported parsed response: {type(parsed).__name__}")

    def _extra_body(self, branch_input: BranchInput) -> dict[str, Any] | None:
        num_ctx = branch_input.parameters.get("num_ctx")
        if num_ctx is None:
            return None
        value = int(num_ctx)
        if value <= 0:
            raise ValueError("num_ctx must be positive")
        return {"options": {"num_ctx": value}}

    def _messages(self, branch_input: BranchInput, oasis_trace: dict[str, Any] | None) -> list[dict[str, str]]:
        labels = branch_input.parameters.get("labels", [])
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": branch_input.seed,
                        "branch_index": branch_input.branch_index,
                        "rng_seed": branch_input.rng_seed,
                        "target_output_kind": branch_input.target_output_kind,
                        "parameters": branch_input.parameters,
                        "labels": labels,
                        "oasis_trace": oasis_trace,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]

    def _run_oasis_if_requested(self, branch_input: BranchInput) -> dict[str, Any] | None:
        simulation_id = branch_input.parameters.get("oasis_simulation_id")
        if not simulation_id or not branch_input.parameters.get("run_oasis", False):
            return None
        try:
            # The OASIS engine ships with `kswarm-worker[engine]` (the full backend).
            # Container images carry only the protocol schemas and the LLM client.
            from app.services.simulation_runner import RunnerStatus, SimulationRunner
        except ImportError as exc:
            raise OasisEngineUnavailable(
                "branch requests an OASIS simulation but the engine is not installed; "
                "install kswarm-worker[engine] or run this branch outside the container image"
            ) from exc

        SimulationRunner.start_simulation(
            simulation_id,
            platform=str(branch_input.parameters.get("oasis_platform", "parallel")),
            max_rounds=int(branch_input.parameters.get("oasis_max_rounds", 1)),
        )
        deadline = time.time() + float(branch_input.parameters.get("oasis_timeout_seconds", 600))
        while time.time() < deadline:
            current = SimulationRunner.get_run_state(simulation_id)
            if current and current.runner_status in {RunnerStatus.COMPLETED, RunnerStatus.FAILED, RunnerStatus.STOPPED}:
                return current.to_detail_dict()
            time.sleep(2)
        raise TimeoutError(f"OASIS simulation timed out: {simulation_id}")
