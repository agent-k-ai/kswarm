ARG BONSOL_COMMIT=25a590d09cca0404cc48ec028122df4d1a8c651b

# RISC Zero components, pinned to exact versions. `rzup install` with no version
# installs the newest of each, and a guest ELF is a function of its toolchain, so an
# unpinned install silently moves every image id this container builds. That is what
# moved the branch reducer's recorded id without a source change.
#
# These match the `risc0-zkvm =3.0.3` line the protocol crates pin. `rust 1.88.0` is the
# guest toolchain for that line; the guest builds themselves run inside
# `risczero/risc0-guest-builder`, which `protocol/scripts/run-bonsol-builder.sh` pins by
# digest for the same reason.
ARG RISC0_RUST_VERSION=1.88.0
ARG RISC0_CPP_VERSION=2024.1.5
ARG RISC0_R0VM_VERSION=3.0.3
ARG RISC0_CARGO_RISCZERO_VERSION=3.0.3

# `risczero/risc0-guest-builder:r0.1.88.0`, the tag `risc0-build 3.0.3` defaults to,
# pinned by digest. The tag is mutable upstream.
ARG RISC0_GUEST_BUILDER_DIGEST=sha256:3e12f71bacd27527a61dea96fa0e53e468c99aa261d3a1019b593f6dbd943eb3

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
ARG RISC0_RUST_VERSION
ARG RISC0_CPP_VERSION
ARG RISC0_R0VM_VERSION
ARG RISC0_CARGO_RISCZERO_VERSION
ARG RISC0_GUEST_BUILDER_DIGEST

ENV DEBIAN_FRONTEND=noninteractive
# Read by `protocol/scripts/run-bonsol-builder.sh`, so the pin travels with the image
# that uses it rather than being repeated in the script.
ENV RISC0_GUEST_BUILDER_DIGEST=${RISC0_GUEST_BUILDER_DIGEST}
ENV PATH="/opt/bonsol/bin:/root/.risc0/bin:/root/.local/share/solana/install/active_release/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    docker.io \
    git \
    jq \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://risczero.com/install | bash \
    && /root/.risc0/bin/rzup install rust "${RISC0_RUST_VERSION}" \
    && /root/.risc0/bin/rzup install cpp "${RISC0_CPP_VERSION}" \
    && /root/.risc0/bin/rzup install r0vm "${RISC0_R0VM_VERSION}" \
    && /root/.risc0/bin/rzup install cargo-risczero "${RISC0_CARGO_RISCZERO_VERSION}" \
    && /root/.risc0/bin/rzup show

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
