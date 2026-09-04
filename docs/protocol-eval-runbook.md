# Protocol Eval Runbook

!!! note "This page describes the whole kswarm source tree"
    Some of what it names is not published in this repository: the local
    development and Bonsol evaluation compose stacks, their `make` targets,
    the flagship demo scripts, `scripts/bootstrap-handson.sh`, and the
    Polymarket adapter. `docker-compose.swarm.yml` and
    `scripts/swarm-smoke.sh` are here and run the swarm end to end on a
    local validator. See "Not published here yet" in the README.

If you want one straight-line operator guide for the current stack, start with the three-node evaluation guide.

This runbook covers two paths:

- local smoke test on one machine
- real evaluation on three machines with an existing payment mint (KAI on mainnet; a stand-in classic SPL mint with 6 decimals on devnet or localnet)

The control plane (validator, program deploy, IPFS swarm, artifact gateway, settlement watcher) is `docker-compose.protocol.yml`. The workers are the Python stack in `docker-compose.swarm.yml` (`branch-worker`, `verifier-worker`, `aggregator-runner`, `cli`), see [Containers](containers.md). The Node worker is retired.

## 1. Requirements

- Docker and Docker Compose on every machine
- a reachable Solana RPC
- an existing payment mint in shared environments (see [KAI Payment Token](kai-payment-token.md))
- enough SOL for admin, customer, watcher, and worker wallets
- enough payment tokens for the customer and worker wallets

## 2. Runtime Files

Every node reads runtime material from `./runtime/protocol`, bind-mounted into `/runtime/protocol`.

Important files:

- `admin.json`: admin keypair for the control plane (protocol admin, funds the other runtime wallets)
- `customer.json`: customer keypair used in demo flow
- `verifier.json`: verifier keypair
- `watcher.json`: watcher/settler keypair
- `worker.json`: control-plane worker keypair, used only by the Node helper scripts (`show-eval-state.mjs`, `withdraw-unlocked-stake.mjs`); the Python daemons on worker machines use their own wallet files
- `protocol.json`: generated runtime config
- `deployed`: created by the deployer once the program is on chain
- `ready`: created by `protocol-bootstrap` once the protocol is initialized and `protocol.json` is written
- `payment-mint.json`: optional local-only mint file; not needed when `PROTOCOL_PAYMENT_MINT` is supplied

Every keypair file is random, generated once per deployment by the deployer, and written with mode `0600`. Every reader refuses a key file that group or others can read, and fails closed when a required key is missing. Deterministic seed-derived wallets exist only for throwaway localnet runs: set `KSWARM_INSECURE_LOCALNET_SEEDS=1` together with `SOLANA_CLUSTER=localnet`; on any other cluster the flag is refused.

`runtime/` is gitignored and stays local to each operator. Create the bind-mounted directories as your own user before the first start, so the uid 1000 containers can write to them:

```bash
install -d -m 700 runtime/protocol runtime/ipfs
```

Deploy keys for a real cluster (the program keypair and the upgrade authority) never live in the repository. Keep them in a directory outside it and point `PROTOCOL_KEYS_HOST_DIR` at it; the deployer mounts it read-only at `/keys`. Localnet needs no deploy keys because the validator loads the program at genesis.

## 3. Local Smoke Test

The one-host path is the containerized swarm smoke test. It builds the four images, starts a local validator with the program, bootstraps wallets, mint, protocol, registrations, and stake, opens a two-branch prediction, waits for the branch worker, verifier, and aggregator, reads the report, settles both branch jobs, and tears down:

```bash
cargo build-sbf --tools-version v1.51 --manifest-path solana/programs/kswarm_protocol/Cargo.toml -- --locked
LLM_BASE_URL=http://<host>:11434/v1 LLM_MODEL_NAME=llama3.2:3b-instruct-q5_K_M scripts/swarm-smoke.sh
```

The log lands under `runtime/swarm-smoke/`. That path is only for single-host local validation.

For the hands-on protocol path with IPFS and Bonsol available together, prefer the unified dev stack:

```bash
make handson-up
source runtime/handson.env
make dev-status
```

See [docs/dev-infrastructure.md](dev-infrastructure.md) for service topology, port remapping, healthchecks, and recovery commands.

## 4. Three-Machine Eval Topology

Use these roles:

- Machine 1: control plane
- Machine 2: worker A
- Machine 3: worker B

Recommended services per machine:

- Machine 1 (`docker-compose.protocol.yml`):
  - `solana-validator` for localnet eval, or use shared testnet RPC
  - `protocol-program-builder`
  - `protocol-deployer`
  - `protocol-bootstrap`
  - `protocol-api`
  - `protocol-watcher`
  - `ipfs-bootstrap`
- Machine 2 (`docker-compose.swarm.yml`, `devnet` profile against Machine 1's RPC):
  - `ipfs`
  - `bootstrap-devnet`
  - `branch-worker-devnet`
  - `verifier-worker-devnet`
- Machine 3 (same files):
  - `ipfs`
  - `bootstrap-devnet`
  - `branch-worker-devnet`
  - `aggregator-runner-devnet`

The customer opens runs from the `cli` image on any machine that has the customer wallet. Every service mints nothing and reads the mint, decimals, and floors from the on-chain config.

## 5. Prepare Runtime Keypairs

Generate missing keypairs inside Docker.

Control plane:

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node protocol/scripts/create-runtime-keypair.mjs admin
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node protocol/scripts/create-runtime-keypair.mjs customer
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node protocol/scripts/create-runtime-keypair.mjs watcher
```

Worker machine:

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node protocol/scripts/create-runtime-keypair.mjs worker
```

If a key file already exists, leave it in place. `create-runtime-keypair.mjs` refuses to replace an existing file unless you pass `--force`, and the deployer never overwrites existing runtime keys. The deployer also generates any missing control-plane key itself, so on a single host this step is optional.

## 6. Fund Wallets

For shared eval environments:

- fund `admin`, `customer`, `watcher`, and each worker wallet with SOL
- fund `customer` with enough payment tokens to cover job escrow
- fund each worker wallet with enough payment tokens to cover its stake floor

The protocol does not mint shared-environment tokens for you.

Current worker policy (`kswarm swarm bootstrap`, see [Node Requirements](node-requirements-matrix.md)):

- the branch worker registers as `worker-proof` and stakes the tier-one floor (`50,000 KAI` by default)
- the verifier registers as `verifier` and stakes the verifier floor (`100,000 KAI` by default)
- the aggregator registers as `worker-proof` with the Bonsol aggregate capability and the reducer image id, and stakes the tier-one floor
- `--worker-stake`, `--verifier-stake`, and `--aggregator-stake` raise those targets; stake is topped up, never withdrawn

## 7. Bring Up The Control Plane

Example with local validator and an external mint:

```bash
export PROTOCOL_PAYMENT_MINT=<payment-mint-address>
export PROTOCOL_AUTO_FUND_SOL=0
export PROTOCOL_BOOTSTRAP_LOCAL_MINT=0
# Optional: stake floors in human units (defaults shown).
export PROTOCOL_STAKE_FLOORS=50000,250000,1000000
export PROTOCOL_VERIFIER_STAKE_FLOOR=100000
export IPFS_BOOTSTRAP_ADVERTISE_HOST=<machine-1-hostname-or-ip>
# protocol-bootstrap writes into runtime/protocol; set the uid:gid that owns it.
export PROTOCOL_RUNTIME_UID=$(id -u) PROTOCOL_RUNTIME_GID=$(id -g)

docker compose -f docker-compose.protocol.yml up --build \
  solana-validator \
  protocol-program-builder \
  ipfs-bootstrap \
  protocol-deployer \
  protocol-bootstrap \
  protocol-api \
  protocol-watcher
```

If you are using a shared external RPC instead of the local validator:

```bash
export PROTOCOL_RPC_URL=https://your-rpc.example
export KSWARM_CLUSTER=devnet
```

`protocol-deployer` deploys the program with `admin.json` as the upgrade authority and writes `deployed`. `protocol-bootstrap` then runs `kswarm protocol initialize` as that key (only the upgrade authority may initialize) and `kswarm protocol runtime-config`, which writes `runtime/protocol/protocol.json` and `ready`. `protocol-api` and `protocol-watcher` start after that.

With the local validator, the program is loaded at genesis from the builder's artifact and the deployer skips the deploy step. Against an external cluster that does not already hold the program, the deployer needs the program keypair and an upgrade authority, both kept outside the repository:

```bash
export SOLANA_CLUSTER=devnet
export PROTOCOL_KEYS_HOST_DIR=/secure/kswarm-keys          # holds the two files below, mode 0600
export PROTOCOL_PROGRAM_KEYPAIR=/keys/kswarm_protocol-keypair.json
export PROTOCOL_UPGRADE_AUTHORITY_KEYPAIR=/keys/upgrade-authority.json
```

The deployer checks that the program keypair derives to the declared program id, refuses group- or world-readable key files, and signs the deploy with the upgrade authority, which must hold enough SOL. Outside localnet there is no airdrop: fund `admin.json` (its public key is printed by the deployer) or set `PROTOCOL_AUTO_FUND_SOL=0` and fund every runtime wallet yourself.

## 8. Distribute Runtime Config To Workers

Worker machines do not need `protocol.json`; the Python daemons read the mint, decimals, and floors from the on-chain config. They need:

- Machine 1's RPC URL (`KSWARM_RPC_URL`)
- the payment mint address (`KSWARM_PAYMENT_MINT`, the same value the control plane used)
- their own wallet files (see the next section)

Do not copy the same worker wallet to multiple machines.

## 9. Join Remote IPFS Peers

The private network key is generated by `ipfs-bootstrap` on Machine 1 at `runtime/ipfs/swarm.key` (mode `0600`). Copy that file to `runtime/ipfs/swarm.key` on each worker machine before starting its peers; a peer without the key exits with an error instead of joining a public swarm.

Set the bootstrap multiaddr on each worker machine:

```bash
export IPFS_BOOTSTRAP_MULTIADDR=/dns4/<machine-1-hostname-or-ip>/tcp/4001/p2p/<bootstrap-peer-id>
```

The bootstrap node also supports explicit advertise settings:

```bash
export IPFS_BOOTSTRAP_ADVERTISE_HOST=<machine-1-hostname-or-ip>
export IPFS_BOOTSTRAP_ADVERTISE_KIND=dns4
```

If you want the exact bootstrap multiaddr, read it on Machine 1:

```bash
cat runtime/ipfs/bootstrap.addr
```

Or inspect the bootstrap peer id directly inside the container.

## 10. Start Worker Machines

Each worker machine holds its wallet files in a directory (`KSWARM_WALLETS_DIR`, `./secrets/wallets` by default) named `admin.json`, `customer.json`, `worker-a.json`, `verifier.json`, and `aggregator.json` (the ones the machine runs must exist and be funded; `admin.json` is only read when the protocol still needs initialization). Create them with `kswarm wallet create <name>` on a dev host or `solana-keygen new -o <name>.json`.

Machine 2 (branch worker and verifier):

```bash
export KSWARM_RPC_URL=http://<machine-1-hostname-or-ip>:8899
export KSWARM_CLUSTER=local            # devnet when Machine 1 uses a public RPC
export KSWARM_PAYMENT_MINT=<payment-mint-address>
export KSWARM_WALLETS_DIR=./secrets/wallets
export LLM_BASE_URL=http://<llm-host>:11434/v1 LLM_MODEL_NAME=<model>

docker compose -f docker-compose.swarm.yml --profile devnet up -d ipfs bootstrap-devnet branch-worker-devnet verifier-worker-devnet
```

Machine 3 (branch worker and aggregator):

```bash
export KSWARM_RPC_URL=http://<machine-1-hostname-or-ip>:8899
export KSWARM_CLUSTER=local
export KSWARM_PAYMENT_MINT=<payment-mint-address>
export KSWARM_WALLETS_DIR=./secrets/wallets
export LLM_BASE_URL=http://<llm-host>:11434/v1 LLM_MODEL_NAME=<model>

docker compose -f docker-compose.swarm.yml --profile devnet up -d ipfs bootstrap-devnet branch-worker-devnet aggregator-runner-devnet
```

`bootstrap-devnet` registers and stakes the wallets it finds (no airdrop, no mint, no funding), then the daemons start. Each branch worker claims only what it can execute, the verifier re-executes every completed branch, and the aggregator combines a run once its branches are attested.

Kubo on the worker machines is a plain node; join it to the control plane's private swarm with the multiaddr from section 9 if artifacts must stay inside that swarm.

## 11. Run The Demo Job

Open a prediction run from the `cli` image on a machine that has the customer wallet:

```bash
docker compose -f docker-compose.swarm.yml run --rm cli --json predict open \
  --question "Will sentiment around the seeded public news item be net-negative?" \
  --output-kind scalar --branches 2 --reward-per-branch 1KAI --aggregator-reward 1KAI --challenge-window 600
docker compose -f docker-compose.swarm.yml run --rm cli --json predict status <parent-run>
docker compose -f docker-compose.swarm.yml run --rm cli --json predict report <parent-run>
```

The branch workers claim, execute, and submit receipts; the verifier attests; the aggregator submits the aggregate receipt; the watcher on Machine 1 settles the branch jobs after the challenge window (or run `kswarm settle <job>`).

## 12. Run The Swarm Demo

The prediction run above is the swarm demo: one parent request expands into `N` branch jobs and one aggregate job, each branch is independently escrowed, claimed, executed, attested, and settled, and the aggregate receipt is derived from the branch outputs. `scripts/swarm-smoke.sh` runs it end to end on one host.

## 13. Three-Machine Swarm Eval

Bring up the control plane on Machine 1 and the worker profiles on Machines 2 and 3 as described above, then open the run from the `cli` image. Nothing else differs from the single-host path.

## 14. What This Demonstrates

- users cannot create work without on-chain KAI escrow
- workers cannot claim without on-chain staked KAI above the configured tier floor
- payment is released only by Solana settlement
- stale workers can be slashed and the user refunded
- artifacts are off-chain, but payment and state transitions are on-chain
- one submitted request can expand into `N+` settled child jobs across the swarm
- branch receipts can carry real proof artifacts for `EZKL` and `zkVM`

## 15. Current Limits

- the Python stack has no settle daemon; settlement is the Node watcher or `kswarm settle`
- multi-machine startup is operator-driven; there is not yet an auto-discovery control plane
- the parent request is off-chain orchestration; parent/child linkage is not yet a first-class on-chain account model
- the aggregate job settles only through the Bonsol marker path; without `KSWARM_BONSOL_AGGREGATE_COMMAND` the aggregate receipt cannot settle on chain
- verification is re-execution by a staked verifier, not a proof of the LLM output

## 16. Manual Operator Helpers

The Python CLI covers every lifecycle step (`kswarm --help`, [CLI Reference](cli-reference.md)); run it from the `cli` image or a dev host. The Node helpers below remain for the control plane's runtime keys:

Cancel an open job:

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node scripts/cancel-open-job.mjs <job-address>
```

Settle a completed job manually:

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node scripts/settle-job.mjs <job-address>
```

Slash a stale claimed job manually:

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node scripts/slash-stale-job.mjs <job-address>
```

Withdraw unlocked worker stake (the control plane's `worker.json`):

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node scripts/withdraw-unlocked-stake.mjs <amount-ui>
```

Inspect current balances and jobs:

```bash
docker compose -f docker-compose.protocol.yml --profile tools run --rm protocol-tooling node scripts/show-eval-state.mjs
```

## 17. Unified Bonsol And IPFS Local Eval

Use `docker-compose.dev.yml` for current single-machine Bonsol evaluation. It extends the existing Bonsol compose services without modifying `docker-compose.bonsol.yml`, adds Kubo IPFS, and puts every service on the `kswarm-dev` network.

Bring up the core stack:

```bash
make dev-up
make dev-status
```

Run the Tier 3 callback harness through the unified stack:

```bash
docker compose -f docker-compose.dev.yml --profile tier3 run --rm --no-deps \
  -e PHASE0_CALLBACK_TEST_MODE=settle \
  bonsol-callback-smoke-test
```

Run the public-opinion demo after setting a local LLM endpoint:

```bash
source runtime/handson.env
export LLM_BASE_URL=http://127.0.0.1:<llm-port>/v1
export LLM_MODEL_NAME=<local-model-id>
export LLM_API_KEY=local-llm
RUN_TIER3_DEMO=1 PYTHONPATH=$PWD/backend:$PWD/worker:$PWD/cli python3 -m pytest worker/tests/test_demo_e2e.py -q
```

What this does:

- runs a real Kubo daemon for branch inputs, worker outputs, verifier evidence, and aggregate artifacts
- builds the Bonsol verifier and callback programs
- builds the `mirofish_bonsol_branch_reducer` image
- starts a Bonsol local validator, image server, and prover node
- executes production-style Bonsol callback verification against the local validator

Expected result:

- `make dev-status` shows all core services healthy
- the callback harness prints JSON with `"status": "success"`
- `runtime/ipfs/api` contains the Kubo host API URL used by CLI and workers

Known behavior:

- the first few execute attempts may retry with `Invalid deployment account` / `custom program error: 0x12`
- the smoke script handles this automatically because it is a fresh-local-validator deployment visibility race
- Kubo CORS is set to `Access-Control-Allow-Origin = ["*"]` for local development only

## 18. Three-Tier Testing Strategy

Use `docs/testing-strategy.md` as the command reference.

Tier 1 runs on every pull request:

```bash
CARGO_TARGET_DIR=/tmp/phase0c-cargo cargo test --package anchor_integration --features tier1 -- --test-threads=1
```

Tier 1 uses `solana_program_test::ProgramTest` and `BanksClient`. It covers Q1 attestation, R3 reassignment/cancel paths, account state machines, payment CPIs, and aggregate settlement error paths. It does not load Bonsol.

Tier 2 runs on a Docker-capable self-hosted runner for `main`, manual dispatch, or pull requests labeled `tier2-bonsol`:

```bash
docker compose -f docker-compose.bonsol.yml build bonsol-builder
CARGO_TARGET_DIR=/tmp/phase0c-cargo cargo test --package anchor_integration --features tier2-bonsol -- --test-threads=1 --nocapture
```

Tier 2 uses the pinned real `bonsol.so`, real callback program, real `kswarm_protocol.so`, and real `bonsol-node`. The harness uses the existing Bonsol Compose stack to spawn a local `solana-test-validator`, image server, and prover node on non-conflicting ports. It reuses `BONSOL_RUNTIME_HOST_DIR` when supplied, otherwise completes `/tmp/kswarm-bonsol-phase0b-fresh`, and only builds a Phase 0c runtime if no reusable artifact set exists.

Tier 3 remains the full Phase 0b smoke path above. Run it for release-candidate tags and operator eval rehearsals.
