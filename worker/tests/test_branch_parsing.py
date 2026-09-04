from __future__ import annotations

import pytest

from app.protocol.branch_schemas import BranchInput
from branch_worker.parsing import (
    InvalidModelOutputError,
    ParsedCategorical,
    ParsedNarrative,
    ParsedScalar,
    parse_bps_integer,
    parse_guardrail_scores,
    parse_label_index,
    parse_model_response,
    parse_probability_bps,
)


def _input(kind: str, **parameters: object) -> BranchInput:
    return BranchInput(parent_job="parent", branch_index=1, seed="q", parameters=parameters, rng_seed=9, target_output_kind=kind)


# The five values from the review, applied to both field declarations.
@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 10000), (1.0, 10000), (0.5, 5000), ("0.5", 5000), (0, 0), ("1", 10000), (0.12345, 1235)],
)
def test_probability_field_reads_unit_interval_values(value: object, expected: int) -> None:
    assert parse_probability_bps(value, field="scalar_value") == expected


@pytest.mark.parametrize("value", [5000, 1.5, -0.1, "abc", "", None, True, float("nan"), [0.5], {"v": 0.5}])
def test_probability_field_rejects_values_outside_the_contract(value: object) -> None:
    with pytest.raises(InvalidModelOutputError):
        parse_probability_bps(value, field="scalar_value")


@pytest.mark.parametrize(("value", "expected"), [(1, 1), (5000, 5000), (0, 0), (10000, 10000)])
def test_bps_field_reads_integers_only(value: object, expected: int) -> None:
    assert parse_bps_integer(value, field="severity_bps") == expected


@pytest.mark.parametrize("value", [1.0, 0.5, "0.5", "5000", 10001, -1, True, None])
def test_bps_field_rejects_floats_strings_and_out_of_range(value: object) -> None:
    with pytest.raises(InvalidModelOutputError):
        parse_bps_integer(value, field="severity_bps")


def test_label_index_is_checked_against_the_committed_labels() -> None:
    assert parse_label_index(2, field="categorical_label_index", label_count=3) == 2
    assert parse_label_index(7, field="categorical_label_index", label_count=0) == 7
    for bad in (3, -1, 256, "2", 2.0, True, None):
        with pytest.raises(InvalidModelOutputError):
            parse_label_index(bad, field="categorical_label_index", label_count=3)


def test_categorical_garbage_is_rejected_not_defaulted_to_zero() -> None:
    branch_input = _input("categorical", labels=["a", "b"])
    with pytest.raises(InvalidModelOutputError, match="missing"):
        parse_model_response(branch_input, {"answer": "b"})
    with pytest.raises(InvalidModelOutputError):
        parse_model_response(branch_input, {"categorical_label_index": "b"})
    assert parse_model_response(branch_input, {"label_index": 1}) == ParsedCategorical(1)


def test_scalar_response_requires_a_scalar_and_ordered_bounds() -> None:
    branch_input = _input("scalar")
    parsed = parse_model_response(branch_input, {"scalar_value": 0.7, "confidence_lower": 0.6, "confidence_upper": 0.8, "rationale": "r"})
    assert parsed == ParsedScalar(7000, 6000, 8000, "r")
    assert parse_model_response(branch_input, {"probability": "0.25"}) == ParsedScalar(2500, None, None, None)
    with pytest.raises(InvalidModelOutputError, match="missing scalar_value"):
        parse_model_response(branch_input, {"rationale": "no number"})
    with pytest.raises(InvalidModelOutputError, match="exceeds"):
        parse_model_response(branch_input, {"scalar_value": 0.5, "confidence_lower": 0.7, "confidence_upper": 0.6})
    with pytest.raises(InvalidModelOutputError):
        parse_model_response(branch_input, {"scalar_value": 0.5, "rationale": ""})
    with pytest.raises(InvalidModelOutputError):
        parse_model_response(branch_input, ["not", "an", "object"])


def test_narrative_guardrails_are_never_defaulted() -> None:
    branch_input = _input("narrative_with_scalar")
    with pytest.raises(InvalidModelOutputError, match="missing guardrail severity"):
        parse_model_response(branch_input, {"scalar_value": 0.4, "narrative_text": "text"})
    with pytest.raises(InvalidModelOutputError, match="missing guardrail ood"):
        parse_model_response(branch_input, {"scalar_value": 0.4, "narrative_text": "text", "narrative_scores": {"severity": 0.1, "quality": 0.9}})
    with pytest.raises(InvalidModelOutputError, match="narrative_text"):
        parse_model_response(branch_input, {"scalar_value": 0.4, "narrative_scores": {"severity": 0.1, "quality": 0.9, "ood": 0.0}})
    with pytest.raises(InvalidModelOutputError, match="missing scalar_value"):
        parse_model_response(branch_input, {"narrative_text": "t", "narrative_scores": {"severity": 0.1, "quality": 0.9, "ood": 0.0}})


def test_narrative_integer_one_is_a_probability_not_one_basis_point() -> None:
    branch_input = _input("narrative_with_scalar")
    parsed = parse_model_response(
        branch_input,
        {"scalar_value": 1, "narrative_text": "sure", "narrative_scores": {"severity": 1, "quality": 0.5, "ood": 0}},
    )
    assert parsed == ParsedNarrative(10000, "sure", {"severity_bps": 10000, "quality_bps": 5000, "ood_bps": 0})


def test_narrative_bps_form_is_accepted_and_mixed_forms_are_rejected() -> None:
    scores = parse_guardrail_scores({"scores": {"severity_bps": 1, "quality_bps": 5000, "ood_bps": 10000}})
    assert scores == {"severity_bps": 1, "quality_bps": 5000, "ood_bps": 10000}
    top_level = parse_guardrail_scores({"severity": 0.2, "quality_bps": 300, "ood": "0.5"})
    assert top_level == {"severity_bps": 2000, "quality_bps": 300, "ood_bps": 5000}
    with pytest.raises(InvalidModelOutputError, match="both"):
        parse_guardrail_scores({"narrative_scores": {"severity": 0.2, "severity_bps": 2000, "quality": 0.1, "ood": 0.1}})
    with pytest.raises(InvalidModelOutputError, match="JSON object"):
        parse_guardrail_scores({"narrative_scores": [0.1, 0.2, 0.3]})
    with pytest.raises(InvalidModelOutputError):
        parse_guardrail_scores({"severity": 5000, "quality": 0.1, "ood": 0.1})
