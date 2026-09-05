# Development Infrastructure

!!! note "This page describes the whole kswarm source tree"
    Some of what it names is not published in this repository: the local
    development and Bonsol evaluation compose stacks, their `make` targets,
    the flagship demo scripts, `scripts/bootstrap-handson.sh`, and the
    Polymarket adapter. `docker-compose.swarm.yml` and
    `scripts/swarm-smoke.sh` are here and run the swarm end to end on a
    local validator. See "Not published here yet" in the README.

Phase 0f uses `docker-compose.dev.yml` as the single local-development entrypoint for IPFS, Bonsol, Solana tooling, and hands-on operator support.

## Compose Strategy

`docker-compose.dev.yml` imports the Bonsol services from `docker-compose.bonsol.yml` with Compose `extends`. The original Bonsol and protocol compose files are left intact for Phase 0c regression tests. The dev file only adds overrides for profiles, host port defaults, a shared `kswarm-dev` network, health ordering, and dev-only support services. It does not restate the validator's flags; see [How The Validators Load The Program](#how-the-validators-load-the-program).

**Docker Compose 2.24 or newer is required.** The dev file replaces the extended service's published ports with the `!override` tag, which older plugins ignore: they merge the two lists instead, the validator gets each host port bound twice, and the stack fails to start with `bind: address already in use` on a port nothing is listening on. `docker compose version` reports the plugin actually in use, and a user-level plugin in `~/.docker/cli-plugins` shadows the system one.

Use these targets:

```bash
make dev-up
make dev-status
make handson-up
make tier3-up
make dev-down
make dev-down-clean
```

`make dev-up` runs:

```bash
docker compose -f docker-compose.dev.yml --profile core up -d --wait --wait-timeout 1800
```

## Services

| Service | Profile | Purpose |
| --- | --- | --- |
| `ipfs-kubo` | `core`, `tier3`, `inspect` | Real Kubo daemon for branch inputs, worker outputs, verifier evidence, and aggregate artifacts. |
| `bonsol-builder` | `core`, `tier3`, `inspect` | Builds Bonsol verifier artifacts, callback harness, and the kswarm reducer manifest. |
| `bonsol-validator` | `core`, `tier3`, `inspect` | `solana-test-validator` with the Bonsol programs loaded and the kswarm program loaded upgradeably. Its flags live in `docker-compose.bonsol.yml`; `docker-compose.dev.yml` extends that service rather than restating them. |
| `bonsol-image-server` | `core`, `tier3`, `inspect` | Local Bonsol zk-program image server. |
| `bonsol-node` | `core`, `tier3`, `inspect` | Real Bonsol prover/verifier node connected to the local validator. |
| `bonsol-callback-smoke-test` | `tier3` | Production-style Bonsol callback smoke harness. |
| `solana-toolchain` | `tools`, `inspect` | Containerized `solana` and `solana-test-validator` binaries. |
| `python-toolchain` | `tools`, `inspect` | Python 3.12 fallback for the CLI when the host lacks `uv` or a compatible Python. |
| `dev-inspector` | `inspect` | Small status page with default local service links. |

## Ports

All host ports can be remapped with environment variables.

| Service | Container port | Host default | Env var |
| --- | ---: | ---: | --- |
| IPFS API | `5001` | `4501` | `KSWARM_IPFS_API_PORT` |
| IPFS gateway | `8080` | `48080` | `KSWARM_IPFS_GATEWAY_PORT` |
| IPFS swarm TCP/UDP | `4001` | `4401` | `KSWARM_IPFS_SWARM_PORT` |
| Bonsol validator RPC | `8899` | `38899` | `BONSOL_VALIDATOR_RPC_PORT` |
| Bonsol validator WS | `8900` | `38900` | `BONSOL_VALIDATOR_WS_PORT` |
| Bonsol image server | `8080` | `38080` | `BONSOL_IMAGE_SERVER_PORT` |
| Inspector | `80` | `39080` | `KSWARM_INSPECT_PORT` |

The IPFS defaults avoid the common host `5001` collision while still exposing Kubo's native API inside the container. `runtime/ipfs/api` contains the host URL that CLI and worker processes should use.

## Runtime Files

Runtime state is local and gitignored.

| Path | Producer | Notes |
| --- | --- | --- |
| `runtime/ipfs/api` | `ipfs-kubo` init script | Host API URL, for example `http://127.0.0.1:4501`. |
| `runtime/ipfs/api.multiaddr` | `ipfs-kubo` init script | Host API multiaddr. |
| `runtime/bonsol/` | `bonsol-builder` | Bonsol programs, reducer manifest, and node/client keypairs. |
| `runtime/bonsol/runtime-keypair.json` | `scripts/bootstrap-handson.sh` | Host-readable copy of the Bonsol client keypair for operator workflows. It is also the validator's program upgrade authority, so the bootstrap installs it as the `admin` wallet. |
| `~/.config/kswarm/wallets/admin.json` | `scripts/install-admin-keypair.sh` | The program upgrade authority, installed as the admin wallet, mode `0600`. Never overwritten: an admin wallet holding a different key stops the bootstrap. |
| `runtime/handson.env` | `scripts/bootstrap-handson.sh` | Shell exports for CLI and worker commands. |
| `~/.config/kswarm/handson-state.json` | `scripts/bootstrap-handson.sh` | Operator state with wallets, ports, RPC, IPFS, and Bonsol paths. |
| `runtime/ipfs/swarm.key` | `ipfs-bootstrap` (`docker-compose.protocol.yml`) | IPFS private-network key, generated on first start with mode `0600`. Copy it to every remote peer. |
| `runtime/ipfs/bootstrap.addr` | `ipfs-bootstrap` | Bootstrap multiaddr for peers. |
| `runtime/protocol/*.json` | `protocol-deployer` | Runtime wallets (`admin`, `customer`, `verifier`, `worker`, `watcher`), random per deployment, mode `0600`. |
| `runtime/protocol/protocol.json` | `protocol-deployer` | Public runtime config: program id, mint, token program, decimals, stake floors. |
| `runtime/keys/` | operator (optional) | Default `PROTOCOL_KEYS_HOST_DIR`, mounted read-only at `/keys` in the deployer. Holds deploy keys for real clusters only; empty on localnet. |

## How The Validators Load The Program

Every local validator loads the kswarm program with `--upgradeable-program <id> <so> <authority>`, never with `--bpf-program`. `initialize_protocol` reads the protocol admin out of the program's `ProgramData` account (`validate_upgrade_authority`), and a `--bpf-program` account has no `ProgramData` and therefore no upgrade authority, so the protocol can never be initialized on such a validator. Between PR #10 and this fix `docker-compose.dev.yml` still used `--bpf-program`, which is what `make handson-up` runs, so the operator entry point failed with `AdminNotUpgradeAuthority`.

That also decides which key the admin is. The authority is fixed when the validator starts, so the admin wallet has to be that key rather than a fresh one:

| Stack | Upgrade authority | How the admin wallet becomes it |
| --- | --- | --- |
| `docker-compose.dev.yml` (`make handson-up`, `scripts/demo-*.sh`) | Bonsol client keypair (`runtime/bonsol/client-keypair.json`) | `scripts/install-admin-keypair.sh` installs the host-readable copy as `admin` before any wallet is created. |
| `docker-compose.swarm.yml` | the `keygen` one-shot's `admin` wallet | The wallet exists before the validator starts; the validator reads its pubkey. |
| `docker-compose.protocol.yml` | `runtime/protocol/admin.json` | The `protocol-keygen` one-shot writes the runtime wallets before the validator starts; `protocol-deployer` reuses them. |

`scripts/ci/check-validator-program-loads.sh` enforces this over every compose file and every file that starts a validator, and `scripts/tests/run.sh` runs it.

## Program Id And Key Material

The protocol program id is `ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM` (`declare_id!` in `solana/programs/kswarm_protocol/src/lib.rs`). Every consumer reads it from one place per stack: `kswarm_cli.constants.KSWARM_PROGRAM_ID`, `protocol/src/protocol.mjs` `PROGRAM_ID`, `kswarm_protocol::ID` in the Rust tests, and the `--upgradeable-program` entries in the compose validators. The id was rotated on 2026-09-03 because the previous keypair had been tracked in git; that keypair and the `phase0_callback_probe` keypair were deleted without a history rewrite, so treat both old ids as burned.

The program keypair is held in the project's secret store, recorded in `SECURITY.md`, and never in this repository. Only a deploy or upgrade on a real cluster needs it; localnet validators load the program at genesis. To generate a replacement:

```bash
umask 077
solana-keygen new --no-bip39-passphrase -o ~/secure/kswarm_protocol-keypair.json
solana-keygen pubkey ~/secure/kswarm_protocol-keypair.json   # becomes the new declare_id!
```

Rules that CI enforces with `scripts/check-no-secrets.sh` (run it locally before committing; `scripts/check-no-secrets.sh --self-test` proves the scanner works):

- no tracked `*-keypair.json`, `*.keypair.json`, `swarm.key`, `*.key`, `*.pem`, `*.log`, `runtime/**`, `solana/deploy/**`, or `.env*` other than `.env.example`
- no tracked blob that contains a 64-byte secret-key array, a PEM private-key block, or an IPFS swarm key header

### One heavy build at a time

Guest builds, image builds and `cargo build-sbf` each saturate a machine, and more than
one agent or operator can be driving the same build host. A per-caller "one at a time"
rule cannot see the other callers, so the exclusion lives outside all of them:
`scripts/heavy-build-lock.sh` takes a host-wide `flock` and runs the command under it.

```bash
scripts/heavy-build-lock.sh docker build -f docker/swarm/Dockerfile --target cli .
```

`protocol/scripts/build-aggregate-reducer.sh`, `scripts/swarm-smoke.sh` and
`scripts/bootstrap-handson.sh` already wrap their own heavy steps, so an operator using
those does not have to think about it. `KSWARM_HEAVY_BUILD_LOCK` names the lock file and
`KSWARM_HEAVY_BUILD_LOCK_WAIT` the seconds to wait for it. On a machine where the lock
directory does not exist -- a laptop, a single-job CI runner -- the command runs
unlocked and says so.

**Do not nest the lock inside itself.** Because those scripts already take the lock,
wrapping one of them in `scripts/heavy-build-lock.sh` again -- or in a bare `flock` on the
same file, which is the usual rule for heavy work on a shared build host -- deadlocks: the
outer `flock` holds the file while the inner one waits `KSWARM_HEAVY_BUILD_LOCK_WAIT`
seconds, two hours by default, for a lock its own parent is holding. Nothing reports it;
the command simply sits there. When an outer exclusion is wanted anyway, because it covers
more than the inner one does, point the inner lock at a different file first:

```bash
export KSWARM_HEAVY_BUILD_LOCK="${KSWARM_HEAVY_BUILD_LOCK%.lock}-handson-inner.lock"
flock -w 7200 "${OUTER_LOCK}" make handson-up
```

Containers run as uid 1000 (`node` in `docker/protocol-node`, `builder` in `docker/program-builder`, `app` in the root `Dockerfile`, `ipfs` for Kubo). Bind-mounted runtime directories therefore must belong to the host user with uid 1000: run `install -d -m 700 runtime/protocol runtime/ipfs` before the first `docker compose up`. The only root service is `protocol-program-builder`, because `solana-verify` drives the host Docker socket; the compose file says so next to the override.

Base images are pinned by digest with the tag in a comment. To move a pin, resolve the new digest and update both:

```bash
docker buildx imagetools inspect node:22-bookworm | grep -m1 Digest
```

Toolchains inside the images are pinned too, and pinned in one place: `protocol/risc0-toolchain.env` declares every `rzup` component by name and version (rust 1.88.0, cpp 2024.1.5, cargo-risczero 3.0.3, r0vm 3.0.3, the risc0 3.0.3 line the Bonsol path uses), and `docker/protocol-node`, `docker/swarm` and `docker/bonsol-eval` install exactly those through `scripts/install-risc0-toolchain.sh`. An unpinned `rzup install` would take the latest of each component; four versions repeated in three Dockerfiles would drift the moment one was edited.

## IPFS Configuration

Kubo is pinned to `ipfs/kubo:v0.40.1`. The container init script configures:

```text
Addresses.API = /ip4/0.0.0.0/tcp/5001
Addresses.Gateway = /ip4/0.0.0.0/tcp/8080
API.HTTPHeaders.Access-Control-Allow-Origin = ["*"]
```

The CORS setting is for local development only. Do not reuse it for a shared or internet-facing Kubo node.

Smoke test:

```bash
source runtime/handson.env
printf 'hello\n' >/tmp/kswarm-ipfs-smoke.txt
CID="$(curl -fsS -X POST -F file=@/tmp/kswarm-ipfs-smoke.txt \
  "${KSWARM_IPFS_API_URL}/api/v0/add?pin=true&cid-version=1" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["Hash"])')"
curl -fsS -X POST "${KSWARM_IPFS_API_URL}/api/v0/cat?arg=${CID}"
curl -fsS "http://127.0.0.1:${KSWARM_IPFS_GATEWAY_PORT}/ipfs/${CID}"
```

## Tooling

Add Docker-backed wrappers to `PATH` for zero-install shells:

```bash
export PATH="$PWD/scripts/bin:$PATH"
bonsol --help
solana --version
solana-test-validator --version
```

To install native Bonsol instead:

```bash
scripts/install-bonsol-cli.sh
```

The native installer builds the pinned Bonsol commit used by `docker/bonsol-eval/Dockerfile` and installs `bonsol` into `${HOME}/.local/bin` unless `BONSOL_INSTALL_DIR` is set.

## Troubleshooting

Port collision:

```bash
KSWARM_IPFS_API_PORT=14501 KSWARM_IPFS_GATEWAY_PORT=18088 make dev-up
source runtime/handson.env
```

Check health:

```bash
make dev-status
docker compose -f docker-compose.dev.yml --profile core ps
docker logs kswarm-ipfs-kubo
docker compose -f docker-compose.bonsol.yml logs bonsol-node
```

Reset local state:

```bash
make dev-down-clean
```

This removes the dev compose volumes, `runtime/ipfs`, `runtime/bonsol`, `runtime/handson.env`, and `~/.config/kswarm`.

Inspect IPFS swarm addresses:

```bash
docker compose -f docker-compose.dev.yml exec -T ipfs-kubo ipfs id
docker compose -f docker-compose.dev.yml exec -T ipfs-kubo ipfs swarm peers
```

Use the inspector profile:

```bash
docker compose -f docker-compose.dev.yml --profile inspect up -d --wait
open http://127.0.0.1:${KSWARM_INSPECT_PORT:-39080}
```
