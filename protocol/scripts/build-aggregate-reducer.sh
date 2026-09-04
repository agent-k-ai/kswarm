#!/usr/bin/env bash
# Build the Bonsol aggregate reducer guest and rewrite its pin.
#
# The image id in `cli/kswarm_cli/reducer_image.py` is every aggregate job's
# `required_software_digest`: it decides who may claim the job and which Bonsol marker
# can settle it. It is a property of the compiled ELF -- the guest source, its
# dependencies, the crate name and the RISC Zero toolchain all reach it -- so it is
# recorded from a real build and never derived.
#
#   protocol/scripts/build-aggregate-reducer.sh              # build, print, rewrite the pin
#   protocol/scripts/build-aggregate-reducer.sh --check      # build and compare, change nothing
#
# The build runs inside the pinned `kswarm-bonsol-eval` image, which is the same
# builder `docker-compose.bonsol.yml` uses, so the id this prints is the id the Bonsol
# node will serve. `BONSOL_RUNTIME_HOST_DIR` selects the runtime directory the manifest
# is copied to (default `./runtime/bonsol`).
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_dir="${BONSOL_RUNTIME_HOST_DIR:-${repo_dir}/runtime/bonsol}"
manifest_path="${runtime_dir}/aggregate-reducer-manifest.json"
pin_file="${repo_dir}/cli/kswarm_cli/reducer_image.py"
check_only=0

if [ "${1:-}" = "--check" ]; then
  check_only=1
fi

mkdir -p "${runtime_dir}"
# A stale manifest would be reported as a fresh build.
rm -f "${manifest_path}" "${repo_dir}/protocol/bonsol-aggregate-reducer/manifest.json"

# Both steps go through the host-wide lock: they saturate the machine, and a build host
# can be shared with another agent or operator whose own "one build at a time" rule
# cannot see this one.
lock="${repo_dir}/scripts/heavy-build-lock.sh"
echo "building the aggregate reducer guest (this runs the RISC Zero docker build)" >&2
"${lock}" docker compose -f "${repo_dir}/docker-compose.bonsol.yml" build bonsol-builder
BONSOL_RUNTIME_HOST_DIR="${runtime_dir}" "${lock}" docker compose -f "${repo_dir}/docker-compose.bonsol.yml" \
  run --rm --no-deps -T -e BONSOL_BUILDER_KEEPALIVE=0 bonsol-builder

if [ ! -f "${manifest_path}" ]; then
  echo "no manifest at ${manifest_path}: the guest build failed" >&2
  exit 1
fi

image_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["imageId"])' "${manifest_path}")"
if [ "${#image_id}" -ne 64 ]; then
  echo "manifest image id is not 64 hex digits: ${image_id}" >&2
  exit 1
fi
echo "aggregate reducer image id: ${image_id}" >&2

current="$(python3 -c 'import re,sys; print(re.search(r"^AGGREGATE_REDUCER_IMAGE_ID = \"([0-9a-f]*)\"", open(sys.argv[1]).read(), re.M).group(1))' "${pin_file}")"
if [ "${check_only}" = "1" ]; then
  if [ "${current}" != "${image_id}" ]; then
    echo "pin is ${current:-<unset>}, build is ${image_id}" >&2
    exit 1
  fi
  echo "pin matches the build" >&2
  exit 0
fi

python3 - "${pin_file}" "${image_id}" <<'PY'
import pathlib
import re
import sys

path, image_id = pathlib.Path(sys.argv[1]), sys.argv[2]
source = path.read_text(encoding="utf-8")
updated = re.sub(
    r'^AGGREGATE_REDUCER_IMAGE_ID = "[0-9a-f]*"$',
    f'AGGREGATE_REDUCER_IMAGE_ID = "{image_id}"',
    source,
    count=1,
    flags=re.M,
)
if updated == source and f'"{image_id}"' not in source:
    raise SystemExit(f"could not rewrite the pin in {path}")
path.write_text(updated, encoding="utf-8")
PY

echo "rewrote ${pin_file}" >&2
echo "${image_id}"
