from __future__ import annotations

import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.protocol.canonical_hash import canonical_json_bytes, snap_scalar_to_bps


json_scalars = st.one_of(st.none(), st.booleans(), st.integers(min_value=-10_000, max_value=10_000), st.text(max_size=24))
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=5),
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonical_json_round_trips(value: object) -> None:
    encoded = canonical_json_bytes(value)

    assert encoded == canonical_json_bytes(json.loads(encoded.decode("utf-8")))
    assert b": " not in encoded
    assert b", " not in encoded


@given(st.dictionaries(st.text(min_size=1, max_size=8), json_scalars, min_size=1, max_size=8))
def test_canonical_json_sorts_dict_keys(value: dict[str, object]) -> None:
    reversed_items = dict(reversed(list(value.items())))

    assert canonical_json_bytes(value) == canonical_json_bytes(reversed_items)


@given(st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False))
def test_snap_scalar_is_deterministic_and_bounded(value: float) -> None:
    first = snap_scalar_to_bps(value)
    second = snap_scalar_to_bps(value)

    assert first == second
    assert 0 <= first <= 10000


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_snap_scalar_rejects_non_finite(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        snap_scalar_to_bps(value)
