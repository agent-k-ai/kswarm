# kswarm Operator Docs

!!! note "This page describes the whole kswarm source tree"
    Some of what it names is not published in this repository: the local
    development and Bonsol evaluation compose stacks, their `make` targets,
    the flagship demo scripts, `scripts/bootstrap-handson.sh`, and the
    Polymarket adapter. `docker-compose.swarm.yml` and
    `scripts/swarm-smoke.sh` are here and run the swarm end to end on a
    local validator. See "Not published here yet" in the README.

This site collects the protocol material needed to run the local kswarm hands-on stack, inspect on-chain state, and validate the Bonsol aggregate settlement path.

## Current Status

- Phase 0d adds a Python CLI at `cli/` with wrappers for every `kswarm_protocol` instruction.
- Local cluster is the default target at `http://127.0.0.1:38899`.
- Devnet is available through the same CLI surface with `--cluster devnet`; program deployment and mint funding are still operator-controlled.
- The payment and stake token is KAI (classic SPL Token, 6 decimals). Devnet and local use a stand-in mint with the same layout. A `mainnet` profile exists, but it has no program id until the mainnet program is deployed.
- The local hands-on stack starts through `./scripts/bootstrap-handson.sh`.

## Fast Paths

- [Operator Quickstart](operator-quickstart.md): copy-paste walkthroughs for branch happy path, slash path, and aggregate Bonsol flow.
- [CLI Reference](cli-reference.md): command inventory and instruction coverage map.
- [Architecture Overview](architecture-overview.md): parent, branch, aggregate, verifier, and marker model.
- [KAI Payment Token](kai-payment-token.md): mint facts, base-unit math, stake floors, and the per-cluster mint policy.
- [Testing Strategy](testing-strategy.md): Tier 1, Tier 2, and Tier 3 validation scope.

## Local Commands

```bash
make cli-test
make docs-serve
make handson-up
make handson-down
```
