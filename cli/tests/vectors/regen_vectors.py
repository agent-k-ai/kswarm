"""Regenerate cli/tests/vectors/bonsol_harness_vectors.json from the built harness.

Each vector records what `bonsol-callback-harness prepare` does with the input:
the prepared fields when the harness accepts it, or the verbatim error when it
does not. The rejection cases are part of the contract -- `score_hex` became a
required field element in `fix/proof-binding`, and the CLI must refuse exactly
what the harness refuses.
"""
import json, subprocess, sys, pathlib, hashlib

BIN, OUT, IMAGE_ID, HARNESS_COMMIT = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
VALID = "3901000000000000000000000000000000000000000000000000000000000000"
ZERO = "00" * 32
NOT_REDUCED = "ff" * 32

def branch(**over):
    d = {"branch_key": "baseline", "child_job_id": "child-baseline-1",
         "parent_request_id": "parent-bonsol-eval", "line_count": 3, "word_count": 17,
         "score_hex": VALID}
    d.update(over)
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)

CLI_AGGREGATE_INPUT = json.dumps({
    "bonsol": {"framing": "u64le-length-prefix", "image_id": IMAGE_ID, "public_input": "input-artifact"},
    "branch_jobs": [{"branch_index": 0, "input_cid": "bafkreibranch0",
                     "job": "7EYURCnmBEGFo6EeD39KsZkQB5LLcYDFsFnAK7GoD7bo", "nonce": 1000}],
    "combiner": "trimmed-mean", "combiner_parameters": {"trim_bps": 1000},
    "output_schema_hash": "0" * 64, "parent_manifest_cid": "bafkreiparentmanifestcidexample",
    "schema_version": 2,
}, separators=(",", ":"), sort_keys=True)

CASES = [
    ("harness-default", branch()),
    ("score-hex-zero-felt", branch(score_hex=ZERO, line_count=0, word_count=0)),
    ("u32-truncation", branch(line_count=4294967301, word_count=18446744073709551615)),
    ("unicode-strings", branch(branch_key="ключ", child_job_id="子任务", parent_request_id="pé")),
    ("descriptive-fields-defaulted", json.dumps({"score_hex": VALID}, separators=(",", ":"))),
    ("mistyped-descriptive-fields", json.dumps({"branch_key": None, "line_count": -1, "word_count": 2.5, "score_hex": VALID}, separators=(",", ":"))),
    ("cli-aggregate-input", CLI_AGGREGATE_INPUT),
    ("score-hex-0x-prefix", branch(score_hex="0x" + VALID[2:])),
    ("score-hex-too-short", branch(score_hex="f")),
    ("score-hex-not-hex", branch(score_hex="z" * 64)),
    ("score-hex-uppercase", branch(score_hex=("deadbeef" + "00" * 28).upper())),
    ("score-hex-not-reduced", branch(score_hex=NOT_REDUCED)),
    ("score-hex-missing", json.dumps({"branch_key": "only"}, separators=(",", ":"))),
    ("score-hex-not-a-string", json.dumps({"score_hex": 12}, separators=(",", ":"))),
]

manifest = pathlib.Path("/tmp/regen-manifest.json")
manifest.write_text(json.dumps({"imageId": IMAGE_ID}))

vectors = []
for name, payload in CASES:
    proc = subprocess.run([BIN, "prepare", "--manifest", str(manifest), "--execution-id", f"vec-{name}", "--input-json", payload],
                          text=True, capture_output=True)
    entry = {"name": name, "input_json": payload}
    if proc.returncode == 0:
        prepared = json.loads(proc.stdout)
        idg = bytes.fromhex(prepared["callbackInputDigest"])
        co = bytes.fromhex(prepared["committedOutputs"])
        entry["accepted"] = True
        entry["prepared"] = prepared
        entry["journalHash"] = hashlib.sha256(idg + co).hexdigest()
    else:
        entry["accepted"] = False
        entry["error"] = (proc.stderr.strip().splitlines() or [""])[-1]
    vectors.append(entry)

OUT.write_text(json.dumps({
    "source": "protocol/bonsol-callback-harness prepare",
    "harness_commit": HARNESS_COMMIT,
    "image_id": IMAGE_ID,
    "vectors": vectors,
}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"{len(vectors)} vectors, accepted={sum(1 for v in vectors if v['accepted'])}, rejected={sum(1 for v in vectors if not v['accepted'])}")
for v in vectors:
    print(" ", v["name"], "OK" if v["accepted"] else "REJECT " + v.get("error", ""))
