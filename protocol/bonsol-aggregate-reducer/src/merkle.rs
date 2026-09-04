//! Merkle root over the sorted branch result hashes.
//!
//! Leaves and inner nodes use different hash prefixes (RFC 6962 style), so a leaf can
//! never be confused with a node. An odd node at the end of a level is promoted
//! unchanged; it is never paired with a copy of itself. Together these rules make the
//! root a unique function of the sorted leaf list. Pairing an odd node with itself
//! makes `[A, B, B]` and `[A, B, B, B]` produce the same root (CVE-2012-2459 class).

use alloc::vec::Vec;
use sha2::{Digest, Sha256};

use crate::combiner::CombinerError;

/// `SHA256(0x00 || leaf)`.
pub const MERKLE_LEAF_PREFIX: u8 = 0x00;
/// `SHA256(0x01 || left || right)`.
pub const MERKLE_NODE_PREFIX: u8 = 0x01;

pub fn sorted_branches_merkle_root(branch_hashes: &[[u8; 32]]) -> Result<[u8; 32], CombinerError> {
    if branch_hashes.is_empty() {
        return Err(CombinerError::EmptyBranches);
    }
    let mut leaves = branch_hashes.to_vec();
    leaves.sort();
    let mut level: Vec<[u8; 32]> = leaves.iter().map(merkle_leaf_hash).collect();
    while level.len() > 1 {
        let mut next = Vec::with_capacity(level.len().div_ceil(2));
        for pair in level.chunks(2) {
            match pair.get(1) {
                Some(right) => next.push(merkle_node_hash(&pair[0], right)),
                None => next.push(pair[0]),
            }
        }
        level = next;
    }
    Ok(level[0])
}

pub fn merkle_leaf_hash(leaf: &[u8; 32]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update([MERKLE_LEAF_PREFIX]);
    hasher.update(leaf);
    hasher.finalize().into()
}

pub fn merkle_node_hash(left: &[u8; 32], right: &[u8; 32]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update([MERKLE_NODE_PREFIX]);
    hasher.update(left);
    hasher.update(right);
    hasher.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decode_hex_array;
    use proptest::prelude::*;

    const A: [u8; 32] = [0x11; 32];
    const B: [u8; 32] = [0x22; 32];
    const C: [u8; 32] = [0x33; 32];

    /// The scheme this repository shipped before the domain separation was added.
    /// Kept only to prove the regression vector really collided.
    fn legacy_duplicate_last_root(branch_hashes: &[[u8; 32]]) -> [u8; 32] {
        let mut level = branch_hashes.to_vec();
        level.sort();
        while level.len() > 1 {
            let mut next = Vec::with_capacity(level.len().div_ceil(2));
            for pair in level.chunks(2) {
                let right = pair.get(1).unwrap_or(&pair[0]);
                let mut hasher = Sha256::new();
                hasher.update(pair[0]);
                hasher.update(right);
                next.push(hasher.finalize().into());
            }
            level = next;
        }
        level[0]
    }

    #[test]
    fn merkle_root_matches_the_golden_vectors_of_the_branch_reducer() {
        // Computed independently with Python hashlib:
        // L(x) = sha256(0x00 || x); N(l, r) = sha256(0x01 || l || r).
        assert_eq!(
            sorted_branches_merkle_root(&[A]).unwrap(),
            decode_hex_array("4635e1fa62a599a7880a8d14a56f720a1d40f6e5448ab5a5e39bedc8bd87fa8e").unwrap()
        );
        assert_eq!(
            sorted_branches_merkle_root(&[B, A]).unwrap(),
            decode_hex_array("cc15b132263fd4fd2748c0e7cb9e1c4ad0afe70fcf9382ee644c4da8af0286a5").unwrap()
        );
        assert_eq!(
            sorted_branches_merkle_root(&[A, B, C]).unwrap(),
            decode_hex_array("9bee4401962e94b921336a7910a5a9718836ffcbc545dde0a3f34d858beb5752").unwrap()
        );
        assert_eq!(
            sorted_branches_merkle_root(&[A, B, C, C]).unwrap(),
            decode_hex_array("4118b0b8b03727613a79962aa22cb29474c01378848625423390a5b36e6735a0").unwrap()
        );
    }

    #[test]
    fn merkle_root_regression_duplicate_last_leaf_no_longer_collides() {
        let three = [A, B, B];
        let four = [A, B, B, B];
        assert_eq!(
            legacy_duplicate_last_root(&three),
            legacy_duplicate_last_root(&four),
            "the legacy scheme must collide on this vector or the regression test is wrong"
        );
        assert_ne!(
            sorted_branches_merkle_root(&three).unwrap(),
            sorted_branches_merkle_root(&four).unwrap()
        );
    }

    #[test]
    fn merkle_root_single_leaf_is_the_leaf_hash_not_the_raw_leaf() {
        assert_eq!(sorted_branches_merkle_root(&[A]).unwrap(), merkle_leaf_hash(&A));
        assert_ne!(sorted_branches_merkle_root(&[A]).unwrap(), A);
    }

    #[test]
    fn merkle_root_inner_node_cannot_pose_as_a_leaf() {
        let inner = merkle_node_hash(&merkle_leaf_hash(&A), &merkle_leaf_hash(&B));
        assert_ne!(
            sorted_branches_merkle_root(&[A, B, C]).unwrap(),
            sorted_branches_merkle_root(&[inner, C]).unwrap()
        );
    }

    #[test]
    fn merkle_root_rejects_empty_input() {
        assert_eq!(
            sorted_branches_merkle_root(&[]),
            Err(CombinerError::EmptyBranches)
        );
    }

    proptest! {
        #[test]
        fn root_is_invariant_under_reordering(mut hashes in prop::collection::vec(any::<[u8; 32]>(), 1..32)) {
            let original = sorted_branches_merkle_root(&hashes)?;
            hashes.sort();
            hashes.reverse();
            prop_assert_eq!(original, sorted_branches_merkle_root(&hashes)?);
        }

        #[test]
        fn root_changes_when_the_last_leaf_is_duplicated(hashes in prop::collection::vec(any::<[u8; 32]>(), 1..32)) {
            let original = sorted_branches_merkle_root(&hashes)?;
            let mut extended = hashes.clone();
            extended.push(*hashes.iter().max().unwrap());
            prop_assert_ne!(original, sorted_branches_merkle_root(&extended)?);
        }

        #[test]
        fn root_changes_when_any_leaf_changes(
            hashes in prop::collection::vec(any::<[u8; 32]>(), 1..32),
            index in any::<prop::sample::Index>(),
            replacement in any::<[u8; 32]>(),
        ) {
            let position = index.index(hashes.len());
            prop_assume!(!hashes.contains(&replacement));
            let original = sorted_branches_merkle_root(&hashes)?;
            let mut changed = hashes.clone();
            changed[position] = replacement;
            prop_assert_ne!(original, sorted_branches_merkle_root(&changed)?);
        }
    }
}
