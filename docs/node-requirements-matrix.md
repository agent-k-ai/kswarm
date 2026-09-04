# Node Requirements Matrix

This document defines the node-role, stake, and eligibility policy for kswarm.

Parts of this are now enforced on-chain. The remaining sections call out what still needs to be added.

## Current On-Chain Reality

Today the Solana program enforces these node-policy rules:

- a worker must register before participating
- a worker registers with a `role`
- a worker registers with a `capability_class_hash`
- a worker registers with a `software_digest`
- a worker can deposit stake
- a job defines `required_stake`
- a job defines `job_class`
- a job defines `required_role`
- a job defines `required_tier`
- a job can optionally define `required_capability_class_hash`
- a worker can claim only if `available_stake >= required_stake`
- a worker can claim only if `worker.role` satisfies `job.required_role`
- a worker can claim only if derived stake tier satisfies `job.required_tier`
- a worker can claim only if its capability hash matches the job requirement when one is set
- successful settlement unlocks the job's locked stake
- stale claimed jobs can slash the locked stake

That logic is implemented in `solana/programs/kswarm_protocol/src/lib.rs`.

What does **not** exist on-chain yet:

- verifier/artifact-peer registration flows
- max concurrent claims by tier
- software-digest-based claim gating
- verifier challenge bond rules
- artifact-peer retention/accountability rules

So the answer is:

**Yes, the core worker role/tier/capability checks now exist on-chain, but the broader node-policy matrix is still incomplete.**

## What The Containers Register

`kswarm swarm bootstrap` (the `bootstrap` service of `docker-compose.swarm.yml`) registers the shipped daemons as follows. Stakes are the configured floors unless `--worker-stake`, `--verifier-stake`, or `--aggregator-stake` say otherwise.

| Service | Role | Capability | Software digest | Stake |
|---|---|---|---|---:|
| `branch-worker` | `worker-proof` | `worker-proof` | `worker-canonical` | tier-one floor (`50,000 KAI`) |
| `verifier-worker` | `verifier` | `worker-proof` | `worker-canonical` | verifier floor (`100,000 KAI`) |
| `aggregator-runner` | `worker-proof` | `branch-aggregator-bonsol` | the reducer image id | tier-one floor (`50,000 KAI`) |

`predict open` requires tier one for its branch and aggregate jobs, so those stakes are enough for the shipped flow. The role and tier proposals below are policy, not what the containers enforce.

## Design Principle

Anyone can run the software.

Only nodes that satisfy the protocol's stake and role rules can:

- register for a role
- claim paid work
- receive protocol payouts
- appear in protocol job discovery

That means the protocol is:

- permissionless at the software layer
- permissioned by economics at the job/settlement layer

## Role Matrix

| Role | Purpose | Must Stake | Suggested Minimum Stake | Current On-Chain Support | Future Required Checks |
|---|---|---:|---:|---|---|
| `worker-basic` | deterministic jobs, low-risk branch execution | Yes | `50,000 KAI` (tier one) | Yes | max claims |
| `worker-proof` | proof-carrying branch execution: the `zkVM` branch canonicalization receipt | Yes | `250,000 KAI` (tier two) | Yes | proof mode eligibility refinement |
| `worker-premium` | high-value or replicated branch execution | Yes | `1,000,000 KAI` (tier three) | Partial | premium class rollout, higher slash exposure |
| `verifier` | verifies proofs, challenges bad jobs | Yes | `100,000 KAI` (verifier floor) | No | verifier registry, challenger bond rules |
| `artifact-peer` | stores and serves artifacts | Optional early, yes later | `50,000 KAI` | No | uptime/accountability checks |
| `watcher/keeper` | submits settle/slash txs | Low or none | `0-100 KAI` | No | optional reputation, no trust authority |
| `gateway` | upload/download convenience API | No protocol stake | `0` | Off-chain only | should remain non-authoritative |
| `bootstrap peer` | IPFS swarm rendezvous | No protocol stake initially | `0` | Off-chain only | later federation/reputation only |

## Job-Class Matrix

| Job Class | Example | Minimum Node Role | Minimum Stake | Parallelism Policy | Verification Policy |
|---|---|---|---:|---|---|
| `deterministic-basic` | text normalization, canonical extraction | `worker-basic` | `50,000 KAI` | many claims allowed | deterministic hash checks |
| `branch-proof` | branch execution with a canonicalization receipt | `worker-proof` | `250,000 KAI` | bounded by tier | `zkVM` receipt bound to the published branch output |
| `branch-replicated` | high-value replicated branch | `worker-premium` | `1,000,000 KAI` | low concurrency | replicated run and/or proof |
| `aggregate-proof` | deterministic branch aggregation | `worker-proof` or `verifier` | `250,000 KAI` | low concurrency | `zkVM` / Bonsol path |
| `artifact-retention` | storage/serving duty | `artifact-peer` | `50,000 KAI` | storage-capacity based | availability checks |

## Suggested Tier Rules

### Worker Tiers

The stake floors below are the defaults set at `initialize_protocol` (owner decision 2026-09-03). They are config values, not program constants; see [KAI Payment Token](kai-payment-token.md). `V2` figures are proposals and are not implemented.

| Tier | Stake Floor | Allowed Job Classes | Max Concurrent Claims | Slash Multiplier |
|---|---:|---|---:|---:|
| `T1` | `50,000 KAI` | `deterministic-basic` | `2` | `1.0x required_stake` |
| `T2` | `250,000 KAI` | `deterministic-basic`, `branch-proof` | `5` | `1.25x required_stake` |
| `T3` | `1,000,000 KAI` | all current branch classes | `10` | `1.5x required_stake` |

### Verifier Tiers

| Tier | Stake Floor | Allowed Duties | Challenger Bond |
|---|---:|---|---:|
| `V1` | `100,000 KAI` | verify receipts, submit challenges | `5,000 KAI` |
| `V2` | `500,000 KAI` | premium jobs, proof-heavy classes | `25,000 KAI` |

## Required On-Chain Checks To Add

The next version of the program should check all of these at claim/assignment time:

1. `role`
- worker, verifier, artifact peer

2. `stake tier`
- current free stake
- currently locked stake
- tier derived from total active stake

3. `capability class`
- deterministic-only
- `zkvm-v1`
- `bonsol-v1`
- premium replicated branch

4. `software commitment`
- container digest or software measurement hash

5. `job eligibility`
- job class must match role + capability + tier

6. `concurrency limit`
- active claims cannot exceed tier allowance

7. `slashability`
- node must maintain enough unlocked + locked capital to remain punishable

## Proposed Node Registry Fields

Each registered node should eventually have:

- `authority_pubkey`
- `role`
- `status`
- `stake_total`
- `stake_locked`
- `stake_tier`
- `capability_class_hash`
- `software_digest`
- `proof_modes`
- `max_concurrent_claims`
- `successful_jobs`
- `slashed_jobs`
- `reputation_score`

## Proposed Job Fields To Support Policy

Each job should eventually carry:

- `job_class`
- `required_role`
- `required_capability_class`
- `required_proof_mode`
- `required_stake`
- `max_parallel_claims`
- `replication_factor`
- `challenge_bond`

## Enforcement Table

| Check | When It Should Happen | Enforced Today | Needed Next |
|---|---|---|---|
| customer must prepay escrow | `open_job` | Yes | keep |
| worker must have enough free stake | `claim_job` | Yes | keep |
| worker role must match job class | `claim_job` | Yes | keep |
| worker capability must match job class | `claim_job` | Yes, when required hash is set | keep |
| worker stake tier must satisfy job tier | `claim_job` | Yes | keep |
| max concurrent claims by tier | `claim_job` | No | add |
| software digest must match declared class | `claim_job` or registration | Registered only | add claim gating |
| verifier must stake before challenge | `challenge_job` | No | add |
| artifact peers must satisfy retention rules | storage reward flow | No | later |

## Pilot Recommendation

For the pilot, keep the policy simple:

| Role | Pilot Rule |
|---|---|
| `worker-basic` | allowed, `50,000 KAI` minimum |
| `worker-proof` | allowed, `250,000 KAI` minimum |
| `verifier` | design now, implement next |
| `artifact-peer` | do not pay separately yet |
| `watcher` | no protocol stake requirement yet |

That means the next concrete contract step is:

1. extend `Worker` into a general `NodeRegistry` account
2. add max-concurrency enforcement by tier
3. add verifier registration and challenge-bond rules
4. add software-digest and proof-mode claim gating

## Bottom Line

We need smart-contract-level checks for staked node requirements, and we now have the first meaningful slice of that matrix implemented on-chain.

Right now we have:

- customer escrow requirement
- worker stake requirement
- worker role requirement
- worker stake-tier requirement
- worker capability-class requirement
- stale-worker slashing

Next we need:

- verifier and artifact-peer roles
- concurrency controls
- software/proof-mode eligibility rules
- verifier registration and challenge economics
