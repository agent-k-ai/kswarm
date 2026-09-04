"""Strict parsing of model responses into committed branch fields.

Every value that reaches the canonical hash must come from the model response.
Nothing here invents a default. A response that does not satisfy the declared
contract raises :class:`InvalidModelOutputError`, and the caller decides whether
to retry the same request or to give up without submitting.

Number rules (explicit, so the int/float confusion cannot come back):

* A *probability* field is declared in the unit interval ``[0, 1]``. It accepts
  a JSON number or a numeric string whose value lies in ``[0, 1]``. Integers
  ``0`` and ``1`` are the interval end points. The value is never read as basis
  points, so ``5000`` in a probability field is an error.
* A *bps* field is declared in basis points ``[0, 10000]``. It accepts a JSON
  integer only. Floats and strings are errors, so ``0.5`` in a bps field is an
  error and ``1`` in a bps field is one basis point.
* Booleans are never numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.protocol.branch_schemas import BranchInput


BPS_SCALE = 10000
MAX_LABEL_INDEX = 255
GUARDRAIL_NAMES = ("severity", "quality", "ood")
SCALAR_FIELD_NAMES = ("scalar_value", "probability")
LABEL_FIELD_NAMES = ("categorical_label_index", "label_index")
NARRATIVE_FIELD_NAMES = ("narrative_text", "narrative", "rationale", "explanation")
SCORE_CONTAINER_NAMES = ("narrative_scores", "scores")


class InvalidModelOutputError(ValueError):
    """The model response does not satisfy the branch output contract."""


@dataclass(frozen=True)
class ParsedScalar:
    scalar_value_bps: int
    confidence_lower_bps: int | None
    confidence_upper_bps: int | None
    narrative_text: str | None


@dataclass(frozen=True)
class ParsedCategorical:
    label_index: int


@dataclass(frozen=True)
class ParsedNarrative:
    scalar_value_bps: int
    narrative_text: str
    narrative_scores: dict[str, int]


ParsedResponse = ParsedScalar | ParsedCategorical | ParsedNarrative


def parse_probability_bps(value: Any, *, field: str) -> int:
    """Parse a unit-interval probability into basis points, rounding half up."""

    numeric = _numeric(value, field=field, allow_string=True, allow_float=True)
    if numeric < 0 or numeric > 1:
        raise InvalidModelOutputError(f"{field} must lie in [0, 1]; got {value!r}")
    snapped = Decimal(repr(numeric)) * Decimal(BPS_SCALE)
    return int(snapped.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_bps_integer(value: Any, *, field: str) -> int:
    """Parse a field declared in basis points. Integers in [0, 10000] only."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidModelOutputError(f"{field} must be a JSON integer in basis points; got {value!r}")
    if value < 0 or value > BPS_SCALE:
        raise InvalidModelOutputError(f"{field} must lie in [0, {BPS_SCALE}]; got {value!r}")
    return value


def parse_label_index(value: Any, *, field: str, label_count: int) -> int:
    """Parse a categorical label index against the committed label dictionary."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidModelOutputError(f"{field} must be a JSON integer; got {value!r}")
    if value < 0 or value > MAX_LABEL_INDEX:
        raise InvalidModelOutputError(f"{field} must lie in [0, {MAX_LABEL_INDEX}]; got {value!r}")
    if label_count > 0 and value >= label_count:
        raise InvalidModelOutputError(f"{field} {value} is outside the {label_count} committed labels")
    return value


def parse_model_response(branch_input: BranchInput, response: Any) -> ParsedResponse:
    """Dispatch on the branch's declared output kind. Never fills in a default."""

    if not isinstance(response, dict):
        raise InvalidModelOutputError(f"model response must be a JSON object; got {type(response).__name__}")
    kind = branch_input.target_output_kind
    if kind == "scalar":
        return parse_scalar_response(response)
    if kind == "categorical":
        labels = branch_input.parameters.get("labels") or []
        return parse_categorical_response(response, label_count=len(labels))
    if kind == "narrative_with_scalar":
        return parse_narrative_response(response)
    raise InvalidModelOutputError(f"unsupported target_output_kind: {kind!r}")


def parse_scalar_response(response: dict[str, Any]) -> ParsedScalar:
    scalar_name, scalar_raw = _first_present(response, SCALAR_FIELD_NAMES)
    if scalar_name is None:
        raise InvalidModelOutputError(f"model response is missing {' or '.join(SCALAR_FIELD_NAMES)}")
    scalar_value_bps = parse_probability_bps(scalar_raw, field=scalar_name)
    lower = response.get("confidence_lower")
    upper = response.get("confidence_upper")
    lower_bps = parse_probability_bps(lower, field="confidence_lower") if lower is not None else None
    upper_bps = parse_probability_bps(upper, field="confidence_upper") if upper is not None else None
    if lower_bps is not None and upper_bps is not None and lower_bps > upper_bps:
        raise InvalidModelOutputError(f"confidence_lower {lower_bps} exceeds confidence_upper {upper_bps} (bps)")
    narrative_name, narrative_raw = _first_present(response, NARRATIVE_FIELD_NAMES)
    narrative_text: str | None = None
    if narrative_name is not None:
        narrative_text = _non_empty_text(narrative_raw, field=narrative_name)
    return ParsedScalar(scalar_value_bps, lower_bps, upper_bps, narrative_text)


def parse_categorical_response(response: dict[str, Any], *, label_count: int) -> ParsedCategorical:
    name, raw = _first_present(response, LABEL_FIELD_NAMES)
    if name is None:
        raise InvalidModelOutputError(f"model response is missing {' or '.join(LABEL_FIELD_NAMES)}")
    return ParsedCategorical(parse_label_index(raw, field=name, label_count=label_count))


def parse_narrative_response(response: dict[str, Any]) -> ParsedNarrative:
    scalar_name, scalar_raw = _first_present(response, SCALAR_FIELD_NAMES)
    if scalar_name is None:
        raise InvalidModelOutputError(f"model response is missing {' or '.join(SCALAR_FIELD_NAMES)}")
    scalar_value_bps = parse_probability_bps(scalar_raw, field=scalar_name)
    narrative_name, narrative_raw = _first_present(response, NARRATIVE_FIELD_NAMES)
    if narrative_name is None:
        raise InvalidModelOutputError(f"model response is missing {' or '.join(NARRATIVE_FIELD_NAMES)}")
    narrative_text = _non_empty_text(narrative_raw, field=narrative_name)
    return ParsedNarrative(scalar_value_bps, narrative_text, parse_guardrail_scores(response))


def parse_guardrail_scores(response: dict[str, Any]) -> dict[str, int]:
    """Parse the three required guardrails. Each must be present in exactly one form."""

    container = _score_container(response)
    scores: dict[str, int] = {}
    for name in GUARDRAIL_NAMES:
        bps_name = f"{name}_bps"
        has_probability = name in container
        has_bps = bps_name in container
        if has_probability and has_bps:
            raise InvalidModelOutputError(f"guardrail {name} is given as both {name} and {bps_name}")
        if has_probability:
            scores[bps_name] = parse_probability_bps(container[name], field=name)
        elif has_bps:
            scores[bps_name] = parse_bps_integer(container[bps_name], field=bps_name)
        else:
            raise InvalidModelOutputError(f"model response is missing guardrail {name} (as {name} in [0,1] or {bps_name})")
    return scores


def _score_container(response: dict[str, Any]) -> dict[str, Any]:
    for name in SCORE_CONTAINER_NAMES:
        if name in response:
            container = response[name]
            if not isinstance(container, dict):
                raise InvalidModelOutputError(f"{name} must be a JSON object; got {type(container).__name__}")
            return container
    return response


def _first_present(response: dict[str, Any], names: tuple[str, ...]) -> tuple[str | None, Any]:
    for name in names:
        if name in response and response[name] is not None:
            return name, response[name]
    return None, None


def _non_empty_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidModelOutputError(f"{field} must be a non-empty string; got {value!r}")
    return value


def _numeric(value: Any, *, field: str, allow_string: bool, allow_float: bool) -> float:
    if isinstance(value, bool):
        raise InvalidModelOutputError(f"{field} must be a number; got a boolean")
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float) and allow_float:
        if not math.isfinite(value):
            raise InvalidModelOutputError(f"{field} must be finite; got {value!r}")
        return value
    if isinstance(value, str) and allow_string:
        stripped = value.strip()
        try:
            numeric = float(stripped)
        except ValueError as exc:
            raise InvalidModelOutputError(f"{field} must be numeric; got {value!r}") from exc
        if not math.isfinite(numeric):
            raise InvalidModelOutputError(f"{field} must be finite; got {value!r}")
        return numeric
    raise InvalidModelOutputError(f"{field} must be a JSON number or numeric string; got {value!r}")
