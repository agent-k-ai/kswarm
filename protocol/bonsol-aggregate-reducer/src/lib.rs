//! One definition of every rule the proof layer depends on, and the library half of
//! the Bonsol aggregate reducer guest (`src/main.rs`).
//!
//! A zero-knowledge guest, the callback harness, the Solana-side vectors and the
//! Python stack all have to agree on these bytes exactly. Where they disagree, a job
//! is opened against a journal hash the guest will never produce, and the job can
//! never settle. So the rules live here once and everything else calls in.
//!
//! * [`combiner`] -- the three combiners, in exact integer arithmetic for the value
//!   the guest commits, and in the historical `f64` form for compatibility.
//! * [`merkle`] -- the sorted branch Merkle root (RFC 6962 style domain separation).
//! * [`mfb2`] -- the branch receipt encoding the Solana program stores the hash of.
//! * [`aggregate`] -- the aggregate artifact this crate's guest consumes and the
//!   journal it commits.
//! * [`canonical_json`] -- the deterministic JSON encoding the branch canonical hash
//!   is taken over.
//! * [`branch_receipt`] -- the off-chain branch canonicalization journal, used by the
//!   `protocol/zkvm-reducer` guest.
//!
//! The crate is `alloc`-only so a RISC Zero guest can depend on it. Why the library
//! shares a package with the guest rather than sitting in a crate of its own is
//! recorded at the top of `Cargo.toml`.

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;

pub mod aggregate;
pub mod branch_receipt;
pub mod canonical_json;
pub mod combiner;
pub mod merkle;
pub mod mfb2;

pub use aggregate::{
    aggregate_committed_outputs, aggregate_journal, reduce_aggregate_artifact, AggregateError,
    AggregateJournal, AggregateReduction, AGGREGATE_COMMITTED_OUTPUTS_LEN, AGGREGATE_JOURNAL_LEN,
    MAX_BRANCHES,
};
pub use branch_receipt::{
    branch_receipt_journal, recompute_branch_receipt, BranchReceiptError, BranchReceiptJournal,
    BRANCH_RECEIPT_JOURNAL_LEN,
};
pub use combiner::{
    combiner_params_digest, majority_vote, trim_count_from_bps, trimmed_mean, trimmed_mean_bps,
    validate_combiner_id, weighted_mean, weighted_mean_bps, CategoricalVote, CombinerError,
    CombinerParams, TrimmedMeanResult, WeightedValue, COMBINER_MAJORITY_VOTE, COMBINER_TRIMMED_MEAN,
    COMBINER_WEIGHTED_MEAN,
};
pub use merkle::{
    merkle_leaf_hash, merkle_node_hash, sorted_branches_merkle_root, MERKLE_LEAF_PREFIX,
    MERKLE_NODE_PREFIX,
};
pub use canonical_json::{canonical_json_bytes, CanonicalJsonError};
pub use mfb2::{
    parse_branch_result_bytes, BranchResult, Mfb2Error, OutputKind, MFB2_MAGIC,
    MFB2_SCHEMA_VERSION,
};

use sha2::{Digest, Sha256};

/// SHA-256 as a fixed array. Every digest in this crate is this function.
pub fn sha256(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize().into()
}

/// The Bonsol public-input frame: a little-endian `u64` byte length, then the bytes.
///
/// The guest reads the length first and then exactly that many bytes, so the digest
/// covers the length as well and a truncated input cannot be reframed as a shorter
/// valid one.
pub fn framed_input(payload: &[u8]) -> alloc::vec::Vec<u8> {
    let mut framed = alloc::vec::Vec::with_capacity(8 + payload.len());
    framed.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    framed.extend_from_slice(payload);
    framed
}

/// `input_digest` for a Bonsol execution: SHA-256 over the framed input.
pub fn framed_input_digest(payload: &[u8]) -> [u8; 32] {
    sha256(&framed_input(payload))
}

/// The program's rule: `journal_hash = sha256(input_digest || committed_outputs)`.
pub fn journal_hash(input_digest: &[u8; 32], committed_outputs: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(input_digest);
    hasher.update(committed_outputs);
    hasher.finalize().into()
}

/// Decode lowercase hex into a fixed-size array. Uppercase and `0x` are rejected:
/// the encoding this crate reads is always lowercase and unprefixed, and accepting
/// a second spelling would let two different strings hash to one artifact.
pub fn decode_hex_array<const N: usize>(text: &str) -> Option<[u8; N]> {
    let bytes = decode_hex(text)?;
    if bytes.len() != N {
        return None;
    }
    let mut out = [0u8; N];
    out.copy_from_slice(&bytes);
    Some(out)
}

/// Decode lowercase hex into bytes. An odd digit count or any non-hex byte is `None`.
pub fn decode_hex(text: &str) -> Option<alloc::vec::Vec<u8>> {
    let digits = text.as_bytes();
    if digits.len() % 2 != 0 {
        return None;
    }
    let mut out = alloc::vec::Vec::with_capacity(digits.len() / 2);
    for pair in digits.chunks(2) {
        out.push((hex_nibble(pair[0])? << 4) | hex_nibble(pair[1])?);
    }
    Some(out)
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        _ => None,
    }
}

/// Lowercase hex, the spelling every artifact in this repository uses.
pub fn encode_hex(bytes: &[u8]) -> alloc::string::String {
    use alloc::string::String;
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec;

    #[test]
    fn framed_input_prefixes_a_little_endian_u64_length() {
        assert_eq!(framed_input(b"ab"), vec![2, 0, 0, 0, 0, 0, 0, 0, b'a', b'b']);
        assert_eq!(framed_input(b""), vec![0u8; 8]);
    }

    #[test]
    fn framed_input_digest_matches_an_independent_hash() {
        // sha256(0200000000000000 || "ab"), computed with Python hashlib.
        assert_eq!(
            encode_hex(&framed_input_digest(b"ab")),
            "5dff2b3fa79721a8181b9beb1db6bcce93b482f9aa3a5c9b864fc4429e31d5f2"
        );
    }

    #[test]
    fn hex_round_trips_and_rejects_a_second_spelling() {
        assert_eq!(encode_hex(&[0x0a, 0xff]), "0aff");
        assert_eq!(decode_hex("0aff"), Some(vec![0x0a, 0xff]));
        assert_eq!(decode_hex("0AFF"), None);
        assert_eq!(decode_hex("0x0aff"), None);
        assert_eq!(decode_hex("0af"), None);
        assert_eq!(decode_hex_array::<2>("0aff"), Some([0x0a, 0xff]));
        assert_eq!(decode_hex_array::<3>("0aff"), None);
    }
}
