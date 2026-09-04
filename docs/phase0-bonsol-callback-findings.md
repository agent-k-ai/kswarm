# Phase 0a — Bonsol Callback Discovery Findings

## Status

- Captured: 2026-05-07
- Phase 0b production rerun: 2026-05-07
- Bonsol pinned commit: 25a590d09cca0404cc48ec028122df4d1a8c651b
- Local validator: anzaxyz/agave:v2.0.13

## Summary

The callback path can be wired end-to-end into a kswarm-controlled program: Bonsol StatusV1 invoked `phase0_callback_probe`, the probe validated the Bonsol signer/execution account/image id, wrote the marker PDA, and emitted `Phase0CallbackRecorded`. Two ADR assumptions did not hold in this local stack. Callback runtime errors were not harmless in practice: RPC rejected the StatusV1 transaction with the probe's custom error and Bonsol did not write the marker. Also, `verifyInputHash: true` did not reject a deliberately wrong `inputHash`; the execution cleaned up with exit code `0`.

Phase 0b reran the same path against production `kswarm_protocol::record_aggregate_verification`. The production callback wrote a 210-byte `BonsolAggregateVerification` PDA, enforced Bonsol owner/signer/image/digest/journal/PDA binding, and `settle_aggregate_proof_job` paid the aggregate worker only after the marker and verifier attestation were both present.

## Test matrix results

| ID | Test | Expected | Observed | Notes |
|---|---|---|---|---|
| Happy path | Valid callback config, correct image id, `verifyInputHash: true` | StatusV1 succeeds and marker is written | Success. Marker `B5FJ3pwD6jFDhTgPWnBnbinkaUtBWuwKJ2RJBa5x4RMS` was verified from a 276-byte marker account. StatusV1 signature `ku85vTSmTy5hkHfoykQx3nw13wsRxU6Py9FSYbrgqCHti4fWPR9AnffPn2buXB16TJyva9wb8k28cjmHZ7vT6hf`. | Status logs include `Proof verified with V3_0_3` and `Phase0CallbackRecorded ... output_len=41`. |
| N1 | Callback runtime error | Bonsol success, no marker | No marker, but StatusV1 did not succeed. Bonsol node logged `TransactionError(InstructionError(0, Custom(28679)))`; `28679` is probe error `0x7007` intentional failure. | This contradicts the callback-swallow assumption from the pinned source. The execute CLI then waited until its local timeout. |
| N2 | Wrong image id submitted in execute request | Bonsol rejects at StatusV1 time; no callback invoked | Rejected at execute transaction simulation before StatusV1 with Bonsol custom `0x12`. No callback invoked. | Request image id was all zeroes; Bonsol consumed 2061 CU before rejecting. |
| N3 | Wrong execution account passed at status time | Bonsol rejects; no callback invoked | Happy setup first wrote marker `95hoEU6TXHcpXinR84v8LduwyFGi6CtCNR12BTixBYfu`. Replay with a wrong execution account was rejected: `Error processing Instruction 0: custom program error: 0x1`. | Replay result file recorded `"accepted": false`, mode `wrong-execution-account`. |
| N4 | Mismatched extra accounts in callback config | Callback invocation fails at runtime; per N1, Bonsol still success, no marker | No marker, and StatusV1 did not succeed. Bonsol node logged `TransactionError(InstructionError(0, Custom(28676)))`; `28676` is probe error `0x7004` marker PDA mismatch. | Passed marker PDA was `5ohX24nXjRUJFoTcJsdBtvKr1XdavH7SvaKmfA7mVf1M`; expected marker PDA was `E8VjfBSMHck3Z9n2HPhBT3jWAQPSscxQ7FD7ugjHPeZo`. |
| N5 | `verifyInputHash: true` with wrong `inputHash` | Bonsol rejects at StatusV1; no callback invoked | Did not reject. The execution account `7kickHxPs8shrLXRdptajJLcJLK7oPfCfiVusCn52d3W` cleaned up to one byte with exit code `0`. | Wrong hash submitted was `a5` repeated 32 bytes. This matches the pinned `status.rs` bug where the hash check result is not propagated. |
| N6 | Replay attempt after Bonsol cleanup | Bonsol rejects; no callback invoked | Happy setup first wrote marker `8MqTPzskz2RAUXtGJZRWn57zngUS7JLjpHuj6nPyLtXf`. Replay after cleanup was rejected: `Error processing Instruction 0: custom program error: 0x1`. | Replay result file recorded `"accepted": false`, mode `replay`. |

## Phase 0b production callback matrix

| ID | Test | Production observed result | Status / replay evidence |
|---|---|---|---|
| Happy path | Valid production callback config, correct image id, `verifyInputHash: true` | Success. Marker `D23mzbc5FB8siJSbKbPRViuiupjYtN2EyLHUXkXMwtHY` was verified from a 210-byte `BonsolAggregateVerification` account. | StatusV1 signature `pEevo58wggM6xh79D83CzCFR7TbJoAEbMKh5sArQKdkMfnyBzXSg39vTTSLvPxe5eQe2v7iT49YnU2Q6Sa7Fawz`. |
| Settle | Happy path followed by `settle_aggregate_proof_job` | Success. Marker `D2rBJHQT7McDpf7AqU3D9bQabzZ72aJRGM72gVyGvYkF`; worker token balance increased by reward `25000000000`. | StatusV1 signature `apbBdkPUWkNj9bgZogAgVNPT1eKqnr9ciYvjYs6MGqUzg311tbboPnRvHPXsdoJL1QimLKVYGbtuEBHvteujNDd`; settle signature `3uQhaK183jTnDWYSEQe5dLh2uakpqqseyeKoEtZf8PZYzGFXFCuuYGkb6oALUuRsGD69Y8mDYDRBRcuWAnkCUScL`. |
| N1 | Callback argument with wrong output digest | StatusV1 failed and no marker was written. | `InstructionError(0, Custom(6045))`, production `BonsolOutputDigestMismatch`, signature `63UdDrgBWudkXdHAhvvvQz1XB3TNg4RUYr1279aUmoLiof9jppSX6tMN6XGqEkiLux4Y3d9degMGDzzQsX2W5LDL`. |
| N2 | Wrong image id submitted in execute request | Execute simulation rejected before callback. | Bonsol custom `0x12`, Bonsol consumed `2061` CU in simulation. |
| N3 | Wrong execution account replay at status time | Initial happy StatusV1 wrote marker `G7cBgoTdBnuR6rUwuNojGAZKnA7Q3b2rEPw6gKpDVqWz`; replay was rejected. | Replay result: `"accepted": false`, `Error processing Instruction 0: custom program error: 0x1`. |
| N4 | Wrong callback marker PDA in extra accounts | StatusV1 failed and neither the wrong marker nor the expected marker was written. | `InstructionError(0, Custom(6051))`, production `BonsolMarkerMismatch`, signature `X2BP1JqrMVor1a3dhUSGzRqDLWa2fTmmXKD9dSnKr1sGkYYgp8Kk62eJqxaHvAKbbJG8JAKGz4HrcSbaWxPMXd5`. |
| N5 | `verifyInputHash: true` with wrong configured `inputHash` | Still accepted by Bonsol. Production marker `3onYTegN52zAQ2V6RWy2FUbqLh8Rpesci3Kjie96Xzqe` was written because the actual framed input digest matched the execution record and aggregate job commitment. | StatusV1 signature `FYKyeTqfiVPXv479wkaB4ukkTqrs1BKx7Yz2CKEZDY1b2q6vR1Xv7r1gU1Nfg8gXK9w1SPnSVANqCgnbo83dZyy`. This preserves the Phase 0a finding that Bonsol does not bind the configured `inputHash`. |
| N6 | Replay after Bonsol cleanup | Initial happy StatusV1 wrote marker `7KLxyUgRNSCrBSTCk9Xp7FwU1m6jVcH2YHSpDr6ShzYa`; replay was rejected. | Replay result: `"accepted": false`, `Error processing Instruction 0: custom program error: 0x1`. |

## CU measurements

| Path | Measured CU | Within default 200k? |
|---|---:|---|
| StatusV1 with callback (happy path) | 147452 | Yes |
| Phase 0b StatusV1 with production callback (happy path) | 145339 | Yes |
| Phase 0b production callback CPI only (happy path) | 13950 | Yes |
| Phase 0b StatusV1 with production callback plus settle-mode setup | 146845 | Yes |
| Phase 0b production callback CPI only in settle mode | 15458 | Yes |
| Phase 0b `settle_aggregate_proof_job` transaction | 30336 | Yes |
| Phase 0b N1 failing callback StatusV1 | 138718 | Yes |
| Phase 0b N4 marker-mismatch StatusV1 | 149589 | Yes |
| Phase 0b N5 wrong configured `inputHash` StatusV1 | 147073 | Yes |

The same Phase 0a StatusV1 transaction log reported the probe callback consumed 16212 CU and Bonsol consumed 147452 of 200000 CU total. That left 52548 CU before the default cap and made Phase 0b CU re-measurement mandatory.

The Phase 0b production happy path stayed below the default 200000 CU limit without adding a compute-budget instruction. The production callback CPI consumed less CU than the Phase 0a probe callback in the clean happy run, and settlement is intentionally a separate transaction with measured CU `30336`.

## Surprises / unexpected behaviors

- The pinned source appears to catch callback CPI errors at `status.rs` lines 173-178, but the local runtime still rejected StatusV1 when the callback returned a custom error. See `https://github.com/bonsol-collective/bonsol/blob/25a590d09cca0404cc48ec028122df4d1a8c651b/onchain/bonsol/src/actions/status.rs#L160-L184`.
- `verifyInputHash` is not effective at the pinned commit. The source calls `check_bytes_match` inside `Option::map` and drops the result instead of returning `?`, so a mismatched digest can still clean up successfully. See `https://github.com/bonsol-collective/bonsol/blob/25a590d09cca0404cc48ec028122df4d1a8c651b/onchain/bonsol/src/actions/status.rs#L104-L108`.
- The RISC Zero guest must commit the input digest as the first 32 journal bytes because the Bonsol node splits the journal at byte 32 before StatusV1. See `https://github.com/bonsol-collective/bonsol/blob/25a590d09cca0404cc48ec028122df4d1a8c651b/node/src/risc0_runner/mod.rs#L474-L488`.
- The direct FlatBuffer helper must submit one framed public input matching the deployment manifest's single `Public` input. The CLI JSON path accepted the smoke request shape, but the direct helper rejected two FlatBuffer public inputs with Bonsol custom `0x3` before the helper was corrected.

## Recommendations for Phase 0b

- Treat callback failure as transaction-fatal for this pinned stack. Phase 0b production N1 and N4 reproduced transaction-fatal callback failures with production errors `6045` and `6051`.
- Do not rely on Bonsol `verifyInputHash` at commit `25a590d09cca0404cc48ec028122df4d1a8c651b`. The production callback or settlement path must bind `execution_id`, `image_id`, input digest, and output digest in kswarm-owned state and test a wrong-digest case.
- Derive every callback account from deterministic seeds before submitting ExecuteV1. A wrong extra account caused StatusV1 rejection, so the request builder should precompute and verify the marker/settlement PDA client-side and the callback should verify the same PDA on-chain.
- Keep `execution_id`, `image_id`, input digest, and committed-output digest in the marker PDA seeds. This made the happy-path marker verification unambiguous and made wrong-account failures easy to diagnose.
- Preserve the Phase 0b CU measurements when production callback logic changes. The discovery probe left about 52k CU under the default cap; the measured production callback still fits under the cap.
- Keep settlement split from StatusV1. The production callback fits under the default cap, and `settle_aggregate_proof_job` was measured independently at `30336` CU.

## Reproduction

From a clean checkout on branch `feature/phase0a-bonsol-callback-discovery`, run from
the repository root:

> Reproduction note (2026-09-03): the probe keypair is no longer in the repository and no compose stack builds or loads the probe; see `solana/programs/phase0_callback_probe/DEPRECATED.md`. The values recorded below were produced with the probe at `HFaoNx7zQ1mwVgf6dKCTBFxtADUkMq7Y9jXWiL1WS5h8`, and the protocol program id has since been rotated to `ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM`.

```bash
cargo build --locked --manifest-path protocol/bonsol-callback-harness/Cargo.toml
CARGO_TARGET_DIR=/tmp/phase0-reducer-target cargo check --locked --manifest-path protocol/bonsol-branch-reducer/Cargo.toml
cargo build-sbf --manifest-path solana/programs/phase0_callback_probe/Cargo.toml
```

Run the final happy callback smoke:

```bash
docker compose -f docker-compose.bonsol.yml --profile callback-test down --remove-orphans
BONSOL_RUNTIME_HOST_DIR=/tmp/kswarm-bonsol-runtime-phase0a-repro \
BONSOL_VALIDATOR_RPC_PORT=56899 \
BONSOL_VALIDATOR_WS_PORT=56900 \
BONSOL_IMAGE_SERVER_PORT=56080 \
PHASE0_CALLBACK_TEST_MODE=happy \
docker compose -f docker-compose.bonsol.yml --profile callback-test up \
  --abort-on-container-exit \
  --exit-code-from bonsol-callback-smoke-test \
  --force-recreate
```

Run each negative mode:

```bash
for mode in \
  n1-callback-error \
  n2-wrong-image-id \
  n3-wrong-execution-account \
  n4-wrong-extra-accounts \
  n5-wrong-input-hash \
  n6-replay-after-cleanup
do
  docker compose -f docker-compose.bonsol.yml --profile callback-test down --remove-orphans
  BONSOL_RUNTIME_HOST_DIR=/tmp/kswarm-bonsol-runtime-phase0a-repro \
  PHASE0_CALLBACK_TEST_MODE="${mode}" \
  PHASE0_CALLBACK_EXPECTED_FAILURE_TIMEOUT=240 \
  docker compose -f docker-compose.bonsol.yml --profile callback-test up \
    --abort-on-container-exit \
    --exit-code-from bonsol-callback-smoke-test \
    --force-recreate
done
```

Run the existing non-callback smoke:

```bash
docker compose -f docker-compose.bonsol.yml --profile test down --remove-orphans
BONSOL_RUNTIME_HOST_DIR=/tmp/kswarm-bonsol-runtime-phase0a-repro \
docker compose -f docker-compose.bonsol.yml --profile test up \
  --abort-on-container-exit \
  --exit-code-from bonsol-smoke-test \
  --force-recreate
```
