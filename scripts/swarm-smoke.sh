#!/usr/bin/env bash
# End-to-end smoke test of the containerized Python stack (docker-compose.swarm.yml,
# local profile): build the images, bring the stack up, bootstrap, open one
# two-branch prediction from the cli container, let the branch worker execute and the
# verifier attest, settle both branch jobs, bind and aggregate the run, read
# `predict report`, and tear everything down.
#
# THIS SMOKE TEST RUNS THE AGGREGATE UNPROVEN, DELIBERATELY.
# The aggregate job is opened by `predict bind-aggregate` against the MFA3 artifact
# built from the settled branch receipts, and the aggregator runner normally proves
# that reduction with a Bonsol RISC Zero execution before its receipt can settle. This
# stack has no Bonsol node, so the runner is given KSWARM_ALLOW_UNBOUND_AGGREGATE=1 and
# submits the receipt with no proof behind it. The consequence is real and expected:
# `settle_aggregate_proof_job` will refuse that receipt, because no
# `BonsolAggregateVerification` marker exists for it, so this script settles the branch
# jobs and never the aggregate one. What is covered here is the pipeline -- open,
# execute, attest, settle, bind, reduce, report. The proven path, where the aggregate
# receipt carries a marker and settles, is exercised by the Tier-2 Bonsol run.
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
#
# Proven-aggregate mode (KSWARM_SMOKE_BONSOL=1) additionally uses:
#   KSWARM_SMOKE_BONSOL_PROJECT   (kswarm-bonsol-<pid>)  compose project for the Bonsol stack
#   BONSOL_RUNTIME_HOST_DIR       (./runtime/bonsol)  where the builder writes keys and manifests
#   BONSOL_VALIDATOR_RPC_PORT     (38899)  the validator the Bonsol node watches
#   KSWARM_SMOKE_HOST_IP          (auto)   the address swarm containers reach this host on
#   KSWARM_BONSOL_HOOK_PORT       (38099)  where the proving service listens
#   KSWARM_SMOKE_AGGREGATE_TIMEOUT (2400)  seconds to wait for the proof and the marker
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
# The aggregate job inherits this window, and its clock starts when the aggregate
# receipt lands, before the Bonsol proof is requested. The verifier can only attest
# inside the window, so proven mode needs one longer than a proof takes.
challenge_window="${KSWARM_SMOKE_CHALLENGE_WINDOW:-300}"
if [ "${KSWARM_SMOKE_BONSOL:-0}" = "1" ] && [ -z "${KSWARM_SMOKE_CHALLENGE_WINDOW:-}" ]; then
  challenge_window=1800
fi
timeout_seconds="${KSWARM_SMOKE_TIMEOUT:-900}"
keep="${KSWARM_SMOKE_KEEP:-0}"
bonsol_mode="${KSWARM_SMOKE_BONSOL:-0}"
# Unique by default: two smoke runs, or a smoke run beside another agent's stack, must
# not share a compose project. Container names follow the project, because the compose
# file declares none.
bonsol_project="${KSWARM_SMOKE_BONSOL_PROJECT:-kswarm-bonsol-$$}"
bonsol_runtime="${BONSOL_RUNTIME_HOST_DIR:-${repo_dir}/runtime/bonsol}"
bonsol_rpc_port="${BONSOL_VALIDATOR_RPC_PORT:-38899}"
hook_port="${KSWARM_BONSOL_HOOK_PORT:-38099}"
aggregate_timeout="${KSWARM_SMOKE_AGGREGATE_TIMEOUT:-2400}"
hook_server_pid=""
bonsol_builder_container=""
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

# --- proven-aggregate mode -------------------------------------------------------
#
# The swarm compose has no Bonsol node, and it should not: proving is a service, not
# part of a hardened worker image. KSWARM_SMOKE_BONSOL=1 therefore composes two stacks.
#
#   docker-compose.bonsol.yml   validator (kswarm program loaded upgradeably), Bonsol
#                               node, image server, guest builder
#   docker-compose.swarm.yml    ipfs, branch worker, verifier, aggregator, CLI, pointed
#                               at that validator instead of their own
#
# and one host process, `protocol/scripts/bonsol-hook-server.py`, which holds the Bonsol
# CLI, the client keypair and the docker socket the aggregator image deliberately lacks.
# The aggregator reaches it over HTTP and still checks every digest it returns against
# its own reduction and against the job account.
#
# Everything the branch receipt path needs runs inside the swarm containers: the worker
# proves with the binary its image carries, and the verifier verifies with the same
# binary and refuses a receipt from any other guest.

bonsol_compose=(docker compose -f docker-compose.bonsol.yml -p "${bonsol_project}")

detect_host_ip() {
  # The address a swarm container uses to reach this host's published ports.
  if [ -n "${KSWARM_SMOKE_HOST_IP:-}" ]; then
    printf '%s' "${KSWARM_SMOKE_HOST_IP}"
    return 0
  fi
  ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1
}

bonsol_stack_up() {
  log "starting the Bonsol stack (project ${bonsol_project}, runtime ${bonsol_runtime})"
  mkdir -p "${bonsol_runtime}"
  # Recreate the validator by default. Its entrypoint resets the ledger, and the swarm
  # stack bootstraps a protocol and a stand-in mint on it; reusing a chain from an
  # earlier run leaves a mint whose authority is a wallet this run's config volume does
  # not have, and bootstrap refuses. KSWARM_SMOKE_BONSOL_RESET=0 keeps the chain.
  # The builder is recreated too, so every run re-checks that the guest ELFs its
  # manifests name are actually there. A manifest outlives the `target/` directory it
  # points into, and `bonsol deploy` only finds out much later.
  local recreate=()
  [ "${KSWARM_SMOKE_BONSOL_RESET:-1}" = "1" ] && recreate=(--force-recreate)
  # Locked: bringing this stack up builds the reducer guests on the host daemon.
  BONSOL_RUNTIME_HOST_DIR="${bonsol_runtime}" BONSOL_VALIDATOR_RPC_PORT="${bonsol_rpc_port}" \
  BONSOL_VALIDATOR_BIND="${BONSOL_VALIDATOR_BIND:-0.0.0.0}" \
    scripts/heavy-build-lock.sh "${bonsol_compose[@]}" up -d "${recreate[@]}" \
      bonsol-builder bonsol-validator bonsol-image-server bonsol-node >> "${log_path}" 2>&1 \
    || fail "the Bonsol stack did not start"
  # Count the healthy ones rather than the unhealthy ones: a container that exits
  # vanishes from `docker ps`, so "nothing unhealthy" would read as success.
  local deadline=$((SECONDS + 1800))
  local expected=4  # builder, validator, image server, node
  while :; do
    local healthy
    healthy="$(docker ps --filter "label=com.docker.compose.project=${bonsol_project}" --format '{{.Status}}' | grep -c '(healthy)' || true)"
    [ "${healthy}" = "${expected}" ] && break
    [ "${SECONDS}" -lt "${deadline}" ] || {
      docker ps -a --filter "label=com.docker.compose.project=${bonsol_project}" --format '{{.Names}} {{.Status}}' >> "${log_path}" 2>&1 || true
      fail "the Bonsol stack has ${healthy}/${expected} healthy containers"
    }
    sleep 10
  done
  bonsol_builder_container="$(docker ps --filter "label=com.docker.compose.project=${bonsol_project}" --filter "label=com.docker.compose.service=bonsol-builder" --format '{{.Names}}' | head -n 1)"
  [ -f "${bonsol_runtime}/aggregate-reducer-manifest.json" ] \
    || fail "no aggregate reducer manifest in ${bonsol_runtime}: the guest build did not run"
  log "Bonsol stack healthy, aggregate reducer $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["imageId"])' "${bonsol_runtime}/aggregate-reducer-manifest.json")"
}

seed_admin_wallet() {
  # `initialize_protocol` accepts only the program's upgrade authority, and on this
  # validator that is the Bonsol client keypair, so bootstrap has to find it as the
  # `admin` wallet. In the local profile wallets live in the `cli-config` docker volume,
  # not on the host, so the file is installed through a one-shot `cli` container that
  # mounts that volume. Bootstrap reuses an existing wallet file, so this is enough.
  #
  # The keypair itself is written by a root container, and is read out through docker
  # rather than off the host filesystem for the same reason.
  [ -n "${bonsol_builder_container}" ] || fail "no bonsol-builder container to read the client keypair from"
  # Piped, not staged: the key never touches this host's filesystem, and a bind mount
  # would need a mode the container's uid 10001 can read, which is the opposite of what
  # a keypair wants.
  docker exec "${bonsol_builder_container}" cat /runtime/bonsol/client-keypair.json \
    | "${compose[@]}" run --rm --no-deps -T --entrypoint /bin/sh cli -c \
      'umask 077 && mkdir -p /home/kswarm/.config/kswarm/wallets && cat > /home/kswarm/.config/kswarm/wallets/admin.json' \
    >> "${log_path}" 2>&1 || fail "could not install the admin wallet into the config volume"
  log "seeded the admin wallet with the program's upgrade authority"
}

write_bonsol_cli_wrappers() {
  # The hook shells out to `bonsol` and `solana`. Neither is on this host, and both are
  # in the builder container along with the keypairs and the compose network the
  # validator and image server are on, so the wrappers are `docker exec` one-liners
  # pointed at the container this run started.
  local builder="$1"
  local bin_dir="$(dirname "${log_path}")/bin"
  mkdir -p "${bin_dir}"
  printf '#!/usr/bin/env bash\nexec docker exec -i %s /opt/bonsol/bin/bonsol "$@"\n' "${builder}" > "${bin_dir}/bonsol"
  printf '#!/usr/bin/env bash\nexec docker exec -i %s solana "$@"\n' "${builder}" > "${bin_dir}/solana"
  chmod +x "${bin_dir}/bonsol" "${bin_dir}/solana"
  export KSWARM_BONSOL_CLI="$(cd "${bin_dir}" && pwd)/bonsol"
  export KSWARM_SOLANA_CLI="$(cd "${bin_dir}" && pwd)/solana"
  log "bonsol CLI wrappers in ${bin_dir} target ${builder}"
}

start_hook_server() {
  local bind_ip="$1"
  log "starting the proving service on ${bind_ip}:${hook_port}"
  # The service also serves the framed guest input the Bonsol node fetches, so nothing
  # else has to store it. `--public-base` is what the node dials, which is this host's
  # address rather than the bind address it would see from inside its own container.
  BONSOL_RUNTIME_HOST_DIR="${bonsol_runtime}" \
  KSWARM_BONSOL_KEYPAIR="${KSWARM_BONSOL_KEYPAIR:-/runtime/bonsol/client-keypair.json}" \
  KSWARM_BONSOL_MANIFEST="${bonsol_runtime}/aggregate-reducer-manifest.json" \
  KSWARM_BONSOL_CLI="${KSWARM_BONSOL_CLI:-}" \
  KSWARM_SOLANA_CLI="${KSWARM_SOLANA_CLI:-}" \
  KSWARM_BONSOL_RPC_URL="${KSWARM_BONSOL_RPC_URL:-http://bonsol-validator:8899}" \
  KSWARM_BONSOL_IMAGE_SERVER="${KSWARM_BONSOL_IMAGE_SERVER:-http://bonsol-image-server:8080}" \
    "${PYTHON:-python3}" protocol/scripts/bonsol-hook-server.py \
      --bind "${bind_ip}" --port "${hook_port}" --public-base "http://${bind_ip}:${hook_port}" \
    >> "${log_path}" 2>&1 &
  hook_server_pid=$!
  local deadline=$((SECONDS + 60))
  while :; do
    # The same address the Bonsol node will dial: the service binds that interface, not
    # loopback, so probing 127.0.0.1 would test something the node cannot reach.
    curl -fsS -m 3 "http://${bind_ip}:${hook_port}/healthz" >/dev/null 2>&1 && break
    kill -0 "${hook_server_pid}" 2>/dev/null || fail "the proving service exited on startup"
    [ "${SECONDS}" -lt "${deadline}" ] || fail "the proving service did not answer /healthz"
    sleep 1
  done
  log "proving service ready (pid ${hook_server_pid})"
}

aggregator_pass() {
  # One aggregation pass. The runner logs its own failures and still exits zero, so a
  # pass exiting zero proves nothing; the caller polls `predict status` for the
  # aggregate output_cid.
  #
  # In proven mode the runner reaches the host proving service and its receipt carries
  # a Bonsol marker. Otherwise it is told, explicitly, that an unproven receipt is
  # acceptable here, and that receipt can never settle.
  if [ "${bonsol_mode}" = "1" ]; then
    "${compose[@]}" run --rm --no-deps -T \
      -e KSWARM_BONSOL_AGGREGATE_COMMAND="python -m aggregator_runner.bonsol_http_hook" \
      -e KSWARM_BONSOL_HOOK_URL="http://${host_ip}:${hook_port}/prove" \
      -e KSWARM_BONSOL_HOOK_TIMEOUT_SECONDS="${aggregate_timeout}" \
      -e KSWARM_BONSOL_HOOK_URL_TIMEOUT_SECONDS="${aggregate_timeout}" \
      aggregator-runner --once --allow-completed-branches
  else
    "${compose[@]}" run --rm --no-deps -T \
      -e KSWARM_ALLOW_UNBOUND_AGGREGATE=1 \
      aggregator-runner --once --allow-completed-branches
  fi
}

verifier_pass() {
  # An extra verifier pass, for the aggregate attestation `settle_aggregate_proof_job`
  # requires. The long-running verifier does this on its own poll; running one on
  # demand keeps the wait bounded.
  "${compose[@]}" run --rm --no-deps -T verifier-worker --once
}

cleanup() {
  status=$?
  if [ -n "${hook_server_pid}" ]; then
    kill "${hook_server_pid}" 2>/dev/null || true
    wait "${hook_server_pid}" 2>/dev/null || true
  fi
  if [ "${keep}" = "1" ]; then
    log "keeping the stack up (project ${project}); tear down with: ${compose[*]} --profile local down -v"
    exit "${status}"
  fi
  log "collecting container logs"
  "${compose[@]}" --profile local logs --no-color --timestamps >> "${log_path}" 2>&1 || true
  log "tearing down"
  "${compose[@]}" --profile local --profile tools down -v --remove-orphans >> "${log_path}" 2>&1 || true
  if [ "${bonsol_mode}" = "1" ]; then
    # The Bonsol stack is left running on purpose: bringing it back costs a guest build
    # and a validator boot, and it holds no state this test wrote.
    log "the Bonsol stack is still up; stop it with: docker compose -f docker-compose.bonsol.yml -p ${bonsol_project} down -v"
    # Only this run's networks; another agent's stack must survive.
    docker network prune -f --filter "label=com.docker.compose.project=${project}" >> "${log_path}" 2>&1 || true
  fi
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

host_ip=""
if [ "${bonsol_mode}" = "1" ]; then
  host_ip="$(detect_host_ip)"
  [ -n "${host_ip}" ] || fail "could not detect the host address; set KSWARM_SMOKE_HOST_IP"
  bonsol_stack_up
  write_bonsol_cli_wrappers "${bonsol_builder_container}"
  # Every swarm service talks to the Bonsol validator instead of its own, so the jobs
  # the aggregator proves are on the chain the Bonsol node watches.
  export KSWARM_RPC_URL="http://${host_ip}:${bonsol_rpc_port}"
  export KSWARM_AGGREGATE_IMAGE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["imageId"])' "${bonsol_runtime}/aggregate-reducer-manifest.json")"
  log "swarm rpc ${KSWARM_RPC_URL}, aggregate reducer ${KSWARM_AGGREGATE_IMAGE_ID}"
  start_hook_server "${host_ip}"
fi

# --- build and start -------------------------------------------------------------
log "building images (sha ${KSWARM_GIT_SHA})"
# Under the host-wide lock: the zkVM builder stage saturates a machine, and more than
# one agent or operator can be driving this host.
scripts/heavy-build-lock.sh "${compose[@]}" --profile local --profile tools build >> "${log_path}" 2>&1 || fail "image build failed"
# After the images exist and before bootstrap: the wallet goes into the config volume
# through a `cli` container. Written as an `if` because `[ ... ] && cmd` as the last
# command of a line exits a `set -e` script when the test is false.
if [ "${bonsol_mode}" = "1" ]; then
  seed_admin_wallet
fi
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

# --- wait for both branches to be executed and attested -------------------------------
# The aggregate job does not exist yet: `predict open` only plans it, because its
# input_bundle_hash is a function of branch receipts that do not exist until now.
deadline=$((SECONDS + timeout_seconds))
while :; do
  status_json="$(cli predict status "${parent_run}")"
  summary="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
branches = [j for j in d["jobs"] if j["kind"] == "branch"]
attested = sum(1 for j in branches if j["status"] in ("completed", "settled") and j["verifier_hash"])
print(f"branches={len(branches)} attested={attested} statuses=" + ",".join(j["status"] for j in d["jobs"]))
print("READY" if attested == len(branches) == 2 else "WAIT")
')"
  log "${summary%$'\n'*}"
  case "${summary##*$'\n'}" in
    READY) break ;;
  esac
  [ "${SECONDS}" -lt "${deadline}" ] || fail "branches were not attested within ${timeout_seconds}s"
  sleep 10
done

# --- settle both branch jobs after the challenge window --------------------------------
# Settling first is what lets `predict bind-aggregate` run without
# --allow-completed-branches: the aggregate artifact is then built from receipts the
# chain has finalised.
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

# --- bind and aggregate ----------------------------------------------------------------
# The long-running aggregator-runner has no Bonsol hook and no unproven-aggregate
# permission, so it correctly refuses to claim the job. Stopping it keeps that refusal
# out of the log on every poll and keeps it from racing the one-shot pass below for the
# run manifest lock.
log "stopping the long-running aggregator-runner so it does not race the one-shot passes"
"${compose[@]}" --profile local stop aggregator-runner >> "${log_path}" 2>&1 || true

log "binding the aggregate job to the settled branch receipts"
bound="$(cli predict bind-aggregate "${parent_run}" --as customer)"
printf '%s\n' "${bound}" >> "${log_path}"
aggregate_job="$(printf '%s' "${bound}" | json_field 'd["aggregate_job"]')"
aggregate_input_cid="$(printf '%s' "${bound}" | json_field 'd["aggregate_input_cid"]')"
log "aggregate job ${aggregate_job} bound to artifact ${aggregate_input_cid}"

aggregate_deadline_seconds="${timeout_seconds}"
[ "${bonsol_mode}" = "1" ] && aggregate_deadline_seconds="${aggregate_timeout}"
deadline=$((SECONDS + aggregate_deadline_seconds))
while :; do
  aggregator_pass >> "${log_path}" 2>&1 || fail "the aggregator pass could not be started"
  status_json="$(cli predict status "${parent_run}")"
  summary="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
aggregate = [j for j in d["jobs"] if j["kind"] == "aggregate"]
aggregated = sum(1 for j in aggregate if j["status"] in ("completed", "settled") and j["output_cid"])
print("aggregated=" + str(aggregated) + " statuses=" + ",".join(j["kind"] + "=" + j["status"] for j in d["jobs"]))
print("READY" if aggregated == 1 else "WAIT")
')"
  log "${summary%$'\n'*}"
  case "${summary##*$'\n'}" in
    READY) break ;;
  esac
  [ "${SECONDS}" -lt "${deadline}" ] || fail "the aggregate was not submitted within ${aggregate_deadline_seconds}s"
  sleep 10
done

# --- settle the aggregate, when it carries a proof --------------------------------
aggregate_proven=false
settle_signature=""
marker_pda=""
if [ "${bonsol_mode}" = "1" ]; then
  log "attesting the aggregate reduction"
  attest_deadline=$((SECONDS + 300))
  while :; do
    verifier_pass >> "${log_path}" 2>&1 || true
    attestation="$(cli inspect job "${aggregate_job}" | json_field 'str(d["account"]["verifier_attestation_hash"])')"
    [ "${attestation}" != "None" ] && break
    [ "${SECONDS}" -lt "${attest_deadline}" ] || fail "the aggregate was not attested within 300s"
    sleep 5
  done
  log "aggregate attestation ${attestation}"

  # The runner waits for the execution, so the callback has normally landed by now; poll
  # anyway, because "the marker is not here yet" and "no proof was produced" look the
  # same for a few seconds and only one of them is a failure.
  marker_deadline=$((SECONDS + 300))
  while :; do
    marker_pda="$(cli inspect marker --job "${aggregate_job}" | json_field 'd[0]["marker"] if d else "None"')"
    [ "${marker_pda}" != "None" ] && break
    [ "${SECONDS}" -lt "${marker_deadline}" ] || fail "no BonsolAggregateVerification marker for ${aggregate_job}"
    sleep 10
  done
  log "Bonsol marker ${marker_pda}"

  # `settle_aggregate_proof_job` refuses until the challenge window has closed.
  log "waiting for the aggregate challenge window"
  window_deadline=$((SECONDS + challenge_window + 300))
  while :; do
    now="$(date -u +%s)"
    deadline_unix="$(cli inspect job "${aggregate_job}" | json_field 'd["account"]["challenge_deadline"]')"
    [ "${now}" -gt "${deadline_unix}" ] && break
    [ "${SECONDS}" -lt "${window_deadline}" ] || fail "the aggregate challenge window did not close"
    sleep 10
  done
  settled_json="$(cli settle-aggregate "${aggregate_job}")"
  printf '%s\n' "${settled_json}" >> "${log_path}"
  settle_signature="$(printf '%s' "${settled_json}" | json_field 'd["signature"]')"
  aggregate_state="$(cli inspect job "${aggregate_job}" | json_field 'd["account"]["status_name"]')"
  [ "${aggregate_state}" = "settled" ] || fail "the aggregate job is ${aggregate_state}, expected settled"
  aggregate_proven=true
  log "aggregate settled: signature ${settle_signature}"
fi

# --- report --------------------------------------------------------------------------
report="$(cli predict report "${parent_run}")"
printf '%s\n' "${report}" >> "${log_path}"
final_bps="$(printf '%s' "${report}" | json_field 'd["final_scalar_bps"]')"
branch_count="$(printf '%s' "${report}" | json_field 'd["aggregate_output"]["result"]["branch_count"]')"
[ "${final_bps}" != "None" ] || fail "predict report has no final scalar"
[ "${branch_count}" = "2" ] || fail "aggregate combined ${branch_count} branches, expected 2"
log "report: final_scalar_bps=${final_bps} branch_count=${branch_count}"

final="$(cli predict status "${parent_run}")"
printf '%s\n' "${final}" >> "${log_path}"
log "run ${parent_run}: $(printf '%s' "${final}" | python3 -c 'import json,sys; print(", ".join(j["kind"] + "=" + j["status"] for j in json.load(sys.stdin)["jobs"]))')"
if [ "${aggregate_proven}" = "true" ]; then
  log "the aggregate settled against its Bonsol marker"
else
  log "the aggregate job stays completed and unsettled on purpose: it carries no Bonsol proof"
fi
printf '{"swarm_smoke":{"parent_run":"%s","aggregate_job":"%s","aggregate_proven":%s,"bonsol_marker":"%s","settle_signature":"%s","final_scalar_bps":%s,"branch_count":%s,"settled_branch_jobs":["%s"]}}\n' \
  "${parent_run}" "${aggregate_job}" "${aggregate_proven}" "${marker_pda}" "${settle_signature}" "${final_bps}" "${branch_count}" "${branch_jobs// /\",\"}"
