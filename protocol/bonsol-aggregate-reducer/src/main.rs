//! The Bonsol aggregate reducer guest.
//!
//! It reads one framed public input -- the aggregate artifact -- and commits a
//! 105-byte journal saying which combiner ran, over which parameters, what value it
//! produced, how many branches it read, and the Merkle root of those branches' receipt
//! hashes. See `kswarm_bonsol_aggregate_reducer::aggregate` for the layouts.
//!
//! The guest recomputes. It hashes each branch receipt itself and decodes the branch
//! values out of those bytes, so the value it commits is a function of receipts whose
//! hashes the Solana program already stores. A caller cannot hand it a summary.
//!
//! Any malformed or inconsistent artifact aborts the guest, so there is no receipt for
//! a bad input rather than a receipt for a different claim.

use kswarm_bonsol_aggregate_reducer::{aggregate_committed_outputs, reduce_aggregate_artifact};
use risc0_zkvm::{
    guest::{env, sha::Impl},
    sha::Sha256,
};

fn main() {
    let mut public_len_bytes = [0u8; 8];
    env::read_slice(&mut public_len_bytes);
    let public_len = u64::from_le_bytes(public_len_bytes) as usize;
    let mut public_bytes = vec![0u8; public_len];
    env::read_slice(&mut public_bytes);

    // The digest covers the length prefix as well, so a truncated artifact cannot be
    // reframed as a shorter valid one. This is the same rule the Bonsol execution
    // request commits as `inputHash`, and the same rule the job's `input_bundle_hash`
    // was opened against.
    let framed_input = [&public_len_bytes[..], public_bytes.as_slice()].concat();
    let input_digest = Impl::hash_bytes(&framed_input);

    let reduction = match reduce_aggregate_artifact(&public_bytes) {
        Ok(reduction) => reduction,
        Err(error) => panic!("aggregate artifact rejected: {error}"),
    };
    let outputs = aggregate_committed_outputs(&reduction);

    env::commit_slice(input_digest.as_bytes());
    env::commit_slice(&outputs);
}
