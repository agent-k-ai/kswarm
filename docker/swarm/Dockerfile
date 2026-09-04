# kswarm container images: branch-worker, verifier-worker, aggregator-runner, cli.
#
# One multi-stage build, four targets that share one runtime layer:
#
#   docker build -f docker/swarm/Dockerfile --target branch-worker -t kswarm/branch-worker .
#
# Bases are pinned by digest. Python packages come from docker/swarm/uv.lock
# (`uv sync --frozen`). The MiroFish backend is not installed: the images copy
# only backend/app/protocol, backend/app/utils/llm_client.py, and app/config.py
# onto PYTHONPATH, so no Flask app, OASIS, or Zep code ships. Every image runs
# as uid 10001 and holds no key, RPC URL, or token; those come from the
# environment and mounted files (docs/containers.md).
#
# The build context is the repository root; docker/swarm/Dockerfile.dockerignore
# allowlists what enters the context.

ARG PYTHON_IMAGE=python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc

ARG RUST_IMAGE=rust:1.90-slim-bookworm@sha256:64232e656c058f4468e8d024e990acff04f0fd5a5c0a88a574dc37773d7325c9

FROM ${UV_IMAGE} AS uv

# --- zkvm-builder: the branch canonicalization guest and the host that verifies it ---
#
# The branch worker proves, and the verifier verifies, that a branch output document
# encodes to exactly the MFB2 receipt whose hash the chain accepted. Both need this
# binary, and the guest ELF is compiled into it by `risc0-build`, so the image carries
# the program it is pinned to rather than fetching one.
#
# Every RISC Zero component is pinned to an exact version. `rzup install` with no
# version installs the newest of each, and that is how a guest image id moves without a
# source change: the id is a property of the compiled ELF, so the toolchain is part of
# the proof, not a build detail. These four versions and `protocol/zkvm-reducer/IMAGE_ID`
# are one unit; the build fails below if they stop agreeing.
FROM ${RUST_IMAGE} AS zkvm-builder

ARG RISC0_RUST_VERSION=1.88.0
ARG RISC0_CPP_VERSION=2024.1.5
ARG RISC0_R0VM_VERSION=3.0.3
ARG RISC0_CARGO_RISCZERO_VERSION=3.0.3

# `protocol/zkvm-reducer/rust-toolchain.toml` asks for `stable`, which makes rustup try
# to update the toolchain this image already pins. The host build uses the image's
# toolchain; `risc0-build` clears this variable and calls the risc0 `rustc` by path for
# the guest, so the guest is unaffected.
ENV RUSTUP_TOOLCHAIN=1.90.0 \
    CARGO_TERM_COLOR=never \
    PATH=/root/.risc0/bin:/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      curl \
      pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://risczero.com/install | bash \
    && rzup install rust "${RISC0_RUST_VERSION}" \
    && rzup install cpp "${RISC0_CPP_VERSION}" \
    && rzup install r0vm "${RISC0_R0VM_VERSION}" \
    && rzup install cargo-risczero "${RISC0_CARGO_RISCZERO_VERSION}" \
    && rzup show

# The shared reducer crate is a path dependency of both the guest and the host, so one
# definition of the journal layout reaches guest, host and the Python daemons alike.
COPY protocol/bonsol-aggregate-reducer /src/protocol/bonsol-aggregate-reducer
COPY protocol/zkvm-reducer /src/protocol/zkvm-reducer

WORKDIR /src/protocol/zkvm-reducer
RUN cargo build --release --locked -p host

# The id this build produced must be the id the repository pins, or a verifier running
# this image would refuse every receipt a worker running it produced.
#
# `IMAGE_ID` sits inside the copied crate, so editing it invalidates the build layer
# above. That is the right trade: the pin changes when the guest changes, and when the
# guest changes the guest has to be rebuilt anyway. Only reconciling the pin without a
# source change pays for a rebuild it did not need, and that happens once.
RUN set -eu; \
    mkdir -p /out/bin /out/share; \
    install -m 0755 target/release/host /out/bin/kswarm-zkvm-reducer; \
    built="$(/out/bin/kswarm-zkvm-reducer image-id)"; \
    pinned="$(tr -d '[:space:]' < IMAGE_ID)"; \
    if [ "${built}" != "${pinned}" ]; then \
      echo "guest image id drift: built ${built}, protocol/zkvm-reducer/IMAGE_ID pins ${pinned}" >&2; \
      echo "the pinned RISC Zero components or the guest source changed; see protocol/zkvm-reducer/IMAGE_ID.md" >&2; \
      exit 1; \
    fi; \
    printf '%s\n' "${built}" > /out/share/kswarm-zkvm-image-id; \
    echo "branch canonicalization guest image id ${built}"

# --- builder: locked virtualenv ------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/kswarm/venv

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /opt/kswarm/src/docker/swarm

# Third-party dependencies first: this layer only changes when the lock changes.
COPY docker/swarm/pyproject.toml docker/swarm/uv.lock ./
COPY cli/pyproject.toml /opt/kswarm/src/cli/pyproject.toml
COPY worker/pyproject.toml /opt/kswarm/src/worker/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-package kswarm-cli --no-install-package kswarm-worker

# Then our own packages (editable, sources stay under /opt/kswarm/src).
COPY cli /opt/kswarm/src/cli
COPY worker /opt/kswarm/src/worker
RUN uv sync --frozen --no-dev

# --- runtime: shared by every target -------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG GIT_SHA=unknown
ARG GIT_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
# The repository the image was built from. No host name is baked into this file:
# the defaults name the public repository, and the release workflow overrides them
# with the repository it is actually running in, derived from GITHUB_SERVER_URL
# and GITHUB_REPOSITORY.
ARG SOURCE_URL=https://github.com/agent-k-ai/kswarm
ARG VENDOR=kswarm

ENV PATH=/opt/kswarm/venv/bin:${PATH} \
    PYTHONPATH=/opt/kswarm/engine \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/kswarm \
    KSWARM_PREDICT_RUNS_DIR=/var/lib/kswarm/predict_runs

RUN groupadd --gid 10001 kswarm \
    && useradd --uid 10001 --gid 10001 --home-dir /home/kswarm --create-home --shell /usr/sbin/nologin kswarm \
    && mkdir -p /home/kswarm/.config/kswarm /var/lib/kswarm/predict_runs \
    && chown -R kswarm:kswarm /home/kswarm /var/lib/kswarm

COPY --from=builder --chown=kswarm:kswarm /opt/kswarm /opt/kswarm

# The engine subset the daemons import (see the header). app/utils gets an empty
# package init: the file parser and locale helpers of the Flask app stay out.
COPY --chown=kswarm:kswarm backend/app/__init__.py backend/app/config.py /opt/kswarm/engine/app/
COPY --chown=kswarm:kswarm backend/app/protocol /opt/kswarm/engine/app/protocol
COPY --chown=kswarm:kswarm backend/app/utils/llm_client.py /opt/kswarm/engine/app/utils/llm_client.py
RUN install -o kswarm -g kswarm -m 0644 /dev/null /opt/kswarm/engine/app/utils/__init__.py

USER 10001:10001
WORKDIR /home/kswarm

# Every entry point and every engine module the daemons import must resolve in
# this image; the build fails here if the subset above is incomplete.
RUN python -c "import branch_worker.cli, verifier_worker.cli, aggregator_runner.cli, kswarm_cli.main, app.protocol.branch_schemas, app.protocol.canonical_hash, app.utils.llm_client"

LABEL org.opencontainers.image.title="kswarm" \
      org.opencontainers.image.description="Solana + IPFS prediction swarm: branch worker, verifier, aggregator, operator CLI" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.ref.name="${GIT_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      org.opencontainers.image.vendor="${VENDOR}"

# --- targets ---------------------------------------------------------------------

# The two images on the proof path. The aggregator and the CLI do not carry the
# prover: the aggregator's proof is produced by a Bonsol node, and the CLI proves
# nothing, so neither needs a 150 MB binary or the attack surface of one.
FROM runtime AS proving-runtime
USER root
COPY --from=zkvm-builder --chown=root:root /out/bin/kswarm-zkvm-reducer /opt/kswarm/bin/kswarm-zkvm-reducer
COPY --from=zkvm-builder --chown=root:root /out/share/kswarm-zkvm-image-id /opt/kswarm/share/kswarm-zkvm-image-id
RUN chmod 0755 /opt/kswarm/bin/kswarm-zkvm-reducer && chmod 0644 /opt/kswarm/share/kswarm-zkvm-image-id
USER 10001:10001
# Proving and verifying are on by default in these images, and the guest they accept
# is the one this image was built with. An operator can still point either variable
# somewhere else, or unset KSWARM_ZKVM_HOST to turn the proof path off.
ENV KSWARM_ZKVM_HOST=/opt/kswarm/bin/kswarm-zkvm-reducer \
    KSWARM_ZKVM_IMAGE_ID_FILE=/opt/kswarm/share/kswarm-zkvm-image-id
# The binary must run, and it must be the guest the file names, before anything ships.
RUN test "$(/opt/kswarm/bin/kswarm-zkvm-reducer image-id)" = "$(cat /opt/kswarm/share/kswarm-zkvm-image-id)"

FROM proving-runtime AS branch-worker
ENV KSWARM_WORKER_METRICS_HOST=0.0.0.0 \
    KSWARM_WORKER_METRICS_PORT=9461
EXPOSE 9461
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9461/metrics', timeout=3)"]
ENTRYPOINT ["kswarm-branch-worker"]

FROM proving-runtime AS verifier-worker
ENV KSWARM_WORKER_METRICS_HOST=0.0.0.0 \
    KSWARM_WORKER_METRICS_PORT=9462
EXPOSE 9462
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9462/metrics', timeout=3)"]
ENTRYPOINT ["kswarm-verifier-worker"]

FROM runtime AS aggregator-runner
ENV KSWARM_WORKER_METRICS_HOST=0.0.0.0 \
    KSWARM_WORKER_METRICS_PORT=9463
EXPOSE 9463
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9463/metrics', timeout=3)"]
ENTRYPOINT ["kswarm-aggregator-runner"]

FROM runtime AS cli
# One-shot operator commands; nothing listens, so there is no health probe.
HEALTHCHECK NONE
ENTRYPOINT ["kswarm"]
CMD ["--help"]
