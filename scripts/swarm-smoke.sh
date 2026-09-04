#!/usr/bin/env bash
# End-to-end smoke test of the containerized Python stack (docker-compose.swarm.yml,
# local profile): build the images, bring the stack up, bootstrap, open one
# two-branch prediction from the cli container, let the branch worker execute,
# the verifier attest, and the aggregator aggregate, read `predict report`,
# settle both branch jobs after the challenge window, and tear everything down.
#
# Requirements: docker with compose v2, python3, curl, a built program artifact,
# and an OpenAI-compatible LLM endpoint. The log goes to $KSWARM_SMOKE_LOG.
#
# Environment (defaults in parentheses):
#   LLM_BASE_URL                 required; OpenAI-compatible chat completions endpoint
#   LLM_MODEL_NAME               required; model id on that endpoint
#   LLM_MAX_TOKENS               (12000)  completion cap passed to the daemons
#   KSWARM_PROGRAM_SO          (./solana/target/deploy/kswarm_protocol.so)
#   KSWARM_VALIDATOR_RPC_PORT  (a free loopback port)
#   KSWARM_SMOKE_PROJECT       (kswarm-smoke-<pid>)  compose project name
#   KSWARM_SMOKE_LOG           (runtime/swarm-smoke/<timestamp>.log)
#   KSWARM_SMOKE_CHALLENGE_WINDOW (300) seconds; the verifier re-executes the branch inside it
#   KSWARM_SMOKE_TIMEOUT       (900)  seconds to wait for outputs and attestations
#   KSWARM_SMOKE_KEEP          (0)    1 leaves the stack running after the test
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

if [ -z "${LLM_BASE_URL:-}" ] || [ -z "${LLM_MODEL_NAME:-}" ]; then
  printf 'LLM_ENDPOINT_UNREACHABLE: LLM_BASE_URL and LLM_MODEL_NAME must be set\n' >&2
  exit 2
fi
export LLM_BASE_URL LLM_MODEL_NAME
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-12000}"
export KSWARM_PROGRAM_SO="${KSWARM_PROGRAM_SO:-./solana/target/deploy/kswarm_protocol.so}"
export KSWARM_GIT_SHA="${KSWARM_GIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
export KSWARM_GIT_REF="${KSWARM_GIT_REF:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"
export KSWARM_BUILD_DATE="${KSWARM_BUILD_DATE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
project="${KSWARM_SMOKE_PROJECT:-kswarm-smoke-$$}"
challenge_window="${KSWARM_SMOKE_CHALLENGE_WINDOW:-300}"
timeout_seconds="${KSWARM_SMOKE_TIMEOUT:-900}"
keep="${KSWARM_SMOKE_KEEP:-0}"
log_path="${KSWARM_SMOKE_LOG:-runtime/swarm-smoke/$(date -u +%Y%m%dT%H%M%SZ).log}"
mkdir -p "$(dirname "${log_path}")"

free_port() {
  python3 - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}
export KSWARM_VALIDATOR_RPC_PORT="${KSWARM_VALIDATOR_RPC_PORT:-$(free_port)}"

compose=(docker compose -f docker-compose.swarm.yml -p "${project}")

log() {
  printf '[swarm-smoke %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "${log_path}" >&2
}

fail() {
  log "FAIL: $*"
  exit 1
}

json_field() {
  # json_field <expression> reads JSON on stdin; the expression sees it as `d`.
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"
}

context_path="$(dirname "${log_path}")/context.txt"

cli() {
  # One-shot CLI invocation inside the compose network, JSON output on stdout.
  # The seeded news item is bind-mounted for `predict open --context-file`.
  "${compose[@]}" run --rm --no-deps -T -v "$(cd "$(dirname "${context_path}")" && pwd)/context.txt:/run/kswarm/context.txt:ro" cli --json "$@"
}

cleanup() {
  status=$?
  if [ "${keep}" = "1" ]; then
    log "keeping the stack up (project ${project}); tear down with: ${compose[*]} --profile local down -v"
    exit "${status}"
  fi
  log "collecting container logs"
  "${compose[@]}" --profile local logs --no-color --timestamps >> "${log_path}" 2>&1 || true
  log "tearing down"
  "${compose[@]}" --profile local --profile tools down -v --remove-orphans >> "${log_path}" 2>&1 || true
  if [ "${status}" -eq 0 ]; then
    log "PASS (log: ${log_path})"
  else
    log "exit ${status} (log: ${log_path})"
  fi
  exit "${status}"
}
trap cleanup EXIT

# --- preflight -------------------------------------------------------------------
for tool in docker python3 curl; do
  command -v "${tool}" >/dev/null 2>&1 || fail "missing required command: ${tool}"
done
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is required"
[ -f "${KSWARM_PROGRAM_SO}" ] || fail "program artifact not found: ${KSWARM_PROGRAM_SO} (run cargo build-sbf or set KSWARM_PROGRAM_SO)"
curl -fsS -m 10 -H "authorization: Bearer ${LLM_API_KEY:-local-llm}" "${LLM_BASE_URL%/}/models" >/dev/null \
  || fail "LLM_ENDPOINT_UNREACHABLE: ${LLM_BASE_URL}"
log "project=${project} program=${KSWARM_PROGRAM_SO} llm=${LLM_BASE_URL} model=${LLM_MODEL_NAME} rpc-port=${KSWARM_VALIDATOR_RPC_PORT}"

# --- build and start -------------------------------------------------------------
log "building images (sha ${KSWARM_GIT_SHA})"
"${compose[@]}" --profile local --profile tools build >> "${log_path}" 2>&1 || fail "image build failed"
log "starting the local profile"
"${compose[@]}" --profile local up -d >> "${log_path}" 2>&1 || fail "compose up failed"

container_state() {
  container="$("${compose[@]}" --profile local ps -q "$1" 2>/dev/null | head -n 1)"
  [ -n "${container}" ] || { printf 'missing 0'; return; }
  docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "${container}"
}

log "waiting for the bootstrap to complete"
deadline=$((SECONDS + 600))
while :; do
  state="$(container_state bootstrap)"
  case "${state}" in
    "exited 0") break ;;
    exited\ *) "${compose[@]}" --profile local logs bootstrap >> "${log_path}" 2>&1 || true; fail "bootstrap exited with ${state#exited }" ;;
  esac
  [ "${SECONDS}" -lt "${deadline}" ] || fail "bootstrap did not complete in time (state: ${state})"
  sleep 3
done
"${compose[@]}" --profile local logs --no-color bootstrap >> "${log_path}" 2>&1 || true
log "bootstrap complete"

# --- open one two-branch prediction ------------------------------------------------
# A scalar forecast over a seeded (fictional) news item. The context gives the
# model evidence to reason about, so the strict parser gets a non-empty rationale.
cat > "${context_path}" <<'CONTEXT'
Seeded public news item (fictional, for the swarm smoke test): The city council of
Riverton voted 7-2 on Tuesday to raise downtown parking fees by 40 percent starting
next month. Local business owners said the increase will drive customers to the
suburban mall. The council said the revenue funds free weekend transit. Early social
media reaction was mixed, with most posts from downtown merchants opposed and several
commuters in favor.
CONTEXT
opened="$(cli predict open \
  --question "Will sentiment around the seeded public news item be net-negative?" \
  --output-kind scalar \
  --branches 2 \
  --combiner weighted-mean \
  --reward-per-branch 1KAI \
  --aggregator-reward 1KAI \
  --challenge-window "${challenge_window}" \
  --context-file /run/kswarm/context.txt \
  --persona-set builtin-public-opinion-v1)"
parent_run="$(printf '%s' "${opened}" | json_field 'd["parent_run"]')"
log "opened prediction run ${parent_run}"

# --- wait for branches, attestations, and the aggregate receipt -----------------------
deadline=$((SECONDS + timeout_seconds))
while :; do
  status_json="$(cli predict status "${parent_run}")"
  summary="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
branches = [j for j in d["jobs"] if j["kind"] == "branch"]
aggregate = [j for j in d["jobs"] if j["kind"] == "aggregate"]
attested = sum(1 for j in branches if j["status"] in ("completed", "settled") and j["verifier_hash"])
aggregated = sum(1 for j in aggregate if j["status"] in ("completed", "settled") and j["output_cid"])
print(f"branches={len(branches)} attested={attested} aggregated={aggregated} statuses=" + ",".join(j["status"] for j in d["jobs"]))
print("READY" if attested == len(branches) == 2 and aggregated == 1 else "WAIT")
')"
  log "${summary%$'\n'*}"
  case "${summary##*$'\n'}" in
    READY) break ;;
  esac
  [ "${SECONDS}" -lt "${deadline}" ] || fail "branches were not attested and aggregated within ${timeout_seconds}s"
  sleep 10
done

# --- report --------------------------------------------------------------------------
report="$(cli predict report "${parent_run}")"
printf '%s\n' "${report}" >> "${log_path}"
final_bps="$(printf '%s' "${report}" | json_field 'd["final_scalar_bps"]')"
branch_count="$(printf '%s' "${report}" | json_field 'd["aggregate_output"]["result"]["branch_count"]')"
[ "${final_bps}" != "None" ] || fail "predict report has no final scalar"
[ "${branch_count}" = "2" ] || fail "aggregate combined ${branch_count} branches, expected 2"
log "report: final_scalar_bps=${final_bps} branch_count=${branch_count}"

# --- settle both branch jobs after the challenge window --------------------------------
branch_jobs="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
print(" ".join(j["job"] for j in json.load(sys.stdin)["jobs"] if j["kind"] == "branch"))
')"
for job in ${branch_jobs}; do
  while :; do
    inspected="$(cli inspect job "${job}")"
    challenge_deadline="$(printf '%s' "${inspected}" | json_field 'd["account"]["challenge_deadline"]')"
    now="$(date -u +%s)"
    if [ "${now}" -gt "$((challenge_deadline + 2))" ]; then
      break
    fi
    log "job ${job}: challenge window open for $((challenge_deadline - now))s more"
    sleep 5
  done
  cli settle "${job}" >> "${log_path}"
  settled="$(cli inspect job "${job}" | json_field 'd["account"]["status_name"]')"
  [ "${settled}" = "settled" ] || fail "job ${job} is ${settled}, expected settled"
  log "settled branch job ${job}"
done

final="$(cli predict status "${parent_run}")"
printf '%s\n' "${final}" >> "${log_path}"
log "run ${parent_run}: $(printf '%s' "${final}" | python3 -c 'import json,sys; print(", ".join(j["kind"] + "=" + j["status"] for j in json.load(sys.stdin)["jobs"]))')"
printf '{"swarm_smoke":{"parent_run":"%s","final_scalar_bps":%s,"branch_count":%s,"settled_branch_jobs":["%s"]}}\n' \
  "${parent_run}" "${final_bps}" "${branch_count}" "${branch_jobs// /\",\"}"
