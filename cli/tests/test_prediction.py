from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kswarm_cli.prediction import (
    BPS_SCALE,
    COMBINERS,
    DEFAULT_TRIM_BPS,
    JOB_COMMITTED,
    JOB_DEFERRED,
    JOB_OPENED,
    JOB_PLANNED,
    NONCE_MAX,
    RUN_CANCELLED,
    RUN_OPEN,
    RUN_OPENING,
    RUN_SCHEMA_VERSION,
    combiner_parameters,
    job_entry_status,
    load_run_manifest,
    pending_job_entries,
    planned_nonces,
    random_base_nonce,
    run_is_resumable,
    run_job_entries,
    run_status,
    save_run_manifest,
    scalar_bps_to_probability,
    validate_combiner,
    validate_output_kind,
    validate_trim_bps,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REDUCER_LIB = REPO_ROOT / "protocol" / "bonsol-aggregate-reducer" / "src" / "combiner.rs"


def test_combiner_ids_match_the_reducer_registry() -> None:
    """`COMBINER_<NAME>: u8 = <id>` in the reducer crate is the on-chain registry."""

    source = REDUCER_LIB.read_text(encoding="utf-8")
    found = {name.lower().replace("_", "-"): int(value) for name, value in re.findall(r"pub const COMBINER_([A-Z_]+): u8 = (\d+);", source)}
    assert found == COMBINERS


def test_combiner_names_match_the_aggregate_reduction() -> None:
    """One Python combiner table: the one the aggregate journal is computed from."""

    from kswarm_cli.aggregate import COMBINER_IDS

    assert COMBINER_IDS == COMBINERS


@pytest.mark.parametrize("name", ["weighted-mean", "trimmed-mean", "majority-vote"])
def test_validate_combiner_accepts_registry_names(name: str) -> None:
    assert validate_combiner(name) == name


@pytest.mark.parametrize("name", ["mean", "trimmed_mean", "WEIGHTED-MEAN", "", "median"])
def test_validate_combiner_rejects_unknown_names(name: str) -> None:
    with pytest.raises(ValueError, match="unknown combiner"):
        validate_combiner(name)


def test_validate_output_kind() -> None:
    assert validate_output_kind("scalar") == "scalar"
    with pytest.raises(ValueError, match="output kind"):
        validate_output_kind("number")


def test_trimmed_mean_defaults_trim_bps_to_ten_percent() -> None:
    assert combiner_parameters("trimmed-mean", "scalar", None) == {"trim_bps": DEFAULT_TRIM_BPS}
    assert DEFAULT_TRIM_BPS == 1000
    assert combiner_parameters("trimmed-mean", "narrative_with_scalar", 2500) == {"trim_bps": 2500}
    assert combiner_parameters("trimmed-mean", "scalar", 0) == {"trim_bps": 0}
    assert combiner_parameters("trimmed-mean", "scalar", BPS_SCALE - 1) == {"trim_bps": 9999}


@pytest.mark.parametrize("value", [-1, BPS_SCALE, BPS_SCALE + 1, 10**9])
def test_trim_bps_range_matches_the_aggregator(value: int) -> None:
    with pytest.raises(ValueError, match="trim-bps"):
        validate_trim_bps(value)
    with pytest.raises(ValueError, match="trim-bps"):
        combiner_parameters("trimmed-mean", "scalar", value)


def test_trim_bps_rejects_bool_and_non_integers() -> None:
    with pytest.raises(ValueError, match="integer"):
        validate_trim_bps(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        validate_trim_bps(0.5)  # type: ignore[arg-type]


def test_other_combiners_carry_no_parameters_and_refuse_trim_bps() -> None:
    assert combiner_parameters("weighted-mean", "scalar", None) == {}
    assert combiner_parameters("majority-vote", "categorical", None) == {}
    with pytest.raises(ValueError, match="only applies to trimmed-mean"):
        combiner_parameters("weighted-mean", "scalar", 1000)
    with pytest.raises(ValueError, match="only applies to trimmed-mean"):
        combiner_parameters("majority-vote", "categorical", 0)


def test_combiner_output_kind_pairing_fails_closed_like_the_aggregator() -> None:
    with pytest.raises(ValueError, match="needs --output-kind scalar"):
        combiner_parameters("weighted-mean", "categorical", None)
    with pytest.raises(ValueError, match="needs --output-kind scalar"):
        combiner_parameters("trimmed-mean", "categorical", None)
    with pytest.raises(ValueError, match="needs --output-kind categorical"):
        combiner_parameters("majority-vote", "scalar", None)
    with pytest.raises(ValueError, match="unknown combiner"):
        combiner_parameters("median", "scalar", None)


def test_random_base_nonce_is_a_u64_that_leaves_room_for_the_aggregate() -> None:
    assert NONCE_MAX == 2**64 - 1
    branches = 128
    seen: set[int] = set()
    for _ in range(64):
        base = random_base_nonce(branches)
        assert 0 <= base <= NONCE_MAX - branches
        seen.add(base)
    assert len(seen) > 1, "base nonce is not random"
    largest = random_base_nonce(branches, randbelow=lambda upper: upper - 1)
    assert largest == NONCE_MAX - branches
    _, aggregate = planned_nonces(largest, branches)
    assert aggregate == NONCE_MAX
    assert random_base_nonce(0, randbelow=lambda upper: upper - 1) == NONCE_MAX


def test_random_base_nonce_uses_the_secrets_module_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_randbelow(upper: int) -> int:
        calls.append(upper)
        return 42

    monkeypatch.setattr("kswarm_cli.prediction.secrets.randbelow", fake_randbelow)
    # The default argument was bound at import time; call through the module attribute explicitly.
    from kswarm_cli import prediction

    assert prediction.random_base_nonce(3, randbelow=prediction.secrets.randbelow) == 42
    assert calls == [NONCE_MAX - 3 + 1]


def test_planned_nonces_are_consecutive_and_bounded() -> None:
    branch_nonces, aggregate = planned_nonces(1000, 4)
    assert branch_nonces == [1000, 1001, 1002, 1003]
    assert aggregate == 1004
    with pytest.raises(ValueError, match="u64"):
        planned_nonces(NONCE_MAX, 1)
    with pytest.raises(ValueError, match="u64"):
        planned_nonces(-1, 1)
    with pytest.raises(ValueError):
        random_base_nonce(-1)


@pytest.mark.parametrize(
    "value, expected",
    [(5000, 0.5), (0, 0.0), (10000, 1.0), (4321.5, 0.43215), (1, 0.0001), (None, None), (True, None), ("5000", None), ([5000], None)],
)
def test_scalar_bps_to_probability_one_rule(value, expected) -> None:
    result = scalar_bps_to_probability(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
        assert isinstance(result, float)


def _run(status: str = RUN_OPENING, aggregate_status: str = JOB_PLANNED) -> dict:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": status,
        "parent_run": "AGG",
        "base_nonce": 7,
        "branch_jobs": [
            {"kind": "branch", "branch_index": 0, "job": "B0", "status": JOB_COMMITTED},
            {"kind": "branch", "branch_index": 1, "job": "B1", "status": JOB_OPENED},
            {"kind": "branch", "branch_index": 2, "job": "B2", "status": JOB_PLANNED},
        ],
        "aggregate": {"kind": "aggregate", "branch_index": None, "job": "AGG", "status": aggregate_status},
    }


def test_run_job_entries_and_pending_order() -> None:
    run = _run()
    assert [entry["job"] for entry in run_job_entries(run)] == ["B0", "B1", "B2", "AGG"]
    assert [entry["job"] for entry in pending_job_entries(run)] == ["B1", "B2", "AGG"]
    deferred = _run(aggregate_status=JOB_DEFERRED)
    assert [entry["job"] for entry in pending_job_entries(deferred)] == ["B1", "B2"]


def test_schema_one_manifests_read_as_fully_open() -> None:
    legacy = {"schema_version": 1, "parent_run": "AGG", "branch_jobs": [{"branch_index": 0, "job": "B0"}]}
    assert run_status(legacy) == RUN_OPEN
    assert job_entry_status(legacy["branch_jobs"][0]) == JOB_COMMITTED
    assert [entry["job"] for entry in run_job_entries(legacy)] == ["B0"]
    assert pending_job_entries(legacy) == []
    resumable, reason = run_is_resumable(legacy)
    assert not resumable and "schema 1" in reason


def test_run_is_resumable_only_while_opening() -> None:
    assert run_is_resumable(_run()) == (True, "")
    ok, reason = run_is_resumable(_run(status=RUN_OPEN))
    assert not ok and "already fully open" in reason
    ok, reason = run_is_resumable(_run(status=RUN_CANCELLED))
    assert not ok and "cancelled" in reason
    ok, reason = run_is_resumable(_run(status="weird"))
    assert not ok and "unknown status" in reason


def test_save_run_manifest_is_atomic_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "AGG.json"
    run = _run()
    save_run_manifest(path, run)
    assert load_run_manifest(path) == run
    assert not path.with_name(".AGG.json.tmp").exists()
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == run
    run["status"] = RUN_OPEN
    save_run_manifest(path, run)
    assert load_run_manifest(path)["status"] == RUN_OPEN
    assert sorted(item.name for item in path.parent.iterdir()) == ["AGG.json"]


def test_save_run_manifest_leaves_the_old_file_when_the_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "AGG.json"
    save_run_manifest(path, _run())
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("kswarm_cli.prediction.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_run_manifest(path, _run(status=RUN_OPEN))
    assert path.read_bytes() == before
