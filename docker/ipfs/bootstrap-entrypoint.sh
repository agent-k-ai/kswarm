#!/bin/sh
set -eu

IPFS_PATH="${IPFS_PATH:-/data/ipfs}"
IPFS_RUNTIME_DIR="${IPFS_RUNTIME_DIR:-/runtime/ipfs}"
# The private-network key is generated here on first start, never checked in. Peers on other
# machines need a copy of this file.
SWARM_KEY_SOURCE="${SWARM_KEY_SOURCE:-${IPFS_RUNTIME_DIR}/swarm.key}"
BOOTSTRAP_ADDR_FILE="${IPFS_RUNTIME_DIR}/bootstrap.addr"
BOOTSTRAP_ADVERTISE_KIND="${IPFS_BOOTSTRAP_ADVERTISE_KIND:-dns4}"
BOOTSTRAP_ADVERTISE_HOST="${IPFS_BOOTSTRAP_ADVERTISE_HOST:-ipfs-bootstrap}"

mkdir -p "${IPFS_PATH}" "${IPFS_RUNTIME_DIR}"

if [ ! -f "${SWARM_KEY_SOURCE}" ]; then
  sh /config/generate-swarm-key.sh "${SWARM_KEY_SOURCE}"
fi

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
ipfs bootstrap rm --all >/dev/null
ipfs config --json AutoConf.Enabled false >/dev/null
ipfs config --json AutoTLS.Enabled false >/dev/null
ipfs config Routing.Type dht >/dev/null
ipfs config Addresses.API /ip4/0.0.0.0/tcp/5001 >/dev/null
ipfs config Addresses.Gateway /ip4/0.0.0.0/tcp/8080 >/dev/null

PEER_ID="$(ipfs config Identity.PeerID)"
echo "/${BOOTSTRAP_ADVERTISE_KIND}/${BOOTSTRAP_ADVERTISE_HOST}/tcp/4001/p2p/${PEER_ID}" > "${BOOTSTRAP_ADDR_FILE}.tmp"
mv "${BOOTSTRAP_ADDR_FILE}.tmp" "${BOOTSTRAP_ADDR_FILE}"

exec ipfs daemon --migrate=true
