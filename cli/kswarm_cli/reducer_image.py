"""The pinned RISC Zero image id of the Bonsol aggregate reducer.

`predict bind-aggregate` writes this id into the aggregate job's
`required_software_digest`, so only a worker registered with the same digest can
claim the job, and `settle_aggregate_proof_job` pays only when the Bonsol marker
carries the same id. It is therefore the one value that says *which program* produced
the aggregate result.

The id is the image id of `protocol/bonsol-aggregate-reducer`. It is a property of the
compiled ELF: the guest source, its dependencies, the crate name, and the RISC Zero
toolchain all reach it. Nothing here derives it -- it is recorded from a real build.

Rebuild and re-pin:

```bash
protocol/scripts/build-aggregate-reducer.sh          # bonsol build + rewrite this file
```

The script runs `bonsol build --zk-program-path protocol/bonsol-aggregate-reducer`
inside the pinned builder image, copies the resulting `manifest.json` to
`runtime/bonsol/aggregate-reducer-manifest.json`, and rewrites the constant below from
its `imageId`. `docs/proof-layer-status.md` records the procedure and the current
value. Operators can point one run at a different image with `--aggregate-image-id` or
the `KSWARM_AGGREGATE_IMAGE_ID` environment variable.

An empty pin fails closed: `resolve_aggregate_image_id` refuses rather than opening an
aggregate job against an image id nobody built.
"""

from __future__ import annotations


# Recorded from `protocol/scripts/build-aggregate-reducer.sh` on 2026-09-04, built from
# `protocol/bonsol-aggregate-reducer` with the pinned `kswarm-bonsol-eval` builder image.
AGGREGATE_REDUCER_IMAGE_ID = "785b584bc39a38d76e10fd0bb0c75cab62ae582497b577d03e6c1a9659204f4d"

# The previous pin named `protocol/bonsol-branch-reducer`, whose guest commits the
# statistics its caller supplied. That reducer cannot consume an aggregate artifact, so
# every aggregate job opened against it was opened unbound and could never settle. It
# is kept only for the Bonsol callback smoke test, which exercises marker and replay
# semantics that do not depend on which guest ran.
#
# What the pinned toolchain builds, confirmed by rebuilding that guest against the
# digest-pinned RISC Zero components. `a41fa6df04dd43946b3ca83ae18b50397fbade7e4d5d801b5594feb3b6234d39`
# is the id the flagship demo transcripts record: it was produced before any of this was
# pinned, when `rzup install` took the newest of everything, and it is kept in those
# transcripts because they describe the run that produced it. Nothing reads this
# constant except the vectors test that names it.
LEGACY_BRANCH_REDUCER_IMAGE_ID = "6017b38ca12ad7fbc9b4f9db6005b726e292c5d8dc4022e3130fe6654f66ccfb"
