"""Shared fixtures: values captured from a real ezkl 23.0.5 run.

prepare_assets.py + prove_branch.py with line_count=3, word_count=17 give
score = 2*3 + 3*17 + 1 = 58 at scale 8. Every element is 64 lowercase hex
characters of the little-endian field element.
"""

import copy

from binding import BranchClaim

LINE_FELT = "0003000000000000000000000000000000000000000000000000000000000000"
WORD_FELT = "0011000000000000000000000000000000000000000000000000000000000000"
SCORE_FELT = "003a000000000000000000000000000000000000000000000000000000000000"
INSTANCES = [[LINE_FELT, WORD_FELT, SCORE_FELT]]
ZERO_FELT = "00" * 32
MINUS_ONE_FELT = "000000f093f5e1439170b97948e833285d588181b64550b829a031e1724e6430"


def make_settings():
    return {
        "run_args": {
            "input_scale": 8,
            "param_scale": 8,
            "logrows": 12,
            "input_visibility": "Public",
            "output_visibility": "Public",
            "param_visibility": "Fixed",
        },
        "model_instance_shapes": [[1, 2], [1, 1]],
        "model_input_scales": [8],
        "model_output_scales": [8],
        "version": "23.0.5",
    }


def make_claim(line_count=3.0, word_count=17.0, score_hex=SCORE_FELT):
    return BranchClaim(line_count=line_count, word_count=word_count, score_hex=score_hex)


def make_proof(instances=None):
    return {"protocol": None, "instances": copy.deepcopy(INSTANCES if instances is None else instances), "proof": "00"}


def make_bundle(instances=None, **overrides):
    bundle = {
        "bundle_version": "kswarm-ezkl-proof-v1",
        "features": {"line_count": 3.0, "word_count": 17.0},
        "proof_sha256": "ab" * 32,
        "public_instances": copy.deepcopy(INSTANCES if instances is None else instances),
        "score_hex": SCORE_FELT,
        "verified": True,
        "vk_sha256": "cd" * 32,
    }
    bundle.update(overrides)
    return bundle
