# node:22-bookworm, pinned by digest; the tag is recorded for humans.
FROM node:22-bookworm@sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d

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
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Every service in this image runs as the image's built-in unprivileged `node` user (uid 1000).
# Directories under /runtime exist here so named volumes inherit that ownership on first use;
# bind mounts (runtime/protocol, runtime/ipfs) must be owned by uid 1000 on the host.
# /keys is the read-only mount point for deploy keys kept outside the repository.
RUN mkdir -p /app /opt/ezkl-venv /opt/kswarm-proofs /opt/kswarm/bin \
        /runtime/protocol /runtime/program-artifacts /runtime/solana-ledger /keys \
    && chown -R node:node /app /opt/ezkl-venv /opt/kswarm-proofs /opt/kswarm /runtime /keys

USER node
ENV HOME=/home/node
ENV PATH="/home/node/.cargo/bin:/home/node/.local/share/solana/install/active_release/bin:${PATH}"

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal --default-toolchain 1.94.1
RUN sh -c "$(curl -sSfL https://release.anza.xyz/v2.3.11/install)"
RUN cargo install rzup --version 0.6.0-rc.1 --locked
# risc0 toolchain pinned to the 3.0.3 line that the Bonsol path uses (risc0-zkvm =3.0.3 in
# protocol/bonsol-branch-reducer and the callback harness). An unpinned `rzup install` takes
# the latest of every default component (cargo-risczero, cpp, r0vm, rust), so each one is
# named with its version here.
RUN rzup install rust 1.88.0 \
    && rzup install cpp 2024.1.5 \
    && rzup install cargo-risczero 3.0.3 \
    && rzup install r0vm 3.0.3

ENV PROTOCOL_EZKL_VENV="/opt/ezkl-venv"
ENV PROTOCOL_EZKL_ASSETS_DIR="/opt/kswarm-proofs/ezkl-linear"
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

RUN python3 -m venv "${PROTOCOL_EZKL_VENV}" \
    && "${PROTOCOL_EZKL_VENV}/bin/pip" install --no-cache-dir ezkl==23.0.5 onnx==1.18.0 numpy==2.2.4 \
    && "${PROTOCOL_EZKL_VENV}/bin/python" /app/protocol/proofs/ezkl/prepare_assets.py --output-dir "${PROTOCOL_EZKL_ASSETS_DIR}"

# `--locked` and the id assertion for the same reason `docker/swarm/Dockerfile` has
# them: this image's binary carries a guest ELF, and a verifier that trusts one binary's
# id must get the same one from every image that builds it. The RISC Zero components
# above are already pinned to the versions that produce it.
RUN cd /app/protocol/zkvm-reducer \
    && cargo build --release --locked -p host \
    && cp /app/protocol/zkvm-reducer/target/release/host "${PROTOCOL_ZKVM_REDUCER_BIN}" \
    && built="$("${PROTOCOL_ZKVM_REDUCER_BIN}" image-id)" \
    && pinned="$(tr -d '[:space:]' < /app/protocol/zkvm-reducer/IMAGE_ID)" \
    && if [ "${built}" != "${pinned}" ]; then \
         echo "guest image id drift: built ${built}, protocol/zkvm-reducer/IMAGE_ID pins ${pinned}" >&2; \
         exit 1; \
       fi \
    && echo "branch canonicalization guest image id ${built}"

WORKDIR /app/protocol
