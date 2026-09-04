"""The branch canonicalization receipt: prove it, verify it, bind it to a claim.

# Why it exists

A branch worker publishes a JSON output document to IPFS and submits `MFB2` receipt
bytes on chain. The Solana program stores only `sha256(result_bytes)`; it never sees
the document. Re-execution catches a worker that invented a forecast. It does not
catch a worker whose published document and submitted receipt describe different
values, because both sides of a re-execution comparison are the verifier's own.

The `protocol/zkvm-reducer` guest closes that gap. It is given the branch output
document and derives the receipt from it, committing
`input_digest || result_hash || output_len`. The verifier binds those to the job: the
frame it rebuilt from the job's own input and output, the on-chain
`submitted_result_hash`, and the document it fetched.

# The proof document

The guest is shown the branch output **without** `zkvm_receipt_cid`, because that field
names the receipt and cannot exist before the receipt does. It is excluded from the
canonical hash preimage for the same reason, so the `MFB2` bytes the guest computes are
the bytes the worker submitted. `proof_document` is the one definition of that
stripping, used by the worker and the verifier alike.

# Cost

Proving is CPU work measured in minutes, not milliseconds. `KSWARM_ZKVM_HOST` is
therefore opt-in per worker: a worker with it set proves every branch and fails the
branch closed when proving fails; a worker without it publishes no receipt and says so
at startup. A verifier with `KSWARM_ZKVM_REQUIRE_RECEIPT=1` refuses to attest to a
branch that carries none, which leaves that job to time out rather than settle on an
unverified receipt.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.protocol.canonical_hash import canonical_json_bytes
from kswarm_cli.encoding import sha256


LOGGER = logging.getLogger("kswarm.branch_receipt")

ZKVM_HOST_ENV = "KSWARM_ZKVM_HOST"
ZKVM_TIMEOUT_ENV = "KSWARM_ZKVM_TIMEOUT_SECONDS"
ZKVM_IMAGE_ID_ENV = "KSWARM_ZKVM_IMAGE_ID"
ZKVM_IMAGE_ID_FILE_ENV = "KSWARM_ZKVM_IMAGE_ID_FILE"
ZKVM_REQUIRE_ENV = "KSWARM_ZKVM_REQUIRE_RECEIPT"
DEFAULT_TIMEOUT_SECONDS = 1800.0

BRANCH_RECEIPT_SCHEMA = "MFBR1"
BRANCH_RECEIPT_SCHEMA_VERSION = 1
BUNDLE_VERSION = "kswarm-branch-receipt-v1"
JOURNAL_LEN = 32 + 32 + 4
# Named by the branch output but never inside the proof: the receipt cannot name itself.
RECEIPT_CID_FIELD = "zkvm_receipt_cid"


class BranchReceiptError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReceiptJournal:
    input_digest: bytes
    result_hash: bytes
    output_len: int

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ReceiptJournal":
        try:
            input_digest = bytes.fromhex(payload["input_digest"])
            result_hash = bytes.fromhex(payload["result_hash"])
            output_len = int(payload["output_len"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BranchReceiptError(f"receipt journal is malformed: {exc}") from exc
        if len(input_digest) != 32 or len(result_hash) != 32 or output_len < 0:
            raise BranchReceiptError("receipt journal fields are out of range")
        return cls(input_digest, result_hash, output_len)

    def to_json(self) -> dict[str, Any]:
        return {
            "input_digest": self.input_digest.hex(),
            "result_hash": self.result_hash.hex(),
            "output_len": self.output_len,
        }


def host_binary(environ: dict[str, str] | None = None) -> str | None:
    value = (environ if environ is not None else os.environ).get(ZKVM_HOST_ENV, "").strip()
    return value or None


def timeout_seconds(environ: dict[str, str] | None = None) -> float:
    raw = (environ if environ is not None else os.environ).get(ZKVM_TIMEOUT_ENV, "").strip()
    return float(raw) if raw else DEFAULT_TIMEOUT_SECONDS


def pinned_image_id(environ: dict[str, str] | None = None) -> str | None:
    """The guest image id a receipt must name: the environment, then the shipped file.

    The worker images install `protocol/zkvm-reducer/IMAGE_ID` next to the host binary
    they were built with and point `KSWARM_ZKVM_IMAGE_ID_FILE` at it, so a container
    pins by default rather than accepting a receipt from any guest. An operator can
    still override with `KSWARM_ZKVM_IMAGE_ID`, and an unreadable or malformed file is
    the same as no pin rather than a crash: the receipt checks that follow are what
    actually bind it, and the pin only narrows which guest may have produced it.
    """

    source = environ if environ is not None else os.environ
    value = source.get(ZKVM_IMAGE_ID_ENV, "").strip()
    if value:
        return value.lower()
    path = source.get(ZKVM_IMAGE_ID_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        recorded = Path(path).read_text(encoding="utf-8").strip().lower()
    except OSError:
        LOGGER.warning("%s=%s could not be read; this verifier pins no guest image id", ZKVM_IMAGE_ID_FILE_ENV, path)
        return None
    if len(recorded) != 64 or any(character not in "0123456789abcdef" for character in recorded):
        LOGGER.warning("%s=%s does not hold 64 hex digits; this verifier pins no guest image id", ZKVM_IMAGE_ID_FILE_ENV, path)
        return None
    return recorded


def receipt_required(environ: dict[str, str] | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(ZKVM_REQUIRE_ENV, "").strip() == "1"


def proof_document(output_payload: dict[str, Any]) -> dict[str, Any]:
    """The branch output as the guest sees it: everything except the receipt locator."""

    return {key: value for key, value in output_payload.items() if key != RECEIPT_CID_FIELD}


def guest_input_bytes(branch_input_payload: dict[str, Any], output_payload: dict[str, Any]) -> bytes:
    """The canonical MFBR1 frame the guest reads.

    `branch_input_sha256` is not used by the reduction. It is in the frame so the
    committed `input_digest` binds this receipt to one branch input, and the verifier
    rebuilds the whole frame before it looks at the journal.
    """

    return canonical_json_bytes(
        {
            "schema": BRANCH_RECEIPT_SCHEMA,
            "schema_version": BRANCH_RECEIPT_SCHEMA_VERSION,
            "branch_input_sha256": sha256(canonical_json_bytes(branch_input_payload)).hex(),
            "branch_output": proof_document(output_payload),
        }
    )


def expected_journal(branch_input_payload: dict[str, Any], output_payload: dict[str, Any], result_hash: bytes) -> ReceiptJournal:
    """What an honest guest must have committed for this input and this output."""

    frame = guest_input_bytes(branch_input_payload, output_payload)
    document = canonical_json_bytes(proof_document(output_payload))
    return ReceiptJournal(
        input_digest=sha256(len(frame).to_bytes(8, "little") + frame),
        result_hash=result_hash,
        output_len=len(document),
    )


def prove(
    binary: str,
    branch_input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the guest and return its receipt bundle. Any failure raises."""

    frame = guest_input_bytes(branch_input_payload, output_payload)
    with tempfile.TemporaryDirectory(prefix="kswarm-zkvm-") as work:
        input_path = Path(work) / "guest-input.json"
        bundle_path = Path(work) / "receipt-bundle.json"
        input_path.write_bytes(frame)
        _run(binary, "prove", input_path, bundle_path, timeout=timeout)
        bundle = _read_json(bundle_path)
    _validate_bundle_shape(bundle)
    return bundle


def verify(binary: str, bundle: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ReceiptJournal:
    """Verify a receipt bundle with the host binary and return the verified journal.

    The journal returned is the one the *verifier binary* decoded from the receipt, not
    the one the bundle claims. A bundle whose stated journal disagrees is refused by the
    host itself.
    """

    _validate_bundle_shape(bundle)
    with tempfile.TemporaryDirectory(prefix="kswarm-zkvm-") as work:
        bundle_path = Path(work) / "receipt-bundle.json"
        result_path = Path(work) / "verified.json"
        bundle_path.write_bytes(canonical_json_bytes(bundle))
        _run(binary, "verify", bundle_path, result_path, timeout=timeout)
        verified = _read_json(result_path)
    if verified.get("verified") is not True:
        raise BranchReceiptError("host verify did not report a verified receipt")
    journal = ReceiptJournal.from_json(verified.get("journal") or {})
    if len(bytes.fromhex(verified.get("journal_hex", ""))) != JOURNAL_LEN:
        raise BranchReceiptError(f"verified journal is not {JOURNAL_LEN} bytes")
    return journal


def bind_to_claim(
    journal: ReceiptJournal,
    expected: ReceiptJournal,
    *,
    submitted_result_hash: bytes,
) -> list[str]:
    """Every reason the verified journal does not describe this claim.

    An empty list means the receipt proves that the document the verifier fetched, under
    the branch input the job committed, encodes to the receipt hash the chain accepted.
    """

    errors: list[str] = []
    if journal.input_digest != expected.input_digest:
        errors.append(
            f"receipt input_digest {journal.input_digest.hex()} is not the digest of this job's input and output "
            f"{expected.input_digest.hex()}"
        )
    if journal.result_hash != submitted_result_hash:
        errors.append(
            f"receipt result_hash {journal.result_hash.hex()} is not the on-chain submitted_result_hash "
            f"{submitted_result_hash.hex()}"
        )
    if journal.output_len != expected.output_len:
        errors.append(
            f"receipt output_len {journal.output_len} is not the fetched document length {expected.output_len}"
        )
    return errors


def image_id_matches(bundle: dict[str, Any], pinned: str | None) -> list[str]:
    """The bundle must name the pinned guest, when one is pinned."""

    if pinned is None:
        return []
    if str(bundle.get("image_id_hex", "")).lower() != pinned:
        return [f"receipt image id {bundle.get('image_id_hex')!r} is not the pinned guest {pinned!r}"]
    return []


def _validate_bundle_shape(bundle: Any) -> None:
    if not isinstance(bundle, dict):
        raise BranchReceiptError("receipt bundle is not a JSON object")
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise BranchReceiptError(f"unknown receipt bundle version {bundle.get('bundle_version')!r}")
    for field in ("image_id_hex", "journal_hex", "receipt_b64"):
        if not isinstance(bundle.get(field), str) or not bundle[field]:
            raise BranchReceiptError(f"receipt bundle is missing {field}")
    ReceiptJournal.from_json(bundle.get("journal") or {})


def _run(binary: str, command: str, input_path: Path, output_path: Path, *, timeout: float) -> None:
    argv = [binary, command, str(input_path), str(output_path)]
    try:
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BranchReceiptError(f"zkvm host {command} did not run: {exc}") from exc
    if completed.returncode != 0:
        raise BranchReceiptError(f"zkvm host {command} exited {completed.returncode}: {completed.stderr.strip()[:500]}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BranchReceiptError(f"zkvm host wrote no readable output at {path}: {exc}") from exc
