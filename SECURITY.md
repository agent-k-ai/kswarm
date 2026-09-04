# Security policy

## Status

kswarm is **pre-release**. The Solana program has not been audited, nothing is
deployed to mainnet, and the trust layer does not yet prove what a reader might
assume it proves. [docs/proof-layer-status.md](docs/proof-layer-status.md) is the
honest account; read it before integrating.

## Reporting a vulnerability

Report vulnerabilities through GitHub private vulnerability reporting on this
repository (Security tab -> Report a vulnerability). Do not open a public issue
for a security problem.

Include, as far as you have it:

- which component: branch worker, verifier worker, aggregator runner, the
  `kswarm` CLI, the Node control plane, a container image, or the engine subset
  under `backend/app/`;
- the preconditions an attacker needs;
- the impact: funds moved, stake released, a forged or unattributable result, key
  material exposed, or an operator machine compromised;
- a reproduction, ideally a failing test.

Vulnerabilities in the on-chain program belong in the
[kswarm-protocol](https://github.com/agent-k-ai/kswarm-protocol) repository, but
a report sent here will be routed rather than bounced.

## What to expect

- Acknowledgement that the report arrived, and whether it is being treated as a
  vulnerability.
- An assessment, and a fix or a written reason for not fixing.
- Credit in the release notes if you want it.

There is no bug bounty.

## Operator hygiene

Two things account for most realistic loss in a stack like this one, and neither
is a code vulnerability:

- **Keys.** Worker, verifier and aggregator daemons sign with hot keys. Keep the
  wallet directory owner-only (`0700`, key files `0600`, which the CLI enforces
  and refuses to load otherwise), and keep the program upgrade authority
  somewhere a running daemon cannot reach.
- **Endpoints.** The LLM endpoint and the IPFS API are trusted by the daemons.
  Do not expose the Kubo API port to a network you do not control.

`scripts/check-no-secrets.sh` fails on key material anywhere in the git index and
runs first in CI. Run it before you push.
