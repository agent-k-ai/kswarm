# Contributing to kswarm

## How this repository works

Development happens on a private tree. Each release is exported here as one
commit by the tooling described in `PROVENANCE.md`, so this repository has no
day-to-day history and pull requests cannot be merged into it directly.

Contributions are still welcome. A pull request here is read, and an accepted
change is applied to the source tree and appears in the next export with
attribution. An issue with a clear reproduction is often more useful than a
patch.

Changes to the engine itself belong upstream, at
[666ghj/MiroFish](https://github.com/666ghj/MiroFish).

## Getting set up

```bash
git submodule update --init engine    # only needed for OASIS simulation branches

cd cli    && uv sync --locked --group dev
cd worker && uv sync --group dev
cd protocol && npm install --no-package-lock
```

## Before you open something

Run what the CI runs:

```bash
cd cli      && uv run --locked pytest -q -m "not integration"
cd worker   && PYTHONPATH="${PWD}/../backend" uv run pytest -q -m "not integration"
cd protocol && node --check src/*.mjs scripts/*.mjs && node --test "test/**/*.test.mjs"
scripts/check-no-secrets.sh --self-test && scripts/check-no-secrets.sh
```

The `integration` tests need `solana-test-validator`, a Kubo IPFS node, a built
program artifact, and an OpenAI-compatible LLM endpoint. `scripts/swarm-smoke.sh`
runs the whole containerised stack end to end and is the best single check that
a change did not break the lifecycle.

## What makes a change easy to accept

- **A test that fails before and passes after.**
- **No fabricated values on a path that gets hash-committed.** If a value cannot
  be computed, the job should fail, not guess. Several past bugs were exactly
  this.
- **Nothing that widens what a proof appears to claim** without the proof
  actually claiming it. If a change touches the proof layer, update
  `docs/proof-layer-status.md` in the same commit.
- **No key material, no internal hostnames, no absolute paths from your machine**
  in anything you commit. `scripts/check-no-secrets.sh` catches the first.
- **Plain commit messages** that say what changed and why.

## Security

Do not report a vulnerability in a pull request or a public issue. See
[SECURITY.md](SECURITY.md).

## Licence

Contributions to this repository are accepted under AGPL-3.0, the licence of this
repository.
