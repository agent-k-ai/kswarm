#!/usr/bin/env python3
"""`KSWARM_BONSOL_AGGREGATE_COMMAND`: request the Bonsol proof of an aggregate receipt.

The aggregator runner has already reduced the artifact the aggregate job committed and
knows every value the marker must carry. This hook's job is narrow: get a Bonsol node
to run the aggregate reducer guest on that artifact so its callback writes the
`BonsolAggregateVerification` marker the program settles against.

It does what `settle_aggregate_proof_job` needs, in order:

1. funds the marker PDA, because the callback allocates it and the program requires the
   account to already hold rent,
2. deploys the guest image to the image server (idempotent; an already-deployed image
   is not an error),
3. writes an execution request whose `inputHash` is the artifact's framed digest and
   whose `callbackConfig` carries the raw `record_aggregate_verification` prefix and the
   four accounts the program's fallback reads, in order, and
4. runs `bonsol execute --wait`.

It then prints the binding the runner re-checks against its own reduction. It never
decides what is proven: every digest it prints comes from the payload it was given, and
the runner refuses the execution if any of them moved.

Configuration, all optional except where noted:

  KSWARM_BONSOL_KEYPAIR       Bonsol client keypair (default runtime/bonsol/client-keypair.json)
  KSWARM_BONSOL_RPC_URL       Solana RPC the Bonsol node watches (default http://127.0.0.1:38899)
  KSWARM_BONSOL_IMAGE_SERVER  image server base URL (default http://127.0.0.1:38080)
  KSWARM_BONSOL_MANIFEST      aggregate reducer manifest (default runtime/bonsol/aggregate-reducer-manifest.json)
  KSWARM_BONSOL_CLI           bonsol CLI (default scripts/bin/bonsol)
  KSWARM_SOLANA_CLI           solana CLI (default scripts/bin/solana)
  KSWARM_BONSOL_WORK_DIR      where requests and logs are written (default runtime/bonsol/aggregate)
  KSWARM_BONSOL_EXECUTE_TIMEOUT  seconds to wait for the proof (default 1800)
  KSWARM_BONSOL_TIP           lamport tip on the execution request (default 12000)
  KSWARM_BONSOL_EXPIRY        expiry in slots (default 1500)
  KSWARM_BONSOL_DEPLOY_TIMEOUT  seconds to wait for the image deployment to appear on
                              chain before executing against it (default 180)
  KSWARM_BONSOL_EXECUTE_ATTEMPTS  execution submissions before giving up, for the window
                              where the deployment is on chain but not yet visible to the
                              simulator (default 6)
  KSWARM_BONSOL_EXECUTE_RETRY_SECONDS  delay between those attempts (default 10)
  KSWARM_PROGRAM_ID           callback program (default the checked-in kswarm program id)
  KSWARM_BONSOL_INPUT_PUBLISH_URL   where to POST a large framed input so the Bonsol
                              node can fetch it back; the proving service serves this
                              itself at /input, so no other storage is needed
  KSWARM_BONSOL_INPUT_IPFS_API_URL  IPFS API to publish it to instead
                              (default $KSWARM_IPFS_API_URL, else http://127.0.0.1:4501)
  KSWARM_BONSOL_INPUT_GATEWAY_URL   IPFS gateway base the Bonsol NODE can reach
                              (default http://127.0.0.1:48080/ipfs)

# How the input reaches the guest

A Solana transaction carries at most 1232 raw bytes, and an execution request also
carries the callback prefix and four accounts, so an aggregate artifact of even two
branches does not fit inline: the framed input alone is over 1900 hex characters. The
hook therefore publishes the framed bytes and sends a `PublicUrl` input, which the
Bonsol node downloads and hands to the guest verbatim. `verifyInputHash` still holds:
the guest hashes what it read and commits that digest as the first 32 bytes of its
journal, and the Bonsol program compares it with the request's `inputHash`. A small
framed input is still sent inline as `PublicData`, so the existing smoke tests are
unaffected.

Paths handed to the `bonsol` CLI are container paths when the CLI is the compose
wrapper, so `KSWARM_BONSOL_CONTAINER_RUNTIME` (default `/runtime/bonsol`) says how to
rewrite a host runtime path for it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROGRAM_ID = "ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
INSTRUCTIONS_SYSVAR_ID = "Sysvar1nstructions1111111111111111111111111"
RECORD_AGGREGATE_VERIFICATION_RAW_IX = 1
MARKER_RENT_SOL = "0.02"
EXECUTION_ID_MAX_BYTES = 32
# A framed input at or below this many bytes is sent inline. Above it the transaction
# would exceed the 1232-byte Solana limit once the callback prefix and accounts are
# added, so the input is published and fetched by URL instead.
MAX_INLINE_INPUT_BYTES = 400


class HookError(RuntimeError):
    pass


def env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def runtime_dir() -> Path:
    return Path(env("BONSOL_RUNTIME_HOST_DIR", str(REPO_ROOT / "runtime" / "bonsol")))


def container_path(host_path: Path) -> str:
    """Rewrite a host runtime path to the path the `bonsol` CLI sees."""

    prefix = env("KSWARM_BONSOL_CONTAINER_RUNTIME", "/runtime/bonsol")
    try:
        relative = host_path.resolve().relative_to(runtime_dir().resolve())
    except ValueError:
        return str(host_path)
    return f"{prefix}/{relative}" if str(relative) != "." else prefix


def run(argv: list[str], *, timeout: float, allow: tuple[str, ...] = ()) -> str:
    completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if completed.returncode != 0:
        if any(token in completed.stdout for token in allow):
            return completed.stdout
        raise HookError(f"{argv[0]} exited {completed.returncode}: {completed.stdout.strip()[-2000:]}")
    return completed.stdout


DEPLOYED_MARKERS = ("already deployed", "already exists", "AccountAlreadyInitialized")

# `ChannelError::InvalidDeploymentAccount` as the Bonsol program returns it
# (`ProgramError::Custom(18)`), which the RPC reports in hex.
DEPLOYMENT_NOT_VISIBLE = "custom program error: 0x12"


def execute_request(bonsol_cli: str, keypair: Path, rpc_url: str, request_path: Path, timeout: float) -> str:
    """Submit the execution request, retrying while the deployment is not visible yet.

    `ExecuteV1` checks that the deployment account derived from the image id is owned by
    the Bonsol program, and returns `InvalidDeploymentAccount` when it is not. On a
    chain where the image was deployed moments earlier that check can still fail even
    though the account is there and correct: reading it back at `confirmed` -- which is
    what the deploy command's own lookup does, and what this hook waits on -- does not
    guarantee the bank the transaction simulates against has it.

    Retrying is the honest fix, because the condition is transient and nothing else
    about the request changes. Any other program error fails immediately: a request that
    is wrong will not become right by being sent again.
    """

    argv = [bonsol_cli, "-k", container_path(keypair), "-u", rpc_url, "execute", "-f", container_path(request_path),
            "--wait", "--timeout", str(int(timeout))]
    attempts = int(env("KSWARM_BONSOL_EXECUTE_ATTEMPTS", "6"))
    delay = float(env("KSWARM_BONSOL_EXECUTE_RETRY_SECONDS", "10"))
    for attempt in range(1, attempts + 1):
        try:
            return run(argv, timeout=timeout + 120)
        except HookError as error:
            if DEPLOYMENT_NOT_VISIBLE not in str(error) or attempt == attempts:
                raise
            print(
                f"bonsol-aggregate-hook: the deployment is not visible to the simulator yet "
                f"(attempt {attempt}/{attempts}); retrying in {delay:.0f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise HookError("unreachable")


def deploy_image(bonsol_cli: str, keypair: Path, rpc_url: str, image_server: str, manifest: Path) -> None:
    """Deploy the guest image, and do not return until the chain shows it.

    `bonsol execute` names the deployment account derived from the image id, and the
    Bonsol program rejects the request with `InvalidDeploymentAccount` when that account
    is not there yet. The deploy transaction and the execute that follows it are two
    submissions a second apart, so on a fresh chain the second one can simulate against
    a slot that has not seen the first: the whole aggregate then fails with
    `custom program error: 0x12` while a deploy run by hand a minute later reports the
    deployment already exists.

    So deploy, then re-run the same command until it says the deployment is there. The
    CLI's own account lookup is the readiness check, which avoids teaching this script a
    second way to derive the PDA.
    """

    argv = [bonsol_cli, "-k", container_path(keypair), "-u", rpc_url, "deploy", "url", "--url", image_server,
            "--manifest-path", container_path(manifest), "--auto-confirm"]
    output = run(argv, timeout=600, allow=DEPLOYED_MARKERS)
    if any(token in output for token in DEPLOYED_MARKERS):
        return
    deadline = time.time() + float(env("KSWARM_BONSOL_DEPLOY_TIMEOUT", "180"))
    while True:
        output = run(argv, timeout=600, allow=DEPLOYED_MARKERS)
        if any(token in output for token in DEPLOYED_MARKERS):
            return
        if time.time() > deadline:
            raise HookError(
                "the guest image deployment is still not visible on chain; `bonsol execute` would fail with "
                "InvalidDeploymentAccount"
            )
        time.sleep(3)


def marker_pda(program_id: str, aggregate_job: str, execution_id: bytes, image_id: bytes, input_digest: bytes, journal_hash: bytes) -> str:
    from solders.pubkey import Pubkey

    address, _ = Pubkey.find_program_address(
        [b"bonsol_aggregate_verification", bytes(Pubkey.from_string(aggregate_job)), execution_id, image_id, input_digest, journal_hash],
        Pubkey.from_string(program_id),
    )
    return str(address)


def execution_id_bytes(execution_id: str) -> bytes:
    raw = execution_id.encode("utf-8")
    if not raw or len(raw) > EXECUTION_ID_MAX_BYTES:
        raise HookError(f"execution id must be 1..{EXECUTION_ID_MAX_BYTES} bytes; got {len(raw)}")
    return raw.ljust(EXECUTION_ID_MAX_BYTES, b"\x00")


def publish_guest_input(framed: bytes) -> dict[str, str]:
    """The one input the guest reads, small enough to fit in a transaction.

    Inline when it fits. Otherwise the framed bytes are added to IPFS and the request
    carries the gateway URL: the node downloads them and hands them to the guest
    unchanged, so the digest the guest commits is still the digest of these bytes.
    """

    if len(framed) <= MAX_INLINE_INPUT_BYTES:
        return {"inputType": "PublicData", "data": "0x" + framed.hex()}
    publish_url = env("KSWARM_BONSOL_INPUT_PUBLISH_URL", "")
    if publish_url:
        return {"inputType": "PublicUrl", "data": http_publish(publish_url, framed)}
    api_url = (env("KSWARM_BONSOL_INPUT_IPFS_API_URL", "") or env("KSWARM_IPFS_API_URL", "http://127.0.0.1:4501")).rstrip("/")
    gateway = env("KSWARM_BONSOL_INPUT_GATEWAY_URL", "http://127.0.0.1:48080/ipfs").rstrip("/")
    cid = ipfs_add(api_url, framed)
    return {"inputType": "PublicUrl", "data": f"{gateway}/{cid}"}


def http_publish(publish_url: str, framed: bytes) -> str:
    """Hand the framed bytes to the proving service and take back a URL for them.

    The service serves what it was given, unchanged, and the Bonsol program compares the
    guest's committed digest with the request's `inputHash`, so a store that returned
    different bytes produces no marker rather than a wrong one.
    """

    import urllib.request

    request = urllib.request.Request(
        publish_url,
        data=framed,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - without the input there is no execution
        raise HookError(f"cannot publish the framed guest input to {publish_url}: {error}") from error
    url = answer.get("url")
    if not isinstance(url, str) or not url:
        raise HookError(f"{publish_url} did not return a url: {answer!r}")
    return url


def ipfs_add(api_url: str, payload: bytes) -> str:
    """Add raw bytes to IPFS and return the CID. Written with urllib to keep the hook
    dependency-free: it runs as a subprocess of the aggregator, not inside it."""

    import urllib.request
    import uuid

    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"aggregate-input.framed\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        f"{api_url}/api/v0/add?pin=true&cid-version=1",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            last = [line for line in response.read().decode("utf-8").splitlines() if line.strip()][-1]
    except Exception as error:  # noqa: BLE001 - any failure here means no proof can be requested
        raise HookError(f"cannot publish the framed guest input to {api_url}: {error}") from error
    return json.loads(last)["Hash"]


def main() -> int:
    if len(sys.argv) < 2:
        raise HookError("usage: bonsol-aggregate-hook.py '<payload json>'")
    payload = json.loads(sys.argv[-1])

    aggregate_job = str(payload["aggregate_job"])
    image_id = bytes.fromhex(payload["image_id"])
    artifact = bytes.fromhex(payload["input_artifact_hex"])
    input_digest = bytes.fromhex(payload["input_digest"])
    committed_outputs = bytes.fromhex(payload["committed_outputs"])
    output_digest = bytes.fromhex(payload["output_digest"])
    journal_hash = bytes.fromhex(payload["journal_hash"])

    # Recompute rather than trust: a hook that forwarded digests it never checked would
    # request a proof of a different claim and the runner would only find out afterwards.
    framed = len(artifact).to_bytes(8, "little") + artifact
    if hashlib.sha256(framed).digest() != input_digest:
        raise HookError("payload input_digest is not sha256 of the framed artifact")
    if hashlib.sha256(committed_outputs).digest() != output_digest:
        raise HookError("payload output_digest is not sha256(committed_outputs)")
    if hashlib.sha256(input_digest + committed_outputs).digest() != journal_hash:
        raise HookError("payload journal_hash is not sha256(input_digest || committed_outputs)")

    program_id = env("KSWARM_PROGRAM_ID", DEFAULT_PROGRAM_ID)
    rpc_url = env("KSWARM_BONSOL_RPC_URL", "http://127.0.0.1:38899")
    image_server = env("KSWARM_BONSOL_IMAGE_SERVER", "http://127.0.0.1:38080")
    bonsol_cli = env("KSWARM_BONSOL_CLI", str(REPO_ROOT / "scripts" / "bin" / "bonsol"))
    solana_cli = env("KSWARM_SOLANA_CLI", str(REPO_ROOT / "scripts" / "bin" / "solana"))
    keypair = Path(env("KSWARM_BONSOL_KEYPAIR", str(runtime_dir() / "client-keypair.json")))
    manifest = Path(env("KSWARM_BONSOL_MANIFEST", str(runtime_dir() / "aggregate-reducer-manifest.json")))
    work_dir = Path(env("KSWARM_BONSOL_WORK_DIR", str(runtime_dir() / "aggregate")))
    timeout = float(env("KSWARM_BONSOL_EXECUTE_TIMEOUT", "1800"))
    work_dir.mkdir(parents=True, exist_ok=True)

    if not manifest.is_file():
        raise HookError(f"no aggregate reducer manifest at {manifest}: run protocol/scripts/build-aggregate-reducer.sh")
    manifest_image_id = json.loads(manifest.read_text(encoding="utf-8"))["imageId"]
    if manifest_image_id != image_id.hex():
        raise HookError(
            f"the deployed aggregate reducer is {manifest_image_id}, but the job requires {image_id.hex()}; "
            "rebuild the guest or re-pin the job's required_software_digest"
        )

    guest_input = publish_guest_input(framed)
    execution_id = f"agg-{aggregate_job[:8]}-{int(time.time())}"
    marker = marker_pda(program_id, aggregate_job, execution_id_bytes(execution_id), image_id, input_digest, journal_hash)

    # The callback allocates the marker with `invoke_signed`, and the program requires
    # the account to already hold rent. An unfunded marker fails the whole execution.
    run(
        [solana_cli, "-u", rpc_url, "transfer", "--from", container_path(keypair), "--fee-payer", container_path(keypair),
         "--allow-unfunded-recipient", marker, MARKER_RENT_SOL],
        timeout=120,
    )

    deploy_image(bonsol_cli, keypair, rpc_url, image_server, manifest)

    prefix = [RECORD_AGGREGATE_VERIFICATION_RAW_IX] + list(execution_id_bytes(execution_id)) + list(image_id) + list(input_digest) + list(output_digest) + list(journal_hash)
    request = {
        "imageId": image_id.hex(),
        "executionId": execution_id,
        "executionConfig": {"verifyInputHash": True, "inputHash": input_digest.hex(), "forwardOutput": True},
        # The guest reads a little-endian u64 length and then that many bytes, so the
        # input it is handed IS the framed artifact, and `inputHash` is the digest of
        # exactly those bytes.
        "inputs": [guest_input],
        "tip": int(env("KSWARM_BONSOL_TIP", "12000")),
        "expiry": int(env("KSWARM_BONSOL_EXPIRY", "1500")),
        "callbackConfig": {
            "programId": program_id,
            "instructionPrefix": prefix,
            # The order the program's fallback reads: marker (writable), aggregate job,
            # system program, instructions sysvar.
            "extraAccounts": [
                {"pubkey": marker, "isSigner": False, "isWritable": True},
                {"pubkey": aggregate_job, "isSigner": False, "isWritable": False},
                {"pubkey": SYSTEM_PROGRAM_ID, "isSigner": False, "isWritable": False},
                {"pubkey": INSTRUCTIONS_SYSVAR_ID, "isSigner": False, "isWritable": False},
            ],
        },
    }
    request_path = work_dir / f"{execution_id}-execution-request.json"
    request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    (work_dir / f"{execution_id}-input.json").write_bytes(artifact)

    output = execute_request(bonsol_cli, keypair, rpc_url, request_path, timeout)
    (work_dir / f"{execution_id}-execute.log").write_text(output, encoding="utf-8")

    print(
        json.dumps(
            {
                "execution_id": execution_id,
                "image_id": image_id.hex(),
                "input_digest": input_digest.hex(),
                "output_digest": output_digest.hex(),
                "journal_hash": journal_hash.hex(),
                "committed_outputs": committed_outputs.hex(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - the contract is exit status plus stderr
        print(f"bonsol-aggregate-hook: {error}", file=sys.stderr)
        sys.exit(1)
