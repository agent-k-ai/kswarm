# Testing Strategy

kswarm uses three test tiers. The tiers differ by cadence and runtime cost, not by truthfulness.

## Tier 1: Anchor ProgramTest

Run Tier 1 on every pull request.

```bash
cargo build-sbf --tools-version v1.51 --manifest-path solana/programs/kswarm_protocol/Cargo.toml -- --locked
CARGO_TARGET_DIR=/tmp/phase0c-cargo cargo test --package anchor_integration --features tier1 -- --test-threads=1
```

Tier 1 uses `solana_program_test::ProgramTest` and `BanksClient`. The harness loads the compiled program (`kswarm_protocol.so`) as a genuine upgradeable-loader program whose upgrade authority is the test payer, so the artifact under test is the one that gets deployed and `initialize_protocol`'s upgrade-authority check runs unchanged. The artifact is looked up at `KSWARM_PROGRAM_SO`, then `SBF_OUT_DIR`/`BPF_OUT_DIR`, then `CARGO_TARGET_DIR/deploy`, then `solana/target/deploy`; build it first. Tier 1 covers Q1 verifier attestation, full slash payment flows, stale-slash accounting, R3 verifier reassignment and both cancellation paths (registry exhaustion and marker timeout), aggregate settlement guards, double settlement, assigned-verifier challenge authorization, `open_job` class/capability pairing, `cancel_open_job`, `withdraw_unlocked_stake`, claim-window expiry, and initialization authority. It does not load Bonsol.

## Tier 2: Real Bonsol Callback Flow

Run Tier 2 on a Docker-capable self-hosted runner for pull requests labeled `tier2-bonsol`, for `main`, and for manual dispatch.

```bash
docker compose -f docker-compose.bonsol.yml build bonsol-builder
CARGO_TARGET_DIR=/tmp/phase0c-cargo cargo test --package anchor_integration --features tier2-bonsol -- --test-threads=1 --nocapture
```

Tier 2 loads the real pinned Bonsol verifier program, the real callback example program, and the real `kswarm_protocol` program into a Compose-managed `solana-test-validator`. The harness starts the real `bonsol-node` Docker container, issues real Bonsol deploy/execute requests through the existing callback smoke harness, waits for real StatusV1 transactions, and verifies kswarm marker PDA and settlement behavior.

Artifact policy:

- Prefer `BONSOL_RUNTIME_HOST_DIR` when supplied.
- Otherwise complete and reuse `/tmp/kswarm-bonsol-phase0b-fresh`.
- Fall back to `/tmp/kswarm-bonsol-phase0b` only when the fresh runtime directory does not exist.
- If no reusable runtime exists, build `/tmp/kswarm-bonsol-phase0c-runtime` through `protocol/scripts/run-bonsol-builder.sh`.

The Tier 2 tests do not use a mock verifier program and do not replay saved proof artifacts.

## Tier 3: Release-Candidate Smoke

Run Tier 3 on release-candidate tags or before an operator evaluation.

```bash
docker compose -f docker-compose.bonsol.yml build bonsol-builder bonsol-image-server bonsol-node bonsol-smoke-test
docker compose -f docker-compose.bonsol.yml down -v
docker compose -f docker-compose.bonsol.yml --profile test up --abort-on-container-exit --exit-code-from bonsol-smoke-test --force-recreate
```

Tier 3 remains the full Phase 0b Bonsol smoke path. It verifies the broader Docker stack and operator-facing runbook flow.

The containerized Python stack has its own end-to-end smoke test, `scripts/swarm-smoke.sh` (wrapped by `worker/tests/test_swarm_smoke.py` under the `integration` marker with `KSWARM_SWARM_SMOKE=1`). It builds the four images from `docker/swarm/Dockerfile`, starts the `local` profile of `docker-compose.swarm.yml`, bootstraps, opens a two-branch prediction, waits for the branch worker, verifier, and aggregator, reads `predict report`, settles both branch jobs, and tears down. It needs Docker, the program artifact, and `LLM_BASE_URL` / `LLM_MODEL_NAME`. See [Containers](containers.md).
