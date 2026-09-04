//! Shared reducer logic for the Bonsol branch reducer.
//!
//! Two kinds of code live here:
//!
//! 1. The journal contract: `decode_score_felt`, `reducer_canonical_bytes`,
//!    and `committed_outputs`. The guest (`main.rs`) and the callback harness
//!    both use these so the bytes the guest commits and the bytes the harness
//!    predicts come from one implementation. See `docs/proof-layer-status.md`.
//!
//! 2. The combiners (`weighted_mean`, `trimmed_mean`, `majority_vote`,
//!    `sorted_branches_merkle_root`). The guest does not call these today.
//!    The guest commits the statistics the caller supplies (a hash echo); it
//!    does not recompute the reduction. The worker-trust PR will mirror the
//!    semantics pinned by the tests below. See `docs/proof-layer-status.md`.

use sha2::{Digest, Sha256};

pub const COMBINER_WEIGHTED_MEAN: u8 = 1;
pub const COMBINER_TRIMMED_MEAN: u8 = 2;
pub const COMBINER_MAJORITY_VOTE: u8 = 3;

/// Domain separator for a Merkle leaf hash: `SHA256(0x00 || leaf)`.
pub const MERKLE_LEAF_PREFIX: u8 = 0x00;
/// Domain separator for a Merkle inner node: `SHA256(0x01 || left || right)`.
pub const MERKLE_NODE_PREFIX: u8 = 0x01;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CombinerError {
    EmptyBranches,
    ZeroWeight,
    TrimCountTooLarge,
    UnknownCombiner,
}

impl core::fmt::Display for CombinerError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for CombinerError {}

/// Length of the committed score: one BN254 scalar field element.
pub const SCORE_FELT_LEN: usize = 32;
/// `reducer_digest (32) || line_count le32 || word_count le32 || score (32)`.
pub const COMMITTED_OUTPUTS_LEN: usize = 32 + 4 + 4 + SCORE_FELT_LEN;
/// BN254 scalar field modulus `r`, big-endian bytes.
pub const BN254_SCALAR_MODULUS_BE: [u8; 32] = [
    0x30, 0x64, 0x4e, 0x72, 0xe1, 0x31, 0xa0, 0x29, 0xb8, 0x50, 0x45, 0xb6, 0x81, 0x81, 0x58, 0x5d,
    0x28, 0x33, 0xe8, 0x48, 0x79, 0xb9, 0x70, 0x91, 0x43, 0xe1, 0xf5, 0x93, 0xf0, 0x00, 0x00, 0x01,
];

/// Why a `score_hex` string could not be decoded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScoreHexError {
    /// A byte that is not a lowercase ASCII hex digit. Uppercase, a `0x`
    /// prefix, and multi-byte UTF-8 all land here.
    InvalidHexDigit,
    /// The digit count is not 64.
    WrongLength { digits: usize },
    /// The value is not less than the BN254 scalar field modulus.
    NotReduced,
}

impl core::fmt::Display for ScoreHexError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for ScoreHexError {}

/// Decode `score_hex` into the 32 bytes the guest commits.
///
/// `score_hex` is exactly 64 lowercase hex digits, no prefix, the little-endian
/// bytes of a BN254 scalar field element, reduced modulo the field. The returned
/// bytes are in string order, so `bytes[0]` is the least significant byte of the
/// score. The encoding is fixed by this function and the callers that mirror it;
/// no proving system in the tree produces or consumes it.
///
/// Rules, checked in this order:
/// - every byte must be a lowercase ASCII hex digit (`InvalidHexDigit`)
/// - the digit count must be 64 (`WrongLength`)
/// - the value must be less than the field modulus (`NotReduced`)
///
/// The function works on bytes, never on `str` slices, so a multi-byte
/// character cannot cause a char-boundary panic. Malformed input is an
/// error, never a panic.
pub fn decode_score_felt(score_hex: &str) -> Result<[u8; SCORE_FELT_LEN], ScoreHexError> {
    let digits = score_hex.as_bytes();
    let nibbles: Result<Vec<u8>, ScoreHexError> = digits.iter().map(|byte| hex_nibble(*byte)).collect();
    let nibbles = nibbles?;
    if nibbles.len() != SCORE_FELT_LEN * 2 {
        return Err(ScoreHexError::WrongLength { digits: nibbles.len() });
    }
    let mut score = [0u8; SCORE_FELT_LEN];
    for (index, pair) in nibbles.chunks(2).enumerate() {
        score[index] = (pair[0] << 4) | pair[1];
    }
    if !is_reduced_le(&score) {
        return Err(ScoreHexError::NotReduced);
    }
    Ok(score)
}

fn hex_nibble(byte: u8) -> Result<u8, ScoreHexError> {
    match byte {
        b'0'..=b'9' => Ok(byte - b'0'),
        b'a'..=b'f' => Ok(byte - b'a' + 10),
        _ => Err(ScoreHexError::InvalidHexDigit),
    }
}

/// True when the little-endian value is strictly less than the field modulus.
fn is_reduced_le(value_le: &[u8; SCORE_FELT_LEN]) -> bool {
    for index in (0..SCORE_FELT_LEN).rev() {
        let value_byte = value_le[index];
        let modulus_byte = BN254_SCALAR_MODULUS_BE[SCORE_FELT_LEN - 1 - index];
        if value_byte != modulus_byte {
            return value_byte < modulus_byte;
        }
    }
    false
}

/// The bytes whose SHA-256 is the `reducer_digest`.
///
/// `score_hex` is hashed as the raw string. Counts are decimal text.
pub fn reducer_canonical_bytes(
    branch_key: &str,
    child_job_id: &str,
    parent_request_id: &str,
    score_hex: &str,
    line_count: u32,
    word_count: u32,
) -> Vec<u8> {
    format!("{branch_key}|{child_job_id}|{parent_request_id}|{score_hex}|{line_count}|{word_count}").into_bytes()
}

/// The committed outputs Bonsol forwards to the callback: the guest journal
/// without its leading 32-byte input digest.
pub fn committed_outputs(
    reducer_digest: &[u8; 32],
    line_count: u32,
    word_count: u32,
    score: &[u8; SCORE_FELT_LEN],
) -> [u8; COMMITTED_OUTPUTS_LEN] {
    let mut outputs = [0u8; COMMITTED_OUTPUTS_LEN];
    outputs[..32].copy_from_slice(reducer_digest);
    outputs[32..36].copy_from_slice(&line_count.to_le_bytes());
    outputs[36..40].copy_from_slice(&word_count.to_le_bytes());
    outputs[40..].copy_from_slice(score);
    outputs
}

#[derive(Debug, Clone, Copy)]
pub struct WeightedValue {
    pub value: i64,
    pub weight: u64,
}

#[derive(Debug, Clone, Copy)]
pub struct CategoricalVote {
    pub category: u32,
    pub weight: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TrimmedMeanResult {
    pub mean: f64,
    pub rejected_count: usize,
}

pub fn validate_combiner_id(combiner_id: u8) -> Result<(), CombinerError> {
    match combiner_id {
        COMBINER_WEIGHTED_MEAN | COMBINER_TRIMMED_MEAN | COMBINER_MAJORITY_VOTE => Ok(()),
        _ => Err(CombinerError::UnknownCombiner),
    }
}

pub fn weighted_mean(branches: &[WeightedValue]) -> Result<f64, CombinerError> {
    if branches.is_empty() {
        return Err(CombinerError::EmptyBranches);
    }
    let total_weight: u128 = branches.iter().map(|branch| branch.weight as u128).sum();
    if total_weight == 0 {
        return Err(CombinerError::ZeroWeight);
    }
    let weighted_sum: i128 = branches
        .iter()
        .map(|branch| branch.value as i128 * branch.weight as i128)
        .sum();
    Ok(weighted_sum as f64 / total_weight as f64)
}

pub fn trimmed_mean(
    values: &[i64],
    outlier_count: usize,
) -> Result<TrimmedMeanResult, CombinerError> {
    if values.is_empty() {
        return Err(CombinerError::EmptyBranches);
    }
    if outlier_count >= values.len() {
        return Err(CombinerError::TrimCountTooLarge);
    }
    let mut indexed: Vec<(usize, i64)> = values.iter().copied().enumerate().collect();
    indexed.sort_by_key(|(_, value)| *value);
    let median = indexed[indexed.len() / 2].1;
    indexed.sort_by(|(left_idx, left), (right_idx, right)| {
        let left_distance = (*left as i128 - median as i128).abs();
        let right_distance = (*right as i128 - median as i128).abs();
        right_distance
            .cmp(&left_distance)
            .then_with(|| left.cmp(right))
            .then_with(|| left_idx.cmp(right_idx))
    });
    let mut keep = vec![true; values.len()];
    for (idx, _) in indexed.iter().take(outlier_count) {
        keep[*idx] = false;
    }
    let retained: Vec<i64> = values
        .iter()
        .enumerate()
        .filter_map(|(idx, value)| keep[idx].then_some(*value))
        .collect();
    let sum: i128 = retained.iter().map(|value| *value as i128).sum();
    Ok(TrimmedMeanResult {
        mean: sum as f64 / retained.len() as f64,
        rejected_count: outlier_count,
    })
}

pub fn majority_vote(votes: &[CategoricalVote]) -> Result<u32, CombinerError> {
    if votes.is_empty() {
        return Err(CombinerError::EmptyBranches);
    }
    let mut totals: Vec<(u32, u128)> = Vec::new();
    for vote in votes {
        if vote.weight == 0 {
            continue;
        }
        if let Some((_, total)) = totals
            .iter_mut()
            .find(|(category, _)| *category == vote.category)
        {
            *total += vote.weight as u128;
        } else {
            totals.push((vote.category, vote.weight as u128));
        }
    }
    if totals.is_empty() {
        return Err(CombinerError::ZeroWeight);
    }
    totals.sort_by(
        |(left_category, left_weight), (right_category, right_weight)| {
            right_weight
                .cmp(left_weight)
                .then_with(|| left_category.cmp(right_category))
        },
    );
    Ok(totals[0].0)
}

/// Merkle root over the sorted branch hashes.
///
/// Leaves and inner nodes use different hash prefixes (RFC 6962 style), so a
/// leaf can never be confused with a node. An odd node at the end of a level
/// is promoted unchanged; it is never paired with a copy of itself. Together
/// these rules make the root a unique function of the sorted leaf list. The
/// old scheme paired the odd node with itself, so `[A, B, B]` and
/// `[A, B, B, B]` produced the same root (CVE-2012-2459 class).
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
    use proptest::prelude::*;

    fn weighted_values_strategy() -> impl Strategy<Value = Vec<WeightedValue>> {
        prop::collection::vec((-1_000_000i64..1_000_000, 1u64..10_000), 1..40).prop_map(|items| {
            items
                .into_iter()
                .map(|(value, weight)| WeightedValue { value, weight })
                .collect()
        })
    }

    fn hex_to_array(hex: &str) -> [u8; 32] {
        let mut out = [0u8; 32];
        for (index, chunk) in hex.as_bytes().chunks(2).enumerate() {
            out[index] = u8::from_str_radix(std::str::from_utf8(chunk).unwrap(), 16).unwrap();
        }
        out
    }

    /// The scheme this crate shipped before the fix: no domain separation and
    /// the odd node is paired with a copy of itself. Kept only to prove the
    /// regression vector really collided.
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

    const A: [u8; 32] = [0x11; 32];
    const B: [u8; 32] = [0x22; 32];
    const C: [u8; 32] = [0x33; 32];

    #[test]
    fn merkle_root_matches_golden_vectors() {
        // Computed independently with Python hashlib:
        // L(x) = sha256(0x00 || x); N(l, r) = sha256(0x01 || l || r).
        assert_eq!(
            sorted_branches_merkle_root(&[A]).unwrap(),
            hex_to_array("4635e1fa62a599a7880a8d14a56f720a1d40f6e5448ab5a5e39bedc8bd87fa8e")
        );
        assert_eq!(
            sorted_branches_merkle_root(&[B, A]).unwrap(),
            hex_to_array("cc15b132263fd4fd2748c0e7cb9e1c4ad0afe70fcf9382ee644c4da8af0286a5")
        );
        assert_eq!(
            sorted_branches_merkle_root(&[A, B, C]).unwrap(),
            hex_to_array("9bee4401962e94b921336a7910a5a9718836ffcbc545dde0a3f34d858beb5752")
        );
        assert_eq!(
            sorted_branches_merkle_root(&[A, B, C, C]).unwrap(),
            hex_to_array("4118b0b8b03727613a79962aa22cb29474c01378848625423390a5b36e6735a0")
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
    fn merkle_root_single_leaf_is_leaf_hash_not_raw_leaf() {
        assert_eq!(sorted_branches_merkle_root(&[A]).unwrap(), merkle_leaf_hash(&A));
        assert_ne!(sorted_branches_merkle_root(&[A]).unwrap(), A);
    }

    #[test]
    fn merkle_root_inner_node_cannot_pose_as_leaf() {
        let inner = merkle_node_hash(&merkle_leaf_hash(&A), &merkle_leaf_hash(&B));
        assert_ne!(
            sorted_branches_merkle_root(&[A, B, C]).unwrap(),
            sorted_branches_merkle_root(&[inner, C]).unwrap()
        );
    }

    #[test]
    fn merkle_root_rejects_empty_input() {
        assert_eq!(sorted_branches_merkle_root(&[]), Err(CombinerError::EmptyBranches));
    }

    const SCORE_FELT: &str = "003a000000000000000000000000000000000000000000000000000000000000";
    const LOW_BYTE_FELT: &str = "3901000000000000000000000000000000000000000000000000000000000000";
    const MINUS_ONE_FELT: &str = "000000f093f5e1439170b97948e833285d588181b64550b829a031e1724e6430";
    const MODULUS_FELT: &str = "010000f093f5e1439170b97948e833285d588181b64550b829a031e1724e6430";

    #[test]
    fn decode_score_felt_returns_little_endian_bytes_in_string_order() {
        let score = decode_score_felt(SCORE_FELT).unwrap();
        assert_eq!(score[0], 0x00);
        assert_eq!(score[1], 0x3a);
        assert_eq!(&score[2..], &[0u8; 30]);
        assert_eq!(decode_score_felt(&"00".repeat(32)).unwrap(), [0u8; 32]);
    }

    #[test]
    fn decode_score_felt_regression_low_byte_is_the_first_byte_not_the_last() {
        // 313 = 0x0139 at scale 8 is 1.22; little-endian hex starts "3901".
        // The legacy decoder read the last two hex digits ("00") and committed 0.
        let score = decode_score_felt(LOW_BYTE_FELT).unwrap();
        assert_eq!(score[0], 0x39);
        assert_eq!(score[1], 0x01);
        assert_eq!(u16::from_le_bytes([score[0], score[1]]), 313);
        let legacy_last_two_digits = u8::from_str_radix(&LOW_BYTE_FELT[62..], 16).unwrap();
        assert_eq!(legacy_last_two_digits, 0);
    }

    #[test]
    fn decode_score_felt_accepts_reduced_and_rejects_unreduced_values() {
        assert!(decode_score_felt(MINUS_ONE_FELT).is_ok());
        assert_eq!(decode_score_felt(MODULUS_FELT), Err(ScoreHexError::NotReduced));
        assert_eq!(decode_score_felt(&"ff".repeat(32)), Err(ScoreHexError::NotReduced));
    }

    #[test]
    fn decode_score_felt_rejects_malformed_input_without_panicking() {
        assert_eq!(decode_score_felt("\u{e9}a"), Err(ScoreHexError::InvalidHexDigit));
        assert_eq!(decode_score_felt("a"), Err(ScoreHexError::WrongLength { digits: 1 }));
        assert_eq!(decode_score_felt(""), Err(ScoreHexError::WrongLength { digits: 0 }));
        assert_eq!(decode_score_felt("zz"), Err(ScoreHexError::InvalidHexDigit));
        assert_eq!(decode_score_felt("0xff"), Err(ScoreHexError::InvalidHexDigit));
        assert_eq!(decode_score_felt("deadbeef"), Err(ScoreHexError::WrongLength { digits: 8 }));
        assert_eq!(decode_score_felt(&SCORE_FELT.to_uppercase()), Err(ScoreHexError::InvalidHexDigit));
        assert_eq!(decode_score_felt(&format!("0x{SCORE_FELT}")), Err(ScoreHexError::InvalidHexDigit));
        assert_eq!(decode_score_felt(&SCORE_FELT[..62]), Err(ScoreHexError::WrongLength { digits: 62 }));
        assert_eq!(decode_score_felt("\u{e9}"), Err(ScoreHexError::InvalidHexDigit));
    }

    #[test]
    fn committed_outputs_match_golden_vector() {
        // Golden values computed independently with Python hashlib for
        // branch_key=baseline child_job_id=child-baseline-1
        // parent_request_id=parent-bonsol-eval line_count=3 word_count=17
        // score_hex=SCORE_FELT.
        let canonical = reducer_canonical_bytes("baseline", "child-baseline-1", "parent-bonsol-eval", SCORE_FELT, 3, 17);
        assert_eq!(
            canonical,
            format!("baseline|child-baseline-1|parent-bonsol-eval|{SCORE_FELT}|3|17").into_bytes()
        );
        let reducer_digest: [u8; 32] = Sha256::digest(&canonical).into();
        assert_eq!(
            reducer_digest,
            hex_to_array("015c09c8aeadb048416fe04d61b50cc187b34eb66e772ea4fff92cdbcf1c2aeb")
        );
        let outputs = committed_outputs(&reducer_digest, 3, 17, &decode_score_felt(SCORE_FELT).unwrap());
        assert_eq!(outputs.len(), COMMITTED_OUTPUTS_LEN);
        assert_eq!(&outputs[..32], &reducer_digest);
        assert_eq!(&outputs[32..36], &3u32.to_le_bytes());
        assert_eq!(&outputs[36..40], &17u32.to_le_bytes());
        assert_eq!(outputs[40], 0x00);
        assert_eq!(outputs[41], 0x3a);
        let output_digest: [u8; 32] = Sha256::digest(outputs).into();
        assert_eq!(
            output_digest,
            hex_to_array("76a8ed05cc918de950431cc891b1d316d6d7233b6f9fc951d7e36966e322c1ea")
        );
    }

    #[test]
    fn committed_outputs_carry_the_true_low_byte() {
        let reducer_digest = [0u8; 32];
        let outputs = committed_outputs(&reducer_digest, 3, 17, &decode_score_felt(LOW_BYTE_FELT).unwrap());
        assert_eq!(outputs[40], 0x39);
        assert_eq!(outputs[41], 0x01);
        assert_eq!(&outputs[42..], &[0u8; 30]);
    }

    proptest! {
        #[test]
        fn weighted_mean_is_deterministic_for_identical_input(branches in weighted_values_strategy()) {
            let first = weighted_mean(&branches)?;
            let second = weighted_mean(&branches)?;
            prop_assert_eq!(first, second);
        }

        #[test]
        fn weighted_mean_over_normalized_weights_is_order_independent(branches in weighted_values_strategy()) {
            let mut reversed = branches.clone();
            reversed.reverse();
            let first = weighted_mean(&branches)?;
            let second = weighted_mean(&reversed)?;
            prop_assert!((first - second).abs() <= 1e-9);
        }

        #[test]
        fn trimmed_mean_rejects_exactly_configured_outlier_count(values in prop::collection::vec(-1_000_000i64..1_000_000, 2..40), outlier_count in 0usize..20) {
            prop_assume!(outlier_count < values.len());
            let result = trimmed_mean(&values, outlier_count)?;
            prop_assert_eq!(result.rejected_count, outlier_count);
        }

        #[test]
        fn majority_vote_applies_lowest_category_tie_break(category_a in 0u32..1_000, category_b in 0u32..1_000, weight in 1u64..10_000) {
            prop_assume!(category_a != category_b);
            let votes = vec![
                CategoricalVote { category: category_a, weight },
                CategoricalVote { category: category_b, weight },
            ];
            let expected = category_a.min(category_b);
            prop_assert_eq!(majority_vote(&votes)?, expected);
        }

        #[test]
        fn branches_merkle_root_is_invariant_under_sorted_reordering(mut branch_hashes in prop::collection::vec(any::<[u8; 32]>(), 1..32)) {
            let original = sorted_branches_merkle_root(&branch_hashes)?;
            branch_hashes.sort();
            branch_hashes.reverse();
            let reordered = sorted_branches_merkle_root(&branch_hashes)?;
            prop_assert_eq!(original, reordered);
        }

        #[test]
        fn branches_merkle_root_changes_when_last_leaf_is_duplicated(branch_hashes in prop::collection::vec(any::<[u8; 32]>(), 1..32)) {
            let original = sorted_branches_merkle_root(&branch_hashes)?;
            let mut extended = branch_hashes.clone();
            extended.push(*branch_hashes.iter().max().unwrap());
            let duplicated = sorted_branches_merkle_root(&extended)?;
            prop_assert_ne!(original, duplicated);
        }

        #[test]
        fn branches_merkle_root_changes_when_any_leaf_changes(branch_hashes in prop::collection::vec(any::<[u8; 32]>(), 1..32), index in any::<prop::sample::Index>(), replacement in any::<[u8; 32]>()) {
            let position = index.index(branch_hashes.len());
            prop_assume!(!branch_hashes.contains(&replacement));
            let original = sorted_branches_merkle_root(&branch_hashes)?;
            let mut changed = branch_hashes.clone();
            changed[position] = replacement;
            prop_assert_ne!(original, sorted_branches_merkle_root(&changed)?);
        }

        #[test]
        fn decode_score_felt_never_panics(input in any::<String>()) {
            let _ = decode_score_felt(&input);
        }

        #[test]
        fn decode_score_felt_roundtrips_small_values(value in any::<u128>()) {
            let mut bytes = [0u8; 32];
            bytes[..16].copy_from_slice(&value.to_le_bytes());
            let text: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
            prop_assert_eq!(decode_score_felt(&text), Ok(bytes));
        }

        #[test]
        fn decode_score_felt_rejects_every_wrong_length(digits in 0usize..200) {
            prop_assume!(digits != 64);
            let text = "0".repeat(digits);
            prop_assert_eq!(decode_score_felt(&text), Err(ScoreHexError::WrongLength { digits }));
        }

        #[test]
        fn combiner_id_outside_active_range_rejects_with_unknown_combiner(combiner_id in any::<u8>()) {
            prop_assume!(!(1..=3).contains(&combiner_id));
            prop_assert_eq!(
                validate_combiner_id(combiner_id),
                Err(CombinerError::UnknownCombiner)
            );
        }
    }
}
