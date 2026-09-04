//! The branch canonicalization guest.
//!
//! It is handed one branch output document and recomputes the receipt the worker
//! claimed: canonical JSON of the hash preimage, the canonical hash, the `MFB2`
//! encoding, and `sha256` of that encoding. It commits
//! `input_digest || result_hash || output_len`.
//!
//! The rules live in `kswarm_bonsol_aggregate_reducer::branch_receipt`, so the guest,
//! the host verifier and the Python verifier share one definition.
//!
//! A malformed document aborts the guest, so there is no receipt for a document the
//! worker could not have produced.

use kswarm_bonsol_aggregate_reducer::branch_receipt::branch_receipt_journal;
use risc0_zkvm::guest::env;

fn main() {
    let payload: Vec<u8> = env::read();
    let journal = match branch_receipt_journal(&payload) {
        Ok(journal) => journal,
        Err(error) => panic!("branch output rejected: {error}"),
    };
    env::commit_slice(&journal.journal_bytes());
}
