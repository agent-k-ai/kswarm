"""The MFA3 aggregate artifact and the journal the aggregate reducer commits.

This module is the Python half of `protocol/bonsol-aggregate-reducer`. Every rule
here has a counterpart there, and `cli/tests/test_aggregate_artifact.py` checks both
against the same vectors, generated from the Rust crate. Where the two disagree, an
aggregate job is opened against a journal hash the guest will never produce and the
job can never settle, so the agreement is checked rather than assumed.

Three callers use it:

* `kswarm predict bind-aggregate` builds the artifact from the settled branch
  receipts, predicts the journal, and opens the aggregate job bound to both.
* `worker/aggregator_runner` rebuilds the artifact from the job's committed input
  artifact, checks it reduces to the same journal, and submits the committed outputs
  as the receipt.
* `worker/verifier_worker` reduces the artifact again before it attests.

The aggregate job's `input_bundle_hash` and `expected_result_hash` are fixed by
`open_job` and never change, so the artifact must be complete before the job is
opened. That is why the aggregate job of a prediction run is opened after its
branches settle, not alongside them.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

from kswarm_cli.bonsol import framed_input_digest, journal_hash
from kswarm_cli.encoding import sha256


AGGREGATE_SCHEMA = "MFA3"
AGGREGATE_SCHEMA_VERSION = 3
# `combiner_id || params_digest || result_value || branch_count || merkle_root`.
AGGREGATE_COMMITTED_OUTPUTS_LEN = 1 + 32 + 4 + 4 + 32
AGGREGATE_JOURNAL_LEN = 32 + AGGREGATE_COMMITTED_OUTPUTS_LEN
MAX_BRANCHES = 128

COMBINER_WEIGHTED_MEAN = 1
COMBINER_TRIMMED_MEAN = 2
COMBINER_MAJORITY_VOTE = 3
COMBINER_IDS = {
    "weighted-mean": COMBINER_WEIGHTED_MEAN,
    "trimmed-mean": COMBINER_TRIMMED_MEAN,
    "majority-vote": COMBINER_MAJORITY_VOTE,
}
COMBINER_NAMES = {value: key for key, value in COMBINER_IDS.items()}
BPS_SCALE = 10_000

COMBINER_PARAMS_DOMAIN = "kswarm-combiner-params-v1"

# `SHA256(0x00 || leaf)` and `SHA256(0x01 || left || right)`: a leaf can never be
# read as an inner node.
MERKLE_LEAF_PREFIX = b"\x00"
MERKLE_NODE_PREFIX = b"\x01"

MFB2_MAGIC = b"MFB2"
MFB2_SCHEMA_VERSION = 2
OUTPUT_KIND_BY_ID = {1: "scalar", 2: "categorical", 3: "narrative_with_scalar"}
SCALAR_OUTPUT_KINDS = frozenset({"scalar", "narrative_with_scalar"})
FLAG_SCALAR = 1 << 0
FLAG_LOWER = 1 << 1
FLAG_UPPER = 1 << 2
FLAG_CATEGORY = 1 << 3
FLAG_SCORES = 1 << 4
FLAG_KNOWN = FLAG_SCALAR | FLAG_LOWER | FLAG_UPPER | FLAG_CATEGORY | FLAG_SCORES
MAX_NARRATIVE_SCORES = 32

_HEX_DIGITS = frozenset("0123456789abcdef")


class AggregateError(ValueError):
    """The artifact is one the guest would refuse, so no binding exists for it."""


@dataclass(frozen=True)
class BranchReceipt:
    """The fields of an `MFB2` branch receipt an aggregate reduction reads."""

    branch_index: int
    output_kind: str
    scalar_value_bps: int | None
    categorical_label_index: int | None
    canonical_hash: bytes
    result_hash: bytes


@dataclass(frozen=True)
class AggregateReduction:
    combiner_id: int
    trim_bps: int
    category_dictionary_size: int
    params_digest: bytes
    result_value: int
    branch_count: int
    branch_hashes: tuple[bytes, ...]
    merkle_root: bytes

    def committed_outputs(self) -> bytes:
        return aggregate_committed_outputs(self)


@dataclass(frozen=True)
class AggregateJournal:
    input_digest: bytes
    reduction: AggregateReduction

    @property
    def committed_outputs(self) -> bytes:
        return self.reduction.committed_outputs()

    @property
    def output_digest(self) -> bytes:
        return sha256(self.committed_outputs)

    @property
    def journal_hash(self) -> bytes:
        return journal_hash(self.input_digest, self.committed_outputs)

    @property
    def journal_bytes(self) -> bytes:
        return self.input_digest + self.committed_outputs

    def to_json(self) -> dict[str, Any]:
        return {
            "input_digest": self.input_digest.hex(),
            "committed_outputs": self.committed_outputs.hex(),
            "output_digest": self.output_digest.hex(),
            "journal_hash": self.journal_hash.hex(),
            "combiner_id": self.reduction.combiner_id,
            "combiner_params_digest": self.reduction.params_digest.hex(),
            "result_value": self.reduction.result_value,
            "branch_count": self.reduction.branch_count,
            "merkle_root": self.reduction.merkle_root.hex(),
        }


def canonical_json_bytes(payload: Any) -> bytes:
    """The one canonical JSON rule: sorted keys, no whitespace, UTF-8, no NaN."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def combiner_id_for(name: str) -> int:
    try:
        return COMBINER_IDS[name]
    except KeyError as exc:
        raise AggregateError(f"unknown combiner {name!r}") from exc


def combiner_params_canonical_bytes(combiner_id: int, trim_bps: int, category_dictionary_size: int) -> bytes:
    """One line naming every parameter of the registry, so adding one is a version bump."""

    return (
        f"{COMBINER_PARAMS_DOMAIN}|combiner_id={combiner_id}"
        f"|trim_bps={trim_bps}|category_dictionary_size={category_dictionary_size}"
    ).encode("utf-8")


def combiner_params_digest(combiner_id: int, trim_bps: int, category_dictionary_size: int) -> bytes:
    return sha256(combiner_params_canonical_bytes(combiner_id, trim_bps, category_dictionary_size))


def merkle_leaf_hash(leaf: bytes) -> bytes:
    return sha256(MERKLE_LEAF_PREFIX + leaf)


def merkle_node_hash(left: bytes, right: bytes) -> bytes:
    return sha256(MERKLE_NODE_PREFIX + left + right)


def sorted_branches_merkle_root(branch_hashes: list[bytes]) -> bytes:
    """RFC 6962 style: separate leaf and node prefixes, and an odd node is promoted.

    Pairing an odd node with a copy of itself makes `[A, B, B]` and `[A, B, B, B]`
    produce the same root (the CVE-2012-2459 class), so it is never done here.
    """

    if not branch_hashes:
        raise AggregateError("merkle root over no branches")
    level = [merkle_leaf_hash(leaf) for leaf in sorted(branch_hashes)]
    while len(level) > 1:
        level = [
            merkle_node_hash(level[index], level[index + 1]) if index + 1 < len(level) else level[index]
            for index in range(0, len(level), 2)
        ]
    return level[0]


def parse_branch_result_bytes(data: bytes) -> BranchReceipt:
    """The guest's `MFB2` parser, in Python. Strict on every field.

    An unknown magic, version, kind or flag bit, a basis-point value above 10000, and
    any trailing byte are all errors: a branch result must have exactly one spelling,
    or it would have two hashes.
    """

    if len(data) < 4 + 1 + 1 + 4 + 1 + 32:
        raise AggregateError("branch receipt is too short")
    if data[:4] != MFB2_MAGIC:
        raise AggregateError("branch receipt magic is not MFB2")
    version = data[4]
    if version != MFB2_SCHEMA_VERSION:
        raise AggregateError(f"unsupported MFB2 version {version}")
    kind_id = data[5]
    if kind_id not in OUTPUT_KIND_BY_ID:
        raise AggregateError(f"unknown output kind id {kind_id}")
    branch_index = struct.unpack_from("<I", data, 6)[0]
    flags = data[10]
    if flags & ~FLAG_KNOWN:
        raise AggregateError(f"unknown MFB2 flag bits in {flags:#04x}")

    offset = 11
    scalar_value_bps: int | None = None
    if flags & FLAG_SCALAR:
        scalar_value_bps, offset = _read_bps(data, offset)
    if flags & FLAG_LOWER:
        _, offset = _read_bps(data, offset)
    if flags & FLAG_UPPER:
        _, offset = _read_bps(data, offset)
    categorical_label_index: int | None = None
    if flags & FLAG_CATEGORY:
        categorical_label_index, offset = _read_u8(data, offset)
    if flags & FLAG_SCORES:
        count, offset = _read_u8(data, offset)
        if count == 0 or count > MAX_NARRATIVE_SCORES:
            raise AggregateError(f"MFB2 narrative score count {count} is out of range")
        for _ in range(count):
            if offset + 4 > len(data):
                raise AggregateError("branch receipt is too short")
            offset += 4
            _, offset = _read_bps(data, offset)
    if offset + 32 > len(data):
        raise AggregateError("branch receipt is too short")
    if offset + 32 != len(data):
        raise AggregateError("branch receipt has trailing bytes")
    return BranchReceipt(
        branch_index=branch_index,
        output_kind=OUTPUT_KIND_BY_ID[kind_id],
        scalar_value_bps=scalar_value_bps,
        categorical_label_index=categorical_label_index,
        canonical_hash=data[offset : offset + 32],
        result_hash=sha256(data),
    )


def _read_u8(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 1 > len(data):
        raise AggregateError("branch receipt is too short")
    return data[offset], offset + 1


def _read_bps(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(data):
        raise AggregateError("branch receipt is too short")
    value = struct.unpack_from("<H", data, offset)[0]
    if value > BPS_SCALE:
        raise AggregateError(f"MFB2 basis-point value {value} is out of range")
    return value, offset + 2


def trim_count_from_bps(branch_count: int, trim_bps: int) -> int:
    """`floor(branch_count * trim_bps / 10000)`."""

    if isinstance(trim_bps, bool) or not isinstance(trim_bps, int):
        raise AggregateError("trim_bps must be an integer")
    if trim_bps < 0 or trim_bps >= BPS_SCALE:
        raise AggregateError(f"trim_bps must lie in [0, {BPS_SCALE}); got {trim_bps}")
    return branch_count * trim_bps // BPS_SCALE


def _round_half_up(numerator: int, denominator: int) -> int:
    """Exact integer round-half-up on a non-negative quotient.

    The guest commits an integer, so the value is computed in integers on both sides.
    No rounding mode and no floating-point unit has to agree for the journal to match.
    """

    if denominator <= 0:
        raise AggregateError("aggregate weight total is zero")
    if numerator < 0:
        raise AggregateError("aggregate value is negative")
    value = (numerator * 2 + denominator) // (denominator * 2)
    if value < 0 or value > BPS_SCALE:
        raise AggregateError(f"aggregate value {value} is outside the basis-point range")
    return value


def weighted_mean_bps(values: list[tuple[int, int]]) -> int:
    """Weighted mean of `(value_bps, weight)` pairs, rounded half up."""

    if not values:
        raise AggregateError("weighted mean over no branches")
    total_weight = 0
    weighted_sum = 0
    for value, weight in values:
        _check_bps(value)
        total_weight += weight
        weighted_sum += value * weight
    return _round_half_up(weighted_sum, total_weight)


def trimmed_mean_bps(values: list[int], outlier_count: int) -> int:
    """Mean of the retained values, rounded half up."""

    retained = retained_values(values, outlier_count)
    for value in retained:
        _check_bps(value)
    return _round_half_up(sum(retained), len(retained))


def retained_values(values: list[int], outlier_count: int) -> list[int]:
    """Drop the `outlier_count` values farthest from the lower median.

    Median: stable sort by value, take index `len/2`. Rejection order: distance
    descending, then value ascending, then original index ascending. Retained values
    keep their original order.
    """

    if not values:
        raise AggregateError("trimmed mean over no branches")
    if outlier_count < 0:
        raise AggregateError("outlier count must not be negative")
    if outlier_count >= len(values):
        raise AggregateError(f"trim count {outlier_count} of {len(values)} leaves nothing")
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    median = indexed[len(indexed) // 2][1]
    by_rejection = sorted(indexed, key=lambda item: (-abs(item[1] - median), item[1], item[0]))
    rejected = {index for index, _ in by_rejection[:outlier_count]}
    return [value for index, value in enumerate(values) if index not in rejected]


def majority_vote(votes: list[tuple[int, int]]) -> int:
    """Highest accumulated weight wins; the lowest category breaks a tie."""

    if not votes:
        raise AggregateError("majority vote over no branches")
    totals: dict[int, int] = {}
    for category, weight in votes:
        if weight == 0:
            continue
        totals[category] = totals.get(category, 0) + weight
    if not totals:
        raise AggregateError("every majority-vote weight is zero")
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _check_bps(value: int) -> None:
    if value < 0 or value > BPS_SCALE:
        raise AggregateError(f"branch value {value} is outside the basis-point range")


def build_aggregate_artifact(
    *,
    parent_run: str,
    parent_manifest_cid: str,
    output_schema_hash: str,
    combiner: str,
    combiner_parameters: dict[str, Any],
    branches: list[dict[str, Any]],
    aggregate_plan_cid: str | None = None,
) -> bytes:
    """Canonical MFA3 bytes.

    `branches` entries carry `branch_index`, `job`, `output_cid`, `result_bytes` (hex
    of the on-chain receipt) and `weight`. `result_hash` is derived here rather than
    accepted, so the artifact cannot be built with a hash that does not match its own
    bytes.

    `aggregate_plan_cid` is the CID `predict open` pinned before any branch ran. The
    guest ignores the field -- it reads only `combiner_id`, `combiner_parameters` and
    `branches` -- but `input_digest` is taken over the whole artifact and the job's
    `input_bundle_hash` is fixed at open time, so carrying it here commits the plan on
    chain. Without it, nothing outside the customer's own machine says which combiner
    and which branches this run committed to before its results were visible. It is
    optional so the checked-in vectors, which predate the field, still rebuild exactly.
    """

    combiner_id = combiner_id_for(combiner)
    entries = []
    for branch in sorted(branches, key=lambda item: int(item["branch_index"])):
        result_bytes = bytes.fromhex(branch["result_bytes"]) if isinstance(branch["result_bytes"], str) else bytes(branch["result_bytes"])
        entries.append(
            {
                "branch_index": int(branch["branch_index"]),
                "job": str(branch["job"]),
                "output_cid": str(branch["output_cid"]),
                "result_bytes": result_bytes.hex(),
                "result_hash": sha256(result_bytes).hex(),
                "weight": int(branch.get("weight", 1)),
            }
        )
    artifact: dict[str, Any] = {
        "schema": AGGREGATE_SCHEMA,
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "parent_run": parent_run,
        "parent_manifest_cid": parent_manifest_cid,
        "output_schema_hash": output_schema_hash,
        "combiner": combiner,
        "combiner_id": combiner_id,
        "combiner_parameters": dict(combiner_parameters),
        "branches": entries,
    }
    if aggregate_plan_cid is not None:
        artifact["aggregate_plan_cid"] = str(aggregate_plan_cid)
    return canonical_json_bytes(artifact)


def reduce_aggregate_artifact(artifact: bytes) -> AggregateReduction:
    """The guest's reduction, in Python. Rejects exactly what the guest rejects."""

    try:
        value = json.loads(artifact.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregateError(f"aggregate artifact is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AggregateError("aggregate artifact must be a JSON object")

    schema = _string(value, "schema")
    if schema != AGGREGATE_SCHEMA:
        raise AggregateError(f"unknown aggregate artifact schema {schema!r}")
    schema_version = _unsigned(value, "schema_version")
    if schema_version != AGGREGATE_SCHEMA_VERSION:
        raise AggregateError(f"unknown aggregate artifact schema version {schema_version}")

    combiner_id = _unsigned(value, "combiner_id")
    if combiner_id not in COMBINER_NAMES:
        raise AggregateError(f"unknown combiner id {combiner_id}")
    if COMBINER_NAMES[combiner_id] != _string(value, "combiner"):
        raise AggregateError("combiner name and combiner id disagree")

    parameters = value.get("combiner_parameters")
    if not isinstance(parameters, dict):
        raise AggregateError("combiner_parameters must be an object")
    trim_bps = _optional_unsigned(parameters, "trim_bps") or 0
    dictionary_size = _optional_unsigned(parameters, "category_dictionary_size") or 0
    params_digest = combiner_params_digest(combiner_id, trim_bps, dictionary_size)

    branches = value.get("branches")
    if not isinstance(branches, list):
        raise AggregateError("branches must be an array")
    if not branches:
        raise AggregateError("aggregate artifact carries no branches")
    if len(branches) > MAX_BRANCHES:
        raise AggregateError(f"aggregate artifact carries {len(branches)} branches; the cap is {MAX_BRANCHES}")

    branch_hashes: list[bytes] = []
    scalars: list[tuple[int, int]] = []
    scalar_values: list[int] = []
    votes: list[tuple[int, int]] = []
    previous_index: int | None = None

    for position, branch in enumerate(branches):
        if not isinstance(branch, dict):
            raise AggregateError(f"branches[{position}] is not an object")
        declared_index = _unsigned(branch, "branch_index")
        if previous_index is not None and declared_index <= previous_index:
            raise AggregateError(f"branches[{position}] does not increase branch_index")
        previous_index = declared_index

        receipt_bytes = _hex_bytes(branch, "result_bytes")
        receipt = parse_branch_result_bytes(receipt_bytes)
        if receipt.branch_index != declared_index:
            raise AggregateError(
                f"branch {declared_index} declares an index the receipt does not carry ({receipt.branch_index})"
            )
        declared_hash = _hex_bytes(branch, "result_hash")
        if declared_hash != receipt.result_hash:
            raise AggregateError(f"branch {declared_index} result_hash is not sha256(result_bytes)")
        weight = _unsigned(branch, "weight")
        if weight == 0:
            raise AggregateError(f"branch {declared_index} has weight 0")
        if combiner_id == COMBINER_TRIMMED_MEAN and weight != 1:
            raise AggregateError(f"branch {declared_index} carries a weight trimmed-mean would ignore")
        branch_hashes.append(receipt.result_hash)

        if combiner_id == COMBINER_MAJORITY_VOTE:
            if receipt.categorical_label_index is None:
                raise AggregateError(f"branch {declared_index} carries no categorical label")
            if dictionary_size == 0:
                raise AggregateError("majority-vote needs combiner_parameters.category_dictionary_size")
            if receipt.categorical_label_index >= dictionary_size:
                raise AggregateError(
                    f"branch {declared_index} label {receipt.categorical_label_index} is outside the committed dictionary"
                )
            votes.append((receipt.categorical_label_index, weight))
        else:
            if receipt.output_kind not in SCALAR_OUTPUT_KINDS or receipt.scalar_value_bps is None:
                raise AggregateError(f"branch {declared_index} carries no scalar value")
            scalars.append((receipt.scalar_value_bps, weight))
            scalar_values.append(receipt.scalar_value_bps)

    if combiner_id == COMBINER_WEIGHTED_MEAN:
        result_value = weighted_mean_bps(scalars)
    elif combiner_id == COMBINER_TRIMMED_MEAN:
        result_value = trimmed_mean_bps(scalar_values, trim_count_from_bps(len(scalar_values), trim_bps))
    else:
        result_value = majority_vote(votes)

    return AggregateReduction(
        combiner_id=combiner_id,
        trim_bps=trim_bps,
        category_dictionary_size=dictionary_size,
        params_digest=params_digest,
        result_value=result_value,
        branch_count=len(branch_hashes),
        branch_hashes=tuple(branch_hashes),
        merkle_root=sorted_branches_merkle_root(branch_hashes),
    )


def aggregate_committed_outputs(reduction: AggregateReduction) -> bytes:
    out = bytearray()
    out.append(reduction.combiner_id)
    out += reduction.params_digest
    out += struct.pack("<I", reduction.result_value)
    out += struct.pack("<I", reduction.branch_count)
    out += reduction.merkle_root
    if len(out) != AGGREGATE_COMMITTED_OUTPUTS_LEN:
        raise AggregateError("aggregate committed outputs have the wrong length")
    return bytes(out)


def aggregate_journal(artifact: bytes) -> AggregateJournal:
    """Everything `open_job` needs so the reducer's Bonsol callback can settle the job."""

    return AggregateJournal(input_digest=framed_input_digest(artifact), reduction=reduce_aggregate_artifact(artifact))


def _string(value: dict[str, Any], field: str) -> str:
    found = value.get(field)
    if not isinstance(found, str):
        raise AggregateError(f"{field} must be a string")
    return found


def _unsigned(value: dict[str, Any], field: str) -> int:
    found = value.get(field)
    if isinstance(found, bool) or not isinstance(found, int) or found < 0:
        raise AggregateError(f"{field} must be a non-negative integer")
    return found


def _optional_unsigned(value: dict[str, Any], field: str) -> int | None:
    if value.get(field) is None:
        return None
    return _unsigned(value, field)


def _hex_bytes(value: dict[str, Any], field: str) -> bytes:
    text = _string(value, field)
    if len(text) % 2 or any(character not in _HEX_DIGITS for character in text):
        raise AggregateError(f"{field} must be lowercase hex")
    return bytes.fromhex(text)
