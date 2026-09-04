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

FROM ${UV_IMAGE} AS uv

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
FROM runtime AS branch-worker
ENV KSWARM_WORKER_METRICS_HOST=0.0.0.0 \
    KSWARM_WORKER_METRICS_PORT=9461
EXPOSE 9461
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9461/metrics', timeout=3)"]
ENTRYPOINT ["kswarm-branch-worker"]

FROM runtime AS verifier-worker
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
