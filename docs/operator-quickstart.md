# Operator Quickstart

!!! note "This page describes the whole kswarm source tree"
    Some of what it names is not published in this repository: the local
    development and Bonsol evaluation compose stacks, their `make` targets,
    the flagship demo scripts, `scripts/bootstrap-handson.sh`, and the
    Polymarket adapter. `docker-compose.swarm.yml` and
    `scripts/swarm-smoke.sh` are here and run the swarm end to end on a
    local validator. See "Not published here yet" in the README.

This walkthrough assumes the unified local development stack created by:

```bash
make handson-up
source runtime/handson.env
cd cli
```

The bootstrap starts Kubo IPFS, the Bonsol stack, and the local Solana validator. The validator loads the protocol program as an upgradeable program whose authority is the Bonsol client keypair, because `initialize_protocol` accepts only the program's upgrade authority, so the bootstrap installs that keypair as the `admin` wallet before anything else. An `admin` wallet that already exists and holds a different key is not overwritten: the bootstrap stops and tells you to move it aside. It creates `admin`, `customer`, `worker-a`, `verifier`, and `aggregator`, creates a stand-in KAI mint (classic SPL Token, 6 decimals), initializes the protocol with the default stake floors, registers the worker roles, stakes each operator above its floor (`worker-a` at tier two), and writes `~/.config/kswarm/handson-state.json`.

If the host does not have `uv` or a Python version compatible with the CLI package, use the Docker-backed command printed by `make handson-up`. The bootstrap provisions the `python-toolchain` container for that path.

## Containerized Stack

The same flow runs from the shipped images without a Python checkout. `docker-compose.swarm.yml` starts a local validator with the program, Kubo, a one-shot bootstrap (`kswarm swarm bootstrap`: wallets, stand-in mint, `initialize_protocol` with the default floors, funding, registration, stake), and the three daemons. Every operator command then runs in the `cli` image inside the compose network:

```bash
cargo build-sbf --tools-version v1.51 --manifest-path solana/programs/kswarm_protocol/Cargo.toml -- --locked
export LLM_BASE_URL=http://<host>:11434/v1 LLM_MODEL_NAME=llama3.2:3b-instruct-q5_K_M
docker compose -f docker-compose.swarm.yml --profile local up -d
docker compose -f docker-compose.swarm.yml run --rm cli --json protocol show
docker compose -f docker-compose.swarm.yml run --rm cli --json predict open \
  --question "Will sentiment around the seeded public news item be net-negative?" \
  --output-kind scalar --branches 2 --reward-per-branch 1KAI --aggregator-reward 1KAI --challenge-window 600
docker compose -f docker-compose.swarm.yml run --rm cli --json predict status <parent-run>
docker compose -f docker-compose.swarm.yml run --rm cli --json predict report <parent-run>
docker compose -f docker-compose.swarm.yml run --rm cli --json settle <branch-job>   # after the challenge window
docker compose -f docker-compose.swarm.yml --profile local down -v
```

`scripts/swarm-smoke.sh` runs exactly that sequence end to end. Images, environment, profiles, and the registry are described in [Containers](containers.md).

The local validator loads the program as an upgradeable program whose authority is the bootstrap's `admin` wallet, because `initialize_protocol` accepts only the upgrade authority.

## Preflight

```bash
uv run kswarm protocol show
uv run kswarm token balance customer
uv run kswarm worker show worker-a
uv run kswarm worker show verifier
curl -fsS -X POST "${KSWARM_IPFS_API_URL}/api/v0/version"
make -C .. dev-status
```

Expected result: protocol config is present (`payment_decimals` is `6`, `token_program` is the classic SPL Token program), customer has KAI, `worker-a` is `worker-proof`, `verifier` is `verifier`, IPFS reports a Kubo version, and every core compose service is healthy.

## Keys And Secrets

- The protocol program id is `ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM`. It is `declare_id!` in `solana/programs/kswarm_protocol/src/lib.rs` and `KSWARM_PROGRAM_ID` in `cli/kswarm_cli/constants.py`. The `local` and `devnet` profiles carry it; `mainnet` gets one only after the audited deployment.
- The program keypair is held in the project's secret store, not in this repository; `SECURITY.md` records where. It is needed only to deploy or upgrade the program on a real cluster. Never copy it into the repository or into `runtime/`.
- CLI wallets live in `~/.config/kswarm/wallets/<name>.json`. The CLI creates that directory with mode `0700` and every wallet file with mode `0600`. It refuses to load a wallet or a `--keypair` file that group or others can read, and prints the `chmod 600` command that fixes it.
- The compose stacks write their own key material outside git: runtime wallets to `runtime/protocol/*.json` (random per deployment, mode `0600`) and the IPFS private-network key to `runtime/ipfs/swarm.key`. Create both directories yourself before the first start (`install -d -m 700 runtime/protocol runtime/ipfs`) so they belong to your user, not root.
- Run `scripts/check-no-secrets.sh` before every commit. CI runs it first and fails on any tracked key file, 64-byte secret-key array, PEM private key, or swarm key.

## Scenario A: Branch-Proof Happy Path

1. Open a branch-proof job.

   ```bash
   JOB_A="$(uv run kswarm --json job open --as customer --class branch-proof --reward 25 --required-stake 500 --challenge-window 30 --capability worker-proof --required-tier T1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["job"])')"
   echo "${JOB_A}"
   ```

   Expected output: a job pubkey.

2. Commit the input artifact.

   ```bash
   uv run kswarm job commit-input --job "${JOB_A}" --cid bafkreihandsoninputa --as customer
   uv run kswarm inspect job "${JOB_A}"
   ```

   Expected output: `status_name` is `open` and `input_cid` is `bafkreihandsoninputa`.

3. Claim the job as the worker.

   ```bash
   uv run kswarm job claim "${JOB_A}" --as worker-a
   uv run kswarm inspect job "${JOB_A}"
   ```

   Expected output: `status_name` is `claimed` and `worker` is the `worker-a` pubkey.

4. Submit a receipt.

   ```bash
   uv run kswarm job submit-receipt "${JOB_A}" --output-cid bafkreihandsonoutputa --result-bytes 0a0b0c --as worker-a
   ```

   Expected output includes `submitted_result_hash`:

   ```text
   9909ec831e2cf6d0c73fb5480f31945a80987a13faee005704166cb53a26ceca
   ```

5. Submit a matching verifier attestation.

   ```bash
   uv run kswarm attest "${JOB_A}" --result-hash 9909ec831e2cf6d0c73fb5480f31945a80987a13faee005704166cb53a26ceca --evidence-cid bafkreiverifiera --software-digest worker-canonical --as verifier
   ```

   Expected output: a transaction signature.

6. Wait for the challenge window and settle.

   ```bash
   sleep 31
   uv run kswarm token balance worker-a
   uv run kswarm settle "${JOB_A}"
   uv run kswarm inspect job "${JOB_A}"
   uv run kswarm token balance worker-a
   ```

   Expected result: `status_name` is `settled`, and the worker's KAI balance increases by `25`.

## Scenario B: Branch-Proof Slash Flow

1. Open a second branch-proof job with a smaller challenge bond so the verifier and customer both receive slash proceeds.

   ```bash
   JOB_B="$(uv run kswarm --json job open --as customer --class branch-proof --reward 25 --required-stake 500 --challenge-bond 100 --challenge-window 120 --capability worker-proof --required-tier T1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["job"])')"
   echo "${JOB_B}"
   ```

2. Commit, claim, and submit the worker receipt.

   ```bash
   uv run kswarm job commit-input --job "${JOB_B}" --cid bafkreihandsoninputb --as customer
   uv run kswarm job claim "${JOB_B}" --as worker-a
   uv run kswarm job submit-receipt "${JOB_B}" --output-cid bafkreihandsonoutputb --result-bytes 0a0b0d --as worker-a
   ```

3. Submit a mismatched verifier attestation.

   ```bash
   uv run kswarm attest "${JOB_B}" --result-hash ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff --evidence-cid bafkreiverifierb --software-digest worker-canonical --as verifier
   ```

   Expected output: a transaction signature. The attestation is accepted because mismatches are recorded first and challenged next.

4. Challenge the receipt.

   ```bash
   uv run kswarm challenge "${JOB_B}" --as verifier
   uv run kswarm inspect job "${JOB_B}"
   ```

   Expected output: `status_name` is `slashed`, `challenger` is the verifier pubkey, and `slash_settled` is `false`.

5. Complete slash payouts.

   ```bash
   uv run kswarm refund-slashed "${JOB_B}"
   uv run kswarm claim-verifier-slash-reward "${JOB_B}" --as verifier
   uv run kswarm claim-customer-slash-compensation "${JOB_B}" --as customer
   uv run kswarm inspect job "${JOB_B}"
   ```

   Expected result: `escrow_refunded`, `verifier_reward_paid`, `customer_slash_paid`, and `slash_settled` are all `true`.

6. Observe balances.

   ```bash
   uv run kswarm token balance customer
   uv run kswarm token balance verifier
   uv run kswarm token balance worker-a
   ```

   Expected result: customer recovers escrow plus remaining slashed stake; verifier receives the `100` KAI challenge bond amount from worker stake.

## Scenario C: Aggregate-Proof Full Flow With Bonsol

This path uses the Bonsol callback harness through `docker-compose.dev.yml`. The CLI still inspects the marker and performs the final aggregate settlement command.

1. Confirm the Bonsol stack is alive.

   ```bash
   ../scripts/bin/solana -u http://127.0.0.1:38899 block-height
   curl -fsS http://127.0.0.1:38080 >/dev/null
   ```

2. Run the production callback harness setup and execution.

   ```bash
   docker compose -f ../docker-compose.dev.yml --profile tier3 run --rm --no-deps \
     -e PHASE0_CALLBACK_TEST_MODE=settle \
     bonsol-callback-smoke-test
   ```

   Expected output: the harness reports a production marker PDA and aggregate job pubkey.

3. Inspect the marker.

   ```bash
   EXECUTION_ID="$(ls -1 ../runtime/bonsol/phase0b-callback/*-prepared.json | tail -1 | xargs -I{} jq -r '.executionId' {})"
   uv run kswarm inspect marker "${EXECUTION_ID}"
   ```

   Expected output: at least one `BonsolAggregateVerification` marker with `status_name` equal to `verified`.

4. Settle the aggregate job if the harness was run in marker-only mode.

   ```bash
   AGGREGATE_JOB="$(ls -1 ../runtime/bonsol/phase0b-callback/*-prepared.json | tail -1 | xargs -I{} jq -r '.aggregateJob' {})"
   uv run kswarm settle-aggregate "${AGGREGATE_JOB}"
   uv run kswarm inspect job "${AGGREGATE_JOB}"
   ```

   Expected result: `status_name` is `settled`. If the callback smoke test was run with `PHASE0_CALLBACK_TEST_MODE=settle`, the job may already be settled.

## Demo 1: Public Opinion Forecast

This scenario requires a local chat-completions-compatible LLM endpoint. The IPFS API comes from `make handson-up`.

```bash
export LLM_BASE_URL=http://127.0.0.1:<llm-port>/v1
export LLM_MODEL_NAME=<local-model-id>
export LLM_API_KEY=local-llm
source runtime/handson.env
```

Run the demo script from the repository root:

```bash
scripts/demo-public-opinion.sh
```

The script creates local wallets, registers branch, verifier, and aggregate workers, opens a two-branch prediction run, executes real LLM branch work, submits verifier attestations, runs the aggregate runner, and prints a `public_opinion_report` JSON object.

The manual equivalent is:

```bash
cd cli
PARENT_RUN="$(uv run kswarm --json predict open \
  --question "Will sentiment around the seeded public news item be net-negative?" \
  --output-kind scalar \
  --branches 2 \
  --combiner weighted-mean \
  --reward-per-branch 1KAI \
  --aggregator-reward 1KAI \
  --challenge-window 600 \
  --persona-set builtin-public-opinion-v1 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["parent_run"])')"

cd ..
PYTHONPATH="${PWD}/cli:${PWD}/backend:${PWD}/worker" KSWARM_WORKER_KEYPAIR=worker-a python3 -m branch_worker.cli --once
PYTHONPATH="${PWD}/cli:${PWD}/backend:${PWD}/worker" KSWARM_WORKER_KEYPAIR=verifier python3 -m verifier_worker.cli --once
PYTHONPATH="${PWD}/cli:${PWD}/backend:${PWD}/worker" KSWARM_WORKER_KEYPAIR=aggregator python3 -m aggregator_runner.cli --once --allow-completed-branches

cd cli
uv run kswarm predict report "${PARENT_RUN}"
```

### If `predict open` stops early

`predict open` prints `parent_run=<pubkey> base_nonce=<u64> run_manifest=<path>` on stderr before its first transaction and updates the manifest after every confirmed one. Escrow is locked only in the jobs the manifest marks `committed` or `opened`. Check, then either continue or unwind:

```bash
uv run kswarm predict status "${PARENT_RUN}"
uv run kswarm predict resume "${PARENT_RUN}"            # continue; confirmed steps are skipped
uv run kswarm predict cancel "${PARENT_RUN}" --as customer   # or unwind every unclaimed job
```

Expected result of `resume`: `run_status` is `open` and every job shows `manifest_status` `committed`. Expected result of `cancel`: `run_status` is `cancelled`, `cancelled_jobs` lists every job that was awaiting artifact or open, and the customer's KAI balance is restored. A cancelled run cannot be resumed; open a new one.

For production-style aggregate settlement, configure the aggregate runner's Bonsol hook:

```bash
export KSWARM_BONSOL_AGGREGATE_COMMAND="<command that reuses protocol/scripts/run-bonsol-callback-smoke-test.sh or the callback harness>"
```

The aggregate job is bound at open time: its `required_software_digest` is the reducer image id (`--aggregate-image-id`, `KSWARM_AGGREGATE_IMAGE_ID`, or the checked-in default), its `input_bundle_hash` is the framed digest of the committed `aggregate-input.json`, and its `expected_result_hash` is the journal hash the reducer must produce over that input. The hook must run the reducer over exactly that artifact, and the aggregator wallet must be registered with `--software-digest <image-id>`; otherwise `claim_job` fails with `SoftwareDigestMismatch` and `settle-aggregate` with `BonsolMarkerMismatch`.

## Scenario D: Flagship Policy Passage Forecast

This Tier A demo produces `runtime/demo-policy-passage-forecast/customer-report.json` with a settled scalar, Bonsol marker PDA, and settlement signature.

```bash
cd /tmp/kswarm-phase0f-worktree
make dev-up
LLM_BASE_URL=http://<llm-host>:11434/v1 \
LLM_MODEL_NAME=llama3.2:3b-instruct-q5_K_M \
LLM_API_KEY=local-llm \
KSWARM_IPFS_API_URL=http://127.0.0.1:4501 \
PYTHON=/tmp/phase0e-venv/bin/python \
scripts/demo-policy-passage-forecast.sh
```

## Scenario E: Flagship Brand Crisis Trajectory

This Tier B demo produces `runtime/demo-brand-crisis-trajectory/customer-report.json`. The customer report must show that the narrative is hash-committed but not cryptographically verified for correctness.

```bash
cd /tmp/kswarm-phase0f-worktree
make dev-up
LLM_BASE_URL=http://<llm-host>:11434/v1 \
LLM_MODEL_NAME=llama3.2:3b-instruct-q5_K_M \
LLM_API_KEY=local-llm \
KSWARM_IPFS_API_URL=http://127.0.0.1:4501 \
PYTHON=/tmp/phase0e-venv/bin/python \
scripts/demo-brand-crisis-trajectory.sh
```

## Scenario F: Flagship OASIS Replication Study

This academic demo produces `runtime/demo-oasis-replication-study/customer-report.json` with a settled scalar replication receipt.

```bash
cd /tmp/kswarm-phase0f-worktree
make dev-up
LLM_BASE_URL=http://<llm-host>:11434/v1 \
LLM_MODEL_NAME=llama3.2:3b-instruct-q5_K_M \
LLM_API_KEY=local-llm \
KSWARM_IPFS_API_URL=http://127.0.0.1:4501 \
PYTHON=/tmp/phase0e-venv/bin/python \
scripts/demo-oasis-replication-study.sh
```

## Failure Pointers

- `InvalidJobState`: inspect the job and confirm the prior step changed state.
- `InsufficientAvailableStake`: inspect the worker and stake more KAI.
- `InsufficientStakeTier`: the worker's total stake is below the configured tier floor (default tier one 50,000 KAI). Check `protocol show` for the floors.
- `WrongTokenProgram`: the cluster profile's `token_program` does not match the on-chain config. Re-run `protocol initialize` to refresh the profile from chain.
- `AttestationWindowClosed`: use a longer challenge window or submit the attestation sooner.
- `BonsolMarkerMissing`: rerun Scenario C step 2 and inspect the prepared JSON under `runtime/bonsol/phase0b-callback/`.
- `SoftwareDigestMismatch` on an aggregate claim: the aggregator wallet's software digest is not the reducer image id the run was opened with (`predict status` and the run manifest's `bonsol.image_id` show it).
- `planned job account already exists (nonce collision)`: another run of the same customer already used a planned nonce; run `predict open` again, it draws a new random base.
- `run was cancelled` from `predict resume`: the run was unwound with `predict cancel`; open a new run.
- `IPFS_ARTIFACT_TOO_LARGE`: `predict report` refused an artifact above `KSWARM_IPFS_MAX_BYTES` (default 8 MiB); raise the limit only for artifacts you trust.
- `LLM_ENDPOINT_UNREACHABLE`: set `LLM_BASE_URL` and `LLM_MODEL_NAME`, then confirm the endpoint is reachable.
- `IPFS_UNREACHABLE`: run `source runtime/handson.env`, then confirm `KSWARM_IPFS_API_URL` points at the Kubo API. The default Phase 0f host port is `4501`, not `5001`.
- `port is already allocated`: rerun with remapped ports, for example `KSWARM_IPFS_API_PORT=14501 KSWARM_IPFS_GATEWAY_PORT=18088 make handson-up`.
