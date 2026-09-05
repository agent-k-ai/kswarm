"""`predict open` / `resume` / `status` / `cancel` against a fake chain.

The fake interprets every instruction the commands send (`open_job`,
`commit_input_artifact`, `cancel_open_job`) by its Anchor discriminator and
keeps job accounts in memory, so the incremental run manifest, the nonce
collision guard, the Bonsol binding `predict bind-aggregate` writes, and the
interrupt/resume/cancel paths are all exercised without a validator or IPFS.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from typer import rich_utils
from typer.testing import CliRunner

from kswarm_cli import main as cli_main
from kswarm_cli.aggregate import aggregate_journal, build_aggregate_artifact
from kswarm_cli.bonsol import framed_input_digest
from kswarm_cli.constants import CAPABILITY_CLASS, JOB_CLASS, JOB_STATUS_BY_NAME, KAI_MAINNET_MINT, KSWARM_PROGRAM_ID, SOFTWARE_DIGEST, TOKEN_PROGRAM_ID, ZERO_HASH
from kswarm_cli.context import CliContext
from kswarm_cli.encoding import anchor_ix_discriminator, read_string
from kswarm_cli.prediction import JOB_CANCELLED, JOB_COMMITTED, JOB_DEFERRED, JOB_PLANNED, RUN_CANCELLED, RUN_OPEN, RUN_OPENING
from kswarm_cli.protocol import JobAccount, ProtocolAddresses
from kswarm_cli.reducer_image import AGGREGATE_REDUCER_IMAGE_ID
from kswarm_cli.rpc import RpcError


# The one program id, so a rotation cannot leave a test behind.
PROGRAM_ID = KSWARM_PROGRAM_ID
PROTO = ProtocolAddresses(PROGRAM_ID, KAI_MAINNET_MINT, TOKEN_PROGRAM_ID)
OPEN_JOB = anchor_ix_discriminator("open_job")
COMMIT_INPUT = anchor_ix_discriminator("commit_input_artifact")
CANCEL_OPEN = anchor_ix_discriminator("cancel_open_job")


def _job_account(nonce: int, customer: Pubkey, data: bytes) -> JobAccount:
    """A `Job` as `open_job` would write it, from the instruction data the CLI sent."""

    return JobAccount(
        bump=1,
        nonce=nonce,
        customer=customer,
        worker=Pubkey.default(),
        status=JOB_STATUS_BY_NAME["awaiting-artifact"],
        reward_amount=struct.unpack_from("<Q", data, 80)[0],
        required_stake=struct.unpack_from("<Q", data, 88)[0],
        job_class=data[96],
        required_role=data[97],
        required_tier=data[98],
        required_capability_class_hash=data[99:131],
        required_software_digest=data[131:163],
        created_at=0,
        claim_deadline=0,
        execution_window_seconds=struct.unpack_from("<I", data, 167)[0],
        execute_deadline=0,
        challenge_window_seconds=struct.unpack_from("<I", data, 171)[0],
        challenge_deadline=0,
        challenge_bond=struct.unpack_from("<Q", data, 175)[0],
        challenger=Pubkey.default(),
        slash_settled=False,
        escrow_refunded=False,
        verifier_reward_paid=False,
        customer_slash_paid=False,
        input_bundle_hash=data[16:48],
        expected_result_hash=data[48:80],
        submitted_result_hash=ZERO_HASH,
        input_cid="",
        output_cid="",
        result_bytes=b"",
        verifier_authority=None,
        verifier_attestation_hash=None,
        verifier_evidence_cid=None,
        verifier_attestation_unix=None,
        assigned_verifier_authority=None,
        assigned_verifier_unix=None,
        reassignment_counter=0,
    )


class FakeChain:
    def __init__(self) -> None:
        self.jobs: dict[str, JobAccount] = {}
        self.opens = 0
        self.fail_after_opens: int | None = None
        self.signatures = 0
        self.ipfs: dict[str, bytes] = {}

    # --- what the CLI monkeypatches call ---
    def sign_and_send(self, rpc: Any, payer: Keypair, instructions: list[Any], extra_signers: Any = None) -> str:
        for ix in instructions:
            data = bytes(ix.data)
            disc = data[:8]
            if disc == OPEN_JOB:
                if self.fail_after_opens is not None and self.opens >= self.fail_after_opens:
                    raise RpcError("InjectedRpcFailure", f"injected failure after {self.opens} opens")
                job_key = str(ix.accounts[3].pubkey)
                nonce = struct.unpack_from("<Q", data, 8)[0]
                assert job_key not in self.jobs, "open_job on an existing PDA"
                self.jobs[job_key] = _job_account(nonce, ix.accounts[0].pubkey, data)
                self.opens += 1
            elif disc == COMMIT_INPUT:
                job_key = str(ix.accounts[1].pubkey)
                job = self.jobs[job_key]
                assert job.status == JOB_STATUS_BY_NAME["awaiting-artifact"]
                cid, _ = read_string(data, 8)
                self.jobs[job_key] = _replace(job, status=JOB_STATUS_BY_NAME["open"], input_cid=cid)
            elif disc == CANCEL_OPEN:
                job_key = str(ix.accounts[3].pubkey)
                job = self.jobs[job_key]
                assert job.status in {JOB_STATUS_BY_NAME["awaiting-artifact"], JOB_STATUS_BY_NAME["open"]}
                self.jobs[job_key] = _replace(job, status=JOB_STATUS_BY_NAME["cancelled"])
            else:
                raise AssertionError(f"unexpected instruction {disc.hex()}")
        self.signatures += 1
        return f"sig{self.signatures}"

    def fetch_job(self, rpc: Any, key: Pubkey) -> JobAccount | None:
        return self.jobs.get(str(key))

    def get_multiple_account_infos(self, keys: list[str]) -> list[dict | None]:
        return [{"owner": str(PROGRAM_ID)} if key in self.jobs else None for key in keys]

    def ipfs_add(self, url: str, filename: str, payload: bytes) -> str:
        cid = "bafy" + hashlib.sha256(payload).hexdigest()[:20]
        self.ipfs[cid] = payload
        return cid

    def ipfs_cat_json(self, url: str, cid: str) -> Any:
        """`predict bind-aggregate` reads the pinned aggregate plan back before it binds."""

        if cid not in self.ipfs:
            raise RuntimeError(f"404 Not Found for {cid}")
        return json.loads(self.ipfs[cid].decode("utf-8"))


def _replace(job: JobAccount, **changes: Any) -> JobAccount:
    values = {name: getattr(job, name) for name in JobAccount.__dataclass_fields__}
    values.update(changes)
    return JobAccount(**values)


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeChain:
    fake = FakeChain()
    wallets = {"customer": Keypair(), "other": Keypair()}
    context = CliContext(
        cluster_name="local",
        rpc_url="http://fake-rpc",
        commitment="confirmed",
        keypair_path=None,
        json_output=True,
        console=Console(),
        cluster_config={"name": "local", "rpc_url": "http://fake-rpc", "program_id": str(PROGRAM_ID)},
    )
    monkeypatch.setattr(cli_main.CliContext, "load", classmethod(lambda cls, **kwargs: context))
    monkeypatch.setattr(cli_main, "PREDICT_RUNS_DIR", tmp_path / "predict_runs")
    monkeypatch.setattr(cli_main, "_rpc", lambda ctx: fake)
    monkeypatch.setattr(cli_main, "_payment_context", lambda ctx, rpc: (PROTO, 6))
    monkeypatch.setattr(cli_main, "sign_and_send", fake.sign_and_send)
    monkeypatch.setattr(cli_main, "fetch_job", fake.fetch_job)
    monkeypatch.setattr(cli_main, "ipfs_check", lambda url: None)
    monkeypatch.setattr(cli_main, "ipfs_add_bytes", fake.ipfs_add)
    monkeypatch.setattr(cli_main, "_ipfs_cat_json", fake.ipfs_cat_json)
    monkeypatch.setattr(cli_main, "random_base_nonce", lambda branches: 1_000)

    def load_wallet(name: str) -> SimpleNamespace:
        keypair = wallets[name]
        return SimpleNamespace(name=name, pubkey=keypair.pubkey(), keypair=keypair, path=tmp_path / f"{name}.json")

    monkeypatch.setattr(cli_main, "load_wallet", load_wallet)
    fake.customer = wallets["customer"].pubkey()  # type: ignore[attr-defined]
    return fake


runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(cli_main.app, ["--json", *args])


def _payload(result) -> dict[str, Any]:
    assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return json.loads(result.stdout)


# CSI and OSC escape sequences. Typer builds its error console with
# `force_terminal=True` whenever GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set
# (`typer.rich_utils.FORCE_TERMINAL`), so on a hosted runner the same message
# arrives coloured as well as boxed, and colour lands in the middle of a word.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _flat(output: str) -> str:
    """The message as text: Rich colours it and wraps it in a box, so undo both."""

    return " ".join(_ANSI.sub("", output).replace("\u2502", " ").split())


def _save_manifest(run_id: str, run: dict[str, Any]) -> None:
    (cli_main.PREDICT_RUNS_DIR / f"{run_id}.json").write_text(json.dumps(run), encoding="utf-8")


def _manifest(run_id: str) -> dict[str, Any]:
    return json.loads((cli_main.PREDICT_RUNS_DIR / f"{run_id}.json").read_text(encoding="utf-8"))


OPEN_ARGS = [
    "predict",
    "open",
    "--question",
    "Will it rain?",
    "--branches",
    "4",
    "--reward-per-branch",
    "1KAI",
    "--aggregator-reward",
    "2",
    "--branch-required-stake",
    "500",
    "--aggregate-required-stake",
    "700",
    "--challenge-window",
    "600",
    "--claim-window",
    "100",
    "--execution-window",
    "200",
    "--as",
    "customer",
]


def test_open_opens_the_branches_and_plans_the_aggregate(chain: FakeChain) -> None:
    result = _invoke(*OPEN_ARGS, "--combiner", "trimmed-mean", "--trim-bps", "2500")
    payload = _payload(result)
    run_id = payload["parent_run"]
    assert payload["base_nonce"] == 1_000
    assert payload["run_status"] == RUN_OPEN
    assert result.stderr.splitlines()[0] == f"parent_run={run_id} base_nonce=1000 run_manifest={cli_main.PREDICT_RUNS_DIR / (run_id + '.json')}"
    assert payload["combiner_parameters"] == {"trim_bps": 2500}

    run = _manifest(run_id)
    assert run["status"] == RUN_OPEN
    assert run["customer_wallet"] == "customer"
    assert [entry["nonce"] for entry in run["branch_jobs"]] == [1000, 1001, 1002, 1003]
    assert run["aggregate_nonce"] == 1004 and run["aggregate"]["nonce"] == 1004
    assert all(entry["status"] == JOB_COMMITTED for entry in run["branch_jobs"])
    assert run["parent_manifest"]["combiner_parameters"] == {"trim_bps": 2500}
    assert run["parent_manifest"]["aggregate_image_id"] == AGGREGATE_REDUCER_IMAGE_ID
    assert run["open_parameters"] == {"required_role": "worker-proof", "required_tier": "T1", "claim_window": 100, "execution_window": 200, "challenge_window": 600}

    # Every branch job is on chain with its input committed.
    assert len(chain.jobs) == 4
    for entry in run["branch_jobs"]:
        job = chain.jobs[entry["job"]]
        assert job.status == JOB_STATUS_BY_NAME["open"]
        assert job.input_cid == entry["input_cid"]
        assert job.input_bundle_hash == hashlib.sha256(chain.ipfs[entry["input_cid"]]).digest()
        assert job.expected_result_hash == ZERO_HASH
        assert job.required_software_digest == SOFTWARE_DIGEST["worker-canonical"]
        assert job.required_capability_class_hash == CAPABILITY_CLASS["worker-proof"]
        assert job.job_class == JOB_CLASS["branch-proof"]
        assert (job.reward_amount, job.required_stake, job.challenge_bond) == (1_000_000, 500_000_000, 500_000_000)
        assert (job.execution_window_seconds, job.challenge_window_seconds) == (200, 600)
        branch_input = json.loads(chain.ipfs[entry["input_cid"]])
        assert branch_input["parameters"]["combiner_parameters"] == {"trim_bps": 2500}

    # The aggregate job is planned, not opened. Its input artifact carries the branch
    # receipts, which do not exist until the branches run, and `open_job` fixes
    # `input_bundle_hash` and `expected_result_hash` for good.
    assert run["aggregate_job"] not in chain.jobs
    assert run["aggregate"]["status"] == JOB_DEFERRED
    assert run["aggregate"]["input_cid"] is None
    assert run["aggregate"]["input_bundle_hash"] is None
    assert run["aggregate"]["expected_result_hash"] is None
    assert run["aggregate"]["required_software_digest"] == AGGREGATE_REDUCER_IMAGE_ID
    assert run["aggregate_input_cid"] is None
    assert run["aggregate_image_id"] == AGGREGATE_REDUCER_IMAGE_ID
    assert run["bonsol"] == {
        "bound": False,
        "reason": "aggregate job is opened by predict bind-aggregate",
        "image_id": AGGREGATE_REDUCER_IMAGE_ID,
    }
    assert "predict bind-aggregate" in result.stderr

    # The plan is still pinned, as provenance: it names the branch jobs, the combiner
    # and the reducer image this run committed to before any branch ran.
    plan = json.loads(chain.ipfs[run["aggregate_plan_cid"]])
    assert plan["schema_version"] == 3
    assert plan["combiner"] == "trimmed-mean"
    assert plan["combiner_parameters"] == {"trim_bps": 2500}
    assert plan["bonsol"] == {"image_id": AGGREGATE_REDUCER_IMAGE_ID, "public_input": "input-artifact", "framing": "u64le-length-prefix"}
    assert plan["branch_jobs"] == [
        {"branch_index": entry["branch_index"], "job": entry["job"], "nonce": entry["nonce"], "input_cid": entry["input_cid"]} for entry in run["branch_jobs"]
    ]


def test_open_honours_the_image_id_env_and_flag(chain: FakeChain, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSWARM_AGGREGATE_IMAGE_ID", "11" * 32)
    payload = _payload(_invoke(*OPEN_ARGS))
    assert _manifest(payload["parent_run"])["aggregate_image_id"] == "11" * 32
    chain.jobs.clear()
    monkeypatch.setattr(cli_main, "random_base_nonce", lambda branches: 5_000)
    payload = _payload(_invoke(*OPEN_ARGS, "--aggregate-image-id", "22" * 32))
    assert _manifest(payload["parent_run"])["aggregate_image_id"] == "22" * 32
    result = _invoke(*OPEN_ARGS, "--aggregate-image-id", "nope")
    assert result.exit_code != 0 and "not hex" in _flat(result.output)


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--combiner", "median"], "unknown combiner"),
        (["--combiner", "weighted-mean", "--trim-bps", "100"], "only applies to trimmed-mean"),
        (["--combiner", "trimmed-mean", "--trim-bps", "10000"], "trim-bps"),
        (["--combiner", "majority-vote"], "needs --output-kind categorical"),
        (["--output-kind", "categorical", "--labels", "a,b"], "needs --output-kind scalar"),
        (["--output-kind", "number"], "output kind"),
    ],
)
def test_open_rejects_bad_combiner_settings_before_any_transaction(chain: FakeChain, extra: list[str], message: str) -> None:
    result = _invoke(*OPEN_ARGS, *extra)
    assert result.exit_code != 0
    assert message in _flat(result.output)
    assert chain.opens == 0 and not chain.jobs
    assert not list(cli_main.PREDICT_RUNS_DIR.glob("*.json")) if cli_main.PREDICT_RUNS_DIR.exists() else True


def test_error_messages_survive_the_rendering_a_hosted_runner_forces(
    chain: FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published CI runs with GITHUB_ACTIONS set, and typer renders differently there.

    `typer.rich_utils.FORCE_TERMINAL` is true whenever GITHUB_ACTIONS, FORCE_COLOR
    or PY_COLORS is set, and the error console is built from it on every render.
    The message then arrives coloured and wrapped in a box, with escape sequences
    at every line break, which is what made five assertions in this file fail on
    every run the public `kswarm` workflow ever executed while the same tests
    passed on a build host. Assertions go through `_flat`, so they must not.
    """

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", 80)

    result = _invoke(*OPEN_ARGS, "--combiner", "trimmed-mean", "--trim-bps", "10000")
    assert result.exit_code != 0
    assert "\x1b[" in result.output, "typer rendered no escape sequences; this test would prove nothing"
    assert "trim-bps" not in result.output, "the message was not wrapped; this test would prove nothing"
    assert "trim-bps" in _flat(result.output)


def test_open_refuses_a_nonce_collision_before_spending_escrow(chain: FakeChain) -> None:
    colliding = str(PROTO.job_pda(chain.customer, 1_002))  # type: ignore[attr-defined]
    chain.jobs[colliding] = _job_account(1_002, chain.customer, bytes(183))  # type: ignore[attr-defined]
    result = _invoke(*OPEN_ARGS)
    assert result.exit_code != 0
    message = _flat(result.output)
    assert "nonce collision" in message and colliding in message
    assert chain.opens == 0
    assert not list(cli_main.PREDICT_RUNS_DIR.glob("*.json"))


def test_interrupted_open_is_resumable_and_never_repeats_a_confirmed_step(chain: FakeChain) -> None:
    chain.fail_after_opens = 2
    result = _invoke(*OPEN_ARGS)
    assert result.exit_code != 0
    assert isinstance(result.exception, RpcError)
    run_id = result.stderr.splitlines()[0].split()[0].removeprefix("parent_run=")
    run = _manifest(run_id)
    assert run["status"] == RUN_OPENING
    assert [entry["status"] for entry in run["branch_jobs"]] == [JOB_COMMITTED, JOB_COMMITTED, JOB_PLANNED, JOB_PLANNED]
    assert run["aggregate"]["status"] == JOB_DEFERRED
    assert len(chain.jobs) == 2

    status = _payload(_invoke("predict", "status", run_id))
    assert status["run_status"] == RUN_OPENING and status["base_nonce"] == 1_000
    assert [(row["manifest_status"], row["status"]) for row in status["jobs"]] == [
        (JOB_COMMITTED, "open"),
        (JOB_COMMITTED, "open"),
        (JOB_PLANNED, "missing"),
        (JOB_PLANNED, "missing"),
        (JOB_DEFERRED, "missing"),
    ]

    # Simulate the manifest lagging the chain: branch 2 was opened but not recorded.
    chain.fail_after_opens = None
    entry = run["branch_jobs"][2]
    chain.sign_and_send(None, None, [cli_main.open_job_ix(PROTO, chain.customer, entry["nonce"], bytes.fromhex(entry["input_bundle_hash"]), ZERO_HASH, 1, 1, 2, 2, 1, CAPABILITY_CLASS["worker-proof"], SOFTWARE_DIGEST["worker-canonical"], 1, 1, 1, 1)])  # type: ignore[attr-defined]
    opens_before = chain.opens

    resumed = _payload(_invoke("predict", "resume", run_id))
    assert resumed["run_status"] == RUN_OPEN
    assert resumed["parent_run"] == run_id
    assert chain.opens == opens_before + 1, "only branch 3 needed open_job; the aggregate is deferred"
    run = _manifest(run_id)
    assert all(entry["status"] == JOB_COMMITTED for entry in run["branch_jobs"])
    assert run["aggregate"]["status"] == JOB_DEFERRED
    assert all(job.status == JOB_STATUS_BY_NAME["open"] for job in chain.jobs.values())
    assert len(chain.jobs) == 4

    again = _payload(_invoke("predict", "resume", run_id))
    assert again["status"] == "already-open"
    assert chain.opens == opens_before + 1


def test_resume_checks_wallet_and_cluster(chain: FakeChain, monkeypatch: pytest.MonkeyPatch) -> None:
    chain.fail_after_opens = 1
    result = _invoke(*OPEN_ARGS)
    run_id = result.stderr.splitlines()[0].split()[0].removeprefix("parent_run=")
    chain.fail_after_opens = None
    wrong = _invoke("predict", "resume", run_id, "--as", "other")
    assert wrong.exit_code != 0 and "not the run customer" in _flat(wrong.output)
    run = _manifest(run_id)
    run["cluster"] = "devnet"
    cli_main.save_run_manifest(cli_main.PREDICT_RUNS_DIR / f"{run_id}.json", run)
    wrong_cluster = _invoke("predict", "resume", run_id)
    assert wrong_cluster.exit_code != 0 and "--cluster devnet" in _flat(wrong_cluster.output)
    missing = _invoke("predict", "resume", "nope")
    assert missing.exit_code != 0 and "unknown local prediction run" in _flat(missing.output)


def test_cancel_unwinds_a_partial_run_and_blocks_resume(chain: FakeChain) -> None:
    chain.fail_after_opens = 3
    result = _invoke(*OPEN_ARGS)
    run_id = result.stderr.splitlines()[0].split()[0].removeprefix("parent_run=")
    chain.fail_after_opens = None
    run = _manifest(run_id)
    assert [entry["status"] for entry in run["branch_jobs"]] == [JOB_COMMITTED, JOB_COMMITTED, JOB_COMMITTED, JOB_PLANNED]
    cancelled = _payload(_invoke("predict", "cancel", run_id, "--as", "customer"))
    assert cancelled["run_status"] == RUN_CANCELLED
    assert sorted(cancelled["cancelled_jobs"]) == sorted(entry["job"] for entry in run["branch_jobs"][:3])
    assert cancelled["skipped_jobs"] == []
    run = _manifest(run_id)
    assert run["status"] == RUN_CANCELLED
    assert [entry["status"] for entry in run["branch_jobs"]] == [JOB_CANCELLED] * 4
    assert run["aggregate"]["status"] == JOB_CANCELLED
    assert all(job.status == JOB_STATUS_BY_NAME["cancelled"] for job in chain.jobs.values())
    blocked = _invoke("predict", "resume", run_id)
    assert blocked.exit_code != 0 and "cancelled" in _flat(blocked.output)


def test_cancel_reports_jobs_it_cannot_cancel(chain: FakeChain) -> None:
    payload = _payload(_invoke(*OPEN_ARGS))
    run_id = payload["parent_run"]
    claimed_key = payload["branch_jobs"][1]["job"]
    chain.jobs[claimed_key] = _replace(chain.jobs[claimed_key], status=JOB_STATUS_BY_NAME["claimed"])
    cancelled = _payload(_invoke("predict", "cancel", run_id, "--as", "customer"))
    # Three branches on chain; the fourth is claimed and the aggregate was never opened.
    assert len(cancelled["cancelled_jobs"]) == 3
    assert cancelled["skipped_jobs"] == [{"job": claimed_key, "status": "claimed"}]
    run = _manifest(run_id)
    assert run["branch_jobs"][1]["status"] == JOB_COMMITTED
    assert run["status"] == RUN_CANCELLED


def test_the_defer_flag_is_accepted_and_changes_nothing(chain: FakeChain) -> None:
    payload = _payload(_invoke(*OPEN_ARGS, "--defer-aggregate-open"))
    run = _manifest(payload["parent_run"])
    assert run["status"] == RUN_OPEN
    assert run["aggregate"]["status"] == JOB_DEFERRED
    assert run["aggregate_open_deferred"] is True
    assert run["aggregate_job"] not in chain.jobs
    assert len(chain.jobs) == 4
    assert run["bonsol"]["image_id"] == AGGREGATE_REDUCER_IMAGE_ID
    status = _payload(_invoke("predict", "status", payload["parent_run"]))
    assert status["jobs"][-1]["manifest_status"] == JOB_DEFERRED and status["jobs"][-1]["status"] == "missing"


def test_legacy_schema_one_manifest_still_reports_status_and_refuses_resume(chain: FakeChain) -> None:
    cli_main.PREDICT_RUNS_DIR.mkdir(parents=True)
    legacy = {
        "schema_version": 1,
        "parent_run": "AGG",
        "aggregate_job": "AGG",
        "branch_jobs": [{"branch_index": 0, "job": str(Keypair().pubkey()), "nonce": 1, "input_cid": "bafy"}],
        "parent_manifest": {"question": "q"},
        "ipfs_api_url": "http://ipfs",
        "combiner": "weighted-mean",
    }
    legacy["aggregate_job"] = str(Keypair().pubkey())
    legacy["parent_run"] = legacy["aggregate_job"]
    (cli_main.PREDICT_RUNS_DIR / f"{legacy['parent_run']}.json").write_text(json.dumps(legacy))
    status = _payload(_invoke("predict", "status", legacy["parent_run"]))
    assert status["run_status"] == RUN_OPEN
    assert [row["kind"] for row in status["jobs"]] == ["branch"]
    blocked = _invoke("predict", "resume", legacy["parent_run"])
    assert blocked.exit_code != 0 and "schema 1" in _flat(blocked.output)


def test_job_open_default_nonce_is_random(chain: FakeChain, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "random_base_nonce", lambda branches: 77)
    payload = _payload(
        _invoke("job", "open", "--as", "customer", "--class", "branch-proof", "--reward", "1", "--required-stake", "1", "--challenge-window", "1", "--capability", "worker-proof")
    )
    assert payload["nonce"] == 77
    assert chain.jobs[payload["job"]].nonce == 77


# --- predict bind-aggregate -----------------------------------------------------
#
# The aggregate job is opened here, against the MFA3 artifact built from the branch
# receipts the chain accepted. This is the moment the job becomes provable, and the
# moment a wrong artifact would fund a job that can never settle, so the command
# refuses more than it accepts.


def _mfb2_scalar_receipt(branch_index: int, value_bps: int) -> bytes:
    """The `MFB2` bytes a scalar branch submits, built by hand.

    The CLI package has no encoder -- it only reads receipts -- so the test writes the
    layout `backend/app/protocol/canonical_hash.py` produces.
    """

    out = bytearray(b"MFB2")
    out += struct.pack("<BBIB", 2, 1, branch_index, 1)  # version, scalar kind, index, FLAG_SCALAR
    out += struct.pack("<H", value_bps)
    out += bytes([branch_index]) + bytes(31)  # stand-in canonical hash
    return bytes(out)


def _settle_branches(chain: FakeChain, run: dict[str, Any], values: list[int]) -> dict[str, bytes]:
    receipts: dict[str, bytes] = {}
    for entry, value in zip(run["branch_jobs"], values):
        receipt = _mfb2_scalar_receipt(entry["branch_index"], value)
        receipts[entry["job"]] = receipt
        chain.jobs[entry["job"]] = _replace(
            chain.jobs[entry["job"]],
            status=JOB_STATUS_BY_NAME["settled"],
            output_cid=f"bafyout{entry['branch_index']}",
            result_bytes=receipt,
            submitted_result_hash=hashlib.sha256(receipt).digest(),
        )
    return receipts


def test_bind_aggregate_opens_the_job_against_the_journal_the_guest_will_commit(chain: FakeChain) -> None:
    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])

    payload = _payload(_invoke("predict", "bind-aggregate", run_id))
    run = _manifest(run_id)
    aggregate = chain.jobs[run["aggregate_job"]]

    artifact = chain.ipfs[run["aggregate_input_cid"]]
    journal = aggregate_journal(artifact)
    assert aggregate.input_bundle_hash == journal.input_digest
    assert aggregate.expected_result_hash == journal.journal_hash
    assert aggregate.required_software_digest.hex() == AGGREGATE_REDUCER_IMAGE_ID
    assert aggregate.required_capability_class_hash == CAPABILITY_CLASS["branch-aggregator-bonsol"]
    assert aggregate.job_class == JOB_CLASS["aggregate-proof"]
    assert aggregate.status == JOB_STATUS_BY_NAME["open"]
    assert aggregate.input_cid == run["aggregate_input_cid"]
    assert run["aggregate"]["status"] == JOB_COMMITTED

    # The artifact carries the receipts the chain accepted, and the journal is a
    # function of exactly those bytes.
    document = json.loads(artifact)
    assert document["schema"] == "MFA3"
    assert [branch["branch_index"] for branch in document["branches"]] == [0, 1, 2, 3]
    for branch in document["branches"]:
        chain_job = chain.jobs[branch["job"]]
        assert bytes.fromhex(branch["result_bytes"]) == chain_job.result_bytes
        assert bytes.fromhex(branch["result_hash"]) == chain_job.submitted_result_hash
    assert journal.reduction.result_value == 5000
    assert journal.reduction.branch_count == 4
    assert run["bonsol"]["bound"] is True
    assert run["bonsol"]["journal_hash"] == journal.journal_hash.hex()
    assert payload["bonsol"]["merkle_root"] == journal.reduction.merkle_root.hex()

    # Idempotent: a second call reports the job it already opened.
    again = _payload(_invoke("predict", "bind-aggregate", run_id))
    assert again["status"] == "already-bound"


def test_bind_aggregate_carries_the_plan_cid_so_the_chain_commits_to_it(chain: FakeChain) -> None:
    """The artifact names the plan pinned before any branch ran.

    The guest ignores the field, but `input_digest` covers the whole artifact and the
    job's `input_bundle_hash` is fixed at open time, so this is what puts the combiner
    and the branch set this run committed to beyond the customer's reach afterwards.
    """

    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    plan_cid = run["aggregate_plan_cid"]
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])
    _invoke("predict", "bind-aggregate", run_id)

    run = _manifest(run_id)
    document = json.loads(chain.ipfs[run["aggregate_input_cid"]])
    assert document["aggregate_plan_cid"] == plan_cid
    plan = json.loads(chain.ipfs[plan_cid])
    assert plan["combiner"] == document["combiner"]
    assert [item["job"] for item in plan["branch_jobs"]] == [b["job"] for b in document["branches"]]
    # The reduction ignores the field, so the journal is unchanged by carrying it.
    assert aggregate_journal(chain.ipfs[run["aggregate_input_cid"]]).reduction.branch_count == 4


def test_bind_aggregate_refuses_a_combiner_swapped_after_the_branches_ran(chain: FakeChain) -> None:
    """The results are visible at bind time, so the combiner must still be the planned one."""

    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])

    run = _manifest(run_id)
    run["combiner"] = "trimmed-mean"
    run["combiner_parameters"] = {"trim_bps": 2500}
    _save_manifest(run_id, run)

    result = _invoke("predict", "bind-aggregate", run_id)
    assert result.exit_code != 0
    message = _flat(result.output)
    assert "departs from the aggregate plan" in message
    assert "combiner" in message


def test_bind_aggregate_refuses_a_branch_dropped_after_the_branches_ran(chain: FakeChain) -> None:
    """Dropping the first branches is the case the nonce layout cannot detect.

    Branch nonces are `base .. base+N-1` and the aggregate is `base+N`, so a reader who
    derives the branch window from the aggregate nonce and the journal's `branch_count`
    lands on exactly the surviving suffix. Only the plan catches it.
    """

    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])

    run = _manifest(run_id)
    run["branch_jobs"] = run["branch_jobs"][2:]
    _save_manifest(run_id, run)

    result = _invoke("predict", "bind-aggregate", run_id)
    assert result.exit_code != 0
    message = _flat(result.output)
    assert "departs from the aggregate plan" in message
    assert "branch set is not the planned set" in message


def test_bind_aggregate_refuses_when_the_pinned_plan_cannot_be_read(chain: FakeChain) -> None:
    """An unreachable plan is a refusal, not a warning: otherwise hiding it is the attack."""

    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])
    del chain.ipfs[_manifest(run_id)["aggregate_plan_cid"]]

    result = _invoke("predict", "bind-aggregate", run_id)
    assert result.exit_code != 0
    assert "cannot read the pinned aggregate plan" in _flat(result.output)


def test_bind_aggregate_refuses_a_branch_that_has_not_settled(chain: FakeChain) -> None:
    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])
    unsettled = run["branch_jobs"][2]["job"]
    chain.jobs[unsettled] = _replace(chain.jobs[unsettled], status=JOB_STATUS_BY_NAME["claimed"])

    result = _invoke("predict", "bind-aggregate", run_id)
    assert result.exit_code != 0
    assert "needs every branch settled" in _flat(result.output)
    assert run["aggregate_job"] not in chain.jobs

    allowed = _invoke("predict", "bind-aggregate", run_id, "--allow-completed-branches")
    assert allowed.exit_code != 0, "claimed is not completed-with-an-attestation either"


def test_bind_aggregate_refuses_receipt_bytes_that_do_not_match_the_chain(chain: FakeChain) -> None:
    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])
    tampered = run["branch_jobs"][0]["job"]
    chain.jobs[tampered] = _replace(chain.jobs[tampered], submitted_result_hash=bytes.fromhex("aa" * 32))

    result = _invoke("predict", "bind-aggregate", run_id)
    assert result.exit_code != 0
    assert "do not hash to submitted_result_hash" in _flat(result.output)
    assert run["aggregate_job"] not in chain.jobs


def test_bind_aggregate_refuses_an_artifact_the_reducer_would_reject(chain: FakeChain) -> None:
    """A categorical receipt under a scalar combiner is a job that could never settle."""

    run_id = _payload(_invoke(*OPEN_ARGS))["parent_run"]
    run = _manifest(run_id)
    _settle_branches(chain, run, [2000, 4000, 6000, 8000])
    entry = run["branch_jobs"][1]
    categorical = bytearray(b"MFB2")
    categorical += struct.pack("<BBIB", 2, 2, entry["branch_index"], 8)  # categorical kind, FLAG_CATEGORY
    categorical += bytes([1]) + bytes([entry["branch_index"]]) + bytes(31)
    chain.jobs[entry["job"]] = _replace(
        chain.jobs[entry["job"]],
        result_bytes=bytes(categorical),
        submitted_result_hash=hashlib.sha256(bytes(categorical)).digest(),
    )

    result = _invoke("predict", "bind-aggregate", run_id)
    assert result.exit_code != 0
    assert "the aggregate reducer would reject this artifact" in _flat(result.output)
    assert run["aggregate_job"] not in chain.jobs
