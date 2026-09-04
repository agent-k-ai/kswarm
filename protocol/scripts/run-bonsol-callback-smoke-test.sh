#!/usr/bin/env bash
set -euo pipefail

shared_dir="${BONSOL_SHARED_DIR:-/runtime/bonsol}"
rpc_url="${BONSOL_RPC_URL:-http://bonsol-validator:8899}"
repo_dir="${BONSOL_REPO_DIR:-/app}"
image_server_url="${BONSOL_IMAGE_SERVER_URL:-http://bonsol-image-server:8080}"
harness_manifest="${repo_dir}/protocol/bonsol-callback-harness/Cargo.toml"
harness_bin="${BONSOL_CALLBACK_HARNESS_BIN:-${shared_dir}/bonsol-callback-harness}"
manifest_path="${repo_dir}/protocol/bonsol-branch-reducer/manifest.json"
if [ ! -f "${manifest_path}" ] && [ -f "${shared_dir}/reducer-manifest.json" ]; then
  manifest_path="${shared_dir}/reducer-manifest.json"
fi
work_dir="${shared_dir}/phase0b-callback"
kswarm_program_id="ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM"
system_program_id="11111111111111111111111111111111"
instructions_sysvar_id="Sysvar1nstructions1111111111111111111111111"
test_mode="${PHASE0_CALLBACK_TEST_MODE:-happy}"
execute_timeout="${PHASE0_CALLBACK_EXECUTE_TIMEOUT:-1200}"
expected_failure_timeout="${PHASE0_CALLBACK_EXPECTED_FAILURE_TIMEOUT:-300}"
max_attempts="${PHASE0_CALLBACK_MAX_ATTEMPTS:-12}"
execute_delay="${PHASE0_CALLBACK_RETRY_DELAY:-5}"

run_harness() {
  if [ -x "${harness_bin}" ]; then
    "${harness_bin}" "$@"
  else
    cargo run --quiet --manifest-path "${harness_manifest}" -- "$@"
  fi
}

fetch_transaction_json() {
  local sig="$1"
  local output_path="$2"
  curl -sS "${rpc_url}" \
    -H 'content-type: application/json' \
    -d "$(jq -nc --arg sig "${sig}" '{jsonrpc:"2.0",id:1,method:"getTransaction",params:[$sig,{encoding:"json",commitment:"confirmed",maxSupportedTransactionVersion:0}]}')" \
    | jq '.result' > "${output_path}"
}

find_success_status_transaction_json() {
  local address="$1"
  local output_path="$2"
  local signatures_json
  signatures_json="$(
    curl -sS "${rpc_url}" \
      -H 'content-type: application/json' \
      -d "$(jq -nc --arg address "${address}" '{jsonrpc:"2.0",id:1,method:"getSignaturesForAddress",params:[$address,{limit:100,commitment:"confirmed"}]}')"
  )"
  local sig
  while read -r sig; do
    [ -n "${sig}" ] || continue
    local tx_response_path="${output_path}.${sig}.response.json"
    curl -sS "${rpc_url}" \
      -H 'content-type: application/json' \
      -d "$(jq -nc --arg sig "${sig}" '{jsonrpc:"2.0",id:1,method:"getTransaction",params:[$sig,{encoding:"json",commitment:"confirmed",maxSupportedTransactionVersion:0}]}')" \
      > "${tx_response_path}"
    if jq -e --arg program "${kswarm_program_id}" '
      .result.meta.err == null
      and any(.result.meta.logMessages[]?; contains("Proof verified with V3_0_3"))
      and any(.result.meta.logMessages[]?; contains($program))
    ' "${tx_response_path}" >/dev/null; then
      jq '.result' "${tx_response_path}" > "${output_path}"
      printf '%s\n' "${sig}"
      return 0
    fi
  done < <(printf '%s' "${signatures_json}" | jq -r '.result[].signature')
  printf 'could not find production StatusV1 transaction for %s\n' "${address}" >&2
  return 1
}

find_failed_status_transaction_json() {
  local address="$1"
  local output_path="$2"
  local signatures_json
  signatures_json="$(
    curl -sS "${rpc_url}" \
      -H 'content-type: application/json' \
      -d "$(jq -nc --arg address "${address}" '{jsonrpc:"2.0",id:1,method:"getSignaturesForAddress",params:[$address,{limit:100,commitment:"confirmed"}]}')"
  )"
  local sig
  while read -r sig; do
    [ -n "${sig}" ] || continue
    local tx_response_path="${output_path}.${sig}.response.json"
    curl -sS "${rpc_url}" \
      -H 'content-type: application/json' \
      -d "$(jq -nc --arg sig "${sig}" '{jsonrpc:"2.0",id:1,method:"getTransaction",params:[$sig,{encoding:"json",commitment:"confirmed",maxSupportedTransactionVersion:0}]}')" \
      > "${tx_response_path}"
    if jq -e --arg program "${kswarm_program_id}" '
      .result.meta.err != null
      and (
        any(.result.meta.logMessages[]?; contains($program))
        or any(.result.meta.logMessages[]?; contains("Proof verified with V3_0_3"))
      )
    ' "${tx_response_path}" >/dev/null; then
      jq '.result' "${tx_response_path}" > "${output_path}"
      printf '%s\n' "${sig}"
      return 0
    fi
  done < <(printf '%s' "${signatures_json}" | jq -r '.result[].signature')
  return 1
}

write_account_data_bin() {
  local account="$1"
  local json_path="$2"
  local bin_path="$3"
  if solana -u "${rpc_url}" account "${account}" --output json > "${json_path}" 2>/dev/null; then
    jq -r '.account.data[0]' "${json_path}" | base64 -d > "${bin_path}"
    return 0
  fi
  return 1
}

ensure_no_production_marker() {
  local account="$1"
  local label="$2"
  local json_path="${work_dir}/${execution_id}-${label}-account.json"
  local bin_path="${work_dir}/${execution_id}-${label}.bin"
  if write_account_data_bin "${account}" "${json_path}" "${bin_path}"; then
    local len
    len="$(wc -c < "${bin_path}")"
    if [ "${len}" -eq 210 ]; then
      printf 'production marker was written unexpectedly for %s at %s\n' "${test_mode}" "${account}" >&2
      exit 1
    fi
  fi
}

mkdir -p "${work_dir}"

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

image_id="$(jq -r '.imageId' "${manifest_path}")"
expect_status_failure="0"
expect_execute_failure="0"
expect_marker="0"
marker_mode="expected"
fault="none"
request_image_id="${image_id}"
input_hash_mode="normal"
replay_mode=""
run_settle="0"

case "${test_mode}" in
  happy)
    execution_id="p0b-happy-$(date +%s)"
    expect_marker="1"
    ;;
  settle)
    execution_id="p0b-settle-$(date +%s)"
    expect_marker="1"
    run_settle="1"
    ;;
  n1-callback-error)
    execution_id="p0b-n1-$(date +%s)"
    expect_status_failure="1"
    fault="output-digest"
    ;;
  n2-wrong-image-id)
    execution_id="p0b-n2-$(date +%s)"
    expect_execute_failure="1"
    request_image_id="0000000000000000000000000000000000000000000000000000000000000000"
    ;;
  n3-wrong-execution-account)
    execution_id="p0b-n3-$(date +%s)"
    expect_marker="1"
    replay_mode="wrong-execution-account"
    ;;
  n4-wrong-extra-accounts)
    execution_id="p0b-n4-$(date +%s)"
    marker_mode="wrong"
    expect_status_failure="1"
    ;;
  n5-wrong-input-hash)
    execution_id="p0b-n5-$(date +%s)"
    input_hash_mode="wrong"
    expect_marker="1"
    ;;
  n6-replay-after-cleanup)
    execution_id="p0b-n6-$(date +%s)"
    expect_marker="1"
    replay_mode="replay"
    ;;
  *)
    printf 'unknown PHASE0_CALLBACK_TEST_MODE: %s\n' "${test_mode}" >&2
    exit 2
    ;;
esac

prepared_path="${work_dir}/${execution_id}-prepared.json"
execution_request_path="${work_dir}/${execution_id}-execution-request.json"
marker_account_path="${work_dir}/${execution_id}-marker-account.json"
marker_bin_path="${work_dir}/${execution_id}-marker.bin"

prepare_args=(
  prepare-production
  --rpc-url "${rpc_url}"
  --keypair "${shared_dir}/client-keypair.json"
  --manifest "${manifest_path}"
  --execution-id "${execution_id}"
  --marker-mode "${marker_mode}"
)
if [ "${fault}" != "none" ]; then
  prepare_args+=(--fault "${fault}")
fi
run_harness "${prepare_args[@]}" > "${prepared_path}"

marker_pda="$(jq -r '.markerPda' "${prepared_path}")"
expected_marker_pda="$(jq -r '.expectedMarkerPda' "${prepared_path}")"
aggregate_job="$(jq -r '.aggregateJob' "${prepared_path}")"
execution_account="$(jq -r '.executionAccount' "${prepared_path}")"
input_json="$(jq -r '.inputJson' "${prepared_path}")"
framed_input_hex="$(jq -r '.framedInputHex' "${prepared_path}")"
input_hash="$(jq -r '.executionConfigInputHash' "${prepared_path}")"
if [ "${input_hash_mode}" = "wrong" ]; then
  input_hash="$(printf 'a5%.0s' {1..32})"
fi
callback_program="$(jq -r '.callbackProgramId' "${prepared_path}")"
callback_prefix="$(jq -c '.callbackInstructionPrefix' "${prepared_path}")"

if [ "${test_mode}" != "n2-wrong-image-id" ]; then
  solana -u "${rpc_url}" transfer \
    --from "${shared_dir}/client-keypair.json" \
    --fee-payer "${shared_dir}/client-keypair.json" \
    --allow-unfunded-recipient \
    "${marker_pda}" \
    0.02 \
    >/dev/null
fi

jq -n \
  --arg imageId "${request_image_id}" \
  --arg executionId "${execution_id}" \
  --arg inputHash "${input_hash}" \
  --arg framedInput "0x${framed_input_hex}" \
  --arg callbackProgram "${callback_program}" \
  --arg markerPda "${marker_pda}" \
  --arg aggregateJob "${aggregate_job}" \
  --arg systemProgram "${system_program_id}" \
  --arg instructionsSysvar "${instructions_sysvar_id}" \
  --argjson callbackPrefix "${callback_prefix}" \
  '{
    imageId: $imageId,
    executionId: $executionId,
    executionConfig: {
      verifyInputHash: true,
      inputHash: $inputHash,
      forwardOutput: true
    },
    inputs: [
      {
        inputType: "PublicData",
        data: $framedInput
      }
    ],
    tip: 12000,
    expiry: 1500,
    callbackConfig: {
      programId: $callbackProgram,
      instructionPrefix: $callbackPrefix,
      extraAccounts: [
        {
          pubkey: $markerPda,
          isSigner: false,
          isWritable: true
        },
        {
          pubkey: $aggregateJob,
          isSigner: false,
          isWritable: false
        },
        {
          pubkey: $systemProgram,
          isSigner: false,
          isWritable: false
        },
        {
          pubkey: $instructionsSysvar,
          isSigner: false,
          isWritable: false
        }
      ]
    }
  }' > "${execution_request_path}"

bonsol -k "${shared_dir}/client-keypair.json" -u "${rpc_url}" deploy url \
  --url "${image_server_url}" \
  --manifest-path "${manifest_path}" \
  --auto-confirm

execute_attempt=1
observed_status_failure="0"
mode_execute_timeout="${execute_timeout}"
if [ "${expect_status_failure}" = "1" ]; then
  mode_execute_timeout="${expected_failure_timeout}"
fi

while true; do
  set +e
  execute_output="$(
    timeout "${mode_execute_timeout}" \
      bonsol -k "${shared_dir}/client-keypair.json" -u "${rpc_url}" execute \
        -f "${execution_request_path}" \
        --wait \
        --timeout "${mode_execute_timeout}" 2>&1
  )"
  execute_status=$?
  set -e

  if [ "${execute_status}" -eq 0 ]; then
    if [ "${expect_execute_failure}" = "1" ]; then
      printf 'expected execute failure for %s, but execution succeeded\n' "${test_mode}" >&2
      exit 1
    fi
    if [ "${expect_status_failure}" = "1" ]; then
      printf 'expected StatusV1 failure for %s, but execution completed\n' "${test_mode}" >&2
      exit 1
    fi
    break
  fi

  if [ "${expect_execute_failure}" = "1" ]; then
    result_json="$(jq -n \
      --arg testMode "${test_mode}" \
      --arg imageId "${request_image_id}" \
      --arg executionId "${execution_id}" \
      --arg error "${execute_output}" \
      '{testMode:$testMode,imageId:$imageId,executionId:$executionId,status:"execute_failed_as_expected",error:$error}')"
    printf '%s\n' "${result_json}"
    exit 0
  fi

  is_retryable="0"
  if printf '%s' "${execute_output}" | grep -q "Invalid deployment account\|custom program error: 0x12\|Computational budget exceeded"; then
    is_retryable="1"
  fi

  if [ "${is_retryable}" = "1" ] && [ "${execute_attempt}" -lt "${max_attempts}" ]; then
    printf 'execute attempt %s/%s hit retryable local submission error, retrying in %ss\n' \
      "${execute_attempt}" "${max_attempts}" "${execute_delay}" >&2
    sleep "${execute_delay}"
    execute_attempt=$((execute_attempt + 1))
    continue
  fi

  if [ "${expect_status_failure}" = "1" ]; then
    observed_status_failure="1"
    status_failure_error="${execute_output}"
    break
  fi

  printf '%s\n' "${execute_output}" >&2
  exit "${execute_status}"
done

if [ "${expect_marker}" = "1" ]; then
  if write_account_data_bin "${expected_marker_pda}" "${marker_account_path}" "${marker_bin_path}"; then
    run_harness verify-production-marker \
      --prepared "${prepared_path}" \
      --marker-bin "${marker_bin_path}"
  else
    printf 'expected production marker %s was not found\n' "${expected_marker_pda}" >&2
    exit 1
  fi
else
  ensure_no_production_marker "${marker_pda}" "marker"
  if [ "${marker_pda}" != "${expected_marker_pda}" ]; then
    ensure_no_production_marker "${expected_marker_pda}" "expected-marker"
  fi
fi

if [ "${expect_marker}" = "1" ] || [ "${run_settle}" = "1" ]; then
  status_tx_json_path="${work_dir}/${execution_id}-status-transaction.json"
  status_sig="$(
    find_success_status_transaction_json "${expected_marker_pda}" "${status_tx_json_path}"
  )"
  status_compute_units="$(jq -r '.meta.computeUnitsConsumed // empty' "${status_tx_json_path}")"
  status_err="$(jq -c '.meta.err // null' "${status_tx_json_path}")"
  if [ -z "${status_compute_units}" ]; then
    printf 'StatusV1 transaction %s did not expose meta.computeUnitsConsumed\n' "${status_sig}" >&2
    exit 1
  fi
fi

if [ "${observed_status_failure}" = "1" ] && [ "${execution_account}" != "null" ]; then
  status_tx_json_path="${work_dir}/${execution_id}-failed-status-transaction.json"
  if status_sig="$(find_failed_status_transaction_json "${execution_account}" "${status_tx_json_path}")"; then
    status_compute_units="$(jq -r '.meta.computeUnitsConsumed // empty' "${status_tx_json_path}")"
    status_err="$(jq -c '.meta.err // null' "${status_tx_json_path}")"
  fi
fi

if [ -n "${replay_mode}" ]; then
  replay_result_path="${work_dir}/${execution_id}-status-replay.json"
  run_harness replay-status \
    --rpc-url "${rpc_url}" \
    --keypair "${shared_dir}/node-keypair.json" \
    --tx-json "${status_tx_json_path}" \
    --mode "${replay_mode}" \
    > "${replay_result_path}"
  if [ "$(jq -r '.accepted' "${replay_result_path}")" != "false" ]; then
    printf 'expected replay rejection for %s, got accepted replay\n' "${test_mode}" >&2
    cat "${replay_result_path}" >&2
    exit 1
  fi
fi

if [ "${run_settle}" = "1" ]; then
  settle_result_path="${work_dir}/${execution_id}-settle.json"
  run_harness settle-production \
    --rpc-url "${rpc_url}" \
    --keypair "${shared_dir}/client-keypair.json" \
    --prepared "${prepared_path}" \
    --marker "${expected_marker_pda}" \
    > "${settle_result_path}"
  settle_sig="$(jq -r '.settleSignature' "${settle_result_path}")"
  settle_tx_json_path="${work_dir}/${execution_id}-settle-transaction.json"
  fetch_transaction_json "${settle_sig}" "${settle_tx_json_path}"
  settle_compute_units="$(jq -r '.meta.computeUnitsConsumed // empty' "${settle_tx_json_path}")"
fi

result_json="$(jq -n \
  --arg testMode "${test_mode}" \
  --arg imageId "${image_id}" \
  --arg executionId "${execution_id}" \
  --arg executionAccount "${execution_account}" \
  --arg markerPda "${expected_marker_pda}" \
  --arg callbackMarkerPda "${marker_pda}" \
  --arg aggregateJob "${aggregate_job}" \
  --arg preparedPath "${prepared_path}" \
  --arg executionRequestPath "${execution_request_path}" \
  '{
    testMode: $testMode,
    imageId: $imageId,
    executionId: $executionId,
    executionAccount: $executionAccount,
    aggregateJob: $aggregateJob,
    markerPda: $markerPda,
    callbackMarkerPda: $callbackMarkerPda,
    preparedPath: $preparedPath,
    executionRequestPath: $executionRequestPath
  }')"

if [ "${observed_status_failure}" = "1" ]; then
  result_json="$(printf '%s' "${result_json}" | jq \
    --arg output "${status_failure_error:-}" \
    '. + {statusFailureObserved:true,statusFailureOutput:$output}')"
fi
if [ -n "${status_sig:-}" ]; then
  result_json="$(printf '%s' "${result_json}" | jq \
    --arg sig "${status_sig}" \
    --argjson err "${status_err:-null}" \
    '. + {statusSignature:$sig,statusError:$err}')"
fi
if [ -n "${status_compute_units:-}" ]; then
  result_json="$(printf '%s' "${result_json}" | jq \
    --argjson computeUnits "${status_compute_units}" \
    '. + {statusComputeUnits:$computeUnits}')"
fi
if [ -n "${replay_result_path:-}" ]; then
  result_json="$(printf '%s' "${result_json}" | jq \
    --arg path "${replay_result_path}" \
    '. + {replayResultPath:$path}')"
fi
if [ -n "${settle_result_path:-}" ]; then
  result_json="$(printf '%s' "${result_json}" | jq \
    --arg path "${settle_result_path}" \
    --arg sig "${settle_sig}" \
    --argjson computeUnits "${settle_compute_units:-null}" \
    '. + {settleResultPath:$path,settleSignature:$sig,settleComputeUnits:$computeUnits}')"
fi
if [ "${observed_status_failure}" = "1" ]; then
  result_json="$(printf '%s' "${result_json}" | jq '. + {status:"status_failed_as_observed"}')"
else
  result_json="$(printf '%s' "${result_json}" | jq '. + {status:"success"}')"
fi

printf '%s\n' "${result_json}"
