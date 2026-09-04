# User Community Guide

!!! note "This page describes the whole kswarm source tree"
    Some of what it names is not published in this repository: the local
    development and Bonsol evaluation compose stacks, their `make` targets,
    the flagship demo scripts, `scripts/bootstrap-handson.sh`, and the
    Polymarket adapter. `docker-compose.swarm.yml` and
    `scripts/swarm-smoke.sh` are here and run the swarm end to end on a
    local validator. See "Not published here yet" in the README.

*Status: pre-release. Last updated 2026-09-03.*

## 1. Welcome

This guide explains how to use and how to run kswarm. Customers want sections 4 and 8. Node operators want 5, verifiers 6, aggregators 7. KAI holders want 3 and 8. Contributors want 11. Everybody should read 3 and 9.

Two statements set the tone.

- **Nothing here is deployed with real stake.** The protocol runs on a local validator and is being prepared for devnet. Mainnet needs an external audit first.
- **We do not claim a forecasting edge.** Our own sealed pre-registered test on one class of sports-market events returned a null result. We published it. The protocol pays for computation. It is not a trading product and it does not manage anyone's money.

## 2. What the swarm does

kswarm is a prediction engine. You give it seed material and a question about the future. It builds a set of simulated scenarios, runs each one with a language model, and combines the results into one forecast with a range.

The Swarm Protocol turns that engine into an open network: many independent operators run pieces of the work instead of one server. Solana holds the money and the rules, IPFS holds the artifacts, and KAI is the payment and stake token.

### Job lifecycle

1. **Plan.** `predict open` plans the whole run before it touches the chain: it draws a random base nonce, pins the parent manifest and every job input to IPFS, and writes a run manifest to disk.
2. **Escrow.** It opens one branch job per scenario plus one aggregate job, and each `open_job` locks its reward in a Solana escrow vault. No escrow, no work. This is what stops spam.
3. **Commit the input.** `commit_input_artifact` records the input CID and moves the job from `awaiting-artifact` to `open`.
4. **Claim.** A registered worker with enough free stake calls `claim_job`. The program locks `required_stake` and starts an execution deadline.
5. **Execute.** The worker runs the branch against an OpenAI-compatible chat endpoint at temperature 0 with a fixed seed, so the same input gives the same output.
6. **Receipt.** The worker publishes the output and transcript to IPFS, then calls `submit_receipt`. The job becomes `completed` and the challenge window starts.
7. **Verify.** A verifier re-executes the same branch with the same model and seed, then submits an attestation carrying its own result hash.
8. **Settle or challenge.** After the challenge window, `settle_job` pays the reward and unlocks the stake. If the assigned verifier's result differs, `challenge_job` slashes the stake instead.
9. **Aggregate.** The aggregate job combines the branch results. The on-chain gate is real: `settle_aggregate_proof_job` pays only when a Bonsol proof marker is verified on-chain and a matching attestation exists. **In this release your aggregate job does not reach that gate.** The reducer image the CLI names cannot read the aggregate input document, so `predict open` opens the aggregate job unbound and warns, and no running process checks an off-chain EZKL proof or zkVM receipt against the result it claims. See section 3, "Status and what to expect", before you rely on either.
10. **Report.** `predict report` returns the aggregate value and branch narratives.

Every job reaches exactly one terminal state. Three escape paths stop money from locking forever: `cancel_open_job` for a job nobody claimed, `slash_stale_job` for a claim with no receipt, and `cancel_aggregate_proof_job` when the aggregate proof never lands.

## 3. Status and what to expect

The chain plumbing is real and runs end to end on a local validator: escrow, stake, claim, receipt, attestation, settlement, cancellation, slashing. The trust layer is newer and has known gaps.

| Area | Today |
| --- | --- |
| Escrow, stake, claim, settle, slash | Works. Covered by integration and unit tests. |
| Verifier re-execution | The default. Hash-only checking survives only behind `VERIFIER_HASH_ONLY=1`. |
| Challenge authorization | Only the verifier the customer or admin assigned may challenge. |
| Aggregate settlement gate | Proof-gated on-chain through Bonsol. Sound. |
| zkVM guests | They hash the values they are given; they do not recompute them from the source text. A proof says "the guest saw these values", not "these values are true". The EZKL branch proof is a fixed two-input linear function, off-chain only: research tooling. |
| Aggregate settlement in practice | The checked-in reducer image cannot read the aggregate input document, so `predict open` opens the aggregate job unbound and warns. Branch jobs are unaffected. |
| Off-chain proof checking | Nothing runs it. The rules that bind an EZKL proof or a zkVM receipt to the result it claims are implemented and tested, but the only component that called them was the Node worker, retired in this release. The verifier re-executes the branch instead, which catches a fabricated result but is not proof verification. |
| Settle daemon | None in the Python stack. Branch jobs stay `completed` until somebody runs `kswarm settle <job>`. |

### Release path

Consolidation and the switch to KAI are done. Next is a devnet release with a stand-in test mint, new program keys, and a recorded multi-node evaluation. Then an external security audit of the Solana program. Mainnet with real KAI comes after the audit and an operator go decision, and not before.

## 4. Getting started as a customer

Install the CLI from the repository:

```bash
uv venv .venv
uv pip install -e cli --python .venv/bin/python
kswarm --help
```

Global options come before the command name, for example `kswarm --cluster local --json <command>`. `local` uses a validator on your own machine, `devnet` the public Solana devnet. `mainnet` exists as a profile but has no program id, so every on-chain command there fails with a clear message. That is deliberate.

### Wallet

```bash
kswarm wallet create customer --airdrop 10
kswarm wallet activate customer
```

The keypair is written under `~/.config/kswarm/wallets/`. Read section 9 first.

### Funding

On mainnet you hold KAI. On `local` and `devnet` there is no KAI, so you use a stand-in mint with the same layout. The CLI refuses to create or mint tokens on any other cluster.

```bash
kswarm token create-mint --authority admin
kswarm token mint 300000 --to customer
kswarm token balance customer
```

### Open a prediction

```bash
kswarm predict open \
  --question "Will sentiment around the seeded public news item be net-negative?" \
  --output-kind scalar \
  --branches 16 \
  --combiner weighted-mean \
  --reward-per-branch 1KAI \
  --aggregator-reward 5KAI \
  --challenge-window 600
```

Defaults: `scalar`, 16 branches, `weighted-mean`, a 600-second challenge window, wallet `customer`, and a required stake of 500 for every job. Only `trimmed-mean` accepts `--trim-bps` (default `1000` = 10% of the branches); with any other combiner it is an error. `--context-file` embeds your seed material and hash-binds it in the parent manifest.

The first line on stderr, before the first transaction, is:

```text
parent_run=<aggregate-job-pubkey> base_nonce=<u64> run_manifest=<path>
```

Keep the `parent_run` value; it names the run in every later command.

### Watch, resume, cancel, report

```bash
kswarm predict status <parent-run>
kswarm predict resume <parent-run>
kswarm predict cancel <parent-run> --as customer
kswarm predict report <parent-run>
```

`predict open` sends one transaction at a time and rewrites the manifest after each confirmed one, so if it stops early, escrow is locked only in the jobs the manifest marks `opened` or `committed`. `predict resume` opens what is missing, using the wallet and cluster recorded in the manifest. `predict cancel` unwinds every job still `awaiting-artifact` or `open` and refunds their escrow; a cancelled run cannot be resumed. `predict report` reads the aggregate output and branch narratives from IPFS, capped by `KSWARM_IPFS_MAX_BYTES` (8 MiB).

Read the report with one thing in mind. The scalar guardrail is the part the verifier re-executed and the protocol committed. The narrative prose is hash-committed but is **not** checked for correctness, source faithfulness, or quality. Two honest runs can produce different prose and still match.

### Cost and refunds

You pay `reward-per-branch` for every branch plus `aggregator-reward` once. Each branch costs at least one model call for the worker and one more for the verifier. You get the escrow back if you cancel a job nobody claimed, if a worker claimed and missed the execution deadline (plus that worker's locked stake), if the assigned verifier challenged the receipt and won (plus the stake minus the verifier's reward), or if the aggregate proof did not land within 24 hours of the challenge window closing (the worker's stake unlocks and is not slashed).

You do not get money back when a worker delivers a receipt that no verifier successfully challenges. The protocol pays for computation that was done, not for a forecast that turns out to be right.

## 5. Running a branch worker

### Requirements

| Item | Value |
| --- | --- |
| Stake | 50,000 KAI (tier one), 250,000 (tier two), 1,000,000 (tier three) |
| Role | `worker-basic`, `worker-proof`, or `worker-premium` |
| Job classes | Policy: `deterministic-basic` at tier one, add `branch-proof` at tier two, all current branch classes at tier three. The program enforces the `required_tier` the customer sets on each job. |
| Compute | An OpenAI-compatible chat-completions endpoint running the network's model. The container itself is small: the reference compose file gives each daemon 1 CPU and 1 GB. |
| Storage and network | A Kubo IPFS node with local pins kept for input, transcript, output, and evidence, plus a Solana RPC endpoint. |

Your tier comes from total staked KAI against the floors in the on-chain config. Run `kswarm protocol show` to read the live floors; they are configuration, not program constants.

### Register and stake

```bash
kswarm worker register --as worker-a \
  --role worker-proof --capability worker-proof --software-digest worker-canonical
kswarm worker stake 50000 --as worker-a
kswarm worker show worker-a
```

### Containers and configuration

The Python stack ships as four images from one Dockerfile: `branch-worker`, `verifier-worker`, `aggregator-runner`, and `cli`. Each runs as an unprivileged user with a read-only root filesystem, all capabilities dropped, and a `/metrics` endpoint.

> The images and `kswarm swarm bootstrap` are on the release branch. The
> compose file requires `LLM_BASE_URL` and `LLM_MODEL_NAME`: without them even
> `docker compose ... config` fails. Export both before you bring the stack up.

```bash
docker compose -f docker-compose.swarm.yml --profile local up -d
docker compose -f docker-compose.swarm.yml --profile local logs -f branch-worker
```

The images hold no key, RPC URL, or endpoint. Everything comes from the environment.

| Variable | Meaning |
| --- | --- |
| `KSWARM_CLUSTER`, `KSWARM_RPC_URL`, `KSWARM_PROGRAM_ID` | Which chain and which program |
| `KSWARM_WALLET_FILE` | Path of the daemon's keypair file |
| `KSWARM_IPFS_API_URL` | Kubo API URL |
| `LLM_BASE_URL`, `LLM_MODEL_NAME` | Chat-completions endpoint and model. Required. |
| `LLM_API_KEY`, `LLM_MAX_TOKENS` | Bearer token (`local-llm` for a local server) and completion cap (12000) |
| `KSWARM_WORKER_MAX_CLAIMS` | Unsettled claims this worker may hold |
| `KSWARM_WORKER_POLL_SECONDS`, `KSWARM_CLAIM_COOLDOWN_SECONDS` | Poll interval and wait between claims |
| `KSWARM_EXECUTE_DEADLINE_MARGIN_SECONDS` | Stop retrying this long before the deadline |

`LLM_MODEL_NAME`, `LLM_MAX_TOKENS`, and the system prompt are part of the version hash the verifier compares. Treat them as network parameters, not as local taste.

### Claims and deadlines

`claim_job` locks stake and starts a clock. There is no instruction to release a claim. If you do not submit a receipt before `execute_deadline`, anyone may call `slash_stale_job` and the customer takes your locked stake.

The daemon reduces this risk: it checks the model endpoint and IPFS before it claims, keeps a claim budget against the on-chain `active_claims` count, retries until a margin before the deadline, and opens a circuit breaker after repeated failure. It cannot remove the risk. A host that dies after a claim loses that stake.

`KSWARM_WORKER_MAX_CLAIMS` counts completed jobs still waiting out their challenge window, so a low value also caps throughput. That is the price of bounding stake at risk.

### What gets you slashed

Two things. If you claim a job and miss the execution deadline, the full `required_stake` goes to the customer. If the assigned verifier re-executes, gets a different result, and challenges, `min(required_stake, challenge_bond)` goes to that verifier and the rest to the customer. A stale slash pays once: the program sets every settlement flag in the same instruction, so the claim instructions reject the job afterwards.

There is also a false-positive risk. Local model inference can diverge even at temperature 0 with a fixed seed. The protocol snaps scalar outputs to basis points, but two honest nodes can still land in different buckets. Run repeated local trials for each model, quantization, and prompt family before you accept paid traffic.

### Withdraw stake

`kswarm worker withdraw-stake 10000 --as worker-a` returns unlocked stake. Stake locked against a claim stays locked until that job settles, is cancelled, or is slashed.

## 6. Running a verifier

The verifier floor is **100,000 KAI**.

```bash
kswarm worker register --as verifier \
  --role verifier --capability worker-proof --software-digest worker-canonical
kswarm worker stake 100000 --as verifier
```

**Assignment.** Any verifier at the floor may post an attestation, but only the verifier the customer or the protocol admin assigned may challenge. The assignment must happen before an attestation lands, with `kswarm assign-verifier <job> --verifier verifier`. The program accepts an assignment on any job class, refuses one after an attestation is recorded, and refuses to assign the job's own worker.

**Re-execution.** The daemon re-runs the branch with the same model, prompt, and seed, then compares its own canonical commitment with the worker's receipt.

```bash
kswarm attest <job> --result-hash <hex> --evidence-cid <cid> \
  --software-digest worker-canonical --as verifier
kswarm challenge <job> --as verifier
kswarm claim-verifier-slash-reward <job> --as verifier
```

**Limits you must accept.**

- The job binds a software digest, not a model. If a worker names a model you do not run, you cannot reproduce it. Skip that job; do not challenge it.
- A different `LLM_MODEL_NAME`, `LLM_MAX_TOKENS`, or system prompt gives a different hash for every job. With `KSWARM_CHALLENGE_ON_MISMATCH` on, you would challenge every honest worker.
- `VERIFIER_HASH_ONLY=1` only re-hashes the worker's own artifact. It cannot catch a lie. It is a diagnostic mode and warns on every attestation.
- Each verification costs one model call, and an attestation is one-shot per job. Attest only after a successful re-execution.

**Reward and bond.** `challenge_bond` is set by the customer at `open_job`, defaults to the required stake, and caps what a winning challenger receives. In this release the verifier posts **no** bond of its own, and no verifier stake is locked or forfeited. A bonded dispute path with an adjudicator is a separate milestone; until it lands, the assigned-verifier rule bounds the damage a bad verifier can do.

## 7. Running an aggregator

The aggregator combines a run's attested branches into one result and submits the aggregate receipt. Register with the aggregate capability and the reducer image id as your software digest, because `claim_job` gates the aggregate job on both:

```bash
kswarm worker register --as aggregator \
  --role worker-proof --capability branch-aggregator-bonsol --software-digest <reducer-image-id>
kswarm worker stake 50000 --as aggregator
```

Set `KSWARM_BONSOL_AGGREGATE_COMMAND` to a hook that runs the reducer over exactly the committed input artifact, framed with a little-endian 64-bit length prefix. Without that hook the aggregate receipt cannot settle on chain. Settlement is `kswarm settle-aggregate <aggregate-job>`.

Note the limit from section 3: the checked-in reducer image cannot read the aggregate input document, so a run opened with the default image cannot settle. Point `--aggregate-image-id` at a reducer whose input is the aggregate artifact, or use `--defer-aggregate-open` and bind the aggregate job yourself.

## 8. The KAI token in the protocol

| Field | Value |
| --- | --- |
| Token | KAI |
| Mint | `CZHcDHQZerSch8Fhhi2KgV4cLiD2KtdwjJBrb8fypump` |
| Standard | Classic SPL Token, not Token-2022 |
| Decimals | 6 |
| Supply | Fixed |
| Mint authority | Revoked |
| Freeze authority | Revoked |

**There is no new token.** The protocol uses the KAI mint that already exists. KAI exists only on mainnet, so local and devnet use a stand-in classic SPL mint with 6 decimals. The mint address is a per-cluster configuration value, never a constant in the code.

**Base-unit math.** The program stores every amount as an unsigned 64-bit integer in base units, and reads the mint's decimals once at `initialize_protocol`. 1 KAI = 1,000,000 base units, so 0.000001 KAI is one base unit (the CLI truncates anything smaller) and 50,000 KAI is 50,000,000,000 base units.

**Roles in the protocol.** KAI does four jobs and no others. **Escrow**: the customer locks the reward at `open_job`; it leaves only to the worker on settlement, or back to the customer on cancel, timeout, or slash. **Stake**: a worker locks `required_stake` at `claim_job`; it unlocks on settlement or cancellation. **Reward**: paid from escrow to the worker on settlement. **Slash**: paid from the worker's vault to the customer, and to a challenging verifier up to the challenge bond.

**Stake floors** are arguments to `initialize_protocol` and live in the on-chain config, not in program constants. The initial values are 50,000 / 250,000 / 1,000,000 KAI for the three worker tiers and 100,000 KAI for verifiers. Read the live values with `kswarm protocol show`.

**What the protocol does not do.** It does not pay yield. It does not buy back, burn, or mint KAI; the mint authority is revoked, so it could not. It promises no price, no return, and no forecasting edge. It custodies no trading capital. Holding KAI gives you no claim on the project. Inside the protocol, KAI pays for computation and backs the promise to do that computation honestly. Nothing else.

## 9. Safety and key custody

- **Wallet files hold secret keys.** The CLI writes them at mode `0600` inside a `0700` directory, and it refuses to load any key file that group or others can read: you get `InsecureKeyFileError` naming the file and the fix. If a key file was created before this release, or by another tool, repair it with `chmod 600 <file>` and `chmod 700` on the directory holding it.
- **Never share a key file, a seed phrase, or a private key.** No maintainer, no bot, and no support channel will ever ask for one. Anyone who does is attacking you.
- **Verify the program id before you sign anything.** It is `ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM`. Two earlier keypairs were tracked in git and are burned: `Hjp1BotySPaS4Uy3TT6ySsfuZ3pUVuUVCweTWP9C4pPH` and `HFaoNx7zQ1mwVgf6dKCTBFxtADUkMq7Y9jXWiL1WS5h8`. Never send funds to anything deployed at those addresses. Check what you are talking to with `kswarm protocol show`.
- **Watch for phishing.** There is no airdrop, no presale, no allocation form, and no "claim your KAI" page. No official support operates by direct message. Check links against the repository named in section 11 before you open them.
- **The program is unaudited.** An external audit is required before any mainnet deployment with real KAI. Until this document says that audit is complete, treat every deployment as a test system and stake nothing you cannot lose.
- **Keep deploy keys out of the repository**, referenced by environment variable instead.

## 10. FAQ

**Can I make money?**
Nobody should count on it. Workers and verifiers are paid in KAI for computation they perform, and lose stake for failing to perform it. There is no yield and no return, and the forecast customers pay for has no demonstrated edge.

**Does the swarm beat a prediction market?**
Not on the evidence we have. Our sealed, pre-registered test on one class of sports-market events was null on both doors tested, and we published that. Anyone claiming a kswarm forecasting edge is not speaking for this project.

**Is my prediction private, and where does the data live?**
It is not private. Inputs, outputs, and transcripts go to IPFS, pinned by whoever ran the job; job records go on a public chain. Assume anything you submit is readable by anyone who can reach the artifact network. Do not submit confidential material. Pinning policy and retention windows are not implemented, so keep your own pins.

**What model runs my branch, and why is it the same for every branch?**
Whatever model the network is configured for. Workers point `LLM_BASE_URL` and `LLM_MODEL_NAME` at an OpenAI-compatible endpoint. The job binds a software digest, not a model, so model identity is not enforced on chain today; that is a known gap. It is one model per network because verification is re-execution: a verifier catches a lying worker only if it can reproduce that worker's configuration.

**Why did my job get slashed?**
Two reasons only. Either you claimed a job and did not submit a receipt before the execution deadline, or the assigned verifier re-executed your branch, got a different result, and challenged inside the challenge window. For a missed deadline anyone may send the slash transaction; for a bad result, only the assigned verifier.

**A verifier challenged me but my work was correct. What now?**
There is no appeal path in this release. That is why the challenge right is restricted to the assigned verifier, and why the bonded dispute path is a named milestone and not a claim.

**Can I run on a laptop?**
The worker daemon fits in about 1 CPU and 1 GB. The model is the real requirement, and you need a GPU only if you host the model yourself. But a laptop that sleeps, drops its network, or shuts down after a claim loses that job's stake. Fine for local testing, poor for paid traffic.

**How much stake do I need, and can I get it back?**
50,000 KAI for tier one, 250,000 for tier two, 1,000,000 for tier three, and 100,000 for a verifier. Read the live numbers with `kswarm protocol show`. Withdraw any stake not locked against a claim with `worker withdraw-stake`.

**Is there a new token, an airdrop, or a presale?**
No. The protocol uses the existing KAI mint, whose mint and freeze authorities are revoked, so no new supply can exist. Any airdrop or presale in the name of this protocol is a scam.

**Can I run a node on mainnet today?**
No. There is no mainnet program id, and the CLI refuses on-chain commands on the `mainnet` profile for that reason. Devnet is next, then the external audit.

**Why did my aggregate job never settle?**
Most likely the reducer limit in section 3: the default image cannot read the aggregate input document, so the job is opened unbound and warns. Twenty-four hours after the challenge window closes, the customer can cancel it and recover the escrow.

**Why is my branch job stuck at `completed`?**
The Python stack has no settle daemon, so after the challenge window somebody has to call `kswarm settle <job>`. The Node watcher does it automatically if you run that stack.

## 11. Getting help and contributing

**Questions and bugs.** Open an issue on the repository the problem belongs to: [kswarm](https://github.com/agent-k-ai/kswarm) for the workers, the CLI, the control plane and the containers, or [kswarm-protocol](https://github.com/agent-k-ai/kswarm-protocol) for the on-chain program. Community discussion is in [kswarm Discussions](https://github.com/agent-k-ai/kswarm/discussions).

**Security issues.** Do not open a public issue. Follow `SECURITY.md` in the repository and report through GitHub private vulnerability reporting (Security tab -> Report a vulnerability). State the affected component, the impact, and the steps to reproduce. You get an acknowledgement, a severity assessment, and a fix or mitigation plan.

**Contributing code.**

1. Work on a feature branch. Never commit to a release branch.
2. Add tests. A change to the Solana program needs a test in the anchor integration suite; a change to the CLI or workers needs a pytest.
3. Run the tier-one program tests and the Python tests, then open a pull request into the current release branch and record in its body what you ran and what passed.

**Licensing.** The swarm repository is **AGPL-3.0**, because it depends on the AGPL-licensed MiroFish simulation engine. The Solana program repository carries a **permissive** licence (Apache-2.0 proposed), because it imports nothing from that engine and its audit scope should stay small. Contributions are accepted under the licence of the repository they target.

## 12. Glossary

| Term | Meaning |
| --- | --- |
| **Attestation** | A verifier's on-chain record of the result hash it computed for a job. |
| **Bonsol** | The service that proves a RISC Zero guest ran and reports it to Solana. |
| **Branch** | One scenario of a prediction run: baseline, optimistic, shock, and so on. |
| **Challenge bond** | The customer-set cap on what a winning challenger is paid. |
| **Challenge window** | The period after a receipt in which the assigned verifier may challenge. |
| **Escrow** | The vault holding a job's reward until the job reaches a terminal state. |
| **KAI** | The payment and stake token. Mint `CZHcDHQZerSch8Fhhi2KgV4cLiD2KtdwjJBrb8fypump`. |
| **Receipt** | A worker's on-chain record of an output CID and a result hash. |
| **Slash** | The transfer of locked worker stake to the customer, and to a challenger. |
| **Verifier** | A staked operator that re-executes a branch and attests or challenges. |
