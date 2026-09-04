#!/bin/sh
set -eu

IPFS_PATH="${IPFS_PATH:-/data/ipfs}"
IPFS_RUNTIME_DIR="${IPFS_RUNTIME_DIR:-/runtime/ipfs}"
# Written by the bootstrap node on the control plane. Remote peers copy it into their own
# runtime/ipfs before starting; a missing key is an error, never a fallback to a public swarm.
SWARM_KEY_SOURCE="${SWARM_KEY_SOURCE:-${IPFS_RUNTIME_DIR}/swarm.key}"
BOOTSTRAP_ADDR_FILE="${IPFS_RUNTIME_DIR}/bootstrap.addr"
BOOTSTRAP_MULTIADDR="${IPFS_BOOTSTRAP_MULTIADDR:-}"
SWARM_KEY_WAIT_SECONDS="${IPFS_SWARM_KEY_WAIT_SECONDS:-120}"

mkdir -p "${IPFS_PATH}"

waited=0
while [ ! -f "${SWARM_KEY_SOURCE}" ]; do
  if [ "${waited}" -ge "${SWARM_KEY_WAIT_SECONDS}" ]; then
    echo "swarm key not found at ${SWARM_KEY_SOURCE} after ${SWARM_KEY_WAIT_SECONDS}s; copy it from the bootstrap machine's runtime/ipfs/swarm.key" >&2
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done

if [ ! -f "${IPFS_PATH}/config" ]; then
  ipfs init --profile=server >/dev/null
fi

# Kubo 0.40 ships 'auto' placeholders that only AutoConf can fill; with AutoConf disabled the
# daemon logs errors for them. Use the system resolver and no delegated routers or publishers.
ipfs config --json DNS.Resolvers '{}' >/dev/null
ipfs config --json Routing.DelegatedRouters '[]' >/dev/null
ipfs config --json Ipns.DelegatedPublishers '[]' >/dev/null
# The server profile refuses to dial or announce private address ranges ("gater disallows
# connection to peer"). This swarm is a private network (PSK) on private address space by
# design, so allow them.
ipfs config --json Swarm.AddrFilters '[]' >/dev/null
ipfs config --json Addresses.NoAnnounce '[]' >/dev/null

cp "${SWARM_KEY_SOURCE}" "${IPFS_PATH}/swarm.key"
chmod 600 "${IPFS_PATH}/swarm.key"
ipfs config --json AutoConf.Enabled false >/dev/null
ipfs config --json AutoTLS.Enabled false >/dev/null
ipfs config Routing.Type dht >/dev/null
ipfs config Addresses.API /ip4/0.0.0.0/tcp/5001 >/dev/null
ipfs config Addresses.Gateway /ip4/0.0.0.0/tcp/8080 >/dev/null

if [ -z "${BOOTSTRAP_MULTIADDR}" ]; then
  while [ ! -f "${BOOTSTRAP_ADDR_FILE}" ]; do
    sleep 1
  done
  BOOTSTRAP_MULTIADDR="$(cat "${BOOTSTRAP_ADDR_FILE}")"
fi

ipfs bootstrap rm --all >/dev/null
ipfs bootstrap add "${BOOTSTRAP_MULTIADDR}" >/dev/null

ipfs daemon --migrate=true &
DAEMON_PID=$!

until ipfs --api /ip4/127.0.0.1/tcp/5001 id >/dev/null 2>&1; do
  sleep 1
done

ipfs --api /ip4/127.0.0.1/tcp/5001 swarm connect "${BOOTSTRAP_MULTIADDR}" >/dev/null 2>&1 || true

wait "${DAEMON_PID}"
