#!/bin/sh
# Generate an IPFS private-network swarm key (32 random bytes, base16) at the given path.
# POSIX sh and busybox-compatible so it runs inside the kubo image. Refuses to overwrite.
set -eu

target="${1:-}"
if [ -z "${target}" ]; then
  echo "usage: generate-swarm-key.sh <path/to/swarm.key>" >&2
  exit 2
fi
if [ -e "${target}" ]; then
  echo "swarm key already exists: ${target}" >&2
  exit 1
fi

umask 077
mkdir -p "$(dirname "${target}")"
hex="$(head -c 32 /dev/urandom | od -An -v -tx1 | tr -d ' \n')"
if [ "${#hex}" -ne 64 ]; then
  echo "failed to read 32 random bytes" >&2
  exit 1
fi
printf '/key/swarm/psk/1.0.0/\n/base16/\n%s\n' "${hex}" > "${target}.tmp"
chmod 600 "${target}.tmp"
mv "${target}.tmp" "${target}"
echo "wrote swarm key to ${target}"
