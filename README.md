# kswarm

A prediction swarm. You give it seed material and a question about the future;
it expands that into many simulated branches, runs each one with a language
model on a different operator's machine, and combines the results into one
forecast with a range. Solana holds the money and the rules, IPFS holds the
artifacts, and KAI is the payment and stake token.

kswarm is built on the [MiroFish](https://github.com/666ghj/MiroFish)
multi-agent simulation engine (AGPL-3.0). The engine is not re-published here;
it is a pinned submodule at `engine/`. See [NOTICE](NOTICE).

> **Status: pre-release.** Nothing is deployed to mainnet, the Solana program is
> **not audited**, and the trust layer is honestly incomplete — read
> [docs/proof-layer-status.md](docs/proof-layer-status.md) before you believe
> anything about what is proven. Do not put real funds behind this.

## What is real today

The chain plumbing runs end to end on a local validator: escrow, worker stake,
claim rights, receipts, verifier re-execution and attestation, aggregate
settlement through a Bonsol callback, cancellation, and slashing.

The trust layer is narrower than it sounds, so here is the split. Two things
carry a zero-knowledge proof: the aggregate reduction, which the Solana program
will not pay without, and the branch canonicalization receipt, which proves the
document a worker published is exactly the one its on-chain receipt refers to.
Both guests recompute rather than restate the values they are given.

**The language model step is not proven, and no 2026 technology proves it.** The
largest language model anyone can prove with released code is GPT-2 small at 124
million parameters, and the fastest published figure for it is at a 16-token
sequence; the branch model is roughly 25 times larger, and every prover that can
reach even that size is licensed for evaluation only and tied to its vendor's own
proving network. That step is secured **economically**: a second staked operator
re-runs the branch with the identical model, seed and configuration, and a worker
whose result differs is challenged and slashed. It is not a cryptographic
guarantee, and it rests on determinism measured on one model and one prompt
family. Branch narrative prose is hash-committed but is not checked for
correctness. All of this is written down, with what would have to change, in
[docs/proof-layer-status.md](docs/proof-layer-status.md).

There is no measured forecasting edge. A sealed, pre-registered test on one class
of events was null, and that result is published rather than buried. Anyone
claiming a kswarm forecasting edge is not speaking for this project.

## Quick start

Start with the **[Community Guide](docs/community-guide.md)**. It covers using
the swarm as a customer, running a branch worker, running a verifier, running an
aggregator, and what your KAI is actually at risk of.

The short version, on a local validator:

```bash
uv venv .venv
uv pip install -e cli --python .venv/bin/python
kswarm --help
kswarm wallet create customer --airdrop 10
kswarm predict open --question "..." --output-kind scalar --branches 16
```

For the containerised stack (four non-root images, one compose file), see
[docs/containers.md](docs/containers.md) and
[docs/operator-quickstart.md](docs/operator-quickstart.md).

## Layout

| Path | What |
|---|---|
| `worker/` | branch worker, verifier worker, aggregator runner, shared config and IPFS client |
| `cli/` | the `kswarm` operator CLI: wallets, tokens, `predict`, settle, inspect |
| `protocol/` | Node control plane (artifact gateway, settlement watcher), the branch canonicalization zkVM guest and host, the Bonsol aggregate reducer and the callback harness |
| `docker/swarm/` | the four runtime images |
| `docker/protocol-node/` | the Node and proving toolchain image; building it needs the program repository checked out at `solana/` (see below) |
| `docker/ipfs/` | the Kubo bootstrap and peer entrypoints those images use |
| `backend/app/` | the four engine modules the daemons import; see [NOTICE](NOTICE) |
| `docs/` | operator and community documentation |

The Solana program lives in its own repository,
[kswarm-protocol](https://github.com/agent-k-ai/kswarm-protocol), under
Apache-2.0, so that its audit scope stays small.

### Not published here yet

This is a filtered export, not the whole source tree. The documentation
describes some pieces that are not in this repository: the local development and
Bonsol evaluation compose stacks and their `make` targets, the flagship demo
scripts, `scripts/bootstrap-handson.sh`, and the Polymarket adapter.
`docker-compose.swarm.yml` and `scripts/swarm-smoke.sh` are the parts of the
hands-on stack that are here, and they are enough to run the swarm end to end on
a local validator. Every page that describes the wider tree carries a note
saying so.

`docker/protocol-node/Dockerfile` copies `solana/`, which is the program and
lives in the other repository. Build it with the program checked out beside this
tree, the way the CLI job in `.github/workflows/ci.yml` places `lib.rs`:

```bash
git clone https://github.com/agent-k-ai/kswarm-protocol .kswarm-protocol
cp -r .kswarm-protocol/solana solana
docker build -f docker/protocol-node/Dockerfile -t kswarm/protocol-node .
```

## Running the tests

```bash
cd cli    && uv sync --locked --group dev && uv run --locked pytest -q -m "not integration"
cd worker && uv sync --group dev && PYTHONPATH="${PWD}/../backend" uv run pytest -q -m "not integration"
cd protocol && npm install --no-package-lock && node --test "test/**/*.test.mjs"
```

The `integration` tests additionally need `solana-test-validator`, a Kubo IPFS
node, a built program artifact, and an OpenAI-compatible LLM endpoint.

## Security

The program is not audited and the trust layer is incomplete. Report a
vulnerability privately: see [SECURITY.md](SECURITY.md).

## Contributing

Development happens on a private tree and is exported here per release, so pull
requests are triaged and re-applied rather than merged directly. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

AGPL-3.0, because this work depends on the AGPL-3.0 MiroFish engine. See
[LICENSE](LICENSE) and [NOTICE](NOTICE).
