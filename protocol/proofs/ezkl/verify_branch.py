#!/usr/bin/env python3
"""Verify one branch EZKL proof and bind it to the claimed result.

Exit code 0 means: the proof verifies against the pinned verification key,
the proof and key hashes match the bundle, and the public instances equal
the claimed line count, word count, and score. Any other outcome exits 1.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from binding import BindingError, BranchClaim, bind_bundle, check_expected


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--expected-line-count", type=float)
    parser.add_argument("--expected-word-count", type=float)
    parser.add_argument("--expected-score-hex")
    args = parser.parse_args(argv)
    expected = (args.expected_line_count, args.expected_word_count, args.expected_score_hex)
    if any(value is not None for value in expected) and any(value is None for value in expected):
        parser.error("--expected-line-count, --expected-word-count and --expected-score-hex go together")
    return args


def verify(args: argparse.Namespace) -> dict:
    assets_dir = Path(args.assets_dir)
    bundle_path = Path(args.bundle)
    proof_path = Path(args.proof)

    settings_path = assets_dir / "settings.json"
    srs_path = assets_dir / "kzg.srs"
    vk_path = assets_dir / "vk.key"

    bundle = json.loads(bundle_path.read_text())
    if not isinstance(bundle, dict):
        raise BindingError("bundle must be a JSON object")
    if bundle.get("proof_sha256") != sha256_hex(proof_path):
        raise BindingError("ezkl proof sha256 mismatch")
    if bundle.get("vk_sha256") != sha256_hex(vk_path):
        raise BindingError("ezkl verification key sha256 mismatch")

    import ezkl

    verified = ezkl.verify(str(proof_path), str(settings_path), str(vk_path), str(srs_path), False)
    if not verified:
        raise BindingError("ezkl.verify returned false")

    proof = json.loads(proof_path.read_text())
    settings = json.loads(settings_path.read_text())
    bound = bind_bundle(bundle, proof, settings)
    if args.expected_score_hex is not None:
        check_expected(
            bound,
            BranchClaim(
                line_count=args.expected_line_count,
                word_count=args.expected_word_count,
                score_hex=args.expected_score_hex,
            ),
        )

    return {
        "bound_instances": {
            "input_scale": bound.input_scale,
            "line_count": bound.line_count,
            "output_scale": bound.output_scale,
            "score": bound.score,
            "word_count": bound.word_count,
        },
        "proof_sha256": bundle["proof_sha256"],
        "score": bound.score_value,
        "score_hex": bound.score_hex,
        "verified": True,
        "vk_sha256": bundle["vk_sha256"],
    }


def main() -> None:
    args = parse_args()
    try:
        report = verify(args)
    except BindingError as error:
        print(json.dumps({"error": str(error), "verified": False}))
        sys.exit(1)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
