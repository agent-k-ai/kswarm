//! The three combiners of the Phase 1 registry.
//!
//! Two arithmetics live here on purpose.
//!
//! * The `*_bps` functions are **exact integer** arithmetic and produce the value a
//!   guest commits. Their result is a function of the inputs alone, with no rounding
//!   mode to agree on and no floating-point unit to trust, so a Rust guest and the
//!   Python aggregator cannot drift.
//! * [`weighted_mean`] and [`trimmed_mean`] keep the historical `f64` definition
//!   because the aggregate output artifact still reports a mean. They are never the
//!   source of a committed value. `tests` pins the two against each other.
//!
//! Rounding for the `*_bps` functions is round-half-up on a non-negative quotient:
//! `(2*numerator + denominator) / (2*denominator)`, in `i128`. Basis-point values are
//! in `[0, 10000]` and weights are positive, so the quotient is never negative.

use alloc::vec::Vec;
use sha2::{Digest, Sha256};

pub const COMBINER_WEIGHTED_MEAN: u8 = 1;
pub const COMBINER_TRIMMED_MEAN: u8 = 2;
pub const COMBINER_MAJORITY_VOTE: u8 = 3;

/// Basis-point scale. A probability of 1 is 10000 bps.
pub const BPS_SCALE: i64 = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CombinerError {
    EmptyBranches,
    ZeroWeight,
    TrimCountTooLarge,
    UnknownCombiner,
    /// A value outside `[0, 10000]` reached a basis-point combiner.
    ValueOutOfRange,
    /// The exact quotient did not land in `[0, 10000]`.
    ResultOutOfRange,
    /// The weighted sum or the weight total did not fit `i128`/`u128`.
    Overflow,
}

impl core::fmt::Display for CombinerError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

#[cfg(feature = "std")]
impl std::error::Error for CombinerError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WeightedValue {
    pub value: i64,
    pub weight: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CategoricalVote {
    pub category: u32,
    pub weight: u64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TrimmedMeanResult {
    pub mean: f64,
    pub rejected_count: usize,
}

/// The combiner parameters that reach the journal.
///
/// Only the parameters that change the result are here. `weights` are per branch and
/// travel with the branches, not with the parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CombinerParams {
    /// `trimmed-mean` only: the fraction of branches to drop, in basis points.
    pub trim_bps: u32,
    /// `majority-vote` only: the size of the committed category dictionary.
    pub category_dictionary_size: u32,
}

/// Domain-separated canonical bytes of the combiner parameters.
///
/// A single line naming every parameter of the registry, so a parameter added later
/// is a version bump rather than a silent change of meaning. Absent parameters are
/// written as `0`, which is what the combiners that ignore them see.
pub const COMBINER_PARAMS_DOMAIN: &str = "kswarm-combiner-params-v1";

pub fn combiner_params_canonical_bytes(combiner_id: u8, params: &CombinerParams) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(COMBINER_PARAMS_DOMAIN.as_bytes());
    out.extend_from_slice(b"|combiner_id=");
    push_u64(&mut out, u64::from(combiner_id));
    out.extend_from_slice(b"|trim_bps=");
    push_u64(&mut out, u64::from(params.trim_bps));
    out.extend_from_slice(b"|category_dictionary_size=");
    push_u64(&mut out, u64::from(params.category_dictionary_size));
    out
}

pub fn combiner_params_digest(combiner_id: u8, params: &CombinerParams) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(combiner_params_canonical_bytes(combiner_id, params));
    hasher.finalize().into()
}

fn push_u64(out: &mut Vec<u8>, mut value: u64) {
    if value == 0 {
        out.push(b'0');
        return;
    }
    let mut digits = [0u8; 20];
    let mut length = 0;
    while value > 0 {
        digits[length] = b'0' + (value % 10) as u8;
        value /= 10;
        length += 1;
    }
    for index in (0..length).rev() {
        out.push(digits[index]);
    }
}

pub fn validate_combiner_id(combiner_id: u8) -> Result<(), CombinerError> {
    match combiner_id {
        COMBINER_WEIGHTED_MEAN | COMBINER_TRIMMED_MEAN | COMBINER_MAJORITY_VOTE => Ok(()),
        _ => Err(CombinerError::UnknownCombiner),
    }
}

/// `floor(branch_count * trim_bps / 10000)`, the manifest `trim_bps` as a branch count.
pub fn trim_count_from_bps(branch_count: usize, trim_bps: u32) -> Result<usize, CombinerError> {
    if trim_bps >= BPS_SCALE as u32 {
        return Err(CombinerError::ValueOutOfRange);
    }
    Ok((branch_count as u128 * u128::from(trim_bps) / BPS_SCALE as u128) as usize)
}

/// Weighted mean in exact integer arithmetic, rounded half up, in basis points.
pub fn weighted_mean_bps(branches: &[WeightedValue]) -> Result<u32, CombinerError> {
    if branches.is_empty() {
        return Err(CombinerError::EmptyBranches);
    }
    let mut total_weight: u128 = 0;
    let mut weighted_sum: i128 = 0;
    for branch in branches {
        check_bps(branch.value)?;
        total_weight = total_weight
            .checked_add(u128::from(branch.weight))
            .ok_or(CombinerError::Overflow)?;
        weighted_sum = weighted_sum
            .checked_add(i128::from(branch.value) * i128::from(branch.weight))
            .ok_or(CombinerError::Overflow)?;
    }
    if total_weight == 0 {
        return Err(CombinerError::ZeroWeight);
    }
    round_half_up(weighted_sum, total_weight as i128)
}

/// Trimmed mean in exact integer arithmetic, rounded half up, in basis points.
///
/// The retained set is chosen by [`trimmed_mean`], so the two arithmetics always
/// average the same values.
pub fn trimmed_mean_bps(values: &[i64], outlier_count: usize) -> Result<u32, CombinerError> {
    let retained = retained_values(values, outlier_count)?;
    let mut sum: i128 = 0;
    for value in &retained {
        check_bps(*value)?;
        sum += i128::from(*value);
    }
    round_half_up(sum, retained.len() as i128)
}

fn check_bps(value: i64) -> Result<(), CombinerError> {
    if !(0..=BPS_SCALE).contains(&value) {
        return Err(CombinerError::ValueOutOfRange);
    }
    Ok(())
}

fn round_half_up(numerator: i128, denominator: i128) -> Result<u32, CombinerError> {
    if denominator <= 0 {
        return Err(CombinerError::ZeroWeight);
    }
    if numerator < 0 {
        return Err(CombinerError::ValueOutOfRange);
    }
    let rounded = (numerator * 2 + denominator) / (denominator * 2);
    if rounded < 0 || rounded > i128::from(BPS_SCALE) {
        return Err(CombinerError::ResultOutOfRange);
    }
    Ok(rounded as u32)
}

pub fn weighted_mean(branches: &[WeightedValue]) -> Result<f64, CombinerError> {
    if branches.is_empty() {
        return Err(CombinerError::EmptyBranches);
    }
    let total_weight: u128 = branches.iter().map(|branch| u128::from(branch.weight)).sum();
    if total_weight == 0 {
        return Err(CombinerError::ZeroWeight);
    }
    let weighted_sum: i128 = branches
        .iter()
        .map(|branch| i128::from(branch.value) * i128::from(branch.weight))
        .sum();
    Ok(weighted_sum as f64 / total_weight as f64)
}

/// Drop the `outlier_count` values farthest from the lower median, then average the rest.
///
/// Median: stable sort by value, take index `len/2`. Rejection order: distance
/// descending, then value ascending, then original index ascending. Retained values
/// keep their original order.
pub fn trimmed_mean(values: &[i64], outlier_count: usize) -> Result<TrimmedMeanResult, CombinerError> {
    let retained = retained_values(values, outlier_count)?;
    let sum: i128 = retained.iter().map(|value| i128::from(*value)).sum();
    Ok(TrimmedMeanResult {
        mean: sum as f64 / retained.len() as f64,
        rejected_count: outlier_count,
    })
}

fn retained_values(values: &[i64], outlier_count: usize) -> Result<Vec<i64>, CombinerError> {
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
        let left_distance = (i128::from(*left) - i128::from(median)).abs();
        let right_distance = (i128::from(*right) - i128::from(median)).abs();
        right_distance
            .cmp(&left_distance)
            .then_with(|| left.cmp(right))
            .then_with(|| left_idx.cmp(right_idx))
    });
    let mut keep = alloc::vec![true; values.len()];
    for (idx, _) in indexed.iter().take(outlier_count) {
        keep[*idx] = false;
    }
    Ok(values
        .iter()
        .enumerate()
        .filter_map(|(idx, value)| keep[idx].then_some(*value))
        .collect())
}

/// Highest accumulated weight wins; the lowest category breaks a tie. Zero-weight
/// votes are skipped, and a ballot of nothing but zero weights is an error.
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
            *total += u128::from(vote.weight);
        } else {
            totals.push((vote.category, u128::from(vote.weight)));
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

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec;
    use proptest::prelude::*;

    fn uniform(values: &[i64]) -> Vec<WeightedValue> {
        values
            .iter()
            .map(|value| WeightedValue {
                value: *value,
                weight: 1,
            })
            .collect()
    }

    #[test]
    fn weighted_mean_bps_rounds_half_up() {
        assert_eq!(weighted_mean_bps(&uniform(&[1, 2])).unwrap(), 2);
        assert_eq!(weighted_mean_bps(&uniform(&[1, 1, 2])).unwrap(), 1);
        assert_eq!(weighted_mean_bps(&uniform(&[0, 1])).unwrap(), 1);
        assert_eq!(weighted_mean_bps(&uniform(&[5000, 5001])).unwrap(), 5001);
        assert_eq!(weighted_mean_bps(&uniform(&[10000, 10000])).unwrap(), 10000);
    }

    #[test]
    fn weighted_mean_bps_honours_weights() {
        let branches = vec![
            WeightedValue { value: 0, weight: 3 },
            WeightedValue {
                value: 10000,
                weight: 1,
            },
        ];
        assert_eq!(weighted_mean_bps(&branches).unwrap(), 2500);
    }

    #[test]
    fn weighted_mean_bps_rejects_a_value_outside_the_basis_point_range() {
        assert_eq!(
            weighted_mean_bps(&uniform(&[10001])),
            Err(CombinerError::ValueOutOfRange)
        );
        assert_eq!(
            weighted_mean_bps(&uniform(&[-1])),
            Err(CombinerError::ValueOutOfRange)
        );
        assert_eq!(weighted_mean_bps(&[]), Err(CombinerError::EmptyBranches));
        assert_eq!(
            weighted_mean_bps(&[WeightedValue { value: 1, weight: 0 }]),
            Err(CombinerError::ZeroWeight)
        );
    }

    #[test]
    fn trimmed_mean_bps_averages_the_retained_values() {
        // 10 is the far outlier from the lower median 2; dropping it leaves 1,2,3.
        assert_eq!(trimmed_mean_bps(&[1, 2, 3, 10], 1).unwrap(), 2);
        assert_eq!(trimmed_mean_bps(&[1, 2, 3, 10], 0).unwrap(), 4);
        assert_eq!(
            trimmed_mean_bps(&[1, 2], 2),
            Err(CombinerError::TrimCountTooLarge)
        );
    }

    #[test]
    fn trim_count_is_a_floor_of_the_basis_point_fraction() {
        assert_eq!(trim_count_from_bps(10, 1_000).unwrap(), 1);
        assert_eq!(trim_count_from_bps(9, 1_000).unwrap(), 0);
        assert_eq!(trim_count_from_bps(2, 5_000).unwrap(), 1);
        assert_eq!(
            trim_count_from_bps(2, 10_000),
            Err(CombinerError::ValueOutOfRange)
        );
    }

    #[test]
    fn majority_vote_breaks_ties_on_the_lowest_category() {
        let votes = vec![
            CategoricalVote {
                category: 3,
                weight: 1,
            },
            CategoricalVote {
                category: 1,
                weight: 1,
            },
        ];
        assert_eq!(majority_vote(&votes).unwrap(), 1);
        assert_eq!(majority_vote(&[]), Err(CombinerError::EmptyBranches));
        assert_eq!(
            majority_vote(&[CategoricalVote {
                category: 1,
                weight: 0
            }]),
            Err(CombinerError::ZeroWeight)
        );
    }

    #[test]
    fn combiner_params_canonical_bytes_name_every_registry_parameter() {
        let params = CombinerParams {
            trim_bps: 1_000,
            category_dictionary_size: 0,
        };
        assert_eq!(
            combiner_params_canonical_bytes(COMBINER_TRIMMED_MEAN, &params),
            b"kswarm-combiner-params-v1|combiner_id=2|trim_bps=1000|category_dictionary_size=0"
                .to_vec()
        );
    }

    #[test]
    fn combiner_id_registry_is_closed() {
        assert!(validate_combiner_id(COMBINER_WEIGHTED_MEAN).is_ok());
        assert!(validate_combiner_id(COMBINER_TRIMMED_MEAN).is_ok());
        assert!(validate_combiner_id(COMBINER_MAJORITY_VOTE).is_ok());
        assert_eq!(validate_combiner_id(0), Err(CombinerError::UnknownCombiner));
        assert_eq!(validate_combiner_id(4), Err(CombinerError::UnknownCombiner));
    }

    proptest! {
        /// The exact arithmetic and the historical `f64` arithmetic must agree for
        /// every basis-point input. If they ever disagree, the aggregate output
        /// artifact would report one number and the journal commit another.
        #[test]
        fn exact_and_float_weighted_mean_agree(
            values in prop::collection::vec(0i64..=10_000, 1..40),
            weights in prop::collection::vec(1u64..1_000, 1..40),
        ) {
            let branches: Vec<WeightedValue> = values
                .iter()
                .zip(weights.iter().cycle())
                .map(|(value, weight)| WeightedValue { value: *value, weight: *weight })
                .collect();
            let exact = weighted_mean_bps(&branches)?;
            let float = weighted_mean(&branches)?;
            let rounded = float_round_half_up(float);
            prop_assert_eq!(i64::from(exact), rounded);
        }

        #[test]
        fn exact_and_float_trimmed_mean_agree(
            values in prop::collection::vec(0i64..=10_000, 2..40),
            outlier_count in 0usize..20,
        ) {
            prop_assume!(outlier_count < values.len());
            let exact = trimmed_mean_bps(&values, outlier_count)?;
            let float = trimmed_mean(&values, outlier_count)?;
            prop_assert_eq!(i64::from(exact), float_round_half_up(float.mean));
            prop_assert_eq!(float.rejected_count, outlier_count);
        }

        #[test]
        fn weighted_mean_bps_is_order_independent(
            values in prop::collection::vec(0i64..=10_000, 1..40),
        ) {
            let branches = uniform(&values);
            let mut reversed = branches.clone();
            reversed.reverse();
            prop_assert_eq!(weighted_mean_bps(&branches)?, weighted_mean_bps(&reversed)?);
        }

        #[test]
        fn trimmed_mean_bps_rejects_exactly_the_configured_count(
            values in prop::collection::vec(0i64..=10_000, 2..40),
            outlier_count in 0usize..20,
        ) {
            prop_assume!(outlier_count < values.len());
            let retained = retained_values(&values, outlier_count)?;
            prop_assert_eq!(retained.len(), values.len() - outlier_count);
        }

        #[test]
        fn combiner_params_digest_separates_every_parameter(
            trim_bps in 0u32..10_000,
            dictionary in 0u32..1_000,
        ) {
            let params = CombinerParams { trim_bps, category_dictionary_size: dictionary };
            let other = CombinerParams { trim_bps: trim_bps + 1, category_dictionary_size: dictionary };
            prop_assert_ne!(
                combiner_params_digest(COMBINER_TRIMMED_MEAN, &params),
                combiner_params_digest(COMBINER_TRIMMED_MEAN, &other)
            );
            prop_assert_ne!(
                combiner_params_digest(COMBINER_TRIMMED_MEAN, &params),
                combiner_params_digest(COMBINER_WEIGHTED_MEAN, &params)
            );
        }
    }

    /// The rounding the Python runner applied before this crate existed:
    /// `Decimal(repr(mean)).quantize(1, ROUND_HALF_UP)`.
    fn float_round_half_up(value: f64) -> i64 {
        let floor = value.floor();
        if value - floor >= 0.5 {
            floor as i64 + 1
        } else {
            floor as i64
        }
    }
}
