#!/usr/bin/env bash
# Install the RISC Zero components `protocol/risc0-toolchain.env` declares.
#
#   scripts/install-risc0-toolchain.sh
#
# Every image that compiles a RISC Zero guest calls this instead of writing
# `rzup install` lines of its own. `rzup install <component>` with no version
# takes the newest of each, and a guest ELF is a function of its compiler, so an
# unpinned install silently moves every image id the container builds. Naming the
# versions in each Dockerfile fixes that but replaces it with a second problem:
# two Dockerfiles holding the same four literals drift apart the moment one of
# them is edited. One file, read by one script, cannot.
#
# RISC0_TOOLCHAIN_ENV overrides where the declaration is read from; the default is
# the repository copy next to this script.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${RISC0_TOOLCHAIN_ENV:-${here}/../protocol/risc0-toolchain.env}"

if [ ! -f "${env_file}" ]; then
  echo "install-risc0-toolchain: no declaration at ${env_file}" >&2
  echo "  a Dockerfile that calls this script must copy protocol/risc0-toolchain.env" >&2
  echo "  alongside it, or set RISC0_TOOLCHAIN_ENV to where it put it" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "${env_file}"

for var in RISC0_RUST_VERSION RISC0_CPP_VERSION RISC0_R0VM_VERSION RISC0_CARGO_RISCZERO_VERSION; do
  if [ -z "${!var:-}" ]; then
    echo "install-risc0-toolchain: ${env_file} does not declare ${var}" >&2
    exit 1
  fi
done

# rzup itself is not part of the guest's compiler: it is the installer that fetches
# it. Whatever version is already on PATH is used, and the official installer
# provides one when there is none.
if ! command -v rzup >/dev/null 2>&1; then
  curl -L https://risczero.com/install | bash
  export PATH="${HOME}/.risc0/bin:${PATH}"
fi
if ! command -v rzup >/dev/null 2>&1; then
  echo "install-risc0-toolchain: rzup is still not on PATH after installing it" >&2
  exit 1
fi

rzup install rust "${RISC0_RUST_VERSION}"
rzup install cpp "${RISC0_CPP_VERSION}"
rzup install r0vm "${RISC0_R0VM_VERSION}"
rzup install cargo-risczero "${RISC0_CARGO_RISCZERO_VERSION}"
rzup show
