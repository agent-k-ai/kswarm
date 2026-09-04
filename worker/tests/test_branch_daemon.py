from __future__ import annotations

import hashlib
import logging

import pytest
from solders.pubkey import Pubkey

from app.protocol.branch_schemas import BranchInput
from branch_worker.daemon import BranchWorkerDaemon, ClaimCircuitBreaker, backoff_seconds
from branch_worker.executor import BranchExecutor, LlmEndpointError
from fakes import FakeIpfs, FakeJob, FakeProtocol, StubLlmClient, make_config
from kswarm_cli.constants import JOB_STATUS_BY_NAME


VALID_SCALAR = {"scalar_value": 0.31, "confidence_lower": 0.2, "confidence_upper": 0.4, "rationale": "evidence"}


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _branch_input(index: int = 0) -> BranchInput:
    return BranchInput(parent_job="parent", branch_index=index, seed="Will it rain?", parameters={}, rng_seed=7 + index, target_output_kind="scalar")


def _daemon(scripted: list, *, jobs: int = 1, active_claims: int = 0, max_claims: int = 1, ipfs_healthy: bool = True, execution_window: int = 3600, models_error: Exception | None = None):
    ipfs = FakeIpfs()
    keys = [Pubkey.new_unique() for _ in range(jobs)]
    job_table = {}
    for index, key in enumerate(keys):
        cid = ipfs.add_bytes("input", _branch_input(index).model_dump_json().encode("utf-8"))
        job_table[key] = FakeJob(input_cid=cid)
    ipfs.uploads = 0
    ipfs.healthy = ipfs_healthy
    clock = FakeClock()
    protocol = FakeProtocol(job_table, active_claims=active_claims, execution_window=execution_window, now=clock.now)
    client = StubLlmClient(scripted, models_error=models_error)
    config = make_config(max_concurrent_claims=max_claims)
    executor = BranchExecutor(ipfs, llm_base_url=config.llm_base_url, llm_model_name=config.llm_model_name, client=client)
    daemon = BranchWorkerDaemon(config, protocol=protocol, ipfs=ipfs, executor=executor, clock=clock, sleep=clock.sleep)
    return daemon, protocol, client, clock, keys


def test_backoff_doubles_and_caps() -> None:
    assert [backoff_seconds(n, 5.0, 60.0) for n in range(1, 7)] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    with pytest.raises(ValueError):
        backoff_seconds(0, 5.0, 60.0)


def test_breaker_opens_for_the_cooldown_only() -> None:
    breaker = ClaimCircuitBreaker(cooldown_seconds=300)
    assert not breaker.is_open(1000)
    breaker.trip(1000)
    assert breaker.is_open(1299)
    assert breaker.remaining(1200) == 100
    assert not breaker.is_open(1300)


def test_success_path_submits_and_logs_the_result_hash(caplog: pytest.LogCaptureFixture) -> None:
    daemon, protocol, _, _, keys = _daemon([VALID_SCALAR])
    with caplog.at_level(logging.INFO, logger="kswarm.branch_worker"):
        assert daemon.run_once() == 1

    job_key, output_cid, result_bytes = protocol.receipts[0]
    assert job_key == keys[0]
    assert protocol.jobs_by_key[job_key].status == JOB_STATUS_BY_NAME["completed"]
    assert f"result_hash={hashlib.sha256(result_bytes).hexdigest()}" in caplog.text
    assert result_bytes.hex() not in caplog.text
    assert daemon.metrics.counters["jobs_succeeded"] == 1


def test_llm_outage_blocks_the_claim_before_any_stake_is_locked() -> None:
    import httpx
    import openai

    request = httpx.Request("GET", "http://llm.test/v1/models")
    daemon, protocol, _, _, _ = _daemon([VALID_SCALAR], models_error=openai.APIConnectionError(request=request))

    assert daemon.run_once() == 0
    assert protocol.claims == []
    assert daemon.metrics.counters["preflight_failures"] == 1


def test_ipfs_outage_blocks_the_claim() -> None:
    daemon, protocol, _, _, _ = _daemon([VALID_SCALAR], ipfs_healthy=False)

    assert daemon.run_once() == 0
    assert protocol.claims == []
    assert daemon.metrics.counters["preflight_failures"] == 1


def test_max_concurrent_claims_is_enforced_against_on_chain_active_claims() -> None:
    daemon, protocol, _, _, _ = _daemon([VALID_SCALAR], jobs=2, active_claims=1, max_claims=1)
    assert daemon.run_once() == 0
    assert protocol.claims == []

    daemon, protocol, _, _, keys = _daemon([VALID_SCALAR, VALID_SCALAR], jobs=2, active_claims=0, max_claims=1)
    assert daemon.run_once() == 1
    assert protocol.claims == [keys[0]]
    assert protocol.jobs_by_key[keys[1]].status == JOB_STATUS_BY_NAME["open"]

    daemon, protocol, _, _, keys = _daemon([VALID_SCALAR, VALID_SCALAR], jobs=2, active_claims=1, max_claims=3)
    assert daemon.run_once() == 2
    assert protocol.claims == keys


def test_unregistered_worker_cannot_claim() -> None:
    daemon, protocol, _, _, _ = _daemon([VALID_SCALAR])
    protocol.registered = False
    with pytest.raises(RuntimeError, match="not registered"):
        daemon.run_once()
    assert protocol.claims == []


def test_transient_failures_retry_with_backoff_until_the_deadline_margin(caplog: pytest.LogCaptureFixture) -> None:
    daemon, protocol, client, clock, keys = _daemon([], execution_window=600)
    client.scripted = [LlmEndpointError("down", retryable=True) for _ in range(50)]

    with caplog.at_level(logging.INFO, logger="kswarm.branch_worker"):
        assert daemon.run_once() == 0

    job = protocol.jobs_by_key[keys[0]]
    assert protocol.claims == [keys[0]]
    assert protocol.receipts == []
    assert job.status == JOB_STATUS_BY_NAME["claimed"]
    # Claimed at t=1000 with a 600s window: attempts may start until 1480 (deadline 1600 - 120 margin).
    # Backoff 5, 10, 20, 40 then 60 x6 reaches t=1435; the next wait would end at 1495 > 1480, so it stops.
    assert clock.sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0]
    assert clock.now + 60.0 > job.execute_deadline - daemon.config.execute_deadline_margin_seconds
    assert daemon.metrics.counters["execution_retries"] == len(clock.sleeps)
    assert daemon.metrics.counters["claims_abandoned"] == 1
    assert "branch job failed job=" in caplog.text
    assert "no on-chain release exists" in caplog.text
    assert f"stake at risk={job.required_stake} base units" in caplog.text
    assert daemon.breaker.is_open(clock.now)


def test_breaker_pauses_claiming_for_the_cooldown_after_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    daemon, protocol, client, clock, keys = _daemon([LlmEndpointError("bad request", retryable=False)], jobs=2)
    assert daemon.run_once() == 0
    assert protocol.claims == [keys[0]]
    assert clock.sleeps == []

    client.scripted = [VALID_SCALAR]
    with caplog.at_level(logging.WARNING, logger="kswarm.branch_worker"):
        assert daemon.run_once() == 0
    assert "claiming paused" in caplog.text
    assert protocol.claims == [keys[0]]
    assert daemon.metrics.counters["claim_pauses"] == 1

    clock.now += daemon.config.claim_cooldown_seconds
    protocol.active_claims = 0
    assert daemon.run_once() == 1
    assert protocol.claims == [keys[0], keys[1]]


def test_rejected_model_output_abandons_without_submitting() -> None:
    daemon, protocol, _, clock, keys = _daemon([{"scalar_value": 5000}, {"scalar_value": "high"}, {"junk": 1}])

    assert daemon.run_once() == 0
    assert protocol.claims == [keys[0]]
    assert protocol.receipts == []
    assert clock.sleeps == []
    assert daemon.metrics.counters["jobs_failed"] == 1
    assert daemon.breaker.is_open(clock.now)


def test_invalid_branch_input_abandons_without_retrying() -> None:
    daemon, protocol, _, clock, keys = _daemon([VALID_SCALAR])
    protocol.jobs_by_key[keys[0]].input_cid = daemon.ipfs.add_bytes("input", b'{"not": "a branch input"}')

    assert daemon.run_once() == 0
    assert protocol.receipts == []
    assert clock.sleeps == []
    assert daemon.breaker.is_open(clock.now)


def test_claim_race_moves_on_without_tripping_the_breaker() -> None:
    from kswarm_cli.rpc import RpcError

    daemon, protocol, _, clock, _ = _daemon([VALID_SCALAR])
    protocol.claim_error = RpcError("InvalidJobState", "claimed elsewhere")

    assert daemon.run_once() == 0
    assert daemon.metrics.counters["claim_races"] == 1
    assert not daemon.breaker.is_open(clock.now)


def test_expired_claim_window_and_wrong_class_are_skipped() -> None:
    daemon, protocol, _, clock, keys = _daemon([VALID_SCALAR], jobs=2)
    protocol.jobs_by_key[keys[0]].claim_deadline = int(clock.now) - 1
    protocol.jobs_by_key[keys[1]].job_class = 4

    assert daemon.run_once() == 0
    assert protocol.claims == []
