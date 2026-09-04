//! The aggregate artifact and the journal the aggregate guest commits.
//!
//! # What the guest is asked to prove
//!
//! An aggregate-proof job pays for one claim: *these branch receipts, combined by
//! this combiner with these parameters, give this value*. The artifact therefore
//! carries the branch receipt **bytes**, not a caller-supplied summary of them. The
//! guest hashes those bytes itself, decodes the branch values out of them, runs the
//! combiner, and commits the result. `sha256(result_bytes)` is the
//! `submitted_result_hash` the Solana program already stores for each branch job, so
//! anyone can check that the branches the guest reduced are the branches that settled
//! on chain -- the Merkle root in the journal is over exactly those hashes.
//!
//! # Artifact (`MFA3`)
//!
//! Canonical JSON: sorted keys, no whitespace, UTF-8. `schema` is `"MFA3"` and
//! `schema_version` is `3`.
//!
//! ```json
//! {
//!   "schema": "MFA3",
//!   "schema_version": 3,
//!   "parent_run": "<aggregate job pubkey>",
//!   "parent_manifest_cid": "<cid>",
//!   "output_schema_hash": "<64 lowercase hex>",
//!   "combiner": "trimmed-mean",
//!   "combiner_id": 2,
//!   "combiner_parameters": {"trim_bps": 1000, "category_dictionary_size": 0},
//!   "branches": [
//!     {"branch_index": 0, "job": "<pubkey>", "output_cid": "<cid>",
//!      "result_bytes": "<lowercase hex of the MFB2 receipt>",
//!      "result_hash": "<64 lowercase hex>", "weight": 1}
//!   ]
//! }
//! ```
//!
//! The guest reads `combiner_id`, `combiner_parameters`, and `branches`. Everything
//! else is provenance for human readers and for the Python cross-check; it is covered
//! by `input_digest` because that digest is over the whole framed artifact, so it
//! cannot be changed after the job is opened either.
//!
//! # Journal (105 bytes)
//!
//! | Offset | Field | Encoding |
//! | --- | --- | --- |
//! | 0..32 | `input_digest` | SHA-256 of the framed public input (`len le64 \|\| artifact`) |
//! | 32..33 | `combiner_id` | u8 |
//! | 33..65 | `combiner_params_digest` | SHA-256 of the canonical parameter line |
//! | 65..69 | `result_value` | u32 little-endian: basis points, or the label index |
//! | 69..73 | `branch_count` | u32 little-endian |
//! | 73..105 | `merkle_root` | sorted branch-hash Merkle root |
//!
//! Bonsol forwards bytes 32..105 (73 bytes, the committed outputs) to the callback.
//! The program stores `output_digest = sha256(committed_outputs)` and
//! `journal_hash = sha256(input_digest || committed_outputs)`.
//!
//! Every failure here aborts the guest, so a malformed or dishonest artifact produces
//! no receipt at all rather than a receipt for a different claim.

use alloc::string::String;
use alloc::vec::Vec;
use serde_json::Value;

use crate::combiner::{
    combiner_params_digest, majority_vote, trim_count_from_bps, trimmed_mean_bps, validate_combiner_id,
    weighted_mean_bps, CategoricalVote, CombinerError, CombinerParams, WeightedValue,
    COMBINER_MAJORITY_VOTE, COMBINER_TRIMMED_MEAN, COMBINER_WEIGHTED_MEAN,
};
use crate::merkle::sorted_branches_merkle_root;
use crate::mfb2::{parse_branch_result_bytes, Mfb2Error};
use crate::{decode_hex, decode_hex_array, framed_input_digest};

pub const AGGREGATE_SCHEMA: &str = "MFA3";
pub const AGGREGATE_SCHEMA_VERSION: u64 = 3;
/// `combiner_id || params_digest || result_value || branch_count || merkle_root`.
pub const AGGREGATE_COMMITTED_OUTPUTS_LEN: usize = 1 + 32 + 4 + 4 + 32;
pub const AGGREGATE_JOURNAL_LEN: usize = 32 + AGGREGATE_COMMITTED_OUTPUTS_LEN;
/// `predict open` caps a run at 128 branches, and the artifact must fit a Bonsol
/// input. A larger array is rejected rather than reduced.
pub const MAX_BRANCHES: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AggregateError {
    NotJson,
    NotAnObject,
    MissingField(&'static str),
    WrongType(&'static str),
    UnknownSchema(String),
    UnknownSchemaVersion(u64),
    CombinerIdMismatch,
    BranchesEmpty,
    TooManyBranches(usize),
    BranchIndexNotIncreasing { position: usize },
    BranchIndexMismatch { declared: u32, encoded: u32 },
    ResultHashMismatch { branch_index: u32 },
    ZeroWeight { branch_index: u32 },
    NonUniformWeightForTrimmedMean { branch_index: u32 },
    BadHex(&'static str),
    BadReceipt { branch_index: u32, error: Mfb2Error },
    MissingScalar { branch_index: u32 },
    MissingLabel { branch_index: u32 },
    LabelOutsideDictionary { branch_index: u32, label: u8 },
    DictionarySizeMissing,
    Combiner(CombinerError),
}

impl From<CombinerError> for AggregateError {
    fn from(error: CombinerError) -> Self {
        AggregateError::Combiner(error)
    }
}

impl core::fmt::Display for AggregateError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

#[cfg(feature = "std")]
impl std::error::Error for AggregateError {}

/// Everything the guest derived from the artifact, before it is framed as a journal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregateReduction {
    pub combiner_id: u8,
    pub params: CombinerParams,
    pub params_digest: [u8; 32],
    pub result_value: u32,
    pub branch_count: u32,
    pub branch_hashes: Vec<[u8; 32]>,
    pub merkle_root: [u8; 32],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregateJournal {
    pub input_digest: [u8; 32],
    pub reduction: AggregateReduction,
}

impl AggregateJournal {
    pub fn committed_outputs(&self) -> [u8; AGGREGATE_COMMITTED_OUTPUTS_LEN] {
        aggregate_committed_outputs(&self.reduction)
    }

    pub fn journal_bytes(&self) -> [u8; AGGREGATE_JOURNAL_LEN] {
        let mut out = [0u8; AGGREGATE_JOURNAL_LEN];
        out[..32].copy_from_slice(&self.input_digest);
        out[32..].copy_from_slice(&self.committed_outputs());
        out
    }

    pub fn output_digest(&self) -> [u8; 32] {
        crate::sha256(&self.committed_outputs())
    }

    pub fn journal_hash(&self) -> [u8; 32] {
        crate::journal_hash(&self.input_digest, &self.committed_outputs())
    }
}

pub fn aggregate_committed_outputs(
    reduction: &AggregateReduction,
) -> [u8; AGGREGATE_COMMITTED_OUTPUTS_LEN] {
    let mut out = [0u8; AGGREGATE_COMMITTED_OUTPUTS_LEN];
    out[0] = reduction.combiner_id;
    out[1..33].copy_from_slice(&reduction.params_digest);
    out[33..37].copy_from_slice(&reduction.result_value.to_le_bytes());
    out[37..41].copy_from_slice(&reduction.branch_count.to_le_bytes());
    out[41..].copy_from_slice(&reduction.merkle_root);
    out
}

/// Reduce a framed artifact into its journal. This is the whole guest.
pub fn aggregate_journal(artifact: &[u8]) -> Result<AggregateJournal, AggregateError> {
    Ok(AggregateJournal {
        input_digest: framed_input_digest(artifact),
        reduction: reduce_aggregate_artifact(artifact)?,
    })
}

pub fn reduce_aggregate_artifact(artifact: &[u8]) -> Result<AggregateReduction, AggregateError> {
    let value: Value = serde_json::from_slice(artifact).map_err(|_| AggregateError::NotJson)?;
    let object = value.as_object().ok_or(AggregateError::NotAnObject)?;

    let schema = string_field(&value, "schema")?;
    if schema != AGGREGATE_SCHEMA {
        return Err(AggregateError::UnknownSchema(String::from(schema)));
    }
    let schema_version = u64_field(&value, "schema_version")?;
    if schema_version != AGGREGATE_SCHEMA_VERSION {
        return Err(AggregateError::UnknownSchemaVersion(schema_version));
    }

    let combiner_id = u64_field(&value, "combiner_id")?;
    if combiner_id > u64::from(u8::MAX) {
        return Err(AggregateError::WrongType("combiner_id"));
    }
    let combiner_id = combiner_id as u8;
    validate_combiner_id(combiner_id)?;
    // The name and the id must agree, so a reader of the artifact and the guest can
    // never be looking at two different combiners.
    let combiner_name = string_field(&value, "combiner")?;
    if combiner_name_id(combiner_name) != Some(combiner_id) {
        return Err(AggregateError::CombinerIdMismatch);
    }

    let parameters = object
        .get("combiner_parameters")
        .ok_or(AggregateError::MissingField("combiner_parameters"))?;
    if !parameters.is_object() {
        return Err(AggregateError::WrongType("combiner_parameters"));
    }
    let params = CombinerParams {
        trim_bps: optional_u32(parameters, "trim_bps")?.unwrap_or(0),
        category_dictionary_size: optional_u32(parameters, "category_dictionary_size")?
            .unwrap_or(0),
    };
    let params_digest = combiner_params_digest(combiner_id, &params);

    let branches = object
        .get("branches")
        .ok_or(AggregateError::MissingField("branches"))?
        .as_array()
        .ok_or(AggregateError::WrongType("branches"))?;
    if branches.is_empty() {
        return Err(AggregateError::BranchesEmpty);
    }
    if branches.len() > MAX_BRANCHES {
        return Err(AggregateError::TooManyBranches(branches.len()));
    }

    let mut branch_hashes: Vec<[u8; 32]> = Vec::with_capacity(branches.len());
    let mut scalars: Vec<WeightedValue> = Vec::with_capacity(branches.len());
    let mut scalar_values: Vec<i64> = Vec::with_capacity(branches.len());
    let mut votes: Vec<CategoricalVote> = Vec::with_capacity(branches.len());
    let mut previous_index: Option<u32> = None;

    for (position, branch) in branches.iter().enumerate() {
        if !branch.is_object() {
            return Err(AggregateError::WrongType("branches[]"));
        }
        let declared_index = u32_field(branch, "branch_index")?;
        if let Some(previous) = previous_index {
            if declared_index <= previous {
                return Err(AggregateError::BranchIndexNotIncreasing { position });
            }
        }
        previous_index = Some(declared_index);

        let receipt_hex = string_field(branch, "result_bytes")?;
        let receipt = decode_hex(receipt_hex).ok_or(AggregateError::BadHex("result_bytes"))?;
        let parsed = parse_branch_result_bytes(&receipt).map_err(|error| {
            AggregateError::BadReceipt {
                branch_index: declared_index,
                error,
            }
        })?;
        if parsed.branch_index != declared_index {
            return Err(AggregateError::BranchIndexMismatch {
                declared: declared_index,
                encoded: parsed.branch_index,
            });
        }
        let declared_hash: [u8; 32] = decode_hex_array(string_field(branch, "result_hash")?)
            .ok_or(AggregateError::BadHex("result_hash"))?;
        if declared_hash != parsed.result_hash {
            return Err(AggregateError::ResultHashMismatch {
                branch_index: declared_index,
            });
        }
        let weight = u64_field(branch, "weight")?;
        // A zero weight would silently drop a branch the Merkle root still counts, so
        // the reduction and the root would describe different branch sets.
        if weight == 0 {
            return Err(AggregateError::ZeroWeight {
                branch_index: declared_index,
            });
        }
        // `trimmed-mean` averages the retained values unweighted. Accepting a weight
        // it then ignores would let the artifact claim an influence the journal does
        // not reflect.
        if combiner_id == COMBINER_TRIMMED_MEAN && weight != 1 {
            return Err(AggregateError::NonUniformWeightForTrimmedMean {
                branch_index: declared_index,
            });
        }
        branch_hashes.push(parsed.result_hash);

        if combiner_id == COMBINER_MAJORITY_VOTE {
            let label = parsed
                .categorical_label_index
                .ok_or(AggregateError::MissingLabel {
                    branch_index: declared_index,
                })?;
            if params.category_dictionary_size == 0 {
                return Err(AggregateError::DictionarySizeMissing);
            }
            if u32::from(label) >= params.category_dictionary_size {
                return Err(AggregateError::LabelOutsideDictionary {
                    branch_index: declared_index,
                    label,
                });
            }
            votes.push(CategoricalVote {
                category: u32::from(label),
                weight,
            });
        } else {
            if !parsed.output_kind.carries_scalar() {
                return Err(AggregateError::MissingScalar {
                    branch_index: declared_index,
                });
            }
            let scalar = parsed.scalar_value_bps.ok_or(AggregateError::MissingScalar {
                branch_index: declared_index,
            })?;
            scalars.push(WeightedValue {
                value: i64::from(scalar),
                weight,
            });
            scalar_values.push(i64::from(scalar));
        }
    }

    let result_value = match combiner_id {
        COMBINER_WEIGHTED_MEAN => weighted_mean_bps(&scalars)?,
        COMBINER_TRIMMED_MEAN => {
            let outlier_count = trim_count_from_bps(scalar_values.len(), params.trim_bps)?;
            trimmed_mean_bps(&scalar_values, outlier_count)?
        }
        COMBINER_MAJORITY_VOTE => majority_vote(&votes)?,
        _ => return Err(AggregateError::Combiner(CombinerError::UnknownCombiner)),
    };

    let merkle_root = sorted_branches_merkle_root(&branch_hashes)?;
    Ok(AggregateReduction {
        combiner_id,
        params,
        params_digest,
        result_value,
        branch_count: branch_hashes.len() as u32,
        branch_hashes,
        merkle_root,
    })
}

fn combiner_name_id(name: &str) -> Option<u8> {
    match name {
        "weighted-mean" => Some(COMBINER_WEIGHTED_MEAN),
        "trimmed-mean" => Some(COMBINER_TRIMMED_MEAN),
        "majority-vote" => Some(COMBINER_MAJORITY_VOTE),
        _ => None,
    }
}

fn string_field<'a>(value: &'a Value, field: &'static str) -> Result<&'a str, AggregateError> {
    value
        .get(field)
        .ok_or(AggregateError::MissingField(field))?
        .as_str()
        .ok_or(AggregateError::WrongType(field))
}

fn u64_field(value: &Value, field: &'static str) -> Result<u64, AggregateError> {
    value
        .get(field)
        .ok_or(AggregateError::MissingField(field))?
        .as_u64()
        .ok_or(AggregateError::WrongType(field))
}

fn u32_field(value: &Value, field: &'static str) -> Result<u32, AggregateError> {
    let raw = u64_field(value, field)?;
    u32::try_from(raw).map_err(|_| AggregateError::WrongType(field))
}

fn optional_u32(value: &Value, field: &'static str) -> Result<Option<u32>, AggregateError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(found) => {
            let raw = found.as_u64().ok_or(AggregateError::WrongType(field))?;
            Ok(Some(u32::try_from(raw).map_err(|_| AggregateError::WrongType(field))?))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::encode_hex;
    use crate::mfb2::{OutputKind, MFB2_MAGIC, MFB2_SCHEMA_VERSION};
    use alloc::format;
    use alloc::string::ToString;
    use alloc::vec;

    fn scalar_receipt(branch_index: u32, value: u16) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MFB2_MAGIC);
        out.push(MFB2_SCHEMA_VERSION);
        out.push(OutputKind::Scalar.id());
        out.extend_from_slice(&branch_index.to_le_bytes());
        out.push(1); // FLAG_SCALAR
        out.extend_from_slice(&value.to_le_bytes());
        let mut canonical = [0u8; 32];
        canonical[0] = branch_index as u8;
        canonical[1] = (value & 0xff) as u8;
        out.extend_from_slice(&canonical);
        out
    }

    fn categorical_receipt(branch_index: u32, label: u8) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MFB2_MAGIC);
        out.push(MFB2_SCHEMA_VERSION);
        out.push(OutputKind::Categorical.id());
        out.extend_from_slice(&branch_index.to_le_bytes());
        out.push(8); // FLAG_CATEGORY
        out.push(label);
        let mut canonical = [0u8; 32];
        canonical[0] = branch_index as u8;
        canonical[1] = label;
        out.extend_from_slice(&canonical);
        out
    }

    fn branch_entry(receipt: &[u8], weight: u64) -> String {
        let parsed = parse_branch_result_bytes(receipt).unwrap();
        format!(
            "{{\"branch_index\":{},\"job\":\"job{}\",\"output_cid\":\"cid{}\",\"result_bytes\":\"{}\",\"result_hash\":\"{}\",\"weight\":{}}}",
            parsed.branch_index,
            parsed.branch_index,
            parsed.branch_index,
            encode_hex(receipt),
            encode_hex(&parsed.result_hash),
            weight
        )
    }

    fn artifact(combiner: &str, combiner_id: u8, parameters: &str, entries: &[String]) -> Vec<u8> {
        format!(
            "{{\"branches\":[{}],\"combiner\":\"{}\",\"combiner_id\":{},\"combiner_parameters\":{},\"output_schema_hash\":\"{}\",\"parent_manifest_cid\":\"cid-parent\",\"parent_run\":\"run\",\"schema\":\"MFA3\",\"schema_version\":3}}",
            entries.join(","),
            combiner,
            combiner_id,
            parameters,
            "00".repeat(32)
        )
        .into_bytes()
    }

    fn weighted_mean_artifact() -> Vec<u8> {
        artifact(
            "weighted-mean",
            1,
            "{}",
            &[
                branch_entry(&scalar_receipt(0, 4000), 1),
                branch_entry(&scalar_receipt(1, 6000), 1),
            ],
        )
    }

    #[test]
    fn reduces_a_weighted_mean_artifact() {
        let reduction = reduce_aggregate_artifact(&weighted_mean_artifact()).unwrap();
        assert_eq!(reduction.combiner_id, COMBINER_WEIGHTED_MEAN);
        assert_eq!(reduction.result_value, 5000);
        assert_eq!(reduction.branch_count, 2);
        assert_eq!(
            reduction.merkle_root,
            sorted_branches_merkle_root(&reduction.branch_hashes).unwrap()
        );
    }

    #[test]
    fn journal_layout_is_the_documented_105_bytes() {
        let journal = aggregate_journal(&weighted_mean_artifact()).unwrap();
        let bytes = journal.journal_bytes();
        assert_eq!(bytes.len(), AGGREGATE_JOURNAL_LEN);
        assert_eq!(bytes.len(), 105);
        assert_eq!(&bytes[..32], &journal.input_digest);
        assert_eq!(bytes[32], COMBINER_WEIGHTED_MEAN);
        assert_eq!(&bytes[33..65], &journal.reduction.params_digest);
        assert_eq!(&bytes[65..69], &5000u32.to_le_bytes());
        assert_eq!(&bytes[69..73], &2u32.to_le_bytes());
        assert_eq!(&bytes[73..], &journal.reduction.merkle_root);
        assert_eq!(journal.committed_outputs().len(), 73);
        assert_eq!(&bytes[32..], &journal.committed_outputs());
        assert_eq!(journal.output_digest(), crate::sha256(&journal.committed_outputs()));
        assert_eq!(
            journal.journal_hash(),
            crate::journal_hash(&journal.input_digest, &journal.committed_outputs())
        );
    }

    #[test]
    fn input_digest_covers_the_whole_artifact() {
        let first = aggregate_journal(&weighted_mean_artifact()).unwrap();
        let mut tampered = weighted_mean_artifact();
        // Change provenance the reduction never reads.
        let text = String::from_utf8(tampered.clone()).unwrap().replace("run", "runX");
        tampered = text.into_bytes();
        let second = aggregate_journal(&tampered).unwrap();
        assert_ne!(first.input_digest, second.input_digest);
        assert_ne!(first.journal_hash(), second.journal_hash());
        assert_eq!(first.committed_outputs(), second.committed_outputs());
    }

    #[test]
    fn reduces_a_trimmed_mean_artifact() {
        let entries = vec![
            branch_entry(&scalar_receipt(0, 100), 1),
            branch_entry(&scalar_receipt(1, 200), 1),
            branch_entry(&scalar_receipt(2, 300), 1),
            branch_entry(&scalar_receipt(3, 10_000), 1),
        ];
        let bytes = artifact("trimmed-mean", 2, "{\"trim_bps\":2500}", &entries);
        let reduction = reduce_aggregate_artifact(&bytes).unwrap();
        assert_eq!(reduction.params.trim_bps, 2500);
        // 10000 is the farthest from the lower median 200; the rest average to 200.
        assert_eq!(reduction.result_value, 200);
    }

    #[test]
    fn reduces_a_majority_vote_artifact() {
        let entries = vec![
            branch_entry(&categorical_receipt(0, 2), 1),
            branch_entry(&categorical_receipt(1, 1), 1),
            branch_entry(&categorical_receipt(2, 1), 1),
        ];
        let bytes = artifact(
            "majority-vote",
            3,
            "{\"category_dictionary_size\":4}",
            &entries,
        );
        let reduction = reduce_aggregate_artifact(&bytes).unwrap();
        assert_eq!(reduction.result_value, 1);
    }

    #[test]
    fn rejects_a_tampered_result_hash() {
        let text = String::from_utf8(weighted_mean_artifact()).unwrap();
        let parsed = parse_branch_result_bytes(&scalar_receipt(0, 4000)).unwrap();
        let mut wrong = parsed.result_hash;
        wrong[0] ^= 0xff;
        let tampered = text.replacen(&encode_hex(&parsed.result_hash), &encode_hex(&wrong), 1);
        assert_eq!(
            reduce_aggregate_artifact(tampered.as_bytes()),
            Err(AggregateError::ResultHashMismatch { branch_index: 0 })
        );
    }

    #[test]
    fn rejects_a_tampered_result_value() {
        // Flip the committed scalar without recomputing the receipt hash: the guest
        // rehashes the bytes it was given, so the declared hash no longer matches.
        let honest = scalar_receipt(0, 4000);
        let mut lying = honest.clone();
        lying[11..13].copy_from_slice(&9000u16.to_le_bytes());
        let text = String::from_utf8(weighted_mean_artifact()).unwrap();
        let tampered = text.replacen(&encode_hex(&honest), &encode_hex(&lying), 1);
        assert_eq!(
            reduce_aggregate_artifact(tampered.as_bytes()),
            Err(AggregateError::ResultHashMismatch { branch_index: 0 })
        );
    }

    #[test]
    fn rejects_a_branch_index_the_receipt_does_not_carry() {
        let entries = vec![
            branch_entry(&scalar_receipt(0, 4000), 1),
            // Declare index 1 but hand over the receipt of branch 2.
            branch_entry(&scalar_receipt(2, 6000), 1).replace("\"branch_index\":2", "\"branch_index\":1"),
        ];
        let bytes = artifact("weighted-mean", 1, "{}", &entries);
        assert_eq!(
            reduce_aggregate_artifact(&bytes),
            Err(AggregateError::BranchIndexMismatch {
                declared: 1,
                encoded: 2
            })
        );
    }

    #[test]
    fn rejects_a_repeated_or_reordered_branch() {
        let entries = vec![
            branch_entry(&scalar_receipt(1, 4000), 1),
            branch_entry(&scalar_receipt(0, 6000), 1),
        ];
        let bytes = artifact("weighted-mean", 1, "{}", &entries);
        assert_eq!(
            reduce_aggregate_artifact(&bytes),
            Err(AggregateError::BranchIndexNotIncreasing { position: 1 })
        );
        let duplicated = vec![
            branch_entry(&scalar_receipt(0, 4000), 1),
            branch_entry(&scalar_receipt(0, 4000), 1),
        ];
        assert_eq!(
            reduce_aggregate_artifact(&artifact("weighted-mean", 1, "{}", &duplicated)),
            Err(AggregateError::BranchIndexNotIncreasing { position: 1 })
        );
    }

    #[test]
    fn rejects_a_combiner_name_that_disagrees_with_its_id() {
        let entries = vec![branch_entry(&scalar_receipt(0, 4000), 1)];
        let bytes = artifact("weighted-mean", 2, "{}", &entries);
        assert_eq!(
            reduce_aggregate_artifact(&bytes),
            Err(AggregateError::CombinerIdMismatch)
        );
    }

    #[test]
    fn rejects_weights_the_combiner_would_ignore_or_drop() {
        let zero = vec![branch_entry(&scalar_receipt(0, 4000), 0)];
        assert_eq!(
            reduce_aggregate_artifact(&artifact("weighted-mean", 1, "{}", &zero)),
            Err(AggregateError::ZeroWeight { branch_index: 0 })
        );
        let weighted = vec![
            branch_entry(&scalar_receipt(0, 4000), 3),
            branch_entry(&scalar_receipt(1, 6000), 1),
        ];
        assert_eq!(
            reduce_aggregate_artifact(&artifact("trimmed-mean", 2, "{\"trim_bps\":0}", &weighted)),
            Err(AggregateError::NonUniformWeightForTrimmedMean { branch_index: 0 })
        );
    }

    #[test]
    fn weighted_mean_honours_the_declared_weights() {
        let entries = vec![
            branch_entry(&scalar_receipt(0, 0), 3),
            branch_entry(&scalar_receipt(1, 10_000), 1),
        ];
        let reduction =
            reduce_aggregate_artifact(&artifact("weighted-mean", 1, "{}", &entries)).unwrap();
        assert_eq!(reduction.result_value, 2500);
    }

    #[test]
    fn rejects_a_label_outside_the_committed_dictionary() {
        let entries = vec![branch_entry(&categorical_receipt(0, 7), 1)];
        let bytes = artifact(
            "majority-vote",
            3,
            "{\"category_dictionary_size\":4}",
            &entries,
        );
        assert_eq!(
            reduce_aggregate_artifact(&bytes),
            Err(AggregateError::LabelOutsideDictionary {
                branch_index: 0,
                label: 7
            })
        );
        let missing = artifact("majority-vote", 3, "{}", &entries);
        assert_eq!(
            reduce_aggregate_artifact(&missing),
            Err(AggregateError::DictionarySizeMissing)
        );
    }

    #[test]
    fn rejects_a_scalar_combiner_over_categorical_branches() {
        let entries = vec![branch_entry(&categorical_receipt(0, 1), 1)];
        assert_eq!(
            reduce_aggregate_artifact(&artifact("weighted-mean", 1, "{}", &entries)),
            Err(AggregateError::MissingScalar { branch_index: 0 })
        );
    }

    #[test]
    fn rejects_a_malformed_or_unknown_artifact() {
        assert_eq!(reduce_aggregate_artifact(b"not json"), Err(AggregateError::NotJson));
        assert_eq!(reduce_aggregate_artifact(b"[]"), Err(AggregateError::NotAnObject));
        let text = String::from_utf8(weighted_mean_artifact()).unwrap();
        assert_eq!(
            reduce_aggregate_artifact(text.replace("\"MFA3\"", "\"MFA2\"").as_bytes()),
            Err(AggregateError::UnknownSchema("MFA2".to_string()))
        );
        assert_eq!(
            reduce_aggregate_artifact(text.replace("\"schema_version\":3", "\"schema_version\":2").as_bytes()),
            Err(AggregateError::UnknownSchemaVersion(2))
        );
        assert_eq!(
            reduce_aggregate_artifact(text.replace("\"branches\":[", "\"branches\":[[],").as_bytes()),
            Err(AggregateError::WrongType("branches[]"))
        );
        let empty = artifact("weighted-mean", 1, "{}", &[]);
        assert_eq!(reduce_aggregate_artifact(&empty), Err(AggregateError::BranchesEmpty));
    }

    #[test]
    fn rejects_hex_that_is_not_the_canonical_spelling() {
        let text = String::from_utf8(weighted_mean_artifact()).unwrap();
        let receipt = scalar_receipt(0, 4000);
        let upper = encode_hex(&receipt).to_uppercase();
        let tampered = text.replacen(&encode_hex(&receipt), &upper, 1);
        assert_eq!(
            reduce_aggregate_artifact(tampered.as_bytes()),
            Err(AggregateError::BadHex("result_bytes"))
        );
    }
}
