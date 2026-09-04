ARG BONSOL_COMMIT=25a590d09cca0404cc48ec028122df4d1a8c651b

# The RISC Zero components are not named here. `protocol/risc0-toolchain.env` declares
# them once and `scripts/install-risc0-toolchain.sh` installs exactly those, which is
# the same declaration and the same script `docker/swarm` and `docker/protocol-node`
# use. `rzup install` with no version installs the newest of each, and a guest ELF is a
# function of its toolchain, so an unpinned install silently moves every image id this
# container builds -- that is what moved the branch reducer's recorded id without a
# source change. Four literals in four Dockerfiles fixes that and replaces it with the
# drift between the four, which is the defect this file no longer carries.
#
# The guest builds themselves run inside `risczero/risc0-guest-builder`, pinned by
# digest in the same declaration and read from it by
# `protocol/scripts/run-bonsol-builder.sh`, which sees the repository this image is
# building. The pin therefore no longer has to travel inside the image.

# docker.io/risczero/risc0-groth16-prover:v2024-05-17.1, pinned by digest.
FROM docker.io/risczero/risc0-groth16-prover:v2024-05-17.1@sha256:5a862bac2c5c070ec50ff615572a05d870c1372818cf0f5d8bb9effc101590c8 AS prover

# ghcr.io/bonsol-collective/bonsol-forge:0.1.1, pinned by digest.
# This image stays root on purpose: the upstream forge base installs Rust, Solana, and risc0
# under /root, `bonsol build` drives the host Docker daemon for guest builds, and the smoke
# tests share the resulting keypairs with the builder. Converting it to an unprivileged user
# needs a tier-2 run on the self-hosted runner and is tracked as follow-up work. It is an
# eval-only image and is never deployed.
FROM ghcr.io/bonsol-collective/bonsol-forge:0.1.1@sha256:f14b35aa72333d03b2fbd288d32b82a51cf253e9d8b0aac32701dc6de6190949

ARG BONSOL_COMMIT

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/opt/bonsol/bin:/root/.risc0/bin:/root/.local/share/solana/install/active_release/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    docker.io \
    git \
    jq \
    && rm -rf /var/lib/apt/lists/*

COPY protocol/risc0-toolchain.env /build/protocol/risc0-toolchain.env
COPY scripts/install-risc0-toolchain.sh /build/scripts/install-risc0-toolchain.sh
RUN /build/scripts/install-risc0-toolchain.sh

WORKDIR /opt
RUN git clone https://github.com/bonsol-collective/bonsol /opt/bonsol-src \
    && cd /opt/bonsol-src \
    && git checkout "${BONSOL_COMMIT}" \
    && cargo build --release -p bonsol-cli -p local-zk-program-server -p bonsol-node

RUN mkdir -p /opt/bonsol/bin /opt/bonsol/stark /opt/bonsol/keys /opt/bonsol/risc0_images \
    && cp /opt/bonsol-src/target/release/bonsol /opt/bonsol/bin/ \
    && cp /opt/bonsol-src/target/release/local-zk-program-server /opt/bonsol/bin/ \
    && cp /opt/bonsol-src/target/release/bonsol-node /opt/bonsol/bin/

COPY --from=prover /app/stark_verify /opt/bonsol/stark/
COPY --from=prover /usr/local/sbin/rapidsnark /opt/bonsol/stark/
COPY --from=prover /app/prover.sh /opt/bonsol/stark/
COPY --from=prover /app/stark_verify.dat /opt/bonsol/stark/
COPY --from=prover /app/stark_verify_final.zkey /opt/bonsol/stark/

WORKDIR /app
COPY protocol /app/protocol
COPY docker/bonsol-eval /app/docker/bonsol-eval
