"""Cross-checks against vectors computed by running protocol/bonsol-branch-reducer/src/lib.rs.

The vectors were produced on 2026-09-03 by a scratch binary that linked the
Rust crate unchanged and printed `weighted_mean`, `trimmed_mean` and
`majority_vote` results with `{:?}` (shortest round-trip f64 formatting).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aggregator_runner.combiners import (
    CategoricalVote,
    CombinerError,
    CombinerErrorKind,
    TrimmedMeanResult,
    WeightedValue,
    combiner_id,
    majority_vote,
    mean_to_bps,
    trim_count_from_bps,
    trimmed_mean,
    validate_combiner_id,
    weighted_mean,
)


RUST_WEIGHTED_MEAN = [
    ([(5000, 1), (7000, 1)], 6000.0),
    ([(1000, 3), (9000, 1)], 3000.0),
    ([(3333, 1), (3333, 1), (3334, 1)], 3333.3333333333335),
    ([(1, 1), (2, 1), (2, 1)], 1.6666666666666667),
    ([(5000, 1)], 5000.0),
    ([(10000, 7), (0, 3)], 7000.0),
    ([(1, 9999999999), (0, 1)], 0.9999999999),
]
RUST_WEIGHTED_MEAN_ERRORS = [
    ([(5000, 0), (7000, 0)], CombinerErrorKind.ZERO_WEIGHT),
    ([], CombinerErrorKind.EMPTY_BRANCHES),
]
RUST_TRIMMED_MEAN = [
    ([1, 2, 3, 100], 1, 2.0),
    ([10, 20, 30, 40], 1, 30.0),
    ([5, 5, 5, 5], 2, 5.0),
    ([7000, 1000, 9000, 5000, 5100], 2, 5700.0),
    ([0, 10000, 5000], 1, 7500.0),
    ([10000, 0, 5000], 1, 7500.0),
    ([4000, 6000], 1, 6000.0),
    ([4000, 6000], 0, 5000.0),
    ([1, 2, 4], 0, 2.3333333333333335),
    ([9, 1, 9, 1, 5, 5], 2, 7.0),
]
RUST_TRIMMED_MEAN_ERRORS = [
    ([3, 3], 2, CombinerErrorKind.TRIM_COUNT_TOO_LARGE),
    ([], 0, CombinerErrorKind.EMPTY_BRANCHES),
]
RUST_MAJORITY_VOTE = [
    ([(2, 1), (0, 1), (2, 1)], 2),
    ([(1, 5), (0, 5)], 0),
    ([(3, 0), (1, 2)], 1),
    ([(7, 1), (7, 1), (2, 3)], 2),
    ([(4, 2), (1, 1), (4, 1), (1, 2)], 1),
]
RUST_MAJORITY_VOTE_ERRORS = [
    ([(3, 0)], CombinerErrorKind.ZERO_WEIGHT),
    ([], CombinerErrorKind.EMPTY_BRANCHES),
]


@pytest.mark.parametrize(("items", "expected"), RUST_WEIGHTED_MEAN)
def test_weighted_mean_matches_rust(items: list[tuple[int, int]], expected: float) -> None:
    assert weighted_mean([WeightedValue(value, weight) for value, weight in items]) == expected


@pytest.mark.parametrize(("items", "kind"), RUST_WEIGHTED_MEAN_ERRORS)
def test_weighted_mean_errors_match_rust(items: list[tuple[int, int]], kind: CombinerErrorKind) -> None:
    with pytest.raises(CombinerError) as excinfo:
        weighted_mean([WeightedValue(value, weight) for value, weight in items])
    assert excinfo.value.kind is kind


@pytest.mark.parametrize(("values", "count", "expected"), RUST_TRIMMED_MEAN)
def test_trimmed_mean_matches_rust(values: list[int], count: int, expected: float) -> None:
    assert trimmed_mean(values, count) == TrimmedMeanResult(expected, count)


@pytest.mark.parametrize(("values", "count", "kind"), RUST_TRIMMED_MEAN_ERRORS)
def test_trimmed_mean_errors_match_rust(values: list[int], count: int, kind: CombinerErrorKind) -> None:
    with pytest.raises(CombinerError) as excinfo:
        trimmed_mean(values, count)
    assert excinfo.value.kind is kind


@pytest.mark.parametrize(("items", "expected"), RUST_MAJORITY_VOTE)
def test_majority_vote_matches_rust(items: list[tuple[int, int]], expected: int) -> None:
    assert majority_vote([CategoricalVote(category, weight) for category, weight in items]) == expected


@pytest.mark.parametrize(("items", "kind"), RUST_MAJORITY_VOTE_ERRORS)
def test_majority_vote_errors_match_rust(items: list[tuple[int, int]], kind: CombinerErrorKind) -> None:
    with pytest.raises(CombinerError) as excinfo:
        majority_vote([CategoricalVote(category, weight) for category, weight in items])
    assert excinfo.value.kind is kind


def test_combiner_registry_fails_closed() -> None:
    assert combiner_id("weighted-mean") == 1
    assert combiner_id("trimmed-mean") == 2
    assert combiner_id("majority-vote") == 3
    for name in ("mean", "weighted_mean", "", "llm-judge"):
        with pytest.raises(CombinerError) as excinfo:
            combiner_id(name)
        assert excinfo.value.kind is CombinerErrorKind.UNKNOWN_COMBINER
    for value in (0, 4, 255):
        with pytest.raises(CombinerError):
            validate_combiner_id(value)


def test_integer_domains_match_the_rust_types() -> None:
    with pytest.raises(ValueError):
        WeightedValue(2**63, 1)
    with pytest.raises(ValueError):
        WeightedValue(1, 2**64)
    with pytest.raises(ValueError):
        CategoricalVote(2**32, 1)
    with pytest.raises(ValueError):
        WeightedValue(True, 1)


@pytest.mark.parametrize(("mean", "expected"), [(6000.0, 6000), (3333.3333333333335, 3333), (2.5, 3), (0.9999999999, 1), (0.4, 0), (10000.0, 10000)])
def test_mean_to_bps_rounds_half_up(mean: float, expected: int) -> None:
    assert mean_to_bps(mean) == expected


def test_mean_to_bps_rejects_values_outside_the_range() -> None:
    with pytest.raises(ValueError):
        mean_to_bps(10000.5)
    with pytest.raises(ValueError):
        mean_to_bps(-0.5)


@pytest.mark.parametrize(("branches", "trim_bps", "expected"), [(4, 2500, 1), (4, 2499, 0), (8, 2500, 2), (3, 0, 0), (10, 9999, 9)])
def test_trim_count_from_bps_floors(branches: int, trim_bps: int, expected: int) -> None:
    assert trim_count_from_bps(branches, trim_bps) == expected


def test_trim_count_from_bps_rejects_bad_parameters() -> None:
    for bad in (10000, -1, 0.5, "2500", True):
        with pytest.raises(ValueError):
            trim_count_from_bps(4, bad)


@given(st.lists(st.tuples(st.integers(-1_000_000, 1_000_000), st.integers(1, 10_000)), min_size=1, max_size=40))
def test_weighted_mean_is_order_independent_within_rust_tolerance(items: list[tuple[int, int]]) -> None:
    forward = weighted_mean([WeightedValue(v, w) for v, w in items])
    backward = weighted_mean([WeightedValue(v, w) for v, w in reversed(items)])
    assert abs(forward - backward) <= 1e-9


@given(st.lists(st.integers(-1_000_000, 1_000_000), min_size=2, max_size=40), st.integers(0, 19))
def test_trimmed_mean_rejects_exactly_the_configured_count(values: list[int], outlier_count: int) -> None:
    if outlier_count >= len(values):
        with pytest.raises(CombinerError):
            trimmed_mean(values, outlier_count)
        return
    result = trimmed_mean(values, outlier_count)
    assert result.rejected_count == outlier_count
    assert min(values) <= result.mean <= max(values)


@given(st.integers(0, 999), st.integers(0, 999), st.integers(1, 10_000))
def test_majority_vote_tie_breaks_to_the_lowest_category(a: int, b: int, weight: int) -> None:
    if a == b:
        return
    assert majority_vote([CategoricalVote(a, weight), CategoricalVote(b, weight)]) == min(a, b)
