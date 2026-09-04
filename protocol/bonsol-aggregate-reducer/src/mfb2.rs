//! The `MFB2` branch receipt encoding.
//!
//! These are the bytes a branch worker passes to `submit_receipt`, so the Solana
//! program stores `submitted_result_hash = sha256(result_bytes)` over exactly this
//! encoding. The aggregate guest therefore reads the branch values out of the bytes
//! whose hash is already on chain, rather than out of a JSON field a caller could
//! set to anything.
//!
//! Layout (`backend/app/protocol/canonical_hash.py`, `branch_output_result_bytes`):
//!
//! ```text
//! "MFB2"                                    4 bytes
//! schema_version                            u8, must be 2
//! output_kind                               u8, 1 scalar / 2 categorical / 3 narrative_with_scalar
//! branch_index                              u32 little-endian
//! flags                                     u8
//! scalar_value_bps            if flags & 1  u16 little-endian
//! scalar_confidence_lower_bps if flags & 2  u16 little-endian
//! scalar_confidence_upper_bps if flags & 4  u16 little-endian
//! categorical_label_index     if flags & 8  u8
//! narrative_scores            if flags & 16 u8 count, then count * (4-byte key hash || u16 bps)
//! canonical_hash                            32 bytes
//! ```
//!
//! Parsing is strict: an unknown magic, an unknown version, an unknown kind, an
//! unknown flag bit, a basis-point value above 10000 and any trailing byte are all
//! errors. A guest that accepted a sloppy encoding would let one branch result have
//! two spellings and therefore two hashes.

use crate::sha256;

pub const MFB2_MAGIC: &[u8; 4] = b"MFB2";
pub const MFB2_SCHEMA_VERSION: u8 = 2;
const BPS_MAX: u16 = 10_000;
const MAX_NARRATIVE_SCORES: u8 = 32;

const FLAG_SCALAR: u8 = 1 << 0;
const FLAG_LOWER: u8 = 1 << 1;
const FLAG_UPPER: u8 = 1 << 2;
const FLAG_CATEGORY: u8 = 1 << 3;
const FLAG_SCORES: u8 = 1 << 4;
const FLAG_KNOWN: u8 = FLAG_SCALAR | FLAG_LOWER | FLAG_UPPER | FLAG_CATEGORY | FLAG_SCORES;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputKind {
    Scalar,
    Categorical,
    NarrativeWithScalar,
}

impl OutputKind {
    pub fn id(self) -> u8 {
        match self {
            OutputKind::Scalar => 1,
            OutputKind::Categorical => 2,
            OutputKind::NarrativeWithScalar => 3,
        }
    }

    fn from_id(id: u8) -> Option<Self> {
        match id {
            1 => Some(OutputKind::Scalar),
            2 => Some(OutputKind::Categorical),
            3 => Some(OutputKind::NarrativeWithScalar),
            _ => None,
        }
    }

    /// Whether a scalar combiner may take a value from this kind.
    pub fn carries_scalar(self) -> bool {
        matches!(self, OutputKind::Scalar | OutputKind::NarrativeWithScalar)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mfb2Error {
    TooShort,
    BadMagic,
    UnsupportedVersion { version: u8 },
    UnknownOutputKind { id: u8 },
    UnknownFlagBits { flags: u8 },
    ValueOutOfRange,
    TooManyNarrativeScores { count: u8 },
    TrailingBytes,
}

impl core::fmt::Display for Mfb2Error {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

#[cfg(feature = "std")]
impl std::error::Error for Mfb2Error {}

/// The fields of a branch receipt an aggregate reduction reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BranchResult {
    pub branch_index: u32,
    pub output_kind: OutputKind,
    pub scalar_value_bps: Option<u16>,
    pub categorical_label_index: Option<u8>,
    pub canonical_hash: [u8; 32],
    /// `sha256(result_bytes)`: the `submitted_result_hash` the program stores.
    pub result_hash: [u8; 32],
}

pub fn parse_branch_result_bytes(data: &[u8]) -> Result<BranchResult, Mfb2Error> {
    if data.len() < 4 + 1 + 1 + 4 + 1 + 32 {
        return Err(Mfb2Error::TooShort);
    }
    if &data[..4] != MFB2_MAGIC {
        return Err(Mfb2Error::BadMagic);
    }
    let version = data[4];
    if version != MFB2_SCHEMA_VERSION {
        return Err(Mfb2Error::UnsupportedVersion { version });
    }
    let output_kind =
        OutputKind::from_id(data[5]).ok_or(Mfb2Error::UnknownOutputKind { id: data[5] })?;
    let branch_index = u32::from_le_bytes([data[6], data[7], data[8], data[9]]);
    let flags = data[10];
    if flags & !FLAG_KNOWN != 0 {
        return Err(Mfb2Error::UnknownFlagBits { flags });
    }
    let mut offset = 11usize;
    let mut scalar_value_bps = None;
    if flags & FLAG_SCALAR != 0 {
        scalar_value_bps = Some(read_bps(data, &mut offset)?);
    }
    if flags & FLAG_LOWER != 0 {
        read_bps(data, &mut offset)?;
    }
    if flags & FLAG_UPPER != 0 {
        read_bps(data, &mut offset)?;
    }
    let mut categorical_label_index = None;
    if flags & FLAG_CATEGORY != 0 {
        categorical_label_index = Some(read_u8(data, &mut offset)?);
    }
    if flags & FLAG_SCORES != 0 {
        let count = read_u8(data, &mut offset)?;
        if count == 0 || count > MAX_NARRATIVE_SCORES {
            return Err(Mfb2Error::TooManyNarrativeScores { count });
        }
        for _ in 0..count {
            // 4-byte key hash, then the basis-point value.
            if offset + 4 > data.len() {
                return Err(Mfb2Error::TooShort);
            }
            offset += 4;
            read_bps(data, &mut offset)?;
        }
    }
    if offset + 32 != data.len() {
        return Err(if offset + 32 > data.len() {
            Mfb2Error::TooShort
        } else {
            Mfb2Error::TrailingBytes
        });
    }
    let mut canonical_hash = [0u8; 32];
    canonical_hash.copy_from_slice(&data[offset..offset + 32]);
    Ok(BranchResult {
        branch_index,
        output_kind,
        scalar_value_bps,
        categorical_label_index,
        canonical_hash,
        result_hash: sha256(data),
    })
}

fn read_u8(data: &[u8], offset: &mut usize) -> Result<u8, Mfb2Error> {
    let value = *data.get(*offset).ok_or(Mfb2Error::TooShort)?;
    *offset += 1;
    Ok(value)
}

fn read_bps(data: &[u8], offset: &mut usize) -> Result<u16, Mfb2Error> {
    if *offset + 2 > data.len() {
        return Err(Mfb2Error::TooShort);
    }
    let value = u16::from_le_bytes([data[*offset], data[*offset + 1]]);
    *offset += 2;
    if value > BPS_MAX {
        return Err(Mfb2Error::ValueOutOfRange);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec::Vec;

    fn scalar_receipt(branch_index: u32, value: u16) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MFB2_MAGIC);
        out.push(MFB2_SCHEMA_VERSION);
        out.push(OutputKind::Scalar.id());
        out.extend_from_slice(&branch_index.to_le_bytes());
        out.push(FLAG_SCALAR);
        out.extend_from_slice(&value.to_le_bytes());
        out.extend_from_slice(&[0xab; 32]);
        out
    }

    #[test]
    fn parses_a_scalar_receipt() {
        let bytes = scalar_receipt(3, 6100);
        let parsed = parse_branch_result_bytes(&bytes).unwrap();
        assert_eq!(parsed.branch_index, 3);
        assert_eq!(parsed.output_kind, OutputKind::Scalar);
        assert_eq!(parsed.scalar_value_bps, Some(6100));
        assert_eq!(parsed.categorical_label_index, None);
        assert_eq!(parsed.canonical_hash, [0xab; 32]);
        assert_eq!(parsed.result_hash, crate::sha256(&bytes));
    }

    #[test]
    fn parses_a_narrative_receipt_with_scores_and_bounds() {
        let mut out = Vec::new();
        out.extend_from_slice(MFB2_MAGIC);
        out.push(MFB2_SCHEMA_VERSION);
        out.push(OutputKind::NarrativeWithScalar.id());
        out.extend_from_slice(&7u32.to_le_bytes());
        out.push(FLAG_SCALAR | FLAG_LOWER | FLAG_UPPER | FLAG_SCORES);
        out.extend_from_slice(&5000u16.to_le_bytes());
        out.extend_from_slice(&4000u16.to_le_bytes());
        out.extend_from_slice(&6000u16.to_le_bytes());
        out.push(2);
        out.extend_from_slice(&[1, 2, 3, 4]);
        out.extend_from_slice(&100u16.to_le_bytes());
        out.extend_from_slice(&[5, 6, 7, 8]);
        out.extend_from_slice(&200u16.to_le_bytes());
        out.extend_from_slice(&[0xcd; 32]);
        let parsed = parse_branch_result_bytes(&out).unwrap();
        assert_eq!(parsed.branch_index, 7);
        assert_eq!(parsed.output_kind, OutputKind::NarrativeWithScalar);
        assert_eq!(parsed.scalar_value_bps, Some(5000));
        assert!(parsed.output_kind.carries_scalar());
    }

    #[test]
    fn parses_a_categorical_receipt() {
        let mut out = Vec::new();
        out.extend_from_slice(MFB2_MAGIC);
        out.push(MFB2_SCHEMA_VERSION);
        out.push(OutputKind::Categorical.id());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.push(FLAG_CATEGORY);
        out.push(2);
        out.extend_from_slice(&[0x01; 32]);
        let parsed = parse_branch_result_bytes(&out).unwrap();
        assert_eq!(parsed.categorical_label_index, Some(2));
        assert_eq!(parsed.scalar_value_bps, None);
        assert!(!parsed.output_kind.carries_scalar());
    }

    #[test]
    fn rejects_every_malformed_encoding() {
        assert_eq!(parse_branch_result_bytes(&[]), Err(Mfb2Error::TooShort));
        let mut bad_magic = scalar_receipt(0, 1);
        bad_magic[0] = b'X';
        assert_eq!(parse_branch_result_bytes(&bad_magic), Err(Mfb2Error::BadMagic));
        let mut bad_version = scalar_receipt(0, 1);
        bad_version[4] = 1;
        assert_eq!(
            parse_branch_result_bytes(&bad_version),
            Err(Mfb2Error::UnsupportedVersion { version: 1 })
        );
        let mut bad_kind = scalar_receipt(0, 1);
        bad_kind[5] = 9;
        assert_eq!(
            parse_branch_result_bytes(&bad_kind),
            Err(Mfb2Error::UnknownOutputKind { id: 9 })
        );
        let mut bad_flags = scalar_receipt(0, 1);
        bad_flags[10] |= 0x20;
        assert_eq!(
            parse_branch_result_bytes(&bad_flags),
            Err(Mfb2Error::UnknownFlagBits { flags: 0x21 })
        );
        let out_of_range = scalar_receipt(0, 10_001);
        assert_eq!(
            parse_branch_result_bytes(&out_of_range),
            Err(Mfb2Error::ValueOutOfRange)
        );
        let mut trailing = scalar_receipt(0, 1);
        trailing.push(0);
        assert_eq!(
            parse_branch_result_bytes(&trailing),
            Err(Mfb2Error::TrailingBytes)
        );
        let mut truncated = scalar_receipt(0, 1);
        truncated.pop();
        assert_eq!(parse_branch_result_bytes(&truncated), Err(Mfb2Error::TooShort));
    }
}
