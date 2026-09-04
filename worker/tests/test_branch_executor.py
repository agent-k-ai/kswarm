from __future__ import annotations

import hashlib
import json

import httpx
import openai
import pytest

from app.protocol.branch_schemas import BranchInput
from app.protocol.canonical_hash import branch_output_result_bytes
from branch_worker import executor as executor_module
from branch_worker.executor import (
    DEFAULT_LLM_MAX_TOKENS,
    LLM_SEED_MODULUS,
    SYSTEM_PROMPT,
    BranchExecutor,
    LlmEndpointError,
    ModelOutputRejectedError,
    normalize_llm_seed,
    system_prompt_sha256,
)
from fakes import FakeIpfs, StubLlmClient


VALID_SCALAR = {"scalar_value": 0.73, "confidence_lower": 0.6, "confidence_upper": 0.8, "rationale": "evidence"}


def test_normalize_llm_seed_preserves_small_seeds() -> None:
    assert normalize_llm_seed(42) == 42


def test_normalize_llm_seed_maps_u64_into_signed_local_llm_range() -> None:
    seed = 2**64 - 1
    normalized = normalize_llm_seed(seed)

    assert normalized == seed % LLM_SEED_MODULUS
    assert 0 <= normalized < LLM_SEED_MODULUS


def test_num_ctx_extra_body_maps_to_ollama_options() -> None:
    branch_input = BranchInput(
        parent_job="parent",
        branch_index=0,
        seed="question",
        parameters={"num_ctx": 16384},
        rng_seed=1,
        target_output_kind="scalar",
        scalar_grid_bps=1,
    )
    executor = object.__new__(BranchExecutor)

    assert executor._extra_body(branch_input) == {"options": {"num_ctx": 16384}}


def _scalar_branch_input(**parameters: object) -> BranchInput:
    return BranchInput(
        parent_job="parent",
        branch_index=0,
        seed="Will the event happen?",
        parameters=parameters,
        rng_seed=2**40 + 5,
        target_output_kind="scalar",
        scalar_grid_bps=1,
    )


def _executor(scripted: list, *, base_url: str = "http://llm-a.test/v1", **kwargs) -> tuple[BranchExecutor, StubLlmClient, FakeIpfs]:
    ipfs = FakeIpfs()
    client = StubLlmClient(scripted, **kwargs)
    return BranchExecutor(ipfs, llm_base_url=base_url, llm_model_name="stub-model", client=client), client, ipfs


def test_scalar_prompt_does_not_anchor_every_answer_to_half() -> None:
    """Guard the flat-0.5 discrimination fix in the scalar system prompt.

    The shipped guidance said to "Use 0.0 for impossible, 0.5 for unknown or
    ambiguous, and 1.0 for certain." Both the local 3B instruct model and the
    strong reasoning model latched onto that 0.5 anchor and returned 0.5 for
    every input (zero discrimination). The corrected guidance must drop that
    default-to-0.5 anchor, instruct estimation from the evidence, reserve ~0.5
    only for genuinely balanced/absent evidence, and still preserve the scalar
    JSON key contract. Worded so that reintroducing the anchor (or dropping the
    anti-default instruction) is a deliberate, visible change, not a silent
    regression.
    """
    executor = object.__new__(BranchExecutor)

    system_prompt = executor._messages(_scalar_branch_input(), None)[0]["content"]

    assert system_prompt == SYSTEM_PROMPT
    assert "0.5 for unknown or ambiguous" not in system_prompt
    assert "do not default to 0.5" in system_prompt
    for key in ("scalar_value", "confidence_lower", "confidence_upper", "rationale"):
        assert key in system_prompt


def test_llm_max_tokens_defaults_and_is_env_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_tokens must default to a reasoning-appropriate budget, env-overridable.

    Reasoning models (vLLM Qwen3 MoE) spend thousands of tokens on hidden
    reasoning before the final JSON; small constants (the old 1200/2048)
    truncated them to an empty/partial content field. The default is therefore
    large (a cap, not a target); LLM_MAX_TOKENS still overrides per deployment.
    """
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    default_executor = BranchExecutor(object(), llm_base_url="http://x/v1", llm_model_name="m")
    assert default_executor.llm_max_tokens == DEFAULT_LLM_MAX_TOKENS == 12000

    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    tuned_executor = BranchExecutor(object(), llm_base_url="http://x/v1", llm_model_name="m")
    assert tuned_executor.llm_max_tokens == 2048

    monkeypatch.setenv("LLM_MAX_TOKENS", "0")
    with pytest.raises(ValueError):
        BranchExecutor(object(), llm_base_url="http://x/v1", llm_model_name="m")


def test_invalid_output_retries_are_env_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_INVALID_OUTPUT_RETRIES", "0")
    executor, client, _ = _executor([{"garbage": True}])
    assert executor.invalid_output_retries == 0
    with pytest.raises(ModelOutputRejectedError):
        executor.execute("job", _scalar_branch_input())
    assert len(client.calls) == 1

    monkeypatch.setenv("LLM_INVALID_OUTPUT_RETRIES", "-1")
    with pytest.raises(ValueError):
        _executor([])


def test_execute_commits_only_parsed_model_values() -> None:
    executor, client, ipfs = _executor([VALID_SCALAR])
    branch_input = _scalar_branch_input()

    execution = executor.execute("job-pubkey", branch_input)

    assert execution.output.scalar_value_bps == 7300
    assert execution.output.scalar_confidence_lower_bps == 6000
    assert execution.output.scalar_confidence_upper_bps == 8000
    assert execution.output.narrative_text == "evidence"
    assert execution.output.llm_model == "stub-model"
    assert execution.output.llm_version_hash == executor.llm_version_hash(branch_input)
    assert execution.result_bytes == branch_output_result_bytes(execution.output)
    assert execution.attempts == 1
    assert client.calls[0]["seed"] == normalize_llm_seed(branch_input.rng_seed)
    assert client.calls[0]["temperature"] == 0.0
    assert ipfs.cat_json(execution.output_cid)["scalar_value_bps"] == 7300
    transcript = ipfs.cat_json(execution.transcript_cid)
    assert transcript["rejected_attempts"] == []
    assert transcript["llm"]["system_prompt_sha256"] == system_prompt_sha256()
    assert transcript["llm"]["version_hash"] == execution.output.llm_version_hash


def test_invalid_output_is_retried_with_the_identical_request() -> None:
    executor, client, ipfs = _executor([{"scalar_value": 5000}, ValueError("not json"), VALID_SCALAR])
    branch_input = _scalar_branch_input()

    execution = executor.execute("job-pubkey", branch_input)

    assert execution.attempts == 3
    assert execution.output.scalar_value_bps == 7300
    seeds = {call["seed"] for call in client.calls}
    assert seeds == {normalize_llm_seed(branch_input.rng_seed)}
    assert all(call["messages"] == client.calls[0]["messages"] for call in client.calls)
    rejected = execution.transcript["rejected_attempts"]
    assert [item["attempt"] for item in rejected] == [1, 2]
    assert "scalar_value must lie in [0, 1]" in rejected[0]["error"]
    assert rejected[0]["raw_content"] == json.dumps({"scalar_value": 5000}, sort_keys=True)
    assert "not a JSON object" in rejected[1]["error"]
    assert ipfs.uploads == 2


def test_persistently_invalid_output_raises_and_submits_nothing() -> None:
    executor, client, ipfs = _executor([{"scalar_value": "high"}, {"probability": 1.7}, {"rationale": "no number"}])

    with pytest.raises(ModelOutputRejectedError) as excinfo:
        executor.execute("job-pubkey", _scalar_branch_input())

    assert len(excinfo.value.attempts) == 3
    assert len(client.calls) == 3
    assert ipfs.uploads == 0


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://llm-a.test/v1/chat/completions")
    return openai.APIStatusError("boom", response=httpx.Response(status, request=request), body=None)


def test_endpoint_errors_are_typed_by_retryability() -> None:
    request = httpx.Request("POST", "http://llm-a.test/v1/chat/completions")
    for error, retryable in ((openai.APIConnectionError(request=request), True), (_status_error(503), True), (_status_error(429), True), (_status_error(400), False)):
        executor, _, ipfs = _executor([error])
        with pytest.raises(LlmEndpointError) as excinfo:
            executor.execute("job-pubkey", _scalar_branch_input())
        assert excinfo.value.retryable is retryable
        assert ipfs.uploads == 0


def test_check_endpoint_maps_connection_failures() -> None:
    request = httpx.Request("GET", "http://llm-a.test/v1/models")
    executor, _, _ = _executor([], models_error=openai.APIConnectionError(request=request))
    with pytest.raises(LlmEndpointError) as excinfo:
        executor.check_endpoint()
    assert excinfo.value.retryable is True

    healthy, _, _ = _executor([])
    healthy.check_endpoint()


def test_llm_version_hash_ignores_the_endpoint_and_binds_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    branch_input = _scalar_branch_input(num_ctx=8192)
    host_a, _, _ = _executor([], base_url="http://llm-a.test/v1")
    host_b, _, _ = _executor([], base_url="http://llm-b.test:8000/v1")

    assert host_a.llm_version_hash(branch_input) == host_b.llm_version_hash(branch_input)
    preimage = host_a.llm_version_preimage(branch_input)
    assert "base_url" not in preimage
    assert preimage["system_prompt_sha256"] == hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert preimage["model"] == "stub-model"
    assert preimage["max_tokens"] == host_a.llm_max_tokens
    assert preimage["num_ctx"] == 8192

    before = host_a.llm_version_hash(branch_input)
    monkeypatch.setattr(executor_module, "SYSTEM_PROMPT", SYSTEM_PROMPT + " Always answer 0.5.")
    assert host_a.llm_version_hash(branch_input) != before

    monkeypatch.setattr(executor_module, "SYSTEM_PROMPT", SYSTEM_PROMPT)
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    smaller_budget, _, _ = _executor([])
    assert smaller_budget.llm_version_hash(branch_input) != before


def test_verifier_reference_binds_the_worker_transcript_without_pinning_a_new_one() -> None:
    worker, _, _ = _executor([VALID_SCALAR])
    branch_input = _scalar_branch_input()
    worker_execution = worker.execute("job-pubkey", branch_input)

    verifier, _, verifier_ipfs = _executor([VALID_SCALAR], base_url="http://llm-b.test/v1")
    verifier_execution = verifier.execute("job-pubkey", branch_input, verifier_reference=worker_execution.output)

    assert verifier_execution.transcript_cid == worker_execution.transcript_cid
    assert verifier_execution.result_bytes == worker_execution.result_bytes
    assert verifier_ipfs.uploads == 1


def test_oasis_branch_fails_clearly_without_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container images ship no `app.services`; a branch that asks for OASIS says what is missing."""

    import builtins

    real_import = builtins.__import__

    def no_engine(name, *args, **kwargs):
        if name == "app.services.simulation_runner":
            raise ImportError("No module named 'app.services'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_engine)
    executor = BranchExecutor(object(), llm_base_url="http://x/v1", llm_model_name="m")
    with pytest.raises(executor_module.OasisEngineUnavailable, match=r"kswarm-worker\[engine\]"):
        executor._run_oasis_if_requested(_scalar_branch_input(oasis_simulation_id="sim-1", run_oasis=True))
    assert executor._run_oasis_if_requested(_scalar_branch_input()) is None
