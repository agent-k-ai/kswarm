"""Contract for `KSWARM_BONSOL_AGGREGATE_COMMAND`.

The hook *executes* the aggregate proof. It does not decide what is proven: the
aggregate job was opened against the MFA3 artifact by `kswarm predict bind-aggregate`,
and the runner has already reduced that artifact itself and knows every value the
Bonsol marker must carry. The hook's job is to get a Bonsol node to run the aggregate
reducer guest on that artifact so the callback writes the marker.

The runner appends one JSON argument to the configured command:

* `run`, `aggregate_job`: the prediction run and its aggregate job pubkey
* `image_id`: the aggregate reducer image id the job requires, 32-byte hex
* `input_cid`, `input_artifact_hex`: the artifact, by locator and by value
* `input_digest`, `committed_outputs`, `output_digest`, `journal_hash`: what the guest
  will commit, as the runner computed it
* `result`: the human-readable reduction, for logs

The command must exit 0 and print exactly one JSON object on stdout with:

* `execution_id`: the Bonsol execution id, 1..32 UTF-8 bytes
* `image_id`, `input_digest`, `output_digest`, `journal_hash`: 32-byte hex, the same
  fields the on-chain `BonsolAggregateVerification` marker carries
* `committed_outputs`: hex, 1..512 bytes, the guest journal outputs

`parse_hook_output` checks `sha256(committed_outputs) == output_digest` and
`sha256(input_digest || committed_outputs) == journal_hash`. The runner then checks
every field against its own reduction and against the job account
(`check_binding_against_job`), so an execution that proved a different claim, or one
the program could never settle, is an error. A hook that fails, or prints anything
else, fails the aggregation. There is no fallback.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BONSOL_HOOK_ENV = "KSWARM_BONSOL_AGGREGATE_COMMAND"
MAX_EXECUTION_ID_BYTES = 32
MAX_COMMITTED_OUTPUT_BYTES = 512
DIGEST_FIELDS = ("image_id", "input_digest", "output_digest", "journal_hash")


class BonsolHookError(RuntimeError):
    pass


@dataclass(frozen=True)
class BonsolBinding:
    execution_id: str
    image_id: bytes
    input_digest: bytes
    output_digest: bytes
    journal_hash: bytes
    committed_outputs: bytes

    def to_json(self) -> dict[str, str]:
        return {
            "execution_id": self.execution_id,
            "image_id": self.image_id.hex(),
            "input_digest": self.input_digest.hex(),
            "output_digest": self.output_digest.hex(),
            "journal_hash": self.journal_hash.hex(),
            "committed_outputs": self.committed_outputs.hex(),
        }


def journal_hash_for(input_digest: bytes, committed_outputs: bytes) -> bytes:
    """The callback harness rule: sha256(input_digest || committed_outputs)."""

    return hashlib.sha256(input_digest + committed_outputs).digest()


def parse_hook_output(stdout: str) -> BonsolBinding:
    """Validate the hook's stdout. Every field is required, typed and internally consistent."""

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BonsolHookError(f"hook stdout is not JSON: {stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise BonsolHookError("hook stdout must be a JSON object")
    execution_id = payload.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise BonsolHookError("hook output is missing a non-empty string execution_id")
    if len(execution_id.encode("utf-8")) > MAX_EXECUTION_ID_BYTES:
        raise BonsolHookError(f"execution_id exceeds {MAX_EXECUTION_ID_BYTES} bytes")
    digests = {field: _hex_bytes(payload, field, expected_len=32) for field in DIGEST_FIELDS}
    committed_outputs = _hex_bytes(payload, "committed_outputs", expected_len=None)
    if not committed_outputs or len(committed_outputs) > MAX_COMMITTED_OUTPUT_BYTES:
        raise BonsolHookError(f"committed_outputs must be 1..{MAX_COMMITTED_OUTPUT_BYTES} bytes; got {len(committed_outputs)}")
    if hashlib.sha256(committed_outputs).digest() != digests["output_digest"]:
        raise BonsolHookError("output_digest is not sha256(committed_outputs)")
    if journal_hash_for(digests["input_digest"], committed_outputs) != digests["journal_hash"]:
        raise BonsolHookError("journal_hash is not sha256(input_digest || committed_outputs)")
    return BonsolBinding(
        execution_id,
        digests["image_id"],
        digests["input_digest"],
        digests["output_digest"],
        digests["journal_hash"],
        committed_outputs,
    )


def run_bonsol_hook(command: str, payload: dict[str, Any], *, cwd: Path, timeout_seconds: float) -> BonsolBinding:
    """Run the configured hook and return its validated binding. Any failure raises BonsolHookError."""

    argv = shlex.split(command)
    if not argv:
        raise BonsolHookError(f"{BONSOL_HOOK_ENV} is set but empty after shell splitting")
    argv.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    try:
        completed = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BonsolHookError(f"hook did not run: {exc}") from exc
    if completed.returncode != 0:
        raise BonsolHookError(f"hook exited {completed.returncode}: {completed.stderr.strip()[:500]}")
    return parse_hook_output(completed.stdout)


def _hex_bytes(payload: dict[str, Any], field: str, *, expected_len: int | None) -> bytes:
    value = payload.get(field)
    if not isinstance(value, str):
        raise BonsolHookError(f"hook output is missing {field}")
    try:
        raw = bytes.fromhex(value.removeprefix("0x"))
    except ValueError as exc:
        raise BonsolHookError(f"{field} is not hex: {value!r}") from exc
    if expected_len is not None and len(raw) != expected_len:
        raise BonsolHookError(f"{field} must be {expected_len} bytes; got {len(raw)}")
    return raw
