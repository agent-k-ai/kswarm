"""Prediction-run rules that do not need a network: combiners, nonces, run manifests.

A run manifest (`~/.config/kswarm/predict_runs/<parent-run>.json`) is written
before the first on-chain transaction and rewritten after every confirmed one,
so an interrupted `predict open` can be continued with `predict resume` or
unwound with `predict cancel`.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any


# Combiner registry. Ids are the reducer's (`protocol/bonsol-branch-reducer/src/lib.rs`,
# `COMBINER_*`); names are the aggregator's (`worker/aggregator_runner/combiners.py`,
# `COMBINER_IDS`). `tests/test_prediction.py` checks both against the source files.
COMBINERS: dict[str, int] = {"weighted-mean": 1, "trimmed-mean": 2, "majority-vote": 3}
SCALAR_COMBINERS = frozenset({"weighted-mean", "trimmed-mean"})
CATEGORICAL_COMBINERS = frozenset({"majority-vote"})
OUTPUT_KINDS = ("scalar", "categorical", "narrative_with_scalar")
SCALAR_OUTPUT_KINDS = frozenset({"scalar", "narrative_with_scalar"})
BPS_SCALE = 10_000
# 10% of the branches, the Phase 1 ADR default for trimmed-mean.
DEFAULT_TRIM_BPS = 1_000

# `OpenJobArgs.job_nonce` is a u64.
NONCE_BITS = 64
NONCE_MAX = (1 << NONCE_BITS) - 1

RUN_SCHEMA_VERSION = 2
RUN_OPENING = "opening"
RUN_OPEN = "open"
RUN_CANCELLED = "cancelled"
JOB_PLANNED = "planned"
JOB_OPENED = "opened"
JOB_COMMITTED = "committed"
JOB_DEFERRED = "deferred"
JOB_CANCELLED = "cancelled"
PENDING_JOB_STATUSES = frozenset({JOB_PLANNED, JOB_OPENED})
ALREADY_OPEN_REASON = "run is already fully open"


def validate_output_kind(output_kind: str) -> str:
    if output_kind not in OUTPUT_KINDS:
        raise ValueError(f"output kind must be one of {', '.join(OUTPUT_KINDS)}; got {output_kind!r}")
    return output_kind


def validate_combiner(combiner: str) -> str:
    """Reject anything the aggregator would fail closed on."""

    if combiner not in COMBINERS:
        raise ValueError(f"unknown combiner {combiner!r}; expected one of {', '.join(COMBINERS)}")
    return combiner


def validate_trim_bps(trim_bps: int) -> int:
    """Aggregator `trim_count_from_bps` accepts an integer in [0, 10000)."""

    if isinstance(trim_bps, bool) or not isinstance(trim_bps, int):
        raise ValueError("--trim-bps must be an integer")
    if trim_bps < 0 or trim_bps >= BPS_SCALE:
        raise ValueError(f"--trim-bps must lie in [0, {BPS_SCALE}); got {trim_bps}")
    return trim_bps


def combiner_parameters(combiner: str, output_kind: str, trim_bps: int | None) -> dict[str, Any]:
    """The `combiner_parameters` object the aggregator reads from the parent manifest.

    `trimmed-mean` carries `trim_bps` (default 1000). The other combiners carry
    no parameters, and `--trim-bps` is an error for them so a typo cannot be
    ignored silently. Scalar combiners need a scalar output kind and
    `majority-vote` needs `categorical`, exactly as the aggregator enforces.
    """

    validate_combiner(combiner)
    validate_output_kind(output_kind)
    if combiner in SCALAR_COMBINERS and output_kind not in SCALAR_OUTPUT_KINDS:
        raise ValueError(f"combiner {combiner} needs --output-kind scalar or narrative_with_scalar; got {output_kind}")
    if combiner in CATEGORICAL_COMBINERS and output_kind != "categorical":
        raise ValueError(f"combiner {combiner} needs --output-kind categorical; got {output_kind}")
    if combiner == "trimmed-mean":
        return {"trim_bps": validate_trim_bps(DEFAULT_TRIM_BPS if trim_bps is None else trim_bps)}
    if trim_bps is not None:
        raise ValueError(f"--trim-bps only applies to trimmed-mean; combiner is {combiner}")
    return {}


def random_base_nonce(branches: int, randbelow: Callable[[int], int] = secrets.randbelow) -> int:
    """A random u64 base such that `base + branches` (the aggregate nonce) still fits a u64.

    Wall-clock bases collide when two runs start within `branches` milliseconds
    of each other; a random base makes a collision a 2^-64 event, and
    `predict open` still checks every planned PDA before spending escrow.
    """

    if branches < 0 or branches > NONCE_MAX:
        raise ValueError("branch count is outside the nonce range")
    return randbelow(NONCE_MAX - branches + 1)


def planned_nonces(base_nonce: int, branches: int) -> tuple[list[int], int]:
    """Branch nonces `base .. base+branches-1` and the aggregate nonce `base+branches`."""

    if base_nonce < 0 or branches < 0 or base_nonce + branches > NONCE_MAX:
        raise ValueError("planned nonces do not fit a u64")
    return [base_nonce + index for index in range(branches)], base_nonce + branches


def scalar_bps_to_probability(value: Any) -> float | None:
    """One rule for `final_scalar`: an int or float basis-point value divided by 10000, else None."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / BPS_SCALE


def save_run_manifest(path: Path, run: dict[str, Any]) -> None:
    """Write-temp, fsync, rename: a reader never sees a torn manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(run, indent=2, sort_keys=True) + "\n"
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def load_run_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_status(run: dict[str, Any]) -> str:
    """Schema 1 manifests predate the open plan; they were only written after a full open."""

    return str(run.get("status", RUN_OPEN))


def run_job_entries(run: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every job entry of the run: the branches in order, then the aggregate."""

    yield from run["branch_jobs"]
    aggregate = run.get("aggregate")
    if aggregate is not None:
        yield aggregate


def job_entry_status(entry: dict[str, Any]) -> str:
    return str(entry.get("status", JOB_COMMITTED))


def pending_job_entries(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Entries that still need `open_job` or `commit_input_artifact`."""

    return [entry for entry in run_job_entries(run) if job_entry_status(entry) in PENDING_JOB_STATUSES]


def run_is_resumable(run: dict[str, Any]) -> tuple[bool, str]:
    """Whether `predict resume` can act, and the reason when it cannot."""

    if int(run.get("schema_version", 1)) < RUN_SCHEMA_VERSION:
        return False, "run manifest predates incremental opens (schema 1); nothing to resume"
    status = run_status(run)
    if status == RUN_CANCELLED:
        return False, "run was cancelled; open a new run instead"
    if status == RUN_OPEN:
        return False, ALREADY_OPEN_REASON
    if status != RUN_OPENING:
        return False, f"run has unknown status {status!r}"
    return True, ""
