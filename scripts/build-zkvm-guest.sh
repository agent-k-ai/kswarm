#!/usr/bin/env bash
# Compile the branch canonicalization guest, and the host binary that carries it,
# at the one location the pinned image id was recorded from.
#
#   scripts/build-zkvm-guest.sh <source-tree> <out-dir>
#
# <source-tree> is a directory holding `protocol/zkvm-reducer` and
# `protocol/bonsol-aggregate-reducer`; the repository root is one. The script
# copies both crates to ZKVM_GUEST_BUILD_ROOT and builds there under
# ZKVM_GUEST_BUILD_HOME, so no caller chooses the build location. That is the
# whole point of the script: the guest's image id is a function of both paths.
#
# It writes
#   <out-dir>/bin/kswarm-zkvm-reducer      the host binary, guest ELF compiled in
#   <out-dir>/share/kswarm-zkvm-image-id   the id of the guest inside it
# and exits non-zero when the id it built is not the one
# `protocol/zkvm-reducer/IMAGE_ID` pins, because a verifier running this build
# would otherwise refuse every receipt a worker running it produced.
#
# Why the paths reach the id, measured 2026-09-04 (`protocol/zkvm-reducer/IMAGE_ID.md`
# holds the table): the guest carries `core::panic::Location` strings, and those
# carry the compile-time path of every file that can panic -- its own crate, the
# reducer crate it shares with the host, and every registry dependency.
# `risc0-build` removes CARGO_HOME along with every other CARGO* variable before
# it invokes the guest build, so the registry path follows $HOME and not
# CARGO_HOME. The host Rust toolchain does not reach the id at all: `risc0-build`
# sets RUSTC to the rzup RISC Zero rustc.
set -euo pipefail

source_tree="${1:-}"
out_dir="${2:-}"
if [ -z "${source_tree}" ] || [ -z "${out_dir}" ]; then
  echo "usage: $0 <source-tree> <out-dir>" >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${RISC0_TOOLCHAIN_ENV:-${here}/../protocol/risc0-toolchain.env}"
if [ ! -f "${env_file}" ]; then
  echo "build-zkvm-guest: no declaration at ${env_file}" >&2
  exit 1
fi
# shellcheck disable=SC1090
. "${env_file}"

for var in RISC0_RUST_VERSION ZKVM_GUEST_BUILD_ROOT ZKVM_GUEST_BUILD_HOME; do
  if [ -z "${!var:-}" ]; then
    echo "build-zkvm-guest: ${env_file} does not declare ${var}" >&2
    exit 1
  fi
done

source_tree="$(cd "${source_tree}" && pwd)"
for crate in protocol/zkvm-reducer protocol/bonsol-aggregate-reducer; do
  if [ ! -d "${source_tree}/${crate}" ]; then
    echo "build-zkvm-guest: ${source_tree} has no ${crate}" >&2
    exit 1
  fi
done

# The build has to own both paths. Nothing here can fall back to a writable
# alternative: a different $HOME is a different image id, not a slower build.
mkdir -p "${ZKVM_GUEST_BUILD_HOME}" 2>/dev/null || true
if [ ! -w "${ZKVM_GUEST_BUILD_HOME}" ]; then
  echo "build-zkvm-guest: ${ZKVM_GUEST_BUILD_HOME} is not writable by uid $(id -u)" >&2
  echo "  the pinned image id embeds ${ZKVM_GUEST_BUILD_HOME}/.cargo/registry/... in the" >&2
  echo "  guest's panic locations, so the guest must be compiled by a user whose \$HOME" >&2
  echo "  is that directory. In an image, build it in a stage that runs as root." >&2
  exit 1
fi

export HOME="${ZKVM_GUEST_BUILD_HOME}"

# The compiler. `risc0-build` picks the rzup RISC Zero Rust toolchain, so assert
# that exactly one is installed and that it is the declared version, rather than
# discovering a silent toolchain change through a failed id comparison.
toolchains_dir="${HOME}/.risc0/toolchains"
mapfile -t rust_toolchains < <(find "${toolchains_dir}" -maxdepth 1 -type d -name 'v*-rust-*' 2>/dev/null | sort)
if [ "${#rust_toolchains[@]}" -ne 1 ]; then
  echo "build-zkvm-guest: expected exactly one RISC Zero Rust toolchain in ${toolchains_dir}," >&2
  echo "  found ${#rust_toolchains[@]}: ${rust_toolchains[*]:-none}" >&2
  echo "  run scripts/install-risc0-toolchain.sh with the same \$HOME first" >&2
  exit 1
fi
case "$(basename "${rust_toolchains[0]}")" in
  "v${RISC0_RUST_VERSION}-rust-"*) ;;
  *)
    echo "build-zkvm-guest: installed RISC Zero Rust toolchain is $(basename "${rust_toolchains[0]}")," >&2
    echo "  but protocol/risc0-toolchain.env declares ${RISC0_RUST_VERSION}" >&2
    exit 1
    ;;
esac

build_root="${ZKVM_GUEST_BUILD_ROOT}/protocol"
rm -rf "${build_root}"
mkdir -p "${build_root}"
cp -a "${source_tree}/protocol/zkvm-reducer" "${build_root}/zkvm-reducer"
cp -a "${source_tree}/protocol/bonsol-aggregate-reducer" "${build_root}/bonsol-aggregate-reducer"
# A `target/` from the caller's tree is not an input and must not be one: `bonsol
# build` leaves a root-owned one behind, and it would only slow this build down.
rm -rf "${build_root}/zkvm-reducer/target" "${build_root}/bonsol-aggregate-reducer/target"

# `--locked` on the workspace, RISC0_BUILD_LOCKED on the guest: `risc0-build`
# passes `--locked` to the guest's own cargo only when that variable is set, and
# the guest resolves `methods/guest/Cargo.lock`, not the workspace lock.
export RISC0_BUILD_LOCKED=1
export CARGO_TERM_COLOR=never

cd "${build_root}/zkvm-reducer"
cargo build --release --locked -p host

mkdir -p "${out_dir}/bin" "${out_dir}/share"
install -m 0755 target/release/host "${out_dir}/bin/kswarm-zkvm-reducer"

built="$("${out_dir}/bin/kswarm-zkvm-reducer" image-id)"
pinned="$(tr -d '[:space:]' < IMAGE_ID)"
if [ "${built}" != "${pinned}" ]; then
  echo "guest image id drift: built ${built}, protocol/zkvm-reducer/IMAGE_ID pins ${pinned}" >&2
  echo "  built at ${build_root}/zkvm-reducer with HOME=${HOME}" >&2
  echo "  and RISC Zero rust ${RISC0_RUST_VERSION}, cpp ${RISC0_CPP_VERSION:-unset}," >&2
  echo "  r0vm ${RISC0_R0VM_VERSION:-unset}, cargo-risczero ${RISC0_CARGO_RISCZERO_VERSION:-unset}" >&2
  echo "  Those six values and the guest source are the whole input; see" >&2
  echo "  protocol/zkvm-reducer/IMAGE_ID.md before changing the pin." >&2
  exit 1
fi

printf '%s\n' "${built}" > "${out_dir}/share/kswarm-zkvm-image-id"
chmod 0644 "${out_dir}/share/kswarm-zkvm-image-id"
echo "branch canonicalization guest image id ${built}"
