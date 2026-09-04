#!/bin/sh
# Protocol bootstrap for docker-compose.protocol.yml, run inside the `cli` image.
#
# Waits for the Node deployer (`deployed` marker), initializes the protocol with
# the Python CLI as the deployer's admin key, then writes protocol.json and the
# `ready` marker that protocol-api and protocol-watcher wait for. Reruns are safe:
# `protocol initialize` reports `already-initialized` and the file is rewritten
# from chain.
#
# Environment: PROTOCOL_SHARED_DIR, PROTOCOL_RPC_URL, PROTOCOL_ARTIFACT_GATEWAY_URL,
# PROTOCOL_PAYMENT_MINT (else payment-mint.json from the deployer's local mint),
# PROTOCOL_STAKE_FLOORS, PROTOCOL_VERIFIER_STAKE_FLOOR, KSWARM_CLUSTER.
set -eu

shared_dir="${PROTOCOL_SHARED_DIR:-/runtime/protocol}"
rpc_url="${PROTOCOL_RPC_URL:-http://solana-validator:8899}"
gateway_url="${PROTOCOL_ARTIFACT_GATEWAY_URL:-http://protocol-api:7001}"
floors="${PROTOCOL_STAKE_FLOORS:-50000,250000,1000000}"
verifier_floor="${PROTOCOL_VERIFIER_STAKE_FLOOR:-100000}"
cluster="${KSWARM_CLUSTER:-local}"
admin_keypair="${shared_dir}/admin.json"
mint_file="${shared_dir}/payment-mint.json"

log() {
  printf '[protocol-bootstrap] %s\n' "$*" >&2
}

rm -f "${shared_dir}/ready"

log "waiting for the deployer"
until [ -f "${shared_dir}/deployed" ] && [ -f "${admin_keypair}" ]; do
  sleep 2
done

if [ -n "${PROTOCOL_PAYMENT_MINT:-}" ]; then
  mint="${PROTOCOL_PAYMENT_MINT}"
elif [ -f "${mint_file}" ]; then
  mint="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mint"])' "${mint_file}")"
else
  log "no payment mint: set PROTOCOL_PAYMENT_MINT or PROTOCOL_BOOTSTRAP_LOCAL_MINT=1 on the deployer"
  exit 2
fi

log "initializing the protocol on ${rpc_url} with mint ${mint}"
kswarm --json --cluster "${cluster}" --rpc-url "${rpc_url}" --keypair "${admin_keypair}" \
  protocol initialize --payment-mint "${mint}" --tier-floors "${floors}" --verifier-floor "${verifier_floor}"

log "writing ${shared_dir}/protocol.json"
kswarm --json --cluster "${cluster}" --rpc-url "${rpc_url}" \
  protocol runtime-config --output "${shared_dir}/protocol.json" --artifact-gateway-url "${gateway_url}"

: > "${shared_dir}/ready"
log "ready"
