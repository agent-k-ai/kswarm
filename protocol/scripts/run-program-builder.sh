#!/bin/bash
set -euo pipefail

# The repository path is the same inside this container and on the host (compose passes ${PWD})
# because solana-verify mounts it into a sibling container by absolute path.
REPO_DIR="${PROTOCOL_REPO_DIR:?set PROTOCOL_REPO_DIR to the repository path (same inside and outside the container)}"
WORKSPACE_DIR="${REPO_DIR}/solana"
ARTIFACT_DIR="${PROTOCOL_PROGRAM_ARTIFACT_DIR:-/runtime/program-artifacts}"
ARTIFACT_PATH="${ARTIFACT_DIR}/kswarm_protocol.so"
BUILD_INFO_PATH="${ARTIFACT_DIR}/kswarm_protocol.build.json"

mkdir -p "${ARTIFACT_DIR}"
rm -f "${ARTIFACT_PATH}" "${BUILD_INFO_PATH}"

echo "[program-builder] waiting for docker socket"
until docker version >/dev/null 2>&1; do
  sleep 1
done

echo "[program-builder] building verified Solana program"
cd "${WORKSPACE_DIR}"
solana-verify build \
  --library-name kswarm_protocol \
  --workspace-path "${WORKSPACE_DIR}" \
  "${WORKSPACE_DIR}"

echo "[program-builder] copying verified artifact"
cp "${WORKSPACE_DIR}/target/deploy/kswarm_protocol.so" "${ARTIFACT_PATH}"
sha256sum "${ARTIFACT_PATH}" | awk '{print $1}' | {
  read -r artifact_sha256
  cat > "${BUILD_INFO_PATH}" <<EOF
{
  "artifact_path": "${ARTIFACT_PATH}",
  "artifact_sha256": "${artifact_sha256}"
}
EOF
}

echo "[program-builder] artifact ready at ${ARTIFACT_PATH}"
tail -f /dev/null
