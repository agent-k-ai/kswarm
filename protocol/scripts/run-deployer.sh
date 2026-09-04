#!/bin/bash
# Control-plane deployer for docker-compose.protocol.yml.
#
# 1. Runtime wallets: random keys generated once per deployment into ${PROTOCOL_SHARED_DIR}
#    with mode 0600. Seed-derived keys need KSWARM_INSECURE_LOCALNET_SEEDS=1 and
#    SOLANA_CLUSTER=localnet; any other cluster refuses the flag.
# 2. SOL funding: airdrop on localnet, otherwise the admin wallet must be pre-funded.
# 3. Program: deployed only when no executable account exists at the declared id. That needs
#    PROTOCOL_PROGRAM_KEYPAIR and PROTOCOL_UPGRADE_AUTHORITY_KEYPAIR, both files kept outside
#    the repository (the compose file mounts ${PROTOCOL_KEYS_HOST_DIR} read-only at /keys).
#    On localnet the validator loads the program at genesis, so this step is skipped there.
# 4. On-chain config bootstrap and protocol.json.
set -euo pipefail

ROOT_DIR="/app"
SHARED_DIR="${PROTOCOL_SHARED_DIR:-/runtime/protocol}"
RPC_URL="${PROTOCOL_RPC_URL:-http://solana-validator:8899}"
PROGRAM_ARTIFACT_DIR="${PROTOCOL_PROGRAM_ARTIFACT_DIR:-/runtime/program-artifacts}"
PROGRAM_SO="${PROGRAM_ARTIFACT_DIR}/kswarm_protocol.so"
DEPLOYED_MARKER="${SHARED_DIR}/deployed"
AUTO_FUND_SOL="${PROTOCOL_AUTO_FUND_SOL:-1}"
BOOTSTRAP_LOCAL_MINT="${PROTOCOL_BOOTSTRAP_LOCAL_MINT:-0}"
PROGRAM_KEYPAIR="${PROTOCOL_PROGRAM_KEYPAIR:-}"
UPGRADE_AUTHORITY_KEYPAIR="${PROTOCOL_UPGRADE_AUTHORITY_KEYPAIR:-}"

die() {
  echo "[deployer] error: $*" >&2
  exit 1
}

# A key file must exist and be readable only by its owner. The path is printed; the key never is.
require_private_key_file() {
  local label="$1"
  local file="$2"
  [ -n "${file}" ] || die "${label} is not set; point it at a keypair file kept outside the repository"
  [ -f "${file}" ] || die "${label} points to a missing file: ${file}"
  local mode
  mode="$(stat -c '%a' "${file}")"
  case "${mode}" in
    600 | 400) ;;
    *) die "${label} file ${file} is readable by group or others (mode ${mode}); run: chmod 600 ${file}" ;;
  esac
}

(umask 077 && mkdir -p "${SHARED_DIR}")
rm -f "${SHARED_DIR}/protocol.json" "${SHARED_DIR}/ready" "${DEPLOYED_MARKER}"

cd "${ROOT_DIR}"
echo "[deployer] preparing runtime wallets in ${SHARED_DIR}"
node protocol/scripts/write-runtime-keypairs.mjs

until solana -u "${RPC_URL}" block-height >/dev/null 2>&1; do
  sleep 1
done

if [ "${AUTO_FUND_SOL}" = "1" ]; then
  echo "[deployer] funding runtime wallets"
  node protocol/scripts/fund-runtime-wallets.mjs
else
  echo "[deployer] skipping SOL wallet funding"
fi

if [ "${BOOTSTRAP_LOCAL_MINT}" = "1" ]; then
  echo "[deployer] creating local stand-in payment mint (classic SPL, 6 decimals)"
  node protocol/scripts/bootstrap-local-mint.mjs
fi

PROGRAM_ID="$(node protocol/scripts/program-status.mjs --print-id)"
echo "[deployer] checking for program ${PROGRAM_ID} on ${RPC_URL}"
if node protocol/scripts/program-status.mjs; then
  echo "[deployer] program ${PROGRAM_ID} is already deployed; skipping deploy"
else
  status=$?
  [ "${status}" -eq 3 ] || die "could not query program status (exit ${status})"
  require_private_key_file PROTOCOL_PROGRAM_KEYPAIR "${PROGRAM_KEYPAIR}"
  require_private_key_file PROTOCOL_UPGRADE_AUTHORITY_KEYPAIR "${UPGRADE_AUTHORITY_KEYPAIR}"
  keypair_id="$(solana-keygen pubkey "${PROGRAM_KEYPAIR}")"
  [ "${keypair_id}" = "${PROGRAM_ID}" ] || die "PROTOCOL_PROGRAM_KEYPAIR derives to ${keypair_id} but the program declares ${PROGRAM_ID}"

  until test -f "${PROGRAM_SO}"; do
    sleep 1
  done

  echo "[deployer] deploying program ${PROGRAM_ID} with upgrade authority $(solana-keygen pubkey "${UPGRADE_AUTHORITY_KEYPAIR}")"
  solana -u "${RPC_URL}" program deploy \
    -k "${UPGRADE_AUTHORITY_KEYPAIR}" \
    --fee-payer "${UPGRADE_AUTHORITY_KEYPAIR}" \
    --upgrade-authority "${UPGRADE_AUTHORITY_KEYPAIR}" \
    --program-id "${PROGRAM_KEYPAIR}" \
    "${PROGRAM_SO}"
fi

# Protocol initialization and protocol.json are the Python CLI's job now
# (docker/swarm/protocol-bootstrap.sh in the protocol-bootstrap service).
echo "[deployer] program deployed; handing over to protocol-bootstrap"
touch "${DEPLOYED_MARKER}"

tail -f /dev/null
