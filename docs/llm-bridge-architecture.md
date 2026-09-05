# LLM Bridge Architecture

Phase 0e connects customer prediction jobs to the existing Solana job protocol, local IPFS artifacts, the OASIS service layer, and a chat-completions-compatible local LLM endpoint.

## Runtime Shape

`kswarm predict open` creates one aggregate job and `N` branch-proof jobs. The aggregate job pubkey is the parent run id. The CLI writes a local run manifest under `~/.config/kswarm/predict_runs/<parent-run>.json` and uploads the parent manifest plus every branch input to IPFS.

Branch workers run as `kswarm-branch-worker`. A worker:

- loads `~/.config/kswarm/wallets/<name>.json`
- polls real Solana `Job` accounts
- filters `Open` branch-proof jobs by capability, software digest, role, tier, and claim deadline
- checks the LLM endpoint and IPFS, and its own claim budget, before every `claim_job` (see Claim Discipline)
- fetches `BranchInput` from IPFS
- calls the configured local LLM through `backend/app/utils/llm_client.py`
- parses the model output strictly, and retries the identical request a bounded number of times when the output does not satisfy the contract
- optionally calls `SimulationRunner.start_simulation` when the input requests an existing OASIS simulation id
- uploads the transcript and `BranchOutput` bundle to IPFS
- submits `submit_receipt` with compact `MFB2` result bytes

Verifier workers run as `kswarm-verifier-worker`. A verifier polls `Completed` branch jobs (`submitted` is retained only as a CLI filter alias for this status) that are inside their challenge window and not yet attested. It fetches the input and the worker's output from IPFS, re-executes the branch with the identical model, seed and configuration, encodes its own canonical `MFB2` bytes, submits `submit_verifier_attestation` with the hash of those bytes, and challenges when the hash differs from the receipt (see Verifier Re-execution).

The aggregate runner reads local predict-run manifests, pulls settled branch outputs, combines them with the combiner the manifest declares, binds the receipt to a Bonsol execution through `KSWARM_BONSOL_AGGREGATE_COMMAND`, and submits an aggregate receipt (see Aggregate Combiners And Bonsol Binding).

## Claim Discipline

The program has no "release claim" instruction. After `claim_job` lands, the worker's `required_stake` is locked, and the only exits are `submit_receipt` before `execute_deadline` or `slash_stale_job` after it. A claim the worker cannot execute is a slashed claim. The daemon (`worker/branch_worker/daemon.py`) protects every claim in five ways:

1. **Pre-claim health check.** Before every claim it lists models on the LLM endpoint (`GET /v1/models`) and calls the IPFS `version` API. If either fails, it does not claim.
2. **Claim budget.** It claims at most `max_concurrent_claims` jobs, measured against the on-chain `Worker.active_claims`. That counter includes every claim not yet settled, challenged, or slashed, so completed jobs waiting out their challenge window still count. `max_concurrent_claims` therefore bounds locked stake, not only in-flight execution. The default is `1`. The program adds its own cap per stake tier (`max_concurrent_claims_for_role_tier`).
3. **Retry until the deadline margin.** After a claim, transient failures (LLM connection errors, timeouts, HTTP 408/409/425/429/5xx, IPFS errors) are retried with exponential backoff (`execute_retry_initial_seconds`, doubling, capped at `execute_retry_max_seconds`) until `execute_deadline - execute_deadline_margin_seconds`. Non-retryable failures (other HTTP 4xx, rejected model output, an invalid `BranchInput`, a failed `submit_receipt`) abandon the claim at once.
4. **Circuit breaker.** After an abandoned claim, the daemon makes no new claims for `claim_cooldown_seconds`.
5. **Stake-at-risk log.** Every abandoned claim logs `branch job failed job=... claim abandoned (no on-chain release exists): stake at risk=<required_stake> base units ...`.

| Setting | `worker.toml` key | Environment | Default |
|---|---|---|---|
| Claim budget | `max_concurrent_claims` | `KSWARM_WORKER_MAX_CLAIMS` | `1` |
| Cooldown after a failure | `claim_cooldown_seconds` | `KSWARM_CLAIM_COOLDOWN_SECONDS` | `300` |
| Deadline safety margin | `execute_deadline_margin_seconds` | `KSWARM_EXECUTE_DEADLINE_MARGIN_SECONDS` | `120` |
| First retry wait | `execute_retry_initial_seconds` | `KSWARM_EXECUTE_RETRY_INITIAL_SECONDS` | `5` |
| Retry wait cap | `execute_retry_max_seconds` | `KSWARM_EXECUTE_RETRY_MAX_SECONDS` | `60` |

## Strict Model Output Parsing

Every value that reaches the canonical hash comes from the model response. `worker/branch_worker/parsing.py` never fills in a default:

- A **probability** field (`scalar_value`, `probability`, `confidence_lower`, `confidence_upper`, and the guardrails `severity`, `quality`, `ood`) must be a JSON number or numeric string in `[0, 1]`. It is converted to basis points by multiplying by 10000 and rounding half up. Integers `0` and `1` are the end points of the interval. The value is never read as basis points: `5000` in a probability field is an error.
- A **bps** field (`severity_bps`, `quality_bps`, `ood_bps`) must be a JSON integer in `[0, 10000]`. Floats and strings are errors: `0.5` in a bps field is an error, and `1` in a bps field is one basis point.
- `categorical_label_index` must be a JSON integer in `[0, 255]` and below the number of committed labels.
- `narrative_with_scalar` requires `narrative_text`, `scalar_value`, and all three guardrails, each in exactly one form.
- Booleans are never numbers.

When the output does not satisfy the contract, the executor sends the identical request again (same messages, same seed) up to `LLM_INVALID_OUTPUT_RETRIES` more times (default `2`). If every attempt fails, it raises `ModelOutputRejectedError`, nothing is uploaded, and nothing is submitted. Rejected attempts are recorded in the transcript.

## Verifier Re-execution

Re-execution is the default (`VERIFIER_REEXECUTE=1`). The verifier:

1. downloads the branch input and the worker's `BranchOutput`
2. refuses the job (no attestation, a warning, and the `reexecution_model_mismatches` metric) when the worker's `llm_model` is not the model this verifier runs, because it cannot reproduce that configuration
3. runs `BranchExecutor.execute(..., verifier_reference=worker_output)`, which uses the identical model, seed (`rng_seed`), sampling parameters, and system prompt, and binds the output to the worker's `transcript_cid` so the canonical preimage is comparable
4. encodes its own `MFB2` bytes and attests to `sha256(bytes)`
5. uploads evidence with `mode`, both outputs, both hashes, the verifier's transcript, and the validation errors

**Mismatch path.** `receipt_is_challengeable` in `solana/programs/kswarm_protocol/src/lib.rs` returns true when `verifier_attestation_hash` is set and differs from `submitted_result_hash`. So an attestation carrying the verifier's own hash makes a lying worker's receipt challengeable, and the daemon then sends `challenge_job` (`KSWARM_CHALLENGE_ON_MISMATCH`, default on). The job ends `Slashed`. `challenge_job` accepts only the verifier the customer or the protocol admin assigned to the job with `assign_verifier`, for every job class (the H2-Interim rule; see [architecture-overview.md](architecture-overview.md#challenge-authorization)). A verifier that is not the assigned one still attests -- the attestation is what makes the receipt challengeable -- then logs `challenge refused job=... code=ChallengeRequiresAssignedVerifier` and counts `challenges_not_assigned`. The attestation stands, and the customer or the admin can assign that verifier so a later challenge is accepted. `worker/tests/test_verifier_daemon.py` exercises this with a fake worker that submits `scalar_value=0.5` without calling the model, and `worker/tests/test_branch_worker_e2e.py` does the same against a real validator, IPFS, and LLM.

**Hash-only mode** re-hashes the worker's own artifact. It exists only for diagnostics, requires the explicit `VERIFIER_HASH_ONLY=1`, logs a warning at start and on every attestation, and cannot catch a worker that fabricated its output. `VERIFIER_REEXECUTE=0` without `VERIFIER_HASH_ONLY=1` is a configuration error.

A verifier that cannot re-execute (LLM outage, IPFS outage, rejected model output) submits no attestation and retries the job on a later pass.

## Aggregate Reduction And The Bonsol Proof

The aggregate job is opened by `kswarm predict bind-aggregate <parent-run>` once its
branches have settled, not by `predict open`. `open_job` fixes `input_bundle_hash` and
`expected_result_hash` for good, and both are functions of the branch receipts. The
command reads each branch job's on-chain `result_bytes`, builds the `MFA3` artifact,
reduces it, pins it, and opens the job against the framed artifact digest and the
predicted journal hash.

`cli/kswarm_cli/aggregate.py` is the one Python definition of that reduction, and it is
the mirror of the guest in `protocol/bonsol-aggregate-reducer`. Both are pinned to
`cli/tests/vectors/aggregate_journal_vectors.json`, so neither can change without the
other failing. The combiner registry:

| Combiner | Registry id | Inputs | Parameters |
|---|---|---|---|
| `weighted-mean` | `1` | `scalar_value_bps` decoded from every branch receipt | per-branch `weight` in the artifact (uniform `1` as `bind-aggregate` writes it) |
| `trimmed-mean` | `2` | `scalar_value_bps` decoded from every branch receipt | `trim_bps`; `outlier_count = floor(branch_count * trim_bps / 10000)`. Weights must be `1`, because this combiner averages unweighted |
| `majority-vote` | `3` | `categorical_label_index` decoded from every branch receipt, checked against `category_dictionary_size` | `category_dictionary_size`; ties go to the lowest label |

The committed value is exact integer arithmetic, rounded half up as
`(2*numerator + denominator) / (2*denominator)` in 128-bit integers, on both sides. No
rounding mode and no floating-point unit has to agree for the journal to match.

**The runner does not decide the aggregate.** `worker/aggregator_runner` fetches the
artifact the job committed, checks its framed digest against `input_bundle_hash`,
re-reduces it, checks the journal against `expected_result_hash`, and checks every branch
receipt in it against that branch job on chain. Then it claims, submits the guest's
committed outputs as `result_bytes`, and only then runs
`KSWARM_BONSOL_AGGREGATE_COMMAND` to get the proof. The order is forced:
`record_aggregate_verification` requires a `Completed` job whose `submitted_result_hash`
is the callback's `output_digest`.

The hook receives one JSON argument (`run`, `aggregate_job`, `image_id`, `input_cid`,
`input_artifact_hex`, `input_digest`, `committed_outputs`, `output_digest`,
`journal_hash`, `result`) and must print
`{execution_id, image_id, input_digest, output_digest, journal_hash, committed_outputs}`.
The runner checks `sha256(committed_outputs) == output_digest` and
`sha256(input_digest || committed_outputs) == journal_hash`, then checks every field
against its own reduction and against the job account, so a hook that proved a different
claim is an error. `protocol/scripts/bonsol-aggregate-hook.py` is the reference
implementation.

Without the hook the runner refuses to claim the job at all: a receipt that can never be
proven leaves the customer to cancel and the worker's stake locked until the execute
deadline. `KSWARM_ALLOW_UNBOUND_AGGREGATE=1` overrides that for a local stack with no
Bonsol node, warns on every run, and is refused on the `devnet` and `mainnet` profiles.

A containerized aggregator cannot run the reference hook itself: it has no Bonsol CLI,
no funded client keypair and no docker socket, and it should not. It uses
`python -m aggregator_runner.bonsol_http_hook`, which forwards the payload to a proving
service (`protocol/scripts/bonsol-hook-server.py`) at `KSWARM_BONSOL_HOOK_URL` and
returns the answer unchanged. The checks above are unaffected, and that is the point: a
proving service that proved a different claim is refused by its caller.

The aggregate output artifact (IPFS) records the combiner, the result, `branch_count`,
the Merkle root, `output_schema_hash`, `aggregate_input_sha256`, the full journal, and
`receipt_binding: bonsol-committed-outputs` (or `mfa3-committed-outputs-unproven`).

## Branch Canonicalization Receipts

Re-execution catches a worker that invented a forecast. It cannot catch a worker whose
published document and submitted receipt describe different values, because the program
stores only `sha256(result_bytes)` and never sees the document, and both sides of a
re-execution comparison are the verifier's own bytes.

With `KSWARM_ZKVM_HOST` set, the branch worker proves that gap closed. The
`protocol/zkvm-reducer` guest is handed the output document and recomputes the receipt:
canonical JSON of the hash preimage, the canonical hash, the `MFB2` encoding, and
`sha256` of that encoding. It commits `input_digest || result_hash || output_len`. The
worker pins the receipt bundle to IPFS and names it in `BranchOutput.zkvm_receipt_cid`,
which is excluded from the canonical hash preimage because a receipt cannot be inside the
document it proves.

The verifier verifies that receipt against its own copy of the host binary, checks the
image id against `KSWARM_ZKVM_IMAGE_ID` when pinned, and binds the journal to the claim:
the input digest must be the frame rebuilt from this job's input and output, the result
hash must be the on-chain `submitted_result_hash`, and the length must be the document it
fetched. If re-execution disagrees as well, it attests to its own hash and challenges. If
re-execution agrees but the receipt does not, it refuses to attest.

**Refusing to attest does not stop the branch settling.** `settle_job` checks only that
the job is `Completed`, is not an `aggregate-proof` job, and is past its challenge
deadline, and then pays
(`solana/programs/kswarm_protocol/src/lib.rs:563-609`). It reads neither
`verifier_attestation_hash` nor any receipt, and there is no cancel path for a
`Completed` branch. So this receipt is a real check with a real consequence -- an
attestation that disagrees is challengeable and slashable, and a withheld attestation is
visible evidence -- but it is not an on-chain settlement gate, and the aggregate path does
not supply one either: the aggregate guest reads only receipt bytes, and neither it nor
`settle_aggregate_proof_job` sees a branch attestation. The branch-level guarantee is
economic and social. `docs/proof-layer-status.md`, "What the chain enforces about a
branch", states the full position and records the open item.

Proving is tens of seconds of CPU per branch, and it is on by default in the images:
`branch-worker` and `verifier-worker` set `KSWARM_ZKVM_HOST` themselves and
`docker-compose.swarm.yml` also sets `KSWARM_ZKVM_REQUIRE_RECEIPT=1`, so the verifier in
that stack refuses a branch without a receipt. Outside those images the variable is
unset and a worker proves nothing. Turning it off is `KSWARM_ZKVM_HOST=`, explicitly
empty. The window still has to allow for it: a branch job whose execution window is
shorter than the prove time would be slashed for a proof it could not finish.

The `branch-worker` and `verifier-worker` images carry the host binary and the id of the
guest compiled into it, and default `KSWARM_ZKVM_HOST` and `KSWARM_ZKVM_IMAGE_ID_FILE`
at both, so the proof path is on by default in containers and a verifier refuses a
receipt from a guest its own image did not build.

**Run state.** Run files are written to a temporary file in the same directory, fsynced, and renamed into place. Each run is processed under an exclusive non-blocking lock on `<parent-run>.lock`, so two runners on one host cannot both claim it. A claim that was made but not submitted is recorded (`aggregate_claimed_at_unix`) and resumed on the next pass while the job is still claimed by this wallet.

## Canonical Schemas

`backend/app/protocol/branch_schemas.py` defines the protocol-facing Pydantic v2 models:

- `BranchInput`: parent job, branch index, question seed, job parameters, optional persona set CID, deterministic RNG seed, target output kind, and scalar grid.
- `BranchOutput`: scalar, categorical, or `narrative_with_scalar` output with local LLM model metadata and transcript CID.

`backend/app/protocol/canonical_hash.py` defines deterministic JSON serialization, scalar snap-to-grid, `MFB2` result-byte encoding, and result hashing.

`MFB2` is additive. It does not replace existing Phase 0 deterministic result bytes. It carries:

- magic `MFB2`
- schema version
- output kind id
- branch index
- compact scalar, confidence interval, category, and guardrail-score fields when present
- canonical hash of the stable `BranchOutput` verification fields

The compact result is bounded by the program's `MAX_RESULT_BYTES`.

## Determinism

LLM outputs are not assumed to be byte-deterministic. The verified surface is constrained:

- scalar outputs snap to integer basis points in `[0, 10000]`
- categorical outputs use a committed label index
- narrative guardrails snap to integer basis points
- canonical JSON uses sorted keys and no whitespace

For branch hashing, `narrative_text` is excluded. `completed_at_unix` is also excluded because a verifier re-executes later. `transcript_cid` is included, so the worker narrative and transcript remain bound as provenance even though the verifier does not prove that text content.

`llm_version_hash` is the SHA-256 of the canonical JSON of: `model`, `provider`, `response_format`, `temperature`, `max_tokens`, `num_ctx`, and `system_prompt_sha256` (the SHA-256 of the system prompt in `worker/branch_worker/executor.py`). The endpoint URL is not part of it, so two hosts serving the same model agree. A prompt or parameter change produces a different version hash, a different canonical hash, and therefore a verifier mismatch. The transcript records the full preimage.

## Tier A And Tier B

Tier A covers scalar and categorical predictions. The scalar or category is the purchased output. Supporting text can be present in IPFS artifacts, but only the compact scalar/category commitment is verified.

Tier B covers narrative-primary jobs. The narrative is the product, but Phase 0e verifies only scalar guardrails such as severity, quality, and OOD scores. The narrative text is hash-committed provenance, not cryptographically verified narrative correctness.

This distinction is enforced in code:

- `BranchOutput.output_kind` must be one of `scalar`, `categorical`, or `narrative_with_scalar`
- `narrative_with_scalar` requires `narrative_scores`
- canonical hashes exclude `narrative_text`
- verifier evidence includes the Tier B disclosure string

## Local LLM Contract

The worker and the verifier require:

```bash
export LLM_BASE_URL=http://127.0.0.1:<port>/v1
export LLM_MODEL_NAME=<local-model-id>
export LLM_API_KEY=local-llm              # optional for local endpoints
export LLM_MAX_TOKENS=12000               # optional; per-branch completion cap (default 12000)
export LLM_INVALID_OUTPUT_RETRIES=2       # optional; identical-request retries after rejected output (default 2)
export KSWARM_IPFS_API_URL=http://127.0.0.1:5001   # optional; Kubo API (default 5001; `make dev-up` maps host port 4501)
```

The bridge does not assume a cloud endpoint. It uses the repository's chat-completions-compatible client wrapper and passes `temperature=0`. `BranchInput.rng_seed` remains the canonical branch seed; workers normalize it into the signed request-seed range accepted by local endpoints and record both values in the transcript.

`LLM_MODEL_NAME`, `LLM_MAX_TOKENS`, and the system prompt are part of `llm_version_hash`. The verifier must run the same values as the workers it verifies.

Branch transcripts record LLM latency, raw JSON content, rejected attempts, the version-hash preimage, and token usage when the endpoint returns usage metadata. Verifier evidence records the verification mode and surface, both outputs, and the Tier B disclosure string for `narrative_with_scalar` outputs.

## Selectable Model Arms

The branch executor is endpoint-agnostic (any OpenAI-compatible `/v1`), so the
model is selected purely by the env vars above. Two validated arms:

| Arm | `LLM_BASE_URL` | `LLM_MODEL_NAME` | Notes |
|---|---|---|---|
| Cheap comparison | `http://<llm-host>:11434/v1` | `llama3.2:3b-instruct-q5_K_M` | Ollama; fast, weak. Cheap A/B baseline. |
| Strong (default) | `http://<llm-host>:8000/v1` | `qwen36-fp8` | vLLM `Qwen/Qwen3.6-35B-A3B-FP8` (MoE 35B/~3B active, FP8, 32k ctx, tensor-parallel x2) on two GPUs. |

`LLM_MAX_TOKENS` defaults to 12000 on both arms. Set it identically on every worker and verifier of a deployment, because it is part of `llm_version_hash`.

Set `<llm-host>` to the address the workers can reach; a short hostname that
resolves on the GPU host may not resolve on the worker host, so prefer the IP
or an FQDN. Confirm before running: `curl -s "${LLM_BASE_URL}/models"`.

### Reasoning-model notes (qwen36-fp8)

- **Token budget.** Qwen3.6 spends most of its completion budget on hidden
  reasoning before emitting the final JSON (~1.3-1.4k tokens observed). The
  legacy hardcoded 1200 budget truncated it to an empty `content` field; the
  default is now 12000. The client raises a clear error (rather than crashing on
  `None`) if both `content` and the reasoning field come back empty, and the
  executor treats that as rejected output and retries the identical request.
- **`num_ctx` is Ollama-only.** The `--num-ctx` CLI option maps to
  `extra_body={"options": {"num_ctx": ...}}`, which Ollama accepts but vLLM
  rejects. Do not pass `--num-ctx` on the vLLM arm (its 32k window is fixed at
  serve time).
- **Reasoning-parser routing.** This vLLM server exposes the thinking trace in a
  separate `reasoning` field; the final JSON answer is in `content`. The client
  falls back to the `reasoning` field only when `content` is empty.
