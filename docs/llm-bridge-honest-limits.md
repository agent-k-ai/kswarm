# LLM Bridge Honest Limits

Phase 0e proves the protocol bridge path, not a complete launch threat model.

## The LLM Step Is Not Proven, And Nothing In 2026 Proves It

Everything else on this page describes how far the *economic* guarantee stretches. It is
worth being blunt about why an economic guarantee is what secures this step at all.

No zero-knowledge proof system, in 2026, can prove that a language model of this size
produced a particular output from a particular prompt. The largest language model anyone
can prove with released code is GPT-2 small at **124M parameters**, and the fastest
published figure for it is at a **16-token sequence**, which is not a useful input
length. kswarm's branch model is roughly **25 times larger**. Every prover that can reach
even that size is published under a proprietary, **evaluation-only** licence tied to the
vendor's own proving network, so none of them could be shipped in a worker image or run
without reintroducing a trusted third party. General-purpose zkVMs are further away, not
closer: a transformer forward pass costs on the order of a billion cycles per token
inside one.

So the branch LLM step is secured by **verifier re-execution and slashing**, and the
strength of that rests entirely on the determinism measured below -- on one model, one
quantization and one prompt family.

What *is* proven runs and is described in [Proof Layer Status](proof-layer-status.md):
the aggregate reduction, gated on chain through Bonsol, and the branch canonicalization
receipt, verified off chain before a verifier will attest.

If a model proof is ever added here, it will use **public weights** with their hash
committed on chain. A valid proof of inference over *private* weights does not establish
that the declared model ran -- a prover can declare an architecture and parameter count
while embedding structured weights that collapse the real computation (Hollow-LLM,
IEEE S&P 2026) -- and weight privacy is a feature this protocol does not need.

## LLM Determinism

Local LLM inference can diverge even with `temperature=0` and a fixed seed. Phase 0e handles this honestly by snapping scalar outputs to basis points. The verifier re-executes the whole branch and compares its own canonical commitment with the worker's receipt. If an honest worker and honest verifier land in different buckets, the verifier attestation hash differs and the challenge path fires -- for the verifier assigned to that job.

This creates a false-positive slash risk. Operators should run repeated local trials for each model and prompt family before accepting paid traffic. `KSWARM_CHALLENGE_ON_MISMATCH=false` keeps the attestation, which still makes the receipt challengeable, but stops the verifier from challenging on its own. The blast radius is bounded by who may challenge: `challenge_job` accepts only the verifier the customer or the protocol admin assigned to the job with `assign_verifier`, for every job class. An unassigned verifier's mismatching attestation therefore slashes nobody by itself; the daemon logs `challenge refused ... code=ChallengeRequiresAssignedVerifier` and counts `challenges_not_assigned`.

### Phase 0e Tier 2 Measurement

On 2026-05-09, Tier 2 was run against `llama3.2:3b-instruct-q5_K_M` on a local Ollama endpoint with `temperature=0`, fixed branch RNG seed `42`, and a fixed scalar prompt. The sample used 20 real branch jobs and 40 real LLM calls: one worker call and one verifier re-execution per job. Two branch-worker wallets were used, 10 jobs each, to respect the protocol active-claim cap for unsettled jobs.

Results:

- scalar match rate: `20/20`
- verifier attestation hash match rate: `20/20`
- false-positive verifier mismatches: `0`
- scalar output: `5000` bps on every worker and verifier call
- completion tokens: mean `43.0`, p50 `43`, p99 `43`
- LLM call latency across worker and verifier calls: mean `1.278s`, p50 `1.271s`, p99 nearest-rank `1.353s`
- narrative/rationale drift: none observed; every scalar rationale was `Unknown or ambiguous`

This is a small prompt/model sample, not a general determinism proof. New model builds, prompts, quantizations, and host settings need their own repeated trials.

> **Correction (2026-07-12):** The `5000`-bps-on-every-call result above was later root-caused to the branch worker's scalar **system prompt**, not to model or quantization limits. The line "Use 0.0 for impossible, 0.5 for unknown or ambiguous, and 1.0 for certain." anchored the model to 0.5 for any uncertain-but-informative input. Driving the real `BranchExecutor` against the live model with that anchor reworked restores discrimination (e.g. MLB favorite/underdog/even = `0.70/0.20/0.50`) while preserving a genuine ~0.5 fallback and byte-identical verifier determinism. Fixed on branch `fix/branch-prompt-discrimination`. The determinism finding above stands; the implied "inherent flat scalar output" does not.

> **Note (2026-09-03):** Before the worker-trust changes, the verifier did not re-execute at all. It re-hashed the worker's own artifact, so the "verifier re-execution" in the 2026-05-09 measurement describes the intended design, not the code that ran. Re-execution is now the default; the hash-only path survives only behind `VERIFIER_HASH_ONLY=1`.

## Claim Risk

There is no on-chain "release claim" instruction. A claimed job that is not submitted before `execute_deadline` is slashed by `slash_stale_job` for the full `required_stake`. The branch worker reduces this risk (pre-claim health checks, a claim budget against on-chain `active_claims`, retries until the deadline margin, a circuit breaker, and a stake-at-risk log) but cannot remove it:

- A process crash or host loss after `claim_job` loses the stake.
- A model that keeps producing invalid output for one input loses that job's stake; the executor retries the identical request a bounded number of times and then abandons without submitting, which is the correct choice over committing a fabricated value.
- `max_concurrent_claims` counts completed jobs that are waiting out their challenge window, so a low value also limits throughput to one job per challenge window per unit of budget. That is the price of bounding locked stake.

## Verifier Re-execution Limits

- **Model identity is not bound on-chain.** The job binds a software digest, not a model. A worker whose output claims a model this verifier does not run is skipped, not challenged, because the verifier cannot reproduce that configuration. Such a job receives no attestation from this verifier. A network needs a verifier for every model it accepts, or an on-chain model binding.
- **The verifier must run the identical configuration.** `LLM_MODEL_NAME`, `LLM_MAX_TOKENS`, and the system prompt are part of `llm_version_hash`. A verifier with a different value produces a different hash for every job and, with `KSWARM_CHALLENGE_ON_MISMATCH` on, challenges every honest worker. Treat those values as network parameters.
- **Hash-only mode cannot catch a lie.** It only detects an artifact that does not match its receipt. It is a diagnostic mode behind `VERIFIER_HASH_ONLY=1` and logs a warning on every attestation.
- **Each verification costs a model call.** A verifier outage leaves jobs unattested, and an attestation is one-shot per job, so a verifier only attests after a successful re-execution.

## Narrative Verification

Tier B narrative text is not cryptographically verified for correctness, source faithfulness, prose quality, or semantic completeness. It is committed through IPFS and transcript hashes. The verifier re-executes the branch, but only the scalar guardrails enter the canonical hash; two honest runs with different prose still match.

Customer reports must keep the verified guardrail scalar separate from any narrative claim.

## Cost

Each branch normally uses at least one LLM call, and each verifier uses another LLM call. A run with `N` branches therefore costs roughly:

```text
N worker calls + N verifier calls + optional aggregate/report calls
```

Rejected model output adds up to `LLM_INVALID_OUTPUT_RETRIES` identical calls per branch. Cost depends on the configured local endpoint and model. Phase 0e does not include a scheduler-level budget cap.

## IPFS Pinning

Phase 0e uses a local IPFS API. Public or cluster pinning policy is not implemented here. Operators must preserve local pins for input, transcript, output, evidence, parent manifest, and aggregate artifacts.

Public pinning, retention windows, garbage-collection policy, and multi-peer availability are Phase 1 launch concerns.

## OASIS Availability

The branch executor can call `SimulationRunner.start_simulation` when a `BranchInput` includes an existing `oasis_simulation_id` and `run_oasis=true`. Phase 0e does not synthesize a full Zep graph or OASIS persona library from scratch inside the worker.

If the OASIS/Zep stack is unavailable, the worker fails the job execution path rather than substituting mocked simulation output.

## Bonsol Aggregate Path

`settle_aggregate_proof_job` requires the Bonsol marker's `output_digest` to equal `sha256(result_bytes)`. The aggregate receipt bytes must therefore be the reducer's committed outputs; the execution id and digests cannot live inside the receipt. The runner binds the execution by running `KSWARM_BONSOL_AGGREGATE_COMMAND`, validating the returned `committed_outputs` against `output_digest` and `journal_hash`, checking the digests against the aggregate job account, and submitting those bytes. The full result (combiner, parameters, values, binding) is in the IPFS aggregate artifact referenced by the receipt's `output_cid`. A failed hook fails the aggregation; nothing is claimed.

Known gaps:

- `predict open` opens the aggregate job with a zero `required_software_digest`, a zero `expected_result_hash`, and `input_bundle_hash = sha256(aggregate-input.json)`. Such a job cannot settle through `settle_aggregate_proof_job`, whatever the runner submits, because the marker's `image_id`, `journal_hash`, and `input_digest` can never match it. The flagship demo avoids this with `--defer-aggregate-open` and a manual `job open` that binds the reducer image id, framed-input digest, and journal hash. The CLI needs the same binding for production runs.
- `trimmed-mean` requires `parent_manifest.combiner_parameters.trim_bps` (Phase 1 ADR: manifests must bind trim basis points). `predict open` does not write it yet, so a `trimmed-mean` run fails closed until the CLI binds it.
- `weighted-mean` uses uniform weights because the manifest carries no per-branch weights. The result records `combiner_parameters.weights = "uniform"`.
- Without the hook, the runner submits canonical `MFA2` bytes with `receipt_binding: mfa2-canonical-unbound` and a warning. That receipt is for local development only and cannot settle through `settle_aggregate_proof_job`.

## Deferred Work

A per-branch proof of a model -- the DistilBERT risk-scorer lane sketched for Phase 2 --
is not deferred so much as blocked. The plan assumed a hidden dimension of 768, which the
proving toolkit it named is measured to run out of memory on, on a 128 GB machine; the
systems that can prove a 124M-parameter model are licensed for evaluation only. Nothing
in the tree stands in for that lane. The two-feature linear placeholder that used to
carry the name was removed on 2026-09-04, because a proof of
`2 * line_count + 3 * word_count + 1` says nothing about a forecast, and because the
package that produced it ships with no licence file while its documentation asserts that
commercial use requires one.

Domain-specific narrative merging, ranked-list combiners, image combiners, and LLM-as-judge cryptographic lanes are not part of Phase 0e.
