#!/usr/bin/env bash
set -euo pipefail

shared_dir="${BONSOL_SHARED_DIR:-/runtime/bonsol}"
rpc_url="${BONSOL_RPC_URL:-http://bonsol-validator:8899}"

until [ -f "${shared_dir}/ready" ]; do
  sleep 2
done

until solana -u "${rpc_url}" block-height >/dev/null 2>&1; do
  sleep 2
done

node_pubkey="$(solana-keygen pubkey "${shared_dir}/node-keypair.json")"
solana -u "${rpc_url}" airdrop 2 "${node_pubkey}" >/dev/null 2>&1 || true

for _ in $(seq 1 20); do
  node_balance="$(solana -u "${rpc_url}" balance "${node_pubkey}" --lamports 2>/dev/null || echo 0)"
  if [ "${node_balance}" -gt 0 ] 2>/dev/null; then
    break
  fi
  sleep 1
done

ulimit -s unlimited
exec /opt/bonsol/bin/bonsol-node -f "${shared_dir}/NodeDocker.toml"
