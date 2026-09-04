# kswarm — Community Overview

*Plain-language summary. Last updated 2026-09-03, revised at the close of release 01.09.03a. Status: pre-release (devnet next, mainnet after audit).*

## What it is

kswarm is a prediction engine. You give it seed material (a news story, a policy draft, a market question) and a question about the future. It builds a simulated world of AI agents, lets them interact, and reports what happened.

The **Swarm Protocol** turns that engine into an open network. Instead of one server running one simulation, many independent operators run pieces of the work, get paid for it, and can be penalized for cheating. Solana is the settlement layer. **KAI is the payment and stake token.** There is no new token.

## How a job flows

1. **A customer opens a job.** They describe the question, upload the seed material, and lock a KAI reward in escrow on Solana. Nothing runs until the escrow exists, so there is no free work and no spam.
2. **The job splits into branches.** Each branch is one scenario: baseline, optimistic, pessimistic, shock, policy variant, adversarial. One question becomes many child jobs.
3. **Workers claim branches.** Any operator who has staked KAI can claim a branch. A worker runs the simulation for that branch with a large language model using fixed settings, so the same input gives the same output.
4. **Results go to IPFS, receipts go on-chain.** The worker publishes its output and a transcript to the artifact network and submits a receipt hash to Solana.
5. **Verifiers check the work.** A second staked operator re-runs the branch and attests to its own result, and checks the branch's zero-knowledge receipt. For the final aggregation step, a zero-knowledge proof is verified on-chain through Bonsol before any payment moves.
6. **Settlement.** Honest workers are paid from escrow in KAI. A worker who submits bad work or misses the deadline loses stake. The customer is refunded when a job fails.
7. **One answer.** The branch results combine into one forecast with a range of outcomes, not a single point.

## What exists today

- The Solana program: escrow, worker stake, claims, receipts, verifier attestation, settlement, refunds, and slashing. Runs end to end on a local validator. Three demo runs are recorded with on-chain evidence. The escrow authorization fixes are merged, and so are fixes for a double slash, a permanent fund lock, and a free-slash path; every one carries a test that fails without it.
- Proof-gated settlement: the aggregate step will not pay unless a Bonsol proof is verified on-chain and a verifier attests. The proof is a recomputation, not an echo: the guest reads the branch receipts, rehashes each one, applies the combiner itself, and commits the result.
- A private IPFS network for inputs, outputs, transcripts, and proofs.
- LLM branch workers, a verifier worker, and an aggregator, plus an operator command line for every instruction. The verifier re-executes the branch with the identical model, seed and configuration and attests to its own hash, so a worker that never called the model is caught.
- Container images for the worker, verifier, aggregator, and CLI, running unprivileged, with an end-to-end smoke test that opens a prediction, executes it, attests, aggregates, and settles.
- A rigorous evaluation harness with sealed pre-registration and leakage controls.

## What is not done yet

- The zero-knowledge circuits prove the plumbing, not yet the simulation logic. The guests hash the values they are given; they do not recompute them from the source text.
- The language model step is not proven and cannot be. It is secured by deterministic re-execution, attestation, and challenge/slash: a second staked operator runs the same model and slashes a worker whose result differs. That is an economic guarantee, not a cryptographic one.
- Proving costs real time. A branch receipt takes minutes of CPU, so a worker opts into producing them; a verifier that requires them refuses to attest to a branch without one, and that job then cannot settle.
- The EZKL branch proof is research tooling, not part of the release path. Its model is a fixed linear function that is not a submodel of anything kswarm runs, so a proof about it says nothing about a forecast.
- Only the verifier a customer assigns to a job may challenge it. That closes a free-slash hole, and it means a network needs an assignment step before a lying worker can be slashed.
- Nothing is deployed on a public cluster with real stake.

## What we do not claim

- We do not claim the swarm beats a market. Our own sealed test on one class of sports-market events returned a null result, and we published it.
- We do not run anyone's money. The protocol pays for computation. It is not a trading product.

## Release path

1. Consolidate fixes and switch the payment token to KAI. **Done in this release.**
2. Devnet release with a stand-in test token, new program keys, and a recorded multi-node evaluation.
3. External security audit of the on-chain program.
4. Mainnet with real KAI, after the audit and an operator go decision.

## Token facts

| | |
|---|---|
| Token | KAI |
| Mint | `CZHcDHQZerSch8Fhhi2KgV4cLiD2KtdwjJBrb8fypump` |
| Standard | SPL Token, 6 decimals, fixed supply, mint and freeze authority revoked |
| Role in the protocol | Job rewards, worker stake, verifier stake, slashing, refunds |
| Initial worker stake floor | 50,000 KAI (tier one; adjustable by protocol config, not by a program upgrade) |
