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

`docker-compose.dev.yml` imports the Bonsol services from `docker-compose.bonsol.yml` with Compose `extends`. The original Bonsol and protocol compose files are left intact for Phase 0c regression tests. The dev file only adds overrides for profiles, host port defaults, a shared `kswarm-dev` network, health ordering, and dev-only support services.

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
| `bonsol-validator` | `core`, `tier3`, `inspect` | `solana-test-validator` with the Bonsol and kswarm programs loaded. |
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
| `runtime/bonsol/runtime-keypair.json` | `scripts/bootstrap-handson.sh` | Host-readable copy of the Bonsol client keypair for operator workflows. |
| `runtime/handson.env` | `scripts/bootstrap-handson.sh` | Shell exports for CLI and worker commands. |
| `~/.config/kswarm/handson-state.json` | `scripts/bootstrap-handson.sh` | Operator state with wallets, ports, RPC, IPFS, and Bonsol paths. |
| `runtime/ipfs/swarm.key` | `ipfs-bootstrap` (`docker-compose.protocol.yml`) | IPFS private-network key, generated on first start with mode `0600`. Copy it to every remote peer. |
| `runtime/ipfs/bootstrap.addr` | `ipfs-bootstrap` | Bootstrap multiaddr for peers. |
| `runtime/protocol/*.json` | `protocol-deployer` | Runtime wallets (`admin`, `customer`, `verifier`, `worker`, `watcher`), random per deployment, mode `0600`. |
| `runtime/protocol/protocol.json` | `protocol-deployer` | Public runtime config: program id, mint, token program, decimals, stake floors. |
| `runtime/keys/` | operator (optional) | Default `PROTOCOL_KEYS_HOST_DIR`, mounted read-only at `/keys` in the deployer. Holds deploy keys for real clusters only; empty on localnet. |

## Program Id And Key Material

The protocol program id is `ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM` (`declare_id!` in `solana/programs/kswarm_protocol/src/lib.rs`). Every consumer reads it from one place per stack: `kswarm_cli.constants.KSWARM_PROGRAM_ID`, `protocol/src/protocol.mjs` `PROGRAM_ID`, `kswarm_protocol::ID` in the Rust tests, and the `--bpf-program` entries in the compose validators. The id was rotated on 2026-09-03 because the previous keypair had been tracked in git; that keypair and the `phase0_callback_probe` keypair were deleted without a history rewrite, so treat both old ids as burned.

The program keypair is held in the project's secret store, recorded in `SECURITY.md`, and never in this repository. Only a deploy or upgrade on a real cluster needs it; localnet validators load the program at genesis with `--bpf-program`. To generate a replacement:

```bash
umask 077
solana-keygen new --no-bip39-passphrase -o ~/secure/kswarm_protocol-keypair.json
solana-keygen pubkey ~/secure/kswarm_protocol-keypair.json   # becomes the new declare_id!
```

Rules that CI enforces with `scripts/check-no-secrets.sh` (run it locally before committing; `scripts/check-no-secrets.sh --self-test` proves the scanner works):

- no tracked `*-keypair.json`, `*.keypair.json`, `swarm.key`, `*.key`, `*.pem`, `*.log`, `runtime/**`, `solana/deploy/**`, or `.env*` other than `.env.example`
- no tracked blob that contains a 64-byte secret-key array, a PEM private-key block, or an IPFS swarm key header

Containers run as uid 1000 (`node` in `docker/protocol-node`, `builder` in `docker/program-builder`, `app` in the root `Dockerfile`, `ipfs` for Kubo). Bind-mounted runtime directories therefore must belong to the host user with uid 1000: run `install -d -m 700 runtime/protocol runtime/ipfs` before the first `docker compose up`. The only root service is `protocol-program-builder`, because `solana-verify` drives the host Docker socket; the compose file says so next to the override.

Base images are pinned by digest with the tag in a comment. To move a pin, resolve the new digest and update both:

```bash
docker buildx imagetools inspect node:22-bookworm | grep -m1 Digest
```

Toolchains inside the images are pinned too: `docker/protocol-node` installs every `rzup` component by name and version (rust 1.88.0, cpp 2024.1.5, cargo-risczero 3.0.3, r0vm 3.0.3, the risc0 3.0.3 line the Bonsol path uses) and `ezkl==23.0.5`. An unpinned `rzup install` would take the latest of each component.

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
docker logs kswarm-bonsol-node
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
