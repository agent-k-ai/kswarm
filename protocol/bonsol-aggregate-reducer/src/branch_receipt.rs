//! The branch canonicalization receipt: the off-chain proof that a branch output
//! document really encodes to the receipt bytes the worker submitted.
//!
//! # What the guest is asked to prove
//!
//! A branch worker publishes a JSON output document to IPFS and submits `MFB2`
//! receipt bytes on chain. The Solana program stores only `sha256(result_bytes)`; it
//! never sees the document. Re-execution catches a worker that invented a *forecast*.
//! It does not catch a worker whose published document and submitted receipt describe
//! different values, because a verifier that re-executes produces its own document and
//! compares hashes, and both sides of that comparison are the verifier's.
//!
//! This guest closes that gap. It is handed the branch output document and it derives
//! the receipt from it: canonical JSON of the hash preimage, the canonical hash, the
//! `MFB2` encoding, and `sha256` of that encoding. The journal says "the document I
//! was given encodes to this receipt hash and is this many bytes long". The verifier
//! binds those two numbers to the job's on-chain `submitted_result_hash` and to the
//! document it fetched, so a worker cannot publish one document and settle another.
//!
//! # Guest input (`MFBR1`)
//!
//! Canonical JSON, framed as `len le64 || bytes` like every other guest input here.
//!
//! ```json
//! {
//!   "schema": "MFBR1",
//!   "schema_version": 1,
//!   "branch_input_sha256": "<64 lowercase hex>",
//!   "branch_output": { ... the document the worker pinned ... }
//! }
//! ```
//!
//! `branch_input_sha256` is not read by the reduction; it is in the frame so that
//! `input_digest` binds the receipt to one specific branch input, and the verifier
//! recomputes the whole frame before it checks the journal.
//!
//! # Journal (68 bytes)
//!
//! | Offset | Field | Encoding |
//! | --- | --- | --- |
//! | 0..32 | `input_digest` | SHA-256 of the framed guest input |
//! | 32..64 | `result_hash` | SHA-256 of the recomputed `MFB2` receipt bytes |
//! | 64..68 | `output_len` | u32 little-endian, the canonical byte length of the document |

use alloc::string::String;
use alloc::vec::Vec;
use serde_json::Value;

use crate::canonical_json::{canonical_json_bytes, CanonicalJsonError};
use crate::framed_input_digest;
use crate::mfb2::{Mfb2Error, OutputKind, MFB2_MAGIC, MFB2_SCHEMA_VERSION};
use crate::sha256;

pub const BRANCH_RECEIPT_SCHEMA: &str = "MFBR1";
pub const BRANCH_RECEIPT_SCHEMA_VERSION: u64 = 1;
pub const BRANCH_RECEIPT_JOURNAL_LEN: usize = 32 + 32 + 4;

const BPS_MAX: u64 = 10_000;
const MAX_NARRATIVE_SCORES: usize = 32;
const MAX_RESULT_BYTES: usize = 512;

const FLAG_SCALAR: u8 = 1 << 0;
const FLAG_LOWER: u8 = 1 << 1;
const FLAG_UPPER: u8 = 1 << 2;
const FLAG_CATEGORY: u8 = 1 << 3;
const FLAG_SCORES: u8 = 1 << 4;

/// Fields the canonical hash preimage drops: the narrative text (ADR Decision 6), the
/// completion timestamp (an honest verifier re-executes later), and the receipt
/// locator (it names a proof over this very document, so it cannot be inside it).
const PREIMAGE_EXCLUDED: [&str; 3] = ["narrative_text", "completed_at_unix", "zkvm_receipt_cid"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BranchReceiptError {
    NotJson,
    NotAnObject,
    MissingField(&'static str),
    WrongType(&'static str),
    UnknownSchema(String),
    UnknownSchemaVersion(u64),
    UnknownOutputKind(String),
    ValueOutOfRange(&'static str),
    /// The kind requires a field the document does not carry.
    ShapeMismatch(&'static str),
    TooManyNarrativeScores(usize),
    ResultTooLong(usize),
    OutputTooLong(usize),
    CanonicalJson(CanonicalJsonError),
    /// The recomputed bytes did not parse back, which would mean this crate's encoder
    /// and its parser disagree.
    SelfCheck(Mfb2Error),
}

impl From<CanonicalJsonError> for BranchReceiptError {
    fn from(error: CanonicalJsonError) -> Self {
        BranchReceiptError::CanonicalJson(error)
    }
}

impl core::fmt::Display for BranchReceiptError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

#[cfg(feature = "std")]
impl std::error::Error for BranchReceiptError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BranchReceiptJournal {
    pub input_digest: [u8; 32],
    pub result_hash: [u8; 32],
    pub output_len: u32,
    /// The recomputed receipt bytes. Not committed; useful to a host-side caller.
    pub result_bytes: Vec<u8>,
    /// The recomputed canonical hash. Not committed; it is inside `result_bytes`.
    pub canonical_hash: [u8; 32],
}

impl BranchReceiptJournal {
    pub fn journal_bytes(&self) -> [u8; BRANCH_RECEIPT_JOURNAL_LEN] {
        let mut out = [0u8; BRANCH_RECEIPT_JOURNAL_LEN];
        out[..32].copy_from_slice(&self.input_digest);
        out[32..64].copy_from_slice(&self.result_hash);
        out[64..].copy_from_slice(&self.output_len.to_le_bytes());
        out
    }

    pub fn from_journal_bytes(bytes: &[u8]) -> Option<(([u8; 32], [u8; 32]), u32)> {
        if bytes.len() != BRANCH_RECEIPT_JOURNAL_LEN {
            return None;
        }
        let mut input_digest = [0u8; 32];
        input_digest.copy_from_slice(&bytes[..32]);
        let mut result_hash = [0u8; 32];
        result_hash.copy_from_slice(&bytes[32..64]);
        let output_len = u32::from_le_bytes([bytes[64], bytes[65], bytes[66], bytes[67]]);
        Some(((input_digest, result_hash), output_len))
    }
}

/// Recompute a branch receipt from a guest input payload. This is the whole guest.
///
/// `payload` is the MFBR1 document as written, unframed. The committed `input_digest`
/// is taken over the framed form (`len le64 || payload`), so the length is covered too.
pub fn branch_receipt_journal(payload: &[u8]) -> Result<BranchReceiptJournal, BranchReceiptError> {
    let input_digest = framed_input_digest(payload);
    let mut journal = recompute_branch_receipt(payload)?;
    journal.input_digest = input_digest;
    Ok(journal)
}

/// The reduction without the framing, so a host-side caller can check one document.
pub fn recompute_branch_receipt(payload: &[u8]) -> Result<BranchReceiptJournal, BranchReceiptError> {
    let value: Value = serde_json::from_slice(payload).map_err(|_| BranchReceiptError::NotJson)?;
    if !value.is_object() {
        return Err(BranchReceiptError::NotAnObject);
    }
    let schema = string_field(&value, "schema")?;
    if schema != BRANCH_RECEIPT_SCHEMA {
        return Err(BranchReceiptError::UnknownSchema(String::from(schema)));
    }
    let schema_version = u64_field(&value, "schema_version")?;
    if schema_version != BRANCH_RECEIPT_SCHEMA_VERSION {
        return Err(BranchReceiptError::UnknownSchemaVersion(schema_version));
    }
    // Present so `input_digest` binds this receipt to one branch input. Read only to
    // reject a frame that omits it.
    let _ = string_field(&value, "branch_input_sha256")?;

    let output = value
        .get("branch_output")
        .ok_or(BranchReceiptError::MissingField("branch_output"))?;
    if !output.is_object() {
        return Err(BranchReceiptError::WrongType("branch_output"));
    }

    let output_bytes = canonical_json_bytes(output)?;
    let output_len = u32::try_from(output_bytes.len())
        .map_err(|_| BranchReceiptError::OutputTooLong(output_bytes.len()))?;

    let canonical_hash = sha256(&canonical_json_bytes(&hash_preimage(output))?);
    let result_bytes = encode_branch_result_bytes(output, &canonical_hash)?;
    // The parser and the encoder must be inverse. A guest that committed bytes its own
    // parser rejects would produce a receipt no aggregate could ever consume.
    crate::mfb2::parse_branch_result_bytes(&result_bytes).map_err(BranchReceiptError::SelfCheck)?;

    Ok(BranchReceiptJournal {
        input_digest: [0u8; 32],
        result_hash: sha256(&result_bytes),
        output_len,
        result_bytes,
        canonical_hash,
    })
}

/// `BranchOutput.canonical_hash_preimage()`: the document without the two fields an
/// honest verifier cannot reproduce.
fn hash_preimage(output: &Value) -> Value {
    let mut preimage = output.clone();
    if let Some(object) = preimage.as_object_mut() {
        for field in PREIMAGE_EXCLUDED {
            object.remove(field);
        }
    }
    preimage
}

/// `branch_output_result_bytes` from `backend/app/protocol/canonical_hash.py`.
pub fn encode_branch_result_bytes(
    output: &Value,
    canonical_hash: &[u8; 32],
) -> Result<Vec<u8>, BranchReceiptError> {
    let kind_name = string_field(output, "output_kind")?;
    let kind = match kind_name {
        "scalar" => OutputKind::Scalar,
        "categorical" => OutputKind::Categorical,
        "narrative_with_scalar" => OutputKind::NarrativeWithScalar,
        other => return Err(BranchReceiptError::UnknownOutputKind(String::from(other))),
    };
    let branch_index = u32::try_from(u64_field(output, "branch_index")?)
        .map_err(|_| BranchReceiptError::ValueOutOfRange("branch_index"))?;

    let scalar_value = optional_bps(output, "scalar_value_bps")?;
    let lower = optional_bps(output, "scalar_confidence_lower_bps")?;
    let upper = optional_bps(output, "scalar_confidence_upper_bps")?;
    let category = match optional_u64(output, "categorical_label_index")? {
        None => None,
        Some(raw) => Some(
            u8::try_from(raw).map_err(|_| BranchReceiptError::ValueOutOfRange("categorical_label_index"))?,
        ),
    };
    let scores = narrative_scores(output)?;

    // The pydantic model's per-kind rules, restated so a document that could never
    // have been produced by the worker is refused rather than encoded.
    match kind {
        OutputKind::Scalar if scalar_value.is_none() => {
            return Err(BranchReceiptError::ShapeMismatch("scalar_value_bps"))
        }
        OutputKind::Categorical if category.is_none() => {
            return Err(BranchReceiptError::ShapeMismatch("categorical_label_index"))
        }
        OutputKind::NarrativeWithScalar if scores.is_empty() => {
            return Err(BranchReceiptError::ShapeMismatch("narrative_scores"))
        }
        _ => {}
    }
    if let (Some(low), Some(high)) = (lower, upper) {
        if low > high {
            return Err(BranchReceiptError::ValueOutOfRange("scalar_confidence_lower_bps"));
        }
    }

    let mut flags = 0u8;
    if scalar_value.is_some() {
        flags |= FLAG_SCALAR;
    }
    if lower.is_some() {
        flags |= FLAG_LOWER;
    }
    if upper.is_some() {
        flags |= FLAG_UPPER;
    }
    if category.is_some() {
        flags |= FLAG_CATEGORY;
    }
    if !scores.is_empty() {
        flags |= FLAG_SCORES;
    }

    let mut out = Vec::new();
    out.extend_from_slice(MFB2_MAGIC);
    out.push(MFB2_SCHEMA_VERSION);
    out.push(kind.id());
    out.extend_from_slice(&branch_index.to_le_bytes());
    out.push(flags);
    for value in [scalar_value, lower, upper].into_iter().flatten() {
        out.extend_from_slice(&value.to_le_bytes());
    }
    if let Some(label) = category {
        out.push(label);
    }
    if !scores.is_empty() {
        if scores.len() > MAX_NARRATIVE_SCORES {
            return Err(BranchReceiptError::TooManyNarrativeScores(scores.len()));
        }
        out.push(scores.len() as u8);
        for (key, value) in &scores {
            out.extend_from_slice(&sha256(key.as_bytes())[..4]);
            out.extend_from_slice(&value.to_le_bytes());
        }
    }
    out.extend_from_slice(canonical_hash);
    if out.len() > MAX_RESULT_BYTES {
        return Err(BranchReceiptError::ResultTooLong(out.len()));
    }
    Ok(out)
}

/// `narrative_scores` as sorted `(key, bps)` pairs. `serde_json`'s object is already a
/// `BTreeMap`, so iteration order is the byte order Python's `sorted(scores)` uses.
fn narrative_scores(output: &Value) -> Result<Vec<(String, u16)>, BranchReceiptError> {
    let raw = match output.get("narrative_scores") {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(found) => found
            .as_object()
            .ok_or(BranchReceiptError::WrongType("narrative_scores"))?,
    };
    let mut scores = Vec::with_capacity(raw.len());
    for (key, value) in raw {
        let bps = value
            .as_u64()
            .ok_or(BranchReceiptError::WrongType("narrative_scores"))?;
        if bps > BPS_MAX {
            return Err(BranchReceiptError::ValueOutOfRange("narrative_scores"));
        }
        scores.push((key.clone(), bps as u16));
    }
    Ok(scores)
}

fn string_field<'a>(value: &'a Value, field: &'static str) -> Result<&'a str, BranchReceiptError> {
    value
        .get(field)
        .ok_or(BranchReceiptError::MissingField(field))?
        .as_str()
        .ok_or(BranchReceiptError::WrongType(field))
}

fn u64_field(value: &Value, field: &'static str) -> Result<u64, BranchReceiptError> {
    value
        .get(field)
        .ok_or(BranchReceiptError::MissingField(field))?
        .as_u64()
        .ok_or(BranchReceiptError::WrongType(field))
}

fn optional_u64(value: &Value, field: &'static str) -> Result<Option<u64>, BranchReceiptError> {
    match value.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(found) => Ok(Some(
            found.as_u64().ok_or(BranchReceiptError::WrongType(field))?,
        )),
    }
}

fn optional_bps(value: &Value, field: &'static str) -> Result<Option<u16>, BranchReceiptError> {
    match optional_u64(value, field)? {
        None => Ok(None),
        Some(raw) if raw <= BPS_MAX => Ok(Some(raw as u16)),
        Some(_) => Err(BranchReceiptError::ValueOutOfRange(field)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::format;

    fn scalar_output() -> String {
        String::from(
            r#"{"branch_index":2,"completed_at_unix":1756900000,"llm_model":"llama3.2:3b","llm_version_hash":"ab","narrative_scores":null,"narrative_text":null,"output_kind":"scalar","parent_job":"Job111","rng_seed":7,"scalar_confidence_lower_bps":null,"scalar_confidence_upper_bps":null,"scalar_value_bps":6100,"schema_version":1,"transcript_cid":"cid-t"}"#,
        )
    }

    fn frame(output: &str) -> Vec<u8> {
        format!(
            r#"{{"branch_input_sha256":"{}","branch_output":{},"schema":"MFBR1","schema_version":1}}"#,
            "00".repeat(32),
            output
        )
        .into_bytes()
    }

    #[test]
    fn recomputes_a_scalar_receipt() {
        let journal = branch_receipt_journal(&frame(&scalar_output())).unwrap();
        let parsed = crate::mfb2::parse_branch_result_bytes(&journal.result_bytes).unwrap();
        assert_eq!(parsed.branch_index, 2);
        assert_eq!(parsed.scalar_value_bps, Some(6100));
        assert_eq!(parsed.canonical_hash, journal.canonical_hash);
        assert_eq!(journal.result_hash, sha256(&journal.result_bytes));
        assert_eq!(journal.output_len as usize, scalar_output().len());
        assert_eq!(journal.journal_bytes().len(), 68);
    }

    #[test]
    fn the_canonical_hash_ignores_narrative_text_and_the_timestamp() {
        let first = branch_receipt_journal(&frame(&scalar_output())).unwrap();
        let changed = scalar_output().replace("1756900000", "1756900999");
        let second = branch_receipt_journal(&frame(&changed)).unwrap();
        assert_eq!(first.canonical_hash, second.canonical_hash);
        assert_eq!(first.result_hash, second.result_hash);
        // The document still changed, so the frame digest and the length did not.
        assert_ne!(first.input_digest, second.input_digest);
    }

    #[test]
    fn a_changed_forecast_changes_the_receipt_hash() {
        let first = branch_receipt_journal(&frame(&scalar_output())).unwrap();
        let changed = scalar_output().replace("6100", "9100");
        let second = branch_receipt_journal(&frame(&changed)).unwrap();
        assert_ne!(first.result_hash, second.result_hash);
        assert_ne!(first.canonical_hash, second.canonical_hash);
    }

    #[test]
    fn refuses_a_document_whose_shape_the_worker_could_not_have_produced() {
        let no_scalar = scalar_output().replace("\"scalar_value_bps\":6100", "\"scalar_value_bps\":null");
        assert_eq!(
            branch_receipt_journal(&frame(&no_scalar)),
            Err(BranchReceiptError::ShapeMismatch("scalar_value_bps"))
        );
        let out_of_range = scalar_output().replace("6100", "60100");
        assert_eq!(
            branch_receipt_journal(&frame(&out_of_range)),
            Err(BranchReceiptError::ValueOutOfRange("scalar_value_bps"))
        );
        let unknown_kind = scalar_output().replace("\"scalar\"", "\"vibes\"");
        assert_eq!(
            branch_receipt_journal(&frame(&unknown_kind)),
            Err(BranchReceiptError::UnknownOutputKind(String::from("vibes")))
        );
    }

    #[test]
    fn refuses_a_frame_that_is_not_the_documented_one() {
        assert_eq!(
            branch_receipt_journal(b"not json"),
            Err(BranchReceiptError::NotJson)
        );
        let wrong_schema = String::from_utf8(frame(&scalar_output()))
            .unwrap()
            .replace("MFBR1", "MFBR0");
        assert_eq!(
            branch_receipt_journal(wrong_schema.as_bytes()),
            Err(BranchReceiptError::UnknownSchema(String::from("MFBR0")))
        );
        let no_input = format!(
            r#"{{"branch_output":{},"schema":"MFBR1","schema_version":1}}"#,
            scalar_output()
        );
        assert_eq!(
            branch_receipt_journal(no_input.as_bytes()),
            Err(BranchReceiptError::MissingField("branch_input_sha256"))
        );
    }

    #[test]
    fn narrative_scores_are_encoded_in_sorted_key_order() {
        let narrative = String::from(
            r#"{"branch_index":0,"completed_at_unix":1,"llm_model":"m","llm_version_hash":"v","narrative_scores":{"severity_bps":4000,"alpha":100},"narrative_text":"words","output_kind":"narrative_with_scalar","parent_job":"J","rng_seed":1,"scalar_confidence_lower_bps":null,"scalar_confidence_upper_bps":null,"scalar_value_bps":5000,"schema_version":1,"transcript_cid":"c"}"#,
        );
        let journal = branch_receipt_journal(&frame(&narrative)).unwrap();
        let bytes = &journal.result_bytes;
        // magic(4) version(1) kind(1) index(4) flags(1) scalar(2) count(1)
        let count_offset = 4 + 1 + 1 + 4 + 1 + 2;
        assert_eq!(bytes[count_offset], 2);
        let first_key = &bytes[count_offset + 1..count_offset + 5];
        assert_eq!(first_key, &sha256(b"alpha")[..4]);
        let second_key = &bytes[count_offset + 7..count_offset + 11];
        assert_eq!(second_key, &sha256(b"severity_bps")[..4]);
    }
}
