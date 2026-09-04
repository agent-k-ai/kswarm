from __future__ import annotations

import json

import pytest

from app.protocol.branch_schemas import BranchInput, BranchOutput, CanonicalHash
from app.protocol.canonical_hash import (
    branch_output_result_bytes,
    parse_scalar_to_bps,
    parse_branch_output_result_bytes,
    snap_scalar_to_bps,
)


def test_branch_input_defaults_scalar_grid() -> None:
    payload = BranchInput(
        parent_job="11111111111111111111111111111111",
        branch_index=3,
        seed="Will sentiment be negative?",
        parameters={"domain": "public-opinion"},
        rng_seed=42,
        target_output_kind="scalar",
    )

    assert payload.scalar_grid_bps == 1
    assert payload.model_dump(mode="json")["schema_version"] == 1


def test_branch_output_hash_excludes_narrative_text_and_timestamp() -> None:
    base = BranchOutput(
        parent_job="11111111111111111111111111111111",
        branch_index=1,
        output_kind="narrative_with_scalar",
        scalar_value_bps=6123,
        narrative_text="first wording",
        narrative_scores={"severity_bps": 7000, "quality_bps": 8200, "ood_bps": 300},
        rng_seed=7,
        llm_model="local-model",
        llm_version_hash="a" * 64,
        completed_at_unix=100,
        transcript_cid="bafkreitranscript",
    )
    changed_text = base.model_copy(update={"narrative_text": "different wording", "completed_at_unix": 200})
    changed_score = base.model_copy(update={"narrative_scores": {"severity_bps": 7001, "quality_bps": 8200, "ood_bps": 300}})

    assert CanonicalHash.of(base) == CanonicalHash.of(changed_text)
    assert CanonicalHash.of(base) != CanonicalHash.of(changed_score)


def test_branch_result_bytes_round_trip() -> None:
    output = BranchOutput(
        parent_job="11111111111111111111111111111111",
        branch_index=9,
        output_kind="scalar",
        scalar_value_bps=5010,
        scalar_confidence_lower_bps=4500,
        scalar_confidence_upper_bps=5500,
        rng_seed=99,
        llm_model="local-model",
        llm_version_hash="b" * 64,
        completed_at_unix=123,
        transcript_cid="bafkreitranscript",
    )

    result = branch_output_result_bytes(output)
    decoded = parse_branch_output_result_bytes(result)

    assert len(result) <= 512
    assert decoded["schema"] == "MFB2"
    assert decoded["output_kind"] == "scalar"
    assert decoded["branch_index"] == 9
    assert decoded["scalar_value_bps"] == 5010
    assert decoded["canonical_hash"] == CanonicalHash.of(output).hex()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-1.0, 0),
        (0.0, 0),
        (0.12344, 1234),
        (0.12345, 1235),
        (0.99999, 10000),
        (2.0, 10000),
    ],
)
def test_snap_scalar_to_bps(value: float, expected: int) -> None:
    assert snap_scalar_to_bps(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.42", 4200),
        (0.42, 4200),
        (1.5, 10000),
    ],
)
def test_parse_scalar_to_bps_accepts_numeric_json_values(value: object, expected: int) -> None:
    assert parse_scalar_to_bps(value, field="probability") == expected


def test_parse_scalar_to_bps_rejects_non_numeric_string() -> None:
    with pytest.raises(ValueError, match="probability must be numeric"):
        parse_scalar_to_bps("abc", field="probability")


def test_branch_output_rejects_narrative_without_scores() -> None:
    with pytest.raises(ValueError, match="narrative_scores"):
        BranchOutput(
            parent_job="11111111111111111111111111111111",
            branch_index=1,
            output_kind="narrative_with_scalar",
            narrative_text="text",
            rng_seed=7,
            llm_model="local-model",
            llm_version_hash="a" * 64,
            completed_at_unix=100,
            transcript_cid="bafkreitranscript",
        )


def test_branch_output_serializes_json() -> None:
    output = BranchOutput(
        parent_job="11111111111111111111111111111111",
        branch_index=1,
        output_kind="categorical",
        categorical_label_index=2,
        rng_seed=7,
        llm_model="local-model",
        llm_version_hash="a" * 64,
        completed_at_unix=100,
        transcript_cid="bafkreitranscript",
    )

    assert json.loads(output.model_dump_json())["categorical_label_index"] == 2
