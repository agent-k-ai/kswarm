"""In-memory stand-ins for the chain, IPFS, and the LLM used by the worker unit tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.utils.llm_client import LLMJsonResult
from kswarm_cli.config import DEFAULT_CLUSTERS
from kswarm_cli.constants import CAPABILITY_CLASS, JOB_CLASS, JOB_STATUS_BY_NAME, NODE_ROLE, SOFTWARE_DIGEST, STAKE_TIER, ZERO_HASH
from kswarm_cli.rpc import RpcError
from solders.pubkey import Pubkey

from worker_common.config import WorkerConfig
from worker_common.ipfs import IpfsError


def make_config(**overrides: Any) -> WorkerConfig:
    values: dict[str, Any] = {
        "cluster": "local",
        "rpc_url": "http://rpc.test",
        "program_id": Pubkey.from_string(DEFAULT_CLUSTERS["local"]["program_id"]),
        "keypair_name": "worker-a",
        "wallet_file": None,
        "capabilities": (CAPABILITY_CLASS["worker-proof"],),
        "software_digest": SOFTWARE_DIGEST["worker-canonical"],
        "role": NODE_ROLE["worker-proof"],
        "tier": STAKE_TIER["T1"],
        "polling_interval_seconds": 0.0,
        "max_concurrent_claims": 1,
        "ipfs_api_url": "http://ipfs.test",
        "metrics_host": "127.0.0.1",
        "metrics_port": 0,
        "llm_base_url": "http://llm.test/v1",
        "llm_model_name": "stub-model",
        "challenge_on_mismatch": True,
        "claim_cooldown_seconds": 300.0,
        "execute_deadline_margin_seconds": 120.0,
        "execute_retry_initial_seconds": 5.0,
        "execute_retry_max_seconds": 60.0,
        "verifier_reexecute": True,
        "predict_runs_dir": Path("/nonexistent/predict_runs"),
    }
    values.update(overrides)
    return WorkerConfig(**values)


@dataclass
class FakeJob:
    status: int = JOB_STATUS_BY_NAME["open"]
    job_class: int = JOB_CLASS["branch-proof"]
    worker: Pubkey = field(default_factory=lambda: Pubkey.from_bytes(bytes(32)))
    claim_deadline: int = 10_000
    execute_deadline: int = 0
    challenge_deadline: int = 20_000
    required_stake: int = 50_000_000_000
    required_role: int = NODE_ROLE["worker-proof"]
    required_tier: int = STAKE_TIER["T1"]
    required_capability_class_hash: bytes = CAPABILITY_CLASS["worker-proof"]
    required_software_digest: bytes = SOFTWARE_DIGEST["worker-canonical"]
    input_cid: str = ""
    output_cid: str = ""
    input_bundle_hash: bytes = ZERO_HASH
    expected_result_hash: bytes = ZERO_HASH
    submitted_result_hash: bytes = ZERO_HASH
    result_bytes: bytes = b""
    verifier_authority: Pubkey | None = None
    verifier_attestation_hash: bytes | None = None
    assigned_verifier_authority: Pubkey | None = None


class FakeProtocol:
    """Records every instruction the daemons send and mutates job state like the program would."""

    def __init__(self, jobs: dict[Pubkey, FakeJob], *, active_claims: int = 0, registered: bool = True, execution_window: int = 3600, now: float = 1_000.0):
        self.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
        self.jobs_by_key = jobs
        self.active_claims = active_claims
        self.registered = registered
        self.execution_window = execution_window
        self.now = now
        self.claims: list[Pubkey] = []
        self.receipts: list[tuple[Pubkey, str, bytes]] = []
        self.attestations: list[tuple[Pubkey, bytes, str, bytes]] = []
        self.challenges: list[Pubkey] = []
        self.claim_error: RpcError | None = None
        self.challenge_error: RpcError | None = None

    def jobs(self):
        return list(self.jobs_by_key.items())

    def job(self, job_key: Pubkey):
        return self.jobs_by_key.get(job_key)

    def worker_account(self):
        if not self.registered:
            return None
        return SimpleNamespace(active_claims=self.active_claims)

    def claim_job(self, job_key: Pubkey) -> str:
        if self.claim_error is not None:
            raise self.claim_error
        job = self.jobs_by_key[job_key]
        if job.status != JOB_STATUS_BY_NAME["open"]:
            raise RpcError("InvalidJobState", "job is not open")
        job.status = JOB_STATUS_BY_NAME["claimed"]
        job.worker = self.wallet.pubkey
        job.execute_deadline = int(self.now) + self.execution_window
        self.active_claims += 1
        self.claims.append(job_key)
        return "claim-signature"

    def submit_receipt(self, job_key: Pubkey, output_cid: str, result_bytes: bytes) -> str:
        job = self.jobs_by_key[job_key]
        if job.status != JOB_STATUS_BY_NAME["claimed"]:
            raise RpcError("InvalidJobState", "job is not claimed")
        job.status = JOB_STATUS_BY_NAME["completed"]
        job.output_cid = output_cid
        job.result_bytes = result_bytes
        job.submitted_result_hash = hashlib.sha256(result_bytes).digest()
        self.receipts.append((job_key, output_cid, result_bytes))
        return "receipt-signature"

    def submit_attestation(self, job_key: Pubkey, result_bytes: bytes, evidence_cid: str, software_digest: bytes) -> str:
        job = self.jobs_by_key[job_key]
        if job.verifier_authority is not None:
            raise RpcError("AttestationAlreadyExists", "attested")
        job.verifier_authority = self.wallet.pubkey
        job.verifier_attestation_hash = hashlib.sha256(result_bytes).digest()
        self.attestations.append((job_key, result_bytes, evidence_cid, software_digest))
        return "attestation-signature"

    def challenge_job(self, job_key: Pubkey, job_account) -> str:
        if self.challenge_error is not None:
            raise self.challenge_error
        job = self.jobs_by_key[job_key]
        # The program accepts a challenge only from the verifier the customer or the
        # admin assigned to the job, for every job class (the H2-Interim rule).
        if job.assigned_verifier_authority is None:
            raise RpcError("ChallengeRequiresAssignedVerifier", "challenge requires a verifier assigned with assign_verifier")
        if job.assigned_verifier_authority != self.wallet.pubkey:
            raise RpcError("VerifierNotAssigned", "caller is not the assigned verifier")
        if job.verifier_attestation_hash is None or job.verifier_attestation_hash == job.submitted_result_hash:
            raise RpcError("ChallengeRejected", "receipt is not challengeable")
        job.status = JOB_STATUS_BY_NAME["slashed"]
        self.challenges.append(job_key)
        return "challenge-signature"


class FakeIpfs:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.healthy = True
        self.uploads = 0

    def check(self) -> None:
        if not self.healthy:
            raise IpfsError("IPFS_UNREACHABLE: fake")

    def add_bytes(self, filename: str, payload: bytes) -> str:
        if not self.healthy:
            raise IpfsError("IPFS_UNREACHABLE: fake")
        cid = "bafk" + hashlib.sha256(payload).hexdigest()[:40]
        self.objects[cid] = payload
        self.uploads += 1
        return cid

    def add_json(self, filename: str, payload: Any) -> str:
        return self.add_bytes(filename, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def cat_bytes(self, cid: str) -> bytes:
        if not self.healthy:
            raise IpfsError("IPFS_UNREACHABLE: fake")
        try:
            return self.objects[cid]
        except KeyError as exc:
            raise IpfsError(f"ipfs cat failed for {cid}") from exc

    def cat_json(self, cid: str) -> Any:
        return json.loads(self.cat_bytes(cid).decode("utf-8"))


class StubLlmClient:
    """Replays scripted model payloads. An Exception entry is raised instead of returned."""

    def __init__(self, scripted: list[Any], *, models_error: Exception | None = None) -> None:
        self.scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []
        self.models_error = models_error
        self.client = SimpleNamespace(models=SimpleNamespace(list=self._list_models))

    def _list_models(self) -> list[str]:
        if self.models_error is not None:
            raise self.models_error
        return ["stub-model"]

    def chat_json_with_metadata(self, messages, *, temperature, max_tokens, seed, extra_body):
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens, "seed": seed, "extra_body": extra_body})
        if not self.scripted:
            raise AssertionError("stub LLM has no scripted response left")
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        raw = json.dumps(item, sort_keys=True)
        return LLMJsonResult(payload=item, completion_tokens=11, prompt_tokens=7, total_tokens=18, raw_content=raw)
