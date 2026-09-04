# `IMAGE_ID`

The RISC Zero image id of the branch canonicalization guest in
`methods/guest`, as `host image-id` prints it.

It is what a verifier pins: a receipt naming any other guest is refused, so this
value decides which program a branch receipt is allowed to have come from. It is
a property of the compiled ELF, so the guest source, the shared reducer crate,
both lockfiles and the pinned RISC Zero toolchain all reach it.

`docker/swarm/Dockerfile` builds the guest with that pinned toolchain and fails
the build when the result differs from this file, so the id cannot drift
silently. The same file is installed into the worker images and is the default
`KSWARM_ZKVM_IMAGE_ID` when the environment does not set one.

To change it: rebuild, take the id the build reports, put it here, and say in
`docs/proof-layer-status.md` what changed in the guest. Changing it retires
every receipt the previous guest produced.
