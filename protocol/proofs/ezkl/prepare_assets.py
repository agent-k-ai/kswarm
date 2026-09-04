#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

import ezkl
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def build_model(model_path: Path) -> None:
    input_tensor = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 2])
    output_tensor = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1])
    weights = np.array([[2.0], [3.0]], dtype=np.float32)
    bias = np.array([1.0], dtype=np.float32)
    node = helper.make_node(
        "Gemm",
        inputs=["input", "W", "B"],
        outputs=["output"],
        alpha=1.0,
        beta=1.0,
        transB=0,
    )
    graph = helper.make_graph(
        [node],
        "kswarm_branch_score",
        [input_tensor],
        [output_tensor],
        initializer=[
            numpy_helper.from_array(weights, name="W"),
            numpy_helper.from_array(bias, name="B"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_operatorsetid("", 13)])
    onnx.save(model, model_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--logrows", type=int, default=12)
    parser.add_argument("--input-scale", type=int, default=8)
    parser.add_argument("--param-scale", type=int, default=8)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    model_path = output_dir / "model.onnx"
    settings_path = output_dir / "settings.json"
    compiled_path = output_dir / "network.compiled"
    srs_path = output_dir / "kzg.srs"
    vk_path = output_dir / "vk.key"
    pk_path = output_dir / "pk.key"
    sample_input_path = output_dir / "sample_input.json"
    sample_witness_path = output_dir / "sample_witness.json"
    metadata_path = output_dir / "metadata.json"

    build_model(model_path)

    run_args = ezkl.PyRunArgs()
    run_args.logrows = args.logrows
    run_args.input_scale = args.input_scale
    run_args.param_scale = args.param_scale
    # The verifier binds the public instances to the claimed inputs and output.
    # Inputs and outputs must be public instances. Params must be fixed in the
    # circuit so the verification key pins the model weights.
    run_args.input_visibility = "public"
    run_args.output_visibility = "public"
    run_args.param_visibility = "fixed"

    if not ezkl.gen_settings(str(model_path), str(settings_path), run_args):
        raise RuntimeError("ezkl.gen_settings failed")
    if not ezkl.compile_circuit(str(model_path), str(compiled_path), str(settings_path)):
        raise RuntimeError("ezkl.compile_circuit failed")

    settings = json.loads(settings_path.read_text())
    ezkl.gen_srs(str(srs_path), settings["run_args"]["logrows"])

    sample_input_path.write_text(json.dumps({"input_data": [[5.0, 7.0]]}))
    ezkl.gen_witness(str(sample_input_path), str(compiled_path), str(sample_witness_path), None, str(srs_path))

    if not ezkl.setup(str(compiled_path), str(vk_path), str(pk_path), str(srs_path), str(sample_witness_path), False):
        raise RuntimeError("ezkl.setup failed")

    metadata = {
        "input_scale": args.input_scale,
        "input_visibility": settings["run_args"]["input_visibility"],
        "logrows": args.logrows,
        "model_digest_description": "linear_branch_score_v1",
        "output_meaning": "2 * line_count + 3 * word_count + 1",
        "output_visibility": settings["run_args"]["output_visibility"],
        "param_scale": args.param_scale,
        "param_visibility": settings["run_args"]["param_visibility"],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
