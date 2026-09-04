# Bonsol harness vectors

`bonsol_harness_vectors.json` holds the outcome of
`protocol/bonsol-callback-harness prepare` for a set of inputs.
`tests/test_bonsol_binding.py` checks `kswarm_cli.bonsol` against every one of
them, in both directions:

* **accepted** vectors carry the harness's `prepared` output. The CLI must
  reproduce the framed input, its digest, the reducer's committed outputs and
  their digest, and `journalHash = sha256(callbackInputDigest || committedOutputs)`
  -- the rule the program and the harness `prepare-production` path use.
* **rejected** vectors carry the harness's verbatim error. The CLI must refuse
  the same inputs with the same stated reason. The rejection taxonomy is part of
  the contract: `fix/proof-binding` made `score_hex` a required BN254 field
  element (exactly 64 lowercase hex digits, reduced modulo the field), and a CLI
  that kept the older lenient rule would compute a journal hash for an input the
  reducer refuses to run.

`cli-aggregate-input` is the artifact `predict open` writes. It is a **rejected**
vector: the branch reducer's input is one branch's
`{branch_key, child_job_id, parent_request_id, line_count, word_count, score_hex}`,
and the aggregate artifact has none of those. `predict open` therefore opens the
aggregate job with `expected_result_hash` unset and warns, rather than writing a
hash the reducer would never commit.

Regenerate after a harness or reducer change:

```bash
cd protocol/bonsol-callback-harness && cargo build --locked
python3 cli/tests/vectors/regen_vectors.py \
  <path to target/debug/bonsol-callback-harness> \
  cli/tests/vectors/bonsol_harness_vectors.json \
  "$(python3 -c 'from kswarm_cli.reducer_image import AGGREGATE_REDUCER_IMAGE_ID as i; print(i)')" \
  "$(git rev-parse --short HEAD)"
```

`harness_commit` records the commit the harness binary was built from; keep it
current so a stale vector file is visible.
