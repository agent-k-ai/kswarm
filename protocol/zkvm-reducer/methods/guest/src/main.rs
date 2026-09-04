use risc0_zkvm::guest::env;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Serialize, Deserialize)]
struct ReducerInput {
    branch_key: String,
    child_job_id: String,
    line_count: u32,
    parent_request_id: String,
    score_hex: String,
    word_count: u32,
}

#[derive(Debug, Serialize, Deserialize)]
struct ReducerJournal {
    branch_key: String,
    child_job_id: String,
    line_count: u32,
    parent_request_id: String,
    reducer_digest: String,
    score_hex: String,
    word_count: u32,
}

fn main() {
    let input: ReducerInput = env::read();
    let mut hasher = Sha256::new();
    hasher.update(input.branch_key.as_bytes());
    hasher.update(input.child_job_id.as_bytes());
    hasher.update(input.parent_request_id.as_bytes());
    hasher.update(input.score_hex.as_bytes());
    hasher.update(input.line_count.to_le_bytes());
    hasher.update(input.word_count.to_le_bytes());
    let journal = ReducerJournal {
        branch_key: input.branch_key,
        child_job_id: input.child_job_id,
        line_count: input.line_count,
        parent_request_id: input.parent_request_id,
        reducer_digest: format!("{:x}", hasher.finalize()),
        score_hex: input.score_hex,
        word_count: input.word_count,
    };
    env::commit(&journal);
}
