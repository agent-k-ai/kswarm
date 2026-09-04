#!/usr/bin/env python3
"""Prove one branch score with the pinned EZKL assets and write a claim bundle.

The bundle is the prover's claim. `verify_branch.py` binds `proof.json` to it.
The prover runs the same binding check on its own output so a misconfigured
asset set (for example private inputs) fails here, not at the verifier.
"""

import argparse
import hashlib
import json
from pathlib import Path

import ezkl

from binding import BranchClaim, bind_instances


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--line-count", required=True, type=float)
    parser.add_argument("--word-count", required=True, type=float)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    assets_dir = Path(args.assets_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    compiled_path = assets_dir / "network.compiled"
    settings_path = assets_dir / "settings.json"
    srs_path = assets_dir / "kzg.srs"
    vk_path = assets_dir / "vk.key"
    pk_path = assets_dir / "pk.key"
    metadata_path = assets_dir / "metadata.json"

    input_path = output_dir / "input.json"
    witness_path = output_dir / "witness.json"
    proof_path = output_dir / "proof.json"
    bundle_path = output_dir / "bundle.json"

    input_payload = {"input_data": [[float(args.line_count), float(args.word_count)]]}
    input_path.write_text(json.dumps(input_payload))

    ezkl.gen_witness(str(input_path), str(compiled_path), str(witness_path), None, str(srs_path))
    proof = ezkl.prove(str(witness_path), str(compiled_path), str(pk_path), str(proof_path), str(srs_path))
    verified = ezkl.verify(str(proof_path), str(settings_path), str(vk_path), str(srs_path), False)
    if not verified:
        raise RuntimeError("ezkl proof verification failed")

    settings = json.loads(settings_path.read_text())
    instances = proof["instances"]
    score_hex = instances[0][-1]
    bound = bind_instances(
        instances,
        settings,
        BranchClaim(line_count=args.line_count, word_count=args.word_count, score_hex=score_hex),
    )

    bundle = {
        "bundle_version": "kswarm-ezkl-proof-v1",
        "features": {
            "line_count": args.line_count,
            "word_count": args.word_count,
        },
        "metadata": json.loads(metadata_path.read_text()),
        "proof_path": str(proof_path),
        "proof_sha256": sha256_hex(proof_path),
        "public_instances": instances,
        "score": bound.score_value,
        "score_hex": score_hex,
        "verified": True,
        "vk_sha256": sha256_hex(vk_path),
    }
    bundle_path.write_text(json.dumps(bundle, indent=2))


if __name__ == "__main__":
    main()
