# node:22-bookworm, pinned by digest; the tag is recorded for humans. One value, used
# by both stages below, so the builder and the runtime cannot drift onto different bases.
ARG NODE_IMAGE=node:22-bookworm@sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d

# --- zkvm-builder: the branch canonicalization guest ------------------------------
#
# Compiled by the same two scripts `docker/swarm/Dockerfile` uses, from the same
# declaration in `protocol/risc0-toolchain.env`, so both images produce the same guest
# and a verifier built from either accepts receipts from a worker running the other.
# Nothing here names a component version or a build path: `scripts/build-zkvm-guest.sh`
# owns both, because the guest's image id is a function of them
# (`protocol/zkvm-reducer/IMAGE_ID.md`).
#
# It is a stage of its own, and it runs as root, for one reason: the pinned id was
# recorded from a build under `$HOME=/root`, and the guest carries that path in the panic
# locations of every registry dependency. The stage is discarded; the image below keeps
# its unprivileged `node` user and receives only the binary and the id.
FROM ${NODE_IMAGE} AS zkvm-builder

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/root/.risc0/bin:/root/.cargo/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# No default toolchain: `protocol/zkvm-reducer/rust-toolchain.toml` names the one this
# build uses, and naming it a second time here is exactly the kind of duplicate that
# drifts.
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal --default-toolchain none

COPY protocol/risc0-toolchain.env /build/protocol/risc0-toolchain.env
COPY scripts/install-risc0-toolchain.sh scripts/build-zkvm-guest.sh /build/scripts/
RUN /build/scripts/install-risc0-toolchain.sh

COPY protocol/bonsol-aggregate-reducer /build/protocol/bonsol-aggregate-reducer
COPY protocol/zkvm-reducer /build/protocol/zkvm-reducer

# Builds the guest and the host that carries it, and fails when the id it produced is not
# the one `protocol/zkvm-reducer/IMAGE_ID` pins.
RUN /build/scripts/build-zkvm-guest.sh /build /out

# --- runtime ------------------------------------------------------------------------
FROM ${NODE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    git \
    libssl-dev \
    libudev-dev \
    pkg-config \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Every service in this image runs as the image's built-in unprivileged `node` user (uid 1000).
# Directories under /runtime exist here so named volumes inherit that ownership on first use;
# bind mounts (runtime/protocol, runtime/ipfs) must be owned by uid 1000 on the host.
# /keys is the read-only mount point for deploy keys kept outside the repository.
RUN mkdir -p /app /opt/kswarm/bin /opt/kswarm/share /opt/kswarm/build \
        /runtime/protocol /runtime/program-artifacts /runtime/solana-ledger /keys \
    && chown -R node:node /app /opt/kswarm /runtime /keys

USER node
ENV HOME=/home/node
ENV PATH="/home/node/.risc0/bin:/home/node/.cargo/bin:/home/node/.local/share/solana/install/active_release/bin:${PATH}"

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal --default-toolchain 1.94.1
RUN sh -c "$(curl -sSfL https://release.anza.xyz/v2.3.11/install)"

# The RISC Zero components this image carries for interactive and harness use, installed
# from the same declaration the builder stage above uses, so the two can never name
# different versions. The guest itself is not compiled here: it comes from that stage, at
# the path and `$HOME` its image id was recorded from.
COPY --chown=node:node protocol/risc0-toolchain.env /opt/kswarm/build/protocol/risc0-toolchain.env
COPY --chown=node:node scripts/install-risc0-toolchain.sh /opt/kswarm/build/scripts/install-risc0-toolchain.sh
RUN /opt/kswarm/build/scripts/install-risc0-toolchain.sh

ENV PROTOCOL_ZKVM_REDUCER_BIN="/opt/kswarm/bin/zkvm-reducer"
ENV RISC0_PROVER="local"
ENV RISC0_EXECUTOR="local"

WORKDIR /app

COPY --chown=node:node protocol/package.json /app/protocol/package.json
WORKDIR /app/protocol
RUN npm install

WORKDIR /app
COPY --chown=node:node protocol /app/protocol
COPY --chown=node:node solana /app/solana
COPY --chown=node:node docker/ipfs /app/docker/ipfs

# A verifier that trusts one binary's id must get the same one from every image that
# builds it, so the binary arrives with the id of the guest inside it and the image
# checks that the two agree before anything ships.
COPY --from=zkvm-builder --chown=node:node /out/bin/kswarm-zkvm-reducer /opt/kswarm/bin/zkvm-reducer
COPY --from=zkvm-builder --chown=node:node /out/share/kswarm-zkvm-image-id /opt/kswarm/share/kswarm-zkvm-image-id
RUN test "$("${PROTOCOL_ZKVM_REDUCER_BIN}" image-id)" = "$(cat /opt/kswarm/share/kswarm-zkvm-image-id)"

WORKDIR /app/protocol
