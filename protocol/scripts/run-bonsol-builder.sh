#!/usr/bin/env bash
set -euo pipefail

shared_dir="${BONSOL_SHARED_DIR:-/runtime/bonsol}"
rpc_url="${BONSOL_RPC_URL:-http://bonsol-validator:8899}"
ws_url="${BONSOL_WS_URL:-ws://bonsol-validator:8900}"
repo_dir="${BONSOL_REPO_DIR:-/app}"
zk_program_dir="${repo_dir}/protocol/bonsol-branch-reducer"
harness_manifest="${repo_dir}/protocol/bonsol-callback-harness/Cargo.toml"

mkdir -p "${shared_dir}"
mkdir -p /root/.config/solana/cli

rm -f "${shared_dir}/ready"

if [ ! -f "${shared_dir}/client-keypair.json" ]; then
  solana-keygen new --no-bip39-passphrase --silent --outfile "${shared_dir}/client-keypair.json"
fi

if [ ! -f "${shared_dir}/node-keypair.json" ]; then
  solana-keygen new --no-bip39-passphrase --silent --outfile "${shared_dir}/node-keypair.json"
fi

cat > "${shared_dir}/NodeDocker.toml" <<EOF
risc0_image_folder = "/opt/bonsol/risc0_images"
max_input_size_mb = 10
image_download_timeout_secs = 60
input_download_timeout_secs = 60
maximum_concurrent_proofs = 1
max_image_size_mb = 8
image_compression_ttl_hours = 24
env = "dev"
stark_compression_tools_path = "/opt/bonsol/stark/"
[transaction_sender_config]
Rpc = { rpc_url = "${rpc_url}" }
[signer_config]
KeypairFile = { path = "${shared_dir}/node-keypair.json" }
[ingester_config]
RpcBlockSubscription = { wss_rpc_url = "${ws_url}" }
EOF

cat > /root/.config/solana/cli/config.yml <<EOF
json_rpc_url: "${rpc_url}"
websocket_url: "${ws_url}"
keypair_path: "${shared_dir}/client-keypair.json"
address_labels:
  {}
commitment: confirmed
EOF

if [ ! -f "${shared_dir}/bonsol.so" ]; then
  cd /opt/bonsol-src/onchain/bonsol
  cargo build-sbf
  cp /opt/bonsol-src/target/deploy/bonsol.so "${shared_dir}/bonsol.so"
fi

if [ ! -f "${shared_dir}/callback_example.so" ]; then
  cd /opt/bonsol-src/onchain/example-program-on-bonsol
  cargo build-sbf
  cp /opt/bonsol-src/target/deploy/callback_example.so "${shared_dir}/callback_example.so"
fi

if [ ! -f "${shared_dir}/kswarm_protocol.so" ]; then
  cd "${repo_dir}/solana/programs/kswarm_protocol"
  cargo build-sbf
  cp "${repo_dir}/solana/target/deploy/kswarm_protocol.so" "${shared_dir}/kswarm_protocol.so"
fi

if [ ! -x "${shared_dir}/bonsol-callback-harness" ]; then
  cd "${repo_dir}"
  cargo build --locked --manifest-path "${harness_manifest}"
  cp "${repo_dir}/protocol/bonsol-callback-harness/target/debug/bonsol-callback-harness" "${shared_dir}/bonsol-callback-harness"
fi

cd "${repo_dir}"
export PATH="${PATH}:/root/.cargo/bin:/root/.local/bin:/root/.risc0/bin"
export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"

if [ ! -f "${shared_dir}/reducer-manifest.json" ]; then
  if ! command -v rzup >/dev/null 2>&1; then
    curl -L https://risczero.com/install | bash
  fi

  if ! command -v cargo-risczero >/dev/null 2>&1; then
    rzup install
  fi

  if [ ! -f "${zk_program_dir}/Cargo.lock" ]; then
    cargo generate-lockfile --manifest-path "${zk_program_dir}/Cargo.toml"
  fi

  bonsol --keypair "${shared_dir}/client-keypair.json" --rpc-url "${rpc_url}" build --zk-program-path "${zk_program_dir}"
  cp "${zk_program_dir}/manifest.json" "${shared_dir}/reducer-manifest.json"
fi

touch "${shared_dir}/ready"

if [ "${BONSOL_BUILDER_KEEPALIVE:-1}" = "1" ]; then
  tail -f /dev/null
fi
