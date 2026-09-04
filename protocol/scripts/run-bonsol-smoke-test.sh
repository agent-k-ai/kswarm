#!/usr/bin/env bash
set -euo pipefail

shared_dir="${BONSOL_SHARED_DIR:-/runtime/bonsol}"
rpc_url="${BONSOL_RPC_URL:-http://bonsol-validator:8899}"
repo_dir="${BONSOL_REPO_DIR:-/app}"
image_server_url="${BONSOL_IMAGE_SERVER_URL:-http://bonsol-image-server:8080}"

until [ -f "${shared_dir}/ready" ]; do
  sleep 2
done

until solana -u "${rpc_url}" block-height >/dev/null 2>&1; do
  sleep 2
done

client_pubkey="$(solana-keygen pubkey "${shared_dir}/client-keypair.json")"
solana -u "${rpc_url}" airdrop 5 "${client_pubkey}" >/dev/null 2>&1 || true

for _ in $(seq 1 20); do
  client_balance="$(solana -u "${rpc_url}" balance "${client_pubkey}" --lamports 2>/dev/null || echo 0)"
  if [ "${client_balance}" -gt 0 ] 2>/dev/null; then
    break
  fi
  sleep 1
done

manifest_path="${repo_dir}/protocol/bonsol-branch-reducer/manifest.json"
execution_request_path="${shared_dir}/execution-request.json"
input_json='{"branch_key":"baseline","child_job_id":"child-baseline-1","parent_request_id":"parent-bonsol-eval","line_count":3,"word_count":17,"score_hex":"003a000000000000000000000000000000000000000000000000000000000000"}'
input_len="$(printf '%s' "${input_json}" | wc -c | tr -d ' ')"

image_id="$(jq -r '.imageId' "${manifest_path}")"
execution_id="kswarm-bonsol-$(date +%s)"

jq -n \
  --arg imageId "${image_id}" \
  --arg executionId "${execution_id}" \
  --arg inputLen "${input_len}" \
  --arg inputData "${input_json}" \
  '{
    imageId: $imageId,
    executionId: $executionId,
    executionConfig: {
      verifyInputHash: false,
      forwardOutput: true
    },
    inputs: [
      {
        inputType: "PublicData",
        data: $inputLen
      },
      {
        inputType: "PublicData",
        data: $inputData
      }
    ],
    tip: 12000,
    expiry: 1500
  }' > "${execution_request_path}"

bonsol -k "${shared_dir}/client-keypair.json" -u "${rpc_url}" deploy url \
  --url "${image_server_url}" \
  --manifest-path "${manifest_path}" \
  --auto-confirm

execute_attempt=1
max_attempts=12
execute_delay=5

while true; do
  set +e
  execute_output="$(
    bonsol -k "${shared_dir}/client-keypair.json" -u "${rpc_url}" execute \
      -f "${execution_request_path}" \
      --wait \
      --timeout 1200 2>&1
  )"
  execute_status=$?
  set -e

  if [ "${execute_status}" -eq 0 ]; then
    break
  fi

  if [ "${execute_attempt}" -ge "${max_attempts}" ] || ! printf '%s' "${execute_output}" | grep -q "Invalid deployment account\|custom program error: 0x12"; then
    printf '%s\n' "${execute_output}" >&2
    exit "${execute_status}"
  fi

  printf 'execute attempt %s/%s hit deployment visibility race, retrying in %ss\n' \
    "${execute_attempt}" "${max_attempts}" "${execute_delay}" >&2
  sleep "${execute_delay}"
  execute_attempt=$((execute_attempt + 1))
done

printf '{\n'
printf '  "imageId": "%s",\n' "${image_id}"
printf '  "executionId": "%s",\n' "${execution_id}"
printf '  "manifestPath": "%s",\n' "${manifest_path}"
printf '  "status": "success"\n'
printf '}\n'
