//! The checked-in aggregate vectors, asserted against this crate.
//!
//! `cli/tests/test_aggregate_artifact.py` asserts the Python mirror against the same
//! file. Between them, the guest and the aggregator cannot drift without a test
//! failing: a change to either reduction changes the file, and the other side then
//! disagrees with it.
//!
//! Regenerate with:
//!
//! ```bash
//! cargo run --example emit-aggregate-vectors > ../../cli/tests/vectors/aggregate_journal_vectors.json
//! ```

use std::{fs, path::PathBuf};

use kswarm_bonsol_aggregate_reducer::{aggregate_journal, encode_hex, reduce_aggregate_artifact};
use serde_json::Value;

fn vectors() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../cli/tests/vectors/aggregate_journal_vectors.json");
    let bytes = fs::read(&path)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
    serde_json::from_slice(&bytes).expect("vectors file is not JSON")
}

#[test]
fn every_accepted_vector_reduces_to_its_recorded_journal() {
    let vectors = vectors();
    let accepted = vectors["accepted"].as_array().expect("accepted is an array");
    assert!(!accepted.is_empty(), "no accepted vectors");
    for vector in accepted {
        let name = vector["name"].as_str().unwrap();
        let artifact = vector["artifact"].as_str().unwrap();
        let journal = aggregate_journal(artifact.as_bytes())
            .unwrap_or_else(|error| panic!("{name}: rejected: {error:?}"));
        assert_eq!(
            encode_hex(&journal.input_digest),
            vector["input_digest"].as_str().unwrap(),
            "{name}: input_digest"
        );
        assert_eq!(
            u64::from(journal.reduction.combiner_id),
            vector["combiner_id"].as_u64().unwrap(),
            "{name}: combiner_id"
        );
        assert_eq!(
            encode_hex(&journal.reduction.params_digest),
            vector["combiner_params_digest"].as_str().unwrap(),
            "{name}: combiner_params_digest"
        );
        assert_eq!(
            u64::from(journal.reduction.result_value),
            vector["result_value"].as_u64().unwrap(),
            "{name}: result_value"
        );
        assert_eq!(
            u64::from(journal.reduction.branch_count),
            vector["branch_count"].as_u64().unwrap(),
            "{name}: branch_count"
        );
        assert_eq!(
            encode_hex(&journal.reduction.merkle_root),
            vector["merkle_root"].as_str().unwrap(),
            "{name}: merkle_root"
        );
        assert_eq!(
            encode_hex(&journal.committed_outputs()),
            vector["committed_outputs"].as_str().unwrap(),
            "{name}: committed_outputs"
        );
        assert_eq!(
            encode_hex(&journal.output_digest()),
            vector["output_digest"].as_str().unwrap(),
            "{name}: output_digest"
        );
        assert_eq!(
            encode_hex(&journal.journal_hash()),
            vector["journal_hash"].as_str().unwrap(),
            "{name}: journal_hash"
        );
    }
}

#[test]
fn every_rejected_vector_is_still_rejected() {
    let vectors = vectors();
    let rejected = vectors["rejected"].as_array().expect("rejected is an array");
    assert!(!rejected.is_empty(), "no rejection vectors");
    for vector in rejected {
        let name = vector["name"].as_str().unwrap();
        let artifact = vector["artifact"].as_str().unwrap();
        let error = reduce_aggregate_artifact(artifact.as_bytes())
            .expect_err(&format!("{name}: accepted an artifact the vectors reject"));
        assert_eq!(
            format!("{error:?}").replace('"', "'"),
            vector["rust_error"].as_str().unwrap(),
            "{name}: rejection reason changed"
        );
    }
}
