# `IMAGE_ID`

The RISC Zero image id of the branch canonicalization guest in
`methods/guest`, as `host image-id` prints it.

It is what a verifier pins: a receipt naming any other guest is refused, so this
value decides which program a branch receipt is allowed to have come from. It is
a property of the compiled ELF, so the guest source, the shared reducer crate,
both lockfiles and the pinned RISC Zero toolchain all reach it.

`scripts/build-zkvm-guest.sh` is the only thing that compiles it. Every image
that carries the guest calls that script, and the script fails when the id it
built is not the one in this file, so the id cannot drift silently. The same
file is installed next to the binary in those images and is the default
`KSWARM_ZKVM_IMAGE_ID` when the environment does not set one.

## What reaches the id

Measured on 2026-09-04 by building the guest eleven times, varying one input at
a time, from one copy of the guest source; only `rust-toolchain.toml` differs
between runs, and only in the last three. Every run installed the same RISC Zero
components (rust 1.88.0, cpp 2024.1.5, r0vm 3.0.3, cargo-risczero 3.0.3) on one
base image. Ten distinct configurations, and the eleventh run repeats the third
and reproduces its ELF byte for byte.

| source root | `$HOME` | host toolchain | guest cargo | image id |
| --- | --- | --- | --- | --- |
| `/src` | `/root` | 1.90.0 | 1.98.1 | `e73c537a…` |
| `/src` | `/root` | 1.94.1 | 1.98.1 | `e73c537a…` |
| `/src` | `/root` | stable 1.98.1 | 1.98.1 | `e73c537a…` |
| `/src` | `/root` | 1.90.0, `CARGO_HOME=/home/node/.cargo` | 1.98.1 | `e73c537a…` |
| `/src` | `/root` | 1.90.0 | 1.90.0 | `e73c537a…` |
| `/src` | `/root` | 1.94.1 | 1.94.1 | `e73c537a…` |
| `/app` | `/root` | 1.90.0 | 1.98.1 | `689059e4…` |
| `/src2` | `/root` | 1.90.0 | 1.98.1 | `d1e906b0…` |
| `/src` | `/home/node` | 1.90.0 | 1.98.1 | `11f19b3b…` |
| `/app` | `/home/node` | 1.90.0 | 1.98.1 | `cc5b1955…` |

So:

- **The Rust toolchain does not reach the guest at all**, neither the one the
  workspace is built with nor the one the guest's own cargo runs as.
  `risc0-build` sets `RUSTC` to the rzup RISC Zero rustc and compiles the guest
  with that, and it strips `RUSTUP_TOOLCHAIN` from the guest's cargo. Three host
  toolchains and three guest cargos each produce a byte-identical ELF.
- **`CARGO_HOME` does not reach it either**, because `risc0-build` removes every
  `CARGO*` variable before invoking the guest build.
- **The absolute path of the source tree does**, and so does **`$HOME`**. The
  guest carries `core::panic::Location` strings: the path of its own crate and of
  `protocol/bonsol-aggregate-reducer` come from where the tree sits, and the path
  of every registry dependency is `$HOME/.cargo/registry/...`, because with
  `CARGO_HOME` stripped the guest's cargo falls back to `$HOME`.

That is why the two values are declared in `protocol/risc0-toolchain.env` beside
the component versions, and why `scripts/build-zkvm-guest.sh` copies the crates
to `ZKVM_GUEST_BUILD_ROOT` and sets `$HOME` itself rather than trusting the
caller. Before it existed, `docker/swarm/Dockerfile` built at `/src` as root and
`docker/protocol-node/Dockerfile` built at `/app` as `node`, and the second image
could not be built at all: it produced `cc5b1955…` and failed its own assertion.

The id is therefore reproducible, but it is not path-independent. Making it so
means remapping those paths out of the ELF, which changes the id, which retires
every receipt the current guest produced; that is a guest version bump and not a
build fix.

## Changing it

Rebuild through `scripts/build-zkvm-guest.sh`, take the id the build reports, put
it here, and say in `docs/proof-layer-status.md` what changed in the guest.
Changing it retires every receipt the previous guest produced.
