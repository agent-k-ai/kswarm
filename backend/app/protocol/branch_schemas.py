from __future__ import annotations

import hashlib
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


OutputKind = Literal["scalar", "categorical", "narrative_with_scalar"]


class BranchInput(BaseModel):
    """Canonical input to a branch worker. Deterministic-JSON serializable."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    parent_job: str
    branch_index: int = Field(ge=0)
    seed: str
    parameters: dict[str, Any]
    persona_set_cid: Optional[str] = None
    rng_seed: int = Field(ge=0)
    target_output_kind: OutputKind
    scalar_grid_bps: Optional[int] = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_grid(self) -> "BranchInput":
        if self.target_output_kind in {"scalar", "narrative_with_scalar"} and self.scalar_grid_bps is None:
            self.scalar_grid_bps = 1
        return self


class BranchOutput(BaseModel):
    """Canonical output from a branch worker. Deterministic-JSON serializable.

    Narrative text is part of the IPFS bundle for provenance and reports, but
    it is not treated as verified content by the canonical result hash.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    parent_job: str
    branch_index: int = Field(ge=0)
    output_kind: OutputKind

    scalar_value_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    scalar_confidence_lower_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    scalar_confidence_upper_bps: Optional[int] = Field(default=None, ge=0, le=10000)

    categorical_label_index: Optional[int] = Field(default=None, ge=0, le=255)

    narrative_text: Optional[str] = None
    narrative_scores: Optional[dict[str, int]] = None

    rng_seed: int = Field(ge=0)
    llm_model: str
    llm_version_hash: str
    completed_at_unix: int = Field(ge=0)
    transcript_cid: str
    # IPFS locator of the zkVM branch canonicalization receipt, when the worker proved
    # one. It names a proof taken over this document, so it cannot be inside the proof
    # and it is excluded from the canonical hash preimage below.
    zkvm_receipt_cid: Optional[str] = None

    @model_validator(mode="after")
    def validate_output_shape(self) -> "BranchOutput":
        if self.output_kind == "scalar" and self.scalar_value_bps is None:
            raise ValueError("scalar output requires scalar_value_bps")
        if self.output_kind == "categorical" and self.categorical_label_index is None:
            raise ValueError("categorical output requires categorical_label_index")
        if self.output_kind == "narrative_with_scalar":
            if not self.narrative_text:
                raise ValueError("narrative_with_scalar output requires narrative_text")
            if not self.narrative_scores:
                raise ValueError("narrative_with_scalar output requires narrative_scores")
        if self.scalar_confidence_lower_bps is not None and self.scalar_confidence_upper_bps is not None:
            if self.scalar_confidence_lower_bps > self.scalar_confidence_upper_bps:
                raise ValueError("scalar lower confidence bound exceeds upper bound")
        if self.narrative_scores is not None:
            normalized: dict[str, int] = {}
            for key, value in self.narrative_scores.items():
                if not key or not key.replace("_", "").isalnum():
                    raise ValueError(f"invalid narrative score key: {key!r}")
                if value < 0 or value > 10000:
                    raise ValueError(f"narrative score {key!r} outside bps range")
                normalized[key] = int(value)
            self.narrative_scores = normalized
        return self

    def canonical_hash_preimage(self) -> dict[str, Any]:
        """Return the stable fields that verifier and worker must match.

        `narrative_text` is excluded by ADR Decision 6. `completed_at_unix` is
        also excluded because an honest verifier re-executes later.
        `zkvm_receipt_cid` is excluded because the receipt it names is a proof over
        this document: including it would make the document depend on its own proof,
        and the guest is shown the document without it.
        """

        data = self.model_dump(mode="json", exclude_none=False)
        data.pop("narrative_text", None)
        data.pop("completed_at_unix", None)
        data.pop("zkvm_receipt_cid", None)
        return data


class CanonicalHash:
    @staticmethod
    def of(output: BranchOutput) -> bytes:
        """Deterministic SHA-256 over canonical BranchOutput verification fields."""

        from .canonical_hash import canonical_json_bytes

        return hashlib.sha256(canonical_json_bytes(output.canonical_hash_preimage())).digest()

