# Containers

!!! note "This page describes the whole kswarm source tree"
    Some of what it names is not published in this repository: the local
    development and Bonsol evaluation compose stacks, their `make` targets,
    the flagship demo scripts, `scripts/bootstrap-handson.sh`, and the
    Polymarket adapter. `docker-compose.swarm.yml` and
    `scripts/swarm-smoke.sh` are here and run the swarm end to end on a
    local validator. See "Not published here yet" in the README.

The Python stack ships as four container images built from one Dockerfile,
`docker/swarm/Dockerfile`, and runs with `docker-compose.swarm.yml`. The Node
worker is retired; see [Foundation Prototype](protocol-foundation-prototype.md).

## Images

| Target | Entry point | Listens | What it does |
| --- | --- | --- | --- |
| `branch-worker` | `kswarm-branch-worker` | `:9461/metrics` | Claims branch-proof jobs it can execute, runs the LLM branch, proves the branch canonicalization receipt, submits the receipt. |
| `verifier-worker` | `kswarm-verifier-worker` | `:9462/metrics` | Re-executes completed branches, verifies their receipts, attests, challenges a mismatch. |
| `aggregator-runner` | `kswarm-aggregator-runner` | `:9463/metrics` | Combines attested branches of a run and submits the aggregate receipt. |
| `cli` | `kswarm` | nothing | Operator commands: bootstrap, `predict`, `settle`, inspection. |

`branch-worker` and `verifier-worker` additionally carry the zkVM prover: the
`zkvm-reducer` host binary at `/opt/kswarm/bin/kswarm-zkvm-reducer`, with the guest ELF
compiled into it and that guest's image id recorded beside it at
`/opt/kswarm/share/kswarm-zkvm-image-id`. It is built in a stage of its own from RISC
Zero components pinned to exact versions, and the build fails if the id it produces is
not the one `protocol/zkvm-reducer/IMAGE_ID` pins. `aggregator-runner` and `cli` do not
carry it: neither proves anything, and it is 150 MB of attack surface.

The aggregate proof is a different shape. Requesting a Bonsol execution needs the Bonsol
CLI, a funded client keypair and a docker socket, none of which belong in a hardened
worker, so the aggregator calls a proving service instead: see
[Proof Layer Status](proof-layer-status.md).

Every image:

- is built from `python:3.12-slim` and `uv`, both pinned by digest;
- installs exactly the packages in `docker/swarm/uv.lock` (`uv sync --frozen`);
- contains `cli/`, `worker/`, and only three pieces of the MiroFish engine:
  `backend/app/protocol/`, `backend/app/utils/llm_client.py`, and
  `backend/app/config.py`. The Flask app, OASIS, and Zep are not in the image.
  A branch that asks for an OASIS simulation fails with
  `OasisEngineUnavailable`; run such branches with `kswarm-worker[engine]`
  outside the image;
- runs as uid 10001 (`kswarm`), with `HOME=/home/kswarm`;
- carries OCI labels: `org.opencontainers.image.revision` is the git SHA,
  `.version` the tag, `.created` the build time, and `.source` and `.vendor` the
  repository the workflow built from. None of them is hardcoded in the Dockerfile;
- holds no key, RPC URL, token, or endpoint. Everything comes from the
  environment and mounted files listed below;
- checks its own health on `/metrics` (the daemons); the `cli` image has no probe.

Build one target by hand:

```bash
docker build -f docker/swarm/Dockerfile --target branch-worker \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" -t kswarm/branch-worker:local .
```

`docker/swarm/Dockerfile.dockerignore` is an allowlist. Keypairs, `runtime/`,
the Node stack, and the Flask app never enter the build context.

## Environment

| Variable | Used by | Meaning |
| --- | --- | --- |
| `KSWARM_CLUSTER` | all | Cluster profile: `local`, `devnet`, `mainnet`. |
| `KSWARM_RPC_URL` | all | Solana RPC URL. Overrides the profile on every cluster. |
| `KSWARM_PROGRAM_ID` | daemons | Protocol program id; the profile's value when unset. |
| `KSWARM_WALLET_FILE` | daemons | Path of the daemon's keypair file. Wins over `KSWARM_WORKER_KEYPAIR`. |
| `KSWARM_WORKER_KEYPAIR` | daemons | Wallet name under `~/.config/kswarm/wallets` (dev hosts). |
| `KSWARM_IPFS_API_URL` | all | Kubo API URL, `http://ipfs:5001` in compose. |
| `KSWARM_IPFS_MAX_BYTES` | cli | Largest artifact `predict report` will fetch. |
| `KSWARM_PREDICT_RUNS_DIR` | cli, aggregator | Run manifests. The images default to `/var/lib/kswarm/predict_runs`. |
| `LLM_BASE_URL`, `LLM_MODEL_NAME` | branch, verifier | OpenAI-compatible chat completions endpoint and model. Required, and declared `${LLM_BASE_URL:?...}` in the compose file, so even `docker compose -f docker-compose.swarm.yml --profile local config` fails without them (`required variable LLM_BASE_URL is missing a value`, exit 15). Export both before validating or starting the stack. |
| `LLM_API_KEY` | branch, verifier | Bearer token; `local-llm` for local servers. |
| `LLM_MAX_TOKENS` | branch, verifier | Completion cap; 12000 by default. |
| `KSWARM_WORKER_MAX_CLAIMS` | branch | Unsettled claims a worker may hold. |
| `KSWARM_WORKER_POLL_SECONDS` | daemons | Poll interval. |
| `KSWARM_CLAIM_COOLDOWN_SECONDS`, `KSWARM_EXECUTE_DEADLINE_MARGIN_SECONDS` | branch | Claim discipline (PR #7). |
| `VERIFIER_REEXECUTE`, `VERIFIER_HASH_ONLY` | verifier | Re-execution is the default; hash-only must be chosen by name. |
| `KSWARM_CHALLENGE_ON_MISMATCH` | verifier | Challenge a receipt whose re-execution differs. |
| `KSWARM_BONSOL_AGGREGATE_COMMAND` | aggregator | Bonsol hook. Unset, the runner will not claim an aggregate job at all, because the receipt could never be proven. |
| `KSWARM_BONSOL_HOOK_URL` | aggregator | The proving service `python -m aggregator_runner.bonsol_http_hook` calls. |
| `KSWARM_ALLOW_UNBOUND_AGGREGATE` | aggregator | `1` submits an aggregate receipt with no proof. Local stacks only; refused on devnet and mainnet. |
| `KSWARM_AGGREGATE_IMAGE_ID` | cli, bootstrap | Reducer image id for aggregate jobs and the aggregator's registration. |
| `KSWARM_ZKVM_HOST` | branch, verifier | The prover. Defaults to the binary the image carries; set it empty to turn the proof path off. |
| `KSWARM_ZKVM_IMAGE_ID`, `KSWARM_ZKVM_IMAGE_ID_FILE` | verifier | The guest a receipt must name. The file defaults to the id the image was built with. |
| `KSWARM_ZKVM_REQUIRE_RECEIPT` | verifier | `1` (the compose default) refuses to attest to a branch that carries no receipt. |
| `KSWARM_ZKVM_TIMEOUT_SECONDS` | branch, verifier | Prove and verify timeout; 1800 by default. Proving costs tens of seconds of CPU per branch, so the execution window has to be longer than it. |
| `KSWARM_PROVER_CPUS`, `KSWARM_PROVER_MEMORY` | branch, verifier | Resource limits for the two services that prove; 8 CPUs and 12 GB by default. The RISC Zero prover is not a 1 GB workload: at the old shared limit it was killed outright (`zkvm host prove exited -9`). A deployment with the proof path off can lower them. |

## Compose profiles

```bash
# local: validator, stand-in mint, generated wallets, bootstrap, three daemons
LLM_BASE_URL=http://<host>:11434/v1 LLM_MODEL_NAME=llama3.2:3b-instruct-q5_K_M \
docker compose -f docker-compose.swarm.yml --profile local up -d

# devnet: external RPC and mint, your wallet files, bootstrap registers and stakes
KSWARM_RPC_URL=https://api.devnet.solana.com KSWARM_PAYMENT_MINT=<mint> \
KSWARM_WALLETS_DIR=./secrets/wallets LLM_BASE_URL=... LLM_MODEL_NAME=... \
docker compose -f docker-compose.swarm.yml --profile devnet up -d
```

`local` needs the program artifact at `KSWARM_PROGRAM_SO`
(`./solana/target/deploy/kswarm_protocol.so` by default):

```bash
cargo build-sbf --tools-version v1.51 --manifest-path solana/programs/kswarm_protocol/Cargo.toml -- --locked
```

The validator (`anzaxyz/agave`, digest-pinned) loads it at
`KSWARM_PROGRAM_ID` and keeps its ledger on tmpfs. `docker compose down`
discards the chain; the `cli-config` volume keeps wallets and profiles, and the
bootstrap converges again on the next `up` (new mint, new initialization).

The `bootstrap` service runs `kswarm swarm bootstrap` once. It creates
`admin`, `customer`, `worker-a`, `verifier`, and `aggregator`, airdrops SOL,
creates the stand-in mint (classic SPL Token, 6 decimals), initializes the
protocol with the KAI floors (50,000 / 250,000 / 1,000,000; verifier 100,000),
funds every non-admin wallet with 300,000 KAI, registers the three workers, and
stakes each at its floor. Every step checks the chain first, so reruns are
no-ops. The daemons start only after it exits 0.

`devnet` expects `KSWARM_WALLETS_DIR` to hold `admin.json`, `customer.json`,
`worker-a.json`, `verifier.json`, and `aggregator.json`, funded with SOL and
with KAI for the stakes. The bootstrap does not create wallets, mints, or
airdrops there; it initializes the protocol if needed, registers, and stakes.
Mainnet is refused by `swarm bootstrap`; registration and staking with real KAI
are explicit `worker register` and `worker stake` commands.

Volumes:

| Volume | Mounted in | Purpose |
| --- | --- | --- |
| `cli-config` | bootstrap, cli (rw); daemons (ro) | `~/.config/kswarm`: wallets, cluster profiles, `worker.toml`. |
| `predict-runs` | cli, aggregator | Run manifests from `predict open`; the aggregator reads them. |
| `ipfs-data` | ipfs | Kubo repository. |

Every daemon has a read-only root filesystem (HOME included; the daemons write
nothing there), tmpfs `/tmp`, all capabilities dropped, `no-new-privileges`, CPU
and memory limits, and log rotation. The local profile shares one throwaway config volume between the
daemons; on devnet each wallet file is the operator's.

Challenges: any staked verifier may attest, but `challenge_job` accepts only the
verifier the customer (or the protocol admin) assigned to the job (PR-3). The
`verifier-worker` attests every completed branch; when its re-execution differs and
it is not assigned, it records the mismatching attestation and logs
`challenge rejected`. To let it slash, assign it before the attestation lands:
`kswarm assign-verifier <job> --verifier verifier` signed by the customer.

Settlement: the Python stack has no settle daemon yet. Branch jobs stay
`completed` until `kswarm settle <job>` runs after the challenge window
(the Node watcher in `docker-compose.protocol.yml` does the same automatically).
The aggregator therefore runs with `--allow-completed-branches` and combines
attested branches; set `KSWARM_AGGREGATOR_ARGS=""` to require settled ones.

## Operating the stack

```bash
compose="docker compose -f docker-compose.swarm.yml"
$compose --profile local ps
$compose --profile local logs -f branch-worker verifier-worker aggregator-runner

# Operator commands run in the cli image inside the compose network.
$compose run --rm cli --json protocol show
$compose run --rm cli --json predict open --question "..." --branches 2 \
  --reward-per-branch 1KAI --aggregator-reward 1KAI --challenge-window 600
$compose run --rm cli --json predict status <parent-run>
$compose run --rm cli --json predict report <parent-run>
$compose run --rm cli --json settle <branch-job>
$compose --profile local down -v      # drop the chain, wallets, and artifacts
```

`scripts/swarm-smoke.sh` runs that whole path end to end (build, up, bootstrap,
one two-branch prediction, report, settle branches, bind and aggregate, down) and
writes a log under `runtime/swarm-smoke/`. `worker/tests/test_swarm_smoke.py` wraps it
under the `integration` marker (`KSWARM_SWARM_SMOKE=1`).

Branch receipts are proven and verified inside the containers on every run, because the
two worker images carry the prover. The aggregate proof is not, by default: this stack
has no Bonsol node, so the aggregator is told explicitly that an unproven receipt is
acceptable and the aggregate job stays unsettled, which the script's final JSON reports
as `"aggregate_proven": false`.

`KSWARM_SMOKE_BONSOL=1` runs the proven path instead. It brings up
`docker-compose.bonsol.yml`, points the swarm stack at that validator, starts
`protocol/scripts/bonsol-hook-server.py` on the host with the Bonsol CLI wrappers and
the client keypair, and then attests, waits out the challenge window and settles the
aggregate against its marker. It reports the marker PDA and the settle signature. All
its builds go through `scripts/heavy-build-lock.sh`, a host-wide lock, because a build
host is often shared and a per-caller "one build at a time" rule cannot see the other
callers.

## Registry and releases

The container release workflow builds all four targets on every pull
request and, on a `v*` tag, pushes them to the container registry as

```
<registry>/<owner>/<repo>/<target>:<tag>
<registry>/<owner>/<repo>/<target>:<sha12>
```

`<owner>/<repo>` comes from `GITHUB_REPOSITORY`, lowercased, so renaming the
repository moves the images with it; only `<registry>` is set in the workflow.
The digests are recorded in the job summary. The registry token comes
from the `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` Actions secrets; the
workflow prints no secret.

Run a tagged release instead of building locally:

```bash
docker login <registry>
KSWARM_IMAGE_PREFIX=<registry>/<owner>/<repo> \
KSWARM_IMAGE_TAG=v0.1.0-devnet \
docker compose -f docker-compose.swarm.yml --profile devnet pull
```

For a digest-pinned deployment, resolve each image once with
`docker buildx imagetools inspect <ref>` and put `<ref>@sha256:...` in an
override file; the job summary of the release run lists the same digests.

## Node control plane

`docker-compose.protocol.yml` keeps `protocol-api` (artifact gateway) and
`protocol-watcher` (settlement and stale-slash loop). Initialization moved to
the `protocol-bootstrap` service, which runs the `cli` image with
`docker/swarm/protocol-bootstrap.sh`: it waits for the deployer's `deployed`
marker, runs `protocol initialize` as `admin.json`, and writes `protocol.json`
and `ready` with `protocol runtime-config`. That service runs as
`PROTOCOL_RUNTIME_UID:PROTOCOL_RUNTIME_GID` (root by default) because it writes
into the deployer's bind-mounted `runtime/protocol`; set both to the uid that
owns that directory on your host.
