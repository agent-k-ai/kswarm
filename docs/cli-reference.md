# CLI Reference

All commands accept the global options before the command name:

```bash
kswarm --cluster local --rpc-url http://127.0.0.1:38899 --commitment confirmed --json <command>
```

`local` uses `http://127.0.0.1:38899`. `devnet` uses `https://api.devnet.solana.com`. `mainnet` uses `https://api.mainnet-beta.solana.com` unless `SOLANA_RPC_URL` is set; it has no `program_id` until the mainnet program is deployed, so every on-chain command fails with a clear message there. `KSWARM_CLUSTER` selects the profile and `KSWARM_RPC_URL` overrides the RPC on every cluster, so containers need no flags (see [Containers](containers.md)).

Amounts are human units of the payment token (KAI, 6 decimals; stand-in mints on local/devnet use the same layout). See [KAI Payment Token](kai-payment-token.md).

## Command Inventory

| Command | Purpose | Tested |
| --- | --- | --- |
| `wallet create <name> [--airdrop SOL]` | Create a local keypair under `~/.config/kswarm/wallets`. | pytest |
| `wallet list` | List configured wallets. | smoke |
| `wallet show <name>` | Show wallet pubkey and path. | smoke |
| `wallet activate <name>` | Set the default signer pointer. | smoke |
| `wallet airdrop <name> <SOL>` | Request local/devnet SOL. | pytest |
| `wallet balance <name>` | Show SOL balance. | smoke |
| `token create-mint --authority <name> [--decimals 6] [--token-2022]` | Create a stand-in payment mint (classic SPL Token, 6 decimals by default) on `local` or `devnet` and persist it in the cluster profile. Refused elsewhere. | pytest |
| `token mint <amount> --to <name>` | Mint stand-in tokens to a wallet ATA on `local` or `devnet`. Refused elsewhere. | pytest |
| `token transfer <amount> --from <name> --to <name>` | Transfer KAI between wallets. | smoke |
| `token balance <name>` | Show KAI token balance. | pytest |
| `protocol initialize [--admin <name>] --payment-mint <pubkey> [--tier-floors 50000,250000,1000000] [--verifier-floor 100000] [--i-understand-real-funds]` | Initialize the protocol config PDA. The signer must be the program's upgrade authority: `--admin` names a wallet, else the global `--keypair` file or the active wallet signs. Floors are human units, converted with the mint's on-chain decimals. `--i-understand-real-funds` is required on `mainnet`. | pytest |
| `protocol show` | Decode the protocol config PDA. | smoke |
| `protocol runtime-config --output <path> [--artifact-gateway-url <url>]` | Write the on-chain config as the `protocol.json` the Node api and watcher read (mint, token program, decimals, floors, program id, RPC URL). | pytest + node --test |
| `swarm bootstrap [--branch-worker <name>]... [--verifier <name>] [--aggregator <name>] [--payment-mint <pubkey>] [--fund-kai <amount>] [--airdrop-sol <sol>] [--worker-stake ...] [--aggregate-image-id <hex>]` | Bring a `local` or `devnet` swarm to ready in one idempotent pass: wallets, SOL, stand-in mint, `initialize_protocol` with the KAI floors, funding, registration, and stake. Refused on `mainnet`. | pytest (fake chain) |
| `worker register --as <name> --role ... --capability ... --software-digest ...` | Register a worker/verifier PDA. | pytest |
| `worker show <pubkey-or-name>` | Decode worker state. | smoke |
| `worker stake <amount> --as <name>` | Deposit unlocked worker stake. | pytest |
| `worker withdraw-stake <amount> --as <name>` | Withdraw unlocked worker stake. | smoke |
| `job open ...` | Open and escrow a job. | pytest |
| `job commit-input --job <pubkey> --cid <cid> --as <customer>` | Move job from awaiting artifact to open. | pytest |
| `job show <pubkey>` | Decode job state. | pytest |
| `job list [--status ...] [--customer ...]` | List program job accounts. | smoke |
| `job claim <pubkey> --as <worker>` | Claim an open job. | pytest |
| `job submit-receipt <pubkey> --output-cid <cid> --result-bytes <hex> --as <worker>` | Submit receipt and result bytes. | pytest |
| `attest <job> --result-hash <hex> --evidence-cid <cid> --software-digest <hex-or-name> --as <verifier>` | Submit verifier attestation. | smoke |
| `settle <job>` | Settle non-aggregate completed job. | pytest |
| `settle-aggregate <job>` | Settle aggregate job using a marker PDA. | harness |
| `challenge <job> --as <verifier>` | Slash a challengeable bad receipt. | smoke |
| `refund-slashed <job>` | Refund job escrow after slash. | smoke |
| `claim-verifier-slash-reward <job> --as <verifier>` | Pay verifier slash reward. | smoke |
| `claim-customer-slash-compensation <job> --as <customer>` | Pay customer slash compensation. | smoke |
| `assign-verifier <job> --verifier <name>` | Assign the verifier that may challenge the job (any class; only the assigned verifier's `challenge_job` is accepted). Signed by the customer or the admin, before the first attestation. | harness + tier2 |
| `reassign-verifier <aggregate-job>` | Reassign after attestation timeout. | unit-covered on-chain |
| `cancel-aggregate <aggregate-job> --as <customer>` | Cancel aggregate after registry exhaustion. | unit-covered on-chain |
| `predict open --question ... --branches N ...` | Plan a prediction run, write its manifest, then open and commit every branch job and (unless deferred) the Bonsol-bound aggregate job. | pytest (fake chain) + validator |
| `predict resume <parent-run> [--as <customer>]` | Continue an interrupted `predict open` from its manifest; confirmed steps are not repeated. | pytest (fake chain) + validator |
| `predict status <parent-run>` | Show manifest and on-chain state for every job of a local prediction run. | pytest (fake chain) + validator |
| `predict report <parent-run>` | Fetch aggregate output and selected branch narratives from IPFS (reads capped by `KSWARM_IPFS_MAX_BYTES`). | tier3 |
| `predict cancel <parent-run> --as <customer>` | Cancel every job of a run that is still awaiting artifact or open, including a partially opened run, and mark the run cancelled. | pytest (fake chain) + validator |
| `inspect job <pubkey>` | Full decoded job state. | pytest |
| `inspect worker <pubkey>` | Full decoded worker state. | smoke |
| `inspect marker <execution-id> [--image-id <hex>]` | Find Bonsol marker PDAs. | harness |
| `inspect protocol-config` | Alias for protocol config inspection. | smoke |
| `inspect events --job <pubkey>` | Recent logs for job-address transactions. | smoke |
| `admin slash-stale <pubkey>` | Slash a claimed job after execution timeout. | unit-covered on-chain |
| `admin cancel-open <pubkey> --as <customer>` | Cancel open or awaiting-artifact job. | smoke |
| `admin record-aggregate-verification ...` | Debug wrapper for the raw Bonsol callback instruction. | harness |

`admin warp` was removed. It called `warpSlot`, which is not a validator RPC method, so it never worked. Wait for real slots or restart `solana-test-validator` with `--warp-slot`.

## Encoding Guarantees

The CLI encodes instructions and decodes accounts by hand; there is no IDL. The program crate is not an Anchor workspace member and does not enable `idl-build`, so `anchor idl build` cannot run against this repository. Instead, `cli/tests/test_program_layout.py` derives every instruction's argument layout, account order, and signer/writable flags, plus every account struct's field layout, from `solana/programs/kswarm_protocol/src/lib.rs` and checks the CLI against them. `cli/tests/test_bonsol_binding.py` checks the aggregate-job binding against vectors produced by `protocol/bonsol-callback-harness`.

## Instruction Coverage

| On-chain instruction | CLI wrapper |
| --- | --- |
| `initialize_protocol` | `protocol initialize` |
| `register_worker` | `worker register` |
| `deposit_worker_stake` | `worker stake` |
| `withdraw_unlocked_stake` | `worker withdraw-stake` |
| `open_job` | `job open` |
| `commit_input_artifact` | `job commit-input` |
| `claim_job` | `job claim` |
| `submit_receipt` | `job submit-receipt` |
| `submit_verifier_attestation` | `attest` |
| raw `fallback` (Bonsol callback, tag byte `1`) | `admin record-aggregate-verification`; Bonsol invokes this path. The Anchor-dispatched `record_aggregate_verification` was removed in PR-3 (IDL entry pending PR-7). |
| `assign_verifier` | `assign-verifier` |
| `reassign_verifier` | `reassign-verifier` |
| `settle_aggregate_proof_job` | `settle-aggregate` |
| `cancel_aggregate_proof_job` | `cancel-aggregate` |
| `settle_job` | `settle` |
| `challenge_job` | `challenge` |
| `refund_slashed_job_escrow` | `refund-slashed` |
| `claim_verifier_slash_reward` | `claim-verifier-slash-reward` |
| `claim_customer_slash_compensation` | `claim-customer-slash-compensation` |
| `cancel_open_job` | `admin cancel-open` |
| `slash_stale_job` | `admin slash-stale` |

Run command-level help for examples:

```bash
kswarm job open --help
kswarm worker register --help
kswarm settle-aggregate --help
kswarm predict open --help
```

## Cluster Profiles

Profiles live in `~/.config/kswarm/clusters/<name>.json` and are created on first run.

| Key | `local` | `devnet` | `mainnet` |
| --- | --- | --- | --- |
| `rpc_url` | `http://127.0.0.1:38899` | `https://api.devnet.solana.com` | `https://api.mainnet-beta.solana.com`, overridden by `SOLANA_RPC_URL` |
| `program_id` | present | present | absent until the mainnet program keypair lands |
| `payment_mint` | set by `token create-mint` or `protocol initialize` | same | KAI `CZHcDHQZerSch8Fhhi2KgV4cLiD2KtdwjJBrb8fypump` |
| `payment_decimals` | read from chain | read from chain | `6` |
| `token_program` | read from chain | read from chain | `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` |

Once the protocol is initialized, the on-chain config is authoritative for mint, token program, and decimals.

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `KSWARM_IPFS_API_URL` (or `PROTOCOL_IPFS_API_URL`) | Kubo API used by `predict open` and `predict report`; `--ipfs-api-url` overrides both. | `http://127.0.0.1:5001` |
| `KSWARM_IPFS_MAX_BYTES` | Largest IPFS artifact `predict report` will read; larger artifacts fail with `IPFS_ARTIFACT_TOO_LARGE`. | `8388608` (8 MiB) |
| `KSWARM_AGGREGATE_IMAGE_ID` | Bonsol reducer image id bound to aggregate jobs; `--aggregate-image-id` overrides it. | `kswarm_cli/reducer_image.py` |
| `SOLANA_RPC_URL` | Mainnet RPC override (see Cluster Profiles). | profile `rpc_url` |
| `KSWARM_CLUSTER` | Default for `--cluster`. | `local` |
| `KSWARM_RPC_URL` | RPC override on every cluster; `--rpc-url` still wins. | profile `rpc_url` |
| `KSWARM_PREDICT_RUNS_DIR` | Where `predict open` writes run manifests and the aggregator runner reads them. | `~/.config/kswarm/predict_runs` |

## Prediction Commands

Open a scalar prediction run:

```bash
kswarm predict open \
  --question "Will sentiment around the seeded public news item be net-negative?" \
  --output-kind scalar \
  --branches 16 \
  --combiner weighted-mean \
  --reward-per-branch 1KAI \
  --aggregator-reward 5KAI \
  --challenge-window 600 \
  --persona-set builtin-public-opinion-v1
```

The returned `parent_run` is the aggregate job pubkey.

### Run manifest and incremental opens

`predict open` works in two phases. First it plans the whole run without touching the chain: it draws a random 64-bit base nonce (branch `i` uses `base_nonce + i`, the aggregate uses `base_nonce + branches`), pins the parent manifest and every job input to IPFS, checks that none of the planned job PDAs exist, and writes the run manifest to `~/.config/kswarm/predict_runs/<parent-run>.json`. Only then does it send transactions, one job at a time (`open_job`, then `commit_input_artifact`), rewriting the manifest after every confirmed transaction (write-temp, fsync, rename).

The first line of output, on stderr, is written before the first transaction:

```text
parent_run=<aggregate-job-pubkey> base_nonce=<u64> run_manifest=<path>
```

If the command stops early (RPC failure, insufficient KAI, Ctrl-C), escrow is only locked in the jobs the manifest marks `committed` or `opened`. Continue or unwind:

```bash
kswarm predict resume <parent-run>          # checks each planned job on chain, then opens what is missing
kswarm predict cancel <parent-run> --as customer   # cancels awaiting-artifact/open jobs, marks the run cancelled
```

`predict resume` uses the wallet recorded in the manifest (`--as` must name the same pubkey) and the cluster the run was opened on. A run that is already open reports `already-open`; a cancelled run is refused. Manifests written before this scheme (schema 1) can still be inspected, reported, and cancelled, but not resumed.

`predict status` shows both the manifest status of each job (`planned`, `opened`, `committed`, `deferred`, `cancelled`) and its on-chain status (`missing` when the account does not exist).

### Combiners

`--combiner` accepts exactly the aggregator's registry: `weighted-mean` (id 1), `trimmed-mean` (id 2), `majority-vote` (id 3). Scalar combiners need `--output-kind scalar` or `narrative_with_scalar`; `majority-vote` needs `categorical`. The CLI writes `combiner_parameters` into the parent manifest, every branch input, and the aggregate input:

| Combiner | `combiner_parameters` |
| --- | --- |
| `weighted-mean` | `{}` |
| `trimmed-mean` | `{"trim_bps": N}` from `--trim-bps` (default `1000` = 10% of the branches; integer in `[0, 10000)`) |
| `majority-vote` | `{}` |

`--trim-bps` with any other combiner is an error.

### Aggregate job binding

`settle_aggregate_proof_job` only settles a job whose `required_software_digest`, `input_bundle_hash`, and `expected_result_hash` match the Bonsol marker's image id, input digest, and journal hash. `predict open` therefore binds the aggregate job at open time, the same way the callback harness does:

| Job field | Value |
| --- | --- |
| `required_software_digest` | reducer image id: `--aggregate-image-id`, else `KSWARM_AGGREGATE_IMAGE_ID`, else `kswarm_cli/reducer_image.py` |
| `input_bundle_hash` | `sha256(u64le(len) || aggregate-input.json)`: the committed input artifact, framed as the reducer reads it |
| `expected_result_hash` | `sha256(input_digest || committed_outputs)`, where `committed_outputs` mirrors the reducer guest over the same input -- **unset (all zero) when the reducer would reject the input**, see below |

The aggregate input carries the rule (`"bonsol": {"image_id", "public_input": "input-artifact", "framing": "u64le-length-prefix"}`), and the run manifest records the full binding under `bonsol`.

**The default reducer image cannot consume the aggregate input today.** `kswarm_cli/reducer_image.py` names `protocol/bonsol-branch-reducer`, whose input is one branch's `{branch_key, child_job_id, parent_request_id, line_count, word_count, score_hex}`. The aggregate artifact is a different document -- the branch job list, the combiner and its parameters -- with no `score_hex`, and since `fix/proof-binding` made `score_hex` a required BN254 field element the reducer rejects it outright. There is therefore no journal hash to write. `predict open` opens the aggregate job with `expected_result_hash` all zero, prints

```
warning: aggregate job opened UNBOUND: the reducer image <id> rejects the aggregate input
(score_hex must be a string of 64 lowercase hex digits), so expected_result_hash is unset ...
```

on stderr after the `parent_run=` line, and records `{"bound": false, "reason": ..., "image_id": ...}` under `bonsol` in the run manifest. `required_software_digest` and `input_bundle_hash` are still set: the first gates who may claim the job, the second is a hash of the CLI's own artifact and holds whatever the reducer does. Point `--aggregate-image-id` at a reducer whose input **is** the aggregate artifact and the binding is computed normally. Until such a reducer exists the aggregate job cannot be settled by `settle_aggregate_proof_job`; branch jobs are unaffected. The aggregator's Bonsol hook must execute the reducer over exactly the committed input artifact, framed with a little-endian u64 length prefix, for its marker to settle. Only a worker registered with the same software digest can claim the job (`worker register --software-digest <image-id>`).

Flagship demos use three additional `predict open` options:

| Option | Purpose |
| --- | --- |
| `--context-file <path>` | Embed a UTF-8 seed/context file in each branch input and hash-bind it in the parent manifest. |
| `--personas-file <path>` | Load a deterministic JSON persona array and assign personas to branches by branch index. |
| `--defer-aggregate-open` | Create the parent manifest and branch jobs now, but leave the aggregate job unopened (manifest status `deferred`) so a runner can open it later with its own Bonsol image, input, and journal commitments. |

Inspect the run:

```bash
kswarm predict status <parent-run>
```

Fetch the report:

```bash
kswarm predict report <parent-run>
```

Cancel jobs that have not been claimed (claimed or settled jobs are listed under `skipped_jobs`):

```bash
kswarm predict cancel <parent-run> --as customer
```

`predict report` divides `scalar_value_bps` by 10000 to produce `final_scalar` whether the aggregator wrote an integer or a float; any other value yields `null`.
