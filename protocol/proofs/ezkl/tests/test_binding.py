import pytest

from binding import (
    BN254_SCALAR_MODULUS,
    BindingError,
    BoundInstances,
    BranchClaim,
    bind_bundle,
    bind_instances,
    check_expected,
    check_settings,
    claim_from_bundle,
    dequantize,
    felt_hex_to_int,
    felt_to_signed,
    quantize,
    signed_to_felt_hex,
)
from proof_fixtures import (
    INSTANCES,
    LINE_FELT,
    MINUS_ONE_FELT,
    SCORE_FELT,
    WORD_FELT,
    ZERO_FELT,
    make_bundle,
    make_claim,
    make_proof,
    make_settings,
)


class TestFeltEncoding:
    def test_decodes_little_endian_value(self):
        assert felt_hex_to_int(LINE_FELT) == 3 * 256
        assert felt_hex_to_int(SCORE_FELT) == 58 * 256
        assert felt_hex_to_int(ZERO_FELT) == 0

    def test_rejects_wrong_length(self):
        with pytest.raises(BindingError, match="64 hex characters"):
            felt_hex_to_int(LINE_FELT[:-2])

    def test_rejects_prefix_and_uppercase(self):
        with pytest.raises(BindingError, match="lowercase hex"):
            felt_hex_to_int("0x" + LINE_FELT[2:])
        with pytest.raises(BindingError, match="lowercase hex"):
            felt_hex_to_int(SCORE_FELT.upper())

    def test_rejects_non_string(self):
        with pytest.raises(BindingError, match="must be a string"):
            felt_hex_to_int(768)

    def test_rejects_unreduced_value(self):
        unreduced = BN254_SCALAR_MODULUS.to_bytes(32, "little").hex()
        with pytest.raises(BindingError, match="not reduced"):
            felt_hex_to_int(unreduced)

    def test_signed_mapping(self):
        assert felt_to_signed(768) == 768
        assert felt_to_signed(felt_hex_to_int(MINUS_ONE_FELT)) == -1
        assert felt_to_signed(BN254_SCALAR_MODULUS - 5) == -5

    def test_signed_roundtrip(self):
        for value in (0, 1, -1, 768, -14848, 2**63, -(2**63)):
            assert felt_to_signed(felt_hex_to_int(signed_to_felt_hex(value))) == value
        assert signed_to_felt_hex(-1) == MINUS_ONE_FELT
        assert signed_to_felt_hex(58 * 256) == SCORE_FELT

    def test_signed_to_felt_hex_rejects_out_of_range(self):
        with pytest.raises(BindingError):
            signed_to_felt_hex(BN254_SCALAR_MODULUS)
        with pytest.raises(BindingError):
            felt_to_signed(-1)


class TestQuantize:
    def test_integer_inputs(self):
        assert quantize(3, 8) == 768
        assert quantize(17.0, 8) == 4352
        assert quantize(0, 8) == 0

    def test_rounds_half_away_from_zero(self):
        assert quantize(0.5, 0) == 1
        assert quantize(-0.5, 0) == -1
        assert quantize(2.5, 0) == 3
        assert quantize(-2.5, 0) == -3
        assert quantize(0.4999, 0) == 0

    def test_rejects_bad_inputs(self):
        with pytest.raises(BindingError):
            quantize("3", 8)
        with pytest.raises(BindingError):
            quantize(True, 8)
        with pytest.raises(BindingError):
            quantize(3.0, -1)
        with pytest.raises(BindingError):
            quantize(float("inf"), 8)

    def test_dequantize(self):
        assert dequantize(58 * 256, 8) == 58.0
        assert dequantize(-256, 8) == -1.0


class TestCheckSettings:
    def test_accepts_public_io_fixed_params(self):
        assert check_settings(make_settings()) == (8, 8)

    @pytest.mark.parametrize(
        "key,value",
        [
            ("input_visibility", "Private"),
            ("output_visibility", "Private"),
            ("param_visibility", "Private"),
            ("input_visibility", "public"),
            ("input_visibility", None),
        ],
    )
    def test_rejects_wrong_visibility(self, key, value):
        settings = make_settings()
        settings["run_args"][key] = value
        with pytest.raises(BindingError, match=key):
            check_settings(settings)

    def test_rejects_missing_run_args(self):
        with pytest.raises(BindingError, match="run_args"):
            check_settings({"model_instance_shapes": [[1, 2], [1, 1]]})

    @pytest.mark.parametrize(
        "shapes",
        [[[1, 2]], [[1, 3], [1, 1]], [[1, 2], [1, 2]], [[1, 2], [1, 1], [1, 1]], "bad", [[1, -2], [1, 1]]],
    )
    def test_rejects_unexpected_shapes(self, shapes):
        settings = make_settings()
        settings["model_instance_shapes"] = shapes
        with pytest.raises(BindingError):
            check_settings(settings)

    @pytest.mark.parametrize("key", ["model_input_scales", "model_output_scales"])
    @pytest.mark.parametrize("value", [None, [], [8, 8], ["8"], [True]])
    def test_rejects_bad_scales(self, key, value):
        settings = make_settings()
        settings[key] = value
        with pytest.raises(BindingError, match=key):
            check_settings(settings)


class TestBindInstances:
    def test_match_passes(self):
        bound = bind_instances(INSTANCES, make_settings(), make_claim())
        assert bound == BoundInstances(
            line_count=768,
            word_count=4352,
            score=14848,
            score_hex=SCORE_FELT,
            input_scale=8,
            output_scale=8,
        )
        assert bound.score_value == 58.0

    def test_line_count_mismatch_fails(self):
        with pytest.raises(BindingError, match="line_count"):
            bind_instances(INSTANCES, make_settings(), make_claim(line_count=4.0))

    def test_word_count_mismatch_fails(self):
        with pytest.raises(BindingError, match="word_count"):
            bind_instances(INSTANCES, make_settings(), make_claim(word_count=16.0))

    def test_score_hex_mismatch_fails(self):
        wrong_score = signed_to_felt_hex(59 * 256)
        with pytest.raises(BindingError, match="score_hex"):
            bind_instances(INSTANCES, make_settings(), make_claim(score_hex=wrong_score))

    def test_score_hex_must_match_exact_string(self):
        with pytest.raises(BindingError, match="score_hex"):
            bind_instances(INSTANCES, make_settings(), make_claim(score_hex=SCORE_FELT.upper()))
        with pytest.raises(BindingError, match="score_hex"):
            bind_instances(INSTANCES, make_settings(), make_claim(score_hex=None))

    def test_input_scale_drives_expected_values(self):
        settings = make_settings()
        settings["model_input_scales"] = [0]
        instances = [[signed_to_felt_hex(3), signed_to_felt_hex(17), SCORE_FELT]]
        bound = bind_instances(instances, settings, make_claim())
        assert (bound.line_count, bound.word_count) == (3, 17)
        with pytest.raises(BindingError, match="line_count"):
            bind_instances(INSTANCES, settings, make_claim())

    def test_negative_output_binds(self):
        instances = [[LINE_FELT, WORD_FELT, MINUS_ONE_FELT]]
        bound = bind_instances(instances, make_settings(), make_claim(score_hex=MINUS_ONE_FELT))
        assert bound.score == -1
        assert bound.score_value == -1 / 256

    @pytest.mark.parametrize(
        "instances",
        [None, [], [[]], [[LINE_FELT, WORD_FELT]], [[LINE_FELT, WORD_FELT, SCORE_FELT, ZERO_FELT]], [INSTANCES[0], INSTANCES[0]], "abc", [LINE_FELT]],
    )
    def test_missing_or_short_instances_fail(self, instances):
        with pytest.raises(BindingError, match="instance"):
            bind_instances(instances, make_settings(), make_claim())

    def test_malformed_element_fails(self):
        instances = [[LINE_FELT, WORD_FELT, "0x3a00"]]
        with pytest.raises(BindingError, match="64 hex characters"):
            bind_instances(instances, make_settings(), make_claim(score_hex="0x3a00"))

    def test_private_inputs_fail_closed(self):
        settings = make_settings()
        settings["run_args"]["input_visibility"] = "Private"
        with pytest.raises(BindingError, match="input_visibility"):
            bind_instances([[SCORE_FELT]], settings, make_claim())


class TestBindBundle:
    def test_match_passes(self):
        bound = bind_bundle(make_bundle(), make_proof(), make_settings())
        assert bound.score == 14848

    def test_claim_from_bundle_reads_features(self):
        assert claim_from_bundle(make_bundle()) == make_claim()

    def test_missing_instances_fails(self):
        proof = make_proof()
        del proof["instances"]
        with pytest.raises(BindingError, match="proof.instances missing"):
            bind_bundle(make_bundle(), proof, make_settings())

    def test_bundle_instances_must_equal_proof_instances(self):
        tampered = [[LINE_FELT, WORD_FELT, signed_to_felt_hex(59 * 256)]]
        with pytest.raises(BindingError, match="public_instances"):
            bind_bundle(make_bundle(instances=tampered), make_proof(), make_settings())

    def test_bundle_feature_mismatch_fails(self):
        bundle = make_bundle()
        bundle["features"]["line_count"] = 4
        with pytest.raises(BindingError, match="line_count"):
            bind_bundle(bundle, make_proof(), make_settings())

    def test_wrong_bundle_version_fails(self):
        with pytest.raises(BindingError, match="bundle_version"):
            bind_bundle(make_bundle(bundle_version="kswarm-ezkl-proof-v0"), make_proof(), make_settings())

    @pytest.mark.parametrize("field", ["features", "score_hex", "public_instances"])
    def test_missing_bundle_field_fails(self, field):
        bundle = make_bundle()
        del bundle[field]
        with pytest.raises(BindingError):
            bind_bundle(bundle, make_proof(), make_settings())

    @pytest.mark.parametrize("bad", ["3", None, True])
    def test_non_numeric_feature_fails(self, bad):
        bundle = make_bundle()
        bundle["features"]["word_count"] = bad
        with pytest.raises(BindingError, match="word_count"):
            bind_bundle(bundle, make_proof(), make_settings())

    def test_non_object_inputs_fail(self):
        with pytest.raises(BindingError):
            bind_bundle([], make_proof(), make_settings())
        with pytest.raises(BindingError):
            bind_bundle(make_bundle(), [], make_settings())


class TestCheckExpected:
    def test_match_passes(self):
        bound = bind_instances(INSTANCES, make_settings(), make_claim())
        check_expected(bound, make_claim())

    @pytest.mark.parametrize(
        "claim,field",
        [
            (make_claim(line_count=2.0), "line_count"),
            (make_claim(word_count=18.0), "word_count"),
            (make_claim(score_hex=ZERO_FELT), "score_hex"),
        ],
    )
    def test_one_field_mismatch_fails(self, claim, field):
        bound = bind_instances(INSTANCES, make_settings(), make_claim())
        with pytest.raises(BindingError, match=field):
            check_expected(bound, claim)
