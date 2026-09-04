"""Python mirrors of `protocol/bonsol-branch-reducer/src/lib.rs`.

Each function reproduces the Rust definition step for step, including the
integer accumulation, the f64 division, the sort orders and the tie-breaks.
`worker/tests/test_aggregator_combiners.py` checks them against vectors that
were computed by running the Rust crate itself.

The Rust functions return an f64 mean. The runner commits basis points, so
`mean_to_bps` states the rounding rule once: round half up to the nearest
integer. That step is the runner's, not the reducer's.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum


COMBINER_WEIGHTED_MEAN = 1
COMBINER_TRIMMED_MEAN = 2
COMBINER_MAJORITY_VOTE = 3

COMBINER_IDS = {
    "weighted-mean": COMBINER_WEIGHTED_MEAN,
    "trimmed-mean": COMBINER_TRIMMED_MEAN,
    "majority-vote": COMBINER_MAJORITY_VOTE,
}

BPS_SCALE = 10000
I64_MIN = -(2**63)
I64_MAX = 2**63 - 1
U64_MAX = 2**64 - 1
U32_MAX = 2**32 - 1


class CombinerErrorKind(str, Enum):
    EMPTY_BRANCHES = "EmptyBranches"
    ZERO_WEIGHT = "ZeroWeight"
    TRIM_COUNT_TOO_LARGE = "TrimCountTooLarge"
    UNKNOWN_COMBINER = "UnknownCombiner"


class CombinerError(ValueError):
    def __init__(self, kind: CombinerErrorKind, detail: str = "") -> None:
        self.kind = kind
        super().__init__(f"{kind.value}{': ' + detail if detail else ''}")


@dataclass(frozen=True)
class WeightedValue:
    value: int
    weight: int

    def __post_init__(self) -> None:
        _check_i64(self.value, "value")
        _check_u64(self.weight, "weight")


@dataclass(frozen=True)
class CategoricalVote:
    category: int
    weight: int

    def __post_init__(self) -> None:
        _check_u32(self.category, "category")
        _check_u64(self.weight, "weight")


@dataclass(frozen=True)
class TrimmedMeanResult:
    mean: float
    rejected_count: int


def combiner_id(name: str) -> int:
    """Map the manifest combiner name to the on-chain registry id. Unknown names fail closed."""

    try:
        return COMBINER_IDS[name]
    except KeyError as exc:
        raise CombinerError(CombinerErrorKind.UNKNOWN_COMBINER, repr(name)) from exc


def validate_combiner_id(value: int) -> None:
    if value not in {COMBINER_WEIGHTED_MEAN, COMBINER_TRIMMED_MEAN, COMBINER_MAJORITY_VOTE}:
        raise CombinerError(CombinerErrorKind.UNKNOWN_COMBINER, str(value))


def weighted_mean(branches: list[WeightedValue]) -> float:
    """Rust: sum(value*weight) as i128 / sum(weight) as u128, both cast to f64 before dividing."""

    if not branches:
        raise CombinerError(CombinerErrorKind.EMPTY_BRANCHES)
    total_weight = sum(branch.weight for branch in branches)
    if total_weight == 0:
        raise CombinerError(CombinerErrorKind.ZERO_WEIGHT)
    weighted_sum = sum(branch.value * branch.weight for branch in branches)
    return float(weighted_sum) / float(total_weight)


def trimmed_mean(values: list[int], outlier_count: int) -> TrimmedMeanResult:
    """Rust: drop the `outlier_count` values farthest from the lower median, then average the rest.

    Median: stable sort by value, take index len/2. Rejection order: distance
    descending, then value ascending, then original index ascending. Retained
    values keep their original order; the mean is sum as i128 / len as f64.
    """

    if not values:
        raise CombinerError(CombinerErrorKind.EMPTY_BRANCHES)
    for value in values:
        _check_i64(value, "value")
    if outlier_count < 0:
        raise ValueError("outlier_count must not be negative")
    if outlier_count >= len(values):
        raise CombinerError(CombinerErrorKind.TRIM_COUNT_TOO_LARGE, f"{outlier_count} of {len(values)}")
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    median = indexed[len(indexed) // 2][1]
    by_rejection = sorted(indexed, key=lambda item: (-abs(item[1] - median), item[1], item[0]))
    rejected = {index for index, _ in by_rejection[:outlier_count]}
    retained = [value for index, value in enumerate(values) if index not in rejected]
    return TrimmedMeanResult(float(sum(retained)) / float(len(retained)), outlier_count)


def majority_vote(votes: list[CategoricalVote]) -> int:
    """Rust: accumulate weights per category (zero weights skipped), highest weight wins, lowest category breaks ties."""

    if not votes:
        raise CombinerError(CombinerErrorKind.EMPTY_BRANCHES)
    totals: dict[int, int] = {}
    for vote in votes:
        if vote.weight == 0:
            continue
        totals[vote.category] = totals.get(vote.category, 0) + vote.weight
    if not totals:
        raise CombinerError(CombinerErrorKind.ZERO_WEIGHT)
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return ranked[0][0]


def mean_to_bps(mean: float) -> int:
    """Runner rule: round the f64 mean half up to an integer basis-point value in [0, 10000]."""

    rounded = int(Decimal(repr(mean)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if rounded < 0 or rounded > BPS_SCALE:
        raise ValueError(f"mean {mean!r} is outside the basis-point range")
    return rounded


def trim_count_from_bps(branch_count: int, trim_bps: int) -> int:
    """Manifest `trim_bps` to the Rust `outlier_count`: floor(branch_count * trim_bps / 10000)."""

    if isinstance(trim_bps, bool) or not isinstance(trim_bps, int):
        raise ValueError("trim_bps must be an integer")
    if trim_bps < 0 or trim_bps >= BPS_SCALE:
        raise ValueError(f"trim_bps must lie in [0, {BPS_SCALE}); got {trim_bps}")
    return (branch_count * trim_bps) // BPS_SCALE


def _check_i64(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < I64_MIN or value > I64_MAX:
        raise ValueError(f"{name} must be an i64 integer; got {value!r}")


def _check_u64(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > U64_MAX:
        raise ValueError(f"{name} must be a u64 integer; got {value!r}")


def _check_u32(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > U32_MAX:
        raise ValueError(f"{name} must be a u32 integer; got {value!r}")
