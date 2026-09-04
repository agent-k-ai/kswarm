//! Generate the cross-language aggregate vectors.
//!
//! ```bash
//! cargo run --example emit-aggregate-vectors > ../../cli/tests/vectors/aggregate_journal_vectors.json
//! ```
//!
//! `tests/cross_language_vectors.rs` asserts this crate still produces the checked-in
//! file, and `cli/tests/test_aggregate_artifact.py` asserts the Python mirror agrees
//! with it. Regenerating the file is therefore a deliberate act: it changes what both
//! sides are pinned to.

use kswarm_bonsol_aggregate_reducer::{
    aggregate_journal, encode_hex, parse_branch_result_bytes, reduce_aggregate_artifact,
    OutputKind, MFB2_MAGIC, MFB2_SCHEMA_VERSION,
};

const FLAG_SCALAR: u8 = 1 << 0;
const FLAG_LOWER: u8 = 1 << 1;
const FLAG_UPPER: u8 = 1 << 2;
const FLAG_CATEGORY: u8 = 1 << 3;
const FLAG_SCORES: u8 = 1 << 4;

fn scalar_receipt(branch_index: u32, value: u16) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(MFB2_MAGIC);
    out.push(MFB2_SCHEMA_VERSION);
    out.push(OutputKind::Scalar.id());
    out.extend_from_slice(&branch_index.to_le_bytes());
    out.push(FLAG_SCALAR);
    out.extend_from_slice(&value.to_le_bytes());
    out.extend_from_slice(&canonical(branch_index, value));
    out
}

fn narrative_receipt(branch_index: u32, value: u16, lower: u16, upper: u16) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(MFB2_MAGIC);
    out.push(MFB2_SCHEMA_VERSION);
    out.push(OutputKind::NarrativeWithScalar.id());
    out.extend_from_slice(&branch_index.to_le_bytes());
    out.push(FLAG_SCALAR | FLAG_LOWER | FLAG_UPPER | FLAG_SCORES);
    out.extend_from_slice(&value.to_le_bytes());
    out.extend_from_slice(&lower.to_le_bytes());
    out.extend_from_slice(&upper.to_le_bytes());
    out.push(2);
    out.extend_from_slice(&[0x11, 0x22, 0x33, 0x44]);
    out.extend_from_slice(&1500u16.to_le_bytes());
    out.extend_from_slice(&[0x55, 0x66, 0x77, 0x88]);
    out.extend_from_slice(&2500u16.to_le_bytes());
    out.extend_from_slice(&canonical(branch_index, value));
    out
}

fn categorical_receipt(branch_index: u32, label: u8) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(MFB2_MAGIC);
    out.push(MFB2_SCHEMA_VERSION);
    out.push(OutputKind::Categorical.id());
    out.extend_from_slice(&branch_index.to_le_bytes());
    out.push(FLAG_CATEGORY);
    out.push(label);
    out.extend_from_slice(&canonical(branch_index, u16::from(label)));
    out
}

/// A stand-in canonical hash. The reduction never interprets it; it only has to be
/// stable so the vectors are reproducible.
fn canonical(branch_index: u32, value: u16) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[..4].copy_from_slice(&branch_index.to_le_bytes());
    out[4..6].copy_from_slice(&value.to_le_bytes());
    out[31] = 0xa5;
    out
}

fn branch_entry(receipt: &[u8], weight: u64) -> String {
    let parsed = parse_branch_result_bytes(receipt).unwrap();
    format!(
        "{{\"branch_index\":{},\"job\":\"BranchJob{}\",\"output_cid\":\"bafybranch{}\",\"result_bytes\":\"{}\",\"result_hash\":\"{}\",\"weight\":{}}}",
        parsed.branch_index,
        parsed.branch_index,
        parsed.branch_index,
        encode_hex(receipt),
        encode_hex(&parsed.result_hash),
        weight
    )
}

/// The artifact with its keys in the order `sort_keys=True` produces, so the bytes are
/// exactly what the Python builder emits.
fn artifact(combiner: &str, combiner_id: u8, parameters: &str, entries: &[String]) -> String {
    format!(
        "{{\"branches\":[{}],\"combiner\":\"{}\",\"combiner_id\":{},\"combiner_parameters\":{},\"output_schema_hash\":\"{}\",\"parent_manifest_cid\":\"bafyparent\",\"parent_run\":\"AggregateJob11111111111111111111111111111111\",\"schema\":\"MFA3\",\"schema_version\":3}}",
        entries.join(","),
        combiner,
        combiner_id,
        parameters,
        "11".repeat(32)
    )
}

fn accepted(name: &str, artifact: &str) -> String {
    let journal = aggregate_journal(artifact.as_bytes()).unwrap();
    format!(
        "{{\"name\":\"{}\",\"artifact\":{},\"input_digest\":\"{}\",\"combiner_id\":{},\"combiner_params_digest\":\"{}\",\"result_value\":{},\"branch_count\":{},\"merkle_root\":\"{}\",\"committed_outputs\":\"{}\",\"output_digest\":\"{}\",\"journal_hash\":\"{}\"}}",
        name,
        serde_json_string(artifact),
        encode_hex(&journal.input_digest),
        journal.reduction.combiner_id,
        encode_hex(&journal.reduction.params_digest),
        journal.reduction.result_value,
        journal.reduction.branch_count,
        encode_hex(&journal.reduction.merkle_root),
        encode_hex(&journal.committed_outputs()),
        encode_hex(&journal.output_digest()),
        encode_hex(&journal.journal_hash())
    )
}

fn rejected(name: &str, artifact: &str) -> String {
    let error = reduce_aggregate_artifact(artifact.as_bytes())
        .expect_err("rejection vector was accepted");
    format!(
        "{{\"name\":\"{}\",\"artifact\":{},\"rust_error\":\"{}\"}}",
        name,
        serde_json_string(artifact),
        format!("{error:?}").replace('"', "'")
    )
}

fn serde_json_string(text: &str) -> String {
    serde_json::to_string(&serde_json::Value::String(text.to_string())).unwrap()
}

fn main() {
    let mut accepted_vectors = Vec::new();
    let mut rejected_vectors = Vec::new();

    accepted_vectors.push(accepted(
        "weighted-mean-uniform",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[
                branch_entry(&scalar_receipt(0, 4000), 1),
                branch_entry(&scalar_receipt(1, 6000), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "weighted-mean-rounds-half-up",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[
                branch_entry(&scalar_receipt(0, 1), 1),
                branch_entry(&scalar_receipt(1, 2), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "weighted-mean-weighted",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[
                branch_entry(&scalar_receipt(0, 0), 3),
                branch_entry(&scalar_receipt(1, 10_000), 1),
                branch_entry(&scalar_receipt(2, 5_000), 2),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "weighted-mean-narrative-branches",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[
                branch_entry(&narrative_receipt(0, 3300, 2000, 4000), 1),
                branch_entry(&narrative_receipt(1, 6700, 6000, 8000), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "trimmed-mean-drops-one-outlier",
        &artifact(
            "trimmed-mean",
            2,
            "{\"trim_bps\":2500}",
            &[
                branch_entry(&scalar_receipt(0, 100), 1),
                branch_entry(&scalar_receipt(1, 200), 1),
                branch_entry(&scalar_receipt(2, 300), 1),
                branch_entry(&scalar_receipt(3, 10_000), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "trimmed-mean-trims-nothing",
        &artifact(
            "trimmed-mean",
            2,
            "{\"trim_bps\":1000}",
            &[
                branch_entry(&scalar_receipt(0, 1234), 1),
                branch_entry(&scalar_receipt(1, 2345), 1),
                branch_entry(&scalar_receipt(2, 3456), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "trimmed-mean-tie-rounds-half-up",
        &artifact(
            "trimmed-mean",
            2,
            "{\"trim_bps\":0}",
            &[
                branch_entry(&scalar_receipt(0, 1), 1),
                branch_entry(&scalar_receipt(1, 2), 1),
                branch_entry(&scalar_receipt(2, 2), 1),
                branch_entry(&scalar_receipt(3, 4), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "majority-vote",
        &artifact(
            "majority-vote",
            3,
            "{\"category_dictionary_size\":4}",
            &[
                branch_entry(&categorical_receipt(0, 2), 1),
                branch_entry(&categorical_receipt(1, 1), 1),
                branch_entry(&categorical_receipt(2, 1), 1),
            ],
        ),
    ));
    accepted_vectors.push(accepted(
        "majority-vote-tie-takes-the-lowest-label",
        &artifact(
            "majority-vote",
            3,
            "{\"category_dictionary_size\":8}",
            &[
                branch_entry(&categorical_receipt(0, 5), 1),
                branch_entry(&categorical_receipt(1, 3), 1),
            ],
        ),
    ));

    let honest = scalar_receipt(0, 4000);
    let mut lying = honest.clone();
    lying[11..13].copy_from_slice(&9000u16.to_le_bytes());
    let base = artifact(
        "weighted-mean",
        1,
        "{}",
        &[
            branch_entry(&honest, 1),
            branch_entry(&scalar_receipt(1, 6000), 1),
        ],
    );

    rejected_vectors.push(rejected(
        "tampered-result-bytes",
        &base.replacen(&encode_hex(&honest), &encode_hex(&lying), 1),
    ));
    rejected_vectors.push(rejected(
        "tampered-result-hash",
        &base.replacen(
            &encode_hex(&parse_branch_result_bytes(&honest).unwrap().result_hash),
            &"00".repeat(32),
            1,
        ),
    ));
    rejected_vectors.push(rejected(
        "combiner-name-disagrees-with-id",
        &base.replacen("\"combiner_id\":1", "\"combiner_id\":2", 1),
    ));
    rejected_vectors.push(rejected(
        "unknown-schema",
        &base.replacen("\"MFA3\"", "\"MFA2\"", 1),
    ));
    rejected_vectors.push(rejected(
        "unknown-schema-version",
        &base.replacen("\"schema_version\":3", "\"schema_version\":4", 1),
    ));
    rejected_vectors.push(rejected(
        "branch-index-not-increasing",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[
                branch_entry(&scalar_receipt(1, 4000), 1),
                branch_entry(&scalar_receipt(0, 6000), 1),
            ],
        ),
    ));
    rejected_vectors.push(rejected(
        "zero-weight",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[branch_entry(&scalar_receipt(0, 4000), 0)],
        ),
    ));
    rejected_vectors.push(rejected(
        "trimmed-mean-non-uniform-weight",
        &artifact(
            "trimmed-mean",
            2,
            "{\"trim_bps\":0}",
            &[
                branch_entry(&scalar_receipt(0, 4000), 2),
                branch_entry(&scalar_receipt(1, 6000), 1),
            ],
        ),
    ));
    rejected_vectors.push(rejected(
        "scalar-combiner-over-categorical-branch",
        &artifact(
            "weighted-mean",
            1,
            "{}",
            &[branch_entry(&categorical_receipt(0, 1), 1)],
        ),
    ));
    rejected_vectors.push(rejected(
        "label-outside-the-dictionary",
        &artifact(
            "majority-vote",
            3,
            "{\"category_dictionary_size\":2}",
            &[branch_entry(&categorical_receipt(0, 7), 1)],
        ),
    ));
    rejected_vectors.push(rejected(
        "majority-vote-without-a-dictionary",
        &artifact(
            "majority-vote",
            3,
            "{}",
            &[branch_entry(&categorical_receipt(0, 1), 1)],
        ),
    ));
    rejected_vectors.push(rejected("no-branches", &artifact("weighted-mean", 1, "{}", &[])));
    rejected_vectors.push(rejected(
        "uppercase-hex",
        &base.replacen(&encode_hex(&honest), &encode_hex(&honest).to_uppercase(), 1),
    ));

    println!(
        "{{\n  \"generator\": \"protocol/bonsol-aggregate-reducer/examples/emit-aggregate-vectors.rs\",\n  \"journal_layout\": \"input_digest(32) || combiner_id(1) || combiner_params_digest(32) || result_value(le u32) || branch_count(le u32) || merkle_root(32)\",\n  \"accepted\": [\n    {}\n  ],\n  \"rejected\": [\n    {}\n  ]\n}}",
        accepted_vectors.join(",\n    "),
        rejected_vectors.join(",\n    ")
    );
}
