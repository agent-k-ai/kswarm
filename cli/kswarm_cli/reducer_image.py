"""Default Bonsol reducer image id for aggregate-proof jobs.

`predict open` binds every aggregate-proof job to one reducer image: the job's
`required_software_digest` is this id, and only a worker registered with the
same software digest can claim the job. The value is the RISC Zero image id of
`protocol/bonsol-branch-reducer`.

Update rule: the reducer build (`protocol/scripts/run-bonsol-builder.sh`) writes
the current id to `runtime/bonsol/reducer-manifest.json` as `imageId`. Copy that
value here whenever the reducer guest changes. Until then, operators can point
one run at a different image with `--aggregate-image-id` or the
`KSWARM_AGGREGATE_IMAGE_ID` environment variable.

The guest's Cargo package name counts as a change to the guest, which is why the
kswarm rename left `mirofish_bonsol_branch_reducer` alone. The reason is recorded
at the top of `protocol/bonsol-branch-reducer/Cargo.toml`.
"""

from __future__ import annotations


# Recorded from the live flagship demo runs: the policy-passage forecast, the
# brand-crisis trajectory, and the OASIS replication study.
AGGREGATE_REDUCER_IMAGE_ID = "a41fa6df04dd43946b3ca83ae18b50397fbade7e4d5d801b5594feb3b6234d39"
