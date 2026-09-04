#!/usr/bin/env bash
set -euo pipefail

shared_dir="${BONSOL_SHARED_DIR:-/runtime/bonsol}"
rpc_url="${BONSOL_RPC_URL:-http://bonsol-validator:8899}"
ws_url="${BONSOL_WS_URL:-ws://bonsol-validator:8900}"
repo_dir="${BONSOL_REPO_DIR:-/app}"
zk_program_dir="${repo_dir}/protocol/bonsol-branch-reducer"
aggregate_program_dir="${repo_dir}/protocol/bonsol-aggregate-reducer"
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

ensure_risc0_toolchain() {
  if ! command -v rzup >/dev/null 2>&1; then
    echo "no rzup in this image: the builder image installs pinned RISC Zero components" >&2
    exit 1
  fi
  if ! command -v cargo-risczero >/dev/null 2>&1; then
    echo "no cargo-risczero in this image: rebuild docker/bonsol-eval" >&2
    exit 1
  fi
  rzup show
}

# `risc0-build` builds a guest inside `risczero/risc0-guest-builder:<tag>`, defaulting
# to the mutable tag `r0.1.88.0`. A guest ELF is a function of its toolchain, so that
# tag decides every image id this script produces. Pull the digest the repository pins,
# retag it locally, and point `risc0-build` at the local tag.
#
# The digest comes from `protocol/risc0-toolchain.env` in the repository being built --
# the one declaration every image that touches a guest reads -- so changing the pin does
# not need the eval image rebuilt. RISC0_GUEST_BUILDER_DIGEST in the environment still
# wins, for a one-off run against a different builder.
pin_guest_builder() {
  digest="${RISC0_GUEST_BUILDER_DIGEST:-}"
  toolchain_env="${repo_dir}/protocol/risc0-toolchain.env"
  if [ -z "${digest}" ] && [ -f "${toolchain_env}" ]; then
    # shellcheck disable=SC1090
    . "${toolchain_env}"
    digest="${RISC0_GUEST_BUILDER_DIGEST:-}"
  fi
  if [ -z "${digest}" ]; then
    echo "no RISC0_GUEST_BUILDER_DIGEST in the environment or in ${toolchain_env}:" >&2
    echo "guest image ids would not be reproducible" >&2
    exit 1
  fi
  pinned_tag="r0.1.88.0-pinned"
  docker pull "risczero/risc0-guest-builder@${digest}"
  docker tag "risczero/risc0-guest-builder@${digest}" "risczero/risc0-guest-builder:${pinned_tag}"
  export RISC0_DOCKER_CONTAINER_TAG="${pinned_tag}"
  echo "guest builder pinned to risczero/risc0-guest-builder@${digest} as :${pinned_tag}"
}

# `bonsol build` runs the RISC Zero docker build with the guest directory as the
# docker context and `cargo build --locked` inside it, so each guest needs its own
# Cargo.lock and must be self-contained.
# A manifest is only a cache hit when the ELF it names is still there. The manifest
# records an absolute `binaryPath` inside the crate's `target/`, which is regenerated
# work: a clean checkout, a `cargo clean`, or anything that removes `target/` leaves a
# manifest pointing at nothing, and `bonsol deploy` then fails with
# "Failed to load binary from manifest ... No such file or directory" long after the
# build step said it had nothing to do.
guest_binary_present() {
  manifest_path="$1"
  [ -f "${manifest_path}" ] || return 1
  binary="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("binaryPath",""))' "${manifest_path}" 2>/dev/null || true)"
  [ -n "${binary}" ] && [ -f "${binary}" ]
}

build_guest() {
  program_dir="$1"
  manifest_name="$2"
  if guest_binary_present "${shared_dir}/${manifest_name}"; then
    return 0
  fi
  rm -f "${shared_dir}/${manifest_name}"
  ensure_risc0_toolchain
  pin_guest_builder
  if [ ! -f "${program_dir}/Cargo.lock" ]; then
    cargo generate-lockfile --manifest-path "${program_dir}/Cargo.toml"
  fi
  bonsol --keypair "${shared_dir}/client-keypair.json" --rpc-url "${rpc_url}" build --zk-program-path "${program_dir}"
  if [ ! -f "${program_dir}/manifest.json" ]; then
    echo "bonsol build produced no manifest for ${program_dir}" >&2
    exit 1
  fi
  cp "${program_dir}/manifest.json" "${shared_dir}/${manifest_name}"
  # This container is root and the repository is bind-mounted, so the `target/` tree it
  # just wrote is unreadable to the user who owns the checkout. A later `docker build`
  # from that checkout then fails in the context sender -- "error from sender: open
  # .../target: permission denied" -- before any Dockerfile line runs, and an ignore
  # rule is not a reliable way out because the sender still stats the directory. Making
  # the output world-readable is: nothing in it is secret, it is compiler output.
  chmod -R a+rX "${program_dir}/target" 2>/dev/null || true
  chmod a+rx "${program_dir}" 2>/dev/null || true
}

# The aggregate reducer is the guest every aggregate-proof job is bound to; the branch
# reducer stays for the callback and replay smoke tests.
build_guest "${aggregate_program_dir}" "aggregate-reducer-manifest.json"
build_guest "${zk_program_dir}" "reducer-manifest.json"

touch "${shared_dir}/ready"

if [ "${BONSOL_BUILDER_KEEPALIVE:-1}" = "1" ]; then
  tail -f /dev/null
fi
